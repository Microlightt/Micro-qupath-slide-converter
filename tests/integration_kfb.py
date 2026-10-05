#!/usr/bin/env python3
"""Actual synthetic KFB -> SVS -> QuPath integration; no patient data or mocks."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from integration import digest, ensure, execute, save, SKILL
from test_kfb import make_kfb


def run(runtime_path, root, tiny_tail_case=False):
    ensure(not root.exists(), 'Work directory must be new')
    runtime = json.loads(runtime_path.read_text())
    root.mkdir(parents=True)
    source = make_kfb(root/'synthetic.kfb', dimensions=(663, 504) if tiny_tail_case else (513, 385), tile_size=128,
                      downsamples=(1,2,4,8,16,32,64,128,256,512,1024),
                      missing=True, overhang=True)
    before = digest(source)
    command = [runtime['python'], SKILL/'scripts/convert_slide.py', '--input', source,
               '--output-dir', root/'converted', '--runtime', runtime_path,
               '--image-type', 'brightfield', '--workers', '2', '--heap-gib', '2',
               '--roi-size', '64', '--min-free-gib', '0.25']
    rejected = subprocess.run([str(v) for v in command], capture_output=True, text=True)
    ensure(rejected.returncode == 2 and 'KFB_MISSING_PIXELS' in rejected.stdout,
           'Sparse input must require an explicit policy')
    refusal = json.loads(rejected.stdout)
    ensure(refusal['kfb']['missing_pixels_total'] == 128*128 and
           refusal['kfb']['coverage'][0]['missing_rectangles'], 'Refusal must report actual coverage')
    ensure(not (root/'converted').exists(), 'Rejected policy created an output')
    command += ['--missing-pixels', 'black']
    elapsed = execute(command, root/'conversion.log')
    work = root/'converted'
    status = json.loads((work/'status.json').read_text())
    ensure(status['status'] == ('QUPATH_VERIFIED_WITH_READER_LIMITATION' if tiny_tail_case else 'QUPATH_VERIFIED'), 'Actual QuPath verification with accurate limitation status required')
    qc = json.loads((work/'pixel_validation.json').read_text())
    ensure(qc['status'] == 'PASS' and qc['mismatched_rgb_values_all_retained_levels'] == 0,
           'Every retained native pixel must match')
    ensure(qc['missing_pixels_total'] == 128*128, 'Missing pixels changed')
    meta = json.loads((work/'source.json').read_text())
    for level in meta['kfb']['coverage']:
        ensure(sum(r['width']*r['height'] for r in level['missing_rectangles']) == level['missing_pixels'],
               'Missing-region positions must account for every absent pixel')
    ensure(len(meta['kfb']['omitted_redundant_levels']) == 1, 'Duplicate 1x1 level must be recorded')
    qp = json.loads((work/'qupath_validation.json').read_text())
    ensure(qp['status'] == 'PASS' and qp['checks']['source_roi_pixels'] == 'PASS', 'QuPath source ROIs')
    if tiny_tail_case:
        ensure(qp['checks']['all_pyramid_levels'] == 'PASS_WITH_READER_LIMITATION' and
               qp['pyramid_structure']['unavailable_in_qupath'] == [dict(width=1,height=1)] and
               len(qp['pyramid_reads']) == len(qc['levels'])-1, 'Precisely documented 1x1 reader limit required')
    else:
        ensure(qp['checks']['all_pyramid_levels'] == 'PASS', 'Ordinary pyramid must pass every level')
    output = Path(status['output'])
    snapshot = (digest(output), output.stat().st_mtime_ns, digest(work/'status.json'))
    execute(command+['--resume'], root/'resume.log')
    ensure(snapshot == (digest(output), output.stat().st_mtime_ns, digest(work/'status.json')),
           'Completed resume must not rewrite output or record')
    ensure(digest(source) == before, 'Source changed')
    blocked = root/'synthetic-fluorescence.kfb'
    blocked.write_bytes(b'\xf1\x01\xee\xeeKFBF'+bytes(88))
    probe = subprocess.run([runtime['python'], str(SKILL/'scripts/slide_probe.py'),
                            '--input', str(blocked), '--runtime', str(runtime_path)], capture_output=True, text=True)
    ensure(probe.returncode == 2 and 'KFBF' in probe.stdout, 'KFBF must not enter RGB route')
    report = dict(status='PASS', source_type='synthetic_binary_KFB_1.6_RGB',
                  explicit_missing_policy='black', rejected_default='PASS', missing_pixels=128*128,
                  retained_levels=len(qc['levels']), native_pixel_validation='PASS_ALL_RETAINED_LEVELS',
                  tiny_tail_case=tiny_tail_case,qupath_readable_levels=len(qp['pyramid_reads']),
                  qupath_version=qp.get('qupath_version'), qupath_report=str(work/'qupath_validation.json'),
                  completed_resume='PASS_UNCHANGED', kfbf_guard='PASS', elapsed_seconds=elapsed,
                  source_sha256=before, output_sha256=snapshot[0])
    save(root/'integration_report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--tiny-tail-case', action='store_true', help='Reproduce QuPath 0.6.0 terminal 1x1 rounding limitation')
    args = parser.parse_args()
    print(json.dumps(run(Path(args.runtime).resolve(), Path(args.workdir).absolute(),args.tiny_tail_case)))
