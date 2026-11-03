"""Native DN -> radiance -> intercalibration -> Ed, with a single final write."""
from __future__ import annotations
from uav_pipeline.exif_support import executable as exif_executable

import argparse
import csv
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import uuid4

import numpy as np
from osgeo import gdal
import tifffile

from .config import load_config
from .preparation import initialize, locations, _gains, latest_candidate_selection, SENSOR
from .general_project import is_general, source_list_path, source_shots, source_signature, raw_for_shots, project_root
from . import time_ed
from .exif_support import require_paths_supported

CENTERS = {1:444, 2:475, 3:531, 4:560, 5:650, 6:668, 7:705, 8:717, 9:740, 10:842}
SCHEMA = 'integrated_radiance_ed_v1'
os.environ['GDAL_PAM_ENABLED']='NO'
gdal.SetConfigOption('GDAL_PAM_ENABLED','NO')
gdal.UseExceptions()


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    os.replace(temporary, path)


def settings_signature(cfg):
    s = cfg['preparation']
    keys = ('raw_directory', 'gps_path', 'ed_csv', 'gain_csv', 'reference_shot', 'source_timezone',
            'time_band', 'max_ed_delta_seconds', 'elc_only', 'integrated_save_radiance', 'integrated_output_root')
    data = {'dataset': cfg['dataset'], 'settings': {k:s.get(k) for k in keys},
            'band_names': cfg['band_names'], 'tool': cfg['preprocess']['tools']['radiometric']}
    if is_general(cfg):
        data.update(general=cfg['general'], source_manifest=source_signature(cfg))
        gps = Path(s['gps_path']) if s.get('gps_path') else None
        data['optional_gps_sha256'] = sha(gps) if gps and gps.is_file() else None
    if s.get('elc_only'):
        data['selected_shots'] = sorted(latest_candidate_selection(cfg)[2])
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()


def input_paths(cfg):
    s = cfg['preparation']
    return {'ed_sha256': s['ed_csv'], 'gain_sha256': s['gain_csv'],
            ('source_list_sha256' if is_general(cfg) else 'gps_sha256'): str(source_list_path(cfg))}


@lru_cache(maxsize=4)
def core(path):
    path = Path(path).resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('studio_integrated_native', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, 'radiometric_pixels'):
        raise ValueError('연결된 방사보정 도구에 통합 처리 인터페이스가 없습니다.')
    return module


def collect_sources(raw, shots):
    found = {}
    for path in sorted(Path(raw).rglob('*')):
        if any('Excluded_Not_in_GPS' in part for part in path.parts):continue
        match = time_ed.SHOT.fullmatch(path.name)
        if not path.is_file() or path.suffix.lower() not in ('.tif', '.tiff') or not match:continue
        shot, suffix = match[1].upper(), int(match[2] or 0)
        if shot not in shots or not suffix:continue
        key = shot, suffix
        if key in found:raise ValueError(f'원본 촬영번호·밴드 중복: {found[key]} / {path}')
        found[key] = path.resolve()
    expected = {(shot, band) for shot in shots for band in range(1,11)}
    missing = expected - found.keys()
    if missing:raise ValueError(f'원본 10밴드 누락 {len(missing)}개: {sorted(missing)[:10]}')
    return found


def measured_bounds(cfg):
    s = cfg['preparation']; times = []
    with Path(s['ed_csv']).open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            if row.get('cycle') and row['cycle'] != cfg['dataset']['flight']:continue
            dt = time_ed.timestamp(row['datetime_kst'], 'Asia/Seoul').astimezone(time_ed.KST)
            if dt.date().isoformat() == cfg['dataset']['date'][:10].replace('_','-'):times.append(dt)
    if not times:raise ValueError('해당 날짜·회차의 실측 Ed가 없습니다.')
    return min(times), max(times)


