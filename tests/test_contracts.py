#!/usr/bin/env python3
"""Public, synthetic contract tests; no microscopy data or network required.

Run with ``python3 -m unittest discover -s tests -v`` from the skill directory.
These tests establish protection/routing behavior, not clinical image validity.
"""
import copy
import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = load_module('slide_probe')
converter = load_module('convert_slide')


def metadata(**overrides):
    result = dict(X=1024, Y=1536, C=3, Z=1, T=1, dtype='uint16',
                  rgb=False, rgb_samples=1, effective_channels=3,
                  channels=['DAPI', 'FITC', 'Cy5'])
    result.update(overrides)
    return result


class InputContracts(unittest.TestCase):
    def test_missing_mrxs_fails_without_installed_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'slide.mrxs'
            original = b'entry placeholder, not a full-resolution image'
            source.write_bytes(original)
            run = subprocess.run(
                [sys.executable, '-S', str(SCRIPTS / 'slide_probe.py'),
                 '--input', str(source), '--preflight-only'],
                text=True, capture_output=True, check=False)
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result['status'], 'INCOMPLETE_SLIDE')
            self.assertTrue(any(p.endswith('Slidedat.ini') for p in result['missing']))
            self.assertTrue(any(p.endswith('Index.dat') for p in result['missing']))
            self.assertTrue(any(p.endswith('Data*.dat') for p in result['missing']))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(Path(tmp).iterdir()), [source])

    def test_mrxs_declared_missing_data_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'slide.MRXS'
            source.write_bytes(b'entry')
            companion = source.with_suffix('')
            companion.mkdir()
            (companion / 'Slidedat.ini').write_text(
                '[DATAFILE]\nFILE_0=Data0000.dat\nFILE_1=Data0001.dat\n')
            (companion / 'Index.dat').write_bytes(b'index')
            (companion / 'Data0000.dat').write_bytes(b'data')
            with self.assertRaises(probe.SlideError) as raised:
                probe.resolve_input(source)
            self.assertEqual(raised.exception.code, 'INCOMPLETE_SLIDE')
            self.assertIn(str(companion.resolve() / 'Data0001.dat'), raised.exception.details['missing'])

    def test_complete_unicode_mrxs_dataset_is_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'synthetic 切片 01.mrxs'
            source.write_bytes(b'entry')
            companion = source.with_suffix('')
            companion.mkdir()
            (companion / 'Slidedat.ini').write_text('[DATAFILE]\nFILE_0=Data0000.dat\n')
            (companion / 'Index.dat').write_bytes(b'index')
            (companion / 'Data0000.dat').write_bytes(b'data')
            (companion / '._Data0000.dat').write_bytes(b'finder metadata')
            self.assertEqual(probe.resolve_input(source), source.resolve())
            self.assertEqual(len(probe.source_files(source)), 4)

    def test_multiple_input_candidates_need_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ['first.tiff', 'second.tiff']:
                (Path(tmp) / name).write_bytes(b'synthetic')
            with self.assertRaises(probe.SlideError) as raised:
                probe.resolve_input(tmp)
            self.assertEqual(raised.exception.code, 'SELECT_INPUT')
            self.assertEqual(len(raised.exception.details['candidates']), 2)

    def test_unknown_extension_does_not_get_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'unknown.vendorblob'
            source.write_bytes(b'untouched')
            with self.assertRaises(probe.SlideError) as raised:
                probe.resolve_input(source)
            self.assertEqual(raised.exception.code, 'UNSUPPORTED_FORMAT')
            self.assertEqual(source.read_bytes(), b'untouched')


