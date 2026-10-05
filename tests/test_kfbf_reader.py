"""Binary format fixtures exercise strict KFBF decoding, never stand in for real WSI evidence."""
import hashlib
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from kfbf_reader import KFBFError, KFBFReader, jpeg_header


def make_kfbf(path, channels=3, width=35, height=27, tile_size=16, levels=(0, 1), missing=None,
              missing_spatial=None, rgb_payload=False, shuffle=True):
    """Synthetic verified-layout binary and exact JPEG-decoded expected planes."""
    missing = set(missing or ())
    missing_spatial = set(missing_spatial or ())
    # Real KFBF2.1 stores these metadata tags as LE tag,length,value.
    tag_specs = [(0x4B,4),(0x4C,8),(0x4D,8),(0x4E,8),(0x4F,8),(0x54,8)]
    header_end = 100 + sum(8+n for _, n in tag_specs)
    data = bytearray(header_end)
    data[:8] = b'\xf1\x01\xee\xeeKFBF'
    struct.pack_into('<f',data,12,2.1)
    struct.pack_into('<3i',data,20,height,width,40)
    data[32:40] = b'JPEG\0\0\0\0'
    struct.pack_into('<f',data,76,0.25)
    struct.pack_into('<i',data,88,tile_size)
    data[92:96] = b'\xff\x01\xee\xee'
    struct.pack_into('<I',data,96,len(tag_specs))
    blocks = {
        0x4C: b''.join(str(c+1).encode().ljust(20,b'\0') for c in range(channels)),
        0x4D: b''.join(('Marker '+str(c+1)).encode().ljust(40,b'\0') for c in range(channels)),
        0x4E: struct.pack('<'+'I'*channels,*range(1,channels+1)),
        0x4F: b''.join(struct.pack('<3I',*(255 if k==c%3 else 0 for k in range(3))) for c in range(channels)),
        0x54: struct.pack('<'+'d'*channels,*(float(c+1) for c in range(channels))),
    }
    pointers = {}
    for tag, block in blocks.items():
        pointers[tag] = len(data)
        data.extend(block)
    pos = 100
    tag_positions = {}
    for tag, length in tag_specs:
        struct.pack_into('<II',data,pos,tag,length)
        struct.pack_into('<I' if length==4 else '<Q',data,pos+8,channels if tag==0x4B else pointers[tag])
        tag_positions[tag] = pos
        pos += 8+length
    planes, records = {}, []
    for lev in levels:
        w,h=max(1,width//(2**lev)),max(1,height//(2**lev))
        for c in range(channels):planes[(lev,c)]=np.zeros((h,w),dtype=np.uint8)
        positions=[(x,y) for y in range(0,h,tile_size) for x in range(0,w,tile_size)]
        if shuffle:positions=list(reversed(positions))
        for x,y in positions:
            if (lev,x,y) in missing_spatial:continue
            tw,th=min(tile_size,w-x),min(tile_size,h-y)
            offsets,lengths=[],[]
            for c in range(channels):
                if (lev,c,x,y) in missing:
                    offsets.append(0);lengths.append(0);continue
                yy,xx=np.indices((th,tw))
                pixels=((xx+x)*3+(yy+y)*7+c*41+lev*13).astype(np.uint8)
                image=Image.fromarray(pixels)
                if rgb_payload and lev==levels[0] and c==0:image=image.convert('RGB')
                buf=io.BytesIO();image.save(buf,format='JPEG',quality=97)
                payload=buf.getvalue();offsets.append(len(data));lengths.append(len(payload));data.extend(payload)
                with Image.open(io.BytesIO(payload)) as decoded:
                    if decoded.mode=='L':planes[(lev,c)][y:y+th,x:x+tw]=np.asarray(decoded)
            records.append(dict(level=lev,x=x,y=y,width=tw,height=th,offsets=offsets,lengths=lengths))
    for r in records:
        r['ot']=len(data);data.extend(struct.pack('<'+'Q'*channels,*r['offsets']))
    for r in records:
        r['lt']=len(data);data.extend(struct.pack('<'+'Q'*channels,*r['lengths']))
    index_offset=len(data)
    for r in records:
        rec=bytearray(64);rec[:4]=b'\xf1\x04\xee\xee';rec[60:64]=b'\xff\x04\xee\xee'
        struct.pack_into('<4if',rec,4,r['x'],r['y'],r['width'],r['height'],40/(2**r['level']))
        struct.pack_into('<iQQ',rec,32,r['lengths'][0],r['ot'],r['lt']);data.extend(rec)
    struct.pack_into('<i',data,16,len(records));struct.pack_into('<Q',data,68,index_offset)
    Path(path).write_bytes(data)
    return dict(planes=planes,records=records,index_offset=index_offset,tag_positions=tag_positions,
                metadata_pointers=pointers,header_end=header_end)


class StrictKFBFTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'source.kfbf'

    def tearDown(self):self.tmp.cleanup()

    def error(self,code):
        with self.assertRaises(KFBFError) as exc:KFBFReader(self.path)
        self.assertEqual(exc.exception.code,code)

    def mutate(self,offset,payload):
        data=bytearray(self.path.read_bytes());data[offset:offset+len(payload)]=payload;self.path.write_bytes(data)

    def test_metadata_explicit_geometry_and_unknown_exposure_unit(self):
        make_kfbf(self.path)
        with KFBFReader(self.path) as reader:
            self.assertEqual((reader.width,reader.height,reader.channel_count),(35,27,3))
            self.assertEqual([c['name'] for c in reader.channels],['Marker 1','Marker 2','Marker 3'])
            self.assertEqual(reader.channels[2]['color_rgb'],[0,0,255])
            self.assertIsNone(reader.channels[0]['exposure_unit'])
            self.assertEqual(reader.coverage_report['base_missing_pixels'],0)

    def test_every_decoded_pixel_and_arbitrary_index_order(self):
        f=make_kfbf(self.path)
        with KFBFReader(self.path) as reader:
            before=reader.metadata()
            for lev in reader.levels:
                for c in range(3):
                    a=reader.read_region(lev['level'],c,0,0,lev['width'],lev['height'])
                    np.testing.assert_array_equal(a,f['planes'][(lev['level'],c)])
            self.assertEqual(reader.metadata(),before)

    def test_channel_counts_not_fixed_rgb(self):
        for count in (1,4,6,8):
            make_kfbf(self.path,channels=count)
            with KFBFReader(self.path) as reader:self.assertEqual(reader.channel_count,count)

    def test_one_channel_missing_is_not_filled_by_default(self):
        f=make_kfbf(self.path,missing={(0,1,16,0)})
        with KFBFReader(self.path) as r:
            self.assertEqual(r.coverage_report['base_missing_pixels'],256)
            with self.assertRaises(KFBFError) as exc:r.read_region(0,1,0,0,35,27)
            self.assertEqual(exc.exception.code,'KFBF_MISSING_PIXELS')
            # Complete other source channels remain exact.
            np.testing.assert_array_equal(r.read_region(0,0,0,0,35,27),f['planes'][(0,0)])
            np.testing.assert_array_equal(r.read_region(0,1,0,0,35,27,missing_pixels='black'),f['planes'][(0,1)])
            report=next(v for v in r.coverage_report['levels'] if v['level']==0 and v['channel']==1)
            self.assertEqual(report['missing_rectangles'],[dict(x=16,y=0,width=16,height=16)])

    def test_absent_spatial_record_audited_for_all_channels(self):
        make_kfbf(self.path,missing_spatial={(0,32,16)})
        with KFBFReader(self.path) as r:self.assertEqual(r.coverage_report['base_missing_pixels'],3*3*11)

    def test_encoded_rgb_cannot_be_implicitly_converted_to_luma(self):
        make_kfbf(self.path,rgb_payload=True);self.error('UNSUPPORTED_KFBF_ENCODING')

    def test_unknown_version_rejected(self):
        make_kfbf(self.path);self.mutate(12,struct.pack('<f',3.0));self.error('UNSUPPORTED_KFBF_VERSION')

    def test_unknown_codec_rejected(self):
        make_kfbf(self.path);self.mutate(32,b'JPEG2000');self.error('UNSUPPORTED_KFBF_CODEC')

    def test_channel_count_absent_not_defaulted_to_one(self):
        f=make_kfbf(self.path);self.mutate(f['tag_positions'][0x4B],struct.pack('<I',0x60));self.error('KFBF_UNKNOWN_CHANNEL_COUNT')

    def test_wrong_channel_count_cannot_silently_drop_plane(self):
        f=make_kfbf(self.path,channels=4)
        self.mutate(f['tag_positions'][0x4B]+8,struct.pack('<I',3))
        self.error('KFBF_CHANNEL_TABLE_LAYOUT')

    def test_unknown_names_remain_null_not_invented(self):
        f=make_kfbf(self.path);self.mutate(f['metadata_pointers'][0x4D],bytes(40))
        with KFBFReader(self.path) as r:self.assertIsNone(r.channels[0]['name'])

    def test_bad_table_offset_fails_before_decode(self):
        f=make_kfbf(self.path);self.mutate(f['index_offset']+36,struct.pack('<Q',self.path.stat().st_size+100));self.error('KFBF_BAD_OFFSET')

    def test_offgrid_coordinates_rejected(self):
        f=make_kfbf(self.path);self.mutate(f['index_offset']+4,struct.pack('<i',1));self.error('KFBF_BAD_GEOMETRY')

    def test_duplicate_grid_position_rejected(self):
        f=make_kfbf(self.path,width=32,height=16,levels=(0,));first=f['records'][0]
        self.mutate(f['index_offset']+64+4,struct.pack('<2i',first['x'],first['y']));self.error('KFBF_OVERLAP')

    def test_jpeg_shape_not_resized(self):
        f=make_kfbf(self.path);self.mutate(f['index_offset']+12,struct.pack('<i',2));self.error('KFBF_JPEG_GEOMETRY')

    def test_truncated_jpeg_is_not_healed(self):
        f=make_kfbf(self.path);r=f['records'][0];self.mutate(r['offsets'][0]+r['lengths'][0]-2,b'\0\0');self.error('KFBF_TRUNCATED_JPEG')

    def test_encoded_precision_is_checked_before_decoder(self):
        f=make_kfbf(self.path);r=f['records'][0];data=self.path.read_bytes();start=r['offsets'][0]
        sof=data.find(b'\xff\xc0',start,start+r['lengths'][0]);self.assertGreater(sof,0)
        self.mutate(sof+4,b'\x0c');self.error('UNSUPPORTED_KFBF_ENCODING')

    def test_truncated_mode_cannot_be_globally_enabled(self):
        make_kfbf(self.path)
        with KFBFReader(self.path) as r:
            old=ImageFile.LOAD_TRUNCATED_IMAGES
            try:
                ImageFile.LOAD_TRUNCATED_IMAGES=True
                with self.assertRaises(KFBFError) as exc:r.decode_tile(r.base_entries[0])
                self.assertEqual(exc.exception.code,'KFBF_UNSAFE_DECODER')
            finally:ImageFile.LOAD_TRUNCATED_IMAGES=old

    def test_region_budget_precedes_allocation(self):
        make_kfbf(self.path)
        with KFBFReader(self.path) as r:
            with self.assertRaises(KFBFError):r.read_region(0,0,0,0,100000,100000)

    def test_declared_channel_entries_are_bounded_before_index_allocation(self):
        make_kfbf(self.path,channels=3)
        self.mutate(16,struct.pack('<I',1_000_000))
        self.error('KFBF_RESOURCE_BUDGET')

    def test_declared_channel_grid_is_bounded_before_coverage_allocation(self):
        make_kfbf(self.path)
        self.mutate(20,struct.pack('<2I',2_000_000,2_000_000))
        self.error('KFBF_RESOURCE_BUDGET')

    def test_cumulative_levels_channel_grid_is_bounded(self):
        make_kfbf(self.path)
        with patch('kfbf_reader.MAX_CHANNEL_GRID_CELLS',20):
            self.error('KFBF_RESOURCE_BUDGET')

    def test_no_source_mutation(self):
        make_kfbf(self.path);before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        with KFBFReader(self.path) as r:r.read_region(0,0,0,0,35,27)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()
