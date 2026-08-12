import json
import os, re, sys, subprocess, platform
import shutil
import tarfile
import time
from distutils import log
from contextlib import closing, contextmanager
from ftplib import FTP

try:
    from urllib.parse import urljoin, unquote, urlparse
    from urllib.request import urlretrieve, urlopen, urlcleanup, Request
except ImportError:  # Py2
    from urlparse import urljoin, unquote, urlparse
    from urllib import urlretrieve, urlcleanup
    from urllib2 import urlopen, Request

multi_make_options = []
try:
    import multiprocessing
    cpus = multiprocessing.cpu_count()
    if cpus > 1:
        if cpus > 5:
            cpus = 5
        multi_make_options = ['-j%d' % (cpus+1)]
except:
    pass


# overridable to control script usage
sys_platform = sys.platform


# use pre-built libraries on Windows

def download_and_extract_windows_binaries(destdir):
    # The published cp27 wheels link MSVCR90.dll, so the Python 2.7 legs compile
    # with VS 9.0 and need prebuilt static libs built against that same CRT.
    # Only Python 2.7 can be that interpreter here, hence the version check.
    use_vs2008 = sys.version_info[0] < 3

    if use_vs2008:
        # 2023.03.26 is the LAST libxml2-win-binaries release that still ships a
        # complete 'vs2008' set (iconv 1.14, libxml2 2.10.3, libxslt 1.1.37,
        # zlib 1.2.12, both bitnesses), so the tag has to be pinned: the
        # newest-releases scan below reaches back only a few releases and never
        # sees it.  Its libxml2 2.10.3 is also the LIBXML2_VERSION the wheel
        # workflow declares, and its objects carry no LTCG, so linking them with
        # the VS 9.0 compiler cannot raise "fatal error C1047".
        release_tag = "2023.03.26"
        release, _ = read_url(
            "https://api.github.com/repos/lxml/libxml2-win-binaries/releases/tags/%s" % release_tag,
            accept="application/vnd.github+json",
            as_json=True,
            github_api_token=os.environ.get("GITHUB_API_TOKEN"),
        )
    else:
        # Python 3 builds with the ambient MSVC 14.44 toolset, so it needs the
        # newest, modern-MSVC drop; pairing that toolset with a vs2008 drop is
        # what would break the link-time code generation pass.
        url = "https://api.github.com/repos/lxml/libxml2-win-binaries/releases?per_page=5"
        releases, _ = read_url(
            url,
            accept="application/vnd.github+json",
            as_json=True,
            github_api_token=os.environ.get("GITHUB_API_TOKEN"),
        )

        release = {'tag_name': ''}
        for candidate in releases:
            if release['tag_name'] < candidate.get('tag_name', ''):
                release = candidate
        release_tag = release['tag_name']

    print('Using libxml2-win-binaries release %s (%s build)' % (
        release_tag, 'vs2008' if use_vs2008 else 'modern MSVC'))

    url = "https://github.com/lxml/libxml2-win-binaries/releases/download/%s/" % release_tag
    filenames = [asset['name'] for asset in release.get('assets', ())]

    # Check for native ARM64 build or the environment variable that is set by
    # Visual Studio for cross-compilation (same variable as setuptools uses)
    if platform.machine() == 'ARM64' or os.getenv('VSCMD_ARG_TGT_ARCH') == 'arm64':
        arch = "win-arm64"
    elif sys.maxsize > 2**32:
        arch = "win64"
    else:
        arch = "win32"

    # Assets are named '<lib>-<version>[.vs2008].<arch>.zip', so the variant token
    # sits between version and architecture and both must be matched together:
    # the plain '.<arch>.' substring would otherwise also accept the vs2008 asset
    # on a Python 3 leg (and vice versa).  find_max_version() is unaffected -
    # its version pattern stops at the first non-digit group, so 'vs2008' can
    # never be read as part of a version number.
    variant_part = '.vs2008' if use_vs2008 else ''
    arch_part = variant_part + '.' + arch + '.'
    filenames = [
        filename for filename in filenames
        if arch_part in filename and (use_vs2008 or '.vs2008.' not in filename)
    ]

    libs = {}
    for libname in ['libxml2', 'libxslt', 'zlib', 'iconv']:
        libs[libname] = "%s-%s%s.%s.zip" % (
            libname,
            find_max_version(libname, filenames),
            variant_part,
            arch,
        )

    if not os.path.exists(destdir):
        os.makedirs(destdir)

    for libname, libfn in libs.items():
        srcfile = urljoin(url, libfn)
        destfile = os.path.join(destdir, libfn)
        if os.path.exists(destfile + ".keep"):
            print('Using local copy of  "{}"'.format(srcfile))
        else:
            print('Retrieving "%s" to "%s"' % (srcfile, destfile))
            # github.com intermittently drops the connection mid-response on the
            # hosted Windows runners (http.client.RemoteDisconnected), which took
            # out three otherwise-identical legs of one matrix run.  Retry with
            # linear backoff, unlinking any partial/error body first so the next
            # attempt cannot unpack a truncated zip.  github.com also 503s this
            # asset when ~20 Windows legs of the matrix request it at once, so
            # back off for minutes, not seconds.
            WINDOWS_DOWNLOAD_ATTEMPTS = 8
            for attempt in range(1, WINDOWS_DOWNLOAD_ATTEMPTS + 1):
                try:
                    if os.path.exists(destfile):
                        os.unlink(destfile)
                    urlcleanup()  # work around FTP bug 27973 in Py2.7.12+
                    urlretrieve(srcfile, destfile)
                    break
                except Exception as exc:
                    if attempt == WINDOWS_DOWNLOAD_ATTEMPTS:
                        raise
                    print('Download of "%s" failed (attempt %d/%d): %s' % (
                        srcfile, attempt, WINDOWS_DOWNLOAD_ATTEMPTS, exc))
                    sys.stdout.flush()
                    time.sleep(15 * attempt)
        d = unpack_zipfile(destfile, destdir)
        duplicate_pdb_files(d)
        libs[libname] = d

    return libs