def metadata_rows(paths, native):
    result = {}
    tags = [*native.EXIFTOOL_TAGS, '-DateTimeOriginal']
    for start in range(0,len(paths),200):
        args = ['-j','-n','-G1','-a','-s',*tags,*map(str,paths[start:start+200])]
        proc = subprocess.run([exif_executable(),'-charset','filename=UTF8','-@','-'], input='\n'.join(args)+'\n',
                              capture_output=True, text=True, encoding='utf8', check=True)
        result.update({str(Path(r['SourceFile']).resolve()):r for r in json.loads(proc.stdout)})
        print(f'METADATA {min(start+200,len(paths))}/{len(paths)}', flush=True)
    if set(result) != set(map(str,paths)):raise ValueError('일부 원본 메타데이터를 읽지 못했습니다.')
    return result


def preflight(cfg):
    initialize(cfg); s = cfg['preparation']; p = locations(cfg)
    adapter_hash=sha(__file__)
    if s['sensor']!=SENSOR or not isinstance(s['integrated_save_radiance'],bool):raise ValueError('센서 종류·중간 저장 옵션을 확인하세요.')
    if not re.fullmatch(r'IMG_\d{4}',str(s['reference_shot'])):raise ValueError('ELC·Ed 기준영상 IMG 번호를 먼저 지정하세요.')
    if not Path(s['ed_csv']).is_file():raise ValueError('검증할 실측 10밴드 Ed CSV를 먼저 연결하세요.')
    if not 0 <= float(s['max_ed_delta_seconds']) <= 5:raise ValueError('통합 처리의 Ed 허용차는 0~5초입니다.')
    if {int(k):int(re.search(r'(\d+)nm',v)[1]) for k,v in cfg['band_names'].items()} != CENTERS:
        raise ValueError('실측 Ed의 파장순 10밴드 구성을 확인하세요.')
    raw = Path(s['raw_directory']).resolve()
    if not s['raw_directory'] or not raw.is_dir():raise ValueError('원본 폴더가 없습니다.')
    shots = source_shots(cfg)
    all_sources = raw_for_shots(raw, shots) if is_general(cfg) else None
    if s.get('elc_only'):shots &= set(latest_candidate_selection(cfg)[2])
    if not shots or s['reference_shot'] not in shots:raise ValueError('기준영상이 이번 처리의 GPS·선택 영상 목록에 없습니다.')
    source_files = ({key: path for key, path in all_sources.items() if key[0] in shots}
                    if all_sources is not None else collect_sources(raw,shots))
    require_paths_supported(source_files.values())
    fingerprints={key:sha(path) for key,path in input_paths(cfg).items()}
    initial_signature = settings_signature(cfg)
    original_stats={str(path):[path.stat().st_size,path.stat().st_mtime_ns] for path in source_files.values()}
    time_dir = p['base'] / p['base'].name.replace('_00_ELC_Process','_03_Time_Ed')
    try:times, time_csv = time_ed.load_prepared_times(cfg,time_dir)
    except ValueError as exc:raise ValueError('04의 촬영시간·태양각 CSV를 먼저 생성하거나 다시 검증하세요. '+str(exc)) from exc
    time_hash=sha(time_csv)
    times = [r for r in times if r['shot'] in shots]
    if {r['shot'] for r in times} != shots or len(times) != len(shots):raise ValueError('검증된 촬영시간 CSV에 선택 영상이 누락·중복됐습니다.')
    matched = time_ed.match_ed(times,s['ed_csv'],cfg)
    first,last = measured_bounds(cfg)
    bad = [r['shot'] for r in matched if r['match_status'] != 'matched' or not first <= time_ed.timestamp(r['capture_time_kst'],'Asia/Seoul') <= last]
    if bad:raise ValueError(f'시간·Ed 미매칭 또는 실측 범위 밖 {len(bad)}촬영분: {bad[:15]}')
    ref = next(r for r in matched if r['shot']==s['reference_shot'])
    by_shot = {r['shot']:r for r in matched}
    native_path = str(Path(cfg['preprocess']['tools']['radiometric']).resolve())
    native_hash=sha(native_path)
    native = core(native_path); gains = _gains(s)
    if not all(np.isfinite(np.float32(v)) and np.float32(v)>0 for v in gains.values()):raise ValueError('Float32로 적용할 수 없는 gain입니다.')
    run_root = Path(s.get('integrated_output_root') or p['base'].parent / (p['base'].name.replace('_00_ELC_Process','_Integrated_Runs'))).resolve()
    if is_general(cfg):
        run_root.relative_to(project_root(cfg))
    for protected in (raw,p['rad'],p['ed'],p['ref'],p['qc']):
        if run_root.is_relative_to(protected.resolve()):raise ValueError('통합 결과는 원본·기존 결과 폴더 밖에 저장하세요.')
    sources = sorted(source_files.values()); meta = metadata_rows(sources,native)
    run_id = datetime.now(time_ed.KST).strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8]
    run = run_root / run_id
    jobs=[]; snapshots={}; uncompressed=0; serials={}; shot_bands={}
    ed_hash,gain_hash=sha(s['ed_csv']),sha(s['gain_csv'])
    for (shot,suffix),source in sorted(source_files.items()):
        data=meta[str(source)]; nm=float(native._meta_get(data,'CentralWavelength'))
        bands=[b for b,center in CENTERS.items() if center==nm]
        if len(bands)!=1:raise ValueError(f'지원하지 않는 실제 파장: {source} / {nm}')
        band=bands[0]; ds=gdal.Open(str(source))
        used=shot_bands.setdefault(shot,set())
        if band in used:raise ValueError(f'한 촬영분에 실제 파장이 중복됐습니다: {shot} / {nm}')
        used.add(band)
        if ds.RasterCount!=1 or ds.GetRasterBand(1).DataType!=gdal.GDT_UInt16 or ds.GetMetadataItem('SENSOR_INTERCALIBRATION_APPLIED')=='true':
            raise ValueError(f'이미 보정됐거나 원본 UInt16이 아닌 입력: {source}')
        coefficients=native._parse_float_list(native._meta_get(data,'RadiometricCalibration'))
        if len(coefficients)!=3 or not all(math.isfinite(v) for v in coefficients) or float(native._meta_get(data,'ExposureTime'))<=0:
            raise ValueError(f'방사보정 메타데이터 확인 필요: {source}')
        center=native._parse_float_list(native._meta_get(data,'VignettingCenter'))
        polynomial=native._parse_float_list(native._meta_get(data,'VignettingPolynomial'))
        iso=float(native._meta_get(data,'ISOSpeed',native._meta_get(data,'ISO',100)))
        if len(center)!=2 or len(polynomial)!=6 or not all(math.isfinite(v) for v in [*center,*polynomial,iso]) or iso<=0:
            raise ValueError(f'비네팅·ISO 메타데이터 확인 필요: {source}')
        width,height=ds.RasterXSize,ds.RasterYSize;ds=None
        source_stat=source.stat();snapshots[str(source)]=[source_stat.st_size,source_stat.st_mtime_ns]
        if snapshots[str(source)]!=original_stats[str(source)]:raise ValueError(f'검사 중 원본이 변경됐습니다: {source}')
        uncompressed+=width*height*4
        row=by_shot[shot];column=f'Band{band}_{cfg["band_names"][str(band)]}'
        image_ed,ref_ed=float(row[column]),float(ref[column]);factor=ref_ed/image_ed
        if not math.isfinite(factor) or not np.isfinite(np.float32(factor)) or np.float32(factor)<=0:raise ValueError('유효하지 않은 Ed 비율')
        serials.setdefault(str(int(nm)),set()).add(str(native._meta_get(data,'SerialNumber','unknown')))
        relative=source.relative_to(raw)
        tags={'PROCESSING_STAGE':'radiometric_sensor_ed_integrated','ED_NORMALIZATION_APPLIED':'true',
              'ED_REFERENCE_SHOT':s['reference_shot'],'ED_REFERENCE_CAPTURE_TIME_KST':ref['capture_time_kst'],
              'ED_REFERENCE_MATCH_TIME_KST':ref['ed_time_kst'],'ED_MATCH_TIME_KST':row['ed_time_kst'],
              'CAPTURE_TIME_KST':row['capture_time_kst'],'SOURCE_TIMEZONE':s['source_timezone'],
              'ED_TIME_DELTA_SECONDS':str(row['ed_abs_delta_seconds']),'ED_VALUE':str(image_ed),
              'ED_MAX_TIME_DELTA_SECONDS':str(s['max_ed_delta_seconds']),
              'ED_REFERENCE_TIME_DELTA_SECONDS':str(ref['ed_abs_delta_seconds']),
              'ED_REFERENCE_VALUE':str(ref_ed),'ED_NORMALIZATION_FACTOR':repr(factor),
              'ED_MATCH_METHOD':'nearest_measured_no_interpolation_no_extrapolation',
              'ED_SOURCE_FILE':str(Path(s['ed_csv']).resolve()),'ED_SOURCE_SHA256':ed_hash,
              'SENSOR_GAIN_SOURCE_SHA256':gain_hash,'INTEGRATED_RUN_ID':run_id,'INTEGRATED_PIPELINE_SCHEMA':SCHEMA,
              'SOURCE_SIZE_BYTES':str(source_stat.st_size),'SOURCE_MTIME_NS':str(source_stat.st_mtime_ns)}
        if is_general(cfg):
            tags.update(SOURCE_TARGET_MANIFEST=str(source_list_path(cfg)),
                        SOURCE_TARGET_MANIFEST_SHA256=fingerprints['source_list_sha256'])
        jobs.append({'src':str(source),'dst':str(run/'Ed_Normalized'/relative),
                     'cache':str(run/'Radiance'/relative) if s.get('integrated_save_radiance',False) else '',
                     'meta':data,'apply_sensor_correction':True,'sensor_gains':gains,'factor':factor,
                     'tags':tags,'width':width,'height':height,'band':band,'shot':shot,'native_path':native_path})
    layers=2 if s.get('integrated_save_radiance',False) else 1
    require_paths_supported(path for job in jobs for path in (job['dst'], job['cache']) if path)
    probe=run_root
    while not probe.exists():probe=probe.parent
    free=shutil.disk_usage(probe).free;required=math.ceil(uncompressed*layers*1.2)+16*1024**2
    if free<required:raise ValueError(f'용량 부족: 권장 {required/1024**3:.2f} GiB / 남음 {free/1024**3:.2f} GiB')
    for key,path in input_paths(cfg).items():
        if sha(path)!=fingerprints[key]:raise ValueError('Input source list, Ed or gain changed during preflight')
    if settings_signature(cfg) != initial_signature:raise ValueError('Settings or target manifest changed during preflight')
    if sha(time_csv)!=time_hash:raise ValueError('검사 도중 촬영시간 CSV가 변경됐습니다.')
    if sha(native_path)!=native_hash or sha(__file__)!=adapter_hash:raise ValueError('검사 도중 처리 코드가 변경됐습니다. 다시 검사하세요.')
    plan={'schema':SCHEMA,'run':str(run),'run_id':run_id,'jobs':jobs,'raw_snapshot':snapshots,
          'settings_snapshot':{'dataset':deepcopy(cfg['dataset']),'preparation':deepcopy(s),'band_names':deepcopy(cfg['band_names'])},
          'settings_signature':settings_signature(cfg),'native_sha256':native_hash,'adapter_sha256':adapter_hash,
          **fingerprints,
          'time_csv':str(time_csv),'time_csv_sha256':sha(time_csv),'time_signature':time_ed.time_signature(cfg,times),
          'shots':len(shots),'files':len(jobs),'sensor_serials':{k:sorted(v) for k,v in serials.items()},
          'save_radiance':layers==2,'uncompressed_bytes':uncompressed*layers,'recommended_free_bytes':required,
          'free_bytes':free,'worker_count':max(1,min(8,int(s['worker_count'])))}
    if is_general(cfg):
        plan['source_manifest'] = source_signature(cfg)
        plan['settings_snapshot']['general'] = deepcopy(cfg['general'])
    run.mkdir(parents=True,exist_ok=False)
    write_json(run/'plan.json',plan);time_ed.write_csv(run/'image_ed_matching.csv',matched)
    time_ed.write_csv(run/'image_time_solar.csv',times)
    time_ed.write_csv(run/'image_band_factors.csv',[{'source':j['src'],'output':j['dst'],'radiance_cache':j['cache'],
        'shot':j['shot'],'spectral_band':j['band'],'wavelength_nm':CENTERS[j['band']],
        'sensor_gain':gains[str(j['band'])],**j['tags']} for j in jobs])
    shutil.copy2(s['ed_csv'],run/'ed_input_snapshot.csv');shutil.copy2(s['gain_csv'],run/'gain_input_snapshot.csv')
    write_json(p['configs']/'integrated_latest.json',{'plan':str(run/'plan.json'),'sha256':sha(run/'plan.json')})
    write_json(run/'state.json',{'status':'checked','files':len(jobs),'shots':len(shots)})
    print(f'통합 검사 통과: {len(shots)}촬영분 / {len(jobs)}개 / 비압축 {uncompressed*layers/1024**3:.2f} GiB / 권장 여유 {required/1024**3:.2f} GiB',flush=True)
    print(f'통합 결과 폴더: {run}',flush=True)
    return plan


