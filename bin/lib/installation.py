import glob
import logging
import os
import sys
import re
import shutil
import subprocess
import tempfile
import time
import hashlib
from datetime import datetime
from collections import defaultdict, ChainMap

import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache

from lib.amazon import list_compilers

VERSIONED_RE = re.compile(r'^(.*)-([0-9.]+)$')

MAX_ITERS = 5

NO_DEFAULT = "__no_default__"

logger = logging.getLogger(__name__)

_memoized_compilers = None
_cpp_properties_compilers = None
_cpp_properties_libraries = None

build_supported_os = ['Linux']
build_supported_buildtype = ['Debug']
build_supported_arch = ['x86_64', 'x86']
build_supported_stdver = ['']
build_supported_stdlib = ['', 'libc++']
build_supported_flags = ['']
build_supported_flagscollection = [['']]

def s3_available_compilers():
    global _memoized_compilers
    if _memoized_compilers is None:
        _memoized_compilers = defaultdict(lambda: [])
        for compiler in list_compilers():
            match = VERSIONED_RE.match(compiler)
            if match:
                _memoized_compilers[match.group(1)].append(match.group(2))
    return _memoized_compilers

def getToolchainPathFromOptions(options):
    match = re.search("--gcc-toolchain=(\S*)", options)
    if match:
        return match[1]
    else:
        match = re.search("--gxx-name=(\S*)", options)
        if match:
            return os.path.realpath(os.path.join(os.path.dirname(match[1]), ".."))
    return False

def getStdVerFromOptions(options):
    match = re.search("-std=(\S*)", options)
    if match:
        return match[1]
    return False

def getTargetFromOptions(options):
    match = re.search("-target (\S*)", options)
    if match:
        return match[1]
    return False

def does_compiler_support_x86(exe, compilerType, options):
    fixedTarget = getTargetFromOptions(options)
    if fixedTarget:
        return False

    if compilerType == "":
        output = subprocess.check_output([exe, '--target-help']).decode('utf-8')
    elif compilerType == "clang":
        folder = os.path.dirname(exe)
        llcexe = os.path.join(folder, 'llc')
        if os.path.exists(llcexe):
            try:
                output = subprocess.check_output([llcexe, '--version']).decode('utf-8')
            except subprocess.CalledProcessError as e:
                output = e.output.decode('utf-8')
        else:
            output = ""
    else:
        output = ""

    if 'x86' in output:
        logger.debug(f'Compiler {exe} supports x86')
        return True
    else:
        logger.debug(f'Compiler {exe} does not support x86')
        return False

def get_cpp_properties_compilers():
    global _cpp_properties_compilers, _cpp_properties_libraries
    url = 'https://raw.githubusercontent.com/mattgodbolt/compiler-explorer/master/etc/config/c%2B%2B.amazon.properties'
    if _cpp_properties_compilers is None:
        _cpp_properties_compilers = defaultdict(lambda: [])
        _cpp_properties_libraries = defaultdict(lambda: [])
        lines = []
        with tempfile.TemporaryFile() as fd:
            request = requests.get(url, stream=True)
            if not request.ok:
                logger.error(f'Failed to fetch {url}: {request}')
                raise RuntimeError(f'Fetch failure for {url}: {request}')
            for chunk in request.iter_content(chunk_size=4 * 1024 * 1024):
                fd.write(chunk)
            fd.flush()
            fd.seek(0)
            lines = fd.readlines()

        logger.debug('Reading properties for groups')
        groups = defaultdict(lambda: [])
        for line in lines:
            sline = line.decode('utf-8').rstrip('\n')
            if sline.startswith('group.'):
                keyval = sline.split('=', 1)
                key = keyval[0].split('.')
                val = keyval[1]
                group = key[1]
                if not group in groups:
                    groups[group] = defaultdict(lambda: [])
                    groups[group]['options'] = ""
                    groups[group]['compilerType'] = ""
                    groups[group]['compilers'] = []
                    groups[group]['supportsBinary'] = True

                if key[2] == "compilers":
                    groups[group]['compilers'] = val.split(':')
                elif key[2] == "options":
                    groups[group]['options'] = val
                elif key[2] == "compilerType":
                    groups[group]['compilerType'] = val
                elif key[2] == "supportsBinary":
                    groups[group]['supportsBinary'] = val == 'true'
            elif sline.startswith('libs.'):
                keyval = sline.split('=', 1)
                key = keyval[0].split('.')
                val = keyval[1]
                libid = key[1]
                if not libid in _cpp_properties_libraries:
                    _cpp_properties_libraries[libid] = defaultdict(lambda: [])

                if key[2] == 'description':
                    _cpp_properties_libraries[libid]['description'] = val
                elif key[2] == 'url':
                    _cpp_properties_libraries[libid]['url'] = val

        logger.debug('Setting default values for compilers')
        for group in groups:
            for compiler in groups[group]['compilers']:
                if not compiler in _cpp_properties_compilers:
                    _cpp_properties_compilers[compiler] = defaultdict(lambda: [])
                _cpp_properties_compilers[compiler]['options'] = groups[group]['options']
                _cpp_properties_compilers[compiler]['compilerType'] = groups[group]['compilerType']
                _cpp_properties_compilers[compiler]['supportsBinary'] = groups[group]['supportsBinary']
                _cpp_properties_compilers[compiler]['group'] = group

        logger.debug('Reading properties for compilers')
        for line in lines:
            sline = line.decode('utf-8').rstrip('\n')
            if sline.startswith('compiler.'):
                keyval = sline.split('=', 1)
                key = keyval[0].split('.')
                val = keyval[1]
                if not key[1] in _cpp_properties_compilers:
                    _cpp_properties_compilers[key[1]] = defaultdict(lambda: [])
                _cpp_properties_compilers[key[1]][key[2]] = val

        logger.debug('Removing compilers that are not available or do not support binaries')
        keysToRemove = defaultdict(lambda: [])
        for compiler in _cpp_properties_compilers:
            if 'supportsBinary' in _cpp_properties_compilers[compiler] and not _cpp_properties_compilers[compiler]['supportsBinary']:
                keysToRemove[compiler] = True
            elif _cpp_properties_compilers[compiler] == 'wine-vc':
                keysToRemove[compiler] = True
            elif 'exe' in _cpp_properties_compilers[compiler]:
                exe = _cpp_properties_compilers[compiler]['exe']
                if not os.path.exists(exe):
                    keysToRemove[compiler] = True
            else:
                keysToRemove[compiler] = True

        for compiler in keysToRemove:
            del _cpp_properties_compilers[compiler]

    return _cpp_properties_compilers

