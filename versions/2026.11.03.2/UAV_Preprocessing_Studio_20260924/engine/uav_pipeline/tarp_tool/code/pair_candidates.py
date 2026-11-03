"""Rank two-location photo pairs without treating them as simultaneous exposures."""
from datetime import datetime
from math import hypot


def rank_pairs(rows, centers, limit=60, trajectory=None):
    usable = [row for row in rows if row['radiance_ready']
              and row.get('saturation_qc', {}).get('status') != 'unknown']
    a = [row for row in usable if 'A' in row['coverage_locations']]
    b = [row for row in usable if 'B' in row['coverage_locations']]
    dx, dy = centers[1][0] - centers[0][0], centers[1][1] - centers[0][1]
    ground_distance = hypot(dx, dy)
    if ground_distance < 1:
        raise ValueError('두 타프 위치가 너무 가깝습니다. 한 위치 모드를 사용하세요.')
    route = trajectory or []
    path = {item['shot']: index for index, item in enumerate(route)}
    traveled = [0.0]
    for first, second in zip(route, route[1:]):
        traveled.append(traveled[-1] + hypot(second['xy'][0] - first['xy'][0],
                                             second['xy'][1] - first['xy'][1]))
    pairs = []
    for first in a:
        for second in b:
            if first['shot'] == second['shot'] and first['id'] != second['id']:
                continue
            try:
                t0 = datetime.fromisoformat(first['capture_time_utc'])
                t1 = datetime.fromisoformat(second['capture_time_utc'])
                dt = abs((t1 - t0).total_seconds())
            except (KeyError, ValueError, TypeError):
                dt = None
            x0, x1 = first.get('camera_xy'), second.get('camera_xy')
            if first['shot'] == second['shot']:
                alignment = 1.0
            elif x0 is not None and x1 is not None:
                cx, cy = x1[0] - x0[0], x1[1] - x0[1]
                length = hypot(cx, cy)
                alignment = abs((cx * dx + cy * dy) / (length * ground_distance)) if length >= 1 else None
            else:
                alignment = None
            directness = None
            if first['shot'] == second['shot']:
                directness = 1.0
            elif first['shot'] in path and second['shot'] in path:
                start, end = sorted((path[first['shot']], path[second['shot']]))
                travel = traveled[end] - traveled[start]
                directness = hypot(trajectory[end]['xy'][0] - trajectory[start]['xy'][0],
                                   trajectory[end]['xy'][1] - trajectory[start]['xy'][1]) / travel if travel else None
            route_match = alignment is not None and alignment >= .7 and (directness is None or directness >= .8)
            pairs.append({'a_id': first['id'], 'b_id': second['id'], 'a_shot': first['shot'],
                          'b_shot': second['shot'], 'delta_seconds': dt,
                          'route_alignment': round(alignment, 3) if alignment is not None else None,
                          'path_directness': round(directness, 3) if directness is not None else None,
                          'route_match': route_match,
                          'saturation_review': any(row['saturation_qc'].get('status') == 'review'
                                                   for row in (first, second)),
                          'same_frame': first['shot'] == second['shot']})
    pairs.sort(key=lambda p: (p['saturation_review'], not p['route_match'], p['delta_seconds'] is None,
                              p['delta_seconds'] if p['delta_seconds'] is not None else float('inf'),
                              p['a_shot'], p['b_shot']))
    return pairs[:limit]