def check_output(path,job,plan_hash,normalized):
    ds=gdal.Open(str(path));tags=ds.GetMetadata()
    expected={**job['tags'],'PROVENANCE_MANIFEST_SHA256':plan_hash}
    if not normalized:expected.update(PROCESSING_STAGE='radiometric_calibration_sensor_intercalibration',ED_NORMALIZATION_APPLIED='false',
                                      ED_NORMALIZATION_FACTOR='1.0',PLANNED_ED_NORMALIZATION_FACTOR=job['tags']['ED_NORMALIZATION_FACTOR'])
    for key,value in expected.items():
        if tags.get(key)!=value:raise ValueError(f'기존 출력 provenance 불일치: {path} / {key}')
    if tags.get('SENSOR_INTERCALIBRATION_APPLIED')!='true' or float(tags.get('SENSOR_INTERCALIBRATION_GAIN','nan'))!=job['sensor_gains'][str(job['band'])]:
        raise ValueError(f'센서 교정 태그 불일치: {path}')
    if tags.get('SENSOR_INTERCALIBRATION_SPECTRAL_BAND')!=str(job['band']) or float(tags.get('CENTRAL_WAVELENGTH','nan'))!=CENTERS[job['band']]:
        raise ValueError(f'실제 파장 태그 불일치: {path}')
    if (ds.RasterXSize,ds.RasterYSize,ds.RasterCount,ds.GetRasterBand(1).DataType)!=(job['width'],job['height'],1,gdal.GDT_Float32):
        raise ValueError(f'출력 크기·자료형 불일치: {path}')
    ds=None
    with tifffile.TiffFile(path) as tif:
        if 'XMP' not in tif.pages[0].tags or not tif.pages[0].tags['ExifTag'].value.get('DateTimeOriginal'):
            raise ValueError(f'출력 EXIF/XMP 누락: {path}')