class RouteContracts(unittest.TestCase):
    def assert_route_error(self, code, source, image_type='auto'):
        with self.assertRaises(probe.SlideError) as raised:
            probe.route(source, image_type)
        self.assertEqual(raised.exception.code, code)

    def test_independent_channel_count_does_not_imply_rgb(self):
        for channels in [1, 2, 3, 6, 8, 17]:
            for dtype in ['uint8', 'uint16']:
                with self.subTest(channels=channels, dtype=dtype):
                    source = metadata(C=channels, effective_channels=channels,
                                      dtype=dtype, channels=['channel-%d' % i for i in range(channels)])
                    before = copy.deepcopy(source)
                    self.assertEqual(probe.route(source), 'ome-tiff')
                    self.assertEqual(source, before)

    def test_rgb_needs_brightfield_confirmation(self):
        source = metadata(rgb=True, rgb_samples=3, effective_channels=1, dtype='uint8')
        self.assert_route_error('SELECT_MODALITY', source)
        self.assert_route_error('SELECT_MODALITY', source, 'fluorescence')
        self.assertEqual(probe.route(source, 'brightfield'), 'svs')

    def test_brightfield_does_not_silently_reduce_bit_depth(self):
        source = metadata(rgb=True, rgb_samples=3, effective_channels=1, dtype='uint16')
        self.assert_route_error('UNSUPPORTED_BRIGHTFIELD_DEPTH', source, 'brightfield')

    def test_unsupported_dimensions_are_not_discarded(self):
        for z, t in [(2, 1), (1, 2), (3, 4)]:
            with self.subTest(z=z, t=t):
                self.assert_route_error('UNSUPPORTED_DIMENSIONS', metadata(Z=z, T=t))

    def test_unsupported_dtype_is_not_cast(self):
        for dtype in ['float32', 'int16', 'uint32']:
            with self.subTest(dtype=dtype):
                self.assert_route_error('UNSUPPORTED_PIXEL_TYPE', metadata(dtype=dtype))

    def test_rgba_and_nonindependent_layout_are_rejected(self):
        self.assert_route_error('UNSUPPORTED_RGB', metadata(C=4, rgb=True, rgb_samples=4), 'brightfield')
        self.assert_route_error('UNSUPPORTED_CHANNEL_LAYOUT', metadata(C=3, effective_channels=1))

    def test_grayscale_brightfield_is_not_recolored(self):
        self.assert_route_error('UNSUPPORTED_BRIGHTFIELD_LAYOUT',
                                metadata(C=1, effective_channels=1), 'brightfield')


class OutputContracts(unittest.TestCase):
    def test_same_source_or_ancestor_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.tiff'
            source.write_bytes(b'original')
            for output in [source, Path(tmp)]:
                with self.subTest(output=str(output)):
                    with self.assertRaises(probe.SlideError) as raised:
                        converter.validate_output(source, output, resume=True)
                    self.assertIn(raised.exception.code, ['UNSAFE_OUTPUT', 'OUTPUT_EXISTS'])
            self.assertEqual(source.read_bytes(), b'original')

    def test_existing_directory_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.tiff'
            source.write_bytes(b'original')
            output = Path(tmp) / 'output'
            output.mkdir()
            sentinel = output / 'image.ome.tiff'
            sentinel.write_bytes(b'existing output')
            with self.assertRaises(probe.SlideError) as raised:
                converter.validate_output(source, output)
            self.assertEqual(raised.exception.code, 'OUTPUT_EXISTS')
            self.assertEqual(sentinel.read_bytes(), b'existing output')

    def test_mrxs_companion_cannot_be_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.mrxs'
            source.write_bytes(b'entry')
            companion = source.with_suffix('')
            companion.mkdir()
            with self.assertRaises(probe.SlideError) as raised:
                converter.validate_output(source, companion / 'converted')
            self.assertEqual(raised.exception.code, 'UNSAFE_OUTPUT')

    def test_resume_does_not_allow_symlink_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.tiff'
            source.write_bytes(b'original')
            target = Path(tmp) / 'target'
            target.mkdir()
            link = Path(tmp) / 'output-link'
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(probe.SlideError) as raised:
                converter.validate_output(source, link, resume=True)
            self.assertEqual(raised.exception.code, 'UNSAFE_OUTPUT')

    def test_new_output_validation_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.tiff'
            source.write_bytes(b'original')
            output = Path(tmp) / 'new' / 'output'
            converter.validate_output(source, output)
            self.assertFalse(output.exists())
            self.assertFalse(output.parent.exists())