class InstallationContext(object):
    def __init__(self, destination, staging, s3_url, dry_run, cache):
        self.destination = destination
        self.staging = staging
        self.s3_url = s3_url
        self.dry_run = dry_run
        if cache:
            self.info(f"Using cache {cache}")
            self.fetcher = CacheControl(requests.session(), cache=FileCache(cache))
        else:
            self.info(f"Making uncached requests")
            self.fetcher = requests

    def debug(self, message):
        logger.debug(message)

    def info(self, message):
        logger.info(message)

    def warn(self, message):
        logger.warning(message)

    def error(self, message):
        logger.error(message)

    def clean_staging(self):
        self.debug(f"Cleaning staging dir {self.staging}")
        if os.path.isdir(self.staging):
            if not sys.platform.startswith('win'):
                subprocess.check_call(["chmod", "-R", "u+w", self.staging])
            shutil.rmtree(self.staging, ignore_errors=True)
        self.debug(f"Recreating staging dir {self.staging}")
        os.makedirs(self.staging)

    def fetch_to(self, url, fd):
        self.debug(f'Fetching {url}')
        request = self.fetcher.get(url, stream=True)
        if not request.ok:
            self.error(f'Failed to fetch {url}: {request}')
            raise RuntimeError(f'Fetch failure for {url}: {request}')
        fetched = 0
        if 'content-length' in request.headers.keys():
            length = int(request.headers['content-length'])
        else:
            length = 0
        self.info(f'Fetching {url} ({length} bytes)')
        report_every_secs = 5
        report_time = time.time() + report_every_secs
        for chunk in request.iter_content(chunk_size=4 * 1024 * 1024):
            fd.write(chunk)
            fetched += len(chunk)
            now = time.time()
            if now >= report_time:
                self.info(f'{100.0 * fetched / length:.1f}% of {url}...')
                report_time = now + report_every_secs
        self.info(f'100% of {url}')
        fd.flush()

    def fetch_url_and_pipe_to(self, url, command, subdir='.'):
        untar_dir = os.path.join(self.staging, subdir)
        os.makedirs(untar_dir, exist_ok=True)
        # We stream to a temporary file first before then piping this to the command
        # as sometimes the command can take so long the URL endpoint closes the door on us
        with tempfile.TemporaryFile() as fd:
            self.fetch_to(url, fd)
            fd.seek(0)
            self.info(f'Piping to {" ".join(command)}')
            subprocess.check_call(command, stdin=fd, cwd=untar_dir)

    def stage_command(self, command):
        self.info(f'Staging with {" ".join(command)}')
        subprocess.check_call(command, cwd=self.staging)

    def fetch_s3_and_pipe_to(self, s3, command):
        return self.fetch_url_and_pipe_to(f'{self.s3_url}/{s3}', command)

    def make_subdir(self, subdir):
        full_subdir = os.path.join(self.destination, subdir)
        if not os.path.isdir(full_subdir):
            os.mkdir(full_subdir)

    def read_link(self, link):
        return os.readlink(os.path.join(self.destination, link))

    def set_link(self, source, dest):
        if self.dry_run:
            self.info(f'Would symlink {source} to {dest}')
            return

        full_dest = os.path.join(self.destination, dest)
        if os.path.exists(full_dest):
            os.remove(full_dest)
        self.info(f'Symlinking {dest} to {source}')
        os.symlink(source, full_dest)

    def glob(self, pattern):
        return [os.path.relpath(x, self.destination) for x in glob.glob(os.path.join(self.destination, pattern))]

    def remove_dir(self, directory):
        if self.dry_run:
            self.info(f'Would remove directory {directory} but in dry-run mode')
        else:
            shutil.rmtree(os.path.join(self.destination, directory), ignore_errors=True)
            self.info(f'Removing {directory}')

    def check_link(self, source, link):
        try:
            link = self.read_link(link)
            self.debug(f'readlink returned {link}')
            return link == source
        except FileNotFoundError:
            self.debug(f'File not found for {link}')
            return False

    def move_from_staging(self, source, dest=None):
        if not dest:
            dest = source
        existing_dir_rename = os.path.join(self.staging, "temp_orig")
        source = os.path.join(self.staging, source)
        dest = os.path.join(self.destination, dest)
        if self.dry_run:
            self.info(f'Would install {source} to {dest} but in dry-run mode')
            return
        self.info(f'Moving from staging ({source}) to final destination ({dest})')
        if not os.path.isdir(source):
            staging_contents = subprocess.check_output(['ls', '-l', self.staging]).decode('utf-8')
            self.info(f"Directory listing of staging:\n{staging_contents}")
            raise RuntimeError(f"Missing source '{source}'")
        # Some tar'd up GCCs are actually marked read-only...
        subprocess.check_call(["chmod", "u+w", source])
        state = ''
        if os.path.isdir(dest):
            self.info(f'Destination {dest} exists, temporarily moving out of the way (to {existing_dir_rename})')
            os.replace(dest, existing_dir_rename)
            state = 'old_renamed'
        try:
            os.replace(source, dest)
            if state == 'old_renamed':
                state = 'old_needs_remove'
        finally:
            if state == 'old_needs_remove':
                self.debug(f'Removing temporarily moved {existing_dir_rename}')
                shutil.rmtree(existing_dir_rename, ignore_errors=True)
            elif state == 'old_renamed':
                self.warn(f'Moving old destination back')
                os.replace(existing_dir_rename, dest)

    def compare_against_staging(self, source, dest=None):
        if not dest:
            dest = source
        source = os.path.join(self.staging, source)
        dest = os.path.join(self.destination, dest)
        self.info(f'Comparing {source} vs {dest}...')
        result = subprocess.call(['diff', '-r', source, dest])
        if result == 0:
            self.info('Contents match')
        else:
            self.warn('Contents differ')
        return result == 0

    def check_output(self, args, env=None):
        args = args[:]
        args[0] = os.path.join(self.destination, args[0])
        logger.debug('Executing %s in %s', args, self.destination)
        return subprocess.check_output(args, cwd=self.destination, env=env).decode('utf-8')

    def strip_exes(self, paths):
        if isinstance(paths, bool):
            if not paths:
                return
            paths = ['.']
        to_strip = []
        for path in paths:
            path = os.path.join(self.staging, path)
            logger.debug(f"Looking for executables to strip in {path}")
            if not os.path.isdir(path):
                raise RuntimeError(f"While looking for files to strip, {path} was not a directory")
            for dirpath, dirnames, filenames in os.walk(path):
                for filename in filenames:
                    full_path = os.path.join(dirpath, filename)
                    if os.access(full_path, os.X_OK):
                        to_strip.append(full_path)

        # Deliberately ignore errors
        subprocess.call(['strip'] + to_strip)