def apply_integrated(job):
    os.environ['GDAL_PAM_ENABLED']='NO';gdal.SetConfigOption('GDAL_PAM_ENABLED','NO');gdal.UseExceptions()
    native=core(job['native_path']);source=Path(job['src']);target=Path(job['dst'])
    if target.exists():
        check_output(target,job,job['plan_hash'],True)
        if job['cache']:check_output(Path(job['cache']),job,job['plan_hash'],False)
        return {'status':'skipped_exists','source':str(source),'output':str(target)}
    radiance,tags=native.radiometric_pixels(job)
    tags.update(job['tags'],PROVENANCE_MANIFEST_SHA256=job['plan_hash'])
    if not np.isfinite(radiance).all():raise ValueError(f'방사보정 비유한 값: {source}')
    if job['cache']:
        cache=Path(job['cache']);cache_tags={**tags,'PROCESSING_STAGE':'radiometric_calibration_sensor_intercalibration','ED_NORMALIZATION_APPLIED':'false',
                                           'ED_NORMALIZATION_FACTOR':'1.0','PLANNED_ED_NORMALIZATION_FACTOR':job['tags']['ED_NORMALIZATION_FACTOR']}
        if cache.exists():check_output(cache,job,job['plan_hash'],False)
        else:native.write_radiometric_pixels(source,cache,radiance,cache_tags)
    result=radiance*np.float32(job['factor'])
    if not np.isfinite(result).all():raise ValueError(f'Ed 정규화 비유한 값: {source}')
    native.write_radiometric_pixels(source,target,result,tags)
    return {'status':'written','source':str(source),'output':str(target)}


