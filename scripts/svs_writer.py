#!/usr/bin/env python3
"""Stream RGB uint8 NGFF (TCZYX) into an Aperio-compatible, lossless SVS.

This is an independent writer, not an Aperio scanner emulator. It deliberately
supports only a bounded subset; unsupported data are never silently flattened.
"""
from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import tifffile
import zarr


class SVSUnsupportedError(ValueError):
    """The input cannot be faithfully represented by this writer's v1 contract."""


def _calibration(metadata: dict[str, Any]) -> tuple[float | None, float | None]:
    x, y = metadata.get('physical_size_x'), metadata.get('physical_size_y')
    if x is None and y is None:
        return None, None
    if x is None or y is None:
        raise SVSUnsupportedError('SVS requires both X and Y calibration or neither; a single known axis must not be silently discarded.')
    unit = metadata.get('physical_size_unit')
    if unit not in ('µm', 'μm', 'um', 'micrometer', 'micrometre'):
        raise SVSUnsupportedError('Normalize physical_size_x/y to micrometres and set physical_size_unit before writing SVS.')
    x, y = float(x), float(y)
    if not all(math.isfinite(v) and v > 0 for v in (x, y)):
        raise SVSUnsupportedError('Pixel calibration must be positive and finite.')
    if not math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12):
        raise SVSUnsupportedError('Aperio MPP is isotropic; this v1 writer cannot preserve unequal X/Y pixel sizes. Request a reviewed OME-TIFF alternative.')
    return x, y


def _icc(metadata: dict[str, Any]) -> bytes | None:
    profile = metadata.get('icc_profile')
    if profile is None and metadata.get('icc_profile_base64') is not None:
        profile = base64.b64decode(metadata['icc_profile_base64'], validate=True)
    if profile is None:
        return None
    if not isinstance(profile, (bytes, bytearray)):
        raise SVSUnsupportedError('icc_profile must be bytes (or supply icc_profile_base64).')
    profile = bytes(profile)
    if len(profile) < 128 or profile[36:40] != b'acsp':
        raise SVSUnsupportedError('Invalid ICC profile header; do not discard a supplied profile silently.')
    return profile


def _levels(path: str | Path) -> list[Any]:
    root = zarr.open_group(str(path), mode='r')
    # bioformats2raw emits one series group named 0. A series group may also be
    # passed directly; only its arrays are selected, never another series.
    group = root['0'] if '0' in root and isinstance(root['0'], zarr.Group) else root
    scales = group.attrs.get('multiscales', [])
    if len(scales) != 1:
        raise SVSUnsupportedError('Require exactly one NGFF multiscales definition in the selected series.')
    scale = scales[0]
    axes = [a['name'] if isinstance(a, dict) else a for a in scale.get('axes', [])]
    if axes != ['t', 'c', 'z', 'y', 'x']:
        raise SVSUnsupportedError(f'Expected explicit TCZYX NGFF axes, found {axes!r}.')
    paths = [d['path'] for d in scale.get('datasets', [])]
    if not paths or len(paths) != len(set(paths)):
        raise SVSUnsupportedError('NGFF pyramid datasets are absent or duplicated.')
    if any(not isinstance(p, str) or p.startswith('/') or '..' in p.split('/') for p in paths):
        raise SVSUnsupportedError('NGFF dataset paths must stay within the selected series.')
    arrays = [group[p] for p in paths]
    previous = None
    for array in arrays:
        if not isinstance(array, zarr.Array) or len(array.shape) != 5 or tuple(array.shape[:3]) != (1, 3, 1):
            raise SVSUnsupportedError('RGB SVS v1 requires TCZYX=(1,3,1,Y,X) at every resolution.')
        if array.dtype != np.dtype('uint8'):
            raise SVSUnsupportedError('RGB SVS v1 supports uint8 only; no automatic bit-depth reduction is permitted.')
        dims = tuple(array.shape[-2:])
        if min(dims) < 1 or (previous and (any(a > b for a, b in zip(dims, previous)) or dims == previous)):
            raise SVSUnsupportedError('Pyramid dimensions must decrease monotonically.')
        previous = dims
    if len(arrays) < 2 and max(arrays[0].shape[-2:]) > 1024:
        raise SVSUnsupportedError('Large WSI lacks reduced pyramid levels; regenerate its NGFF pyramid first.')
    return arrays


