"""Reviewable raw-DN shadow candidates inside approved tarp polygons."""
import hashlib
import json
import re
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from aligned_rgb import CHANNELS, get_stack, preview
from review import folder, load, write_review


def trimmed_mask(shape, points, trim):
    points = np.asarray(points, dtype=np.float32)
    center = points.mean(axis=0)
    reduced = center + (1 - 2 * trim / 100) * (points - center)
    mask = np.zeros(shape, dtype=np.uint8)
    cv2.fillConvexPoly(mask, np.rint(reduced).astype(np.int32), 1)
    return mask.astype(bool)


def detect(stack, points, tarp, trim=5, sensitivity=3.0):
    """Suggest a spatial mask; percentile filtering is deliberately separate."""
    if not 0 <= trim <= 20 or not 1.5 <= sensitivity <= 6:
        raise ValueError('Trim 또는 그림자 민감도가 범위를 벗어났습니다.')
    core = trimmed_mask(stack.shape[1:], points, trim)
    if core.sum() < 20:
        raise ValueError('타프 내부 유효 화소가 너무 적습니다.')
    votes = np.zeros(core.shape, np.uint8)
    used = []
    clipped = {}
    for band in (4, 3, 6):
        image = stack[CHANNELS.index(band)]
        valid = core & np.isfinite(image) & (image != 65535)
        clipped[band] = float(np.mean(image[valid] >= 65520)) if valid.any() else 1.0
        if clipped[band] >= .01:
            continue
        values = image[valid]
        if values.size < 20:
            continue
        upper = values[values >= np.median(values)]
        center = float(np.median(upper))
        spread = max(1.4826 * float(np.median(np.abs(upper - center))), .02 * center, 1.0)
        votes += (valid & (image < center - sensitivity * spread)).astype(np.uint8)
        used.append(band)
    candidate = np.zeros(core.shape, np.uint8)
    if tarp in ('PNU_36', 'PNU_56') and len(used) >= 2:
        binary = (votes >= 2).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
        for label in range(1, count):
            if stats[label, cv2.CC_STAT_AREA] >= 4:
                candidate[labels == label] = 1
        candidate &= core.astype(np.uint8)
    fraction = float(candidate.sum() / core.sum())
    status = ('proposed' if len(used) >= 2 and fraction <= .25
              and tarp in ('PNU_36', 'PNU_56') else 'manual_review')
    return candidate * 255, {'status': status, 'used_bands': used,
                             'clipped_pct_by_band': {str(k): round(v * 100, 2) for k, v in clipped.items()},
                             'roi_pixels': int(core.sum()), 'excluded_pixels': int(candidate.sum()),
                             'excluded_pct': round(fraction * 100, 1)}


def _proposal(job, proposal_id):
    if not re.fullmatch(r'IMG_\d+_PNU_\d+_\d+', proposal_id):
        raise ValueError('Invalid pixel proposal')
    path = folder(job) / 'pixel_proposals' / proposal_id
    meta_path = path / 'proposal.json'
    if not meta_path.is_file():
        raise ValueError('내부 화소 후보가 없습니다. 다시 계산하세요.')
    return path, json.loads(meta_path.read_text(encoding='utf-8'))


def propose(data):
    job, shot, tarp = data['job_id'], data['shot'], data['tarp']
    review = load(job)
    if shot not in review['images'] or tarp not in review['images'][shot]['tarps']:
        raise ValueError('영상·타프 이름 불일치')
    record = review['images'][shot]
    if not record.get('approved'):
        raise ValueError('먼저 해당 영상의 ROI를 승인하세요.')
    trim = float(data.get('trim_percent', 5))
    sensitivity = float(data.get('sensitivity', 3))
    stack = get_stack(shot)
    mask, result = detect(stack, record['tarps'][tarp], tarp, trim, sensitivity)
    proposal_id = f'{shot}_{tarp}_{time.time_ns()}'
    path = folder(job) / 'pixel_proposals' / proposal_id
    path.mkdir(parents=True)
    Image.fromarray(mask).save(path / 'exclude.png')
    info = dict(result, proposal_id=proposal_id, shot=shot, tarp=tarp,
                trim_percent=trim, sensitivity=sensitivity,
                polygon_saved_at_ns=record['saved_at_ns'], points=record['tarps'][tarp],
                image_size=[mask.shape[1], mask.shape[0]], approved=False)
    (path / 'proposal.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    return info


def edit(data):
    path, info = _proposal(data['job_id'], data['proposal_id'])
    if info['approved']:
        raise ValueError('승인된 마스크는 수정할 수 없습니다. 새 후보를 계산하세요.')
    mask = np.asarray(Image.open(path / 'exclude.png').convert('L')).copy()
    x, y = int(round(float(data['x']))), int(round(float(data['y'])))
    radius = int(data.get('radius', 4))
    if not 1 <= radius <= 20 or not (0 <= x < mask.shape[1] and 0 <= y < mask.shape[0]):
        raise ValueError('수정 위치 또는 브러시 크기가 유효하지 않습니다.')
    brush = np.zeros(mask.shape, np.uint8)
    cv2.circle(brush, (x, y), radius, 1, -1)
    core = trimmed_mask(mask.shape, info['points'], info['trim_percent'])
    mask[(brush > 0) & core] = 255 if data.get('exclude') else 0
    Image.fromarray(mask).save(path / 'exclude.png')
    info['excluded_pixels'] = int(np.count_nonzero(mask))
    info['excluded_pct'] = round(100 * info['excluded_pixels'] / info['roi_pixels'], 1)
    (path / 'proposal.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    return info


def overlay(job, proposal_id):
    path, info = _proposal(job, proposal_id)
    image = preview(get_stack(info['shot']), band='4')
    mask = np.asarray(Image.open(path / 'exclude.png').convert('L')) > 0
    image[mask] = (.35 * image[mask] + .65 * np.array([255, 70, 65])).astype(np.uint8)
    result = Image.fromarray(image)
    draw = ImageDraw.Draw(result)
    points = [tuple(p) for p in info['points']]
    draw.line(points + points[:1], fill='#00d4d4', width=2)
    return result


def approve(data):
    path, info = _proposal(data['job_id'], data['proposal_id'])
    current = load(data['job_id'])
    record = current['images'][info['shot']]
    if not record.get('approved') or record['saved_at_ns'] != info['polygon_saved_at_ns']:
        raise ValueError('ROI 경계가 바뀌었습니다. 내부 화소 후보를 다시 계산하세요.')
    mask_path = path / 'exclude.png'
    mask = np.asarray(Image.open(mask_path).convert('L'))
    if list(mask.shape[::-1]) != info['image_size']:
        raise ValueError('제외 마스크 크기가 원영상과 다릅니다.')
    digest = hashlib.sha256(mask_path.read_bytes()).hexdigest()
    record.setdefault('pixel_masks', {})[info['tarp']] = {
        'path': str(mask_path), 'sha256': digest, 'trim_percent': info['trim_percent'],
        'polygon_saved_at_ns': info['polygon_saved_at_ns'],
        'excluded_pixels': int(np.count_nonzero(mask)), 'roi_pixels': info['roi_pixels'],
        'status': info['status'], 'approved_at_ns': time.time_ns(),
    }
    info['approved'] = True
    (path / 'proposal.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    write_review(folder(data['job_id']), current, 'pixel_mask')
    return {'shot': info['shot'], 'tarp': info['tarp'], 'excluded_pixels': int(np.count_nonzero(mask)),
            'roi_pixels': info['roi_pixels'], 'status': info['status']}
