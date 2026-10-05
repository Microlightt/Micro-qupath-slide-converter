#!/usr/bin/env python3
"""Inspect slide structure without converting it. Python >=3.10."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys

EXTENSIONS = {'.mrxs', '.svs', '.ndpi', '.ndpis', '.czi', '.scn', '.vsi', '.bif', '.tif', '.tiff', '.nd2', '.kfb', '.kfbf'}
AUXILIARY_NAMES = {'label','label image','thumbnail','thumbnail image','macro','macro image','overview','overview image'}


class SlideError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def require(value, code, message, **details):
    if not value:
        raise SlideError(code, message, **details)


def dump(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def vendor_kind(path):
    """Choose a KFB-family reader from its signature, never channel count or suffix alone."""
    path = Path(path)
    if path.suffix.lower() not in {'.kfb', '.kfbf'}:
        return None
    with path.open('rb') as stream:
        signature = stream.read(8)
    require(signature[:4] == b'\xf1\x01\xee\xee', 'UNSUPPORTED_KFB_LAYOUT',
            'KFB-family section signature is not supported; do not rename the file')
    kind = signature[4:8].rstrip(b'\0')
    require(kind in {b'KFB', b'KFBF'}, 'UNSUPPORTED_KFB_LAYOUT',
            'Unrecognized KFB-family image kind')
    return 'kfbf' if kind == b'KFBF' else 'kfb'


def resolve_input(path):
    path = Path(path).expanduser().resolve()
    require(path.exists(), 'INPUT_NOT_FOUND', 'Input path is not accessible', path=str(path))
    if path.is_dir():
        candidates = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith('._'))
        require(len(candidates) == 1, 'SELECT_INPUT', 'Choose one slide entry file in this directory', candidates=[str(p) for p in candidates])
        path = candidates[0]
    require(path.suffix.lower() in EXTENSIONS, 'UNSUPPORTED_FORMAT', 'No validated reader route for this extension; do not rename the file', extension=path.suffix)
    if path.suffix.lower() in {'.kfb','.kfbf'}:
        vendor_kind(path)
    if path.suffix.lower() == '.mrxs':
        companion = path.with_suffix('')
        required = [companion / 'Slidedat.ini', companion / 'Index.dat']
        missing = [str(p) for p in required if not p.is_file()]
        if not companion.is_dir() or not any(companion.glob('Data*.dat')):
            missing.append(str(companion / 'Data*.dat'))
        if not missing:
            text = (companion / 'Slidedat.ini').read_bytes().decode('latin1')
            declared = set(re.findall(r'(?im)^\s*[^=\r\n]*=\s*(Data[^\r\n]*\.dat)\s*$', text))
            missing.extend(str(companion / name.strip()) for name in declared if not (companion / name.strip()).is_file())
        require(not missing, 'INCOMPLETE_SLIDE', 'MRXS entry may contain only a thumbnail. Provide the same-named companion directory with Slidedat.ini, Index.dat and every Data*.dat file.', missing=missing,
                example='slides/sample.mrxs + slides/sample/{Slidedat.ini,Index.dat,Data0000.dat,...}')
    return path


def source_files(path):
    path = Path(path)
    if path.suffix.lower() == '.mrxs':
        return [path] + sorted(p for p in path.with_suffix('').rglob('*') if p.is_file() and not p.name.startswith('._'))
    return [path]


def inventory(files):
    result = []
    for p in sorted(set(Path(f).resolve() for f in files)):
        require(p.is_file(), 'MISSING_DEPENDENCY', 'Missing reader dependency', path=str(p))
        st = p.stat()
        result.append(dict(path=str(p), size=st.st_size, mtime_ns=st.st_mtime_ns))
    return result


def runtime_config(path):
    cfg = json.loads(Path(path).read_text(encoding='utf-8'))
    require(isinstance(cfg, dict), 'BAD_RUNTIME', 'Runtime configuration must be an object')
    home = Path(cfg['bioformats2raw_home'])
    require((home / 'lib').is_dir(), 'MISSING_RUNTIME', 'Missing bioformats2raw lib directory')
    return cfg


def open_reader(path, cfg, heap_gib=4):
    kind = vendor_kind(path)
    if kind == 'kfbf':
        from kfbf_backend import open_reader as open_kfbf_reader
        return open_kfbf_reader(path,cfg),None,None
    if kind == 'kfb':
        from kfb_backend import open_reader as open_kfb_reader
        return open_kfb_reader(path,cfg),None,None
    import jpype as j
    if not j.isJVMStarted():
        jars = list((Path(cfg['bioformats2raw_home']) / 'lib').glob('*.jar'))
        require(jars, 'MISSING_RUNTIME', 'No Bio-Formats JAR files')
        j.startJVM(cfg.get('jvm') or j.getDefaultJVMPath(), f'-Xmx{heap_gib}g', '-Djava.awt.headless=true', classpath=[str(p) for p in jars])
    J = j.JClass
    J('loci.common.DebugTools').setRootLevel('ERROR')
    reader = J('com.glencoesoftware.bioformats2raw.MiraxReader' if path.suffix.lower() == '.mrxs' else 'loci.formats.ImageReader')()
    reader.setFlattenedResolutions(False)
    ome = J('loci.formats.MetadataTools').createOMEXMLMetadata()
    reader.setMetadataStore(ome)
    try:
        reader.setId(str(path))
    except BaseException:
        reader.close()
        raise
    return reader, ome, J


def calibration(ome, J, axis, series):
    quantity = getattr(ome, 'getPixelsPhysicalSize' + axis)(series)
    if quantity is None:
        return None
    value = quantity.value(J('ome.units.UNITS').MICROMETER)
    require(value is not None, 'UNKNOWN_CALIBRATION_UNIT', 'Physical size cannot be expressed in micrometers')
    return float(value.doubleValue())


def inspect(path, cfg, series=None, heap_gib=4):
    kind = vendor_kind(path)
    if kind == 'kfbf':
        from kfbf_backend import inspect_kfbf
        return inspect_kfbf(path,cfg,series)
    if kind == 'kfb':
        from kfb_backend import inspect_kfb
        return inspect_kfb(path,cfg,series)
    reader, ome, J = open_reader(path, cfg, heap_gib)
    try:
        images = []
        for index in range(reader.getSeriesCount()):
            reader.setSeries(index)
            reader.setResolution(0)
            name = ome.getImageName(index)
            images.append(dict(series=index, name=None if name is None else str(name),
                               **{a: int(getattr(reader, 'getSize' + a)()) for a in 'XYZCT'}, rgb=bool(reader.isRGB())))
        if series is None:
            primary = [v for v in images if (v['name'] or '').strip().lower() not in AUXILIARY_NAMES]
            require(len(primary) == 1, 'SELECT_SERIES', 'Multiple or ambiguous image series; select the intended full-resolution image explicitly', series=images)
            series = primary[0]['series']
        require(0 <= series < len(images), 'INVALID_SERIES', 'Series index is out of range', series=images)
        require((images[series]['name'] or '').strip().lower() not in AUXILIARY_NAMES,
                'AUXILIARY_IMAGE', 'Selected image is a known thumbnail/label/macro/overview, not a full-resolution primary slide', series=images)
        reader.setSeries(series)
        reader.setResolution(0)
        meta = dict(images[series])
        meta.update(dtype=str(J('loci.formats.FormatTools').getPixelTypeString(reader.getPixelType())),
                    effective_channels=int(reader.getEffectiveSizeC()), rgb_samples=int(reader.getRGBChannelCount()),
                    format=str(reader.getFormat()), resolution_count=int(reader.getResolutionCount()),
                    interleaved=bool(reader.isInterleaved()), little_endian=bool(reader.isLittleEndian()),
                    channels=[None if ome.getChannelName(series, c) is None else str(ome.getChannelName(series, c)) for c in range(ome.getChannelCount(series))],
                    physical_size_x=calibration(ome, J, 'X', series), physical_size_y=calibration(ome, J, 'Y', series), physical_size_unit='µm',
                    series_inventory=images)
        used = [Path(str(p)) for p in reader.getUsedFiles()]
        meta['inventory'] = inventory(source_files(path) + used)
        meta['source_xml'] = str(ome.dumpXML())
        meta['classification'] = 'rgb_requires_modality' if meta['rgb'] else 'independent_channels'
        # ICC is not reliably available through every Bio-Formats reader. Do not fabricate one.
        meta['icc_status'] = 'not_exposed_by_reader'
        if path.suffix.lower() in {'.svs', '.tif', '.tiff'}:
            try:
                import tifffile
                with tifffile.TiffFile(path) as tf:
                    tag = tf.pages[0].tags.get(34675)
                    if tag is not None:
                        meta['icc_profile_base64'] = base64.b64encode(tag.value).decode('ascii')
                        meta['icc_status'] = 'extracted_from_source_tiff'
            except (OSError, ValueError):
                pass
        return meta
    finally:
        reader.close()


def route(meta, image_type='auto'):
    require((meta.get('name') or '').strip().lower() not in AUXILIARY_NAMES,'AUXILIARY_IMAGE','Do not convert an associated thumbnail/label/macro/overview as a slide')
    require(meta['Z'] == 1 and meta['T'] == 1, 'UNSUPPORTED_DIMENSIONS', 'This version requires Z=1 and T=1; no planes are discarded')
    require(meta['dtype'] in ['uint8', 'uint16'], 'UNSUPPORTED_PIXEL_TYPE', 'No implicit conversion of pixel type')
    if meta['rgb']:
        require(meta['C'] == 3 and meta.get('rgb_samples', 3) == 3, 'UNSUPPORTED_RGB', 'Only three-component RGB is supported')
        require(image_type == 'brightfield', 'SELECT_MODALITY', 'RGB alone does not prove brightfield. Confirm brightfield WSI; merged fluorescence cannot recover original channels')
        require(meta['dtype'] == 'uint8', 'UNSUPPORTED_BRIGHTFIELD_DEPTH', 'SVS writer currently requires RGB uint8; no 16-to-8 bit conversion')
        return 'svs'
    require(image_type != 'brightfield', 'UNSUPPORTED_BRIGHTFIELD_LAYOUT', 'SVS branch currently requires RGB uint8; monochrome brightfield is not silently recolored')
    require(meta['C'] >= 1 and meta.get('effective_channels', meta['C']) == meta['C'], 'UNSUPPORTED_CHANNEL_LAYOUT', 'Expected independent channels')
    return 'ome-tiff'


def read_rois(path, cfg, meta, roi_size=128, roi_file=None):
    import numpy as np
    reader, ome, J = open_reader(path, cfg)
    try:
        reader.setSeries(meta['series'])
        reader.setResolution(0)
        width, height = min(roi_size, meta['X']), min(roi_size, meta['Y'])
        positions = json.loads(Path(roi_file).read_text(encoding='utf-8')) if roi_file else [dict(x=int(fx*(meta['X']-width)), y=int(fy*(meta['Y']-height)), width=width, height=height) for fx in [.05,.35,.65,.95] for fy in [.05,.5,.95]]
        dtype = np.dtype(meta['dtype']).newbyteorder('<' if meta['little_endian'] else '>')
        require(isinstance(positions,list) and positions,'INVALID_ROI','ROI JSON must be a nonempty list')
        total_bytes=0
        for pos in positions:
            w,h=int(pos['width']),int(pos['height'])
            require(0<w<=2048 and 0<h<=2048,'ROI_TOO_LARGE','Each ROI edge must be 1–2048 pixels; choose smaller tissue regions')
            total_bytes+=w*h*meta['C']*dtype.itemsize
        require(total_bytes<=512*1024**2,'ROI_BUDGET','Combined retained ROI samples exceed 512 MiB; reduce ROI size/count')
        rows = []
        for pos in positions:
            x,y,w,h = (int(pos[k]) for k in ['x','y','width','height'])
            require(x>=0 and y>=0 and w>0 and h>0 and x+w<=meta['X'] and y+h<=meta['Y'], 'INVALID_ROI', 'ROI is outside the full-resolution image')
            for c in range(1 if meta['rgb'] else meta['C']):
                raw = bytes(reader.openBytes(reader.getIndex(0,c,0),x,y,w,h))
                a = np.frombuffer(raw,dtype=dtype)
                if meta['rgb']:
                    a = a.reshape(h,w,3) if reader.isInterleaved() else a.reshape(3,h,w).transpose(1,2,0)
                else:
                    a = a.reshape(h,w)
                rows.append((dict(x=x,y=y,width=w,height=h,c=c),a.copy()))
        require(any(np.any(np.ptp(a.astype('float64'),axis=(0,1))) for _,a in rows), 'NEEDS_TISSUE_ROI', 'All sampled areas are spatially constant; supply --roi-json with non-background full-resolution coordinates before conversion')
        return rows
    finally:
        reader.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True)
    p.add_argument('--runtime')
    p.add_argument('--series',type=int)
    p.add_argument('--output')
    p.add_argument('--preflight-only',action='store_true')
    a=p.parse_args()
    try:
        path=resolve_input(a.input)
        result=dict(status='INPUT_FILES_PRESENT',input=str(path))
        if not a.preflight_only:
            require(a.runtime,'MISSING_RUNTIME','Run environment.py doctor/setup, then provide --runtime')
            result=inspect(path,runtime_config(a.runtime),a.series)
            result.pop('source_xml',None)
            result['status']='INSPECTED'
        if a.output:dump(a.output,result)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        print(json.dumps(dict(status=getattr(exc,'code','PROBE_FAILED'),message=str(exc),**getattr(exc,'details',{})),ensure_ascii=False))
        return 2
    return 0

if __name__=='__main__':
    sys.exit(main())
