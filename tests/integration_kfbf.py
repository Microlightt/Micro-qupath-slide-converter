#!/usr/bin/env python3
"""Actual KFBF binary -> Zarr -> OME-TIFF -> QuPath integration, synthetic inputs.

The fixtures are synthetic binary KFBF2.1, not independent vendor specimens.
Source values are compared with separately retained JPEG-decoded fixture planes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

import numpy as np
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kfbf_reader import make_kfbf

SKILL = Path(__file__).resolve().parents[1]


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024**2),b''):value.update(chunk)
    return value.hexdigest()


def save(path,value):
    path=Path(path);tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def run(command,log,expected=0):
    started=time.monotonic()
    with Path(log).open('w') as stream:
        result=subprocess.run([str(v) for v in command],stdout=stream,stderr=subprocess.STDOUT,timeout=1800)
    if result.returncode!=expected:raise AssertionError('Unexpected exit %s; see %s'%(result.returncode,log))
    return time.monotonic()-started


def case(root,runtime,runtime_path,count,sparse=False):
    folder=root/('%dc%s'%(count,'-sparse' if sparse else ''));folder.mkdir()
    source=folder/'input.kfbf'
    fixture=make_kfbf(source,channels=count,width=1031,height=777,tile_size=256,
                     missing={(0,1,256,0)} if sparse else None,
                     missing_spatial={(0,768,512)} if sparse else None)
    before_source=digest(source)
    output=folder/'converted'
    command=[runtime['python'],SKILL/'scripts/convert_slide.py','--input',source,
             '--output-dir',output,'--runtime',runtime_path,'--image-type','fluorescence',
             '--workers','2','--heap-gib','2','--min-free-gib','0.25','--roi-size','64']
    if sparse:
        run(command,folder/'refused.log',expected=2)
        if output.exists():raise AssertionError('Default sparse policy created an output directory')
        refusal=[json.loads(v) for v in (folder/'refused.log').read_text().splitlines() if v.startswith('{')][-1]
        if refusal.get('status')!='KFBF_MISSING_PIXELS':raise AssertionError('Default refusal was not missing-pixel policy')
        command+=['--missing-pixels','black']
    elapsed=run(command,folder/'conversion.log')
    status_path=output/'status.json'
    status=json.loads(status_path.read_text())
    if status['status']!='QUPATH_VERIFIED':raise AssertionError('QuPath did not verify completed KFBF output')
    final=Path(status['output'])
    expected=np.stack([fixture['planes'][(0,c)] for c in range(count)])
    with tifffile.TiffFile(final) as image:
        got=image.series[0].asarray()
        if count==1:got=got.reshape(1,777,1031)
        np.testing.assert_array_equal(got,expected)
        if got.dtype!=np.uint8:raise AssertionError('KFBF base dtype changed')
        if len(image.series[0].levels)<2:raise AssertionError('Missing generated display pyramid')
    qp=json.loads((output/'qupath_validation.json').read_text())
    if qp['metadata']['C']!=count or qp['metadata']['rgb']:raise AssertionError('Independent channels were lost')
    if qp['checks']['source_roi_pixels']!='PASS':raise AssertionError('QuPath source ROI check missing')
    if len(qp['roi_checks'])!=12*count:raise AssertionError('Not all expected channel ROIs checked')
    before={str(p):[digest(p),p.stat().st_mtime_ns] for p in [final,status_path]}
    run(command+['--resume'],folder/'resume.log')
    after={str(p):[digest(p),p.stat().st_mtime_ns] for p in [final,status_path]}
    if before!=after:raise AssertionError('Completed resume changed output or status')
    if digest(source)!=before_source:raise AssertionError('Synthetic source changed')
    return dict(status='PASS',channels=count,sparse=sparse,source_sha256=before_source,
                output_sha256=digest(final),base_values_compared=int(expected.size),qupath_version=qp['qupath_version'],
                qupath_source_roi_checks=len(qp['roi_checks']),resume='unchanged',elapsed_seconds=elapsed,output=str(final))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',required=True);parser.add_argument('--workdir',required=True)
    args=parser.parse_args();root=Path(args.workdir).absolute()
    if root.exists():raise ValueError('Use a new integration evidence directory')
    runtime_path=Path(args.runtime).resolve();runtime=json.loads(runtime_path.read_text());root.mkdir(parents=True)
    report=dict(status='RUNNING',scope='synthetic_binary_KFBF2.1_actual_conversion_and_QuPath',cases=[])
    save(root/'integration_report.json',report)
    for count,sparse in [(1,False),(3,False),(6,False),(8,False),(3,True)]:
        try:result=case(root,runtime,runtime_path,count,sparse)
        except Exception as exc:result=dict(status='FAIL',channels=count,sparse=sparse,error=str(exc),traceback=traceback.format_exc())
        report['cases'].append(result);save(root/'integration_report.json',report)
        print(json.dumps(result,ensure_ascii=False),flush=True)
    report['status']='PASS' if all(v['status']=='PASS' for v in report['cases']) else 'FAIL'
    save(root/'integration_report.json',report)
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