def duplicate_pdb_files(extracted_dir):
    """Make the debug info of the prebuilt Windows libs reachable under the name
    that the object files actually reference.

    The libiconv objects in the libxml2-win-binaries drop were compiled with
    '...\\libiconv\\MSVC17\\x64\\lib\\libiconv_a.pdb', but the released zip file
    ships that file as 'iconv_a.pdb'.  The linker therefore fails with
    "fatal error C1090: PDB API call failed, error code '5'" on the missing
    'libiconv_a.pdb'.  Provide a copy under the referenced name.  Never delete
    any '*.pdb' - the linker wants them.
    """
    lib_dir = os.path.join(extracted_dir, 'lib')
    if not os.path.isdir(lib_dir):
        return
    for filename in os.listdir(lib_dir):
        if not filename.endswith('_a.pdb') or filename.startswith('lib'):
            continue
        source = os.path.join(lib_dir, filename)
        target = os.path.join(lib_dir, 'lib' + filename)
        if os.path.exists(target):
            print('Keeping existing "%s"' % target)
            continue
        try:
            shutil.copy2(source, target)
        except Exception as exc:
            print('Failed to copy "%s" to "%s": %s' % (source, target, exc))
        else:
            print('Copied "%s" to "%s"' % (source, target))


def find_top_dir_of_zipfile(zipfile):
    topdir = None
    files = [f.filename for f in zipfile.filelist]
    dirs = [d for d in files if d.endswith('/')]
    if dirs:
        dirs.sort(key=len)
        topdir = dirs[0]
        topdir = topdir[:topdir.index("/")+1]
        for path in files:
            if not path.startswith(topdir):
                topdir = None
                break
    assert topdir, (
        "cannot determine single top-level directory in zip file %s" %
        zipfile.filename)
    return topdir.rstrip('/')


def unpack_zipfile(zipfn, destdir):
    assert zipfn.endswith('.zip')
    import zipfile
    print('Unpacking %s into %s' % (os.path.basename(zipfn), destdir))
    f = zipfile.ZipFile(zipfn)
    try:
        extracted_dir = os.path.join(destdir, find_top_dir_of_zipfile(f))
        f.extractall(path=destdir)
    finally:
        f.close()
    assert os.path.exists(extracted_dir), 'missing: %s' % extracted_dir
    return extracted_dir


