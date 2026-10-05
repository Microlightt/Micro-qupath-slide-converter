#!/usr/bin/env python3
"""Strict KFB 1.6 RGB -> lossless SVS, without a vendor SDK or a Zarr stage."""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
import hashlib
import io
import json
import math
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

from kfb_reader import StrictKFB, KFBReadError, _require


POLICIES = {'error', 'black'}


def _save(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _missing_rectangles(source, level):
    """Exact complement of native logical coverage, using bounded index events.

    Empty grid runs are represented as rectangles, not one entry per absent
    pixel/tile. No image-sized mask is allocated. Adjacent identical X gaps
    merge vertically. The result records every missing logical source pixel.
    """
    events = defaultdict(list)
    events[0]; events[level.height]
    for entry in level.entries:
        x0, y0 = max(0, entry.x), max(0, entry.y)
        x1, y1 = min(level.width, entry.x+entry.width), min(level.height, entry.y+entry.height)
        if x1 > x0 and y1 > y0:
            events[y0].append((1, entry.index, x0, x1))
            events[y1].append((-1, entry.index, x0, x1))
    active = {}
    rectangles, last = [], {}
    previous = 0
    for y in sorted(events):
        if y > previous:
            gaps, end = [], 0
            for start, stop in sorted(active.values()):
                if start > end:
                    gaps.append((end, start))
                end = max(end, stop)
            if end < level.width:
                gaps.append((end, level.width))
            current = {}
            for start, stop in gaps:
                key = (start, stop)
                if key in last and rectangles[last[key]]['y']+rectangles[last[key]]['height'] == previous:
                    i = last[key]
                    rectangles[i]['height'] += y-previous
                else:
                    i = len(rectangles)
                    rectangles.append(dict(x=start, y=previous, width=stop-start, height=y-previous))
                current[key] = i
            last = current
        for delta, index, x0, x1 in events[y]:
            if delta > 0:
                active[index] = (x0, x1)
            else:
                active.pop(index, None)
        previous = y
    expected = source.coverage_report(level)['missing_pixels']
    _require(sum(r['width']*r['height'] for r in rectangles) == expected,
             'Missing-region accounting disagrees with exact source coverage')
    return rectangles


def _native_metadata(source):
    coverage = []
    kept, omitted, previous = [], [], None
    for level in source.levels:
        cov = dict(source.coverage_report(level))
        cov['missing_rectangles'] = _missing_rectangles(source, level)
        cov['missing_coordinates'] = 'native level pixels; origin upper-left; half-open rectangles'
        coverage.append(cov)
        dims = (level.width, level.height)
        if dims == previous:
            omitted.append(dict(native_level=level.index, dimensions=list(dims),
                                reason='Repeated native logical dimensions; source pixels retained in original, not re-encoded as another pyramid level'))
        else:
            if previous and (dims[0] > previous[0] or dims[1] > previous[1]):
                raise KFBReadError('Native pyramid dimensions increase; cannot guess a resolution order', 'KFB_GEOMETRY')
            kept.append(dict(native_level=level.index, width=level.width, height=level.height,
                             downsample=level.downsample, scale=level.scale))
            previous = dims
    return dict(header=source.header, coverage=coverage, kept_levels=kept,
                omitted_redundant_levels=omitted, native_level_count=len(source.levels),
                source_index_sha256=source.index_bytes_sha256,
                offset_audit=source.offset_audit(),
                missing_pixels_total=sum(c['missing_pixels'] for c in coverage),
                missing_policy='error',
                edge_policy='Native logical bounds retained; encoded JPEG overhang lies outside those declared bounds and is clipped/padded for display. Original KFB preserves all encoded bytes.',
                associated_images_excluded=[name for name in source.associated],
                source_metadata_provenance='Read directly from the reviewed KFB 1.6 binary header and tile index; OME XML is synthesized, not vendor-provided')


def _source_xml(width, height, mpp):
    ns = 'http://www.openmicroscopy.org/Schemas/OME/2016-06'
    ET.register_namespace('', ns)
    root = ET.Element('{'+ns+'}OME', Creator='qupath-slide-converter; synthesized KFB header metadata')
    image = ET.SubElement(root, '{'+ns+'}Image', ID='Image:0', Name='KFB primary image')
    ET.SubElement(image, '{'+ns+'}Description').text = 'Synthesized from reviewed KFB 1.6 binary header; not original vendor OME metadata.'
    pixels = ET.SubElement(image, '{'+ns+'}Pixels', ID='Pixels:0', DimensionOrder='XYCZT', Type='uint8',
                           SizeX=str(width), SizeY=str(height), SizeC='3', SizeZ='1', SizeT='1', Interleaved='true',
                           PhysicalSizeX=repr(mpp), PhysicalSizeY=repr(mpp), PhysicalSizeXUnit='µm', PhysicalSizeYUnit='µm')
    ET.SubElement(pixels, '{'+ns+'}Channel', ID='Channel:0:0', SamplesPerPixel='3')
    ET.SubElement(pixels, '{'+ns+'}MetadataOnly')
    return ET.tostring(root, encoding='unicode')


def inspect_kfb(path, cfg=None, series=None):
    if series not in (None, 0):
        raise KFBReadError('KFB backend exposes only the primary RGB image as series 0; associated images are not analytic slide series', 'INVALID_SERIES')
    path = Path(path).resolve()
    with StrictKFB(path) as source:
        width, height = source.dimensions
        details = _native_metadata(source)
        details['missing_policy'] = (cfg or {}).get('kfb_missing_pixels', 'error')
        first = source.jpeg_metadata(source.base_level.entries[0])
        _require(first['mode'] == 'RGB', 'KFB primary JPEG is not RGB; no channel or color-layout inference is allowed')
        st = path.stat()
        image = dict(series=0, name='KFB primary image', X=width, Y=height, Z=1, C=3, T=1, rgb=True)
        result = dict(image, dtype='uint8', effective_channels=1, rgb_samples=3,
                      format='KFB 1.6 JPEG (strict native reader)', resolution_count=len(details['kept_levels']),
                      interleaved=True, little_endian=True, channels=[None],
                      physical_size_x=source.mpp, physical_size_y=source.mpp, physical_size_unit='µm',
                      series_inventory=[image], inventory=[dict(path=str(path), size=st.st_size, mtime_ns=st.st_mtime_ns)],
                      source_xml=_source_xml(width, height, source.mpp), classification='rgb_requires_modality',
                      source_xml_provenance='synthesized_from_kfb_header', kfb=details,
                      icc_status='first_source_tile_sampled; every native tile must agree during conversion')
        profile = first['info'].get('icc_profile')
        if profile:
            result['icc_profile_base64'] = base64.b64encode(profile).decode('ascii')
        return result


def validate_kfb(meta, missing_policy='error'):
    if missing_policy not in POLICIES:
        raise KFBReadError('Missing-pixel policy must be error or black', 'KFB_POLICY')
    details = meta['kfb']
    for coverage in details['coverage']:
        if (coverage['overlapping_pixels_counted_with_multiplicity'] or coverage['off_grid_entry_count'] or
                coverage['fully_outside_logical_entry_count']):
            raise KFBReadError('KFB contains overlapping, off-grid or wholly outside tiles; placement/omission is not guessed', 'KFB_GEOMETRY')
    if details['missing_pixels_total'] and missing_policy == 'error':
        error = KFBReadError('Native KFB coverage is incomplete. Missing source pixel counts and every missing rectangle are in kfb.coverage. Explicit user approval is required to represent these absent pixels with black; no source tissue can be recovered.', 'KFB_MISSING_PIXELS')
        error.details = dict(kfb=dict(missing_pixels_total=details['missing_pixels_total'],
                                      coverage=details['coverage'], missing_policy='error'))
        raise error
    if not details['kept_levels']:
        raise KFBReadError('No primary KFB levels', 'KFB_GEOMETRY')
    tile_size = details['header']['tile_size']
    if tile_size % 16:
        raise KFBReadError('Native KFB tile size is not TIFF-compatible; this version does not silently retile', 'KFB_TILE_SIZE')
    return details


def _match(source, meta, policy):
    fresh = _native_metadata(source)
    _require((meta['X'], meta['Y']) == source.dimensions and
             meta['physical_size_x'] == source.mpp and meta['physical_size_y'] == source.mpp,
             'Supplied metadata differs from the actual KFB header')
    _require(meta['kfb']['source_index_sha256'] == fresh['source_index_sha256'], 'KFB index changed since inspection')
    _require(meta['kfb']['kept_levels'] == fresh['kept_levels'], 'KFB level selection changed since inspection')
    validate_kfb(dict(meta, kfb=fresh), policy)
    return fresh


def read_region(source, level, x, y, width, height, missing_policy='error'):
    if isinstance(level, int):
        level = source.levels[level]
    _require(x >= 0 and y >= 0 and 0 < width <= 4096 and 0 < height <= 4096 and
             x+width <= level.width and y+height <= level.height, 'Invalid or excessive KFB ROI; use bounded in-image coordinates')
    if missing_policy not in POLICIES:
        raise KFBReadError('Unknown missing-pixel policy', 'KFB_POLICY')
    if missing_policy == 'error' and source.coverage_report(level)['missing_pixels']:
        raise KFBReadError('Missing source pixels require an explicit black-fill decision', 'KFB_MISSING_PIXELS')
    out = np.zeros((height, width, 3), np.uint8)
    t = source.tile_size
    for ty in range(y//t*t, y+height, t):
        for tx in range(x//t*t, x+width, t):
            tile = source.read_tile(level, tx, ty)
            x0, y0 = max(x, tx), max(y, ty)
            x1, y1 = min(x+width, tx+t), min(y+height, ty+t)
            out[y0-y:y1-y, x0-x:x1-x] = tile[y0-ty:y1-ty, x0-tx:x1-tx]
    return out


class KFBReaderAdapter:
    def __init__(self, path, cfg):
        self.source = StrictKFB(path)
        self.policy = cfg.get('kfb_missing_pixels', 'error')
        try:
            validate_kfb(dict(kfb=_native_metadata(self.source)), self.policy)
        except BaseException:
            self.source.close()
            raise
    def setSeries(self, index):
        _require(index == 0, 'KFB reader only exposes primary series 0')
    def setResolution(self, index):
        _require(index == 0, 'Core ROI comparison uses full resolution only')
    def isInterleaved(self):
        return True
    def getIndex(self, z, c, t):
        _require((z, c, t) == (0, 0, 0), 'KFB native RGB has one effective channel, Z=1,T=1')
        return 0
    def openBytes(self, index, x, y, width, height):
        _require(index == 0, 'KFB has one primary RGB plane')
        return read_region(self.source, 0, x, y, width, height, self.policy).tobytes()
    def close(self):
        self.source.close()


def open_reader(path, cfg):
    return KFBReaderAdapter(path, cfg)


def _profile(source, meta):
    first = source.jpeg_metadata(source.base_level.entries[0])['info'].get('icc_profile')
    expected = base64.b64decode(meta['icc_profile_base64'], validate=True) if meta.get('icc_profile_base64') else None
    _require(first == expected, 'KFB tile ICC profile differs from inspection metadata')
    if first:
        _require(len(first) >= 128 and first[36:40] == b'acsp', 'Invalid source ICC profile')
    return first


def _checked_decoder(source, profile):
    original = source.decode_tile
    checked = set()
    def decode(entry):
        if entry.index not in checked:
            metadata = source.jpeg_metadata(entry)
            _require(metadata['info'].get('icc_profile') == profile,
                     f'Inconsistent ICC profile at native tile {entry.index}')
            checked.add(entry.index)
        return original(entry)
    source.decode_tile = decode
    return checked


def _description(source, level, missing_policy, thumbnail=False):
    text = (f'Aperio Image\n{source.dimensions[0]}x{source.dimensions[1]} '
            f'[{level.width}x{level.height}] ({source.tile_size}x{source.tile_size}) RGB'
            '|Creator = qupath-slide-converter|OriginalFormat = KFB 1.6'
            '|Converted = true|Compression = lossless deflate'
            f'|MissingSourcePolicy = {missing_policy}')
    if level.index == 0:
        text += f'|MPP = {source.mpp:.15g}|AppMag = {source.objective_power}'
    if thumbnail:
        text += '|AssociatedImage = generated thumbnail'
    return text


def write_kfb_svs(path, meta, output, report_path=None, missing_policy='error', workers=4):
    import tifffile
    output = Path(output)
    _require(output.suffix.lower() == '.svs', 'Output must end in .svs')
    _require(1 <= workers <= 64, 'Worker count must be between 1 and 64')
    validate_kfb(meta, missing_policy)
    started = time.monotonic()
    with StrictKFB(path) as source:
        fresh = _match(source, meta, missing_policy)
        profile = _profile(source, meta)
        checked = _checked_decoder(source, profile)
        levels = [source.levels[r['native_level']] for r in fresh['kept_levels']]
        thumbnail_level = levels[-1]
        # A large lowest native level cannot become a bounded thumbnail without
        # resampling. Refuse rather than allocate a whole WSI or invent a level.
        _require(max(thumbnail_level.dimensions) <= 1024, 'Lowest native level exceeds thumbnail memory bound; unsupported KFB pyramid')
        thumbnail = read_region(source, thumbnail_level, 0, 0, thumbnail_level.width, thumbnail_level.height, missing_policy)
        report = dict(status='WRITING', format='Aperio-compatible SVS', source_format='KFB 1.6 RGB JPEG',
                      compression='lossless deflate', predictor=2, missing_policy=missing_policy,
                      missing_fill_rgb=[0, 0, 0] if missing_policy == 'black' else None,
                      native_coverage=fresh['coverage'], omitted_redundant_levels=fresh['omitted_redundant_levels'],
                      edge_policy=fresh['edge_policy'], associated_images_excluded=fresh['associated_images_excluded'],
                      levels=[], regenerated_pyramid=False, original_jpeg_loss_not_recoverable=True)
        if report_path:
            _save(report_path, report)
        common = dict(photometric='rgb', planarconfig='contig', compression='deflate', compressionargs={'level': 6},
                      predictor=2, metadata=None, iccprofile=profile, software='qupath-slide-converter', datetime=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output.open('xb') as handle, tifffile.TiffWriter(handle, bigtiff=True) as writer:
                for index, level in enumerate(levels):
                    def tiles(level=level):
                        written = 0
                        for y in range(0, level.height, source.tile_size):
                            for x in range(0, level.width, source.tile_size):
                                yield source.read_tile(level, x, y)
                                written += 1
                                if written % 10000 == 0:
                                    print('KFB_WRITE_TILE_PROGRESS', level.index, written, flush=True)
                    writer.write(tiles(), shape=(level.height, level.width, 3), dtype=np.uint8,
                                 tile=(source.tile_size, source.tile_size), subfiletype=0,
                                 description=_description(source, level, missing_policy),
                                 resolution=(10000/(source.mpp*level.downsample),)*2, resolutionunit='CENTIMETER',
                                 maxworkers=workers, buffersize=16*1024**2, **common)
                    report['levels'].append(dict(level=index, native_level=level.index, width=level.width, height=level.height,
                                                  tile_count=math.ceil(level.width/source.tile_size)*math.ceil(level.height/source.tile_size)))
                    if index == 0:
                        writer.write(thumbnail, rowsperstrip=min(64, thumbnail_level.height), subfiletype=0,
                                     description=_description(source, thumbnail_level, missing_policy, True), **common)
                    if report_path:
                        _save(report_path, report)
                    print('KFB_WRITE_LEVEL', index, level.width, level.height, flush=True)
                # Even omitted duplicate-size levels must decode and agree on
                # ICC; their source data stay in the original, explicitly listed.
                for entry in source.entries:
                    if entry.index not in checked:
                        source.decode_tile(entry)
            _require(len(checked) == len(source.entries), 'Not all native KFB JPEG tiles were decoded and ICC checked')
            report.update(status='WRITTEN_PENDING_PIXEL_VALIDATION', native_jpeg_tiles_checked=len(checked),
                          icc_preserved=profile is not None, icc_sha256=hashlib.sha256(profile).hexdigest() if profile else None,
                          size_bytes=output.stat().st_size, elapsed_seconds=time.monotonic()-started)
            if report_path:
                _save(report_path, report)
            return report
        except BaseException as exc:
            report.update(status='FAILED', error=str(exc))
            if report_path:
                _save(report_path, report)
            raise


def verify_kfb_svs(path, meta, output, missing_policy='error'):
    """Compare every logical RGB pixel in every retained level, tile by tile."""
    import tifffile
    validate_kfb(meta, missing_policy)
    result = dict(status='RUNNING', reader='tifffile', levels=[], comparisons=[],
                  missing_policy=missing_policy, comparison_scope='All logical pixels in every retained native level; explicitly chosen black where source has no pixels',
                  qupath_verified=False)
    with StrictKFB(path) as source, tifffile.TiffFile(output) as tiff:
        fresh = _match(source, meta, missing_policy)
        profile = _profile(source, meta)
        checked = _checked_decoder(source, profile)
        levels = [source.levels[r['native_level']] for r in fresh['kept_levels']]
        _require(tiff.is_bigtiff and len(tiff.pages) == len(levels)+1, 'SVS IFD organization or BigTIFF flag differs')
        _require(tiff.pages[0].description.startswith('Aperio Image') and not tiff.pages[1].is_tiled,
                 'SVS first/thumbnail IFD organization differs')
        _require(f'|MPP = {source.mpp:.15g}' in tiff.pages[0].description, 'SVS calibration differs from the native KFB MPP')
        for index, level in enumerate(levels):
            page = tiff.pages[0 if index == 0 else index+1]
            _require(page.is_tiled and page.shape == (level.height, level.width, 3) and page.dtype == np.dtype('uint8'), 'SVS dimensions, RGB layout or bit depth changed')
            _require(int(page.compression) in (8, 32946) and int(page.predictor) == 2, 'SVS lossless encoding differs')
            actual_profile = page.tags[34675].value if 34675 in page.tags else None
            _require(actual_profile == profile, 'SVS ICC profile differs')
            source_hash, target_hash = hashlib.sha256(), hashlib.sha256()
            count, visited = 0, set()
            expected_count = math.ceil(level.width/source.tile_size)*math.ceil(level.height/source.tile_size)
            _require(len(page.dataoffsets) == expected_count and len(set(page.dataoffsets)) == expected_count,
                     'SVS tile count or unique tile offsets differ')
            for pixels, location, shape in page.segments(maxworkers=4, buffersize=16*1024**2):
                _require(pixels is not None, 'SVS tile cannot be decoded')
                y, x = int(location[2]), int(location[3])
                _require((x, y) not in visited and x % source.tile_size == 0 and y % source.tile_size == 0 and
                         0 <= x < level.width and 0 <= y < level.height, 'SVS decoded tile positions are duplicated or invalid')
                visited.add((x, y))
                height, width = min(source.tile_size, level.height-y), min(source.tile_size, level.width-x)
                expected = source.read_tile(level, x, y)[:height, :width]
                actual = pixels[0, :height, :width, :]
                _require(np.array_equal(expected, actual), f'KFB/SVS pixel difference at native level {level.index}, x={x}, y={y}')
                source_hash.update(np.ascontiguousarray(expected).tobytes())
                target_hash.update(np.ascontiguousarray(actual).tobytes())
                count += 1
                if count % 10000 == 0:
                    print('KFB_VERIFY_TILE_PROGRESS', index, count, expected_count, flush=True)
            _require(count == expected_count, 'Not every SVS tile was compared')
            result['levels'].append(dict(level=index, native_level=level.index, width=level.width, height=level.height,
                                          compared_tiles=count, compared_pixels=level.width*level.height,
                                          source_tile_sequence_sha256=source_hash.hexdigest(), output_tile_sequence_sha256=target_hash.hexdigest(),
                                          missing_pixels=fresh['coverage'][level.index]['missing_pixels'], mismatched_rgb_values=0))
            result['comparisons'].append(dict(level=index, native_level=level.index, x=0, y=0,
                                              width=min(128, level.width), height=min(128, level.height), equal=True))
            print('KFB_VERIFY_LEVEL', index, count, flush=True)
        for entry in source.entries:
            if entry.index not in checked:
                source.decode_tile(entry)
        thumb_level = levels[-1]
        expected_thumbnail = read_region(source, thumb_level, 0, 0, thumb_level.width, thumb_level.height, missing_policy)
        _require(np.array_equal(tiff.pages[1].asarray(), expected_thumbnail), 'Generated display thumbnail differs')
        result.update(status='PASS', native_coverage=fresh['coverage'], omitted_redundant_levels=fresh['omitted_redundant_levels'],
                      native_jpeg_tiles_checked=len(checked), missing_pixels_total=fresh['missing_pixels_total'],
                      missing_fill_rgb=[0, 0, 0] if missing_policy == 'black' else None,
                      edge_policy=fresh['edge_policy'], mismatched_rgb_values_all_retained_levels=0)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--metadata', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--missing-pixels', choices=sorted(POLICIES), default='error')
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    meta = json.loads(args.metadata.read_text(encoding='utf-8'))
    report = write_kfb_svs(args.input, meta, args.output, args.report, args.missing_pixels, args.workers)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
