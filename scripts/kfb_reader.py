"""Strict, read-only KFB JPEG tile reader for lossless format conversion.

This module never repairs an index, resizes a tile, applies an ICC transform,
or enables Pillow's truncated-image recovery. The reader is generic and does not upload data or require a vendor SDK.

Binary layout references (independent implementations, not vendor guarantees):
https://raw.githubusercontent.com/tadejsv/openslide/support-kfb/src/openslide-vendor-kfbio.c
https://raw.githubusercontent.com/yifanfeng97/kfbslide/main/src/kfbslide/_kfbformat.py
https://raw.githubusercontent.com/yifanfeng97/kfbslide/main/src/kfbslide/_slide.py

Known limits: the published pure-Python implementation documents KFB v1.6
validation only. This reader supports the reviewed 96-byte JPEG header and
64-byte tile-index layout; unknown/corrupt layouts fail explicitly. Native
logical level dimensions follow floor(base dimensions / native downsample),
as in both reference readers. Encoded tile pixels outside those bounds remain
available through decode_tile/jpeg_bytes and are recorded as overhang.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import struct
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFile


PARSER_SOURCES = [
    "https://raw.githubusercontent.com/tadejsv/openslide/support-kfb/src/openslide-vendor-kfbio.c",
    "https://raw.githubusercontent.com/yifanfeng97/kfbslide/main/src/kfbslide/_kfbformat.py",
    "https://raw.githubusercontent.com/yifanfeng97/kfbslide/main/src/kfbslide/_slide.py",
]


class KFBReadError(RuntimeError):
    def __init__(self, message: str, code: str = 'KFB_READ_ERROR'):
        super().__init__(message)
        self.code = code
        self.details = {}


MAX_TILE_COUNT = 1_000_000
MAX_JPEG_BYTES = 64 * 1024**2
MAX_TILE_EDGE = 4096


@dataclass(frozen=True)
class TileEntry:
    index: int
    x: int
    y: int
    width: int
    height: int
    scale: float
    size: int
    relative_offset: int
    offset: int
    cumulative_offset: int


@dataclass(frozen=True)
class AssociatedImage:
    name: str
    width: int
    height: int
    size: int
    offset: int
    section_offset: int


@dataclass
class NativeLevel:
    index: int
    scale: float
    downsample: float
    width: int
    height: int
    entries: list[TileEntry]
    tile_size: int
    lookup: dict[tuple[int, int], TileEntry] = field(default_factory=dict)
    bins: dict[tuple[int, int], list[TileEntry]] = field(default_factory=dict)

    @property
    def dimensions(self) -> tuple[int, int]:
        return self.width, self.height


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise KFBReadError(message)


def _decoded_rgb(data: bytes, expected: tuple[int, int], context: str) -> np.ndarray:
    _require(not ImageFile.LOAD_TRUNCATED_IMAGES,
             "Pillow LOAD_TRUNCATED_IMAGES must be False for strict conversion")
    _require(data[:2] == b"\xff\xd8", f"{context}: missing JPEG SOI")
    _require(data[-2:] == b"\xff\xd9", f"{context}: missing JPEG EOI")
    try:
        with Image.open(io.BytesIO(data)) as im:
            _require(im.format == "JPEG", f"{context}: codec is {im.format}, expected JPEG")
            _require(im.size == expected,
                     f"{context}: decoded dimensions {im.size}, index dimensions {expected}; resizing forbidden")
            _require(im.mode == "RGB", f"{context}: expected RGB JPEG, got {im.mode}; no implicit color conversion")
            im.load()
            return np.asarray(im, dtype=np.uint8).copy()
    except KFBReadError:
        raise
    except Exception as exc:
        raise KFBReadError(f"{context}: JPEG decode failed: {exc}") from exc


class StrictKFB:
    """Read pixels and metadata with exact index addressing and explicit errors."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(Path(path))
        self._fh = open(self.path, "rb")
        self.file_size = os.fstat(self._fh.fileno()).st_size
        self._coverage: dict[int, dict[str, Any]] = {}
        try:
            self._parse()
        except Exception:
            self.close()
            raise

    def _read(self, offset: int, size: int, context: str) -> bytes:
        _require(offset >= 0 and size > 0 and offset + size <= self.file_size,
                 f"{context}: byte range {offset}:{offset + size} outside file {self.file_size}")
        self._fh.seek(offset)
        data = self._fh.read(size)
        _require(len(data) == size, f"{context}: incomplete read")
        return data

    def _parse_assoc(self, name: str, section_offset: int, sec_type: int) -> AssociatedImage:
        b = self._read(section_offset, 52, f"{name} section")
        _require(b[:4] == bytes((0xF1, sec_type, 0xEE, 0xEE)) and
                 b[48:52] == bytes((0xFF, sec_type, 0xEE, 0xEE)),
                 f"{name}: unsupported associated-image section layout")
        height, width = struct.unpack_from("<II", b, 8)
        size = struct.unpack_from("<I", b, 20)[0]
        _require(0 < size <= MAX_JPEG_BYTES,
                 f"{name}: associated JPEG exceeds the bounded parser allocation")
        rel = struct.unpack_from("<I", b, 24)[0]
        # Both reviewed readers use a 52-byte prefix; the Python implementation
        # reads the section's explicit relative pointer instead of assuming 52.
        _require(rel == 52, f"{name}: unexpected JPEG offset field {rel}, expected 52")
        entry = AssociatedImage(name, width, height, size, section_offset + rel, section_offset)
        _require(width > 0 and height > 0, f"{name}: invalid dimensions")
        data = self.jpeg_bytes(entry)
        _require(data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9",
                 f"{name}: invalid JPEG bounds")
        return entry

    def _parse(self) -> None:
        b = self._read(0, 96, "KFB header")
        _require(b[:4] == b"\xf1\x01\xee\xee" and b[92:96] == b"\xff\x01\xee\xee",
                 "Unsupported KFB header section layout")
        p = b[4:92]
        magic = p[:4].rstrip(b"\0").decode("ascii", errors="strict")
        if magic == "KFBF":
            raise KFBReadError("KFBF requires the independent-channel reader; do not use the RGB KFB reader", "KFBF_WRONG_READER")
        _require(magic == "KFB", f"Invalid KFB magic {magic!r}")
        if p[8:12] != struct.pack("<f", 1.6):
            raise KFBReadError("Only the reviewed KFB 1.6 JPEG layout is supported; another version must not be guessed", "KFB_VERSION_UNSUPPORTED")
        compression = p[28:32].rstrip(b"\0").decode("ascii", errors="strict")
        _require(compression in ("JPG", "JPEG"), f"Unsupported KFB compression {compression!r}")
        self.header = {
            "magic": magic,
            "version": struct.unpack_from("<f", p, 8)[0],
            "tile_count": struct.unpack_from("<I", p, 12)[0],
            "height": struct.unpack_from("<I", p, 16)[0],
            "width": struct.unpack_from("<I", p, 20)[0],
            "objective_power": struct.unpack_from("<I", p, 24)[0],
            "compression": compression,
            "spend_time": struct.unpack_from("<I", p, 36)[0],
            "scan_time": struct.unpack_from("<q", p, 40)[0],
            "macro_section_offset": struct.unpack_from("<I", p, 48)[0],
            "label_section_offset": struct.unpack_from("<I", p, 52)[0],
            "preview_section_offset": struct.unpack_from("<Q", p, 56)[0],
            "tile_index_start": struct.unpack_from("<Q", p, 64)[0],
            "mpp": struct.unpack_from("<f", p, 72)[0],
            "tile_size": struct.unpack_from("<I", p, 84)[0],
            "header_bytes_sha256": hashlib.sha256(b).hexdigest(),
            "header_bytes_hex": b.hex(),
        }
        self.dimensions = (self.header["width"], self.header["height"])
        self.mpp = self.header["mpp"]
        self.objective_power = self.header["objective_power"]
        self.tile_size = self.header["tile_size"]
        _require(all(v > 0 for v in self.dimensions) and 0 < self.tile_size <= MAX_TILE_EDGE and
                 self.objective_power > 0, "Invalid header dimensions/tile size/objective")
        _require(math.isfinite(self.mpp) and self.mpp > 0, "Invalid native MPP; inference forbidden")
        self.associated: dict[str, AssociatedImage] = {}
        for name, field_name, sec_type in (
            ("macro", "macro_section_offset", 2),
            ("label", "label_section_offset", 3),
            ("thumbnail", "preview_section_offset", 2),
        ):
            offset = self.header[field_name]
            if offset:
                self.associated[name] = self._parse_assoc(name, offset, sec_type)
        index_start = self.header["tile_index_start"]
        count = self.header["tile_count"]
        _require(0 < count <= MAX_TILE_COUNT, "Invalid/excessive tile index count; bounded parser refuses unsafe allocation")
        _require(index_start >= 96 and index_start <= self.file_size and count <= (self.file_size-index_start)//64,
                 "Tile index count exceeds the available file bytes")
        raw = self._read(index_start, count * 64, "KFB tile index")
        self.index_bytes_sha256 = hashlib.sha256(raw).hexdigest()
        initial = []
        for i in range(count):
            record = raw[i * 64:(i + 1) * 64]
            _require(record[:4] == b"\xf1\x04\xee\xee" and record[60:64] == b"\xff\x04\xee\xee",
                     f"Tile {i}: unsupported 64-byte record markers")
            x, y, width, height = struct.unpack_from("<IIII", record, 4)
            scale = struct.unpack_from("<f", record, 20)[0]
            size = struct.unpack_from("<I", record, 32)[0]
            relative_offset = struct.unpack_from("<q", record, 36)[0]
            offset = index_start + relative_offset
            _require(width > 0 and height > 0 and width <= self.tile_size and height <= self.tile_size,
                     f"Tile {i}: invalid dimensions {width}x{height}")
            _require(math.isfinite(scale) and scale > 0, f"Tile {i}: invalid native scale {scale}")
            _require(0 <= offset and 0 < size <= MAX_JPEG_BYTES and offset + size <= self.file_size,
                     f"Tile {i}: invalid direct byte range {offset}:{offset + size}")
            initial.append((i, x, y, width, height, scale, size, relative_offset, offset))
        cumulative = initial[0][-1]
        self.entries = []
        for args in initial:
            entry = TileEntry(*args, cumulative)
            self.entries.append(entry)
            cumulative += entry.size
        scales = sorted({e.scale for e in self.entries}, reverse=True)
        _require(math.isclose(scales[0], self.objective_power, rel_tol=1e-6),
                 f"Highest tile scale {scales[0]} differs from header objective {self.objective_power}")
        self.levels = []
        for li, scale in enumerate(scales):
            ds = self.objective_power / scale
            width, height = max(1, int(self.dimensions[0] / ds)), max(1, int(self.dimensions[1] / ds))
            entries = [e for e in self.entries if e.scale == scale]
            level = NativeLevel(li, scale, ds, width, height, entries, self.tile_size)
            bins = defaultdict(list)
            for entry in entries:
                key = (entry.x, entry.y)
                _require(key not in level.lookup,
                         f"Level {li}: duplicate tile origin {key}; ambiguous pixel precedence")
                level.lookup[key] = entry
                for by in range(entry.y // self.tile_size, (entry.y + entry.height - 1) // self.tile_size + 1):
                    for bx in range(entry.x // self.tile_size, (entry.x + entry.width - 1) // self.tile_size + 1):
                        bins[(bx, by)].append(entry)
            level.bins = dict(bins)
            self.levels.append(level)
        self.level_dimensions = tuple(level.dimensions for level in self.levels)
        self.level_downsamples = tuple(level.downsample for level in self.levels)
        self.level_count = len(self.levels)

    @property
    def base_level(self):
        return self.levels[0]

    def jpeg_bytes(self, entry: TileEntry | AssociatedImage) -> bytes:
        return self._read(entry.offset, entry.size, f"JPEG {getattr(entry, 'index', getattr(entry, 'name', '?'))}")

    def decode_tile(self, entry: TileEntry) -> np.ndarray:
        return _decoded_rgb(self.jpeg_bytes(entry), (entry.width, entry.height), f"Tile {entry.index}")

    def decode_associated(self, name: str) -> np.ndarray:
        entry = self.associated[name]
        return _decoded_rgb(self.jpeg_bytes(entry), (entry.width, entry.height), f"Associated {name}")

    def jpeg_metadata(self, entry: TileEntry | AssociatedImage) -> dict[str, Any]:
        """Return unmodified embedded metadata, including actual JPEG ICC bytes.

        icc_profile/exif values are bytes and intentionally not JSON-serializable;
        call the metadata() report for hashes and explicitly save original blobs.
        """
        data = self.jpeg_bytes(entry)
        with Image.open(io.BytesIO(data)) as im:
            return {"size": tuple(im.size), "mode": im.mode, "info": dict(im.info)}

    def coverage_report(self, level: NativeLevel | int) -> dict[str, Any]:
        """Compute exact clipped rectangle union and missing/overlapping coverage."""
        if isinstance(level, int):
            level = self.levels[level]
        if level.index in self._coverage:
            return self._coverage[level.index]
        events: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        clipped_area = 0
        overhang = []
        outside = []
        off_grid = []
        for entry in level.entries:
            x0, y0 = max(0, entry.x), max(0, entry.y)
            x1, y1 = min(level.width, entry.x + entry.width), min(level.height, entry.y + entry.height)
            if entry.x % self.tile_size or entry.y % self.tile_size:
                off_grid.append(entry.index)
            if entry.x + entry.width > level.width or entry.y + entry.height > level.height:
                overhang.append(entry.index)
            if x1 <= x0 or y1 <= y0:
                outside.append(entry.index)
            if x1 > x0 and y1 > y0:
                clipped_area += (x1 - x0) * (y1 - y0)
                events[y0].append((1, entry.index, x0))
                events[y1].append((-1, entry.index, x0))
        active: dict[int, tuple[int, int]] = {}
        union_area = 0
        prev_y = 0
        entries_by_index = {e.index: e for e in level.entries}
        for y in sorted(events):
            intervals = sorted(active.values())
            union_length = 0
            end = 0
            for start, stop in intervals:
                if stop > end:
                    union_length += stop - max(start, end)
                    end = stop
            union_area += union_length * (y - prev_y)
            for delta, idx, x0 in events[y]:
                if delta > 0:
                    entry = entries_by_index[idx]
                    active[idx] = (x0, min(level.width, entry.x + entry.width))
                else:
                    active.pop(idx, None)
            prev_y = y
        nx, ny = math.ceil(level.width/self.tile_size), math.ceil(level.height/self.tile_size)
        present_bins = sum(0 <= bx < nx and 0 <= by < ny for bx, by in level.bins)
        missing_grid_count = nx * ny - present_bins
        missing_grid_samples = []
        if missing_grid_count:
            for by in range(ny):
                for bx in range(nx):
                    if (bx, by) not in level.bins:
                        missing_grid_samples.append([bx*self.tile_size, by*self.tile_size])
                        if len(missing_grid_samples) >= min(64, missing_grid_count):
                            break
                if len(missing_grid_samples) >= min(64, missing_grid_count):
                    break
        logical_pixels = level.width * level.height
        report = {
            "level": level.index, "scale": level.scale,
            "dimensions": [level.width, level.height], "entry_count": len(level.entries),
            "covered_pixels": union_area, "missing_pixels": logical_pixels - union_area,
            "overlapping_pixels_counted_with_multiplicity": clipped_area - union_area,
            "missing_grid_count": missing_grid_count,
            "missing_grid_samples": missing_grid_samples,
            "off_grid_entry_count": len(off_grid), "off_grid_samples": off_grid[:64],
            "edge_overhang_entry_count": len(overhang), "edge_overhang_samples": overhang[:64],
            "fully_outside_logical_entry_count": len(outside), "fully_outside_logical_samples": outside[:64],
            "edge_padding_policy": "Source JPEG overhang is exposed by read_tile; converter reports logical-bound clipping; original source retains overhang bytes",
        }
        self._coverage[level.index] = report
        return report

    def read_tile(self, level: NativeLevel | int, x: int, y: int,
                  tile_size: int | None = None, background: tuple[int, int, int] = (0, 0, 0)) -> np.ndarray:
        """Assemble a padded output tile; no resampling or inferred source pixels.

        Sparse source areas are explicit background RGB (default black matches
        converting the reference reader's transparent RGBA result to RGB).
        The conversion report must document its chosen sparse-background policy.
        Native encoded edge pixels beyond the logical image dimensions are kept
        in TIFF tile padding whenever they fit that output tile, never cropped.
        """
        if isinstance(level, int):
            level = self.levels[level]
        t = tile_size or self.tile_size
        _require(t > 0 and x >= 0 and y >= 0, "Invalid output tile coordinates")
        report = self.coverage_report(level)
        _require(report["overlapping_pixels_counted_with_multiplicity"] == 0,
                 f"Level {level.index}: overlapping source tiles require independent precedence validation")
        out = np.empty((t, t, 3), dtype=np.uint8)
        out[:] = background
        candidates: dict[int, TileEntry] = {}
        for by in range(y // self.tile_size, (y + t - 1) // self.tile_size + 1):
            for bx in range(x // self.tile_size, (x + t - 1) // self.tile_size + 1):
                for entry in level.bins.get((bx, by), []):
                    candidates[entry.index] = entry
        for entry in candidates.values():
            x0, y0 = max(x, entry.x), max(y, entry.y)
            x1 = min(x + t, entry.x + entry.width)
            y1 = min(y + t, entry.y + entry.height)
            if x1 > x0 and y1 > y0:
                pixels = self.decode_tile(entry)
                out[y0 - y:y1 - y, x0 - x:x1 - x] = pixels[
                    y0 - entry.y:y1 - entry.y, x0 - entry.x:x1 - entry.x]
        return out

    def offset_audit(self, check_jpeg_bounds: bool = False) -> dict[str, Any]:
        differences = [e for e in self.entries if e.offset != e.cumulative_offset]
        report: dict[str, Any] = {
            "direct_vs_cumulative_difference_count": len(differences),
            "direct_vs_cumulative_difference_samples": [asdict(e) for e in differences[:16]],
            "direct_offset_rule": "tile_index_start + signed_int64_at_record_byte_36",
            "cumulative_offset_rule": "first_record_direct_offset + sum_prior_record_sizes",
            "jpeg_bounds_checked": bool(check_jpeg_bounds),
        }
        if check_jpeg_bounds:
            for entry in self.entries:
                soi = self._read(entry.offset, 2, f"Tile {entry.index} SOI")
                eoi = self._read(entry.offset + entry.size - 2, 2, f"Tile {entry.index} EOI")
                _require(soi == b"\xff\xd8" and eoi == b"\xff\xd9",
                         f"Tile {entry.index}: invalid direct JPEG boundaries")
            report["jpeg_bounds_passed_count"] = len(self.entries)
        return report

    def metadata(self) -> dict[str, Any]:
        assoc = {}
        for name, entry in self.associated.items():
            m = self.jpeg_metadata(entry)
            icc = m["info"].get("icc_profile")
            assoc[name] = {**asdict(entry), "jpeg_sha256": hashlib.sha256(self.jpeg_bytes(entry)).hexdigest(),
                           "icc_present": bool(icc),
                           "icc_sha256": hashlib.sha256(icc).hexdigest() if icc else None}
        return {
            "source": self.path, "source_file_bytes": self.file_size,
            "header": self.header, "parser_sources": PARSER_SOURCES,
            "parser_limitations": "Exact KFB 1.6 JPEG layout only; no vendor guarantee; native logical dimensions floor(base/downsample).",
            "pixel_decode_policy": "Pillow JPEG decode to uint8 RGB; no ICC transform, resize, self-healing or truncated-image recovery",
            "logical_bounds_policy": "Declare native floor(base/downsample) logical dimensions; retain native overhang in encoded tile padding; any wholly external tile origins require separate preservation and must not be silently omitted",
            "tile_index_bytes_sha256": self.index_bytes_sha256,
            "levels": [self.coverage_report(level) for level in self.levels],
            "associated_images": assoc,
            "offset_audit": self.offset_audit(),
            "icc_policy": "ICC must be checked in source tile JPEG metadata; source reader API returning None is not evidence of absence",
        }

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "StrictKFB":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
