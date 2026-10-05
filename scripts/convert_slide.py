#!/usr/bin/env python3
"""Convert one validated 2D slide and verify it with QuPath. Python >=3.10."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import tempfile
import xml.etree.ElementTree as ET
from slide_probe import SlideError, require, dump, resolve_input, runtime_config, inspect, route, read_rois, inventory, vendor_kind

VERSION='1.2.0'
NS={'o':'http://www.openmicroscopy.org/Schemas/OME/2016-06'}


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    return h.hexdigest()


def stage_fingerprint(meta, settings, cfg):
    # Input stat inventory is conservative change detection, not a source data checksum.
    code={p.name:file_hash(p) for p in Path(__file__).parent.iterdir() if p.suffix in {'.py','.groovy'}}
    spec=dict(source=meta['inventory'],metadata={k:v for k,v in meta.items() if k not in ['source_xml']},settings=settings,runtime=cfg,code=code)
    return hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()


def validate_output(src,work,resume=False):
    raw_work=Path(work).expanduser()
    require(not raw_work.is_symlink(),'UNSAFE_OUTPUT','Output directory cannot be a symlink')
    require(not raw_work.exists() or raw_work.is_dir(),'OUTPUT_EXISTS','Output path is an existing file')
    src,work=Path(src).resolve(),raw_work.resolve()
    require(work!=src and not src.is_relative_to(work),'UNSAFE_OUTPUT','Output cannot be the source or its ancestor')
    if src.suffix.lower()=='.mrxs':
        require(not work.is_relative_to(src.with_suffix('').resolve()),'UNSAFE_OUTPUT','Output cannot be inside the MRXS companion directory')
    require(not work.exists() or resume,'OUTPUT_EXISTS','Use a new output directory, or --resume for a matching recorded run')
    require(not work.is_symlink(),'UNSAFE_OUTPUT','Output directory cannot be a symlink')


def take(array,axes,row,rgb=False):
    choices=dict(T=0,Z=0,C=slice(None) if rgb else row['c'],S=slice(None),
                 Y=slice(row['y'],row['y']+row['height']),X=slice(row['x'],row['x']+row['width']))
    require(set(axes)<=set(choices),'OUTPUT_AXES','Unexpected axes '+axes)
    out=array[tuple(choices[c] for c in axes)]
    kept=''.join(a for a in axes if isinstance(choices[a],slice))
    if rgb and kept in ['CYX','SYX']:
        import numpy as np
        out=np.moveaxis(out,0,-1)
    return out


def check_zarr(path,meta,rows):
    import numpy as np
    import zarr
    group=zarr.open_group(str(path),mode='r')['0']
    scale=group.attrs['multiscales'][0]
    axes=[a['name'] if isinstance(a,dict) else a for a in scale['axes']]
    require(axes==list('tczyx'),'ZARR_AXES','Expected TCZYX NGFF axes')
    array=group[scale['datasets'][0]['path']]
    require(tuple(array.shape)==(1,meta['C'],1,meta['Y'],meta['X']),'ZARR_SHAPE','Dimensions differ from source')
    require(array.dtype==np.dtype(meta['dtype']),'ZARR_TYPE','Pixel type changed')
    for row,src in rows:
        dst=take(array,'TCZYX',row,meta['rgb'])
        require(np.array_equal(src,dst),'ZARR_PIXEL_MISMATCH',str(row))
    require((Path(path)/'OME/METADATA.ome.xml').is_file(),'NO_OME_METADATA','Missing source OME metadata')


def check_ome(path,meta,rows):
    import numpy as np
    import tifffile
    import zarr
    checks=[]
    with tifffile.TiffFile(path) as tf:
        require(tf.ome_metadata,'NOT_OME_TIFF','Missing OME metadata')
        source=ET.fromstring(meta['source_xml']).findall('o:Image',NS)[meta['series']].find('o:Pixels',NS)
        target=ET.fromstring(tf.ome_metadata).find('o:Image/o:Pixels',NS)
        for key in ['SizeX','SizeY','SizeC','SizeZ','SizeT','Type']:
            require(source.get(key)==target.get(key),'METADATA_MISMATCH',key)
        for key in ['PhysicalSizeX','PhysicalSizeY','PhysicalSizeZ','SignificantBits']:
            if source.get(key) is not None:
                require(target.get(key) is not None and float(source.get(key))==float(target.get(key)),'METADATA_MISMATCH',key)
        for key in ['PhysicalSizeXUnit','PhysicalSizeYUnit','PhysicalSizeZUnit']:
            if source.get(key) is not None:require(source.get(key)==target.get(key),'METADATA_MISMATCH',key)
        sc,tc=source.findall('o:Channel',NS),target.findall('o:Channel',NS)
        require(len(sc)==len(tc),'CHANNEL_MISMATCH','Channel count')
        for a,b in zip(sc,tc):
            for key in ['Name','Color','ExcitationWavelength','EmissionWavelength','ExcitationWavelengthUnit','EmissionWavelengthUnit','SamplesPerPixel']:
                if a.get(key) is not None:require(a.get(key)==b.get(key),'CHANNEL_MISMATCH',key)
        require(len(tf.series)==1,'SERIES_MISMATCH','Only one selected primary series expected')
        ts=tf.series[0]
        require(ts.dtype==np.dtype(meta['dtype']),'PIXEL_TYPE_CHANGED','Output dtype')
        store=ts.aszarr()
        try:
            ar=zarr.open(store,mode='r');ar=ar['0'] if isinstance(ar,zarr.Group) else ar
            for row,src in rows:
                require(np.array_equal(src,take(ar,ts.axes,row)),'TIFF_PIXEL_MISMATCH',str(row))
        finally:store.close()
        previous=None
        for n,level in enumerate(ts.levels):
            dims=dict(zip(level.axes,level.shape))
            require(dims.get('C',1)==meta['C'],'CHANNEL_MISMATCH','Pyramid channel count')
            require(int(level.pages[0].compression)==5,'COMPRESSION_CHANGED','Expected lossless LZW')
            if previous:require(dims['X']<previous['X'] and dims['Y']<previous['Y'],'BAD_PYRAMID','Nondecreasing levels')
            store=level.aszarr()
            try:
                ar=zarr.open(store,mode='r');ar=ar[str(n)] if isinstance(ar,zarr.Group) else ar
                for c in range(meta['C']):take(ar,level.axes,dict(c=c,x=0,y=0,width=16,height=16))
            finally:store.close()
            checks.append(dict(level=n,axes=level.axes,shape=list(level.shape)));previous=dims
        require(len(checks)>1 or max(meta['X'],meta['Y'])<=1024,'NO_PYRAMID','Large image needs pyramid')
        return dict(status='PASS',roi_count=len(rows),levels=checks,bigtiff=tf.is_bigtiff)


def preserve_partial(path):
    path=Path(path)
    if path.exists():path.rename(path.with_name(path.name+'.incomplete-'+str(time.time_ns())))


def check_filesystem(work):
    """Fail before conversion if this filesystem cannot publish without clobbering."""
    with tempfile.TemporaryDirectory(prefix='.filesystem-probe-',dir=work) as tmp:
        source=Path(tmp)/'source';target=Path(tmp)/'linked'
        source.write_bytes(b'probe')
        try:os.link(source,target)
        except OSError as exc:
            raise SlideError('UNSUPPORTED_FILESYSTEM','Working directory must support hard links (for example ext4 or APFS); choose another working filesystem') from exc


def run_command(name,args,work,env,state,reserve):
    state.update(stage=name,status='RUNNING')
    state.setdefault('commands',{})[name]=[str(v) for v in args]
    dump(work/'status.json',state)
    started=time.monotonic()
    with (work/(name+'.log')).open('a',encoding='utf-8') as out:
        proc=subprocess.Popen([str(v) for v in args],stdout=out,stderr=subprocess.STDOUT,env=env,start_new_session=True)
        try:
            while proc.poll() is None:
                require(shutil.disk_usage(work).free>reserve,'DISK_LOW','Stopped this stage because free space is below reserve')
                time.sleep(2)
            require(proc.returncode==0,'BACKEND_FAILED',name+' failed; inspect its log')
        except BaseException:
            if proc.poll() is None:
                os.killpg(proc.pid,signal.SIGTERM)
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
            raise
    state.setdefault('elapsed_seconds',{})[name]=time.monotonic()-started
    dump(work/'status.json',state)


def run(args):
    require(args.workers>0 and args.heap_gib>0 and args.roi_size>0 and args.min_free_gib>=0,'BAD_PARAMETERS','Invalid resources/ROI settings')
    src=resolve_input(args.input)
    work=Path(args.output_dir).expanduser().absolute()
    validate_output(src,work,args.resume)
    cfg=runtime_config(args.runtime)
    kind=vendor_kind(src)
    is_kfb=kind=='kfb'
    is_kfbf=kind=='kfbf'
    missing_policy=getattr(args,'missing_pixels','error')
    if is_kfb:
        cfg=dict(cfg,kfb_missing_pixels=missing_policy)
    if is_kfbf:
        cfg=dict(cfg,kfbf_missing_pixels=missing_policy)
    meta=inspect(src,cfg,args.series,min(args.heap_gib,4))
    target=route(meta,args.image_type)
    if is_kfb:
        from kfb_backend import validate_kfb
        validate_kfb(meta,missing_policy)
    if is_kfbf:
        from kfbf_backend import validate_kfbf
        validate_kfbf(meta,missing_policy)
    if target=='svs':
        from svs_writer import validate_metadata
        validate_metadata(meta)
    if not args.skip_qupath:
        require(cfg.get('qupath') and Path(cfg['qupath']).is_file(),'MISSING_QUPATH','Prepare QuPath through environment.py setup --with-qupath; --skip-qupath explicitly omits final QuPath verification')
    settings=dict(image_type=args.image_type,target=target,series=meta['series'],workers=args.workers,heap_gib=args.heap_gib,
                  roi_size=args.roi_size,roi_json=None if not args.roi_json else Path(args.roi_json).read_text(encoding='utf-8'),skip_qupath=args.skip_qupath)
    if is_kfb or is_kfbf:settings['missing_pixels']=missing_policy
    fingerprint=stage_fingerprint(meta,settings,cfg)
    rows=read_rois(src,cfg,meta,args.roi_size,args.roi_json)
    parent=work
    while not parent.exists():parent=parent.parent
    decoded=meta['X']*meta['Y']*meta['C']*(2 if meta['dtype']=='uint16' else 1)
    reserve=int(args.min_free_gib*1024**3)
    free_bytes=shutil.disk_usage(parent).free
    if args.dry_run:
        require(free_bytes>reserve+4*decoded,'DISK_BUDGET','Insufficient free space for conservative temporary+output budget')
        return dict(status='READY_TO_CONVERT',target=target,metadata={k:v for k,v in meta.items() if k not in ['source_xml','inventory','icc_profile_base64']},roi_count=len(rows),
                    disk=dict(decoded_bytes=decoded,required_free_bytes=reserve+4*decoded,available_bytes=free_bytes,filesystem_write_check='DEFERRED_UNTIL_OUTPUT_CREATION'))
    previous=None
    if work.exists():
        require((work/'status.json').is_file(),'NO_RESUME_RECORD','Existing directory has no run record')
        previous=json.loads((work/'status.json').read_text(encoding='utf-8'))
        require(previous.get('fingerprint')==fingerprint,'RESUME_MISMATCH','Input inventory, parameters, code or runtime changed; use a new output directory')
        if previous.get('status') in ['QUPATH_VERIFIED','QUPATH_VERIFIED_WITH_READER_LIMITATION','CONVERTED_NOT_QUPATH_VERIFIED']:
            final=Path(previous['output'])
            require(final.is_file() and file_hash(final)==previous['sha256'],'FINAL_CHANGED','Completed output changed')
            return previous
    require(free_bytes>reserve+4*decoded,'DISK_BUDGET','Insufficient free space for conservative temporary+output budget')
    work.mkdir(parents=True,exist_ok=args.resume)
    lock=work/'.running'
    try:lock_fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise SlideError('RUN_LOCKED','Run lock exists. Check the recorded process before manually removing a stale lock')
    os.write(lock_fd,str(os.getpid()).encode());os.close(lock_fd)
    state=previous or dict(version=VERSION,fingerprint=fingerprint,source=str(src),completed={})
    state.update(status='RUNNING',target=target)
    dump(work/'status.json',state)
    zpath=work/'image.zarr'
    suffix='.svs' if target=='svs' else '.ome.tiff'
    pending=work/('image.pending'+suffix)
    final=work/('image'+suffix)
    try:
        require(not final.exists(),'OUTPUT_EXISTS','Final output already exists without completed record; inspect it before recovery')
        check_filesystem(work)
        dump(work/'source.json',{k:v for k,v in meta.items() if k!='source_xml'})
        (work/'source.ome.xml').write_text(meta['source_xml'],encoding='utf-8')
        if src.suffix.lower()=='.mrxs':shutil.copyfile(src.with_suffix('')/'Slidedat.ini',work/'source_Slidedat.ini')
        (work/'tmp').mkdir(exist_ok=True)
        env=os.environ.copy()
        if cfg.get('java_home'):env['JAVA_HOME']=cfg['java_home'];env['PATH']=str(Path(cfg['java_home'])/'bin')+os.pathsep+env.get('PATH','')
        env.update(JAVA_OPTS=f'-Xmx{args.heap_gib}g -Djava.awt.headless=true',TMPDIR=str(work/'tmp'))
        if not is_kfb and not state['completed'].get('zarr'):
            preserve_partial(zpath)
            if is_kfbf:
                run_command('kfbf_to_zarr',[sys.executable,Path(__file__).with_name('kfbf_backend.py'),'--input',src,'--output',zpath,'--metadata',work/'source.json','--report',work/'kfbf_write.json','--missing-pixels',missing_policy,'--workers',str(args.workers)],work,env,state,reserve)
            else:
                run_command('bioformats2raw',[Path(cfg['bioformats2raw_home'])/'bin/bioformats2raw',src,zpath,'--series',str(meta['series']),'--ngff-version','0.4','--max-workers',str(args.workers),'--memo-directory',work/'tmp'],work,env,state,reserve)
        if not is_kfb:
            state['stage']='zarr_validation';dump(work/'status.json',state)
            check_zarr(zpath,meta,rows)
            if is_kfbf:
                from kfbf_backend import verify_kfbf_zarr
                dump(work/'kfbf_source_zarr_validation.json',verify_kfbf_zarr(src,meta,zpath))
            state['completed']['zarr']=True;dump(work/'status.json',state)
        if not pending.is_file():state['completed']['tiff']=False
        if not state['completed'].get('tiff'):
            preserve_partial(pending)
            if is_kfb:
                run_command('kfb_to_svs',[sys.executable,Path(__file__).with_name('kfb_backend.py'),'--input',src,'--output',pending,'--metadata',work/'source.json','--report',work/'kfb_write.json','--missing-pixels',missing_policy,'--workers',str(args.workers)],work,env,state,reserve)
            elif target=='svs':
                run_command('svs_writer',[sys.executable,Path(__file__).with_name('svs_writer.py'),'--zarr',zpath,'--metadata',work/'source.json','--output',pending],work,env,state,reserve)
            else:
                run_command('raw2ometiff',[Path(cfg['raw2ometiff_home'])/'bin/raw2ometiff',zpath,pending,'--compression','LZW','--max_workers',str(args.workers)],work,env,state,reserve)
        state['stage']='pixel_validation';dump(work/'status.json',state)
        if target=='svs':
            if is_kfb:
                from kfb_backend import verify_kfb_svs
                qc=verify_kfb_svs(src,meta,pending,missing_policy)
            else:
                from svs_writer import verify_svs
                qc=verify_svs(zpath,meta,pending)
            import tifffile,zarr,numpy as np
            with tifffile.TiffFile(pending) as tf:
                store=tf.pages[0].aszarr()
                try:
                    ar=zarr.open(store,mode='r')
                    for row,src_pixels in rows:require(np.array_equal(src_pixels,take(ar,'YXS',row,True)),'SOURCE_SVS_MISMATCH',str(row))
                finally:store.close()
            qc['source_roi_count']=len(rows)
        else:
            qc=check_ome(pending,meta,rows)
            if is_kfbf:
                from kfbf_backend import verify_kfbf_ome
                qc['full_pixel_validation']=verify_kfbf_ome(zpath,meta,pending)
        dump(work/'pixel_validation.json',qc)
        state['completed']['tiff']=True;dump(work/'status.json',state)
        require(meta['inventory']==inventory([v['path'] for v in meta['inventory']]),'SOURCE_CHANGED','Source changed during conversion')
        if not args.skip_qupath:
            import numpy as np
            expected={k:v for k,v in meta.items() if k not in ['source_xml','inventory','icc_profile_base64']}
            expected['channels']=None if meta['rgb'] else meta['channels']
            expected['expected_pyramid_levels']=len(qc['levels']) if is_kfb or target!='svs' else len({v['level'] for v in qc['comparisons']})
            if is_kfb:
                expected['pyramid_dimensions']=[dict(width=v['width'],height=v['height']) for v in qc['levels']]
            expected['roi_checks']=[]
            for row,src_pixels in rows:
                for c in range(3 if meta['rgb'] else 1):
                    pixels=src_pixels[:,:,c] if meta['rgb'] else src_pixels
                    normalized=np.asarray(pixels,dtype=np.dtype(meta['dtype']).newbyteorder('<'))
                    expected['roi_checks'].append(dict(c=c if meta['rgb'] else row['c'],x=row['x'],y=row['y'],w=row['width'],h=row['height'],sha256=hashlib.sha256(normalized.tobytes()).hexdigest()))
            dump(work/'qupath_expected.json',expected)
            qenv=os.environ.copy()
            run_command('qupath',[cfg['qupath'],'script','-i',pending,'-a',work/'qupath_validation.json','-a',work/'qupath_expected.json',Path(__file__).with_name('validate_qupath.groovy')],work,qenv,state,reserve)
            qp=json.loads((work/'qupath_validation.json').read_text(encoding='utf-8'))
            require(qp.get('status')=='PASS','QUPATH_FAILED','QuPath did not record PASS')
        digest=file_hash(pending)
        # Final publication is a no-clobber hard link + unlink on one filesystem.
        os.link(pending,final)
        pending.unlink()
        state.update(status='CONVERTED_NOT_QUPATH_VERIFIED' if args.skip_qupath else 'QUPATH_VERIFIED',stage='complete',output=str(final),sha256=digest,
                     size=final.stat().st_size,roi_count=len(rows),warnings=['Decoded source pixels preserved within tested regions; source compression cannot be undone.','Pyramid regenerated; unsupported vendor metadata remains in source sidecars.','Source stat inventory is not a complete input cryptographic checksum.'])
        if is_kfb:
            state['source_backend']='bundled_strict_kfb'
            state['missing_pixels_policy']=missing_policy
            state['kfb_write_report']=str(work/'kfb_write.json')
            state['warnings']=['KFB native pyramid dimensions are retained except explicitly recorded redundant levels; the source remains unchanged.',
                               'JPEG loss already present in the source cannot be undone; decoded RGB is preserved.',
                               'Absent source pixels remain unknown; an explicitly selected black fill is a display representation, not reconstructed tissue.']
            if not args.skip_qupath:
                state['warnings'].extend(qp.get('warnings',[]))
                state['qupath_readable_levels']=len(qp['pyramid_reads'])
                state['reader_limitations']=qp.get('reader_limitations',[])
                if state['reader_limitations']:state['status']='QUPATH_VERIFIED_WITH_READER_LIMITATION'
        if is_kfbf:
            state['source_backend']='bundled_strict_kfbf'
            state['kfbf_write_report']=str(work/'kfbf_write.json')
            state['missing_pixels_policy']=missing_policy
            state['warnings']=['All recorded independent base-channel decoded pixels were compared with the source; existing JPEG compression cannot be undone.',
                               'Unrecorded pixels remain unknown; explicitly selected black filling is separately quantified and is not recovered tissue.',
                               'Display pyramid was regenerated from the unchanged base channels; native vendor pyramid and extra metadata remain in source sidecars.',
                               'Only the explicitly supported KFBF layout and encoded precision are admitted; other variants stop without coercion.']
        dump(work/'status.json',state)
        return state
    except BaseException as exc:
        if state.get('stage')=='zarr_validation':
            state['completed']['zarr']=False
            state['completed']['tiff']=False
        if state.get('stage')=='pixel_validation':state['completed']['tiff']=False
        state.update(status='CANCELLED' if isinstance(exc,KeyboardInterrupt) else 'FAILED',error=str(exc),error_code=getattr(exc,'code',type(exc).__name__))
        dump(work/'status.json',state)
        raise
    finally:
        lock.unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True);p.add_argument('--output-dir',required=True);p.add_argument('--runtime',required=True)
    p.add_argument('--image-type',choices=['auto','brightfield','fluorescence'],default='auto');p.add_argument('--series',type=int)
    p.add_argument('--workers',type=int,default=4);p.add_argument('--heap-gib',type=int,default=8);p.add_argument('--min-free-gib',type=float,default=10)
    p.add_argument('--roi-size',type=int,default=128);p.add_argument('--roi-json');p.add_argument('--resume',action='store_true')
    p.add_argument('--dry-run',action='store_true');p.add_argument('--skip-qupath',action='store_true')
    p.add_argument('--missing-pixels',choices=['error','black'],default='error',help='KFB/KFBF only: reject absent pixels by default; black requires an explicit user choice')
    a=p.parse_args()
    try:print(json.dumps(run(a),ensure_ascii=False));return 0
    except Exception as exc:
        print(json.dumps(dict(status=getattr(exc,'code','FAILED'),message=str(exc),**getattr(exc,'details',{})),ensure_ascii=False));return 2
if __name__=='__main__':
    require(sys.version_info>=(3,10),'PYTHON_TOO_OLD','Python 3.10 or later is required')
    def cancelled(signum,frame):raise KeyboardInterrupt('Termination requested')
    signal.signal(signal.SIGTERM,cancelled)
    sys.exit(main())
