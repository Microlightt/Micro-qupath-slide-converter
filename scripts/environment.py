#!/usr/bin/env python3
"""Inspect or prepare an isolated WSI conversion runtime (supported bootstrap: Python >=3.10).

No shell activation, sudo, profile edits or uploads. setup writes only --root.
Downloads are official, immutable-version artifacts checked against embedded SHA256.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ARTIFACTS = {
    'miniforge': {'version': '25.3.1-0', 'url': 'https://github.com/conda-forge/miniforge/releases/download/25.3.1-0/Miniforge3-25.3.1-0-Linux-x86_64.sh', 'sha256': '376b160ed8130820db0ab0f3826ac1fc85923647f75c1b8231166e3d559ab768'},
    'bioformats2raw': {'version': '0.12.1', 'url': 'https://github.com/glencoesoftware/bioformats2raw/releases/download/v0.12.1/bioformats2raw-0.12.1.zip', 'sha256': '51fbbf04a83c2042b707fce016ad0c8260d37194ff8fe7d986d53f4ebee116a6'},
    'raw2ometiff': {'version': '0.10.1', 'url': 'https://github.com/glencoesoftware/raw2ometiff/releases/download/v0.10.1/raw2ometiff-0.10.1.zip', 'sha256': 'e78a22e60ee44eb462abecfc60b997f67c5ed399486a962a654fb30d9b54d9b8'},
    'qupath': {'version': '0.6.0', 'url': 'https://github.com/qupath/qupath/releases/download/v0.6.0/QuPath-v0.6.0-Linux.tar.xz', 'sha256': '19ec6d821678c99b943a61852d05c88a770b9fbc48bb09cd0209742a00015ee6'},
}
# Direct packages are constrained; the actual transitive/build lock is exported.
# This is not advertised as a byte-for-byte cross-platform lockfile.
PACKAGES = ['python=3.12', 'openjdk=17', 'numpy>=2,<3', 'tifffile>=2025.5',
            'zarr>=3,<4', 'imagecodecs', 'jpype1>=1.5,<2', 'pillow', 'openslide', 'openslide-python']


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    os.replace(tmp, path)


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def platform_report():
    libc, version = platform.libc_ver()
    return {'system': platform.system(), 'machine': platform.machine(), 'libc': libc,
            'libc_version': version, 'cpu_count': os.cpu_count(),
            'bootstrap_python': sys.version.split()[0], 'display': bool(os.environ.get('DISPLAY'))}


def require_supported(info):
    if info['system'] != 'Linux' or info['machine'] not in ('x86_64', 'AMD64'):
        raise RuntimeError('Automatic setup v1 supports Linux x86_64 only; use a Linux host. No installation performed.')
    if info['libc'] != 'glibc':
        raise RuntimeError('Automatic setup requires glibc Linux; musl/Alpine is not supported.')
    version = tuple(int(v) for v in info['libc_version'].split('.')[:2])
    if version < (2, 28):
        raise RuntimeError('Automatic setup requires glibc >=2.28; use a compatible Linux host/container.')



KFB_CAPABILITY = {
    'reader': 'bundled_strict_kfb',
    'supported_variant': 'KFB 1.6 JPEG tiles',
    'supported_pixels': 'uint8 RGB (three color components)',
    'independent_fluorescence_channels': False,
    'mif_status': 'NOT_PROBED_BY_THIS_READER',
    'kfbf_status': 'SEPARATE_KFBF_READER',
    'proprietary_sdk_required': False,
    'packages': ['numpy', 'Pillow', 'tifffile', 'imagecodecs'],
}


def kfb_probe_in_process(test_input=None):
    """Read-only first-tile capability check, deliberately not a full-slide PASS."""
    report = {'status': 'NOT_VALIDATED', 'capability': dict(KFB_CAPABILITY),
              'file_read_probe_passed': False, 'full_slide_validated': False,
              'source_modified': False, 'network_used': False, 'dependencies': {}}
    if test_input:
        source = Path(test_input).expanduser().resolve()
        report['source'] = str(source)
        try:
            with source.open('rb') as handle:
                header = handle.read(8)
        except OSError as exc:
            report.update(status='INPUT_ERROR', error=str(exc), code='INPUT_UNREADABLE')
            return report
        # KFBF must never be reinterpreted as RGB, before loading any optional decoder.
        if header[:4] == b'KFBF' or header[4:8] == b'KFBF':
            report.update(status='UNSUPPORTED_VARIANT', code='KFBF_WRONG_READER',
                          error='KFBF requires the independent-channel reader. Use --kfbf-test-input; never treat it as KFB RGB.')
            return report
    for module, label in [('numpy', 'numpy'), ('PIL', 'Pillow'), ('tifffile', 'tifffile'), ('imagecodecs', 'imagecodecs')]:
        try:
            loaded = importlib.import_module(module)
            report['dependencies'][label] = {'available': True, 'version': getattr(loaded, '__version__', None)}
        except Exception as exc:
            report['dependencies'][label] = {'available': False, 'error': str(exc), 'exception': type(exc).__name__}
    missing = [name for name, value in report['dependencies'].items() if not value['available']]
    if missing:
        report.update(status='DEPENDENCIES_MISSING', missing_dependencies=missing,
                      action='Prepare a dedicated runtime with environment.py setup; do not add packages to a reused environment.')
        return report
    try:
        from kfb_reader import StrictKFB
    except Exception as exc:
        report.update(status='READER_UNAVAILABLE', error=str(exc), exception=type(exc).__name__)
        return report
    report['dependencies_available'] = True
    if not test_input:
        report.update(status='DEPENDENCIES_AVAILABLE_UNVALIDATED',
                      action='Supply --kfb-test-input to validate a real KFB header, index and full-resolution source JPEG tile.')
        return report
    try:
        import numpy as np
        with StrictKFB(source) as slide:
            base = slide.levels[0]
            if not base.entries:
                raise ValueError('The full-resolution KFB level has no indexed tiles')
            tile = slide.decode_tile(base.entries[0])
            if not isinstance(tile, np.ndarray) or tile.dtype != np.uint8 or tile.ndim != 3 or tile.shape[2] != 3 or not tile.size:
                raise ValueError('Decoded KFB base tile is not a nonempty uint8 RGB image')
            coverage = slide.coverage_report(base)
            report.update(status='FILE_READ_PASS', file_read_probe_passed=True,
                          scope='header_index_base_coverage_and_first_base_JPEG_only',
                          dimensions=list(slide.dimensions), native_levels=len(slide.levels),
                          decoded_tile={'shape':list(tile.shape), 'dtype':str(tile.dtype),
                                        'sha256':hashlib.sha256(np.ascontiguousarray(tile).tobytes()).hexdigest()},
                          base_coverage=coverage, coverage_policy_required=bool(coverage.get('missing_pixels',0)),
                          warnings=[])
            if report['coverage_policy_required']:
                report['warnings'].append('The base index leaves uncovered pixels. Conversion needs an explicit coverage policy; this probe has not filled any pixels.')
            if coverage.get('overlapping_pixels_counted_with_multiplicity',0):
                report['warnings'].append('The base index contains overlap; final conversion must apply the backend overlap rules and validate its output.')
            report['warnings'].append('This probe decoded one source tile; full conversion, pixel QC and QuPath validation are still required.')
    except Exception as exc:
        code = getattr(exc, 'code', None)
        report.update(status='UNSUPPORTED_VARIANT' if code=='KFBF_WRONG_READER' else 'FILE_READ_FAILED',
                      error=str(exc), exception=type(exc).__name__, code=code or 'KFB_READ_FAILED')
    return report


def kfb_readiness(python=None, test_input=None):
    """Run imports/optional data read with the configured runtime, never the wrong host Python."""
    interpreter = str(python or sys.executable)
    command = [interpreter, str(Path(__file__).resolve()), '_kfb-probe']
    if test_input:
        command += ['--input', str(Path(test_input).expanduser().absolute())]
    try:
        child = subprocess.run(command, capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), check=False)
        if child.returncode:
            report = {'status':'PROBE_PROCESS_FAILED', 'file_read_probe_passed':False,
                    'full_slide_validated':False, 'capability':dict(KFB_CAPABILITY),
                    'returncode':child.returncode, 'error':child.stderr[-4000:]}
        else:
            report = json.loads(child.stdout)
            if not isinstance(report, dict) or 'file_read_probe_passed' not in report or 'status' not in report:
                raise ValueError('Invalid KFB probe result')
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        report = {'status':'PROBE_PROCESS_FAILED', 'file_read_probe_passed':False,
                  'full_slide_validated':False, 'capability':dict(KFB_CAPABILITY),
                  'error':str(exc), 'exception':type(exc).__name__}
    report['python'] = interpreter
    if report['status'] in ('DEPENDENCIES_MISSING','PROBE_PROCESS_FAILED'):
        report['setup_command'] = 'python3 '+shlex.quote(str(Path(__file__).resolve()))+' setup --root "$HOME/.local/share/qupath-slide-converter" --with-qupath'
    return report


KFBF_CAPABILITY = {
    'reader': 'bundled_strict_kfbf',
    'supported_variant': 'KFBF 2.1 with validated channel metadata and independent grayscale JPEG tiles',
    'supported_pixels': 'uint8 independent channels; no precision coercion',
    'proprietary_sdk_required': False,
    'packages': ['numpy', 'Pillow', 'tifffile', 'imagecodecs', 'zarr'],
}


def kfbf_probe_in_process(test_input=None):
    """Inspect actual channel metadata and one recorded base tile per channel."""
    report = dict(status='NOT_VALIDATED', capability=dict(KFBF_CAPABILITY),
                  file_read_probe_passed=False, full_slide_validated=False,
                  source_modified=False, network_used=False, dependencies={})
    for module, label in [('numpy','numpy'),('PIL','Pillow'),('tifffile','tifffile'),('imagecodecs','imagecodecs'),('zarr','zarr')]:
        try:
            loaded = importlib.import_module(module)
            report['dependencies'][label] = dict(available=True, version=getattr(loaded,'__version__',None))
        except Exception as exc:
            report['dependencies'][label] = dict(available=False, error=str(exc))
    missing = [name for name,v in report['dependencies'].items() if not v['available']]
    if missing:
        report.update(status='DEPENDENCIES_MISSING',missing_dependencies=missing)
        return report
    if not test_input:
        report.update(status='DEPENDENCIES_AVAILABLE_UNVALIDATED',
                      action='Supply --kfbf-test-input; installed dependencies are not real-file validation.')
        return report
    source=Path(test_input).expanduser().resolve()
    report['source']=str(source)
    reader=None
    try:
        from kfbf_reader import KFBFReader
        reader=KFBFReader(source)
        probes=[]
        for c in range(reader.channel_count):
            tile=next((entry for entry in reader.entries if entry.level==0 and entry.channel==c and entry.length),None)
            if tile is None:raise ValueError('No recorded full-resolution tile for channel '+str(c))
            pixels=reader.decode_tile(tile)
            probes.append(dict(channel=c,shape=list(pixels.shape),dtype=str(pixels.dtype),
                               sha256=hashlib.sha256(pixels.tobytes()).hexdigest()))
        report.update(status='FILE_READ_PASS',file_read_probe_passed=True,
                      scope='strict_metadata_index_coverage_and_one_recorded_base_JPEG_per_channel',
                      dimensions=[reader.width,reader.height],channels=[v['name'] for v in reader.channels],channel_count=reader.channel_count,
                      dtype=reader.dtype,decoded_channels=probes,source_coverage=reader.coverage_report,
                      warnings=['This probe does not fill missing pixels or grant a filling policy.',
                                'Every full-resolution channel and QuPath still require complete conversion validation.'])
    except Exception as exc:
        report.update(status='FILE_READ_FAILED',error=str(exc),code=getattr(exc,'code','KFBF_READ_FAILED'))
    finally:
        if reader is not None:reader.close()
    return report


def kfbf_readiness(python=None,test_input=None):
    interpreter=str(python or sys.executable)
    command=[interpreter,str(Path(__file__).resolve()),'_kfbf-probe']
    if test_input:command+=['--input',str(Path(test_input).expanduser().absolute())]
    try:
        child=subprocess.run(command,capture_output=True,text=True,timeout=120,
                             env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),check=False)
        if child.returncode:raise RuntimeError(child.stderr[-4000:] or 'KFBF probe process failed')
        report=json.loads(child.stdout)
        if not isinstance(report,dict) or 'file_read_probe_passed' not in report or 'status' not in report:
            raise ValueError('Invalid KFBF probe result')
    except (OSError,subprocess.TimeoutExpired,ValueError,RuntimeError) as exc:
        report=dict(status='PROBE_PROCESS_FAILED',file_read_probe_passed=False,full_slide_validated=False,
                    capability=dict(KFBF_CAPABILITY),error=str(exc))
    report['python']=interpreter
    if report['status'] in ('DEPENDENCIES_MISSING','PROBE_PROCESS_FAILED'):
        report['setup_command']='python3 '+shlex.quote(str(Path(__file__).resolve()))+' setup --root "$HOME/.local/share/qupath-slide-converter" --with-qupath'
    return report


def doctor(config=None, kfb_test_input=None, kfbf_test_input=None):
    result = {'platform': platform_report(), 'tools_on_path': {
        name: shutil.which(name) for name in ('python3', 'java', 'conda', 'micromamba', 'bioformats2raw', 'raw2ometiff', 'QuPath', 'qupath')},
        'network_checked': False, 'runtime_validated': False}
    runtime_python = sys.executable
    try:
        require_supported(result['platform'])
        result['automatic_setup_supported'] = True
    except (RuntimeError, ValueError) as exc:
        result.update(automatic_setup_supported=False, setup_reason=str(exc))
    if config:
        data = json.loads(Path(config).read_text())
        result['runtime_config'] = data
        runtime_python = data.get('python') or runtime_python
        result['missing_paths'] = [str(data[k]) for k in ('python', 'java', 'jvm', 'bioformats2raw_home', 'raw2ometiff_home', 'qupath') if data.get(k) and not Path(data[k]).exists()]
        # Existence alone is deliberately not a PASS; setup performs actual I/O.
    result['kfb'] = kfb_readiness(runtime_python, kfb_test_input)
    result['kfbf'] = kfbf_readiness(runtime_python, kfbf_test_input)
    return result


def download(artifact, cache):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / artifact['url'].rsplit('/', 1)[-1]
    if destination.exists():
        if digest(destination) == artifact['sha256']:
            return destination
        raise RuntimeError('Cached artifact checksum mismatch: ' + str(destination) + '; preserved for inspection.')
    temporary = destination.with_suffix(destination.suffix + '.part')
    # A previous partial download is never executed or mistaken for a full artifact.
    request = urllib.request.Request(artifact['url'], headers={'User-Agent': 'qupath-slide-converter/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(temporary, 'wb') as output:
            while True:
                data = response.read(1024 * 1024)
                if not data:
                    break
                output.write(data)
        if digest(temporary) != artifact['sha256']:
            raise RuntimeError('Downloaded artifact checksum mismatch: ' + str(temporary))
        os.replace(temporary, destination)
    except Exception:
        # Keep *.part for diagnosis, but do not extract it or skip validation later.
        raise
    return destination


def contained(path, directory):
    try:
        Path(path).resolve().relative_to(Path(directory).resolve())
        return True
    except ValueError:
        return False


def safe_member(name, destination):
    value = PurePosixPath(name)
    if '\\' in name or value.is_absolute() or '..' in value.parts:
        raise RuntimeError('Unsafe archive path: ' + name)
    target = Path(destination).joinpath(*value.parts)
    if not contained(target, destination):
        raise RuntimeError('Archive path escapes destination: ' + name)
    return target


def extract_archive(archive, destination):
    """Extract into a NEW directory; reject traversal, special files and unsafe links."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for info in source.infolist():
                target = safe_member(info.filename, destination)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise RuntimeError('Symlinks in converter ZIP are not accepted')
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as inp, target.open('xb') as out:
                    shutil.copyfileobj(inp, out)
                target.chmod(0o755 if mode & 0o111 else 0o644)
        return
    with tarfile.open(archive, 'r:*') as source:
        members = source.getmembers()
        links = []
        for member in members:
            target = safe_member(member.name, destination)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as inp, target.open('xb') as out:
                    shutil.copyfileobj(inp, out)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            elif member.issym() or member.islnk():
                if PurePosixPath(member.linkname).is_absolute():
                    raise RuntimeError('Absolute archive link rejected')
                linked = (target.parent / member.linkname) if member.issym() else (destination / member.linkname)
                if not contained(linked, destination):
                    raise RuntimeError('Archive link escapes destination')
                links.append((member, target, linked))
            else:
                raise RuntimeError('Special archive member rejected: ' + member.name)
        # Symlinks are installed last, preventing writes through a link.
        for member, target, linked in links:
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                target.symlink_to(member.linkname)
            else:
                os.link(linked, target)


