"""Connect the existing map selector and require a reviewed pilot before bulk work."""
import csv
import hashlib
import json
import math
from pathlib import Path

from .general_project import project_root, source_shots, source_signature
from .roi_studio_bridge import atomic_json, digest, read_json
from .calibration_presets import expand_tarp_names, tarp_library_key


def map_basis(cfg):
    settings = cfg.get('tarp_workflow', {})
    mosaic = Path(settings.get('reference_mosaic') or '')
    photos = Path(settings.get('photos_override') or '')
    if not settings.get('reference_mosaic') or not mosaic.is_file():
        raise ValueError('02에서 타프 위치를 지정할 모자이크를 연결하세요.')
    if not settings.get('photos_override') or not photos.is_dir():
        raise ValueError('02에서 현재 날짜·회차의 정사영상 Photos 폴더를 연결하세요.')
    names = expand_tarp_names(settings.get('tarp_names', []))
    with Path(cfg['preparation']['tarp_reflectance_csv']).open(encoding='utf-8-sig') as f:
        available = {r['key'] for r in csv.DictReader(f)}
    if len(names) < 2 or not {tarp_library_key(n) for n in names} <= available:
        raise ValueError('02에서 이번 촬영에 사용한 타프 이름을 실측 CSV와 일치하게 두 개 이상 선택하세요.')
    files = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in photos.iterdir()
                   if p.is_file() and p.suffix.lower() in ('.tif', '.tiff'))
    if not files:
        raise ValueError('Photos 폴더 바로 아래에 정사영상 TIFF가 없습니다.')
    stat = mosaic.stat()
    return dict(mosaic=str(mosaic.resolve()), mosaic_signature=[stat.st_size, stat.st_mtime_ns],
                photos=str(photos.resolve()), photos_signature=hashlib.sha256(json.dumps(files).encode()).hexdigest(),
                tarps=names, **source_signature(cfg))


def map_context(cfg):
    from .preparation import locations
    basis = map_basis(cfg)
    paths = locations(cfg)
    raw = str(Path(cfg['preparation']['raw_directory']).resolve())
    data = dict(basis, date=cfg['dataset']['date'], flight=cfg['dataset']['flight'],
                dataset_root=str(project_root(cfg)), label=cfg['general']['date']+' · '+cfg['general']['session'],
                mosaic_role='location_reference_only', raw=raw, image_source=raw,
                radiance=str(paths['rad']), gps_shots=sorted(source_shots(cfg)),
                source_kind='raw_DN_boundary_only', candidate_preview_kind='panel_photos')
    data['identity'] = 'map_' + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:20]
    data['output'] = str(paths['roi'] / 'workflow' / data['identity'])
    data['location_library'] = str(project_root(cfg).parent / 'tarp_locations.json')
    package = Path(__file__).with_name('tarp_tool')
    revision = [(str(p.relative_to(package)), digest(p)) for p in sorted(package.rglob('*.py'))]
    data['code_revision'] = hashlib.sha256(json.dumps(revision).encode()).hexdigest()[:16]
    # Location selection needs footprints, not a full-flight EXIF scan.
    data['reference_timing'] = {'selection_rule': 'reference_selected_in_native_roi'}
    Path(data['output']).mkdir(parents=True, exist_ok=True)
    return data


def require_location(cfg, selection):
    from .tarp_tool.code.location_review import require_selection
    require_selection(selection, cfg['dataset'])
    context = read_json(Path(selection).parent.parent / 'context.json')
    if any(context.get(k) != v for k, v in map_basis(cfg).items()):
        raise ValueError('모자이크·Photos·타프 종류·대상 목록이 변경됐습니다. 02에서 위치와 후보를 다시 확정하세요.')


def raw_tarp_scope(cfg):
    """Check full outer native polygons before any expensive correction."""
    from .elc_priority import approved_shots
    from .elc_fit import _band_for_wavelength, _library, _library_key
    from .roi_studio_bridge import is_fit_roi
    from .tarp_tool.code.saturation_qc import scan_roi
    path, manifest, reference, shots = approved_shots(cfg)
    library = _library(Path(cfg['preparation']['tarp_reflectance_csv']))
    rows = []
    registration = {}
    for entry in read_json(manifest['transforms_json']):
        if entry['shot'] not in shots:
            continue
        band = _band_for_wavelength(float(entry['wavelength_nm']), cfg['band_names'])
        if entry.get('registration_hold'):
            registration.setdefault(band,[]).append(entry['shot'])
            continue
        for tarp in entry['native_rois']:
            outer = entry.get('native_outer_rois', {}).get(tarp)
            if outer is None:
                raise ValueError('전체 타프 경계가 없는 이전 ROI입니다. 03에서 ROI 통계를 다시 생성하세요.')
            result = scan_roi(entry['source_path'], outer, entry['native_image_size'], entry.get('sampling'))
            rows.append(dict(shot=entry['shot'], band=band, tarp=tarp,
                             fit=is_fit_roi(manifest, reference, tarp, library[_library_key(tarp)]), **result))
    included, excluded = [], {}
    for band in range(1, 11):
        fit = {r['tarp'] for r in rows if r['shot'] == reference and r['band'] == band
               and r['fit'] and r['saturated_pct'] < 1.0}
        validation = {_library_key(r['tarp']) for r in rows if r['shot'] != reference and r['band'] == band
                      and r['saturated_pct'] < 1.0}
        rho={float(library[_library_key(t)][f'B{band:02d}']) for t in fit}
        if band in registration:
            excluded[str(band)]='원본 밴드 정합 미통과: '+', '.join(registration[band])
        elif len(fit)<2 or len(rho)<2 or len(validation)<2:
            excluded[str(band)]=f'포화 검사 후 기준 타프 {len(fit)}종(서로 다른 실측값 {len(rho)}개), 독립 검증 타프 {len(validation)}종 · 각각 최소 2개 필요'
        else:included.append(band)
    if not included:
        raise ValueError('산출 가능한 밴드가 없습니다. '+'; '.join(f'B{b}: {reason}' for b,reason in excluded.items()))
    return rows,dict(included=included,excluded=excluded)


