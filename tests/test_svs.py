#!/usr/bin/env python3
"""Synthetic SVS contract tests; no patient data or scanner files required."""
from __future__ import annotations
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import tifffile
import zarr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from svs_writer import SVSUnsupportedError, write_svs, verify_svs


def create_case(folder: Path, name='synthetic_rgb'):
    """Create an odd-sized, colored, deterministic synthetic pyramid."""
    path = folder / (name + '.zarr')
    root = zarr.open_group(str(path), mode='w', zarr_format=2)
    group = root.create_group('0')
    yy, xx = np.indices((769, 1027), dtype=np.uint32)
    rgb = np.stack([(xx * 7 + yy * 3) % 256, (xx // 3 + yy * 11) % 256,
                    (xx ^ yy) % 256], axis=-1).astype(np.uint8)
    arrays = []
    for i, stride in enumerate((1, 2, 4, 8)):
        rgb_level = rgb[::stride, ::stride, :]
        data = np.moveaxis(rgb_level, -1, 0)[None, :, None, :, :]
        group.create_array(str(i), data=data, chunks=(1, 1, 1, 256, 256))
        arrays.append(rgb_level)
    group.attrs['multiscales'] = [{'version': '0.4', 'axes': [{'name': x} for x in 'tczyx'],
                                   'datasets': [{'path': str(i)} for i in range(len(arrays))]}]
    metadata = dict(X=1027, Y=769, C=3, Z=1, T=1, dtype='uint8', rgb=True,
                    channels=['Red', 'Green', 'Blue'], physical_size_x=0.325,
                    physical_size_y=0.325, physical_size_unit='µm')
    return path, metadata, arrays


class SVSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.path, self.meta, self.arrays = create_case(self.folder)
        self.output = self.folder / 'synthetic.pending.svs'

    def tearDown(self):
        self.temp.cleanup()

    def test_all_pixels_and_edges_every_level(self):
        report = write_svs(self.path, self.meta, self.output)
        self.assertEqual(report['levels'][0], [1027, 769])
        with tifffile.TiffFile(self.output) as tif:
            self.assertTrue(tif.is_bigtiff)
            self.assertTrue(tif.is_svs)
            self.assertEqual(len(tif.pages), 5)
            self.assertFalse(tif.pages[1].is_tiled)
            for i, expected in enumerate(self.arrays):
                page = tif.pages[0 if i == 0 else i+1]
                np.testing.assert_array_equal(page.asarray(), expected)
                self.assertEqual(int(page.photometric), 2)
                self.assertEqual(page.samplesperpixel, 3)
                self.assertTrue(page.is_tiled)
                self.assertIn(int(page.compression), (8, 32946))
            self.assertNotIn('AppMag', tif.pages[0].description)
        qc = verify_svs(self.path, self.meta, self.output)
        self.assertEqual(qc['status'], 'PASS')
        self.assertFalse(qc['qupath_verified'])
        self.assertGreaterEqual(len(qc['comparisons']), 20)

    def test_no_overwrite(self):
        self.output.write_bytes(b'preserve existing result')
        with self.assertRaises(FileExistsError):
            write_svs(self.path, self.meta, self.output)
        self.assertEqual(self.output.read_bytes(), b'preserve existing result')

    def test_independent_three_channels_not_rgb(self):
        self.meta['rgb'] = False
        with self.assertRaises(SVSUnsupportedError):
            write_svs(self.path, self.meta, self.output)
        self.assertFalse(self.output.exists())

    def test_uint16_not_downgraded(self):
        self.meta['dtype'] = 'uint16'
        with self.assertRaises(SVSUnsupportedError):
            write_svs(self.path, self.meta, self.output)

    def test_calibration_failures(self):
        for x, y, unit in [(0.25, 0.5, 'µm'), (0.25, None, 'µm'),
                            (0.25, 0.25, 'unknown'), (float('nan'), float('nan'), 'µm')]:
            with self.subTest(x=x, y=y, unit=unit):
                self.meta.update(physical_size_x=x, physical_size_y=y, physical_size_unit=unit)
                with self.assertRaises(SVSUnsupportedError):
                    write_svs(self.path, self.meta, self.output)
                self.assertFalse(self.output.exists())

    def test_unknown_calibration_not_invented(self):
        self.meta.update(physical_size_x=None, physical_size_y=None)
        write_svs(self.path, self.meta, self.output)
        verify_svs(self.path, self.meta, self.output)
        with tifffile.TiffFile(self.output) as tif:
            self.assertNotIn('MPP', tif.pages[0].description)
            self.assertEqual(int(tif.pages[0].resolutionunit), 1)

    def test_inconsistent_dimensions(self):
        self.meta['X'] += 1
        with self.assertRaises(SVSUnsupportedError):
            write_svs(self.path, self.meta, self.output)

    def test_axis_order_is_not_assumed(self):
        group = zarr.open_group(str(self.path), mode='a')['0']
        scales = list(group.attrs['multiscales'])
        scales[0]['axes'] = [{'name': x} for x in 'tzcyx']
        group.attrs['multiscales'] = scales
        with self.assertRaises(SVSUnsupportedError):
            write_svs(self.path, self.meta, self.output)

    def test_z_or_t_not_flattened(self):
        for key in ('Z', 'T'):
            metadata = dict(self.meta, **{key: 2})
            with self.assertRaises(SVSUnsupportedError):
                write_svs(self.path, metadata, self.output)

    def test_icc_preserved(self):
        from PIL import ImageCms
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        self.meta['icc_profile_base64'] = base64.b64encode(profile).decode('ascii')
        write_svs(self.path, self.meta, self.output)
        verify_svs(self.path, self.meta, self.output)
        with tifffile.TiffFile(self.output) as tif:
            self.assertEqual(tif.pages[0].tags[34675].value, profile)


def create_fixture(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    path, meta, arrays = create_case(destination)
    output = destination / 'synthetic_rgb.svs'
    write_svs(path, meta, output)
    verify_svs(path, meta, output)
    # An independent PNG permits actual QuPath/OpenSlide pixel comparisons.
    from PIL import Image
    Image.fromarray(arrays[0]).save(destination / 'synthetic_rgb.png')
    (destination / 'synthetic_rgb.metadata.json').write_text(json.dumps(meta, indent=2))
    print(str(output))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--fixture':
        create_fixture(Path(sys.argv[2]))
    else:
        unittest.main()