def install_archive(name, root):
    artifact = ARTIFACTS[name]
    target = root / 'tools' / (name + '-' + artifact['version'])
    marker = target / '.wsi-artifact.json'
    if target.exists():
        if not marker.exists() or json.loads(marker.read_text()) != artifact:
            raise RuntimeError('Unrecognized existing tool directory; refusing overwrite: ' + str(target))
        return target
    archive = download(artifact, root / 'downloads')
    stage = root / 'tools' / ('.' + name + '-extract-' + str(os.getpid()))
    stage.parent.mkdir(exist_ok=True)
    extract_archive(archive, stage)
    children = list(stage.iterdir())
    content = children[0] if len(children) == 1 and children[0].is_dir() else stage
    write_json(content / '.wsi-artifact.json', artifact)
    os.rename(content, target)
    if stage.exists():
        stage.rmdir()
    return target


class CommandFailure(RuntimeError):
    def __init__(self, stage, returncode, log):
        self.stage = stage
        self.returncode = returncode
        super().__init__(stage + ' failed (exit ' + str(returncode) + '); see ' + str(log))


def run(command, root, name, env=None, timeout=1800):
    logs = root / 'logs'
    logs.mkdir(exist_ok=True)
    log = logs / (name + '.log')
    print('Running ' + name + '; log: ' + str(log), file=sys.stderr, flush=True)
    with log.open('w') as output:
        completed = subprocess.run([str(x) for x in command], stdout=output, stderr=subprocess.STDOUT,
                                   env=env, timeout=timeout, check=False)
    if completed.returncode:
        raise CommandFailure(name, completed.returncode, log)
    return log