def check_raw_tarps(cfg):
    rows,scope=raw_tarp_scope(cfg)
    expected=cfg['preparation'].get('auto_band_scope')
    if expected is not None and scope!=expected:
        raise ValueError('검사 이후 정상 밴드 또는 제외 사유가 변경되었습니다. 새 실행으로 다시 검사하세요.')
    if expected is None and scope['excluded']:
        raise ValueError('10밴드 전부를 사용할 수 없습니다. 05에서 정상 밴드 자동 산출을 선택하세요. '+str(scope['excluded']))
    return rows


def pilot_evidence(cfg, record):
    if record.get('kind') == 'connected_partial_result_v1':
        from .completed_result import approval_evidence
        return approval_evidence(cfg,record)
    from .easy_elc import input_signature
    from .config import load_config
    from .preparation import locations
    from .elc_fit import _current_manifest
    from .elc_priority import checked_coefficients
    if record.get('status') != 'complete' or record.get('full'):
        raise ValueError('선택한 기준·검증 영상의 반사도를 먼저 산출하고 품질 보고서를 확인하세요.')
    if record['input_signature'] != input_signature(cfg):
        raise ValueError('검증 이후 입력·후보·교정값이 바뀌었습니다. 선택 영상 검증을 다시 진행하세요.')
    pilot = load_config(record['config'])
    manifest = _current_manifest(cfg)
    if str(manifest) != record['roi_manifest'] or digest(manifest) != record['roi_sha256']:
        raise ValueError('검증 이후 타프 ROI가 바뀌었습니다.')
    if input_signature(pilot) != record['input_signature']:
        raise ValueError('검증 실행 설정이 변경됐습니다.')
    checked_coefficients(pilot, pilot['preparation']['reference_shot'])
    qc,scope = raw_tarp_scope(cfg)
    if pilot['preparation'].get('auto_band_scope',{'included':list(range(1,11)),'excluded':{}})!=scope:
        raise ValueError('검증 실행과 현재 밴드 판정이 다릅니다.')
    table = locations(pilot)['coef'] / 'elc_priority_reference_validation.csv'
    with table.open(encoding='utf-8-sig') as f:
        rows = [r for r in csv.DictReader(f) if r['role'] == 'independent_validation']
    valid = {(r['shot'], r['band'], r['tarp']) for r in qc if r['saturated_pct'] < 1.0}
    summary = []
    for band in scope['included']:
        pairs = [r for r in rows if int(r['band']) == band and (r['shot'], band, r['tarp']) in valid]
        if len({tarp_library_key(r['tarp']) for r in pairs}) < 2:
            raise ValueError(f'B{band}: 유효한 독립 검증 타프가 2개 미만입니다.')
        errors = [float(r['observed_reflectance'])-float(r['measured_reflectance']) for r in pairs]
        if not all(math.isfinite(e) and int(r['n_pixels']) >= 5 for e, r in zip(errors, pairs)):
            raise ValueError(f'B{band}: 독립 검증 수치 또는 화소 수를 확인하세요.')
        summary.append(dict(band=band, rmse_pp=100*math.sqrt(sum(e*e for e in errors)/len(errors)),
                            bias_pp=100*sum(errors)/len(errors)))
    return dict(input_signature=record['input_signature'], roi_sha256=digest(manifest),
                config_sha256=digest(record['config']), coefficient_sha256=digest(pilot['preparation']['coefficient_csv']),
                table_sha256=digest(table), report_sha256=digest(record['report']), summary=summary)


def approve_pilot(cfg, record=None):
    from .easy_elc import pointer_path
    if record is None:
        record = read_json(pointer_path(cfg))
    proof = pilot_evidence(cfg, record)
    path = Path(cfg['_config_path']).with_suffix('.tarp-validation.json')
    atomic_json(path, dict(approved=True, pilot=record, evidence=proof))
    cfg['preparation']['tarp_validation_approval'] = str(path)
    return proof


def require_full_approval(cfg):
    value = cfg['preparation'].get('tarp_validation_approval')
    if not value or not Path(value).is_file():
        raise ValueError('전체 처리 잠금: 먼저 선택한 타프 영상의 반사도·독립 검증을 완료하고 «검증 결과 승인 · 전체 처리 허용»을 누르세요.')
    saved = read_json(value)
    if saved.get('approved') is not True or pilot_evidence(cfg, saved['pilot']) != saved['evidence']:
        raise ValueError('타프 검증 근거가 변경됐습니다. 검증 결과를 다시 확인·승인하세요.')
    return saved
