#!/usr/bin/env python3
"""Strict, bounded reader for the independently verified KFBF 2.1 JPEG layout.

This implementation reads explicit coordinates and little-endian metadata from
source bytes. It never infers coordinates from index order, recolors a channel,
rescales signal, heals a JPEG, or invents missing pixel values by default.
"""
from dataclasses import dataclass
import hashlib
import io
import math
from pathlib import Path
import struct


class KFBFError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def require(value, code, message, **details):
    if not value:
        raise KFBFError(code, message, **details)


@dataclass(frozen=True)
class Tile:
    index: int
    level: int
    channel: int
    x: int
    y: int
    width: int
    height: int
    offset: int
    length: int


# Only baseline, single-component, 8-bit JPEG has been validated for this route.
MAX_JPEG_BYTES = 64 * 1024 ** 2
MAX_SPATIAL_TILES = 1_000_000
MAX_CHANNELS = 32
MAX_DIMENSION = 2_000_000
MAX_GRID_CELLS = 2_000_000
MAX_CHANNEL_ENTRIES = 2_000_000
MAX_CHANNEL_GRID_CELLS = 2_000_000
MAX_REGION_EDGE = 4096


def jpeg_header(data):
    """Read encoded precision/components and geometry, without decoding/coercion."""
    require(data[:2] == b'\xff\xd8', 'KFBF_BAD_JPEG', 'JPEG SOI marker missing')
    pos, found = 2, None
    while pos < len(data):
        require(data[pos] == 255, 'KFBF_BAD_JPEG', 'Invalid JPEG marker boundary')
        while pos < len(data) and data[pos] == 255:
            pos += 1
        require(pos < len(data), 'KFBF_BAD_JPEG', 'Truncated JPEG marker')
        marker = data[pos]
        pos += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            require(marker != 0xD9, 'KFBF_BAD_JPEG', 'JPEG ended before scan header')
            continue
        require(pos + 2 <= len(data), 'KFBF_BAD_JPEG', 'Truncated JPEG segment length')
        length = int.from_bytes(data[pos:pos + 2], 'big')
        require(length >= 2 and pos + length <= len(data), 'KFBF_BAD_JPEG', 'Invalid or truncated JPEG segment')
        payload = data[pos + 2:pos + length]
        # All SOF markers except Huffman-table, extension, and arithmetic-table markers.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            require(found is None and len(payload) >= 6, 'KFBF_BAD_JPEG', 'Duplicate or truncated JPEG frame header')
            precision, height, width, components = struct.unpack('>BHHB', payload[:6])
            require(len(payload) == 6 + components * 3, 'KFBF_BAD_JPEG', 'JPEG component table length differs')
            found = dict(precision=precision, height=height, width=width, components=components,
                         sof_marker=f'0x{marker:02x}')
        if marker == 0xDA:
            require(found is not None, 'KFBF_BAD_JPEG', 'JPEG has no frame header')
            return found
        pos += length
    raise KFBFError('KFBF_BAD_JPEG', 'JPEG has no complete scan header')