def build_python(root):
    base = root / 'miniforge'
    complete = base / '.wsi-bootstrap-complete'
    if not complete.exists():
        if base.exists():
            raise RuntimeError('Incomplete/unrecognized Miniforge directory; preserve it and choose a new --root.')
        installer = download(ARTIFACTS['miniforge'], root / 'downloads')
        run(['bash', installer, '-b', '-p', base], root, 'install-miniforge')
        complete.write_text(ARTIFACTS['miniforge']['sha256'] + '\n')
    prefix = root / 'runtime'
    marker = prefix / '.wsi-runtime-ready'
    if not marker.exists():
        if prefix.exists():
            raise RuntimeError('Incomplete/unrecognized runtime; preserve it and choose a new --root.')
        env = dict(os.environ, CONDA_PKGS_DIRS=str(root / 'conda-pkgs'), CONDA_ENVS_PATH=str(root / 'conda-envs'),
                   CONDA_AUTO_UPDATE_CONDA='false', CONDA_NO_PLUGINS='true', CONDA_SOLVER='classic', TMPDIR=str(root/'tmp'))
        command = [base / 'bin/conda', 'create', '--yes', '--prefix', prefix, '--override-channels', '-c', 'conda-forge', '--strict-channel-priority', *PACKAGES]
        run(command, root, 'create-runtime', env=env, timeout=3600)
        marker.write_text(json.dumps(PACKAGES) + '\n')
    lock = run([base / 'bin/conda', 'list', '--prefix', prefix, '--explicit'], root, 'conda-explicit')
    shutil.copyfile(lock, root / 'conda-explicit.txt')
    return prefix / 'bin/python'