class Installable(object):
    def __init__(self, install_context, config):
        self.install_context = install_context
        self.config = config
        self.target_name = self.config.get("name", "(unnamed)")
        self.context = self.config_get("context", [])
        self.name = f'{"/".join(self.context)} {self.target_name}'
        self.depends = self.config.get('depends', [])
        self.install_always = self.config.get('install_always', False)
        self._check_link = None
        self.build_type = self.config_get("build_type", "")
        self.staticliblink = []
        self.url = "None"

    def _setup_check_exe(self, path_name):
        self.check_env = dict([x.replace('%PATH%', path_name).split('=', 1) for x in self.config_get('check_env', [])])

        self.check_file = self.config_get('check_file', False)
        if self.check_file:
            self.check_file = os.path.join(path_name, self.check_file)
        else:
            self.check_call = command_config(self.config_get('check_exe'))
            self.check_call[0] = os.path.join(path_name, self.check_call[0])

    def _setup_check_link(self, source, link):
        self._check_link = lambda: self.install_context.check_link(source, link)

    def debug(self, message):
        self.install_context.debug(f'{self.name}: {message}')

    def info(self, message):
        self.install_context.info(f'{self.name}: {message}')

    def warn(self, message):
        self.install_context.warn(f'{self.name}: {message}')

    def error(self, message):
        self.install_context.error(f'{self.name}: {message}')

    def verify(self):
        return True

    def should_install(self):
        return self.install_always or not self.is_installed()

    def should_build(self):
        return True

    def install(self):
        self.debug("Ensuring dependees are installed")
        any_missing = False
        for dependee in self.depends:
            if not dependee.is_installed():
                self.warn("Required dependee {} not installed".format(dependee))
                any_missing = True
        if any_missing:
            return False
        self.debug("Dependees ok")
        return True

    def install_internal(self):
        raise RuntimeError("needs to be implemented")

    def is_installed(self):
        if self._check_link and not self._check_link():
            self.debug('Check link returned false')
            return False

        if self.check_file:
            res = os.path.isfile(os.path.join(self.install_context.destination, self.check_file))
            self.debug(f'Check file for "{self.install_context.destination}/{self.check_file}" returned {res}')
            return res

        try:
            res = self.install_context.check_output(self.check_call, env=self.check_env)
            self.debug(f'Check call returned {res}')
            return True
        except FileNotFoundError:
            self.debug(f'File not found for {self.check_call}')
            return False
        except subprocess.CalledProcessError:
            self.debug(f'Got an error for {self.check_call}')
            return False

    def config_get(self, config_key, default=None):
        if config_key not in self.config and default is None:
            raise RuntimeError(f"Missing required key '{config_key}' in {self.name}")
        return self.config.get(config_key, default)

    def writebuildscript(self, buildfolder, compiler, compileroptions, compilerexe, compilerType, toolchain, buildos, buildtype, arch, stdver, stdlib, flagscombination, group):
        scriptfile = os.path.join(buildfolder, "build.sh")

        libname = self.context[-1]

        f = open(scriptfile, 'w')
        f.write('#!/bin/sh\n\n')
        compilerexecc = compilerexe[:-2]
        if compilerexe.endswith('clang++'):
            compilerexecc = f'{compilerexecc}'
        elif compilerexe.endswith('g++'):
            compilerexecc = f'{compilerexecc}cc'

        f.write(f'export CC={compilerexecc}\n')
        f.write(f'export CXX={compilerexe}\n')

        rpathflags = ''
        archflag = ''
        if arch == '':
            # note: native arch for the compiler, so most of the time 64, but not always
            if os.path.exists(f'{toolchain}/lib64'):
                rpathflags = f'-Wl,-rpath={toolchain}/lib64 -Wl,-rpath={toolchain}/lib'
            else:
                rpathflags = f'-Wl,-rpath={toolchain}/lib'
        elif arch == 'x86':
            rpathflags = f'-Wl,-rpath={toolchain}/lib32 -Wl,-rpath={toolchain}/lib'

            if compilerType == 'clang':
                archflag = '-m32'
            elif compilerType == '':
                archflag = '-march=i386 -m32'

        stdverflag = ''
        if stdver != '':
            stdverflag = f'-std={stdver}'
        
        stdlibflag = ''
        if stdlib != '' and compilerType == 'clang':
            libcxx = stdlib
            stdlibflag = f'-stdlib={stdlib}'
        else:
            libcxx = "libstdc++"

        extraflags = ' '.join(x for x in flagscombination)

        if compilerType == "":
            compilerTypeOrGcc = "gcc"
        else:
            compilerTypeOrGcc = compilerType

        self.debug(compilerTypeOrGcc)

        f.write(f'cmake -DCMAKE_BUILD_TYPE={buildtype} "-DCMAKE_CXX_COMPILER_EXTERNAL_TOOLCHAIN={toolchain}" "-DCMAKE_CXX_FLAGS_DEBUG={compileroptions} {archflag} {stdverflag} {stdlibflag} {rpathflags} {extraflags}" ..\n')
        f.write(f'make {libname}\n')

        f.write(f'libsfound=$(find . -name lib{libname}d.a)\n')
        f.write(f'if [ "$libsfound" = "" ]; then\n')
        f.write(f'  make\n')
        f.write(f'fi\n')

        f.write(f'if [ $? -ne 0 ]; then\n')
        f.write(f'  exit $?\n')
        f.write(f'fi\n')

        f.write(f'libsfound=$(find . -name lib{libname}d.a)\n')
        f.write(f'if [ "$libsfound" != "" ]; then\n')
        f.write(f'  conan export-pkg . {libname}/{self.target_name} -f -s os={buildos} -s build_type={buildtype} -s compiler={compilerTypeOrGcc} -s compiler.version={compiler} -s compiler.libcxx={libcxx} -s arch={arch} -s stdver={stdver} -s "flagcollection={extraflags}"\n')
        f.write(f'  conan upload {libname}/{self.target_name} --all -r=myserver -c\n')
        f.write(f'else\n')
        f.write(f'  exit 1\n')
        f.write(f'fi\n')

        f.close()

        subprocess.check_call(['/bin/chmod','+x', scriptfile])

    def writeconanfile(self, buildfolder):
        scriptfile = os.path.join(buildfolder, "conanfile.py")

        libname = self.context[-1]
        self.debug(libname)

        if self.staticliblink == []:
            self.staticliblink = [f'{libname}']

        libsum = ''
        for lib in self.staticliblink:
            libsum += f'"{lib}",'

        libsum = libsum[:-1]

        f = open(scriptfile, 'w')
        f.write('from conans import ConanFile, tools\n')
        f.write(f'class {libname}Conan(ConanFile):\n')
        f.write(f'    name = "{libname}"\n')
        f.write(f'    version = "{self.target_name}"\n')
        f.write(f'    settings = "os", "compiler", "build_type", "arch", "stdver", "flagcollection"\n')
        f.write(f'    description = "{self.description}"\n')
        f.write(f'    url = "{self.url}"\n')
        f.write(f'    license = "None"\n')
        f.write(f'    author = "None"\n')
        f.write(f'    topics = None\n')
        f.write(f'    def package(self):\n')
        for lib in self.staticliblink:
            f.write(f'        self.copy("lib{lib}d.a", dst="lib", keep_path=False)\n')
        f.write(f'    def package_info(self):\n')
        f.write(f'        self.cpp_info.libs = [{libsum}]\n')
        f.close()

    def executebuildscript(self, buildfolder):
        if subprocess.call(['./build.sh'], cwd=buildfolder) == 0:
            self.info(f'Build succeeded in {buildfolder}')
            return True
        else:
            return False

    def cmakebuildfor(self, compiler, options, exe, compilerType, toolchain, buildos, buildtype, arch, stdver, stdlib, flagscombination, group):
        hasher = hashlib.sha256()
        flagsstr = '|'.join(x for x in flagscombination)
        hasher.update(bytes(f'{compiler},{options},{toolchain},{buildos},{buildtype},{arch},{stdver},{stdlib},{flagsstr}', 'utf-8'))
        combinedhash = compiler + '_' + hasher.hexdigest()

        buildfolder = os.path.join(self.install_context.destination, self.path_name, combinedhash)
        self.debug(buildfolder)
        os.makedirs(buildfolder, exist_ok=True)

        self.writebuildscript(buildfolder, compiler, options, exe, compilerType, toolchain, buildos, buildtype, arch, stdver, stdlib, flagscombination, group)
        self.writeconanfile(buildfolder)
        builtok = self.executebuildscript(buildfolder)

        return builtok

    def cmakebuild(self, buildfor):
        builds_failed = 0
        builds_succeeded = 0

        compilerprops = get_cpp_properties_compilers()

        libname = self.context[-1]
        if not libname in _cpp_properties_libraries:
            raise RuntimeError(f'Library {libname} not found in c++.amazon.properties')

        if 'description' in _cpp_properties_libraries[libname]:
            self.description = _cpp_properties_libraries[libname]['description']
        if 'url' in _cpp_properties_libraries[libname]:
            self.url = _cpp_properties_libraries[libname]['url']

        for compiler in compilerprops:
            if buildfor != "" and compiler != buildfor:
                continue

            exe = compilerprops[compiler]['exe']

            if 'compilerType' in compilerprops[compiler]:
                compilerType = compilerprops[compiler]['compilerType']
            else:
                raise RuntimeError(f'Something is wrong with {compiler}')

            group = compilerprops[compiler]['group']
            toolchain = getToolchainPathFromOptions(_cpp_properties_compilers[compiler]['options'])
            fixedStdver = getStdVerFromOptions(_cpp_properties_compilers[compiler]['options'])
            if not toolchain:
                toolchain = os.path.realpath(os.path.join(os.path.dirname(exe), '..'))

            stdlibs = ['']

            options = compilerprops[compiler]['options']
            if compilerType == "":
                self.debug('Gcc-like compiler')
            elif compilerType == "clang":
                self.debug('Clang-like compiler')
                stdlibs = build_supported_stdlib
            else:
                self.debug('Some other compiler')

            archs = build_supported_arch
            if not does_compiler_support_x86(exe, compilerType, _cpp_properties_compilers[compiler]['options']):
                archs = ['']

            stdvers = build_supported_stdver
            if fixedStdver:
                stdvers = [fixedStdver]

            self.debug(build_supported_os)
            self.debug(build_supported_buildtype)
            self.debug(archs)
            self.debug(stdvers)
            self.debug(build_supported_stdlib)
            self.debug(build_supported_flagscollection)

            for buildos in build_supported_os:
                for buildtype in build_supported_buildtype:
                    for arch in archs:
                        for stdver in stdvers:
                            for stdlib in stdlibs:
                                for flagscombination in build_supported_flagscollection:
                                    if self.cmakebuildfor(compiler, options, exe, compilerType, toolchain, buildos, buildtype, arch, stdver, stdlib, flagscombination, group):
                                        builds_succeeded = builds_succeeded + 1
                                    else:
                                        builds_failed = builds_failed + 1

        # WIP create build folder -> should probably be somewhere /staging
        # DONE get a list of all the compilers we can make a build for
        # DONE download https://raw.githubusercontent.com/mattgodbolt/compiler-explorer/master/etc/config/c%2B%2B.amazon.properties
        # DONE somehow parse it and match it to the list this installer knows
        # DONE get a list of all the variables we support
        # WIP/DONE per compiler and variable -> cmake
        # introduce conanfile to export_pkg all the .a files
        return builds_failed == 0

    def build(self, buildfor):
        if self.build_type == "":
            raise RuntimeError('No build_type')
        
        if self.build_type == "cmake":
            return self.cmakebuild(buildfor)
        else:
            raise RuntimeError('Unsupported build_type')

