#!/usr/bin/env python3
"""Regression tests for source-image, ROI-memory and dry-run safeguards.

All image-reader interactions are mocked; no JVM, server or real WSI is used.
"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import slide_probe as probe
import convert_slide as converter


def metadata(**overrides):
    result = dict(X=64, Y=64, C=3, Z=1, T=1, dtype='uint8', rgb=True,
                  rgb_samples=3, effective_channels=1, name='primary tissue',
                  channels=['Red', 'Green', 'Blue'], series=0,
                  little_endian=True, inventory=[], source_xml='<OME/>')
    result.update(overrides)
    return result


def mocked_reader(spatial_variation=False):
    reader = mock.MagicMock()
    reader.isInterleaved.return_value = True
    reader.getIndex.return_value = 0
    def read(plane, x, y, width, height):
        pixels = np.full((height, width, 3), [250, 249, 248], dtype=np.uint8)
        if spatial_variation:
            pixels[0, 0, 0] = 10
        return pixels.tobytes()
    reader.openBytes.side_effect = read
    return reader


class ProbeLimits(unittest.TestCase):
    def test_uniform_tinted_rgb_does_not_establish_tissue(self):
        reader = mocked_reader()
        with mock.patch.object(probe, 'open_reader', return_value=(reader, None, None)):
            with self.assertRaises(probe.SlideError) as raised:
                probe.read_rois(Path('/synthetic/input.tiff'), {}, metadata(), roi_size=8)
        self.assertEqual(raised.exception.code, 'NEEDS_TISSUE_ROI')
        self.assertEqual(reader.openBytes.call_count, 12)
        reader.close.assert_called_once()

    def test_spatial_rgb_variation_is_accepted(self):
        reader = mocked_reader(spatial_variation=True)
        with mock.patch.object(probe, 'open_reader', return_value=(reader, None, None)):
            rows = probe.read_rois(Path('/synthetic/input.tiff'), {}, metadata(), roi_size=8)
        self.assertEqual(len(rows), 12)
        np.testing.assert_array_equal(rows[0][1][1, 1], [250, 249, 248])
        self.assertEqual(int(rows[0][1][0, 0, 0]), 10)
        reader.close.assert_called_once()

    def test_default_oversize_roi_never_reads_pixels(self):
        reader = mocked_reader()
        with mock.patch.object(probe, 'open_reader', return_value=(reader, None, None)):
            with self.assertRaises(probe.SlideError) as raised:
                probe.read_rois(Path('/synthetic/input.tiff'), {}, metadata(X=10000, Y=10000), roi_size=4096)
        self.assertEqual(raised.exception.code, 'ROI_TOO_LARGE')
        reader.openBytes.assert_not_called()
        reader.close.assert_called_once()

    def test_custom_oversize_roi_never_reads_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            roi = Path(tmp) / 'regions.json'
            roi.write_text(json.dumps([dict(x=0, y=0, width=4096, height=64)]))
            reader = mocked_reader()
            with mock.patch.object(probe, 'open_reader', return_value=(reader, None, None)):
                with self.assertRaises(probe.SlideError) as raised:
                    probe.read_rois(Path('/synthetic/input.tiff'), {}, metadata(X=10000), roi_file=roi)
            self.assertEqual(raised.exception.code, 'ROI_TOO_LARGE')
            reader.openBytes.assert_not_called()
            reader.close.assert_called_once()

    def test_combined_roi_budget_never_reads_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            roi = Path(tmp) / 'regions.json'
            roi.write_text(json.dumps([dict(x=0, y=0, width=2048, height=2048)]))
            reader = mocked_reader()
            # 100 independent uint16 channels exceed 512 MiB without allocating
            # that image. The reader must not be asked for even the first plane.
            large = metadata(X=10000, Y=10000, C=100, rgb=False, dtype='uint16', effective_channels=100)
            with mock.patch.object(probe, 'open_reader', return_value=(reader, None, None)):
                with self.assertRaises(probe.SlideError) as raised:
                    probe.read_rois(Path('/synthetic/input.tiff'), {}, large, roi_file=roi)
            self.assertEqual(raised.exception.code, 'ROI_BUDGET')
            reader.openBytes.assert_not_called()
            reader.close.assert_called_once()

    def test_known_associated_series_cannot_be_routed_as_slide(self):
        for name in ['label', 'Label image', 'thumbnail', 'Thumbnail image',
                     'macro', 'Macro image', 'overview', ' Overview image ']:
            with self.subTest(name=name):
                with self.assertRaises(probe.SlideError) as raised:
                    probe.route(metadata(name=name), image_type='brightfield')
                self.assertEqual(raised.exception.code, 'AUXILIARY_IMAGE')

    def test_symlink_mrxs_companion_cannot_contain_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'slide.mrxs'
            source.write_bytes(b'entry placeholder')
            actual = base / 'stored-original-data'
            actual.mkdir()
            companion = source.with_suffix('')
            companion.symlink_to(actual, target_is_directory=True)
            for output in [companion / 'converted', actual / 'converted']:
                with self.subTest(output=str(output)):
                    with self.assertRaises(probe.SlideError) as raised:
                        converter.validate_output(source, output)
                    self.assertEqual(raised.exception.code, 'UNSAFE_OUTPUT')
            self.assertEqual(list(actual.iterdir()), [])


class DryRunBudget(unittest.TestCase):
    def run_dry(self, folder, free_bytes):
        source = folder / 'synthetic.tiff'
        source.write_bytes(b'synthetic entry')
        output = folder / 'result'
        args = SimpleNamespace(input=str(source), output_dir=str(output), runtime='mock-runtime.json',
                               workers=2, heap_gib=4, roi_size=128, min_free_gib=1,
                               resume=False, series=None, image_type='fluorescence',
                               skip_qupath=True, roi_json=None, dry_run=True)
        meta = metadata(X=1000, Y=500, C=2, effective_channels=2,
                        rgb=False, rgb_samples=1, dtype='uint16', channels=['A', 'B'])
        with mock.patch.object(converter, 'runtime_config', return_value={}), \
             mock.patch.object(converter, 'inspect', return_value=meta), \
             mock.patch.object(converter, 'read_rois', return_value=[('row', 'pixels')]), \
             mock.patch.object(converter.shutil, 'disk_usage', return_value=SimpleNamespace(free=free_bytes)), \
             mock.patch.object(converter, 'run_command') as backend:
            try:
                return converter.run(args)
            finally:
                backend.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(source.read_bytes(), b'synthetic entry')

    def test_dry_run_rejects_insufficient_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(probe.SlideError) as raised:
                self.run_dry(Path(tmp), free_bytes=1024)
            self.assertEqual(raised.exception.code, 'DISK_BUDGET')

    def test_dry_run_reports_space_budget_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_dry(Path(tmp), free_bytes=10*1024**3)
        self.assertEqual(result['status'], 'READY_TO_CONVERT')
        self.assertEqual(result['disk']['decoded_bytes'], 2_000_000)
        self.assertEqual(result['disk']['required_free_bytes'], 1024**3 + 8_000_000)
        self.assertEqual(result['disk']['available_bytes'], 10*1024**3)
        self.assertEqual(result['disk']['filesystem_write_check'], 'DEFERRED_UNTIL_OUTPUT_CREATION')


if __name__ == '__main__':
    unittest.main()