def get_prebuilt_libxml2xslt(download_dir, static_include_dirs, static_library_dirs):
    assert sys_platform.startswith('win')
    libs = download_and_extract_windows_binaries(download_dir)
    for libname, path in libs.items():
        i = os.path.join(path, 'include')
        l = os.path.join(path, 'lib')
        assert os.path.exists(i), 'does not exist: %s' % i
        assert os.path.exists(l), 'does not exist: %s' % l
        static_include_dirs.append(i)
        static_library_dirs.append(l)


## Routines to download and build libxml2/xslt from sources:

LIBXML2_LOCATION = 'https://download.gnome.org/sources/libxml2/'
LIBXSLT_LOCATION = 'https://download.gnome.org/sources/libxslt/'
# ftp.gnu.org is a single point of failure: under CI-scale parallel load some
# runners cannot reach it at all ("[Errno 101] Network is unreachable") and burn
# every retry on the same host.  List GNU mirrors serving the identical tarball
# so a leg can fail over instead of failing.
LIBICONV_LOCATIONS = (
    'https://ftp.gnu.org/pub/gnu/libiconv/',
    'https://mirrors.kernel.org/gnu/libiconv/',
    'https://ftpmirror.gnu.org/libiconv/',
)
# 'https://zlib.net/' only serves the CURRENT release, so a pinned version is a
# guaranteed 404 there: try the fossils archive and the GitHub release assets
# first and keep zlib.net as the last resort.
ZLIB_LOCATIONS = (
    'https://zlib.net/fossils/',
    'https://github.com/madler/zlib/releases/download/v%s/',
    'https://zlib.net/',
)
# The published lxml 4.9.4 wheels embed zlib 1.3 and libiconv 1.17.
# zlib 1.3's pre-1.3.1 'fdopen' macro breaks the current macOS SDK
# ("_stdio.h:322:7: error: expected identifier or '('"), so build 1.3.1 there.
ZLIB_VERSION = '1.3.1' if sys.platform == 'darwin' else '1.3'
LIBICONV_VERSION = '1.17'
match_libfile_version = re.compile('^[^-]*-([.0-9-]+)[.].*').match

# Magic bytes of the archive formats we download (gzip, bzip2, xz, zip).
ARCHIVE_MAGIC_NUMBERS = (b'\x1f\x8b', b'BZh', b'\xfd7zXZ\x00', b'PK\x03\x04')
DOWNLOAD_ATTEMPTS = 4


def _is_archive_file(filename):
    """Verify by magic bytes that a downloaded file really is an archive.

    Both zlib.net (under CI-scale parallel load) and a failed urlretrieve() can
    leave an HTML error body on disk under the archive's name.
    """
    try:
        with open(filename, 'rb') as f:
            header = f.read(8)
    except (IOError, OSError):
        return False
    return any(header.startswith(magic) for magic in ARCHIVE_MAGIC_NUMBERS)


def _find_content_encoding(response, default='iso8859-1'):
    from email.message import Message
    content_type = response.headers.get('Content-Type')
    if content_type:
        msg = Message()
        msg.add_header('Content-Type', content_type)
        charset = msg.get_content_charset(default)
    else:
        charset = default
    return charset


def remote_listdir(url):
    try:
        return _list_dir_urllib(url)
    except IOError:
        assert url.lower().startswith('ftp://')
        print("Requesting with urllib failed. Falling back to ftplib. "
              "Proxy argument will be ignored for %s" % url)
        return _list_dir_ftplib(url)


def _list_dir_ftplib(url):
    parts = urlparse(url)
    ftp = FTP(parts.netloc)
    try:
        ftp.login()
        ftp.cwd(parts.path)
        data = []
        ftp.dir(data.append)
    finally:
        ftp.quit()
    return parse_text_ftplist("\n".join(data))


