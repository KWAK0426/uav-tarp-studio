"""Validate completed partial-band evidence for display and explicit full-flight approval."""
import copy
import csv
import math
from pathlib import Path
from osgeo import gdal
import numpy as np
from uav_pipeline.config import load_config
from uav_pipeline.easy_elc import input_signature
from uav_pipeline.elc_fit import _current_manifest, _library_key
from uav_pipeline.integrated import linked_plan
from uav_pipeline.roi_studio_bridge import read_json, atomic_json, digest


def pointer(cfg):
    return Path(cfg['_config_path']).with_suffix('.completed-result.json')


def table(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def matching_basis(cfg, saved):
    """Identical bundled calibration contents may move between app versions."""
    current = copy.deepcopy(cfg)
    # Input equivalence does not depend on a newly connected output plan.
    current['preparation'].pop('integrated_plan_file',None)
    for key in ('gain_csv', 'tarp_reflectance_csv'):
        if digest(current['preparation'][key]) != digest(saved['preparation'][key]):
            raise ValueError('교정 자료가 완료 결과와 다릅니다: ' + key)
        current['preparation'][key] = saved['preparation'][key]
    # GUI fields serialize numbers as text. Preserve the archived signature when values agree.
    for key in ('time_band','max_ed_delta_seconds'):
        old = saved['preparation'].get(key)
        new = current['preparation'].get(key)
        if old is not None and new is not None and float(old)==float(new):
            current['preparation'][key] = old
    if input_signature(current) != input_signature(saved):
        keys = ('raw_directory','gps_path','ed_csv','gain_csv','reference_shot','source_timezone',
                'time_band','max_ed_delta_seconds','tarp_reflectance_csv')
        changed = [key for key in keys if current['preparation'].get(key)!=saved['preparation'].get(key)]
        changed += [key for key in ('dataset','general','band_names') if current.get(key)!=saved.get(key)]
        raise ValueError('날짜·회차·원영상·Ed·기준영상·후보가 완료 결과와 다릅니다. 변경 항목: '+
                         ', '.join(changed or ['선택 영상·원영상 서명']))
    return current


def inspect(cfg, run):
    run = Path(run).resolve()
    if run.parent != Path(cfg['general']['project_root']).resolve():
        raise ValueError('현재 프로젝트 안의 완료 실행 폴더를 선택하세요.')
    status = read_json(run/'night_status.json')
    verification = read_json(run/'output_verification.json')
    saved = load_config(run/'project.json')
    original_settings = read_json(run/'project.json')['preparation']
    for key in ('gain_csv','tarp_reflectance_csv'):
        saved['preparation'][key] = original_settings[key]
    current = matching_basis(cfg, saved)
    manifest_path = _current_manifest(cfg)
    manifest_hash = digest(manifest_path)
    if (status.get('status') != 'complete' or not verification.get('passed')
            or status.get('source_manifest_sha256') != manifest_hash):
        raise ValueError('현재 승인 ROI와 일치하는 완료·검증 결과가 아닙니다.')
    parent = read_json(manifest_path)
    derived_path = run/'partial_roi_manifest.json'
    derived = read_json(derived_path)
    if derived.get('parent_manifest_sha256') != manifest_hash:
        raise ValueError('부분 밴드 결과의 원래 ROI 출처가 다릅니다.')
    additions = {'elc_bands','excluded_bands','parent_manifest','parent_manifest_sha256','scope_only_derivation'}
    if {k:v for k,v in derived.items() if k not in additions} != parent:
        raise ValueError('완료 결과의 ROI·마스크·기준 자료가 변경됐습니다.')
    bands = status['bands'];excluded = status['excluded']
    if (not bands or len(set(bands)) != len(bands) or any(type(b) is not int or b not in range(1,11) for b in bands)
            or set(excluded) != {str(b) for b in set(range(1,11))-set(bands)}
            or any(not isinstance(v,str) or not v.strip() for v in excluded.values())
            or saved['preparation'].get('elc_bands') != bands or derived.get('elc_bands') != bands
            or derived.get('excluded_bands') != excluded or saved['preparation'].get('elc_excluded_bands') != excluded):
        raise ValueError('출력 밴드와 제외 사유가 일치하지 않습니다.')
    shots = sorted(parent['images'])
    if status['shots'] != shots:
        raise ValueError('완료 결과의 선택 영상이 현재 승인 목록과 다릅니다.')
    plan = linked_plan(saved)
    if not plan or {j['shot'] for j in plan['jobs']} != set(shots):
        raise ValueError('방사·센서·Ed 완료 계획이 선택 영상과 다릅니다.')
    coef = run/'Coefficients/elc_coefficients.csv'
    rows = table(coef)
    if (len(rows)!=len(bands) or {int(r['band']) for r in rows}!=set(bands)
            or any(r['reference_shot']!=parent['reference_shot'] or r['roi_manifest_sha256']!=digest(derived_path)
                   or r['coefficient_convention']!='L=a*rho+b' or int(r['fit_tarp_count'])<2
                   or not math.isfinite(float(r['a'])) or float(r['a'])<=0 or not math.isfinite(float(r['b'])) for r in rows)):
        raise ValueError('완료 계수의 밴드·기준영상·ROI 출처를 확인할 수 없습니다.')
    fit_qc = read_json(run/'Coefficients/reference_raw_roi_saturation_qc.json')['rows']
    validation = table(run/'independent_tarp_validation.csv')
    summary=[]
    for b in bands:
        fit = [r for r in fit_qc if r['band']==b and r['use_for_fit']]
        vals = [r for r in validation if int(r['band'])==b and r['role']=='independent_validation' and r['usable']=='True']
        if (len({_library_key(r['tarp']) for r in fit})<2 or any(r['saturated_pct']>=1 for r in fit)
                or len({_library_key(r['tarp']) for r in vals})<2 or any(r['shot'] not in shots or r['shot']==parent['reference_shot']
                   or float(r['raw_saturation_pct'])>=1 or int(r['n_pixels'])<5 or not math.isfinite(float(r['bias'])) for r in vals)):
            raise ValueError(f'B{b}: 최소 두 유효 타프·독립 검증 조건이 맞지 않습니다.')
        summary.append(dict(band=b,rmse_pp=100*math.sqrt(sum(float(r['bias'])**2 for r in vals)/len(vals))))
    outputs=run/'Reflectance'
    coefficients={int(r['band']):(float(r['a']),float(r['b'])) for r in rows}
    expected={Path(j['src']).name:j for j in plan['jobs'] if j['band'] in bands}
    if {p.name for p in outputs.iterdir() if p.suffix.lower() in ('.tif','.tiff')} != set(expected):
        raise ValueError('반사도 파일이 누락되거나 다른 밴드가 섞였습니다.')
    if verification['reflectance_files'] != len(expected):
        raise ValueError('출력 검증 개수가 다릅니다.')
    for name,j in expected.items():
        ds=gdal.Open(str(outputs/name));tags=ds.GetMetadata()
        if (ds.RasterCount!=1 or ds.GetRasterBand(1).DataType!=gdal.GDT_Float32
                or [ds.RasterXSize,ds.RasterYSize]!=[j['width'],j['height']]
                or tags.get('ELC_COEFFICIENT_SHA256')!=digest(coef)
                or tags.get('ELC_APPLY_FORMULA')!='rho=(L-b)/a'
                or Path(tags.get('INPUT_PATH','')).resolve()!=Path(j['dst']).resolve()
                or tags.get('INPUT_PROVENANCE_MANIFEST_SHA256')!=digest(saved['preparation']['integrated_plan_file'])):
            raise ValueError('반사도 파일의 계산 출처가 다릅니다: '+name)
        source=gdal.Open(j['dst']);raw=source.GetRasterBand(1).ReadAsArray().astype('float64')
        actual=ds.GetRasterBand(1).ReadAsArray();valid=np.isfinite(raw)
        nodata=source.GetRasterBand(1).GetNoDataValue()
        if nodata is not None:valid &= raw!=nodata
        a,b=coefficients[j['band']]
        if not valid.any() or not np.allclose(actual[valid],(raw[valid]-b)/a,rtol=2e-6,atol=2e-6):
            raise ValueError('저장 반사도 값이 기록된 계수와 일치하지 않습니다: '+name)
        source=None
        ds=None
    evidence=[run/'project.json',run/'night_status.json',run/'output_verification.json',derived_path,coef,
              run/'Coefficients/reference_raw_roi_saturation_qc.json',run/'independent_tarp_validation.csv',
              run/'validation_summary.csv',run/'RESULT.html',Path(saved['preparation']['integrated_plan_file']),
              *[outputs/name for name in sorted(expected)]]
    return dict(schema='connected_partial_result_v1',run=str(run),input_signature=input_signature(current),
        roi_sha256=manifest_hash,files={str(p):digest(p) for p in evidence},shots=len(shots),bands=bands,
        excluded=excluded,summary=summary,reflectance_files=len(expected),report=str(run/'RESULT.html'),
        reflectance_directory=str(outputs),scientific_approval=False)


def connect(cfg, run):
    result=inspect(cfg,run)
    atomic_json(pointer(cfg),result)
    return result


def load(cfg):
    recorded=read_json(pointer(cfg))
    current=inspect(cfg,recorded['run'])
    if current!=recorded:
        raise ValueError('연결 후 입력 또는 결과 파일이 바뀌었습니다. 완료 결과를 다시 확인하세요.')
    return current


def approval_record(cfg, verified=None):
    """Perform the full read-only verification before asking the operator to approve."""
    recorded = read_json(pointer(cfg))
    result = recorded if verified is not None and verified == recorded else load(cfg)
    return dict(kind='connected_partial_result_v1', result=result, report=result['report'],
                source_project=cfg['_config_path'])


def approval_evidence(cfg, record):
    """Recheck the approved bytes and current inputs without recalculating saved pixels."""
    result = record['result']
    run = Path(result['run'])
    if run.parent.resolve() != Path(cfg['general']['project_root']).resolve():
        raise ValueError('검증 결과와 현재 프로젝트가 다릅니다.')
    for filename, expected in result['files'].items():
        if digest(filename) != expected:
            raise ValueError('승인 근거 파일이 변경되었습니다: '+Path(filename).name)
    saved = load_config(run/'project.json')
    original = read_json(run/'project.json')['preparation']
    for key in ('gain_csv','tarp_reflectance_csv'):
        saved['preparation'][key] = original[key]
    current = matching_basis(cfg,saved)
    if input_signature(current) != result['input_signature'] or digest(_current_manifest(cfg)) != result['roi_sha256']:
        raise ValueError('검증 이후 원영상·Ed·교정 자료·후보·ROI가 변경되었습니다.')
    from .general_tarp_review import raw_tarp_scope
    _, scope = raw_tarp_scope(cfg)
    if scope['included'] != result['bands']:
        raise ValueError('검증 결과와 현재 유효 밴드가 다릅니다.')
    return dict(connected_result=result, auto_band_scope=scope, summary=result['summary'])


def reuse_coefficients(cfg, approval):
    """Copy the verified fit into a new run, retaining its original receipt and numeric values."""
    from .preparation import locations
    from .elc_priority import checked_coefficients
    proof = approval_evidence(cfg,approval['pilot'])
    if proof != approval['evidence']:
        raise ValueError('승인한 부분 밴드 검증 근거가 변경되었습니다.')
    result = proof['connected_result']
    run = Path(result['run'])
    output = locations(cfg)['coef']
    output.mkdir(parents=True,exist_ok=True)
    qc = read_json(run/'Coefficients/reference_raw_roi_saturation_qc.json')
    atomic_json(output/'reference_raw_roi_saturation_qc.json',qc)
    source = run/'Coefficients/elc_coefficients.csv'
    rows = table(source)
    original_hash = digest(run/'partial_roi_manifest.json')
    parent_hash = digest(_current_manifest(cfg))
    for row in rows:
        if row['roi_manifest_sha256'] != original_hash:
            raise ValueError('검증 계수의 원본 ROI 해시가 다릅니다.')
        row['roi_manifest_sha256'] = parent_hash
    target = output/'elc_coefficients.csv'
    with target.open('w',encoding='utf-8-sig',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    cfg['preparation']['coefficient_csv'] = str(target)
    checked_coefficients(cfg,cfg['preparation']['reference_shot'])
    atomic_json(output/'approved_coefficient_origin.json',dict(source=str(source),sha256=digest(source),
                parent_roi_sha256=parent_hash, original_roi_sha256=original_hash,
                approval=approval, numeric_coefficients_reused=True))
    return dict(coefficient_csv=str(target))


def label(result, band_names):
    included=' · '.join(band_names[str(b)] for b in result['bands'])
    excluded=' · '.join(band_names[b] for b in result['excluded'])
    errors=[r['rmse_pp'] for r in result['summary']]
    return (f"부분 밴드 생성·검증 완료 · {result['shots']}장 × {len(result['bands'])}밴드 = {result['reflectance_files']}개\n"
            f"출력: {included}\n제외: {excluded} (세부 사유는 보고서)\n"
            f"독립 검증 RMSE {min(errors):.2f}–{max(errors):.2f}%p · 과학적 품질 승인·전체 촬영분 처리는 별도")
