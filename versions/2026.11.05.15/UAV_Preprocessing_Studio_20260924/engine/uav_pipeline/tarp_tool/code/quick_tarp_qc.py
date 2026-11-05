"""Diagnostic native-polygon saturation only; never approve ELC or pixel masks."""
import base64
import hashlib
import io
import json
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
from osgeo import gdal
from saturation_qc import SATURATION_DN, wavelength


def assess(rows):
    if any(not r.get('error') and r['usable_tarps']<2 for r in rows):return '포화 기준 부족 · 일부 밴드에서 사용 가능 타프 2개 미만'
    if len(rows)!=10 or any(r.get('error') for r in rows) or len({r.get('wavelength_nm') for r in rows})!=10 or any(r.get('wavelength_nm') is None for r in rows):return '보류 · 밴드 정합/원영상 확인 필요'
    common=set.intersection(*[{n for n,t in r['tarps'].items() if t['usable']} for r in rows])
    if len(common)<2:return '보류 · 10밴드 공통으로 사용할 타프가 2개 미만'
    return '포화 기준 통과 · 정합 육안 확인과 Ed 검사는 별도'


def inspect(paths, regions, names, cache):
    from review_statistics import read, transform
    if not isinstance(regions,dict) or len(regions)<2 or set(regions)-set(names):
        raise ValueError('서로 다른 타프를 두 개 이상 지정하세요.')
    for name,points in regions.items():
        a=np.asarray(points,dtype=float)
        if (a.shape!=(4,2) or not np.isfinite(a).all() or (a<0).any() or (a>1).any()
                or not cv2.isContourConvex(a.astype('float32')) or abs(cv2.contourArea(a.astype('float32')))<1e-8):
            raise ValueError(name+': 네 모서리를 순서대로 지정하세요.')
    polygons=[np.asarray(p,dtype='float32') for p in regions.values()]
    for i,a in enumerate(polygons):
        for b in polygons[:i]:
            overlap,_=cv2.intersectConvexConvex(a,b)
            if overlap>1e-8:raise ValueError('서로 다른 타프 영역이 겹칩니다. 같은 타프를 두 번 지정했는지 확인하세요.')
    before=[(str(Path(p).resolve()),Path(p).stat().st_size,Path(p).stat().st_mtime_ns) for p in paths]
    revision=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('review_statistics.py').read_bytes()).hexdigest()
    signature=hashlib.sha256(json.dumps([revision,before,regions],sort_keys=True).encode()).hexdigest()
    output=Path(cache)/signature;record=output/'result.json'
    if record.exists():return json.loads(record.read_text(encoding='utf8'))
    ref=read(paths[5]);height,width=ref.shape
    reference={n:np.asarray(p,dtype='float32')*[width,height] for n,p in regions.items()}
    rows=[]
    for i,path in enumerate(paths,1):
        row=dict(native_suffix=i,wavelength_nm=wavelength(path),tarps={},usable_tarps=0)
        try:
            ds=gdal.Open(str(path));band=ds.GetRasterBand(1)
            if ds.RasterCount!=1 or band.DataType!=gdal.GDT_UInt16:raise ValueError('native UInt16 영상 아님')
            a=read(path);h,quality=transform(a,ref,i==6)
            if not np.isfinite(h).all() or abs(np.linalg.det(h))<1e-9:raise ValueError('밴드 정합 변환 오류')
            inv=np.linalg.inv(h);valid=np.isfinite(a);finite=a[valid]
            lo,hi=np.percentile(finite,[0,100]);gray=np.nan_to_num(np.clip((a-lo)/max(hi-lo,1),0,1)*210).astype('uint8')
            rgb=np.repeat(gray[:,:,None],3,axis=2);rgb[valid & (a>=SATURATION_DN)]=[255,45,65]
            for j,(name,points) in enumerate(reference.items()):
                native=cv2.perspectiveTransform(points.astype('float32')[None],inv)[0]
                if (not np.isfinite(native).all() or (native<0).any() or (native[:,0]>=a.shape[1]).any()
                        or (native[:,1]>=a.shape[0]).any() or not cv2.isContourConvex(native)):
                    raise ValueError(name+': 변환 영역이 영상 범위 밖이거나 잘못 정합됨')
                mask=np.zeros(a.shape,'uint8');cv2.fillConvexPoly(mask,np.rint(native).astype('int32'),1)
                values=a[(mask>0)&valid]
                if len(values)<20:raise ValueError(name+': 유효 화소가 너무 적음')
                clipped=int((values>=SATURATION_DN).sum());pct=100*clipped/len(values)
                row['tarps'][name]=dict(saturated_pct=pct,saturated_pixels=clipped,valid_pixels=len(values),
                                        usable=pct<1.,native_points=native.tolist())
                row['usable_tarps']+=int(pct<1.)
                color=((117,212,188),(255,211,106),(195,162,255))[j%3]
                cv2.polylines(rgb,[np.rint(native).astype('int32')],True,color,2)
                cv2.putText(rgb,name,tuple(np.rint(native[0]).astype(int)),cv2.FONT_HERSHEY_SIMPLEX,.5,color,1,cv2.LINE_AA)
            row['alignment']=quality
            preview=Image.fromarray(rgb);preview.thumbnail((1000,750))
            buffer=io.BytesIO();preview.save(buffer,format='PNG')
            row['preview']=base64.b64encode(buffer.getvalue()).decode('ascii')
            if row['wavelength_nm'] is None:row['error']='실제 파장 메타데이터 없음'
        except Exception as exc:row['error']=str(exc)
        rows.append(row)
    after=[(str(Path(p).resolve()),Path(p).stat().st_size,Path(p).stat().st_mtime_ns) for p in paths]
    if before!=after:raise ValueError('검사 도중 원영상이 변경되었습니다.')
    result=dict(rows=rows,summary=assess(rows),regions=regions,threshold_dn=SATURATION_DN,
                rejection_fraction=.01,reference_native_suffix=6,source_signature=before,
                role='manual_native_polygon_diagnostic_unapproved',roi_approved=False,
                caution='각 밴드의 변환 경계가 실제 타프에 맞는지 확인하세요. 제외 마스크·백분위 절삭 없이 검사합니다.')
    output.mkdir(parents=True,exist_ok=True);record.write_text(json.dumps(result,ensure_ascii=False),encoding='utf8')
    return result