def read_url(url, decode=True, accept=None, as_json=False, github_api_token=None):
    headers = {'User-Agent': 'https://github.com/lxml/lxml'}
    if accept:
        headers['Accept'] = accept
    if github_api_token:
        headers['authorization'] = "Bearer " + github_api_token
    request = Request(url, headers=headers)

    with closing(urlopen(request)) as res:
        charset = _find_content_encoding(res)
        content_type = res.headers.get('Content-Type')
        data = res.read()

    if decode:
        data = data.decode(charset)
    if as_json:
        data = json.loads(data)
    return data, content_type


def _list_dir_urllib(url):
    data, content_type = read_url(url)
    if content_type and content_type.startswith('text/html'):
        files = parse_html_filelist(data)
    else:
        files = parse_text_ftplist(data)
    return files


def http_find_latest_version_directory(url, version=None):
    data, _ = read_url(url)
    # e.g. <a href="1.0/">
    directories = [
        (int(v[0]), int(v[1]))
        for v in re.findall(r' href=["\']([0-9]+)\.([0-9]+)/?["\']', data)
    ]
    if not directories:
        return url
    best_version = max(directories)
    if version:
        major, minor, _ = version.split(".", 2)
        major, minor = int(major), int(minor)
        if (major, minor) in directories:
            best_version = (major, minor)
    latest_dir = "%s.%s" % best_version
    return urljoin(url, latest_dir) + "/"


def http_listfiles(url, re_pattern):
    data, _ = read_url(url)
    files = re.findall(re_pattern, data)
    return files


def parse_text_ftplist(s):
    for line in s.splitlines():
        if not line.startswith('d'):
            # -rw-r--r--   1 ftp      ftp           476 Sep  1  2011 md5sum.txt
            # Last (9th) element is 'md5sum.txt' in the above example, but there
            # may be variations, so we discard only the first 8 entries.
            yield line.split(None, 8)[-1]


def parse_html_filelist(s):
    re_href = re.compile(
        r'''<a[^>]*\shref=["']([^;?"']+?)[;?"']''',
        re.I|re.M)
    links = set(re_href.findall(s))
    for link in links:
        if not link.endswith('/'):
            yield unquote(link)


def tryint(s):
    try:
        return int(s)
    except ValueError:
        return s


@contextmanager
def py2_tarxz(filename):
    import tempfile
    with tempfile.TemporaryFile() as tmp:
        subprocess.check_call(["xz", "-dc", filename], stdout=tmp.fileno())
        tmp.seek(0)
        with closing(tarfile.TarFile(fileobj=tmp)) as tf:
            yield tf


def download_libxml2(dest_dir, version=None):
    """Downloads libxml2, returning the filename where the library was downloaded"""
    #version_re = re.compile(r'LATEST_LIBXML2_IS_([0-9.]+[0-9](?:-[abrc0-9]+)?)')
    version_re = re.compile(r'libxml2-([0-9.]+[0-9]).tar.xz')
    filename = 'libxml2-%s.tar.xz'

    if version == "2.9.12":
        # Temporarily using the latest master (2.9.12+) until there is a release that supports lxml again.
        from_location = "https://gitlab.gnome.org/GNOME/libxml2/-/archive/dea91c97debeac7c1aaf9c19f79029809e23a353/"
        version = "dea91c97debeac7c1aaf9c19f79029809e23a353"
    else:
        from_location = http_find_latest_version_directory(LIBXML2_LOCATION, version=version)

    return download_library(dest_dir, from_location, 'libxml2',
                            version_re, filename, version=version)


def download_libxslt(dest_dir, version=None):
    """Downloads libxslt, returning the filename where the library was downloaded"""
    #version_re = re.compile(r'LATEST_LIBXSLT_IS_([0-9.]+[0-9](?:-[abrc0-9]+)?)')
    version_re = re.compile(r'libxslt-([0-9.]+[0-9]).tar.xz')
    filename = 'libxslt-%s.tar.xz'
    from_location = http_find_latest_version_directory(LIBXSLT_LOCATION, version=version)
    return download_library(dest_dir, from_location, 'libxslt',
                            version_re, filename, version=version)