def command_config(config):
    if isinstance(config, str):
        return config.split(" ")
    return config


class GitInstallable(Installable):
    def __init__(self, install_context, config):
        super(GitInstallable, self).__init__(install_context, config)
        last_context = self.context[-1]
        self.repo = self.config_get("repo", "")
        self.decompress_flag = 'z'
        self.strip = False
        self.subdir = os.path.join('libs', last_context)
        self.target_prefix = self.config_get("target_prefix", "")
        self.path_name = self.config_get('path_name', os.path.join(self.subdir, self.target_prefix + self.target_name))
        default_untar_dir = f'{last_context}-{self.target_name}'
        self.untar_dir = self.config_get("untar_dir", default_untar_dir)
        if self.repo == "":
            raise RuntimeError(f'Requires repo')
        check_file = self.config_get("check_file", "")
        if check_file == "":
            if self.build_type == "cmake":
                self.check_file = os.path.join(self.path_name, 'CMakeLists.txt')
            elif self.build_type == "make":
                self.check_file = os.path.join(self.path_name, 'Makefile')
            else:
                raise RuntimeError(f'Requires check_file')
        else:
            self.check_file = f'{self.path_name}/{check_file}'

    def stage(self):
        self.install_context.clean_staging()
        self.install_context.fetch_url_and_pipe_to(f'https://github.com/{self.repo}/archive/{self.target_prefix}{self.target_name}.tar.gz', ['tar', f'{self.decompress_flag}xf', '-'])
        if self.strip:
            self.install_context.strip_exes(self.strip)

    def verify(self):
        if not super(GitInstallable, self).verify():
            return False
        self.stage()
        return self.install_context.compare_against_staging(self.untar_dir, self.path_name)

    def install(self):
        if not super(GitInstallable, self).install():
            return False
        self.stage()
        if self.subdir:
            self.install_context.make_subdir(self.subdir)
        self.install_context.move_from_staging(self.untar_dir, self.path_name)
        return True

    def __repr__(self) -> str:
        return f'GitInstallable({self.name}, {self.path_name})'


