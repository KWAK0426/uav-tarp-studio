"""Date-scoped location hints only; never import ROI or calibration approvals."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from osgeo import gdal, osr


def crs(value):
    ref = osr.SpatialReference()
    ref.SetFromUserInput(value)
    ref.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return ref


def key(config):
    date = config['date'][:10].replace('_', '-')
    datetime.strptime(date, '%Y-%m-%d')
    site = str(Path(config['dataset_root']).resolve()).casefold()
    return hashlib.sha256(site.encode('utf8')).hexdigest()[:16] + ':' + date


def locations(config, requested):
    if not isinstance(requested, list) or len(requested) not in (1, 2):
        raise ValueError('타프 위치는 한 곳 또는 두 곳입니다.')
    if sorted(t for r in requested for t in r['tarps']) != sorted(config['tarps']):
        raise ValueError('타프 배정을 확인하세요.')
    ds = gdal.Open(config['mosaic'])
    if not ds or not ds.GetProjection():
        raise ValueError('좌표가 있는 모자이크가 필요합니다.')
    result = []
    for index, item in enumerate(requested):
        u, v, radius = (float(item[k]) for k in ('u', 'v', 'radius'))
        if not all(math.isfinite(x) for x in (u,v,radius)) or not (0 <= u <= 1 and 0 <= v <= 1 and 2 <= radius <= 50):
            raise ValueError('위치·반경 범위를 확인하세요.')
        x, y = gdal.ApplyGeoTransform(ds.GetGeoTransform(), u*ds.RasterXSize, v*ds.RasterYSize)
        result.append(dict(id='AB'[index], point=dict(x=x,y=y,crs=ds.GetProjection(),u=u,v=v),
                           radius_m=radius, tarps=item['tarps']))
    return result


def save(config, requested):
    path = Path(config['location_library'])
    data = json.loads(path.read_text(encoding='utf8')) if path.exists() else {'schema':1,'dates':{}}
    record = dict(date=config['date'][:10].replace('_','-'), flight=config['flight'],
                  mosaic=config['mosaic'], locations=locations(config, requested),
                  saved_at=datetime.now(timezone.utc).isoformat(), role='location_hint_not_approved_roi')
    # Keep separately drawn tarp polygons when saving legacy circle hints.
    record.update({k:v for k,v in data['dates'].get(key(config),{}).items() if k.startswith('region')})
    data['dates'][key(config)] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
    os.replace(temp, path)
    return dict(saved=True, date=record['date'], count=len(record['locations']))


def load(config):
    path = Path(config['location_library'])
    data = json.loads(path.read_text(encoding='utf8')) if path.exists() else {'dates':{}}
    record = data['dates'].get(key(config))
    if not record or not record.get('locations'):
        raise ValueError('저장한 군집 원이 없습니다. 타프별 영역은 «타프별 영역 직접 지정»에서 불러옵니다.')
    ds = gdal.Open(config['mosaic'])
    inverse = gdal.InvGeoTransform(ds.GetGeoTransform())
    result = []
    for loc in record['locations']:
        point = loc['point']
        x,y,_ = osr.CoordinateTransformation(crs(point['crs']),crs(ds.GetProjection())).TransformPoint(point['x'],point['y'])
        px,py = gdal.ApplyGeoTransform(inverse,x,y)
        u,v = px/ds.RasterXSize,py/ds.RasterYSize
        if not (0 <= u <= 1 and 0 <= v <= 1):
            raise ValueError('저장한 타프 위치가 현재 모자이크 범위 밖입니다.')
        result.append(dict(u=u,v=v,radius=loc['radius_m'],tarps=loc['tarps']))
    # Also recheck tarp identities against the current project.
    locations(config,result)
    return dict(locations=result, date=record['date'], source_flight=record['flight'],
                role='location_hint_not_approved_roi')


def region_geometry(config, regions, complete=False):
    """Validate convex four-corner map hints; return world polygons and search circles."""
    import cv2
    import numpy as np
    if not isinstance(regions, dict) or not regions or set(regions)-set(config['tarps']):
        raise ValueError('타프 이름과 영역을 확인하세요.')
    if complete and len(regions)<2:
        raise ValueError('타프 두 개 이상에서 네 모서리를 지정한 뒤 검색하세요.')
    ds=gdal.Open(config['mosaic'])
    if not ds or not ds.GetProjection():raise ValueError('좌표가 있는 모자이크가 필요합니다.')
    world={};circles=[];all_metric=[]
    metric=crs('EPSG:32652');source=crs(ds.GetProjection())
    tx=osr.CoordinateTransformation(source,metric)
    for name,points in regions.items():
        a=np.asarray(points,dtype=float)
        if (a.shape!=(4,2) or not np.isfinite(a).all() or (a<0).any() or (a>1).any()
                or not cv2.isContourConvex(a.astype('float32')) or abs(cv2.contourArea(a.astype('float32')))<1e-12):
            raise ValueError(name+': 겹치지 않도록 네 모서리를 순서대로 지정하세요.')
        pts=[gdal.ApplyGeoTransform(ds.GetGeoTransform(),u*ds.RasterXSize,v*ds.RasterYSize) for u,v in a]
        world[name]=dict(points=pts,crs=ds.GetProjection())
        xy=np.asarray([tx.TransformPoint(x,y)[:2] for x,y in pts]);all_metric.extend(xy)
        center=xy.mean(axis=0);radius=float(np.linalg.norm(xy-center,axis=1).max()+.25)
        circles.append(dict(point=dict(x=float(center[0]),y=float(center[1]),crs='EPSG:32652'),radius_m=radius,tarps=[name]))
    xy=np.asarray(all_metric);center=(xy.min(axis=0)+xy.max(axis=0))/2
    radius=max(2.,float(np.linalg.norm(xy-center,axis=1).max()+.5))
    if radius>50:raise ValueError('타프 영역이 100 m 이상 떨어져 있습니다. 같은 군집인지 확인하세요.')
    x,y,_=osr.CoordinateTransformation(metric,source).TransformPoint(*center)
    px,py=gdal.ApplyGeoTransform(gdal.InvGeoTransform(ds.GetGeoTransform()),x,y)
    requested=[dict(u=px/ds.RasterXSize,v=py/ds.RasterYSize,radius=radius,tarps=list(regions))]
    return world,circles,requested


def save_regions(config, regions):
    world,_,requested=region_geometry(config,regions)
    path=Path(config['location_library'])
    data=json.loads(path.read_text(encoding='utf8')) if path.exists() else {'schema':1,'dates':{}}
    record=data['dates'].setdefault(key(config),{})
    record.update(regions=world,region_flight=config['flight'],region_mosaic=config['mosaic'],
                  region_saved_at=datetime.now(timezone.utc).isoformat())
    path.parent.mkdir(parents=True,exist_ok=True)
    # Retain the previous hints so a mistaken manual edit can be recovered.
    if path.exists():
        history=path.parent/'tarp_location_history';history.mkdir(exist_ok=True)
        (history/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json')).write_bytes(path.read_bytes())
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8');os.replace(temp,path)
    return dict(regions=regions,locations=requested,complete=len(regions)>=2,
                role='location_hint_not_approved_roi')


def load_regions(config):
    path=Path(config['location_library'])
    data=json.loads(path.read_text(encoding='utf8')) if path.exists() else {'dates':{}}
    record=data['dates'].get(key(config),{})
    if not record.get('regions'):return dict(regions={},complete=False)
    ds=gdal.Open(config['mosaic']);inv=gdal.InvGeoTransform(ds.GetGeoTransform());regions={}
    for name,item in record['regions'].items():
        tx=osr.CoordinateTransformation(crs(item['crs']),crs(ds.GetProjection()))
        points=[gdal.ApplyGeoTransform(inv,*tx.TransformPoint(x,y)[:2]) for x,y in item['points']]
        regions[name]=[[x/ds.RasterXSize,y/ds.RasterYSize] for x,y in points]
    _,_,requested=region_geometry(config,regions)
    return dict(regions=regions,locations=requested,complete=len(regions)>=2,
                source_flight=record['region_flight'],role='location_hint_not_approved_roi')
