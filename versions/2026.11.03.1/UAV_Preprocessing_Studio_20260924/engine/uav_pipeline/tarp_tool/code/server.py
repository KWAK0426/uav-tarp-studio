from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import threading
import time
import io
from datetime import datetime, timedelta, timezone
import numpy as np
from osgeo import gdal, osr
from PIL import Image

gdal.UseExceptions()
from context import CONFIG, ROOT, index_files
if __name__ == '__main__':
    import msvcrt
    SERVER_GUARD = (ROOT/'server.lock').open('a+b')
    if SERVER_GUARD.tell() == 0:
        SERVER_GUARD.write(b'0'); SERVER_GUARD.flush()
    SERVER_GUARD.seek(0)
    msvcrt.locking(SERVER_GUARD.fileno(), msvcrt.LK_NBLCK, 1)
import subprocess
import sys
import os
import tracking_jobs
from pair_candidates import rank_pairs
from saturation_qc import QC_VERSION, SATURATION_DN, scan_frame
MOSAIC = Path(CONFIG['mosaic'])
PHOTOS = Path(CONFIG['photos'])
RADIANCE = Path(CONFIG.get('image_source', CONFIG['radiance']))
RAW_ROOT = Path(CONFIG['raw'])
RAW_FILES = index_files(RAW_ROOT)
RAD_FILES = index_files(RADIANCE)
def source_path(shot, band):
    return RAD_FILES.get(f'{shot}_{band}', RADIANCE/f'{shot}_{band}.tif')
import re
allowed = set(CONFIG['gps_shots'])
FILES = {k:p for k,p in index_files(PHOTOS).items() if re.fullmatch(r'IMG_\d{4}_(?:10|[1-9])', k) and k.rsplit('_',1)[0] in allowed}
if not FILES:
    raise ValueError('GPS와 일치하는 해당 회차 Photos가 없습니다.')
for folder in ['cache', 'selections']:
    (ROOT / folder).mkdir(exist_ok=True)
STATE = {'running': False, 'done': 0, 'total': len(FILES), 'candidates': [], 'pairs': [],
         'errors': [], 'point': None, 'locations': [], 'selected_ids': []}
LOCK = threading.Lock()
previous = ROOT/'selections/last_search.json'
if previous.exists():
    saved_state = json.loads(previous.read_text(encoding='utf-8'))
    if (saved_state.get('coverage_rule') == 'full_circle_all_10_bands_conservative'
            and saved_state.get('saturation_qc_version') == QC_VERSION):
        STATE.update(saved_state, running=False)
        if not STATE.get('locations') and STATE.get('point'):
            STATE['locations'] = [{'id': 'A', 'point': STATE['point'], 'radius_m': STATE.get('radius_m', 12),
                                   'tarps': CONFIG['tarps']}]
        for row in STATE['candidates']:
            row['radiance_ready'] = all(source_path(row['shot'], b).is_file() for b in range(1, 11))
            row['source_kind'] = CONFIG['source_kind']
            row['raw_files'] = [str(source_path(row['shot'], b)) for b in range(1, 11)]

def srs(wkt):
    s = osr.SpatialReference()
    s.SetFromUserInput(wkt)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s

def transform(x, y, a, b):
    return osr.CoordinateTransformation(srs(a), srs(b)).TransformPoint(x, y)[:2]

def locate(ds, point):
    x, y = transform(point['x'], point['y'], point['crs'], ds.GetProjection())
    px, py = gdal.ApplyGeoTransform(gdal.InvGeoTransform(ds.GetGeoTransform()), x, y)
    if not 0 <= px < ds.RasterXSize or not 0 <= py < ds.RasterYSize:
        return None
    b = ds.GetRasterBand(1)
    value = b.ReadAsArray(int(px), int(py), 1, 1)[0, 0]
    mask = b.GetMaskBand().ReadAsArray(int(px), int(py), 1, 1)[0, 0]
    if not mask or not np.isfinite(value) or value == 65535 or (b.GetNoDataValue() is not None and value == b.GetNoDataValue()):
        return None
    if ds.RasterCount == 11 and ds.GetRasterBand(11).ReadAsArray(int(px), int(py), 1, 1)[0, 0] <= 0:
        return None
    return px, py