def load_plan(cfg):
    pointer=json.loads((locations(cfg)['configs']/'integrated_latest.json').read_text(encoding='utf8'))
    if sha(pointer['plan'])!=pointer['sha256']:raise ValueError('통합 계획 파일이 변경됐습니다. 다시 검사하세요.')
    plan=json.loads(Path(pointer['plan']).read_text(encoding='utf8'));s=cfg['preparation']
    if plan['settings_signature']!=settings_signature(cfg):raise ValueError('기준영상·Ed·gain·저장 옵션이 변경됐습니다. 다시 검사하세요.')
    expected={'native_sha256':cfg['preprocess']['tools']['radiometric'],'adapter_sha256':__file__,
              **input_paths(cfg),'time_csv_sha256':plan['time_csv']}
    if any(sha(path)!=plan[key] for key,path in expected.items()):raise ValueError('검사 후 입력·코드가 변경됐습니다. 다시 검사하세요.')
    if is_general(cfg):
        sources = raw_for_shots(s['raw_directory'], source_shots(cfg))
        selected = {str(path) for key, path in sources.items() if key[0] in {j['shot'] for j in plan['jobs']}}
        if selected != set(plan['raw_snapshot']):raise ValueError('Raw source inventory changed after preflight')
    for path,snapshot in plan['raw_snapshot'].items():
        stat=Path(path).stat()
        if [stat.st_size,stat.st_mtime_ns]!=snapshot:raise ValueError(f'검사 후 원본이 변경됐습니다: {path}')
    return plan,pointer['sha256']