def download_libiconv(dest_dir, version=None):
    """Downloads libiconv, returning the filename where the library was downloaded"""
    version_re = re.compile(r'libiconv-([0-9.]+[0-9]).tar.gz')
    filename = 'libiconv-%s.tar.gz'
    if version is None:
        version = LIBICONV_VERSION
    return download_library(dest_dir, LIBICONV_LOCATIONS, 'libiconv',
                            version_re, filename, version=version)


def download_zlib(dest_dir, version):
    """Downloads zlib, returning the filename where the library was downloaded"""
    version_re = re.compile(r'zlib-([0-9.]+[0-9]).tar.gz')
    filename = 'zlib-%s.tar.gz'
    if version is None:
        version = ZLIB_VERSION
    return download_library(dest_dir, ZLIB_LOCATIONS, 'zlib',
                            version_re, filename, version=version)


def find_max_version(libname, filenames, version_re=None):
    if version_re is None:
        version_re = re.compile(r'%s-([0-9.]+[0-9](?:-[abrc0-9]+)?)' % libname)
    versions = []
    for fn in filenames:
        match = version_re.search(fn)
        if match:
            version_string = match.group(1)
            versions.append((tuple(map(tryint, version_string.replace("-", ".-").split('.'))),
                             version_string))
    if not versions:
        raise Exception(
            "Could not find the most current version of %s from the files: %s" % (
                libname, filenames))
    versions.sort()
    version_string = versions[-1][-1]
    print('Latest version of %s is %s' % (libname, version_string))
    return version_string


def download_library(dest_dir, location, name, version_re, filename, version=None):
    # 'location' may be a single URL or a sequence of mirrors to try in order.
    locations = list(location) if isinstance(location, (list, tuple)) else [location]
    if version is None:
        list_location = locations[0]
        try:
            if list_location.startswith('ftp://'):
                fns = remote_listdir(list_location)
            else:
                print(list_location)
                fns = http_listfiles(list_location, '(%s)' % filename.replace('%s', '(?:[0-9.]+[0-9])'))
            version = find_max_version(name, fns, version_re)
        except IOError:
            # network failure - maybe we have the files already?
            latest = (0,0,0)
            fns = os.listdir(dest_dir)
            for fn in fns:
                if fn.startswith(name+'-'):
                    match = match_libfile_version(fn)
                    if match:
                        version_tuple = tuple(map(tryint, match.group(1).split('.')))
                        if version_tuple > latest:
                            latest = version_tuple
                            filename = fn
                            version = None
            if latest == (0,0,0):
                raise
    if version:
        filename = filename % version
    dest_filename = os.path.join(dest_dir, filename)
    if os.path.exists(dest_filename) and _is_archive_file(dest_filename):
        print(('Using existing %s downloaded into %s '
               '(delete this file if you want to re-download the package)') % (
            name, dest_filename))
        return dest_filename

    # NOTE: the retries must wrap the download itself, not the version listing:
    # with a pinned version the listing above is skipped entirely.
    last_error = None
    for loc in locations:
        if '%s' in loc:
            if not version:
                continue  # this mirror needs the version in its URL
            loc = loc % version
        full_url = urljoin(loc, filename)
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            # A failed urlretrieve() leaves the HTTP error body behind under the
            # archive's name, which would look like a reusable download later on.
            if os.path.exists(dest_filename):
                try:
                    os.unlink(dest_filename)
                except OSError as exc:
                    print('Failed to remove "%s": %s' % (dest_filename, exc))
            print('Downloading %s into %s from %s (attempt %d/%d)' % (
                name, dest_filename, full_url, attempt, DOWNLOAD_ATTEMPTS))
            try:
                urlcleanup()  # work around FTP bug 27973 in Py2.7.12
                urlretrieve(full_url, dest_filename)
            except Exception as exc:
                last_error = exc
                print('Download failed: %s' % exc)
            else:
                if _is_archive_file(dest_filename):
                    return dest_filename
                # Some mirrors answer with an error page and a 200 status.
                last_error = IOError(
                    'Downloaded file from %s is not an archive file' % full_url)
                print('Download failed: %s' % last_error)
            time.sleep(attempt * 2)

    if os.path.exists(dest_filename):
        try:
            os.unlink(dest_filename)
        except OSError:
            pass
    if last_error is None:
        last_error = IOError('Failed to download %s from %s' % (filename, locations))
    raise last_error