def crop_window(ds, point, radius):
    x, y = transform(point['x'], point['y'], point['crs'], 'EPSG:32652')
    pts = [transform(x+dx, y+dy, 'EPSG:32652', ds.GetProjection()) for dx in [-radius, radius] for dy in [-radius, radius]]
    xy = [gdal.ApplyGeoTransform(gdal.InvGeoTransform(ds.GetGeoTransform()), *p) for p in pts]
    x0 = max(0, int(np.floor(min(p[0] for p in xy))))
    y0 = max(0, int(np.floor(min(p[1] for p in xy))))
    x1 = min(ds.RasterXSize, int(np.ceil(max(p[0] for p in xy))))
    y1 = min(ds.RasterYSize, int(np.ceil(max(p[1] for p in xy))))
    return x0, y0, x1-x0, y1-y0

def contains_radius(ds, point, radius):
    """Conservative full-circle coverage, including cells touching its boundary."""
    metric = 'EPSG:32652'
    cx, cy = transform(point['x'], point['y'], point['crs'], metric)
    inv = gdal.InvGeoTransform(ds.GetGeoTransform())
    # An enclosing square also rejects uncertain coverage at raster edges.
    corners = [transform(cx+dx, cy+dy, metric, ds.GetProjection())
               for dx in [-radius, radius] for dy in [-radius, radius]]
    pixels = [gdal.ApplyGeoTransform(inv, *p) for p in corners]
    x0 = int(np.floor(min(p[0] for p in pixels))) - 1
    y0 = int(np.floor(min(p[1] for p in pixels))) - 1
    x1 = int(np.ceil(max(p[0] for p in pixels))) + 1
    y1 = int(np.ceil(max(p[1] for p in pixels))) + 1
    if x0 < 0 or y0 < 0 or x1 > ds.RasterXSize or y1 > ds.RasterYSize:
        return False
    gt = ds.GetGeoTransform()
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(float)
    xx += .5
    yy += .5
    wx = gt[0] + xx*gt[1] + yy*gt[2]
    wy = gt[3] + xx*gt[4] + yy*gt[5]
    tx = osr.CoordinateTransformation(srs(ds.GetProjection()), srs(metric))
    xy = np.asarray(tx.TransformPoints(np.column_stack([wx.ravel(), wy.ravel()]).tolist()))[:, :2]
    xy = xy.reshape(wx.shape + (2,))
    horizontal = np.linalg.norm(np.diff(xy, axis=1), axis=2).max()
    vertical = np.linalg.norm(np.diff(xy, axis=0), axis=2).max()
    margin = (horizontal + vertical) / 2
    inside = ((xy[:,:,0]-cx)**2 + (xy[:,:,1]-cy)**2) <= (radius+margin)**2
    if not inside.any():
        return False
    win = (x0, y0, x1-x0, y1-y0)
    for n in range(1, min(ds.RasterCount, 10)+1):
        b = ds.GetRasterBand(n)
        a = b.ReadAsArray(*win)[inside]
        if not np.isfinite(a).all() or np.any(a == 65535):
            return False
        if b.GetNoDataValue() is not None and np.any(a == b.GetNoDataValue()):
            return False
        if not np.all(b.GetMaskBand().ReadAsArray(*win)[inside] > 0):
            return False
    if ds.RasterCount == 11:
        alpha = ds.GetRasterBand(11).ReadAsArray(*win)[inside]
        if not np.isfinite(alpha).all() or not np.all(alpha > 0):
            return False
    return True