def run(cfg):
    initialize(cfg);plan,plan_hash=load_plan(cfg);run_dir=Path(plan['run']);lock=run_dir/'RUNNING.lock'
    remaining=sum(j['width']*j['height']*4 for j in plan['jobs'] for path in (j['dst'],j['cache']) if path and not Path(path).exists())
    if shutil.disk_usage(run_dir).free<math.ceil(remaining*1.2)+16*1024**2:raise ValueError('남은 출력의 권장 여유 공간이 부족합니다.')
    try:
        with lock.open('x',encoding='ascii') as stream:stream.write(str(os.getpid()))
    except FileExistsError as exc:raise ValueError('같은 통합 실행의 잠금이 있습니다. 기존 작업을 확인하거나 새 검사로 별도 폴더를 사용하세요. 잠금은 자동 해제하지 않습니다.') from exc
    try:
        write_json(run_dir/'state.json',{'status':'running','pid':os.getpid()})
        native=core(cfg['preprocess']['tools']['radiometric'])
        jobs=[{**job,'plan_hash':plan_hash} for job in plan['jobs']]
        execution=native.Config(raw_root=cfg['preparation']['raw_directory'],gps_path=cfg['preparation']['gps_path'],output_root=str(run_dir),qc_dir_name='QC',worker_count=plan['worker_count'])
        result=native._parallel_map(jobs,apply_integrated,plan['worker_count'],run_dir/'files.csv',execution,'integrated',native._log_stdout)
        if result['failed'] or result['stopped']:raise RuntimeError(f'통합 처리 미완료: {result}')
        for index,job in enumerate(jobs,1):
            check_output(Path(job['dst']),job,plan_hash,True)
            if job['cache']:check_output(Path(job['cache']),job,plan_hash,False)
            if index%1000==0:print(f'출력 태그 검사 {index}/{len(jobs)}',flush=True)
        load_plan(cfg)
        report={'status':'complete','passed':True,'files':len(jobs),'shots':plan['shots'],'save_radiance':plan['save_radiance'],
                'output_bytes':sum(Path(j['dst']).stat().st_size+(Path(j['cache']).stat().st_size if j['cache'] else 0) for j in jobs),
                'header_tags_verified_files':len(jobs)*(2 if plan['save_radiance'] else 1),
                'raw_size_mtime_unchanged_files':len(jobs),'plan_sha256':plan_hash,'result':result,
                'note':'Header/provenance checks; independent pixel equivalence is tested on fixtures, not recomputed for all outputs.'}
        write_json(run_dir/'final_validation.json',report);write_json(run_dir/'state.json',report)
        print('통합 완료: '+str(run_dir),flush=True)
        return report
    except Exception as exc:
        write_json(run_dir/'state.json',{'status':'failed','error':str(exc)});raise
    finally:lock.unlink()