def _tiles(array: Any, tile_size: int) -> Iterator[np.ndarray]:
    height, width = array.shape[-2:]
    for y in range(0, height, tile_size):
        for x in range(0, width, tile_size):
            plane = np.asarray(array[0, :, 0, y:min(y+tile_size, height), x:min(x+tile_size, width)])
            tile = np.zeros((tile_size, tile_size, 3), dtype=np.uint8)
            tile[:plane.shape[1], :plane.shape[2]] = np.moveaxis(plane, 0, -1)
            yield tile


def _description(width: int, height: int, full_width: int, full_height: int,
                 tile_size: int, mpp: float | None = None, thumbnail: bool = False) -> str:
    text = (f'Aperio Image\n{full_width}x{full_height} [{width}x{height}] '
            f'({tile_size}x{tile_size}) RGB|Creator = qupath-slide-converter'
            '|Converted = true|Compression = lossless deflate')
    if thumbnail:
        text += '|AssociatedImage = thumbnail'
    if mpp is not None:
        text += f'|MPP = {mpp:.15g}'
    return text


def validate_metadata(source_metadata: dict[str, Any]) -> None:
    """Reject unsupported metadata before the costly NGFF conversion stage."""
    if source_metadata.get('rgb') is not True or int(source_metadata.get('C', 0)) != 3:
        raise SVSUnsupportedError('SVS v1 requires true three-sample RGB, not three independent channels.')
    if int(source_metadata.get('Z', 0)) != 1 or int(source_metadata.get('T', 0)) != 1:
        raise SVSUnsupportedError('SVS v1 does not flatten Z stacks or time series.')
    if np.dtype(source_metadata.get('dtype')) != np.dtype('uint8'):
        raise SVSUnsupportedError('SVS v1 requires uint8 RGB; no automatic bit-depth reduction.')
    _calibration(source_metadata)
    _icc(source_metadata)


def write_svs(zarr_path: str | Path, source_metadata: dict[str, Any], output: Path,
              *, tile_size: int = 256) -> dict[str, Any]:
    """Write a new SVS without overwriting an existing path.

    Metadata: X/Y/C/Z/T, dtype, rgb; physical_size_x/y in micrometres (both
    numeric or both null), physical_size_unit='µm', optional ICC bytes/base64.
    Caller must establish that the source is brightfield; RGB alone is not a
    biological modality classifier. All validation precedes output creation.
    Interrupted output is kept at the supplied path for the caller to mark
    partial; the pipeline should supply a pending filename and finalize later.
    """
    if source_metadata.get('rgb') is not True or int(source_metadata.get('C', 0)) != 3:
        raise SVSUnsupportedError('SVS v1 needs explicitly identified three-sample RGB; three independent fluorescence channels are not RGB.')
    if int(source_metadata.get('Z', 0)) != 1 or int(source_metadata.get('T', 0)) != 1:
        raise SVSUnsupportedError('SVS v1 does not flatten Z stacks or time series.')
    if np.dtype(source_metadata.get('dtype')) != np.dtype('uint8'):
        raise SVSUnsupportedError('SVS v1 requires uint8 RGB, without bit-depth conversion.')
    if tile_size < 16 or tile_size > 2048 or tile_size % 16:
        raise ValueError('tile_size must be a multiple of 16 between 16 and 2048.')
    mpp_x, mpp_y = _calibration(source_metadata)
    profile = _icc(source_metadata)
    arrays = _levels(zarr_path)
    height, width = arrays[0].shape[-2:]
    if (int(source_metadata['Y']), int(source_metadata['X'])) != (height, width):
        raise SVSUnsupportedError('Source metadata dimensions disagree with the full-resolution NGFF array.')
    output = Path(output)
    if output.suffix.lower() != '.svs':
        raise ValueError('SVS output must use .svs, including a pending.svs filename.')
    # Generate only a bounded display thumbnail. Every tiled pyramid plane is
    # copied from NGFF as-is, including edge pixels and RGB channel order.
    # Bio-Formats expects the stripped thumbnail to be the smallest image;
    # a larger thumbnail can cause it to drop valid pyramid resolutions.
    thumb_source = arrays[-1]
    stride = max(1, math.ceil(max(thumb_source.shape[-2:]) / 1024))
    thumbnail = np.moveaxis(np.asarray(thumb_source[0, :, 0, ::stride, ::stride]), 0, -1)
    output.parent.mkdir(parents=True, exist_ok=True)
    common = dict(photometric='rgb', planarconfig='contig', compression='deflate',
                  compressionargs={'level': 6}, metadata=None,
                  software='qupath-slide-converter', datetime=False)
    # Exclusive creation also rejects pre-existing symlinks. File handle use
    # prevents TiffWriter from truncating an existing result on a rerun.
    with output.open('xb') as stream, tifffile.TiffWriter(stream, bigtiff=True) as writer:
        for index, array in enumerate(arrays):
            h, w = array.shape[-2:]
            calibration = {}
            if mpp_x is not None:
                calibration = dict(resolution=(10000 / (mpp_x * width / w),
                                               10000 / (mpp_y * height / h)),
                                   resolutionunit='CENTIMETER')
            writer.write(_tiles(array, tile_size), shape=(h, w, 3), dtype=np.uint8,
                         tile=(tile_size, tile_size), subfiletype=0,
                         description=_description(w, h, width, height, tile_size,
                                                  mpp_x if index == 0 else None),
                         iccprofile=profile, **calibration, **common)
            if index == 0:
                th, tw = thumbnail.shape[:2]
                writer.write(thumbnail, rowsperstrip=min(th, 64), subfiletype=0,
                             description=_description(tw, th, width, height, tile_size,
                                                      thumbnail=True),
                             iccprofile=profile, **common)
    return {'format': 'Aperio-compatible SVS', 'bigtiff': True,
            'compression': 'lossless deflate', 'dimensions': [width, height],
            'levels': [[a.shape[-1], a.shape[-2]] for a in arrays],
            'thumbnail_dimensions': [thumbnail.shape[1], thumbnail.shape[0]],
            'physical_size_x': mpp_x, 'physical_size_y': mpp_y,
            'icc_preserved': profile is not None, 'size_bytes': output.stat().st_size}