def png(ds, bands, window=None, maxsize=1600, mosaic_display=False):
    win = window or (0, 0, ds.RasterXSize, ds.RasterYSize)
    scale = min(1, maxsize/max(win[2:]))
    w, h = max(1, round(win[2]*scale)), max(1, round(win[3]*scale))
    valid = ds.GetRasterBand(1).GetMaskBand().ReadAsArray(*win, buf_xsize=w, buf_ysize=h) > 0
    if ds.RasterCount == 11:
        valid &= ds.GetRasterBand(11).ReadAsArray(*win, buf_xsize=w, buf_ysize=h) > 0
    channels = []
    for n in bands:
        b = ds.GetRasterBand(n)
        arr = b.ReadAsArray(*win, buf_xsize=w, buf_ysize=h)
        ok = valid & np.isfinite(arr) & (arr != 65535)
        if b.GetNoDataValue() is not None:
            ok &= arr != b.GetNoDataValue()
        limits = [1, 96] if mosaic_display else [2, 98]
        lo, hi = np.percentile(arr[ok], limits) if ok.any() else (0, 1)
        z = np.nan_to_num(np.clip((arr-lo)/max(hi-lo, 1e-9), 0, 1))
        if mosaic_display:
            z = z ** 0.55
        z *= 255
        z[~ok] = 0
        channels.append(z.astype('uint8'))
    im = Image.fromarray(channels[0] if len(channels)==1 else np.stack(channels, axis=2))
    buf = io.BytesIO()
    im.save(buf, format='PNG')
    return buf.getvalue()