class S3TarballInstallable(Installable):
    def __init__(self, install_context, config):
        super(S3TarballInstallable, self).__init__(install_context, config)
        self.subdir = self.config_get("subdir", "")
        last_context = self.context[-1]
        if self.subdir:
            default_s3_path_prefix = f'{self.subdir}-{last_context}-{self.target_name}'
            default_path_name = f'{self.subdir}/{last_context}-{self.target_name}'
            default_untar_dir = f'{last_context}-{self.target_name}'
        else:
            default_s3_path_prefix = f'{last_context}-{self.target_name}'
            default_path_name = f'{last_context}-{self.target_name}'
            default_untar_dir = default_path_name
        s3_path_prefix = self.config_get('s3_path_prefix', default_s3_path_prefix)
        self.path_name = self.config_get('path_name', default_path_name)
        self.untar_dir = self.config_get("untar_dir", default_untar_dir)
        compression = self.config_get('compression', 'xz')
        if compression == 'xz':
            self.s3_path = f'{s3_path_prefix}.tar.xz'
            self.decompress_flag = 'J'
        elif compression == 'gz':
            self.s3_path = f'{s3_path_prefix}.tar.gz'
            self.decompress_flag = 'z'
        elif compression == 'bz2':
            self.s3_path = f'{s3_path_prefix}.tar.bz2'
            self.decompress_flag = 'j'
        else:
            raise RuntimeError(f'Unknown compression {compression}')
        self.strip = self.config_get('strip', False)
        self._setup_check_exe(self.path_name)

    def stage(self):
        self.install_context.clean_staging()
        self.install_context.fetch_s3_and_pipe_to(self.s3_path, ['tar', f'{self.decompress_flag}xf', '-'])
        if self.strip:
            self.install_context.strip_exes(self.strip)

    def verify(self):
        if not super(S3TarballInstallable, self).verify():
            return False
        self.stage()
        return self.install_context.compare_against_staging(self.untar_dir, self.path_name)

    def install(self):
        if not super(S3TarballInstallable, self).install():
            return False
        self.stage()
        if self.subdir:
            self.install_context.make_subdir(self.subdir)
        self.install_context.move_from_staging(self.untar_dir, self.path_name)
        return True

    def __repr__(self) -> str:
        return f'S3TarballInstallable({self.name}, {self.path_name})'