def linked_plan(cfg):
    path=cfg.get('preparation',{}).get('integrated_plan_file','')
    if not path:return None
    plan=json.loads(Path(path).read_text(encoding='utf8'))
    report=json.loads((Path(plan['run'])/'final_validation.json').read_text(encoding='utf8'))
    unlinked=deepcopy(cfg);unlinked['preparation'].pop('integrated_plan_file',None)
    if not report.get('passed') or report['plan_sha256']!=sha(path) or plan['settings_signature']!=settings_signature(unlinked):
        raise ValueError('연결된 통합 결과의 기준이 달라졌습니다. 04에서 기존 연결을 해제하고 다시 검사하세요.')
    for key,source in input_paths(cfg).items():
        if sha(source)!=plan[key]:raise ValueError('연결된 통합 결과의 Ed·gain·GPS가 변경됐습니다. 다시 검사하세요.')
    return plan


def connect_result(cfg):
    plan,plan_hash=load_plan(cfg)
    report=json.loads((Path(plan['run'])/'final_validation.json').read_text(encoding='utf8'))
    if not report.get('passed') or report['plan_sha256']!=plan_hash:raise ValueError('최종 검증을 통과한 통합 결과만 연결할 수 있습니다.')
    for job in plan['jobs']:check_output(Path(job['dst']),job,plan_hash,True)
    cfg['preparation']['integrated_plan_file']=str(Path(plan['run'])/'plan.json')
    cfg['preparation']['coefficient_basis_confirmed']=False
    return str(Path(plan['run'])/'Ed_Normalized')


if __name__=='__main__':
    if getattr(sys.stdout,'reconfigure',None):sys.stdout.reconfigure(encoding='utf8')
    if getattr(sys.stderr,'reconfigure',None):sys.stderr.reconfigure(encoding='utf8')
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--prepare',action='store_true')
    args=parser.parse_args();config=load_config(args.config)
    preflight(config) if args.prepare else run(config)