def runtime_from_paths(python, b2r, r2o, qupath=None):
    python = Path(python).expanduser().resolve()
    java = python.parent / 'java'
    if not java.exists():
        java = Path(shutil.which('java') or '/nonexistent/java')
    java = java.resolve()
    preferred_jvm = java.parent.parent / 'lib/server/libjvm.so'
    info = subprocess.run([str(python), '-c', 'import json,jpype,sys; print(json.dumps({"jvm":jpype.getDefaultJVMPath(),"python":sys.executable}))'],
                          capture_output=True, text=True, timeout=30, check=True)
    jvm = str(preferred_jvm) if preferred_jvm.is_file() else json.loads(info.stdout.strip().splitlines()[-1])['jvm']
    java_home = java.parent.parent
    if not java.exists():
        raise RuntimeError('No usable java launcher found beside reused Python or on PATH')
    paths = {'runtime_version': 1, 'python': str(python), 'java': str(java), 'java_home': str(java_home), 'jvm': jvm,
             'bioformats2raw_home': str(Path(b2r).expanduser().resolve()), 'raw2ometiff_home': str(Path(r2o).expanduser().resolve()),
             'qupath': str(Path(qupath).expanduser().resolve()) if qupath else None}
    for key, exe in [('bioformats2raw_home', 'bioformats2raw'), ('raw2ometiff_home', 'raw2ometiff')]:
        path = Path(paths[key]) / 'bin' / exe
        if not path.is_file():
            raise RuntimeError('Missing launcher: ' + str(path))
    if paths['qupath'] and not Path(paths['qupath']).is_file():
        raise RuntimeError('Missing QuPath launcher: ' + paths['qupath'])
    return paths