class NightlyInstallable(Installable):
    def __init__(self, install_context, config):
        super(NightlyInstallable, self).__init__(install_context, config)
        self.subdir = self.config_get("subdir", "")
        self.strip = self.config_get('strip', False)
        compiler_name = self.config_get('compiler_name', f'{self.context[-1]}-{self.target_name}')
        current = s3_available_compilers()
        if compiler_name not in current:
            raise RuntimeError(f'Unable to find nightlies for {compiler_name}')
        most_recent = max(current[compiler_name])
        self.info(f'Most recent {compiler_name} is {most_recent}')
        self.s3_path = f'{compiler_name}-{most_recent}'
        self.path_name = os.path.join(self.subdir, f'{compiler_name}-{most_recent}')
        self.compiler_pattern = os.path.join(self.subdir, f'{compiler_name}-*')
        self.path_name_symlink = self.config_get('symlink', os.path.join(self.subdir, f'{compiler_name}'))
        self.num_to_keep = self.config_get('num_to_keep', 5)
        self._setup_check_exe(self.path_name)
        self._setup_check_link(self.s3_path, self.path_name_symlink)

    def stage(self):
        self.install_context.clean_staging()
        self.install_context.fetch_s3_and_pipe_to(f'{self.s3_path}.tar.xz', ['tar', f'Jxf', '-'])
        if self.strip:
            self.install_context.strip_exes(self.strip)

    def verify(self):
        if not super(NightlyInstallable, self).verify():
            return False
        self.stage()
        return self.install_context.compare_against_staging(self.s3_path, self.path_name)

    def should_install(self):
        return True

    def install(self):
        if not super(NightlyInstallable, self).install():
            return False
        self.stage()

        # Do this first, and add one for the file we haven't yet installed... (then dry run works)
        num_to_keep = self.num_to_keep + 1
        all_versions = list(sorted(self.install_context.glob(self.compiler_pattern)))
        for to_remove in all_versions[:-num_to_keep]:
            self.install_context.remove_dir(to_remove)

        self.install_context.move_from_staging(self.s3_path, self.path_name)
        self.install_context.set_link(self.s3_path, self.path_name_symlink)

        return True

    def __repr__(self) -> str:
        return f'NightlyInstallable({self.name}, {self.path_name})'