def verify_svs(zarr_path: str | Path, source_metadata: dict[str, Any],
               output: str | Path) -> dict[str, Any]:
    """Independently check structure and sample corners/centre/tile boundaries.

    This establishes TIFF/NGFF agreement only. QuPath opening and original
    source-to-NGFF comparisons are separate mandatory pipeline checks.
    """
    arrays = _levels(zarr_path)
    comparisons = []
    with tifffile.TiffFile(output) as tif:
        if not tif.is_bigtiff or len(tif.pages) != len(arrays) + 1:
            raise ValueError('SVS BigTIFF/IFD count check failed.')
        if not tif.pages[0].description.startswith('Aperio Image') or tif.pages[1].is_tiled:
            raise ValueError('SVS first image/stripped thumbnail organization check failed.')
        profile = _icc(source_metadata)
        if profile is not None and tif.pages[0].tags[34675].value != profile:
            raise ValueError('ICC profile differs.')
        mpp, _ = _calibration(source_metadata)
        description = tif.pages[0].description
        if mpp is not None and f'|MPP = {mpp:.15g}' not in description:
            raise ValueError('SVS baseline MPP differs.')
        if mpp is None and '|MPP =' in description:
            raise ValueError('SVS contains invented calibration.')
        for i, source in enumerate(arrays):
            page = tif.pages[0 if i == 0 else i + 1]
            h, w = source.shape[-2:]
            if not page.is_tiled or page.shape != (h, w, 3) or page.dtype != np.dtype('uint8') or int(page.compression) not in (8, 32946):
                raise ValueError(f'Invalid SVS image layout at level {i}.')
            store = page.aszarr()
            try:
                target = zarr.open(store, mode='r')
                size_y, size_x = min(64, h), min(64, w)
                positions = {(0, 0), (h-size_y, 0), (0, w-size_x),
                             (h-size_y, w-size_x), ((h-size_y)//2, (w-size_x)//2),
                             (min(max(0, page.tilelength-17), h-size_y),
                              min(max(0, page.tilewidth-17), w-size_x))}
                for y, x in sorted(positions):
                    a = np.moveaxis(np.asarray(source[0, :, 0, y:y+size_y, x:x+size_x]), 0, -1)
                    b = np.asarray(target[y:y+size_y, x:x+size_x, :])
                    if not np.array_equal(a, b):
                        raise ValueError(f'SVS pixel comparison failed at level {i}, x={x}, y={y}.')
                    comparisons.append({'level': i, 'x': x, 'y': y, 'width': size_x, 'height': size_y, 'equal': True})
            finally:
                store.close()
    return {'status': 'PASS', 'reader': 'tifffile', 'comparisons': comparisons,
            'qupath_verified': False, 'limitation': 'Sampled TIFF/NGFF pixel agreement, not an exhaustive original-source comparison.'}


def main() -> None:
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zarr', required=True, type=Path)
    parser.add_argument('--metadata', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text(encoding='utf-8'))
    result = write_svs(args.zarr, metadata, args.output)
    result['verification'] = verify_svs(args.zarr, metadata, args.output)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