def prepare_qupath_bridge(runtime, root, native_read_failure=None):
    """Explicit opt-in bridge only after native launcher failure; never emulates QuPath."""
    native = Path(runtime['qupath'])
    probe = subprocess.run([str(native), '--version'], capture_output=True, text=True, timeout=30)
    write_json(root/'qupath-native-probe.json', {'command':[str(native),'--version'], 'returncode':probe.returncode,
                                               'stdout':probe.stdout[-4000:], 'stderr':probe.stderr[-4000:]})
    runtime['qupath_native_launcher'] = str(native)
    runtime['qupath_native_probe_exit'] = probe.returncode
    if native_read_failure is not None:
        runtime['qupath_native_read_failure'] = native_read_failure
    if probe.returncode == 0 and native_read_failure is None:
        runtime['qupath_launch_method'] = 'native'
        return runtime
    app = native.parent.parent
    jvm = app/'lib/runtime/lib/server/libjvm.so'
    jars = app/'lib/app'
    if not jvm.is_file() or not jars.is_dir():
        raise RuntimeError('Native QuPath failed. --qupath-bridge requires an official unpacked application; provide its bin/QuPath path, not a custom wrapper.')
    isolated = root/'qupath-bridge-state'
    isolated.mkdir(exist_ok=True)
    helper = root/'qupath-bridge.py'
    config = root/'qupath-bridge-config.json'
    write_json(config, {'jvm':str(jvm), 'classpath':str(jars/'*'), 'state_dir':str(isolated)})
    code = r'''#!/usr/bin/env python3
# Generated isolated QuPath launch adapter. No image conversion or verification is emulated.
import json, os, sys
from pathlib import Path
import jpype
config=json.loads(Path(sys.argv[1]).read_text())
state=Path(config['state_dir'])
for sub in ('tmp','preferences','javacpp'):
    (state/sub).mkdir(exist_ok=True)
jpype.startJVM(config['jvm'], '-Xmx2G', '-XX:ActiveProcessorCount=4',
    '-Djava.awt.headless=true', '-Djava.io.tmpdir='+str(state/'tmp'),
    '-Djava.util.prefs.userRoot='+str(state/'preferences'),
    '-Dqupath.prefs.name=qupath-slide-converter-bridge',
    '-Dorg.bytedeco.javacpp.cachedir='+str(state/'javacpp'),
    classpath=[config['classpath']])
try:
    jpype.JClass('qupath.QuPath').main(sys.argv[2:])
finally:
    jpype.shutdownJVM()
'''
    if helper.exists() and helper.read_text()!=code:
        raise RuntimeError('Unrecognized existing QuPath bridge helper; choose a new runtime root')
    helper.write_text(code)
    launcher = root/'qupath-headless'
    shell='#!/bin/sh\nexec '+shlex.quote(runtime['python'])+' '+shlex.quote(str(helper))+' '+shlex.quote(str(config))+' "$@"\n'
    if launcher.exists() and launcher.read_text()!=shell:
        raise RuntimeError('Unrecognized existing QuPath bridge launcher; choose a new runtime root')
    launcher.write_text(shell)
    launcher.chmod(0o700)
    runtime['qupath'] = str(launcher)
    runtime['qupath_launch_method'] = 'explicit_jpype_bundled_jvm_headless'
    runtime['qupath_app'] = str(app)
    return runtime