class TarballInstallable(Installable):
    def __init__(self, install_context, config):
        super(TarballInstallable, self).__init__(install_context, config)
        self.install_path = self.config_get('dir')
        self.install_path_symlink = self.config_get('symlink', False)
        self.untar_path = self.config_get('untar_dir', self.install_path)
        if self.config_get('create_untar_dir', False):
            self.untar_to = self.untar_path
        else:
            self.untar_to = '.'
        self.url = self.config_get('url')
        if self.config_get('compression') == 'xz':
            decompress_flag = 'J'
        elif self.config_get('compression') == 'gz':
            decompress_flag = 'z'
        elif self.config_get('compression') == 'bz2':
            decompress_flag = 'j'
        else:
            raise RuntimeError(f'Unknown compression {self.config_get("compression")}')
        self.configure_command = command_config(self.config_get('configure_command', []))
        self.tar_cmd = ['tar', f'{decompress_flag}xf', '-']
        strip_components = self.config_get("strip_components", 0)
        if strip_components:
            self.tar_cmd += ['--strip-components', str(strip_components)]
        self.strip = self.config_get('strip', False)
        self._setup_check_exe(self.install_path)
        if self.install_path_symlink:
            self._setup_check_link(self.install_path, self.install_path_symlink)

    def stage(self):
        self.install_context.clean_staging()
        self.install_context.fetch_url_and_pipe_to(f'{self.url}', self.tar_cmd, self.untar_to)
        if self.configure_command:
            self.install_context.stage_command(self.configure_command)
        if self.strip:
            self.install_context.strip_exes(self.strip)
        if not os.path.isdir(os.path.join(self.install_context.staging, self.untar_path)):
            raise RuntimeError(f"After unpacking, {self.untar_path} was not a directory")

    def verify(self):
        if not super(TarballInstallable, self).verify():
            return False
        self.stage()
        return self.install_context.compare_against_staging(self.untar_path, self.install_path)

    def install(self):
        if not super(TarballInstallable, self).install():
            return False
        self.stage()
        self.install_context.move_from_staging(self.untar_path, self.install_path)
        if self.install_path_symlink:
            self.install_context.set_link(self.install_path, self.install_path_symlink)
        return True

    def __repr__(self) -> str:
        return f'TarballInstallable({self.name}, {self.install_path})'