class RecoveryContracts(unittest.TestCase):
    def test_input_parameters_and_runtime_changes_invalidate_resume(self):
        source = metadata(inventory=[dict(path='/synthetic/input.tiff', size=1234, mtime_ns=1)])
        settings = dict(target='ome-tiff', workers=4, series=0, roi_size=128)
        runtime = dict(runtime_version=1, artifact_version='1.0')
        baseline = converter.stage_fingerprint(source, settings, runtime)
        self.assertEqual(baseline, converter.stage_fingerprint(source, settings, runtime))
        changes = []
        changed_source = copy.deepcopy(source)
        changed_source['inventory'][0]['size'] += 1
        changes.append((changed_source, settings, runtime))
        changed_source = copy.deepcopy(source)
        changed_source['channels'].reverse()
        changes.append((changed_source, settings, runtime))
        changes.append((source, dict(settings, roi_size=64), runtime))
        changes.append((source, settings, dict(runtime, artifact_version='2.0')))
        for meta, parameters, cfg in changes:
            with self.subTest(meta=meta, parameters=parameters, runtime=cfg):
                self.assertNotEqual(baseline, converter.stage_fingerprint(meta, parameters, cfg))

    def test_qupath_validator_changes_invalidate_resume(self):
        source = metadata(inventory=[])
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp)
            (code / 'convert_slide.py').write_text('# synthetic code identity only\n')
            validator = code / 'validate_qupath.groovy'
            validator.write_text('// validator revision one\n')
            with mock.patch.object(converter, '__file__', str(code / 'convert_slide.py')):
                baseline = converter.stage_fingerprint(source, {}, {})
                validator.write_text('// validator revision two\n')
                changed = converter.stage_fingerprint(source, {}, {})
            self.assertNotEqual(baseline, changed)