def smoke_test(runtime, root):
    """Actual LZW image, Java converter, Zarr, TIFF and optional QuPath I/O."""
    work = Path(tempfile.mkdtemp(prefix='smoke-', dir=root))
    py = runtime['python']
    script = work / 'smoke.py'
    script.write_text('''import json,sys\nfrom pathlib import Path\nimport numpy as np, tifffile, zarr, jpype, imagecodecs, PIL\ntry:\n import openslide\n openslide_version=openslide.__version__\nexcept ImportError:\n openslide_version=None\np=Path(sys.argv[1])\na=(np.arange(2*96*128,dtype=np.uint16).reshape(2,96,128)*3)\nif sys.argv[2]=='write':\n tifffile.imwrite(p/'input.ome.tiff',a,ome=True,compression='lzw',metadata={'axes':'CYX','Channel':{'Name':['One','Two']}})\n assert np.array_equal(a,tifffile.imread(p/'input.ome.tiff'))\n print(json.dumps({'numpy':np.__version__,'tifffile':tifffile.__version__,'zarr':zarr.__version__,'jpype':jpype.__version__,'openslide':openslide_version}))\nelse:\n b=tifffile.imread(p/'output.ome.tiff')\n assert b.dtype==a.dtype and b.shape==a.shape and np.array_equal(a,b),(a.shape,b.shape)\n print('PASS: actual uint16 two-channel LZW roundtrip')\n''')
    native_tmp = root / 'tmp'
    native_tmp.mkdir(exist_ok=True)
    env = dict(os.environ, JAVA_HOME=runtime['java_home'], TMPDIR=str(native_tmp), BIOFORMATS2RAW_JVM_OPTS='-Xmx2g -Djava.io.tmpdir='+str(native_tmp), RAW2OMETIFF_JVM_OPTS='-Xmx2g -Djava.io.tmpdir='+str(native_tmp))
    env['PATH'] = str(Path(runtime['python']).parent) + os.pathsep + env.get('PATH', '')
    env['LD_LIBRARY_PATH'] = str(Path(runtime['python']).parent.parent / 'lib') + os.pathsep + env.get('LD_LIBRARY_PATH', '')
    versions_log = run([py, script, work, 'write'], root, 'smoke-python', env)
    versions = json.loads(versions_log.read_text().strip().splitlines()[-1])
    run([Path(runtime['bioformats2raw_home'])/'bin/bioformats2raw', work/'input.ome.tiff', work/'zarr', '--ngff-version', '0.4', '--max-workers', '2', '--resolutions', '1'], root, 'smoke-bioformats2raw', env, 600)
    run([Path(runtime['raw2ometiff_home'])/'bin/raw2ometiff', work/'zarr', work/'output.ome.tiff', '--compression', 'LZW', '--max_workers', '2'], root, 'smoke-raw2ometiff', env, 600)
    run([py, script, work, 'read'], root, 'smoke-roundtrip', env)
    result = {'status': 'PASS', 'type': 'synthetic_uint16_two_channel_complete_pixel_equality', 'work_dir': str(work), 'qupath': 'NOT_RUN', 'package_versions': versions,
              'openslide_available': bool(versions.get('openslide'))}
    if runtime.get('qupath'):
        expected = work/'expected.json'
        write_json(expected, {'X':128,'Y':96,'C':2,'Z':1,'T':1,'dtype':'uint16','rgb':False,'channels':['One','Two']})
        qp_env = dict(os.environ, TMPDIR=str(native_tmp))
        run([runtime['qupath'],'script','-i',work/'output.ome.tiff','-a',work/'qupath.json','-a',expected,Path(__file__).with_name('validate_qupath.groovy')], root, 'smoke-qupath', qp_env, 300)
        validated = json.loads((work/'qupath.json').read_text())
        if validated.get('status') != 'PASS':
            raise RuntimeError('QuPath smoke validation did not pass')
        result['qupath'] = 'PASS'
    return result


