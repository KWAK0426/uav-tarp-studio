"""Unapproved map-to-native boundary proposals; run in an isolated worker."""
import json
import os
from pathlib import Path
import sys


def propose(request):
    import importlib.util
    import cv2
    import numpy as np
    from osgeo import gdal, osr
    gdal.UseExceptions()
    context_path = Path(request['context'])
    config = json.loads(context_path.read_text(encoding='utf-8'))
    os.environ['UAV_TARP_CONTEXT'] = str(context_path)
    sys.path.insert(0, str(Path(__file__).parent/'tarp_tool/code'))
    from review_statistics import read, transform
    spec = importlib.util.spec_from_file_location('native_boundary_core', Path(__file__).resolve().parents[3]/'tarp_center_roi_tool/core.py')
    core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)
    row, state = request['row'], request['state']
    paths = [Path(row['panel_photo']), Path(row['source_files'][5])]
    before = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths]
    photo = gdal.Open(str(paths[0]))
    a = photo.GetRasterBand(6).ReadAsArray().astype('float32')
    valid = photo.GetRasterBand(6).GetMaskBand().ReadAsArray() > 0
    if photo.RasterCount == 11: valid &= photo.GetRasterBand(11).ReadAsArray() > 0
    a[(a == 65535) | ~valid] = np.nan
    raw = read(paths[1]); height, width = raw.shape
    h, quality = transform(a, raw, False)
    def project(points):
        q = cv2.perspectiveTransform(np.asarray(points, 'float32')[None], h)[0]
        if (not np.isfinite(q).all() or (q < 0).any() or (q[:,0] >= width).any()
                or (q[:,1] >= height).any() or not cv2.isContourConvex(q)):
            raise ValueError('위치 연결이 원영상 범위 밖입니다. Photos와 원영상 대응을 확인하세요.')
        return q
    raster = core.Raster(paths[1])
    proposals, found = {}, []
    if state.get('regions'):
        mosaic = gdal.Open(config['mosaic'])
        src, dst = osr.SpatialReference(), osr.SpatialReference()
        src.ImportFromWkt(mosaic.GetProjection()); dst.ImportFromWkt(photo.GetProjection())
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER); dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        conversion = osr.CoordinateTransformation(src, dst)
        inverse = gdal.InvGeoTransform(photo.GetGeoTransform())
        for name, polygon in state['regions'].items():
            points = []
            for u, v in polygon:
                x,y = gdal.ApplyGeoTransform(mosaic.GetGeoTransform(),u*mosaic.RasterXSize,v*mosaic.RasterYSize)
                x,y,_ = conversion.TransformPoint(x,y)
                points.append(gdal.ApplyGeoTransform(inverse,x,y))
            hint = project(points); center = hint.mean(axis=0)
            candidates = raster.propose(center,1,max(24,float(np.ptp(hint,axis=0).max())*1.5))
            if candidates: proposals[name] = candidates[0]['points']
    else:
        x,y,w,hh = row['window']
        region = project([[x,y],[x+w,y],[x+w,y+hh],[x,y+hh]])
        expected_area = abs(cv2.contourArea(region))/(2*float(state.get('radius_m') or 2))**2
        x0,y0 = np.maximum(0,np.floor(region.min(axis=0)-12)).astype(int)
        x1,y1 = np.minimum([width,height],np.ceil(region.max(axis=0)+12)).astype(int)
        local = raw[y0:y1,x0:x1]; ok = np.isfinite(local)
        gray = core.stretch(local,ok)
        # Existing center-guided detector refines contour seed centers inside the mapped search region.
        contours = []
        for low,high in ((15,45),(35,100),(65,170)):
            edge = cv2.Canny(gray,low,high)
            edge = cv2.morphologyEx(edge,cv2.MORPH_CLOSE,np.ones((3,3),'uint8'))
            contours.extend(cv2.findContours(edge,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0])
        for contour in contours:
            area = abs(cv2.contourArea(contour))
            if not 36 < area < .35*abs(cv2.contourArea(region)): continue
            rect = cv2.minAreaRect(contour)
            if min(rect[1]) < 5 or max(rect[1])/min(rect[1]) > 2.3: continue
            center = np.array(rect[0])+[x0,y0]
            if cv2.pointPolygonTest(region,tuple(center),False) < 0: continue
            if any(np.linalg.norm(center-np.mean(p,axis=0))<max(5,np.sqrt(area)*.45) for p in found): continue
            candidates = raster.propose(center,1,max(24,np.sqrt(area)*2))
            if not candidates: continue
            points = np.asarray(candidates[0]['points'],'float32')
            sides = np.linalg.norm(points-np.roll(points,1,axis=0),axis=1)
            vectors = np.roll(points,-1,axis=0)-points
            cosine = np.abs(np.sum(vectors*np.roll(vectors,1,axis=0),axis=1)/
                            (np.linalg.norm(vectors,axis=1)*np.linalg.norm(np.roll(vectors,1,axis=0),axis=1)))
            if (sides.max()/sides.min()>1.7 or cosine.max()>.4
                    or not .5*expected_area<abs(cv2.contourArea(points))<1.8*expected_area): continue
            if any(cv2.intersectConvexConvex(points,p)[0]>.4*min(abs(cv2.contourArea(points)),abs(cv2.contourArea(p))) for p in found): continue
            found.append(points)
        names = request['names']
        if set(names) != {'PNU_s_3','PNU_s_36','PNU_s_56'} or not 2 <= len(found) <= 8:
            raise ValueError(f'위치는 연결됐지만 타프 경계 {len(found)}개를 찾았습니다. 자동 구분이 애매하므로 모자이크에서 타프별 영역을 지정하거나 직접 수정하세요.')
        # Saturated gray/white tarps cannot be identified by brightness: let the user assign names.
    if len(proposals)<2 and len(found)<2: raise ValueError('두 타프 이상의 경계를 찾지 못했습니다. 위치와 경계를 직접 확인하세요.')
    regions = {n:(np.asarray(p)/[width,height]).tolist() for n,p in proposals.items()}
    if before != [(str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in paths]: raise ValueError('제안 중 입력 영상이 변경됐습니다.')
    return dict(regions=regions,candidates=[(np.asarray(p)/[width,height]).tolist() for p in found],alignment=quality,source_signature=before,shot=row['shot'],
                map_regions=state.get('regions',{}),search_window=row['window'],roi_approved=False,
                status='proposal_requires_user_review',names_require_review=not bool(state.get('regions')))


if __name__ == '__main__':
    try:
        print(json.dumps(propose(json.load(sys.stdin)),ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=True));sys.exit(1)