def unpack_tarball(tar_filename, dest):
    print('Unpacking %s into %s' % (os.path.basename(tar_filename), dest))
    if sys.version_info[0] < 3 and tar_filename.endswith('.xz'):
        # Py 2.7 lacks lzma support
        tar_cm = py2_tarxz(tar_filename)
    else:
        tar_cm = closing(tarfile.open(tar_filename))

    base_dir = None
    with tar_cm as tar:
        for member in tar:
            base_name = member.name.split('/')[0]
            if base_dir is None:
                base_dir = base_name
            elif base_dir != base_name:
                print('Unexpected path in %s: %s' % (tar_filename, base_name))
        tar.extractall(dest)
    return os.path.join(dest, base_dir)


def call_subprocess(cmd, **kw):
    import subprocess
    cwd = kw.get('cwd', '.')
    cmd_desc = ' '.join(cmd)
    log.info('Running "%s" in %s' % (cmd_desc, cwd))
    returncode = subprocess.call(cmd, **kw)
    if returncode:
        raise Exception('Command "%s" returned code %s' % (cmd_desc, returncode))


def safe_mkdir(dir):
    if not os.path.exists(dir):
        os.makedirs(dir)


def cmmi(configure_cmd, build_dir, multicore=None, **call_setup):
    print('Starting build in %s' % build_dir)
    call_subprocess(configure_cmd, cwd=build_dir, **call_setup)
    if not multicore:
        make_jobs = multi_make_options
    elif int(multicore) > 1:
        make_jobs = ['-j%s' % multicore]
    else:
        make_jobs = []
    call_subprocess(
        ['make'] + make_jobs,
        cwd=build_dir, **call_setup)
    call_subprocess(
        ['make'] + make_jobs + ['install'],
        cwd=build_dir, **call_setup)


def configure_darwin_env(env_setup):
    import platform
    # configure target architectures on MacOS-X (x86_64 + Arm64, by default)
    major_version, minor_version = tuple(map(int, platform.mac_ver()[0].split('.')[:2]))
    if major_version >= 11:
        env_default = {
            'CFLAGS': "-arch x86_64 -arch arm64 -O3",
            'LDFLAGS': "-arch x86_64 -arch arm64",
            'MACOSX_DEPLOYMENT_TARGET': "11.0"
        }
        env_default.update(os.environ)
        env_setup['env'] = env_default