def setup(args):
    info = platform_report()
    root = Path(args.root).expanduser().absolute()
    plan = {'root': str(root), 'platform': info, 'artifacts': ARTIFACTS, 'packages': PACKAGES,
            'with_qupath': args.with_qupath, 'qupath_bridge_requested':getattr(args, 'qupath_bridge', False), 'downloads_slide_data': False,
            'kfb_test_input': getattr(args, 'kfb_test_input', None),
            'kfbf_test_input': getattr(args, 'kfbf_test_input', None),
            'modifies_system': False, 'minimum_free_bytes': 8 * 1024**3}
    # dry-run performs no filesystem mutation and no network calls, even on macOS.
    if args.dry_run:
        plan['status'] = 'DRY_RUN'
        return plan
    require_supported(info)
    if os.geteuid() == 0:
        raise RuntimeError('Run as an ordinary user; this installer does not support root/sudo.')
    if root in (Path('/'), Path.home(), Path('/usr'), Path('/opt')) or root.is_symlink():
        raise RuntimeError('Choose a dedicated user-owned runtime directory, not a system/home root or symlink.')
    root.mkdir(parents=True, exist_ok=True)
    if root.stat().st_uid != os.geteuid():
        raise RuntimeError('Runtime root must be owned by the current user')
    import fcntl
    with (root/'.setup.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another setup is already using this runtime root')
        if shutil.disk_usage(root).free < plan['minimum_free_bytes']:
            raise RuntimeError('At least 8 GiB free space required for isolated environment setup (slides need additional space).')
        native_tmp = root / 'tmp'
        native_tmp.mkdir(exist_ok=True)
        check = native_tmp / ('exec-check-' + str(os.getpid()))
        try:
            check.write_text('#!/bin/sh\nexit 0\n')
            check.chmod(0o700)
            subprocess.run([str(check)], check=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('Runtime directory does not allow native execution; select a user-owned directory on an executable filesystem') from exc
        finally:
            check.unlink(missing_ok=True)
        write_json(root/'setup-plan.json', plan)
        py = Path(args.reuse_python).expanduser() if args.reuse_python else build_python(root)
        b2r = Path(args.bioformats2raw_home).expanduser() if args.bioformats2raw_home else install_archive('bioformats2raw', root)
        r2o = Path(args.raw2ometiff_home).expanduser() if args.raw2ometiff_home else install_archive('raw2ometiff', root)
        qp = args.qupath or ((shutil.which('QuPath') or shutil.which('qupath')) if args.with_qupath else None)
        if args.with_qupath and not qp:
            directory = install_archive('qupath', root)
            matches = [p for p in directory.rglob('*') if p.is_file() and p.parent.name=='bin' and p.name.startswith('QuPath') and os.access(p, os.X_OK)]
            if len(matches) != 1:
                raise RuntimeError('Could not identify a unique QuPath launcher in the verified archive')
            qp = str(matches[0])
        runtime = runtime_from_paths(py, b2r, r2o, qp)
        runtime['qupath_launch_method'] = 'native' if qp else 'not_available'
        if args.qupath_bridge:
            if not qp:
                raise RuntimeError('--qupath-bridge requires --with-qupath or an explicit --qupath application path')
            runtime = prepare_qupath_bridge(runtime, root)
        try:
            runtime['smoke_test'] = smoke_test(runtime, root)
        except CommandFailure as exc:
            if not (args.qupath_bridge and runtime['qupath_launch_method']=='native' and exc.stage=='smoke-qupath' and exc.returncode < 0):
                raise
            # Preserve the native crash log; a successful version probe is not a read test.
            shutil.copyfile(root/'logs/smoke-qupath.log', root/'logs/smoke-qupath-native-failure.log')
            runtime = prepare_qupath_bridge(runtime, root, {'stage':exc.stage,'returncode':exc.returncode})
            runtime['smoke_test'] = smoke_test(runtime, root)
        runtime.setdefault('capabilities', {})['kfb'] = kfb_readiness(runtime['python'], getattr(args, 'kfb_test_input', None))
        runtime['capabilities']['kfbf'] = kfbf_readiness(runtime['python'], getattr(args, 'kfbf_test_input', None))
        runtime['platform'] = info
        runtime['managed_release_pins'] = {k:v for k,v in ARTIFACTS.items() if (k!='qupath' or args.with_qupath)}
        write_json(root/'runtime.json', runtime)
        if getattr(args, 'kfb_test_input', None) and not runtime['capabilities']['kfb'].get('file_read_probe_passed'):
            raise RuntimeError('Generic environment checks passed, but the KFB input probe did not pass: '+runtime['capabilities']['kfb']['status']+'. See '+str(root/'runtime.json'))
        if getattr(args, 'kfbf_test_input', None) and not runtime['capabilities']['kfbf'].get('file_read_probe_passed'):
            raise RuntimeError('Generic environment checks passed, but the KFBF input probe did not pass: '+runtime['capabilities']['kfbf']['status']+'. See '+str(root/'runtime.json'))
        return {'status':'READY', 'config': str(root/'runtime.json'), 'runtime':runtime}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    d = commands.add_parser('doctor', help='Read-only inspection; never claims validation from file presence')
    d.add_argument('--config')
    d.add_argument('--kfb-test-input', help='Read-only check of a real KFB full-resolution source JPEG tile')
    d.add_argument('--kfbf-test-input', help='Read-only independent fluorescence channel check of a real KFBF')
    k = commands.add_parser('_kfb-probe', help=argparse.SUPPRESS)
    k.add_argument('--input')
    f = commands.add_parser('_kfbf-probe', help=argparse.SUPPRESS)
    f.add_argument('--input')
    s = commands.add_parser('setup', help='Prepare an isolated user-owned Linux environment')
    s.add_argument('--root', required=True)
    s.add_argument('--kfb-test-input', help='After generic setup, test a real KFB input and record coverage warnings without filling pixels')
    s.add_argument('--kfbf-test-input', help='After generic setup, test all independent KFBF channels without converting the slide')
    s.add_argument('--with-qupath', action='store_true')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--qupath-bridge', action='store_true', help='Explicit headless bundled-JVM adapter only if the native QuPath launcher fails')
    for name in ('reuse-python', 'bioformats2raw-home', 'raw2ometiff-home', 'qupath'):
        s.add_argument('--'+name)
    args = parser.parse_args()
    try:
        if args.command == '_kfb-probe':
            result = kfb_probe_in_process(args.input)
        elif args.command == '_kfbf-probe':
            result = kfbf_probe_in_process(args.input)
        elif args.command == 'doctor':
            result = doctor(args.config, args.kfb_test_input, args.kfbf_test_input)
        else:
            result = setup(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status':'ERROR', 'error':str(exc), 'exception':type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
