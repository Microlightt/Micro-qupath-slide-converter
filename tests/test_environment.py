"""Offline bootstrap safety tests; no downloads or user environment mutation."""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location('environment', Path(__file__).resolve().parents[1]/'scripts/environment.py')
environment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(environment)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_dry_run_creates_nothing_and_never_downloads(self):
        args = argparse.Namespace(root=str(self.root/'absent'), with_qupath=True, dry_run=True)
        with patch.object(environment, 'download', side_effect=AssertionError('network')):
            result = environment.setup(args)
        self.assertEqual(result['status'], 'DRY_RUN')
        self.assertFalse((self.root/'absent').exists())
        self.assertFalse(result['modifies_system'])

    def test_darwin_arm_linux_and_old_glibc_are_explicitly_unsupported(self):
        cases = [('Darwin','arm64','', ''), ('Linux','aarch64','glibc','2.35'), ('Linux','x86_64','musl','1.2'), ('Linux','x86_64','glibc','2.17')]
        for system, machine, libc, version in cases:
            with self.subTest(system=system,machine=machine,libc=libc,version=version):
                with self.assertRaises(RuntimeError):
                    environment.require_supported(dict(system=system,machine=machine,libc=libc,libc_version=version))

    def test_current_glibc_linux_supported(self):
        environment.require_supported(dict(system='Linux',machine='x86_64',libc='glibc',libc_version='2.35'))

    def test_doctor_missing_paths_never_claims_validation(self):
        config=self.root/'runtime.json'
        config.write_text(json.dumps({'python':str(self.root/'absent')}))
        result=environment.doctor(config)
        self.assertEqual(len(result['missing_paths']),1)
        self.assertFalse(result['runtime_validated'])

    def test_download_checks_hash_before_finalizing(self):
        content=b'official test bytes'
        artifact={'url':'https://example.test/test.zip','sha256':hashlib.sha256(content).hexdigest()}
        with patch.object(environment.urllib.request,'urlopen',return_value=io.BytesIO(content)):
            result=environment.download(artifact,self.root)
        self.assertEqual(result.read_bytes(),content)
        self.assertFalse((self.root/'test.zip.part').exists())

    def test_download_wrong_hash_is_never_promoted(self):
        artifact={'url':'https://example.test/test.zip','sha256':'0'*64}
        with patch.object(environment.urllib.request,'urlopen',return_value=io.BytesIO(b'bad')):
            with self.assertRaisesRegex(RuntimeError,'checksum'):
                environment.download(artifact,self.root)
        self.assertFalse((self.root/'test.zip').exists())
        self.assertTrue((self.root/'test.zip.part').exists())

    def test_cached_wrong_hash_does_not_trigger_overwrite(self):
        (self.root/'test.zip').write_bytes(b'preserve')
        with patch.object(environment.urllib.request,'urlopen',side_effect=AssertionError('network')):
            with self.assertRaisesRegex(RuntimeError,'checksum'):
                environment.download({'url':'https://example.test/test.zip','sha256':'0'*64},self.root)
        self.assertEqual((self.root/'test.zip').read_bytes(),b'preserve')

    def test_interrupted_network_is_not_success(self):
        with patch.object(environment.urllib.request,'urlopen',side_effect=TimeoutError('network')):
            with self.assertRaises(TimeoutError):
                environment.download({'url':'https://example.test/test.zip','sha256':'0'*64},self.root)
        self.assertFalse((self.root/'test.zip').exists())

    def test_existing_partial_is_redownloaded_and_verified(self):
        (self.root/'test.zip.part').write_bytes(b'old partial')
        data=b'verified'
        artifact={'url':'https://example.test/test.zip','sha256':hashlib.sha256(data).hexdigest()}
        with patch.object(environment.urllib.request,'urlopen',return_value=io.BytesIO(data)):
            environment.download(artifact,self.root)
        self.assertEqual((self.root/'test.zip').read_bytes(),data)

    def test_zip_path_traversal_blocked(self):
        archive=self.root/'unsafe.zip'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('../escaped',b'x')
        with self.assertRaisesRegex(RuntimeError,'Unsafe'):
            environment.extract_archive(archive,self.root/'extract')
        self.assertFalse((self.root/'escaped').exists())

    def test_zip_symlinks_blocked(self):
        archive=self.root/'unsafe.zip'
        item=zipfile.ZipInfo('tool/link')
        item.create_system=3
        item.external_attr=(stat.S_IFLNK | 0o777)<<16
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr(item,'../../escape')
        with self.assertRaisesRegex(RuntimeError,'Symlinks'):
            environment.extract_archive(archive,self.root/'extract')

    def test_tar_link_escape_blocked(self):
        archive=self.root/'unsafe.tar'
        with tarfile.open(archive,'w') as t:
            item=tarfile.TarInfo('escape')
            item.type=tarfile.SYMTYPE
            item.linkname='../outside'
            t.addfile(item)
        with self.assertRaisesRegex(RuntimeError,'escapes'):
            environment.extract_archive(archive,self.root/'extract')

    def test_tar_internal_link_allowed_and_executables_preserved(self):
        archive=self.root/'safe.tar'
        with tarfile.open(archive,'w') as t:
            item=tarfile.TarInfo('tool/bin/run')
            item.size=2
            item.mode=0o755
            t.addfile(item,io.BytesIO(b'hi'))
            link=tarfile.TarInfo('tool/run')
            link.type=tarfile.SYMTYPE
            link.linkname='bin/run'
            t.addfile(link)
        environment.extract_archive(archive,self.root/'extract')
        self.assertEqual((self.root/'extract/tool/run').read_bytes(),b'hi')
        self.assertTrue((self.root/'extract/tool/bin/run').stat().st_mode & 0o111)

    def test_archive_does_not_overwrite_existing_directory(self):
        destination=self.root/'existing'
        destination.mkdir()
        with self.assertRaises(FileExistsError):
            environment.extract_archive(self.root/'unused',destination)

    def test_unrecognized_tool_directory_is_preserved(self):
        artifact=environment.ARTIFACTS['bioformats2raw']
        target=self.root/'tools'/('bioformats2raw-'+artifact['version'])
        target.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError,'refusing overwrite'):
            environment.install_archive('bioformats2raw',self.root)

    def test_incomplete_bootstrap_is_not_reinstalled_over(self):
        (self.root/'miniforge').mkdir()
        with self.assertRaisesRegex(RuntimeError,'Incomplete'):
            environment.build_python(self.root)


    def test_bridge_does_not_replace_working_native_launcher(self):
        runtime={'qupath':str(self.root/'bin/QuPath'),'python':'/usr/bin/python3'}
        probe=SimpleNamespace(returncode=0,stdout='0.6.0',stderr='')
        with patch.object(environment.subprocess,'run',return_value=probe):
            result=environment.prepare_qupath_bridge(runtime,self.root)
        self.assertEqual(result['qupath_launch_method'],'native')
        self.assertFalse((self.root/'qupath-headless').exists())

    def test_bridge_requires_real_bundled_jvm(self):
        runtime={'qupath':str(self.root/'custom-wrapper'),'python':'/usr/bin/python3'}
        probe=SimpleNamespace(returncode=-11,stdout='',stderr='')
        with patch.object(environment.subprocess,'run',return_value=probe):
            with self.assertRaisesRegex(RuntimeError,'official unpacked'):
                environment.prepare_qupath_bridge(runtime,self.root)
        self.assertFalse((self.root/'qupath-headless').exists())

    def test_bridge_records_failure_and_quotes_paths(self):
        app=self.root/'app with space'
        jvm=app/'lib/runtime/lib/server/libjvm.so'
        jvm.parent.mkdir(parents=True)
        jvm.write_bytes(b'test')
        (app/'lib/app').mkdir()
        runtime={'qupath':str(app/'bin/QuPath'),'python':'/opt/python with space/bin/python'}
        probe=SimpleNamespace(returncode=-11,stdout='',stderr='native crash')
        with patch.object(environment.subprocess,'run',return_value=probe):
            result=environment.prepare_qupath_bridge(runtime,self.root)
        self.assertEqual(result['qupath_native_probe_exit'],-11)
        self.assertEqual(result['qupath_launch_method'],'explicit_jpype_bundled_jvm_headless')
        self.assertIn("'/opt/python with space/bin/python'",(self.root/'qupath-headless').read_text())
        self.assertIn('qupath.QuPath',(self.root/'qupath-bridge.py').read_text())



    def test_bridge_can_follow_native_read_crash_despite_good_version(self):
        app=self.root/'app'
        jvm=app/'lib/runtime/lib/server/libjvm.so'
        jvm.parent.mkdir(parents=True)
        jvm.write_bytes(b'test')
        (app/'lib/app').mkdir()
        runtime={'qupath':str(app/'bin/QuPath'),'python':'/usr/bin/python3'}
        probe=SimpleNamespace(returncode=0,stdout='0.6.0',stderr='')
        with patch.object(environment.subprocess,'run',return_value=probe):
            result=environment.prepare_qupath_bridge(runtime,self.root,{'stage':'smoke-qupath','returncode':-11})
        self.assertEqual(result['qupath_native_read_failure']['returncode'],-11)
        self.assertEqual(result['qupath_launch_method'],'explicit_jpype_bundled_jvm_headless')


if __name__=='__main__':
    unittest.main()