class ArrayContracts(unittest.TestCase):
    def test_rgb_zarr_region_matches_source_yxs_layout(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy required for native array contract; run in prepared runtime')
        array = np.arange(1*3*1*7*9, dtype=np.uint8).reshape(1, 3, 1, 7, 9)
        row = dict(c=0, x=2, y=1, width=4, height=3)
        actual = converter.take(array, 'TCZYX', row, rgb=True)
        expected = np.moveaxis(array[0, :, 0, 1:4, 2:6], 0, -1)
        self.assertEqual(actual.shape, (3, 4, 3))
        np.testing.assert_array_equal(actual, expected)


class PipelineStateContracts(unittest.TestCase):
    """Stub native readers to test publication/recovery, not image correctness."""

    @contextlib.contextmanager
    def pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input.tiff'
            source.write_bytes(b'synthetic source remains unchanged')
            qupath = root / 'qupath-placeholder'
            qupath.write_bytes(b'never executed')
            work = root / 'output'
            meta = metadata(C=1, effective_channels=1, channels=['Synthetic'],
                            dtype='uint8', series=0, source_xml='<OME/>',
                            inventory=converter.inventory([source]))
            cfg = dict(qupath=str(qupath), bioformats2raw_home=str(root / 'b2r'),
                       raw2ometiff_home=str(root / 'r2o'))
            args = SimpleNamespace(input=str(source), output_dir=str(work), runtime='mock.json',
                                   image_type='fluorescence', series=None, workers=1, heap_gib=1,
                                   roi_size=16, roi_json=None, min_free_gib=0, resume=False,
                                   skip_qupath=False, dry_run=False)
            controller = dict(qupath_status='PASS', pixel_error=False, zarr_error=False, calls=[])

            def command(name, command_args, command_work, env, state, reserve):
                controller['calls'].append(name)
                state['stage'] = name
                if name == 'bioformats2raw':
                    Path(command_args[2]).mkdir()
                elif name == 'raw2ometiff':
                    Path(command_args[2]).write_bytes(b'synthetic pending output')
                elif name == 'qupath':
                    Path(command_args[5]).write_text(json.dumps({'status': controller['qupath_status']}))

            def pixels(*unused):
                if controller['pixel_error']:
                    raise probe.SlideError('TIFF_PIXEL_MISMATCH', 'Synthetic failed comparison')
                return {'status': 'PASS', 'roi_count': 0, 'levels':[{'level':0}]}

            def zarr_pixels(*unused):
                if controller['zarr_error']:
                    raise probe.SlideError('ZARR_PIXEL_MISMATCH','Synthetic damaged intermediate')

            with contextlib.ExitStack() as stack:
                for name, replacement in [
                    ('runtime_config', lambda unused: cfg),
                    ('inspect', lambda *unused: copy.deepcopy(meta)),
                    ('read_rois', lambda *unused: []),
                    ('check_zarr', zarr_pixels),
                    ('check_ome', pixels),
                    ('run_command', command),
                ]:
                    stack.enter_context(mock.patch.object(converter, name, replacement))
                yield args, controller, source, work

    def test_qupath_failure_blocks_final_and_resume_reuses_valid_stages(self):
        with self.pipeline() as (args, controller, source, work):
            controller['qupath_status'] = 'FAIL'
            with self.assertRaises(probe.SlideError) as raised:
                converter.run(args)
            self.assertEqual(raised.exception.code, 'QUPATH_FAILED')
            self.assertFalse((work / 'image.ome.tiff').exists())
            self.assertTrue((work / 'image.pending.ome.tiff').is_file())
            self.assertFalse((work / '.running').exists())
            state = json.loads((work / 'status.json').read_text())
            self.assertEqual(state['status'], 'FAILED')
            self.assertTrue(state['completed']['zarr'])
            self.assertTrue(state['completed']['tiff'])
            controller['calls'].clear()
            controller['qupath_status'] = 'PASS'
            args.resume = True
            result = converter.run(args)
            self.assertEqual(controller['calls'], ['qupath'])
            self.assertEqual(result['status'], 'QUPATH_VERIFIED')
            self.assertTrue((work / 'image.ome.tiff').is_file())
            self.assertFalse((work / 'image.pending.ome.tiff').exists())
            self.assertEqual(source.read_bytes(), b'synthetic source remains unchanged')

    def test_failed_pixel_stage_is_rebuilt_and_bad_pending_is_preserved(self):
        with self.pipeline() as (args, controller, source, work):
            controller['pixel_error'] = True
            with self.assertRaises(probe.SlideError) as raised:
                converter.run(args)
            self.assertEqual(raised.exception.code, 'TIFF_PIXEL_MISMATCH')
            state = json.loads((work / 'status.json').read_text())
            self.assertFalse(state['completed'].get('tiff', False))
            self.assertNotIn('qupath', controller['calls'])
            self.assertFalse((work / 'image.ome.tiff').exists())
            controller['calls'].clear()
            controller['pixel_error'] = False
            args.resume = True
            result = converter.run(args)
            self.assertEqual(controller['calls'], ['raw2ometiff', 'qupath'])
            self.assertEqual(result['status'], 'QUPATH_VERIFIED')
            self.assertEqual(len(list(work.glob('image.pending.ome.tiff.incomplete-*'))), 1)

    def test_completed_output_hash_mismatch_does_not_trigger_overwrite(self):
        with self.pipeline() as (args, controller, source, work):
            converter.run(args)
            final = work / 'image.ome.tiff'
            final.write_bytes(b'external modification')
            controller['calls'].clear()
            args.resume = True
            with self.assertRaises(probe.SlideError) as raised:
                converter.run(args)
            self.assertEqual(raised.exception.code, 'FINAL_CHANGED')
            self.assertEqual(controller['calls'], [])
            self.assertEqual(final.read_bytes(), b'external modification')

    def test_damaged_zarr_invalidates_completed_stage_and_can_rebuild(self):
        with self.pipeline() as (args, controller, source, work):
            controller['qupath_status'] = 'FAIL'
            with self.assertRaises(probe.SlideError):
                converter.run(args)
            args.resume = True
            controller['zarr_error'] = True
            with self.assertRaises(probe.SlideError) as raised:
                converter.run(args)
            self.assertEqual(raised.exception.code,'ZARR_PIXEL_MISMATCH')
            state=json.loads((work/'status.json').read_text())
            self.assertFalse(state['completed']['zarr'])
            self.assertFalse(state['completed']['tiff'])
            controller.update(zarr_error=False,qupath_status='PASS',calls=[])
            converter.run(args)
            self.assertEqual(controller['calls'],['bioformats2raw','raw2ometiff','qupath'])
            self.assertEqual(len(list(work.glob('image.zarr.incomplete-*'))),1)


if __name__ == '__main__':
    unittest.main()
