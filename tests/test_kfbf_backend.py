"""Backend contract tests using an explicit synthetic reader, not vendor evidence.

Real KFBF decoding is tested separately. Synthetic uint16 tests establish writer
precision preservation only, not availability of a uint16 KFBF decoder.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np
import tifffile
import zarr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import kfbf_backend as backend


class SyntheticReader:
    def __init__(self, path):
        self.spec = json.loads(Path(path).read_text())
        self.width, self.height = self.spec.get('width', 1027), self.spec.get('height', 39)
        self.channel_count = self.spec.get('count', 4)
        self.dtype = self.spec.get('dtype', 'uint8')
        self.source_encoded_bit_depth = self.spec.get('precision', np.dtype(self.dtype).itemsize*8)
        self.channels = [dict(name=None if c == 0 else 'Marker & '+str(c), color_rgb=None if c == 0 else [c, 64, 255], exposure_raw=c+0.25)
                         for c in range(self.channel_count)]
        self.mpp = self.spec.get('mpp', 0.31)
        self.coverage_report = {'status': 'COMPLETE', 'base': True}
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): pass
    def metadata(self): return dict(test_fixture=True, source=self.spec, dimensions=[self.width, self.height])
    def read_region(self, level, c, x, y, w, h, missing_pixels='error'):
        if missing_pixels != 'error' or level != 0 or not (0 <= c < self.channel_count and x >= 0 and y >= 0 and x+w <= self.width and y+h <= self.height):
            raise ValueError('Invalid synthetic source read')
        yy, xx = np.ogrid[y:y+h, x:x+w]
        pixels = ((xx*17+yy*31+c*127+(xx//7)*(yy//3)) % (2**self.source_encoded_bit_depth)).astype(self.dtype)
        if self.spec.get('wrong_dtype'): return pixels.astype('float32')
        return pixels


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.object(backend, '_reader', SyntheticReader)
        self.patch.start()
    def tearDown(self):
        self.patch.stop(); self.temp.cleanup()
    def source(self, **params):
        path = self.root / 'source.kfbf'
        path.write_text(json.dumps(params))
        return path
    def converted(self, **params):
        source = self.source(**params)
        meta = backend.inspect_kfbf(source)
        output = self.root / 'image.zarr'
        backend.write_kfbf_zarr(source, meta, output, self.root/'write.json')
        return source, meta, output
    def write_tiff(self, output, meta):
        group = zarr.open_group(str(output), mode='r')['0']
        target = self.root/'image.ome.tiff'
        levels = meta['kfbf']['output_levels']
        with tifffile.TiffWriter(target, ome=True, bigtiff=True) as tw:
            for n in range(len(levels)):
                data = np.asarray(group[str(n)][0, :, 0, :, :])
                options = dict(photometric='minisblack', tile=(32, 32), compression='lzw')
                if n == 0: options.update(subifds=len(levels)-1, metadata={'axes':'CYX'})
                else: options.update(subfiletype=1)
                tw.write(data, **options)
        return target
    def test_original_names_colors_unknowns_and_xml_escape(self):
        meta = backend.inspect_kfbf(self.source(count=4, mpp=None))
        pixels = ET.fromstring(meta['source_xml']).find('{'+backend.NS+'}Image/{'+backend.NS+'}Pixels')
        channels = pixels.findall('{'+backend.NS+'}Channel')
        self.assertNotIn('Name', channels[0].attrib)
        self.assertNotIn('Color', channels[0].attrib)
        self.assertEqual(channels[1].get('Name'), 'Marker & 1')
        self.assertEqual(channels[1].get('Color'), str(backend._color([1,64,255])))
        self.assertNotIn('PhysicalSizeX', pixels.attrib)
        self.assertNotIn('ExposureTime', meta['source_xml'])
        self.assertEqual(meta['kfbf']['channels'][1]['exposure_raw'],1.25)
    def test_dynamic_channels_types_complete_pixel_equality(self):
        for count, dtype in [(1, 'uint8'), (6, 'uint8'), (3, 'uint16'), (8, 'uint16')]:
            with self.subTest(count=count, dtype=dtype), tempfile.TemporaryDirectory(dir=self.root) as temp:
                old = self.root; self.root = Path(temp)
                try:
                    source, meta, output = self.converted(count=count, dtype=dtype)
                    report = backend.verify_kfbf_zarr(source, meta, output)
                    self.assertEqual(report['status'],'PASS')
                    expected = sum(l['width']*l['height']*count for l in meta['kfbf']['output_levels'])
                    self.assertEqual(report['total_values_compared'],expected)
                    tiff = self.write_tiff(output, meta)
                    result = backend.verify_kfbf_ome(output, meta, tiff)
                    self.assertEqual(result['total_values_compared'],expected)
                    self.assertEqual(result['levels'],report['levels'])
                finally: self.root = old
    def test_source_precision_mismatch_rejected(self):
        with self.assertRaisesRegex(backend.KFBFBackendError,'precision'):
            backend.inspect_kfbf(self.source(dtype='uint8', precision=16))
    def test_unsupported_decoded_type_rejected(self):
        with self.assertRaisesRegex(backend.KFBFBackendError,'Unsupported'):
            backend.inspect_kfbf(self.source(dtype='float32'))
    def test_bad_decoded_region_type_rejected(self):
        source = self.source(wrong_dtype=True)
        meta = backend.inspect_kfbf(source)
        with self.assertRaisesRegex(backend.KFBFBackendError,'unexpected shape or dtype'):
            backend.write_kfbf_zarr(source,meta,self.root/'bad.zarr',self.root/'report.json')
        self.assertFalse((self.root/'report.json').exists())
    def test_changed_metadata_rejected_before_write(self):
        source = self.source()
        meta = backend.inspect_kfbf(source)
        meta['channels'][1] = 'Incorrect name'
        with self.assertRaisesRegex(backend.KFBFBackendError,'changed'):
            backend.write_kfbf_zarr(source,meta,self.root/'bad.zarr',self.root/'report.json')
        self.assertFalse((self.root/'bad.zarr').exists())
    def test_source_metadata_without_xml_can_write_same_xml(self):
        source = self.source()
        meta = backend.inspect_kfbf(source)
        xml = meta.pop('source_xml')
        backend.write_kfbf_zarr(source,meta,self.root/'image.zarr',self.root/'report.json')
        self.assertEqual((self.root/'image.zarr/OME/METADATA.ome.xml').read_text(),xml)
    def test_pixel_corruption_detected_even_outside_default_rois(self):
        source, meta, output = self.converted()
        ar = zarr.open_group(str(output),mode='r+')['0']['0']
        ar[0,2,0,19,777] = int(ar[0,2,0,19,777]) ^ 1
        with self.assertRaisesRegex(backend.KFBFBackendError,'differs'):
            backend.verify_kfbf_zarr(source,meta,output)
    def test_generated_level_corruption_detected(self):
        source, meta, output = self.converted()
        ar = zarr.open_group(str(output),mode='r+')['0']['1']
        ar[0,1,0,8,244] = int(ar[0,1,0,8,244]) ^ 1
        with self.assertRaisesRegex(backend.KFBFBackendError,'differs'):
            backend.verify_kfbf_zarr(source,meta,output)
    def test_overwrite_rejected(self):
        source, meta, output = self.converted()
        with self.assertRaisesRegex(backend.KFBFBackendError,'exists'):
            backend.write_kfbf_zarr(source,meta,output,self.root/'report2.json')
    def test_bf_adapter_preserves_order_uint16(self):
        source = self.source(dtype='uint16')
        reader = backend.open_reader(source)
        try:
            reader.setSeries(0); reader.setResolution(0)
            raw = reader.openBytes(reader.getIndex(0,3,0),17,4,19,11)
            observed = np.frombuffer(raw,dtype='<u2').reshape(11,19)
            expected = SyntheticReader(source).read_region(0,3,17,4,19,11)
            self.assertTrue(np.array_equal(observed,expected))
        finally: reader.close()
    def test_real_binary_missing_policy_and_coverage_units(self):
        from test_kfbf_reader import make_kfbf
        from kfbf_reader import KFBFReader
        source = self.root/'binary.kfbf'
        fixture = make_kfbf(source, channels=3, width=35, height=27,
                           missing={(0,1,16,0)}, missing_spatial={(0,32,16)})
        with patch.object(backend, '_reader', KFBFReader):
            meta = backend.inspect_kfbf(source)
            with self.assertRaisesRegex(backend.KFBFBackendError, 'incomplete'):
                backend.write_kfbf_zarr(source,meta,self.root/'refused.zarr',self.root/'refused.json')
            self.assertFalse((self.root/'refused.zarr').exists())
            meta = backend.inspect_kfbf(source, {'kfbf_missing_pixels':'black'})
            coverage = meta['kfbf']['coverage_summary']
            self.assertEqual(coverage['base_missing_channel_samples'],256+3*3*11)
            self.assertEqual(coverage['base_missing_spatial_union_pixels'],256+3*11)
            output = self.root/'black.zarr'
            backend.write_kfbf_zarr(source,meta,output,self.root/'black.json')
            report = backend.verify_kfbf_zarr(source,meta,output)
            self.assertFalse(report['unknown_content_recovered'])
            ar = zarr.open_group(str(output),mode='r')['0']['0']
            for c in range(3):
                np.testing.assert_array_equal(ar[0,c,0],fixture['planes'][(0,c)])

    def test_bad_source_series_and_color(self):
        source = self.source()
        with self.assertRaisesRegex(backend.KFBFBackendError,'series'):
            backend.inspect_kfbf(source,series=1)
        with self.assertRaisesRegex(backend.KFBFBackendError,'color'):
            backend._color([255,0,999])


if __name__ == '__main__': unittest.main()