def search(locations):
    rows, errors = [], []
    try:
        RAD_FILES.clear();RAD_FILES.update(index_files(RADIANCE))
    except Exception as exc:
        with LOCK: STATE.update(running=False,errors=[{'error':str(exc)}])
        return
    for i, (key, path) in enumerate(FILES.items()):
        try:
            ds = gdal.Open(str(path))
            covered = [loc for loc in locations if locate(ds, loc['point'])
                       and contains_radius(ds, loc['point'], loc['radius_m'])]
            if covered:
                first = covered[0]
                window = crop_window(ds, first['point'], first['radius_m'])
                shot = key.rsplit('_', 1)[0]
                available = all(source_path(shot, b).is_file() for b in range(1, 11))
                raw_paths = [RAW_FILES.get(f'{shot}_{b}') for b in range(1, 11)]
                qc = scan_frame(raw_paths)
                timing = CONFIG.get('reference_timing', {}).get('trajectory_by_shot', {}).get(shot, {})
                camera_xy = None
                if 'longitude' in timing and 'latitude' in timing:
                    camera_xy = transform(timing['longitude'], timing['latitude'], 'EPSG:4326', 'EPSG:32652')
                capture_kst = (datetime.fromisoformat(timing['capture_time_utc'])
                               .astimezone(timezone(timedelta(hours=9))).isoformat()
                               if timing.get('capture_time_utc') else None)
                rows.append({'id': key, 'shot': shot, 'panel_photo': str(path), 'radiance_ready': available,
                             'source_kind': CONFIG['source_kind'], 'source_files': [str(source_path(shot, b)) for b in range(1,11)],
                             'window': window, 'point_pixel': locate(ds, first['point']),
                             'coverage_locations': [loc['id'] for loc in covered],
                             'capture_time_utc': timing.get('capture_time_utc'), 'capture_time_kst': capture_kst,
                             'camera_xy': camera_xy,
                             'saturation_qc': qc})
            ds = None
        except Exception as exc:
            errors.append({'id': key, 'error': str(exc)})
        with LOCK:
            STATE.update(done=i+1)
    with LOCK:
        history = ROOT/'selections'/f'saturation_search_{time.time_ns()}.json'
        centers = [transform(loc['point']['x'], loc['point']['y'], loc['point']['crs'], 'EPSG:32652')
                   for loc in locations]
        pairs = []
        if len(locations) == 2:
            route = []
            for shot, item in CONFIG.get('reference_timing', {}).get('trajectory_by_shot', {}).items():
                if 'longitude' in item and 'latitude' in item and item.get('capture_time_utc'):
                    route.append({'shot': shot, 'time': item['capture_time_utc'],
                                  'xy': transform(item['longitude'], item['latitude'], 'EPSG:4326', 'EPSG:32652')})
            route.sort(key=lambda item: item['time'])
            pairs = rank_pairs(rows, centers, trajectory=route)
        history.write_text(json.dumps({'locations': locations, 'mosaic': str(MOSAIC),
            'scope': 'full_native_frame_preliminary', 'threshold_dn': SATURATION_DN,
            'candidates': rows, 'pairs': pairs, 'errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
        STATE.update(candidates=rows, pairs=pairs, errors=errors, running=False,
                     coverage_rule='full_circle_all_10_bands_conservative',
                     saturation_qc_version=QC_VERSION, saturation_history=str(history))
        (ROOT/'selections/last_search.json').write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding='utf-8')

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT/'web'), **kwargs)
    def reply(self, value, kind='application/json; charset=utf-8', code=200):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == '/state':
                with LOCK:
                    return self.reply(dict(STATE))
            if url.path == '/context':
                return self.reply({k: CONFIG[k] for k in ('identity','label','mosaic','mosaic_role','tarps','source_kind','candidate_preview_kind','output','reference_timing','code_revision')})
            if url.path == '/route':
                ds = gdal.Open(str(MOSAIC))
                inv = gdal.InvGeoTransform(ds.GetGeoTransform())
                path = []
                for shot, row in CONFIG.get('reference_timing', {}).get('trajectory_by_shot', {}).items():
                    x, y = transform(row['longitude'], row['latitude'], 'EPSG:4326', ds.GetProjection())
                    px, py = gdal.ApplyGeoTransform(inv, x, y)
                    path.append({'shot': shot, 'u': px/ds.RasterXSize, 'v': py/ds.RasterYSize,
                                 'capture_time_utc': row['capture_time_utc']})
                path.sort(key=lambda row: row['capture_time_utc'])
                return self.reply(path)
            if url.path == '/band_labels':
                import tifffile
                import xml.etree.ElementTree as ET
                sample=next(iter(FILES)).rsplit('_',1)[0]
                labels=[]
                for b in range(1,11):
                    raw=RAW_FILES.get(f'{sample}_{b}')
                    wave=None
                    if raw:
                        with tifffile.TiffFile(raw) as tf:
                            tag=tf.pages[0].tags.get('XMP')
                            if tag:
                                wave=next((e.text for e in ET.fromstring(tag.value).iter() if e.tag.endswith('}CentralWavelength')),None)
                    labels.append(f'{b} · {wave}nm' if wave else f'{b} · 파장 미확인')
                return self.reply(labels)
            if url.path == '/tracking/status':
                return self.reply(tracking_jobs.status())
            if url.path == '/review/data':
                from review import load
                return self.reply(load(q['job'][0]))
            if url.path == '/review/pixels/preview':
                from pixel_exclusions import overlay
                buf = io.BytesIO()
                overlay(q['job'][0], q['proposal'][0]).save(buf, format='PNG')
                return self.reply(buf.getvalue(), 'image/png')
            if url.path == '/selection/latest':
                paths = sorted((ROOT/'selections').glob('selected_*.json'))
                if not paths:
                    raise ValueError('먼저 후보 영상을 저장하세요.')
                data = json.loads(paths[-1].read_text(encoding='utf-8'))
                for row in data.get('selected', []):
                    row['radiance_ready'] = all(source_path(row['shot'], b).is_file() for b in range(1, 11))
                    row['source_kind'] = CONFIG['source_kind']
                    row['source_files'] = [str(source_path(row['shot'], b)) for b in range(1, 11)]
                data['source_kind'] = CONFIG['source_kind']
                data['radiance_directory'] = str(RADIANCE)
                data['selection_file'] = paths[-1].name
                roi = ROOT/'selections'/('roi_seeds_'+paths[-1].stem+'.json')
                if roi.exists(): data['seeds'] = json.loads(roi.read_text(encoding='utf-8'))
                data['references'] = tracking_jobs.references(paths[-1].name)
                return self.reply(data)
            if url.path == '/radiance.png':
                shot = q['shot'][0]
                import re
                if not re.fullmatch(r'IMG_\d+', shot): raise ValueError('Invalid shot')
                band = q.get('band', ['rgb'])[0].lower()
                if band not in ('rgb', '2', '3', '4', '6', '10'): raise ValueError('Invalid display band')
                from aligned_rgb import get_stack, preview
                buf = io.BytesIO()
                Image.fromarray(preview(get_stack(shot), band)).save(buf, format='PNG')
                return self.reply(buf.getvalue(), 'image/png')
            if url.path == '/circle':
                u, v, radius = [float(q[k][0]) for k in ['u', 'v', 'radius']]
                if not (0 <= u <= 1 and 0 <= v <= 1 and 2 <= radius <= 50):
                    raise ValueError('Invalid circle')
                ds = gdal.Open(str(MOSAIC))
                x, y = gdal.ApplyGeoTransform(ds.GetGeoTransform(), u*ds.RasterXSize, v*ds.RasterYSize)
                cx, cy = transform(x, y, ds.GetProjection(), 'EPSG:32652')
                inv = gdal.InvGeoTransform(ds.GetGeoTransform())
                points = []
                for a in np.linspace(0, 2*np.pi, 121):
                    wx, wy = transform(cx+radius*np.cos(a), cy+radius*np.sin(a), 'EPSG:32652', ds.GetProjection())
                    px, py = gdal.ApplyGeoTransform(inv, wx, wy)
                    points.append([px/ds.RasterXSize*1000, py/ds.RasterYSize*1000])
                return self.reply(points)
            if url.path == '/mosaic.png':
                cache = ROOT/'cache/panel_mosaic.png'
                if not cache.exists():
                    ds=gdal.Open(str(MOSAIC))
                    if not ds.GetProjection(): raise ValueError('위치 참조 모자이크에 좌표계가 없습니다.')
                    bands=[6,4,2] if ds.RasterCount>=10 else [1,2,3] if ds.RasterCount>=3 else [1]
                    cache.write_bytes(png(ds, bands, maxsize=2400, mosaic_display=True))
                return self.reply(cache.read_bytes(), 'image/png')
            if url.path == '/crop.png':
                key = q['id'][0]
                with LOCK:
                    row = next(r for r in STATE['candidates'] if r['id']==key)
                band = int(q.get('band', ['6'])[0])
                if band not in range(1, 11):
                    raise ValueError('Invalid band')
                if CONFIG.get('candidate_preview_kind') == 'panel_photos':
                    source = Path(row['panel_photo'])
                    dataset = gdal.Open(str(source))
                    preview_band = band if dataset and dataset.RasterCount >= band else 1
                else:
                    source = source_path(row['shot'], band)
                    dataset = gdal.Open(str(source))
                    preview_band = 1
                if dataset is None:
                    raise ValueError('후보 미리보기 영상을 읽지 못했습니다.')
                return self.reply(png(dataset, [preview_band]), 'image/png')
            return super().do_GET()
        except Exception as exc:
            return self.reply({'error': str(exc)}, code=400)
    def do_POST(self):
        with tracking_jobs.LOCK:
            return self.handle_post()

    def handle_post(self):
        try:
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                raise ValueError('Cross-origin request rejected')
            if int(self.headers.get('Content-Length', 0)) > 2000000:
                raise ValueError('Request too large')
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/tracking/start':
                paths=sorted((ROOT/'selections').glob('selected_*.json'))
                if not paths: raise ValueError('후보 영상을 먼저 저장하세요.')
                name = data.get('selection_file', paths[-1].name)
                return self.reply(tracking_jobs.start(name))
            if self.path == '/tracking/cancel':
                return self.reply(tracking_jobs.cancel(data.get('job_id')))
            if self.path == '/review/statistics':
                return self.reply(tracking_jobs.start_statistics(data.get('job_id'),data.get('trim_percent',5),
                    data.get('percentile_low',5),data.get('percentile_high',95)))
            if tracking_jobs.status()['running']:
                return self.reply({'error':'추적 중에는 선택·ROI를 변경할 수 없습니다.'},code=409)
            if self.path == '/review/save':
                from review import save
                return self.reply(save(data))
            if self.path == '/review/approve-all':
                from review import approve_all
                return self.reply(approve_all(data))
            if self.path == '/review/pixels/propose':
                from pixel_exclusions import propose
                return self.reply(propose(data))
            if self.path == '/review/pixels/edit':
                from pixel_exclusions import edit
                return self.reply(edit(data))
            if self.path == '/review/pixels/approve':
                from pixel_exclusions import approve
                return self.reply(approve(data))
            if self.path == '/roi/boundaries':
                from seed_boundaries import find_multiband_boundaries
                from aligned_rgb import get_stack
                name=data['selection_file']
                if Path(name).name!=name or not name.startswith('selected_'):raise ValueError('Invalid selection')
                sel=json.loads((ROOT/'selections'/name).read_text(encoding='utf-8'))
                if data['reference'] not in [r['shot'] for r in sel['selected']]:raise ValueError('Invalid reference')
                seeds=data.get('tarps',{})
                if not seeds:raise ValueError('타프 중심을 먼저 찍어주세요.')
                if not set(seeds).issubset(tracking_jobs.tarps_for_shot(sel, data['reference'])):
                    raise ValueError('이 영상에 배정되지 않은 타프입니다.')
                for seed in seeds.values():
                    if not all(np.isfinite(seed[k]) for k in ['x','y','size']) or not 2<=seed['size']<=300:raise ValueError('Invalid seed')
                result=find_multiband_boundaries(get_stack(data['reference']),seeds,raw_dn=CONFIG['source_kind']=='raw_DN_boundary_only')
                basis = 'native_suffix6_pixels_raw_geometry_B2_B3_B4_B6_B10' if CONFIG['source_kind']=='raw_DN_boundary_only' else 'native_suffix6_pixels_registered_radiance_B2_B3_B4_B6_B10'
                return self.reply({'results':result,'coordinate_basis':basis,'confirmed':False})
            if self.path == '/roi/propose':
                raise ValueError('타프 중심을 직접 찍고 RGB·다중밴드 경계 찾기를 사용하세요. Photos와 원영상의 밴드 순서가 다를 수 있습니다.')
                import cv2
                name = data['selection_file']
                if Path(name).name != name or not name.startswith('selected_'): raise ValueError('Invalid selection')
                sel = json.loads((ROOT/'selections'/name).read_text(encoding='utf-8'))
                row = next(r for r in sel['selected'] if r['shot']==data['reference'])
                photo = gdal.Open(str(FILES[row['id']]))
                rad = gdal.Open(str(source_path(row['shot'], 6)))
                def gray(ds, band):
                    a=ds.GetRasterBand(band).ReadAsArray(); valid=np.isfinite(a)&(a!=65535)
                    if ds.RasterCount==11: valid &= ds.GetRasterBand(11).ReadAsArray()>0
                    lo,hi=np.percentile(a[valid],[2,98]);return (np.nan_to_num(np.clip((a-lo)/max(hi-lo,1e-9),0,1))*255).astype('uint8')
                a,b=gray(photo,6),gray(rad,1)
                sift=cv2.SIFT_create(nfeatures=5000)
                ka,da=sift.detectAndCompute(a,None);kb,db=sift.detectAndCompute(b,None)
                if da is None or db is None: raise ValueError('특징점 부족: 중심을 직접 지정하세요.')
                matches=[m for pair in cv2.BFMatcher().knnMatch(da,db,k=2) if len(pair)==2 for m,n in [pair] if m.distance<.7*n.distance]
                if len(matches)<12: raise ValueError('영상 매칭 부족: 중심을 직접 지정하세요.')
                h,inliers=cv2.findHomography(np.float32([ka[m.queryIdx].pt for m in matches]),np.float32([kb[m.trainIdx].pt for m in matches]),cv2.RANSAC,3)
                if h is None or int(inliers.sum())<12: raise ValueError('위치 연결 실패: 중심을 직접 지정하세요.')
                x,y,w,hh=row['window'];corners=np.float32([[[x,y],[x+w,y],[x+w,y+hh],[x,y+hh]]])
                region=cv2.perspectiveTransform(corners,h)[0]
                mask=np.zeros(b.shape,'uint8');cv2.fillConvexPoly(mask,np.int32(region),255)
                edges=cv2.Canny(b,45,120);edges[mask==0]=0
                contours,_=cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
                proposals=[]
                for contour in contours:
                    peri=cv2.arcLength(contour,True);poly=cv2.approxPolyDP(contour,.025*peri,True)
                    area=abs(cv2.contourArea(poly))
                    if len(poly)!=4 or not cv2.isContourConvex(poly) or not 80<area<20000: continue
                    pts=poly[:,0,:];center=pts.mean(axis=0)
                    if any(np.linalg.norm(center-np.array(p['center']))<8 for p in proposals):continue
                    proposals.append({'center':center.tolist(),'points':pts.tolist(),'area':area})
                proposals=sorted(proposals,key=lambda p:p['area'],reverse=True)[:30]
                return self.reply({'proposals':proposals,'inliers':int(inliers.sum()),'confirmed':False})
            if self.path == '/roi/seeds':
                name = data['selection_file']
                if Path(name).name != name or not name.startswith('selected_') or not name.endswith('.json'):
                    raise ValueError('Invalid selection')
                selection = json.loads((ROOT/'selections'/name).read_text(encoding='utf-8'))
                if data['reference'] not in [r['shot'] for r in selection['selected']]:
                    raise ValueError('Invalid reference')
                if set(data['tarps']) != set(tracking_jobs.tarps_for_shot(selection, data['reference'])):
                    raise ValueError('현재 영상에 배정된 타프의 경계만 저장하세요.')
                ds = gdal.Open(str(source_path(data['reference'], 6)))
                width, height = ds.RasterXSize, ds.RasterYSize
                ds = None
                for seed in data['tarps'].values():
                    if not all(np.isfinite(seed[k]) for k in ['x','y','size']): raise ValueError('Invalid seed')
                    if not (0<=seed['x']<width and 0<=seed['y']<height and 2<=seed['size']<=300): raise ValueError('Invalid seed bounds')
                    if seed.get('points'):
                        import cv2
                        pts = np.asarray(seed['points'], dtype=np.float32)
                        if pts.shape != (4,2) or not np.isfinite(pts).all() or not cv2.isContourConvex(pts) or abs(cv2.contourArea(pts)) < 1 or (pts[:,0]<0).any() or (pts[:,0]>=width).any() or (pts[:,1]<0).any() or (pts[:,1]>=height).any(): raise ValueError('Invalid quadrilateral')
                basis = 'native_suffix6_pixels_raw_geometry_B2_B3_B4_B6_B10' if CONFIG['source_kind']=='raw_DN_boundary_only' else 'native_suffix6_pixels_registered_radiance_B2_B3_B4_B6_B10'
                data.update(coordinate_basis=basis, roi_confirmed=False, status='boundary_proposals_pending_review' if any(s.get('points') for s in data['tarps'].values()) else 'centers_for_boundary_estimation')
                data.update(source_kind=CONFIG['source_kind'], image_size=[width,height])
                out = ROOT/'selections'/('roi_seeds_'+Path(name).stem+'.json')
                if out.exists():
                    (ROOT/'selections'/f'roi_history_{time.time_ns()}.json').write_bytes(out.read_bytes())
                out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                return self.reply({'saved':str(out), 'source_kind':CONFIG['source_kind']})
            if self.path == '/search':
                requested = data.get('locations') or [{'u': data['u'], 'v': data['v'],
                                                       'radius': data['radius'], 'tarps': CONFIG['tarps']}]
                if not isinstance(requested, list) or len(requested) not in (1, 2):
                    raise ValueError('타프 위치는 한 곳 또는 두 곳만 지정하세요.')
                all_tarps = [name for loc in requested for name in loc.get('tarps', [])]
                if sorted(all_tarps) != sorted(CONFIG['tarps']):
                    raise ValueError('세 타프를 두 위치에 중복 없이 지정하세요.')
                ds = gdal.Open(str(MOSAIC))
                locations = []
                for i, loc in enumerate(requested):
                    u, v, radius = float(loc['u']), float(loc['v']), float(loc['radius'])
                    if not (0 <= u <= 1 and 0 <= v <= 1 and 2 <= radius <= 50) or not loc['tarps']:
                        raise ValueError('위치·반경·타프 배정을 확인하세요.')
                    x, y = gdal.ApplyGeoTransform(ds.GetGeoTransform(), u*ds.RasterXSize, v*ds.RasterYSize)
                    locations.append({'id': 'AB'[i], 'point': {'x': x, 'y': y, 'crs': ds.GetProjection(), 'u': u, 'v': v},
                                      'radius_m': radius, 'tarps': loc['tarps']})
                if len(locations) == 2:
                    centers = [transform(loc['point']['x'], loc['point']['y'], loc['point']['crs'], 'EPSG:32652') for loc in locations]
                    if np.linalg.norm(np.subtract(*centers)) < 1:
                        raise ValueError('두 위치가 너무 가깝습니다. 한 위치 모드를 사용하세요.')
                with LOCK:
                    if STATE['running']:
                        return self.reply({'error':'검색 중입니다.'}, code=409)
                    STATE.update(running=True, done=0, candidates=[], pairs=[], errors=[],
                                 locations=locations, point=locations[0]['point'], radius_m=locations[0]['radius_m'],
                                 selected_ids=[], selected_pairs=[])
                threading.Thread(target=search, args=(locations,), daemon=True).start()
                return self.reply({'ok':True})
            if self.path == '/save':
                with LOCK:
                    rows = [r for r in STATE['candidates'] if r['id'] in data['ids']]
                    point = STATE['point']
                    locations = STATE['locations']
                if 'locations' in data:
                    searched = [{'u': loc['point']['u'], 'v': loc['point']['v'],
                                 'radius': loc['radius_m'], 'tarps': loc['tarps']} for loc in locations]
                    if data['locations'] != searched:
                        raise ValueError('위치·반경·타프 배정이 바뀌었습니다. 영상 검색을 다시 실행하세요.')
                if not rows or not all(r['radiance_ready'] for r in rows):
                    raise ValueError('선택한 입력 모드의 10개 밴드가 필요합니다. DN과 radiance를 자동 대체하지 않습니다.')
                if data['method'] not in ['manual_centers', 'automatic_proposal']:
                    raise ValueError('ROI 방식 오류')
                if any(r.get('saturation_qc', {}).get('status') == 'unknown' for r in rows):
                    raise ValueError('포화 검사가 끝나지 않은 영상이 포함되어 있습니다. 검사 기록을 확인하세요.')
                if len(locations) == 2 and not all(any(loc['id'] in r['coverage_locations'] for r in rows) for loc in locations):
                    raise ValueError('A와 B 양쪽 위치의 영상을 각각 선택하세요.')
                pair_ids = data.get('selected_pairs', [])
                if not isinstance(pair_ids, list) or any(not isinstance(i, int) or i < 0 or i >= len(STATE['pairs']) or
                    not {STATE['pairs'][i]['a_id'], STATE['pairs'][i]['b_id']}.issubset(data['ids']) for i in pair_ids):
                    raise ValueError('선택한 영상 쌍을 확인하세요.')
                split = len(locations) == 2 and any(set(r['coverage_locations']) != {'A', 'B'} for r in rows)
                result = {'status':'selected_candidates_roi_pending', 'method':data['method'], 'mosaic':str(MOSAIC), 'mosaic_role':CONFIG['mosaic_role'], 'identity':CONFIG['identity'], 'point':point, 'radius_m': STATE['radius_m'], 'locations': locations, 'split_locations': split, 'coverage_rule': STATE['coverage_rule'], 'selected':rows, 'selected_pairs': pair_ids, 'source_kind':CONFIG['source_kind'], 'radiance_directory':str(RADIANCE), 'trim_percent':0, 'roi_confirmed':False, 'saturation_history': STATE.get('saturation_history')}
                out = ROOT/'selections'/f'selected_{time.time_ns()}.json'
                out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                with LOCK:
                    STATE['selected_ids'] = [r['id'] for r in rows]
                    STATE['selected_pairs'] = pair_ids
                    (ROOT/'selections/last_search.json').write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding='utf-8')
                return self.reply({'saved':str(out), 'split_locations': split})
            self.reply({'error':'Unknown endpoint'}, code=404)
        except Exception as exc:
            self.reply({'error':str(exc)}, code=400)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    url=f'http://127.0.0.1:{server.server_port}'
    (ROOT/'server.json').write_text(json.dumps({'url':url,'identity':CONFIG['identity'],'pid':os.getpid()}),encoding='utf8')
    print(url, flush=True)
    server.serve_forever()
