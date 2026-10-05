# Brightfield SVS output contract

## Scope

This independent writer creates an **Aperio-compatible SVS**, not a scanner-native
Aperio acquisition. The creator is explicitly recorded as this converter.
The caller must establish brightfield modality. Three independent fluorescence
channels must never be interpreted as RGB just because C equals three.

The first version accepts true RGB with three uint8 samples and Z=T=1. It accepts
bioformats2raw NGFF with explicit TCZYX axes and one selected series. It rejects
16-bit RGB, alpha samples, gray brightfield, ambiguous axes, stacks/time series,
and inconsistent dimensions. It never silently downcasts, crops, flattens, or
mixes fluorescence channels. If this contract is unsupported, explain why and
ask whether a separate, lossless OME-TIFF route is appropriate.

## Layout and preservation

- BigTIFF: tiled full-resolution IFD, stripped display thumbnail IFD, then tiled
  reduced-resolution IFDs, all with lossless Deflate compression.
- Each pyramid image is copied from the selected NGFF array tile by tile.
  Only tile padding outside image bounds is zero-filled; decoded in-bounds
  pixel values, dimensions, and RGB order are unchanged.
- The thumbnail is a bounded display image derived from a lower resolution.
  It is not used to verify full-resolution image content.
- Supplied ICC profiles are copied, not synthesized. An invalid supplied profile
  stops conversion. Lack of an accessible profile is a reportable limitation;
  raw pixel equality alone does not establish the same color-managed appearance.
- The baseline `MPP` comes only from known, equal X/Y physical sizes normalized
  to micrometres. No objective magnification or scanner identity is invented.
  If both sizes are missing, calibration stays unknown. A single known axis,
  invalid units, or unequal X/Y sizes stops this SVS route because Aperio MPP
  cannot express independent axis calibration to its common readers.
- Labels, macro images, annotations, and vendor-private metadata are not added
  to this SVS by the writer; the pipeline must preserve originals and record
  excluded material in a sidecar. It must not claim those items migrated.
- Deflate introduces no additional pixel loss. It cannot undo compression loss
  already present in the original scan.

The organization and identification follow the
[OpenSlide Aperio format documentation](https://openslide.org/formats/aperio/).
The `ImageDescription` begins with `Aperio Image` for compatibility with
[Bio-Formats SVSReader](https://github.com/ome/bioformats/blob/develop/components/formats-gpl/src/loci/formats/in/SVSReader.java).
The writer uses [tifffile](https://github.com/cgohlke/tifffile) for TIFF encoding;
these dependencies are separate projects with their own licenses.

## API

```python
from pathlib import Path
from svs_writer import write_svs, verify_svs

metadata = {
    'X': 1027, 'Y': 769, 'C': 3, 'Z': 1, 'T': 1,
    'dtype': 'uint8', 'rgb': True,
    'channels': ['Red', 'Green', 'Blue'],
    'physical_size_x': 0.325, 'physical_size_y': 0.325,
    'physical_size_unit': 'µm',
}
# Values above describe the synthetic test only, not defaults for user scans.
output = Path('/data/results/slide.pending.svs')
layout = write_svs('/data/work/slide.zarr', metadata, output)
checks = verify_svs('/data/work/slide.zarr', metadata, output)
```

The writer creates the file exclusively and refuses an existing destination,
including a symlink. A failed/interrupted write remains at its pending path;
the caller manages its recovery and must not announce it as a valid result.
`verify_svs` checks TIFF layout and bounded ROIs at corners, centre and tile
boundaries of every level. Its result is **not** a QuPath pass and is **not** a
comparison against the original scanner data. The pipeline must perform those
separate checks before finalization and success feedback.

## Reproducible tests

With the skill's scientific Python environment active:

```bash
python tests/test_svs.py -v
python tests/test_svs.py --fixture /tmp/svs-synthetic-fixture
```

The fixture has 1027×769 RGB pixels, four resolutions, unequal RGB patterns and
odd edge dimensions. The first command compares every pixel at every resolution
using an independent TIFF read. It also covers ICC bytes, no-overwrite behavior,
calibration failures/absence, contradictory dimensions, ambiguous axis order,
non-RGB three-channel data, unsupported bit depth, and Z/T rejection. The second
command leaves a synthetic SVS, reference PNG and metadata for a real QuPath
validation. This is synthetic coverage, not evidence of compatibility with every
scanner or every SVS-consuming application.

## Validation record

Validated on Linux x86_64 on 2026-10-05 with NumPy 2.5.3, Zarr 3.4.0 and
tifffile 2026.9.20: all ten synthetic tests passed. Actual QuPath 0.6.0
read the generated SVS through both its Bio-Formats and OpenSlide 4.0.0 readers.
Both readers reported RGB, the original 1027×769 dimensions, four readable
levels and 0.325 µm calibration. All 789,763 full-resolution RGB pixels matched
the independently saved PNG with zero differences.

These were background read tests, not GUI interaction or real-scanner coverage.
The existing native launcher failed independently of the image; tests invoked
QuPath main in its bundled JVM through JPype with separate temporary settings.
This distinction is retained in validation evidence; installation readiness
requires the user-facing launcher to pass its own checks. A readable filename, TIFF signature or generated thumbnail alone is
never sufficient to state that a whole slide can be analyzed in QuPath.