class KFBFReader:
    """One read-only file handle; use separate instances for parallel readers."""
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self._stream = self.path.open('rb')
        self.size = self.path.stat().st_size
        self.source_stat = dict(size=self.size, mtime_ns=self.path.stat().st_mtime_ns)
        self.dtype = 'uint8'
        self.source_encoded_bit_depth = 8
        self.decoder_conversion = 'none; single-component baseline JPEG to uint8'
        try:
            self._parse_header()
            self._parse_index()
            self._build_coverage()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if getattr(self, '_stream', None) is not None:
            self._stream.close()
            self._stream = None

    def _read(self, offset, length):
        require(isinstance(offset, int) and isinstance(length, int) and offset >= 0 and length >= 0
                and offset + length <= self.size, 'KFBF_BAD_OFFSET', 'Source byte range exceeds the file',
                offset=offset, length=length, file_size=self.size)
        self._stream.seek(offset)
        data = self._stream.read(length)
        require(len(data) == length, 'KFBF_TRUNCATED', 'Source changed or truncated during read')
        return data

    def _parse_header(self):
        header = self._read(0, 100)
        require(header[:8] == b'\xf1\x01\xee\xeeKFBF', 'UNSUPPORTED_KFBF_LAYOUT', 'Not a supported KFBF file header')
        self.version = struct.unpack_from('<f', header, 12)[0]
        require(abs(self.version - 2.1) < 1e-5, 'UNSUPPORTED_KFBF_VERSION', 'Only the validated KFBF 2.1 layout is admitted', version=self.version)
        require(header[92:96] == b'\xff\x01\xee\xee', 'KFBF_BAD_HEADER', 'Header end marker missing')
        self.spatial_tile_count, raw_dim20, raw_dim24, self.magnification = struct.unpack_from('<4i', header, 16)
        require(0 < self.spatial_tile_count <= MAX_SPATIAL_TILES, 'KFBF_BAD_HEADER', 'Spatial tile count outside supported bounds')
        # These slots are Y and X in the source JPEG/index coordinate system.
        self.height, self.width = raw_dim20, raw_dim24
        require(0 < self.width <= MAX_DIMENSION and 0 < self.height <= MAX_DIMENSION, 'KFBF_BAD_HEADER', 'Image dimensions outside supported bounds')
        require(0 < self.magnification <= 200, 'KFBF_BAD_HEADER', 'Invalid source magnification')
        require(header[32:40].rstrip(b'\0') == b'JPEG', 'UNSUPPORTED_KFBF_CODEC', 'Only source JPEG tiles are admitted')
        self.index_offset = struct.unpack_from('<Q', header, 68)[0]
        self.tile_size = struct.unpack_from('<i', header, 88)[0]
        require(16 <= self.tile_size <= 4096, 'KFBF_BAD_HEADER', 'Unsupported nominal tile size')
        mpp = struct.unpack_from('<f', header, 76)[0]
        self.mpp = float(mpp) if math.isfinite(mpp) and mpp > 0 else None
        count = struct.unpack_from('<I', header, 96)[0]
        require(1 <= count <= 256, 'KFBF_BAD_METADATA', 'Extended metadata count outside supported bounds')
        pos, tags, raw_tags = 100, {}, []
        for _ in range(count):
            tag, length = struct.unpack('<II', self._read(pos, 8))
            require(0 < tag <= 65535 and 0 < length <= 4096, 'KFBF_BAD_METADATA', 'Invalid metadata tag or length')
            require(pos + 8 + length <= min(self.size, 1024 * 1024), 'KFBF_BAD_METADATA', 'Extended header too large')
            require(tag not in tags, 'KFBF_BAD_METADATA', 'Duplicate metadata tag', tag=tag)
            value = self._read(pos + 8, length)
            tags[tag] = value
            raw_tags.append(dict(tag=tag, offset=pos, length=length, value_hex=value.hex()))
            pos += 8 + length
        self.header_end = pos
        self.raw_metadata_tags = raw_tags
        require(0x4B in tags and len(tags[0x4B]) == 4, 'KFBF_UNKNOWN_CHANNEL_COUNT', 'Independent channel count tag absent or invalid')
        self.channel_count = struct.unpack('<I', tags[0x4B])[0]
        require(1 <= self.channel_count <= MAX_CHANNELS, 'KFBF_UNKNOWN_CHANNEL_COUNT', 'Independent channel count outside supported range')
        require(self.spatial_tile_count * self.channel_count <= MAX_CHANNEL_ENTRIES, 'KFBF_RESOURCE_BUDGET',
                'Cumulative channel tile entries exceed the bounded reader budget')
        base_cells = math.ceil(self.width / self.tile_size) * math.ceil(self.height / self.tile_size)
        require(base_cells * self.channel_count <= MAX_CHANNEL_GRID_CELLS, 'KFBF_RESOURCE_BUDGET',
                'Base channel grid exceeds the bounded coverage audit budget')
        pointers, blocks = {}, {}
        for tag, stride in ((0x4C, 20), (0x4D, 40), (0x4E, 4), (0x4F, 12), (0x54, 8)):
            if tag not in tags:
                continue
            require(len(tags[tag]) == 8, 'KFBF_BAD_METADATA', 'Channel metadata pointer has wrong size', tag=tag)
            offset = struct.unpack('<Q', tags[tag])[0]
            require(offset >= self.header_end, 'KFBF_BAD_METADATA', 'Channel metadata points inside header', tag=tag)
            pointers[tag] = (offset, offset + stride * self.channel_count)
            blocks[tag] = self._read(offset, stride * self.channel_count)
        ranges = sorted((a, b, tag) for tag, (a, b) in pointers.items())
        require(all(a[1] <= b[0] for a, b in zip(ranges, ranges[1:])), 'KFBF_BAD_METADATA', 'Channel metadata blocks overlap')
        self.metadata_ranges = ranges
        self.channel_metadata_offsets = {str(tag): a for tag, (a, b) in pointers.items()}
        def name(tag, channel, stride):
            if tag not in blocks:
                return None
            data = blocks[tag][channel * stride:(channel + 1) * stride].split(b'\0', 1)[0]
            if not data:
                return None
            try:
                return data.decode('utf-8')
            except UnicodeDecodeError:
                # Preserve bytes separately; no guess of a biological identity.
                return None
        self.channels = []
        for channel in range(self.channel_count):
            color = None
            if 0x4F in blocks:
                color = list(struct.unpack_from('<3I', blocks[0x4F], channel * 12))
                require(all(0 <= value <= 255 for value in color), 'KFBF_BAD_METADATA', 'Channel color outside RGB8 range')
            exposure = struct.unpack_from('<d', blocks[0x54], channel * 8)[0] if 0x54 in blocks else None
            require(exposure is None or (math.isfinite(exposure) and exposure >= 0), 'KFBF_BAD_METADATA', 'Invalid exposure value')
            channel_id = struct.unpack_from('<I', blocks[0x4E], channel * 4)[0] if 0x4E in blocks else None
            self.channels.append(dict(index=channel, name=name(0x4D, channel, 40), color_rgb=color,
                                      exposure_raw=exposure, exposure_unit=None,
                                      source_channel_id=channel_id, source_channel_label=name(0x4C, channel, 20),
                                      name_raw_hex=blocks[0x4D][channel*40:(channel+1)*40].hex() if 0x4D in blocks else None,
                                      metadata_source='source little-endian KFBF 2.1 metadata; missing values remain unknown'))
        self.header_sha256 = hashlib.sha256(self._read(0, self.header_end)).hexdigest()

    def _parse_index(self):
        require(self.index_offset >= self.header_end, 'KFBF_BAD_OFFSET', 'Tile index overlaps header')
        raw = self._read(self.index_offset, self.spatial_tile_count * 64)
        self.source_index_sha256 = hashlib.sha256(raw).hexdigest()
        self.entries, levels, self._tile_maps = [], {}, {}
        seen_spatial = set()
        table_ranges = set()
        ordered_offset_tables, ordered_length_tables = [], []
        precision_counts = {}
        for index in range(self.spatial_tile_count):
            data = raw[index * 64:(index + 1) * 64]
            require(data[:4] == b'\xf1\x04\xee\xee' and data[60:64] == b'\xff\x04\xee\xee', 'KFBF_BAD_INDEX', 'Tile record marker missing', index=index)
            x, y, width, height, mag = struct.unpack_from('<4if', data, 4)
            require(math.isfinite(mag) and mag > 0, 'KFBF_BAD_INDEX', 'Invalid tile magnification', index=index)
            level_float = math.log2(self.magnification / mag)
            level = round(level_float)
            require(0 <= level <= 30 and abs(level_float - level) < 1e-5, 'KFBF_BAD_INDEX', 'Tile magnification is not a native power-of-two level', index=index)
            level_w, level_h = max(1, self.width // (2 ** level)), max(1, self.height // (2 ** level))
            require(0 <= x < level_w and 0 <= y < level_h and x % self.tile_size == 0 and y % self.tile_size == 0,
                    'KFBF_BAD_GEOMETRY', 'Tile coordinates outside the declared native grid', index=index, x=x, y=y, level=level)
            require(0 < width <= self.tile_size and 0 < height <= self.tile_size and x + width <= level_w and y + height <= level_h,
                    'KFBF_BAD_GEOMETRY', 'Tile dimensions exceed declared logical source bounds', index=index)
            require((level, x, y) not in seen_spatial, 'KFBF_OVERLAP', 'Duplicate spatial tile position', index=index)
            seen_spatial.add((level, x, y))
            offset_table, length_table = struct.unpack_from('<QQ', data, 36)
            ordered_offset_tables.append(offset_table)
            ordered_length_tables.append(length_table)
            for offset in (offset_table, length_table):
                require(offset >= self.header_end and offset + self.channel_count * 8 <= self.index_offset,
                        'KFBF_BAD_OFFSET', 'Channel indirection table outside supported range', index=index)
                table_ranges.add((offset, offset + self.channel_count * 8))
            offsets = struct.unpack('<' + 'Q' * self.channel_count, self._read(offset_table, self.channel_count * 8))
            lengths = struct.unpack('<' + 'Q' * self.channel_count, self._read(length_table, self.channel_count * 8))
            require(struct.unpack_from('<I', data, 32)[0] == lengths[0], 'KFBF_BAD_INDEX',
                    'Primary channel byte length differs from the spatial index', index=index)
            if level not in levels:
                levels[level] = dict(level=level, width=level_w, height=level_h, magnification=float(mag), entries=[])
            for channel, (offset, length) in enumerate(zip(offsets, lengths)):
                require(length <= MAX_JPEG_BYTES, 'KFBF_TILE_TOO_LARGE', 'Source JPEG exceeds bounded tile budget', index=index, channel=channel)
                entry = Tile(index, level, channel, x, y, width, height, offset, length)
                if length:
                    require(offset >= self.header_end and offset + length <= self.index_offset, 'KFBF_BAD_OFFSET', 'JPEG payload outside supported source range', index=index, channel=channel)
                    # Header inspection reads the actual payload and establishes source encoding for every channel and level.
                    payload = self._read(offset, length)
                    require(payload[-2:] == b'\xff\xd9', 'KFBF_TRUNCATED_JPEG', 'Source JPEG EOI missing', index=index, channel=channel)
                    info = jpeg_header(payload)
                    require(info['precision'] == 8 and info['components'] == 1 and info['sof_marker'] == '0xc0',
                            'UNSUPPORTED_KFBF_ENCODING', 'Only independently encoded single-component baseline JPEG8 is validated; no luma/bit-depth conversion', index=index, channel=channel, encoded=info)
                    require(info['width'] == width and info['height'] == height, 'KFBF_JPEG_GEOMETRY', 'Decoded frame dimensions disagree with explicit tile index', index=index, channel=channel, encoded=info)
                    precision_counts['8_bit_1_component_baseline'] = precision_counts.get('8_bit_1_component_baseline', 0) + 1
                self.entries.append(entry)
                levels[level]['entries'].append(entry)
                self._tile_maps.setdefault((level, channel), {})[(x, y)] = entry
        require(0 in levels, 'KFBF_NO_BASE_IMAGE', 'No full-resolution primary pixels exist')
        # The validated variant has one contiguous offset table and one contiguous
        # length table. This independently cross-checks channel count: reading only
        # three slots from a real four-channel table must not silently drop a plane.
        stride = self.channel_count * 8
        require(all(offset == ordered_offset_tables[0] + i * stride for i, offset in enumerate(ordered_offset_tables))
                and all(offset == ordered_length_tables[0] + i * stride for i, offset in enumerate(ordered_length_tables)),
                'KFBF_CHANNEL_TABLE_LAYOUT', 'Channel count does not match contiguous source offset/length table geometry')
        offset_end = ordered_offset_tables[0] + self.spatial_tile_count * stride
        length_end = ordered_length_tables[0] + self.spatial_tile_count * stride
        offset_gap = ordered_length_tables[0] - offset_end
        length_gap = self.index_offset - length_end
        require(offset_gap in (0, stride) and length_gap == offset_gap, 'KFBF_CHANNEL_TABLE_LAYOUT',
                'Unsupported channel table trailing layout; no unrecorded planes are silently dropped')
        self.unindexed_small_jpeg_planes = []
        if offset_gap:
            extra_offsets = struct.unpack('<' + 'Q' * self.channel_count, self._read(offset_end, stride))
            extra_lengths = struct.unpack('<' + 'Q' * self.channel_count, self._read(length_end, stride))
            for c, (offset, length) in enumerate(zip(extra_offsets, extra_lengths)):
                require(0 < length <= MAX_JPEG_BYTES and offset >= self.header_end and offset + length <= ordered_offset_tables[0],
                        'KFBF_BAD_OFFSET', 'Invalid unindexed JPEG plane range', channel=c)
                payload = self._read(offset, length)
                require(payload[-2:] == b'\xff\xd9', 'KFBF_TRUNCATED_JPEG', 'Unindexed JPEG plane EOI missing')
                info = jpeg_header(payload)
                require(info['precision']==8 and info['components']==1 and info['sof_marker']=='0xc0'
                        and 0<info['width']<=1024 and 0<info['height']<=1024,
                        'UNSUPPORTED_KFBF_ENCODING', 'Unsupported unindexed plane; cannot assign an invented position or native level')
                self.unindexed_small_jpeg_planes.append(dict(channel=c, offset=offset, length=length, encoded=info,
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                    disposition='retained in unchanged source; no spatial position or pyramid index is invented'))
        require(len(table_ranges) == self.spatial_tile_count * 2, 'KFBF_BAD_INDEX', 'Aliased channel tables are unsupported')
        # Tables must be distinct; aliasing offsets/length tables is ambiguous.
        ordered_tables = sorted(table_ranges)
        require(all(a[1] <= b[0] for a, b in zip(ordered_tables, ordered_tables[1:])), 'KFBF_BAD_INDEX', 'Channel indirection tables overlap')
        self.table_range_start = ordered_tables[0][0]
        for entry in self.entries:
            if entry.length:
                require(entry.offset + entry.length <= self.table_range_start, 'KFBF_BAD_INDEX', 'JPEG payload overlaps channel indirection tables')
                require(all(entry.offset + entry.length <= a or entry.offset >= b for a, b, _ in self.metadata_ranges),
                        'KFBF_BAD_INDEX', 'JPEG payload overlaps channel metadata')
        self.levels = [levels[k] for k in sorted(levels)]
        self.base_entries = levels[0]['entries']
        self.encoded_tile_counts = precision_counts

    def _build_coverage(self):
        reports = []
        total_grid = sum(math.ceil(lev['width']/self.tile_size)*math.ceil(lev['height']/self.tile_size)
                         for lev in self.levels) * self.channel_count
        require(total_grid <= MAX_CHANNEL_GRID_CELLS, 'KFBF_RESOURCE_BUDGET',
                'Cumulative channel grid exceeds the bounded coverage audit budget')
        for level in self.levels:
            w, h, number = level['width'], level['height'], level['level']
            grid_count = math.ceil(w / self.tile_size) * math.ceil(h / self.tile_size)
            require(grid_count <= MAX_GRID_CELLS, 'KFBF_GRID_TOO_LARGE', 'Declared tile grid exceeds bounded audit budget')
            for channel in range(self.channel_count):
                tiles = self._tile_maps.get((number, channel), {})
                missing = []
                recorded = 0
                for y in range(0, h, self.tile_size):
                    for x in range(0, w, self.tile_size):
                        cw, ch = min(self.tile_size, w-x), min(self.tile_size, h-y)
                        tile = tiles.get((x, y))
                        if tile is None or tile.length == 0:
                            missing.append(dict(x=x, y=y, width=cw, height=ch))
                            continue
                        recorded += tile.width * tile.height
                        if tile.width < cw:
                            missing.append(dict(x=x+tile.width, y=y, width=cw-tile.width, height=tile.height))
                        if tile.height < ch:
                            missing.append(dict(x=x, y=y+tile.height, width=cw, height=ch-tile.height))
                missing_pixels = sum(r['width']*r['height'] for r in missing)
                require(recorded + missing_pixels == w*h, 'KFBF_COVERAGE_ERROR', 'Coverage rectangles fail partition audit')
                reports.append(dict(level=number, channel=channel, width=w, height=h, total_pixels=w*h,
                                    recorded_pixels=recorded, missing_pixels=missing_pixels,
                                    missing_rectangle_count=len(missing), missing_rectangles=missing,
                                    overlap_pixels=0, out_of_bounds_pixels=0))
        self.coverage_report = dict(levels=reports,
                                    base_missing_pixels=sum(v['missing_pixels'] for v in reports if v['level']==0),
                                    all_native_levels_missing_pixels=sum(v['missing_pixels'] for v in reports),
                                    unit='channel pixel samples', missing_policy_default='error')

    def metadata(self):
        return dict(format='KFBF', version='2.1', width=self.width, height=self.height,
                    channel_count=self.channel_count, channels=self.channels, dtype=self.dtype,
                    source_encoded_bit_depth=self.source_encoded_bit_depth, encoded_components=1,
                    decoder_conversion=self.decoder_conversion, physical_size_x=self.mpp, physical_size_y=self.mpp,
                    physical_size_unit='µm' if self.mpp is not None else None,
                    magnification=self.magnification, tile_size=self.tile_size,
                    spatial_tile_count=self.spatial_tile_count, encoded_tile_counts=self.encoded_tile_counts,
                    native_levels=[{k:v for k,v in lev.items() if k!='entries'} for lev in self.levels],
                    unindexed_small_jpeg_planes=self.unindexed_small_jpeg_planes,
                    coverage=self.coverage_report, source_index_sha256=self.source_index_sha256,
                    source_header_sha256=self.header_sha256, raw_metadata_tags=self.raw_metadata_tags,
                    channel_metadata_offsets=self.channel_metadata_offsets,
                    geometry_provenance='explicit source index X@4,Y@8,Width@12,Height@16; source JPEG dimensions checked; fixed-header X@24,Y@20',
                    orientation='native source pixel coordinate order; no transpose or rotation',
                    exposure_unit_status='unknown; raw exposure values retained without assuming milliseconds',
                    source_stat=self.source_stat)

    def decode_tile(self, tile):
        import numpy as np
        from PIL import Image, ImageFile
        require(tile.length > 0, 'KFBF_MISSING_PIXELS', 'Requested tile has no encoded source pixels', index=tile.index, channel=tile.channel)
        require(not ImageFile.LOAD_TRUNCATED_IMAGES, 'KFBF_UNSAFE_DECODER', 'Pillow truncated-image recovery must remain disabled')
        payload = self._read(tile.offset, tile.length)
        require(payload[-2:] == b'\xff\xd9', 'KFBF_TRUNCATED_JPEG', 'JPEG end marker missing')
        info = jpeg_header(payload)
        require(info['precision']==8 and info['components']==1 and info['sof_marker']=='0xc0', 'UNSUPPORTED_KFBF_ENCODING', 'Source encoding changed since inspection')
        with Image.open(io.BytesIO(payload)) as image:
            require(image.format == 'JPEG' and image.mode == 'L' and image.size == (tile.width,tile.height),
                    'KFBF_DECODE_MISMATCH', 'Decoder would change source mode or geometry', mode=image.mode, size=image.size)
            image.load()
            pixels = np.asarray(image).copy()
        require(pixels.dtype == np.uint8 and pixels.shape == (tile.height,tile.width), 'KFBF_DECODE_MISMATCH', 'Decoded source dtype or dimensions changed')
        return pixels

    def read_region(self, level, c, x, y, width, height, missing_pixels='error'):
        import numpy as np
        require(missing_pixels in ('error','black'), 'KFBF_BAD_POLICY', 'Missing pixels policy must be error or black')
        levels = {v['level']:v for v in self.levels}
        require(level in levels and 0<=c<self.channel_count, 'KFBF_BAD_REGION', 'Unknown source level or channel')
        lev=levels[level]
        require(0<width<=MAX_REGION_EDGE and 0<height<=MAX_REGION_EDGE and x>=0 and y>=0
                and x+width<=lev['width'] and y+height<=lev['height'], 'KFBF_BAD_REGION', 'ROI exceeds source bounds or budget')
        result=np.zeros((height,width),dtype=np.uint8)
        known=np.zeros((height,width),dtype=bool)
        mapping=self._tile_maps.get((level,c),{})
        for ty in range((y//self.tile_size)*self.tile_size,y+height,self.tile_size):
            for tx in range((x//self.tile_size)*self.tile_size,x+width,self.tile_size):
                tile=mapping.get((tx,ty))
                if tile is None or tile.length==0:continue
                x0,y0=max(x,tx),max(y,ty)
                x1,y1=min(x+width,tx+tile.width),min(y+height,ty+tile.height)
                if x1<=x0 or y1<=y0:continue
                pixels=self.decode_tile(tile)
                result[y0-y:y1-y,x0-x:x1-x]=pixels[y0-ty:y1-ty,x0-tx:x1-tx]
                known[y0-y:y1-y,x0-x:x1-x]=True
        require(missing_pixels=='black' or bool(known.all()), 'KFBF_MISSING_PIXELS', 'Requested ROI contains unknown source pixels; black fill requires an explicit user choice', missing_pixels=int((~known).sum()),level=level,channel=c)
        return result


# Explicit alias convenient for integrations; both names share the same contract.
StrictKFBF = KFBFReader