def build_libxml2xslt(download_dir, build_dir,
                      static_include_dirs, static_library_dirs,
                      static_cflags, static_binaries,
                      libxml2_version=None,
                      libxslt_version=None,
                      libiconv_version=None,
                      zlib_version=None,
                      multicore=None):
    safe_mkdir(download_dir)
    safe_mkdir(build_dir)
    zlib_dir = unpack_tarball(download_zlib(download_dir, zlib_version), build_dir)
    libiconv_dir = unpack_tarball(download_libiconv(download_dir, libiconv_version), build_dir)
    libxml2_dir  = unpack_tarball(download_libxml2(download_dir, libxml2_version), build_dir)
    libxslt_dir  = unpack_tarball(download_libxslt(download_dir, libxslt_version), build_dir)
    prefix = os.path.join(os.path.abspath(build_dir), 'libxml2')
    lib_dir = os.path.join(prefix, 'lib')
    safe_mkdir(prefix)

    lib_names = ['libxml2', 'libexslt', 'libxslt', 'iconv', 'libz']
    existing_libs = {
        lib: os.path.join(lib_dir, filename)
        for lib in lib_names
        for filename in os.listdir(lib_dir)
        if lib in filename and filename.endswith('.a')
    } if os.path.isdir(lib_dir) else {}

    def has_current_lib(name, build_dir, _build_all_following=[False]):
        if _build_all_following[0]:
            return False  # a dependency was rebuilt => rebuilt this lib as well
        lib_file = existing_libs.get(name)
        found = lib_file and os.path.getmtime(lib_file) > os.path.getmtime(build_dir)
        if found:
            print("Found pre-built '%s'" % name)
        else:
            # also rebuild all following libs (which may depend on this one)
            _build_all_following[0] = True
        return found

    call_setup = {}
    if sys_platform == 'darwin':
        configure_darwin_env(call_setup)

    configure_cmd = ['./configure',
                     '--disable-dependency-tracking',
                     '--disable-shared',
                     '--prefix=%s' % prefix,
                     ]

    # build zlib
    zlib_configure_cmd = [
        './configure',
        '--prefix=%s' % prefix,
    ]
    if not has_current_lib("libz", zlib_dir):
        cmmi(zlib_configure_cmd, zlib_dir, multicore, **call_setup)

    # build libiconv
    if not has_current_lib("iconv", libiconv_dir):
        cmmi(configure_cmd, libiconv_dir, multicore, **call_setup)

    # build libxml2
    libxml2_configure_cmd = configure_cmd + [
        '--without-python',
        '--with-iconv=%s' % prefix,
        '--with-zlib=%s' % prefix,
    ]

    if not libxml2_version:
        libxml2_version = os.path.basename(libxml2_dir).split('-', 1)[-1]

    if tuple(map(tryint, libxml2_version.split('-', 1)[0].split('.'))) >= (2, 9, 5):
        libxml2_configure_cmd.append('--without-lzma')  # can't currently build that

    try:
        if tuple(map(tryint, libxml2_version.split('-', 1)[0].split('.'))) >= (2, 7, 3):
            libxml2_configure_cmd.append('--enable-rebuild-docs=no')
    except Exception:
        pass # this isn't required, so ignore any errors
    if not has_current_lib("libxml2", libxml2_dir):
        if not os.path.exists(os.path.join(libxml2_dir, "configure")):
            # Allow building from git sources by running autoconf etc.
            libxml2_configure_cmd[0] = "./autogen.sh"
        cmmi(libxml2_configure_cmd, libxml2_dir, multicore, **call_setup)

    # Fix up libxslt configure script (needed up to and including 1.1.34)
    # https://gitlab.gnome.org/GNOME/libxslt/-/commit/90c34c8bb90e095a8a8fe8b2ce368bd9ff1837cc
    with open(os.path.join(libxslt_dir, "configure"), 'rb') as f:
        config_script = f.read()
    if b' --libs print ' in config_script:
        config_script = config_script.replace(b' --libs print ', b' --libs ')
        with open(os.path.join(libxslt_dir, "configure"), 'wb') as f:
            f.write(config_script)

    # build libxslt
    libxslt_configure_cmd = configure_cmd + [
        '--without-python',
        '--with-libxml-prefix=%s' % prefix,
        '--without-crypto',
    ]
    if not (has_current_lib("libxslt", libxslt_dir) and has_current_lib("libexslt", libxslt_dir)):
        cmmi(libxslt_configure_cmd, libxslt_dir, multicore, **call_setup)

    # collect build setup for lxml
    xslt_config = os.path.join(prefix, 'bin', 'xslt-config')
    xml2_config = os.path.join(prefix, 'bin', 'xml2-config')

    static_include_dirs.extend([
            os.path.join(prefix, 'include'),
            os.path.join(prefix, 'include', 'libxml2'),
            os.path.join(prefix, 'include', 'libxslt'),
            os.path.join(prefix, 'include', 'libexslt')])
    static_library_dirs.append(lib_dir)

    listdir = os.listdir(lib_dir)
    static_binaries += [os.path.join(lib_dir, filename)
        for lib in lib_names
        for filename in listdir
        if lib in filename and filename.endswith('.a')]

    return xml2_config, xslt_config


def main():
    static_include_dirs = []
    static_library_dirs = []
    download_dir = "libs"

    if sys_platform.startswith('win'):
        return get_prebuilt_libxml2xslt(
            download_dir, static_include_dirs, static_library_dirs)
    else:
        return build_libxml2xslt(
            download_dir, 'build/tmp',
            static_include_dirs, static_library_dirs,
            static_cflags=[],
            static_binaries=[]
        )


if __name__ == '__main__':
    if len(sys.argv) > 1:
        # change global sys_platform setting
        sys_platform = sys.argv[1]
    main()
