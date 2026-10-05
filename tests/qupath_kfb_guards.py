#!/usr/bin/env python3
"""Exercise the narrow QuPath 0.6.0 KFB terminal-1x1 exception against real QuPath.

The converted directory must contain a small already-verified synthetic KFB case
that actually triggers the documented terminal-level reader limitation. A positive
control and every negative case invoke the installed QuPath application; there are
no mock readers or fabricated version results. Evidence stays outside the Skill.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import traceback

VALIDATOR = Path(__file__).resolve().parents[1] / 'scripts/validate_qupath.groovy'
LIMITATION = 'QUPATH_060_TERMINAL_1X1'


def ensure(condition, message):
    if not condition:
        raise AssertionError(message)


def checksum(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


def save(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    temporary.replace(path)


def invoke(launcher, image, expected, folder, timeout):
    folder.mkdir(exist_ok=False)
    supplied = folder/'expected.json'
    result_file = folder/'qupath.json'
    save(supplied, expected)
    command = [launcher, 'script', '-i', str(image), '-a', str(result_file),
               '-a', str(supplied), str(VALIDATOR)]
    started = time.monotonic()
    with (folder/'qupath.log').open('w', encoding='utf-8') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
    ensure(result_file.is_file(), 'Actual QuPath produced no result JSON; inspect '+str(folder/'qupath.log'))
    observed = json.loads(result_file.read_text(encoding='utf-8'))
    return result.returncode, observed, time.monotonic()-started


def negative_variants(expected):
    cases = []
    changed = copy.deepcopy(expected)
    changed.pop('kfb', None)
    cases.append(('missing-kfb-flag', changed, 'outside the documented 0.6.0 terminal-1x1 limitation'))
    changed = copy.deepcopy(expected)
    middle = max(1, min(len(changed['pyramid_dimensions'])-2, len(changed['pyramid_dimensions'])//2))
    changed['pyramid_dimensions'][middle]['width'] += 1
    cases.append(('changed-middle-width', changed, 'pyramid raster dimensions differ'))
    changed = copy.deepcopy(expected)
    changed['pyramid_dimensions'][-1] = {'width':2, 'height':2}
    cases.append(('changed-final-shape', changed, 'outside the documented 0.6.0 terminal-1x1 limitation'))
    changed = copy.deepcopy(expected)
    changed['pyramid_dimensions'].append({'width':1, 'height':1})
    changed['expected_pyramid_levels'] = len(changed['pyramid_dimensions'])
    cases.append(('second-missing-terminal-level', changed, 'outside the documented 0.6.0 terminal-1x1 limitation'))
    return cases


def run(args):
    converted = Path(args.converted_dir).expanduser().resolve()
    work = Path(args.workdir).expanduser().absolute()
    ensure(not work.exists() and not work.is_symlink(), 'Use a new work directory; existing evidence is never overwritten')
    ensure(work != converted and not work.is_relative_to(converted), 'Evidence must be outside the converted directory')
    state_file, expected_file = converted/'status.json', converted/'qupath_expected.json'
    state = json.loads(state_file.read_text(encoding='utf-8'))
    ensure(state.get('status')=='QUPATH_VERIFIED_WITH_READER_LIMITATION', 'Fixture must already have the explicit reader-limitation status')
    image = Path(state['output']).expanduser().resolve()
    ensure(image.is_file(), 'Converted fixture output is missing')
    expected = json.loads(expected_file.read_text(encoding='utf-8'))
    ensure(expected.get('kfb'), 'Fixture must have original KFB metadata')
    dimensions = expected.get('pyramid_dimensions', [])
    ensure(len(dimensions) >= 3 and len(dimensions)==expected.get('expected_pyramid_levels'), 'Fixture needs a consistent native pyramid description')
    ensure(dimensions[-1]=={'width':1,'height':1} and dimensions[-2] in ({'width':2,'height':1},{'width':1,'height':2}), 'Fixture must have the specific 2x1/1x2 and 1x1 tail')
    ensure(dimensions[0]['width']*dimensions[0]['height'] <= 4096*4096, 'This test runner is for small synthetic fixtures, not large real slides')
    runtime_path = Path(args.runtime).expanduser().resolve()
    runtime = json.loads(runtime_path.read_text(encoding='utf-8'))
    launcher = runtime.get('qupath')
    ensure(launcher and os.access(launcher,os.X_OK), 'An actual executable QuPath launcher is required')
    ensure(VALIDATOR.is_file(), 'The bundled QuPath validator is missing')
    before = {str(path):checksum(path) for path in (image, state_file, expected_file)}
    work.mkdir(parents=True, exist_ok=False)
    report = {'status':'RUNNING', 'test_type':'actual_qupath_kfb_negative_guards',
              'runtime_config':str(runtime_path), 'qupath_launch_method':runtime.get('qupath_launch_method'),
              'validator_sha256':checksum(VALIDATOR), 'fixture':str(converted), 'cases':[],
              'limitation':'Only the supplied synthetic terminal-level layout and the actual observed QuPath version are tested.'}
    save(work/'guard_report.json', report)
    try:
        code, control, elapsed = invoke(launcher,image,expected,work/'positive-control',args.timeout)
        ensure(code==0 and control.get('status')=='PASS', 'Positive control did not pass; negative results would be uninterpretable')
        ensure(control.get('checks',{}).get('all_pyramid_levels')=='PASS_WITH_READER_LIMITATION', 'Positive control did not exercise the reader exception')
        ensure(control.get('checks',{}).get('source_roi_pixels')=='PASS', 'Positive control lacks actual source-pixel checks')
        limitations = control.get('reader_limitations', [])
        ensure(any(item.get('code')==LIMITATION for item in limitations), 'Positive control did not record the exact expected reader limitation')
        report['positive_control'] = {'status':'PASS','returncode':code,'elapsed_seconds':elapsed,
                                      'qupath_version':control.get('qupath_version'),
                                      'reader_limitations':limitations}
        save(work/'guard_report.json', report)
        for name, modified, expected_error in negative_variants(expected):
            print('START '+name,flush=True)
            try:
                code, result, elapsed = invoke(launcher,image,modified,work/name,args.timeout)
                ensure(code != 0, 'QuPath must fail the altered expectation with a nonzero exit')
                ensure(result.get('status')=='FAIL', 'QuPath must record FAIL for the altered expectation')
                ensure(result.get('checks',{}).get('metadata')=='PASS', 'Failure occurred before successful image metadata reading')
                ensure(result.get('checks',{}).get('full_resolution_all_channels')=='PASS', 'Failure occurred before actual full-resolution reading')
                ensure(expected_error in result.get('error',''), 'Failure came from an unrelated check: '+str(result.get('error')))
                case={'name':name,'status':'PASS','qupath_exit':code,'observed_status':result['status'],
                      'qupath_version':result.get('qupath_version'),'error':result.get('error'),'elapsed_seconds':elapsed}
            except Exception as exc:
                case={'name':name,'status':'FAIL','error':str(exc),'traceback':traceback.format_exc()}
            report['cases'].append(case)
            save(work/'guard_report.json',report)
            print(case['status']+' '+name,flush=True)
        after = {str(path):checksum(path) for path in (image, state_file, expected_file)}
        ensure(before==after,'Guard test changed the fixture image or its original verification records')
        report['fixture_unchanged'] = True
        report['status'] = 'PASS' if len(report['cases'])==4 and all(item['status']=='PASS' for item in report['cases']) else 'FAIL'
    except Exception as exc:
        report.update(status='FAIL',error=str(exc),traceback=traceback.format_exc())
    save(work/'guard_report.json',report)
    print(json.dumps({'status':report['status'],'report':str(work/'guard_report.json')}),flush=True)
    return 0 if report['status']=='PASS' else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',required=True)
    parser.add_argument('--converted-dir',required=True)
    parser.add_argument('--workdir',required=True)
    parser.add_argument('--timeout',type=int,default=180)
    args=parser.parse_args()
    ensure(args.timeout>0,'Timeout must be positive')
    return run(args)


if __name__=='__main__':
    raise SystemExit(main())
