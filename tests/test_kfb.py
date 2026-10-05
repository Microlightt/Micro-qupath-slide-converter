#!/usr/bin/env python3
"""Real synthetic KFB binary/JPEG fixtures; no SDK, network or patient data."""
from __future__ import annotations
import base64
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageCms, ImageFile
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from kfb_reader import StrictKFB, KFBReadError, MAX_JPEG_BYTES
from kfb_backend import inspect_kfb, validate_kfb, open_reader, write_kfb_svs, verify_kfb_svs, read_region


def make_kfb(path, *, dimensions=(33, 25), tile_size=16, downsamples=(1, 2, 4),
             missing=False, partial=False, overhang=False, duplicate=False, offgrid=False,
             outside=False, icc=None, inconsistent_icc=False, corrupt=False,
             version=1.6, magic=b'KFB\0', grayscale=False):
    """Write actual 96-byte header, signed-offset 64-byte records and JPEGs.

    JPEGs deliberately precede the index and are separated by unrelated bytes;
    cumulative-offset readers therefore fail. Expected pixels are the decoded
    native JPEGs, not the pre-compression drawing used to generate them.
    """
    width, height = dimensions
    blobs, records = [], []
    position = 96
    for level, downsample in enumerate(downsamples):
        w, h = max(1, width//downsample), max(1, height//downsample)
        for y in range(0, h, tile_size):
            for x in range(0, w, tile_size):
                if missing and level == 0 and x == 0 and y == 0:
                    continue
                tw, th = (tile_size, tile_size) if overhang else (min(tile_size, w-x), min(tile_size, h-y))
                if partial and level == 0 and x == 0 and y == 0:
                    tw = max(1, tw-3)
                yy, xx = np.indices((th, tw), dtype=np.uint16)
                pixels = np.stack([(xx*13+x*3+level*7)%256, (yy*19+y*5+level*11)%256,
                                   (xx*7+yy*5+x+y)%256], axis=-1).astype(np.uint8)
                im = Image.fromarray(pixels[..., 0] if grayscale else pixels)
                payload = io.BytesIO()
                kwargs = dict(quality=95, subsampling=0)
                if icc is not None and not (inconsistent_icc and records):
                    kwargs['icc_profile'] = icc
                im.save(payload, format='JPEG', **kwargs)
                jpeg = payload.getvalue()
                if corrupt and not records:
                    jpeg = jpeg[:-2] + b'XX'
                blobs.append((position, jpeg))
                rx = x+1 if offgrid and not records else x
                ry = h+tile_size if outside and not records else y
                records.append([rx, ry, tw, th, 40/downsample, len(jpeg), position])
                position += len(jpeg)+7
    if duplicate:
        records.append(list(records[0]))
    index_start = position+11
    header = bytearray(96)
    header[:4] = b'\xf1\x01\xee\xee'; header[92:] = b'\xff\x01\xee\xee'
    header[4:8] = magic
    struct.pack_into('<f', header, 12, version)
    struct.pack_into('<IIII', header, 16, len(records), height, width, 40)
    header[32:36] = b'JPG\0'
    struct.pack_into('<Q', header, 68, index_start)
    struct.pack_into('<f', header, 76, 0.25)
    struct.pack_into('<I', header, 88, tile_size)
    file = bytearray(index_start+64*len(records))
    file[:96] = header
    for offset, data in blobs:
        file[offset:offset+len(data)] = data
    for i, (x,y,tw,th,scale,size,offset) in enumerate(records):
        record = bytearray(64)
        record[:4] = b'\xf1\x04\xee\xee'; record[60:] = b'\xff\x04\xee\xee'
        struct.pack_into('<IIII', record, 4, x,y,tw,th)
        struct.pack_into('<f', record, 20, scale)
        struct.pack_into('<I', record, 32, size)
        struct.pack_into('<q', record, 36, offset-index_start)
        file[index_start+i*64:index_start+(i+1)*64] = record
    Path(path).write_bytes(file)
    return Path(path)


class KFBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.path = self.folder/'synthetic.kfb'
        self.output = self.folder/'synthetic.pending.svs'
    def tearDown(self):
        self.tmp.cleanup()
    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(KFBReadError) as error:
            function(*args, **kwargs)
        self.assertEqual(error.exception.code, code)
    def test_direct_signed_offsets_and_every_retained_pixel(self):
        make_kfb(self.path)
        meta = inspect_kfb(self.path)
        self.assertEqual((meta['X'],meta['Y'],meta['C']), (33,25,3))
        self.assertEqual(meta['source_xml_provenance'], 'synthesized_from_kfb_header')
        self.assertEqual(meta['channels'], [None])
        with StrictKFB(self.path) as source:
            self.assertTrue(all(e.relative_offset < 0 for e in source.entries))
            self.assertGreater(source.offset_audit()['direct_vs_cumulative_difference_count'], 0)
        write_kfb_svs(self.path, meta, self.output, self.folder/'write.json', workers=1)
        qc = verify_kfb_svs(self.path, meta, self.output)
        self.assertEqual(qc['status'], 'PASS')
        self.assertEqual(len(qc['levels']), 3)
        self.assertEqual(qc['mismatched_rgb_values_all_retained_levels'], 0)
        with StrictKFB(self.path) as source, tifffile.TiffFile(self.output) as tiff:
            self.assertFalse(tiff.pages[1].is_tiled)
            for n, level in enumerate(source.levels):
                wanted = read_region(source, level, 0,0,level.width,level.height)
                actual = tiff.pages[0 if n == 0 else n+1].asarray()
                np.testing.assert_array_equal(actual, wanted)
    def test_sparse_requires_explicit_black_and_exact_missing_rectangles(self):
        make_kfb(self.path, missing=True, partial=True)
        meta = inspect_kfb(self.path)
        self.assert_code('KFB_MISSING_PIXELS', validate_kfb, meta)
        self.assert_code('KFB_MISSING_PIXELS', write_kfb_svs, self.path, meta, self.output)
        self.assertFalse(self.output.exists())
        cov = meta['kfb']['coverage'][0]
        self.assertEqual(cov['missing_pixels'], 16*16)
        self.assertEqual(cov['missing_rectangles'], [dict(x=0,y=0,width=16,height=16)])
        write_kfb_svs(self.path, meta, self.output, missing_policy='black', workers=1)
        qc = verify_kfb_svs(self.path, meta, self.output, 'black')
        self.assertEqual(qc['missing_fill_rgb'], [0,0,0])
        self.assertEqual(qc['missing_pixels_total'], 256)
        with tifffile.TiffFile(self.output) as tiff:
            np.testing.assert_array_equal(tiff.pages[0].asarray()[:16,:16], 0)
    def test_partial_native_tile_missing_area_is_accounted(self):
        make_kfb(self.path, partial=True)
        meta = inspect_kfb(self.path)
        cov = meta['kfb']['coverage'][0]
        self.assertEqual(cov['missing_pixels'], 3*16)
        self.assertEqual(cov['missing_rectangles'], [dict(x=13,y=0,width=3,height=16)])
        self.assert_code('KFB_MISSING_PIXELS', validate_kfb, meta)
    def test_overhang_is_reported_without_cropping_logical_pixels(self):
        make_kfb(self.path, overhang=True)
        meta = inspect_kfb(self.path)
        self.assertGreater(meta['kfb']['coverage'][0]['edge_overhang_entry_count'], 0)
        self.assertEqual(meta['kfb']['missing_pixels_total'], 0)
        write_kfb_svs(self.path, meta, self.output, workers=1)
        self.assertEqual(verify_kfb_svs(self.path, meta, self.output)['status'], 'PASS')
    def test_duplicate_native_dimensions_omitted_and_recorded(self):
        make_kfb(self.path, dimensions=(32,32), downsamples=(1,2,4,8,16,32,64))
        meta = inspect_kfb(self.path)
        self.assertEqual(meta['kfb']['native_level_count'], 7)
        self.assertEqual(meta['resolution_count'], 6)
        self.assertEqual(meta['kfb']['omitted_redundant_levels'][0]['native_level'], 6)
        write_kfb_svs(self.path, meta, self.output, workers=1)
        result = verify_kfb_svs(self.path,meta,self.output)
        self.assertEqual(len(result['levels']),6)
        self.assertEqual(result['native_jpeg_tiles_checked'], meta['kfb']['header']['tile_count'])
    def test_kfbf_and_other_versions_fail(self):
        make_kfb(self.path, magic=b'KFBF')
        self.assert_code('KFBF_WRONG_READER', StrictKFB, self.path)
        make_kfb(self.path, version=1.7)
        self.assert_code('KFB_VERSION_UNSUPPORTED', StrictKFB, self.path)
    def test_index_count_cannot_exceed_file_bounds(self):
        make_kfb(self.path)
        content = bytearray(self.path.read_bytes())
        struct.pack_into('<I',content,16,999999)
        self.path.write_bytes(content)
        with self.assertRaises(KFBReadError):
            StrictKFB(self.path)
    def test_oversized_associated_jpeg_is_rejected_before_payload_read(self):
        make_kfb(self.path)
        # A sparse file makes the declared payload byte range valid without
        # allocating the alleged JPEG. This specifically tests the absolute
        # allocation limit, rather than merely catching an out-of-file offset.
        section_offset = self.path.stat().st_size
        section = bytearray(52)
        section[:4] = b'\xf1\x02\xee\xee'
        section[48:] = b'\xff\x02\xee\xee'
        struct.pack_into('<II', section, 8, 1, 1)
        struct.pack_into('<I', section, 20, MAX_JPEG_BYTES+1)
        struct.pack_into('<I', section, 24, 52)
        with self.path.open('r+b') as handle:
            handle.seek(52)  # native header macro-section pointer
            handle.write(struct.pack('<I', section_offset))
            handle.seek(section_offset)
            handle.write(section)
            handle.truncate(section_offset+52+MAX_JPEG_BYTES+1)
        # No associated or tile JPEG payload may be requested before this
        # impossible associated-image declaration is refused.
        with mock.patch.object(StrictKFB, 'jpeg_bytes', side_effect=AssertionError('payload read must not occur')) as payload:
            with self.assertRaisesRegex(KFBReadError, 'bounded parser allocation'):
                StrictKFB(self.path)
            payload.assert_not_called()
    def test_duplicate_origins_are_rejected(self):
        make_kfb(self.path, duplicate=True)
        with self.assertRaises(KFBReadError):
            StrictKFB(self.path)
    def test_offgrid_and_fully_outside_geometry_rejected(self):
        for kwargs in [dict(offgrid=True),dict(outside=True)]:
            with self.subTest(kwargs=kwargs):
                make_kfb(self.path, **kwargs)
                meta=inspect_kfb(self.path)
                self.assert_code('KFB_GEOMETRY', validate_kfb, meta, 'black')
    def test_corrupt_jpeg_not_zero_filled(self):
        make_kfb(self.path,corrupt=True)
        meta=inspect_kfb(self.path)
        with self.assertRaises(KFBReadError):
            write_kfb_svs(self.path,meta,self.output,missing_policy='black',workers=1)
    def test_truncated_image_recovery_is_never_enabled(self):
        make_kfb(self.path)
        old=ImageFile.LOAD_TRUNCATED_IMAGES
        try:
            ImageFile.LOAD_TRUNCATED_IMAGES=True
            with StrictKFB(self.path) as source:
                with self.assertRaises(KFBReadError):source.decode_tile(source.entries[0])
        finally:ImageFile.LOAD_TRUNCATED_IMAGES=old
    def test_icc_preservation_and_mixed_profiles_rejected(self):
        icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        make_kfb(self.path,icc=icc)
        meta=inspect_kfb(self.path)
        self.assertEqual(base64.b64decode(meta['icc_profile_base64']),icc)
        write_kfb_svs(self.path,meta,self.output,workers=1)
        self.assertEqual(verify_kfb_svs(self.path,meta,self.output)['status'],'PASS')
        with tifffile.TiffFile(self.output) as tiff:self.assertEqual(tiff.pages[0].tags[34675].value,icc)
        self.output.unlink()
        make_kfb(self.path,icc=icc,inconsistent_icc=True)
        meta=inspect_kfb(self.path)
        with self.assertRaises(KFBReadError):write_kfb_svs(self.path,meta,self.output,workers=1)
    def test_existing_output_is_not_overwritten(self):
        make_kfb(self.path)
        meta=inspect_kfb(self.path)
        self.output.write_bytes(b'keep existing result')
        with self.assertRaises(FileExistsError):write_kfb_svs(self.path,meta,self.output,workers=1)
        self.assertEqual(self.output.read_bytes(),b'keep existing result')
    def test_bf_style_adapter_reads_native_rgb_and_closes(self):
        make_kfb(self.path)
        reader=open_reader(self.path,{})
        try:
            reader.setSeries(0);reader.setResolution(0)
            self.assertTrue(reader.isInterleaved())
            actual=np.frombuffer(reader.openBytes(reader.getIndex(0,0,0),7,6,11,12),dtype=np.uint8).reshape(12,11,3)
            with StrictKFB(self.path) as source:
                np.testing.assert_array_equal(actual,read_region(source,0,7,6,11,12))
            with self.assertRaises(KFBReadError):reader.setSeries(1)
        finally:reader.close()
    def test_non_rgb_jpeg_is_not_implicitly_recolored(self):
        make_kfb(self.path,grayscale=True)
        with self.assertRaises(KFBReadError):inspect_kfb(self.path)


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--fixture':
        p=Path(sys.argv[2]);p.parent.mkdir(parents=True,exist_ok=True)
        make_kfb(p,dimensions=(513,385),tile_size=128,downsamples=(1,2,4,8,16,32,64,128,256,512,1024),missing=True,overhang=True)
        print(p)
    else:
        unittest.main()