class ScriptInstallable(Installable):
    def __init__(self, install_context, config):
        super(ScriptInstallable, self).__init__(install_context, config)
        self.install_path = self.config_get('dir')
        self.install_path_symlink = self.config_get('symlink', False)
        self.fetch = self.config_get('fetch')
        self.script = self.config_get('script')
        self.strip = self.config_get('strip', False)
        self._setup_check_exe(self.install_path)
        if self.install_path_symlink:
            self._setup_check_link(self.install_path, self.install_path_symlink)

    def stage(self):
        self.install_context.clean_staging()
        for url in self.fetch:
            url, filename = url.split(' ')
            with open(os.path.join(self.install_context.staging, filename), 'wb') as f:
                self.install_context.fetch_to(url, f)
            self.info(f'{url} -> {filename}')
        self.install_context.stage_command(['bash', '-c', self.script])
        if self.strip:
            self.install_context.strip_exes(self.strip)

    def verify(self):
        if not super(ScriptInstallable, self).verify():
            return False
        self.stage()
        return self.install_context.compare_against_staging(self.install_path)

    def install(self):
        if not super(ScriptInstallable, self).install():
            return False
        self.stage()
        self.install_context.move_from_staging(self.install_path)
        if self.install_path_symlink:
            self.install_context.set_link(self.install_path, self.install_path_symlink)
        return True

    def __repr__(self) -> str:
        return f'ScriptInstallable({self.name}, {self.install_path})'


def targets_from(node, enabled, base_config=None):
    if base_config is None:
        base_config = {}
    return _targets_from(node, enabled, [], "", base_config)


def is_list_of_strings(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def is_value_type(value):
    return isinstance(value, str) \
           or isinstance(value, bool) \
           or isinstance(value, float) \
           or isinstance(value, int) \
           or is_list_of_strings(value)


def needs_expansion(target):
    for value in target.values():
        if is_list_of_strings(value):
            for v in value:
                if '{' in v:
                    return True
        elif isinstance(value, str):
            if '{' in value:
                return True
    return False


def _targets_from(node, enabled, context, name, base_config):
    if not node:
        return

    if isinstance(node, list):
        for child in node:
            for target in _targets_from(child, enabled, context, name, base_config):
                yield target
        return

    if not isinstance(node, dict):
        return

    if 'if' in node:
        condition = node['if']
        if condition not in enabled:
            return

    context = context[:]
    if name:
        context.append(name)
    base_config = dict(base_config)
    for key, value in node.items():
        if key != 'targets' and is_value_type(value):
            base_config[key] = value

    for child_name, child in node.items():
        for target in _targets_from(child, enabled, context, child_name, base_config):
            yield target

    if 'targets' in node:
        base_config['context'] = context
        for target in node['targets']:
            if isinstance(target, float):
                raise RuntimeError(f"Target {target} was parsed as a float. Enclose in quotes")
            if isinstance(target, str):
                target = {'name': target}
            target = ChainMap(target, base_config)
            iterations = 0
            while needs_expansion(target):
                iterations += 1
                if iterations > MAX_ITERS:
                    raise RuntimeError(f"Too many mutual references (in {'/'.join(context)})")
                for key, value in target.items():
                    try:
                        if is_list_of_strings(value):
                            target[key] = [x.format(**target) for x in value]
                        elif isinstance(value, str):
                            target[key] = value.format(**target)
                        elif isinstance(value, float):
                            target[key] = str(value)
                    except KeyError as ke:
                        raise RuntimeError(f"Unable to find key {ke} in {target[key]} (in {'/'.join(context)})")
            yield target


INSTALLER_TYPES = {
    'tarballs': TarballInstallable,
    's3tarballs': S3TarballInstallable,
    'nightly': NightlyInstallable,
    'script': ScriptInstallable,
    'git': GitInstallable
}


def installers_for(install_context, nodes, enabled):
    for target in targets_from(nodes, enabled, {'staging': install_context.staging, 'now': datetime.now()}):
        assert 'type' in target
        target_type = target['type']
        if target_type not in INSTALLER_TYPES:
            raise RuntimeError(f'Unknown installer type {target_type}')
        installer_type = INSTALLER_TYPES[target_type]
        yield installer_type(install_context, target)
