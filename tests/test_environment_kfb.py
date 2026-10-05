"""Offline KFB readiness contract tests: no input conversion and no environment changes."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('kfb_environment', Path(__file__).resolve().parents[1]/'scripts/environment.py')
environment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(environment)


class KFBEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_configured_runtime_python_is_used_read_only(self):
        config=self.root/'runtime.json'
        config.write_text(json.dumps({'python':'/opt/runtime with space/bin/python'}))
        reply={'status':'DEPENDENCIES_AVAILABLE_UNVALIDATED','file_read_probe_passed':False,'full_slide_validated':False}
        child=SimpleNamespace(returncode=0,stdout=json.dumps(reply),stderr='')
        with patch.object(environment.subprocess,'run',return_value=child) as run:
            result=environment.doctor(config)
        self.assertEqual(run.call_args.args[0][0],'/opt/runtime with space/bin/python')
        self.assertEqual(run.call_args.kwargs['env']['PYTHONDONTWRITEBYTECODE'],'1')
        self.assertNotIn('shell',run.call_args.kwargs)
        self.assertFalse(result['kfb']['file_read_probe_passed'])
        self.assertFalse(result['runtime_validated'])

    def test_default_python_does_not_claim_real_file_test(self):
        reply={'status':'DEPENDENCIES_AVAILABLE_UNVALIDATED','file_read_probe_passed':False,'full_slide_validated':False}
        with patch.object(environment.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(reply),stderr='')) as run:
            result=environment.kfb_readiness()
        self.assertEqual(run.call_args.args[0][0],sys.executable)
        self.assertEqual(result['status'],'DEPENDENCIES_AVAILABLE_UNVALIDATED')
        self.assertFalse(result['file_read_probe_passed'])

    def test_missing_dependencies_produce_isolated_setup_guidance(self):
        reply={'status':'DEPENDENCIES_MISSING','file_read_probe_passed':False,'full_slide_validated':False,'missing_dependencies':['Pillow']}
        with patch.object(environment.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(reply),stderr='')):
            result=environment.kfb_readiness('/runtime/python')
        self.assertIn('setup --root',result['setup_command'])
        self.assertNotIn('pip install',result['setup_command'])
        self.assertFalse(result['file_read_probe_passed'])

    def test_process_failure_not_reported_as_reader_success(self):
        child=SimpleNamespace(returncode=7,stdout='',stderr='cannot import dependencies')
        with patch.object(environment.subprocess,'run',return_value=child):
            result=environment.kfb_readiness('/runtime/python')
        self.assertEqual(result['status'],'PROBE_PROCESS_FAILED')
        self.assertEqual(result['returncode'],7)
        self.assertIn('setup_command',result)

    def test_timeout_has_bounded_diagnostic(self):
        with patch.object(environment.subprocess,'run',side_effect=subprocess.TimeoutExpired('probe',120)):
            result=environment.kfb_readiness('/runtime/python')
        self.assertEqual(result['status'],'PROBE_PROCESS_FAILED')
        self.assertFalse(result['full_slide_validated'])

    def test_invalid_child_json_is_not_success(self):
        with patch.object(environment.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='done',stderr='')):
            result=environment.kfb_readiness('/runtime/python')
        self.assertEqual(result['status'],'PROBE_PROCESS_FAILED')

    def test_input_path_is_one_literal_argument(self):
        source=self.root/'slide $(touch unwanted); name.kfb'
        reply={'status':'FILE_READ_FAILED','file_read_probe_passed':False,'full_slide_validated':False}
        with patch.object(environment.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(reply),stderr='')) as run:
            environment.kfb_readiness('/runtime/python',source)
        self.assertEqual(run.call_args.args[0][-2:],['--input',str(source)])
        self.assertFalse((self.root/'unwanted').exists())

    def test_kfbf_is_rejected_before_optional_import_or_reader(self):
        source=self.root/'fluorescence.kfb'
        source.write_bytes(b'KFBF'+b'\0'*96)
        with patch.object(environment.importlib,'import_module',side_effect=AssertionError('must not import decoder')):
            result=environment.kfb_probe_in_process(source)
        self.assertEqual(result['status'],'UNSUPPORTED_VARIANT')
        self.assertEqual(result['code'],'KFBF_WRONG_READER')
        self.assertFalse(result['file_read_probe_passed'])
        self.assertFalse(result['capability']['independent_fluorescence_channels'])

    def test_missing_input_does_not_trigger_installation(self):
        with patch.object(environment.importlib,'import_module',side_effect=AssertionError('must not import decoder')):
            result=environment.kfb_probe_in_process(self.root/'missing.kfb')
        self.assertEqual(result['status'],'INPUT_ERROR')
        self.assertFalse(result['network_used'])

    def test_import_only_cannot_be_file_validation(self):
        packages=SimpleNamespace(__version__='test')
        reader=SimpleNamespace(StrictKFB=lambda path: (_ for _ in ()).throw(AssertionError('no file provided')))
        with patch.object(environment.importlib,'import_module',return_value=packages), patch.dict(sys.modules,{'kfb_reader':reader}):
            result=environment.kfb_probe_in_process()
        self.assertEqual(result['status'],'DEPENDENCIES_AVAILABLE_UNVALIDATED')
        self.assertTrue(result['dependencies_available'])
        self.assertFalse(result['file_read_probe_passed'])
        self.assertFalse(result['full_slide_validated'])

    def test_native_import_error_reports_missing_package(self):
        def fake_import(name):
            if name=='imagecodecs':
                raise OSError('native library unavailable')
            return SimpleNamespace(__version__='test')
        with patch.object(environment.importlib,'import_module',side_effect=fake_import):
            result=environment.kfb_probe_in_process()
        self.assertEqual(result['status'],'DEPENDENCIES_MISSING')
        self.assertEqual(result['missing_dependencies'],['imagecodecs'])


    def test_actual_decode_contract_keeps_coverage_policy_separate(self):
        source=self.root/'sparse.kfb'
        original=b'KFB0'+b'\0'*96
        source.write_bytes(original)
        class Tile:
            dtype='uint8'
            ndim=3
            shape=(2,2,3)
            size=12
            def tobytes(self): return b'\1'*12
        class Reader:
            def __init__(self,path): self.dimensions=(100,200); self.levels=[SimpleNamespace(entries=[object()])]
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def decode_tile(self,entry): return Tile()
            def coverage_report(self,base): return {'missing_pixels':99,'overlapping_pixels_counted_with_multiplicity':0}
        numpy=SimpleNamespace(ndarray=Tile,uint8='uint8',ascontiguousarray=lambda x:x)
        with patch.object(environment.importlib,'import_module',return_value=SimpleNamespace(__version__='test')), patch.dict(sys.modules,{'kfb_reader':SimpleNamespace(StrictKFB=Reader),'numpy':numpy}):
            result=environment.kfb_probe_in_process(source)
        self.assertEqual(result['status'],'FILE_READ_PASS')
        self.assertTrue(result['file_read_probe_passed'])
        self.assertTrue(result['coverage_policy_required'])
        self.assertFalse(result['full_slide_validated'])
        self.assertEqual(source.read_bytes(),original)
        self.assertEqual(result['decoded_tile']['shape'],[2,2,3])



    def test_native_section_header_kfbf_rejected_before_imports(self):
        source=self.root/'native-fluorescence.kfb'
        source.write_bytes(b'\xf1\x01\xee\xeeKFBF'+b'\0'*88)
        with patch.object(environment.importlib,'import_module',side_effect=AssertionError('must not import decoder')):
            result=environment.kfb_probe_in_process(source)
        self.assertEqual(result['code'],'KFBF_WRONG_READER')
        self.assertFalse(result['file_read_probe_passed'])


if __name__=='__main__':
    unittest.main()
