"""KFBF capability probing must be read-only and distinguish data from dependencies."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import environment
from slide_probe import vendor_kind, resolve_input, route, SlideError

class EnvironmentKFBFTests(unittest.TestCase):
    def test_imports_do_not_claim_real_reader_validation(self):
        with patch.object(environment.importlib,'import_module',return_value=SimpleNamespace(__version__='test')):
            result=environment.kfbf_probe_in_process()
        self.assertEqual(result['status'],'DEPENDENCIES_AVAILABLE_UNVALIDATED')
        self.assertFalse(result['file_read_probe_passed'])
        self.assertFalse(result['full_slide_validated'])

    def test_missing_native_dependency_is_not_success(self):
        def importer(name):
            if name=='imagecodecs':raise OSError('native library missing')
            return SimpleNamespace(__version__='test')
        with patch.object(environment.importlib,'import_module',side_effect=importer):
            result=environment.kfbf_probe_in_process()
        self.assertEqual(result['status'],'DEPENDENCIES_MISSING')
        self.assertEqual(result['missing_dependencies'],['imagecodecs'])
        self.assertFalse(result['network_used'])
        self.assertFalse(result['source_modified'])

    def test_probe_uses_configured_interpreter_literal_input(self):
        reply=dict(status='FILE_READ_FAILED',file_read_probe_passed=False,full_slide_validated=False)
        with patch.object(environment.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(reply),stderr='')) as run:
            environment.kfbf_readiness('/runtime with space/python','/data/slide $(command).kfbf')
        self.assertEqual(run.call_args.args[0][0],'/runtime with space/python')
        self.assertEqual(run.call_args.args[0][-1],'/data/slide $(command).kfbf')
        self.assertNotIn('shell',run.call_args.kwargs)

    def test_invalid_probe_protocol_never_becomes_pass(self):
        for output in ['null','{}','success']:
            with self.subTest(output=output), patch.object(environment.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=output,stderr='')):
                result=environment.kfbf_readiness()
            self.assertEqual(result['status'],'PROBE_PROCESS_FAILED')
            self.assertFalse(result['file_read_probe_passed'])
            self.assertIn('setup_command',result)

    def test_header_routes_kfb_named_fluorescence_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fluorescence.kfb'
            path.write_bytes(b'\xf1\x01\xee\xeeKFBF'+bytes(100))
            self.assertEqual(vendor_kind(resolve_input(path)),'kfbf')
            path.write_bytes(b'not-a-slide')
            with self.assertRaises(SlideError):resolve_input(path)

    def test_three_independent_channels_never_become_brightfield(self):
        metadata=dict(X=100,Y=100,C=3,Z=1,T=1,rgb=False,dtype='uint8',effective_channels=3)
        self.assertEqual(route(metadata,'auto'),'ome-tiff')
        with self.assertRaises(SlideError):route(metadata,'brightfield')

if __name__=='__main__':unittest.main()
