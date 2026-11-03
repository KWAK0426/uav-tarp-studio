"""Explicit ROI review; approval is separate from tracking confidence."""
import json
import re
import time
from datetime import datetime
from pathlib import Path
import cv2
import numpy as np
from osgeo import gdal
from context import ROOT, CONFIG, index_files
from tracking_jobs import tarps_for_shot


def automatic_reference(images):
    timing = CONFIG.get('reference_timing', {})
    midpoint = timing.get('observation_midpoint_utc')
    capture = timing.get('capture_time_utc_by_shot', {})
    if not midpoint:
        return ''
    middle = datetime.fromisoformat(midpoint)
    candidates = [(abs((datetime.fromisoformat(capture[shot]) - middle).total_seconds()), shot)
                  for shot in images if shot in capture]
    return min(candidates)[1] if candidates else ''


def folder(job):
    if not isinstance(job, str) or not re.fullmatch(r'[0-9_]+', job):
        raise ValueError('Invalid tracking job')
    p = ROOT/'web'/'tracking'/job
    if not (p/'tracking_results.json').is_file():
        raise ValueError('완료된 추적 결과가 필요합니다.')
    return p


def load(job):
    p = folder(job)
    snap = json.loads((p/'input_snapshot.json').read_text(encoding='utf-8'))
    result = json.loads((p/'tracking_results.json').read_text(encoding='utf-8'))
    if snap['context']['identity'] != CONFIG['identity']:
        raise ValueError('현재 입력과 추적 결과가 다릅니다.')
    saved = json.loads((p/'review_latest.json').read_text(encoding='utf-8')) if (p/'review_latest.json').exists() else {}
    images = {}
    selection = snap['selection']
    for shot, item in result['results'].items():
        points = {n: r.get('points') for n,r in item['tarps'].items()}
        basis = 'tracking_proposal'
        if shot in snap['references']:
            points = {n: r.get('points') for n,r in snap['references'][shot].items()}
            basis = 'saved_manual_reference'
        images[shot] = saved.get('images', {}).get(shot, {'tarps': points, 'approved': False, 'basis': basis})
        images[shot]['expected_tarps'] = tarps_for_shot(selection, shot)
    saved_reference = saved.get('reference_shot', '')
    reference = saved_reference or automatic_reference(images)
    selection = saved.get('reference_selection') or ('automatic_observation_midpoint' if reference else 'manual_required')
    return {'job_id':job, 'images':images, 'tarps':CONFIG['tarps'], 'source_kind':CONFIG['source_kind'],
            'reference_shot':reference,
            'reference_selection':selection,
            'reference_error':CONFIG.get('reference_timing', {}).get('error', ''), 'label':CONFIG['label']}


def validate_polygons(shot, tarps, expected):
    if not isinstance(tarps, dict) or set(tarps) != set(expected):
        raise ValueError('영상·타프 이름 불일치')
    files = index_files(CONFIG['image_source'])
    ds = gdal.Open(str(files[shot+'_6']))
    if ds is None:
        raise ValueError(shot + ': 원영상을 열 수 없습니다.')
    width,height = ds.RasterXSize,ds.RasterYSize
    polygons = []
    for name, points in tarps.items():
        a = np.asarray(points, dtype=np.float32)
        if a.shape != (4,2) or not np.isfinite(a).all() or not cv2.isContourConvex(a) or abs(cv2.contourArea(a))<1:
            raise ValueError(name+': 유효한 네 꼭짓점이 필요합니다.')
        if (a[:,0]<0).any() or (a[:,0]>=width).any() or (a[:,1]<0).any() or (a[:,1]>=height).any():
            raise ValueError(name+': 영상 범위를 벗어났습니다.')
        if any(cv2.intersectConvexConvex(a,b)[0] > .5 for b in polygons):
            raise ValueError('타프 ROI가 겹칩니다.')
        polygons.append(a)
    ds = None


def write_review(p, current, label):
    out = p/('review_history_'+str(time.time_ns())+'_'+label+'.json')
    out.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp = p/'review_latest.tmp'
    tmp.write_bytes(out.read_bytes())
    tmp.replace(p/'review_latest.json')
    return current


