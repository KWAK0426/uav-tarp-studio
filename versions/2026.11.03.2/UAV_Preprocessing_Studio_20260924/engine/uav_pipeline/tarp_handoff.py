"""Import reviewed pilot inputs as unapproved DN boundaries, never calibration."""
import copy
import hashlib
import json
import time
from pathlib import Path
from .preparation import _gps_shots, initialize
from .tarp_workflow import context, defaults


def import_snapshot(cfg, path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    selection, refs = data['selection'], data['references']
    rows = selection['selected']
    draft = copy.deepcopy(cfg)
    defaults(draft)
    s = draft['preparation']
    raw = Path(s['raw_directory']).resolve()
    allowed = _gps_shots(s['gps_path'])
    if not rows or len({r['shot'] for r in rows}) != len(rows) or not {r['shot'] for r in rows}.issubset(allowed):
        raise ValueError('선택 영상이 현재 회차 GPS와 일치하지 않습니다.')
    photo_roots = set()
    for row in rows:
        if row.get('source_kind') != 'raw_DN_boundary_only':
            raise ValueError('이 가져오기는 명시된 DN 경계 시험 스냅샷용입니다.')
        files = [Path(p).resolve() for p in row.get('raw_files', [])]
        if len(files) != 10 or {p.stem.upper() for p in files} != {row['shot']+'_'+str(b) for b in range(1,11)}:
            raise ValueError('스냅샷에 10개 원영상 대응이 필요합니다.')
        if any(not p.is_relative_to(raw) or not p.is_file() for p in files):
            raise ValueError('현재 날짜·회차의 원영상 경로와 스냅샷이 다릅니다.')
        photo = Path(row['panel_photo']).resolve()
        if not photo.is_file() or photo.stem.upper() != row['id']:
            raise ValueError('스냅샷 Photos를 확인하세요.')
        photo_roots.add(str(photo.parent))
    if len(photo_roots) != 1:
        raise ValueError('여러 Photos 폴더를 자동 혼합하지 않습니다.')
    if not set(refs).issubset({r['shot'] for r in rows}) or len(refs) < 2:
        raise ValueError('선택 영상 중 최소 두 기준 ROI가 필요합니다.')
    for shot, tarps in refs.items():
        from osgeo import gdal
        source = next(r for r in rows if r['shot'] == shot)
        band6 = next(Path(p) for p in source['raw_files'] if Path(p).stem.upper() == shot+'_6')
        ds = gdal.Open(str(band6))
        width, height = ds.RasterXSize, ds.RasterYSize
        ds = None
        if set(tarps) != {'PNU_3', 'PNU_36', 'PNU_56'}:
            raise ValueError('현재 가져오기는 PNU 3종 경계용입니다.')
        for tarp in tarps.values():
            import numpy as np
            import cv2
            points = np.asarray(tarp.get('points'), dtype=np.float32)
            center = np.asarray([tarp.get('x'), tarp.get('y'), tarp.get('size')], dtype=float)
            if not np.isfinite(center).all() or not (0<=center[0]<width and 0<=center[1]<height and 2<=center[2]<=300):
                raise ValueError('타프 중심·크기를 확인하세요.')
            if points.shape != (4,2) or not np.isfinite(points).all() or not cv2.isContourConvex(points) or (points[:,0]<0).any() or (points[:,0]>=width).any() or (points[:,1]<0).any() or (points[:,1]>=height).any():
                raise ValueError('타프 사각형 경계를 확인하세요.')
    draft['tarp_workflow'].update(reference_mosaic=selection['mosaic'], photos_override=photo_roots.pop(),
                                   boundary_mode='원영상 ROI', imported_snapshot=str(path))
    c = context(draft)
    root = Path(c['output'])/'selections'
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    marker = root/('import_'+digest+'.json')
    if marker.exists():
        return draft, root
    name = 'selected_'+str(time.time_ns())+'.json'
    selected = copy.deepcopy(selection)
    selected.update(identity=c['identity'], source_kind='raw_DN_boundary_only', roi_confirmed=False,
                    trim_percent=0, imported_from=str(path), import_note='DN geometry only; no reflectance validation')
    (root/name).write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding='utf-8')
    for shot, tarps in refs.items():
        record = {'selection_file': name, 'reference': shot, 'tarps': tarps, 'roi_confirmed': False,
                  'source_kind': 'raw_DN_boundary_only', 'coordinate_basis': 'native_suffix6_pixels_registered_B2_B4_B6_B10',
                  'imported_from': str(path)}
        (root/('roi_history_'+str(time.time_ns())+'_'+shot+'.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    marker.write_text(json.dumps({'source': str(path), 'sha256': digest, 'selection_file': name}), encoding='utf-8')
    return draft, root
