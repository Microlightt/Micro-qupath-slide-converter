#!/usr/bin/env python3
"""Run the actual conversion pipeline and QuPath on public synthetic fixtures.

Run with the prepared runtime's Python:
  python tests/integration.py --runtime /path/to/runtime.json --workdir /new/test-dir

This requires the real converter backends and QuPath. It never substitutes mocks
or silently skips a missing dependency. Evidence is kept outside the Skill.
Synthetic tests establish these layouts; they do not prove every vendor reader.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET


SKILL = Path(__file__).resolve().parents[1]
NS = {'o': 'http://www.openmicroscopy.org/Schemas/OME/2016-06'}
WIDTH, HEIGHT = 1027, 769
CASES = [
    ('fluorescence-1c-uint8', 1, 'uint8', False),
    ('fluorescence-3c-uint16', 3, 'uint16', False),
    ('fluorescence-6c-uint8', 6, 'uint8', False),
    ('fluorescence-8c-uint16', 8, 'uint16', False),
    ('brightfield-rgb-uint8', 3, 'uint8', True),
]


def ensure(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024**2), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def save(path, value):
    temporary = Path(str(path) + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def create_input(path, channels, dtype, rgb):
    import numpy as np
    import tifffile
    yy, xx = np.indices((HEIGHT, WIDTH), dtype=np.uint32)
    modulus = np.iinfo(dtype).max + 1
    planes = [((xx * (13 + 2*c) + yy * (17 + 3*c) +
                ((xx//23) ^ (yy//31))*19 + (c+1)*101) % modulus).astype(dtype)
              for c in range(channels)]
    pixels = np.stack(planes)
    names = ['Synthetic_%02d' % (c+1) for c in range(channels)]
    metadata = dict(axes='YXS' if rgb else 'CYX', Name='Synthetic test image',
                    PhysicalSizeX=0.325, PhysicalSizeY=0.325,
                    PhysicalSizeXUnit='µm', PhysicalSizeYUnit='µm',
                    Channel={'Name': ['RGB'] if rgb else names})
    if rgb:
        pixels = np.moveaxis(pixels, 0, -1)
    tifffile.imwrite(path, pixels, ome=True, photometric='rgb' if rgb else 'minisblack',
                     compression='lzw', tile=(256, 256), metadata=metadata)
    # Independently read the written source before invoking any converter.
    actual = tifffile.imread(path)
    expected_on_disk = pixels[0] if channels == 1 and not rgb else pixels
    ensure(actual.dtype == pixels.dtype, 'Synthetic source dtype mismatch')
    ensure(np.array_equal(actual, expected_on_disk), 'Synthetic source write/read mismatch')
    return pixels, names


def execute(command, log, timeout=900):
    started = time.monotonic()
    with Path(log).open('w', encoding='utf-8') as stream:
        result = subprocess.run([str(x) for x in command], stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout, check=False)
    ensure(result.returncode == 0, 'Native command failed with exit %s; inspect %s' % (result.returncode, log))
    return time.monotonic() - started


def validate_base(output, expected, names, rgb):
    import numpy as np
    import tifffile
    with tifffile.TiffFile(output) as image:
        actual = image.pages[0].asarray() if rgb else image.series[0].levels[0].asarray()
        wanted = expected[0] if not rgb and expected.shape[0] == 1 else expected
        ensure(actual.shape == wanted.shape, 'Full-resolution shape mismatch: %r != %r' % (actual.shape, wanted.shape))
        ensure(actual.dtype == wanted.dtype, 'Full-resolution dtype mismatch')
        ensure(np.array_equal(actual, wanted), 'At least one full-resolution pixel changed')
        if not rgb:
            ome = ET.fromstring(image.ome_metadata)
            pixels = ome.find('o:Image/o:Pixels', NS)
            output_names = [channel.get('Name') for channel in pixels.findall('o:Channel', NS)]
            ensure(output_names == names, 'Output channel names/order changed')
            ensure(len(image.series[0].levels) > 1, 'OME-TIFF output lacks a pyramid')
            levels = [list(level.shape) for level in image.series[0].levels]
        else:
            ensure(image.pages[0].description.startswith('Aperio'), 'SVS lacks Aperio-compatible metadata')
            tiled = [page for page in image.pages if page.is_tiled]
            ensure(len(tiled) > 1, 'SVS output lacks reduced tiled levels')
            levels = [list(page.shape) for page in tiled]
    return dict(status='PASS', comparison='every_full_resolution_pixel',
                pixel_values_compared=int(wanted.size), dtype=str(wanted.dtype),
                shape=list(wanted.shape), levels=levels)


def test_case(case, root, runtime_path, runtime):
    name, channels, dtype, rgb = case
    folder = root / name
    folder.mkdir()
    source = folder / 'input.ome.tiff'
    expected, names = create_input(source, channels, dtype, rgb)
    source_hash = digest(source)
    output_dir = folder / 'converted'
    command = [runtime['python'], SKILL / 'scripts' / 'convert_slide.py',
               '--input', source, '--output-dir', output_dir, '--runtime', runtime_path,
               '--image-type', 'brightfield' if rgb else 'fluorescence',
               '--workers', '2', '--heap-gib', '2', '--min-free-gib', '0.25', '--roi-size', '64']
    elapsed = execute(command, folder / 'conversion.log')
    status_path = output_dir / 'status.json'
    status = json.loads(status_path.read_text(encoding='utf-8'))
    ensure(status['status'] == 'QUPATH_VERIFIED', 'Pipeline did not reach actual QuPath verification')
    target = 'svs' if rgb else 'ome-tiff'
    ensure(status['target'] == target, 'Unexpected output route')
    final = Path(status['output'])
    comparison = validate_base(final, expected, names, rgb)
    qpath = output_dir / 'qupath_validation.json'
    qupath = json.loads(qpath.read_text(encoding='utf-8'))
    ensure(qupath.get('status') == 'PASS', 'QuPath native validation did not pass')
    ensure(len(qupath.get('pyramid_reads', [])) > 1, 'QuPath did not read multiple pyramid levels')
    ensure(qupath.get('checks', {}).get('source_roi_pixels') == 'PASS', 'QuPath source-pixel checks did not pass')
    for axis in ['physical_size_x', 'physical_size_y']:
        value = qupath.get('metadata', {}).get(axis)
        ensure(value is not None and math.isclose(value, 0.325, abs_tol=1e-6), 'QuPath calibration mismatch: ' + axis)
    before = dict(file_sha256=digest(final), file_mtime_ns=final.stat().st_mtime_ns,
                  status_sha256=digest(status_path), status_mtime_ns=status_path.stat().st_mtime_ns)
    ensure(before['file_sha256'] == status['sha256'], 'Recorded final checksum mismatch')
    resume_elapsed = execute(command + ['--resume'], folder / 'resume.log')
    after = dict(file_sha256=digest(final), file_mtime_ns=final.stat().st_mtime_ns,
                 status_sha256=digest(status_path), status_mtime_ns=status_path.stat().st_mtime_ns)
    ensure(before == after, 'Completed resume rewrote a verified output or run record')
    ensure(digest(source) == source_hash, 'Input fixture was modified')
    return dict(name=name, status='PASS', source=str(source), output=str(final),
                source_sha256=source_hash, output_sha256=after['file_sha256'],
                target=target, channels=channels, rgb=rgb, comparison=comparison,
                qupath_report=str(qpath), qupath_version=qupath.get('qupath_version'),
                qupath_server=qupath.get('server_class'),
                resume='PASS_UNCHANGED_OUTPUT_AND_RECORD', elapsed_seconds=elapsed,
                resume_elapsed_seconds=resume_elapsed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, help='Validated runtime JSON with an executable QuPath launcher')
    parser.add_argument('--workdir', required=True, help='New directory for synthetic fixtures and all test evidence')
    args = parser.parse_args()
    root = Path(args.workdir).expanduser().absolute()
    ensure(not root.exists() and not root.is_symlink(), 'Work directory must be new; existing evidence is never overwritten')
    runtime_path = Path(args.runtime).expanduser().resolve()
    runtime = json.loads(runtime_path.read_text(encoding='utf-8'))
    ensure(runtime.get('qupath') and os.access(runtime['qupath'], os.X_OK), 'Actual executable QuPath launcher is required')
    ensure(runtime.get('python') and Path(runtime['python']).is_file(), 'Runtime Python is missing')
    # Import native dependencies before any test claims can be recorded.
    import numpy as np
    import tifffile
    import zarr
    root.mkdir(parents=True, exist_ok=False)
    report = dict(status='RUNNING', test_type='actual_synthetic_pipeline_with_QuPath',
                  limitation='Synthetic layouts only; not proof of vendor-format coverage or clinical validity.',
                  versions=dict(python=sys.version, numpy=np.__version__, tifffile=tifffile.__version__, zarr=zarr.__version__),
                  runtime_config=str(runtime_path), qupath_launcher=runtime['qupath'],
                  qupath_launch_method=runtime.get('qupath_launch_method', 'not_declared_in_runtime'),
                  cases=[])
    save(root / 'integration_report.json', report)
    for case in CASES:
        print('START ' + case[0], flush=True)
        try:
            result = test_case(case, root, runtime_path, runtime)
        except Exception as exc:
            result = dict(name=case[0], status='FAIL', error=str(exc), traceback=traceback.format_exc())
        report['cases'].append(result)
        save(root / 'integration_report.json', report)
        print(result['status'] + ' ' + case[0], flush=True)
    report['status'] = 'PASS' if all(case['status'] == 'PASS' for case in report['cases']) else 'FAIL'
    save(root / 'integration_report.json', report)
    print(json.dumps(dict(status=report['status'], report=str(root / 'integration_report.json'))), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