def save(data):
    p = folder(data['job_id'])
    current = load(data['job_id'])
    shot = data['shot']
    if shot not in current['images']:
        raise ValueError('영상·타프 이름 불일치')
    validate_polygons(shot, data['tarps'], current['images'][shot]['expected_tarps'])
    previous = current['images'][shot]
    unchanged = previous['tarps'] == data['tarps']
    current['images'][shot] = {'tarps':data['tarps'], 'approved':data.get('approved') is True,
                               'basis':'user_reviewed_native_suffix6',
                               'saved_at_ns':previous.get('saved_at_ns') if unchanged and previous.get('saved_at_ns') else time.time_ns()}
    if unchanged and data.get('approved') is True and previous.get('pixel_masks'):
        current['images'][shot]['pixel_masks'] = previous['pixel_masks']
    reference = data.get('reference_shot','')
    if reference and reference not in current['images']:
        raise ValueError('기준 영상이 선택 목록에 없습니다.')
    if reference != current.get('reference_shot'):
        current['reference_selection'] = 'user_saved'
    current['reference_shot'] = reference
    return write_review(p, current, shot)


def approve_all(data):
    p = folder(data['job_id'])
    current = load(data['job_id'])
    shot = data.get('shot')
    if shot not in current['images']:
        raise ValueError('현재 영상이 선택 목록에 없습니다.')
    reference = data.get('reference_shot') or current['reference_shot']
    if reference not in current['images']:
        raise ValueError('ELC·Ed 기준 영상을 하나 선택하세요.')
    for name, item in current['images'].items():
        validate_polygons(name, data['tarps'] if name == shot else item['tarps'], item['expected_tarps'])
    for name, item in current['images'].items():
        if name == shot:
            changed = item['tarps'] != data['tarps']
            item['tarps'] = data['tarps']
            item['basis'] = 'user_reviewed_native_suffix6'
        else:
            changed = False
        if name != shot and not item.get('approved'):
            item['basis'] = 'user_bulk_approved_native_suffix6'
        item['approved'] = True
        if changed or not item.get('saved_at_ns'):
            item['saved_at_ns'] = time.time_ns()
            item.pop('pixel_masks', None)
    if reference != current['reference_shot']:
        current['reference_selection'] = 'user_saved'
    current['reference_shot'] = reference
    return write_review(p, current, 'bulk')


def statistics_snapshot(job, trim, percentile_low=5, percentile_high=95):
    current = load(job)
    if not all(v['approved'] for v in current['images'].values()):
        raise ValueError('모든 선택 영상의 ROI를 검수·승인하세요.')
    if current['reference_shot'] not in current['images']:
        raise ValueError('ELC·Ed 기준 영상을 하나 선택하세요.')
    trim = float(trim)
    if not np.isfinite(trim) or not 0<=trim<=20:
        raise ValueError('Trim은 0~20%입니다.')
    percentile_low, percentile_high = float(percentile_low), float(percentile_high)
    if not np.isfinite(percentile_low) or not np.isfinite(percentile_high) or not 0 <= percentile_low < percentile_high <= 100:
        raise ValueError('하위·상위 백분위는 0 <= 하위 < 상위 <= 100이어야 합니다.')
    for shot, record in current['images'].items():
        for tarp in ('PNU_36', 'PNU_56'):
            if tarp not in record['tarps']:
                continue
            mask = record.get('pixel_masks', {}).get(tarp)
            if not mask or mask.get('polygon_saved_at_ns') != record.get('saved_at_ns') or mask.get('trim_percent') != trim:
                raise ValueError(f'{shot} {tarp}: ROI 내부 화소를 검사·승인한 뒤 통계를 생성하세요. Trim 값도 같아야 합니다.')
    return dict(current, context=CONFIG, trim_percent=trim, trim_method='centroid_scale_1_minus_2f',
                percentile_low=percentile_low, percentile_high=percentile_high,
                pixel_mask_review_version=1, confirmed=True)
