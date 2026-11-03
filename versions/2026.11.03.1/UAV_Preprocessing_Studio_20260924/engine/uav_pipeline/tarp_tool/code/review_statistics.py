"""Measure native pixels through inverse band transforms; do not fit ELC."""
import argparse
import csv
import hashlib
import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path
import cv2
import numpy as np
import tifffile
from osgeo import gdal
from PIL import Image
from context import CONFIG, ROOT, index_files
from aligned_rgb import gray


def read(path):
    ds=gdal.Open(str(path)); b=ds.GetRasterBand(1)
    a=b.ReadAsArray().astype('float32'); no=b.GetNoDataValue()
    a[~np.isfinite(a)]=np.nan
    if no is not None:a[a==no]=np.nan
    return a


def transform(a, ref, same):
    if same:return np.eye(3), {'method':'identity'}
    sift=cv2.SIFT_create(nfeatures=12000)
    ka,da=sift.detectAndCompute(gray(a),None);kr,dr=sift.detectAndCompute(gray(ref),None)
    if da is None or dr is None:raise ValueError('밴드정합 특징점 부족')
    pairs=cv2.BFMatcher().knnMatch(da,dr,k=2)
    good=[p[0] for p in pairs if len(p)==2 and p[0].distance<.72*p[1].distance]
    if len(good)<20:raise ValueError('밴드정합 대응점 부족')
    src=np.float32([ka[m.queryIdx].pt for m in good]); dst=np.float32([kr[m.trainIdx].pt for m in good])
    h,mask=cv2.findHomography(src,dst,cv2.RANSAC,2.5)
    if h is None or mask.sum()<20:raise ValueError('밴드정합 실패')
    error=np.linalg.norm(cv2.perspectiveTransform(src[:,None],h)[:,0]-dst,axis=1)
    med=float(np.median(error[mask.ravel()>0]))
    if med>1.5:raise ValueError('밴드정합 잔차 초과')
    return h, {'method':'SIFT_RANSAC','inliers':int(mask.sum()),'median_error_px':med}


def cached_transform(shot, band, reference_size):
    folder=ROOT/'cache'/'aligned_rgb'
    path=next((p for p in (folder/(shot+'.b234610_v3.json'),folder/(shot+'.b24610_v2.json')) if p.is_file()),None)
    if path is None:return None
    cache=json.loads(path.read_text(encoding='utf-8'))
    if (Path(cache.get('image_source','')).resolve()!=Path(CONFIG['image_source']).resolve()
            or cache.get('source_kind')!=CONFIG['source_kind']
            or cache.get('image_size')!=list(reference_size)):
        return None
    for item in cache.get('bands',[]):
        if item.get('source_suffix')!=band:continue
        h=np.asarray(item['transform'],dtype='float64')
        if h.shape!=(3,3) or not np.isfinite(h).all() or abs(np.linalg.det(h))<1e-9:
            raise ValueError(f'{shot} band {band}: 저장된 정합 변환이 유효하지 않습니다.')
        return h, {'method':'reviewed_preview_cache','source':str(path),
                   'original_method':item['method'],'phase_response':item.get('phase_response'),
                   'inliers':item.get('inliers'),'median_error_px':item.get('median_error_px')}
    return None


def phase_transform(a, ref):
    def edges(image):
        image=gray(image).astype('float32')
        return cv2.magnitude(cv2.Sobel(image,cv2.CV_32F,1,0),cv2.Sobel(image,cv2.CV_32F,0,1))
    if a.shape!=ref.shape:raise ValueError('이동량 정합에는 같은 영상 크기가 필요합니다.')
    (dx,dy),response=cv2.phaseCorrelate(edges(ref),edges(a))
    if not np.isfinite([dx,dy,response]).all() or response<.1 or max(abs(dx),abs(dy))>100:
        raise ValueError(f'영상 이동량 정합 품질 부족 (response={response:.3f})')
    return np.array([[1,0,-dx],[0,1,-dy],[0,0,1]],dtype='float64'), {
        'method':'gradient_phase_translation','phase_response':float(response),
        'phase_shift_px':[float(dx),float(dy)]}


def check_roi_transform(h, shape, tarps):
    h=np.asarray(h,dtype='float64')
    if h.shape!=(3,3) or not np.isfinite(h).all() or abs(np.linalg.det(h))<1e-9:
        raise ValueError('정합 변환행렬이 유효하지 않습니다.')
    inverse=np.linalg.inv(h)
    for tarp,points in tarps.items():
        source=np.asarray(points,dtype='float32')
        native=cv2.perspectiveTransform(source[None],inverse)[0]
        if (not np.isfinite(native).all() or (native[:,0]<0).any() or (native[:,0]>=shape[1]).any()
                or (native[:,1]<0).any() or (native[:,1]>=shape[0]).any()):
            raise ValueError(f'{tarp} ROI가 밴드 영상 범위를 벗어납니다.')
        source_edges=np.linalg.norm(np.roll(source,-1,axis=0)-source,axis=1)
        native_edges=np.linalg.norm(np.roll(native,-1,axis=0)-native,axis=1)
        if (np.min(source_edges)<1 or not cv2.isContourConvex(native)
                or np.min(native_edges/source_edges)<0.4 or np.max(native_edges/source_edges)>2.5
                or np.sign(cv2.contourArea(source,oriented=True)) != np.sign(cv2.contourArea(native,oriented=True))):
            raise ValueError(f'{tarp} ROI 형태가 비정상적으로 뒤틀렸습니다.')


def band_transform(shot, band, a, ref, tarps):
    attempts=[]
    candidates=(('특징점',lambda: transform(a,ref,band==6)),
                ('저장된 정합',lambda: cached_transform(shot,band,(ref.shape[1],ref.shape[0]))),
                ('이동량',lambda: phase_transform(a,ref)))
    for name,produce in candidates:
        try:
            result=produce()
            if result is None:continue
            h,quality=result
            check_roi_transform(h,a.shape,tarps)
            return h,quality
        except (ValueError,np.linalg.LinAlgError,cv2.error) as exc:
            attempts.append(f'{name}: {exc}')
    raise ValueError(f'{shot} band {band}: 정합 후보 모두 실패 ({"; ".join(attempts)})')


def sample(a, points, inverse, exclude=None):
    pts=cv2.perspectiveTransform(np.float32(points)[None],inverse)[0]
    if not np.isfinite(pts).all() or (pts[:,0]<0).any() or (pts[:,0]>=a.shape[1]).any() or (pts[:,1]<0).any() or (pts[:,1]>=a.shape[0]).any():
        raise ValueError('타프 ROI의 밴드별 유효 범위가 부족합니다.')
    mask=np.zeros(a.shape,'uint8');cv2.fillConvexPoly(mask,np.rint(pts).astype('int32'),1)
    valid=(mask>0)&np.isfinite(a)
    if exclude is not None:valid &= ~exclude
    values=a[valid]
    if values.size<5:raise ValueError('타프 ROI 유효 화소 부족')
    return values,pts.tolist()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',required=True);args=parser.parse_args()
    out=Path(args.snapshot).parent;data=json.loads(Path(args.snapshot).read_text(encoding='utf8'))
    files=index_files(CONFIG['image_source']);raw=index_files(CONFIG['raw']);rows=[];audits=[]
    total=len(data['images'])*10;done=0
    for shot,record in data['images'].items():
        ref=read(files[shot+'_6'])
        for band in range(1,11):
            path=files[f'{shot}_{band}'];a=read(path);h,quality=band_transform(shot,band,a,ref,record['tarps']);inv=np.linalg.inv(h)
            with tifffile.TiffFile(raw[f'{shot}_{band}']) as tf:
                tag=tf.pages[0].tags.get('XMP')
                wave=next((e.text for e in ET.fromstring(tag.value).iter() if e.tag.endswith('}CentralWavelength')),None) if tag else None
            if wave is None:raise ValueError('실제 밴드 파장 메타데이터가 없습니다.')
            entry={'shot':shot,'band_suffix':band,'wavelength_nm':float(wave),'source_path':str(path),
                   'native_image_size':[int(a.shape[1]),int(a.shape[0])],
                   'native_to_reference':h.tolist(),'quality':quality,'native_rois':{},'native_exclusion_masks':{}}
            for tarp,points in record['tarps'].items():
                pts=np.asarray(points);center=pts.mean(0);trimmed=center+(1-2*data['trim_percent']/100)*(pts-center)
                full,_=sample(a,pts,inv);values,native=sample(a,trimmed,inv)
                mask_info=record.get('pixel_masks',{}).get(tarp)
                excluded=None
                if mask_info:
                    source=Path(mask_info['path'])
                    if hashlib.sha256(source.read_bytes()).hexdigest()!=mask_info['sha256']:
                        raise ValueError(f'{shot} {tarp}: 승인 제외 마스크가 변경됐습니다.')
                    reference_mask=np.asarray(Image.open(source).convert('L'))
                    if reference_mask.shape!=ref.shape:
                        raise ValueError(f'{shot} {tarp}: 제외 마스크 크기가 기준 영상과 다릅니다.')
                    excluded=cv2.warpPerspective(reference_mask,inv,(a.shape[1],a.shape[0]),flags=cv2.INTER_NEAREST)>0
                    mask_dir=out/'pixel_masks';mask_dir.mkdir(exist_ok=True)
                    mask_path=mask_dir/f'{shot}_{band}_{tarp}.png'
                    Image.fromarray(excluded.astype('uint8')*255).save(mask_path)
                    entry['native_exclusion_masks'][tarp]={'path':str(mask_path),'sha256':hashlib.sha256(mask_path.read_bytes()).hexdigest()}
                elif tarp in ('PNU_36','PNU_56'):
                    raise ValueError(f'{shot} {tarp}: 내부 화소 마스크 승인이 없습니다.')
                clean,_=sample(a,trimmed,inv,excluded)
                p05,p95=np.percentile(clean,[5,95])
                lo,hi=np.percentile(clean,[data['percentile_low'],data['percentile_high']])
                keep=clean[(clean>=lo)&(clean<=hi)]
                entry['native_rois'][tarp]=native
                rows.append({'shot':shot,'tarp':tarp,'band_suffix':band,'wavelength_nm':float(wave),
                             'source_kind':CONFIG['source_kind'],'source_path':str(path),'full_n':int(full.size),'full_mean':float(full.mean()),
                             'trim_n':int(values.size),'trim_mean':float(values.mean()),'p05':float(p05),'p95':float(p95),
                             'mask_excluded_n':int(values.size-clean.size),'mask_retained_n':int(clean.size),
                             'filter_low':float(lo),'filter_high':float(hi),
                             'filtered_n':int(keep.size),'filtered_mean':float(keep.mean()),'filtered_median':float(np.median(keep)),
                             'filtered_std':float(keep.std()),'trim_percent':data['trim_percent']})
            audits.append(entry);done+=1
            print('PROGRESS '+json.dumps({'done':done,'message':f'{shot} band {band} 통계 완료'}),flush=True)
    with (out/'tarp_statistics.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (out/'band_transforms.json').write_text(json.dumps(audits,ensure_ascii=False,indent=2),encoding='utf8')
    raw_geometry = CONFIG['source_kind'] == 'raw_DN_boundary_only'
    transfer = {
        'geometry_source': 'native_raw_multispectral' if raw_geometry else 'native_radiometric_radiance',
        'reference_coordinate_system': 'Band6 native pixels; each other band uses its recorded inverse transform',
        'target': 'matching native-geometry Ed-normalized and sensor-corrected radiance',
        'match_keys': ['shot', 'band_suffix'],
        'required_checks': ['same image width', 'same image height', 'same native pixel orientation'],
        'status': 'pending_corrected_radiance_application' if raw_geometry else 'source_is_radiance',
        'rule': 'Do not apply these polygons to mosaics, ortho photos, or aligned preview arrays.',
    }
    manifest=dict(data,statistics_csv=str(out/'tarp_statistics.csv'),transforms_json=str(out/'band_transforms.json'),
                  identity=CONFIG['identity'],source_kind=CONFIG['source_kind'],coordinate_transfer=transfer,
                  statistics_role='raw_DN_geometry_QC_only' if raw_geometry else 'radiance_ROI_statistics',
                  t0=None,elc_ready=False,
                  pending=['match reference to step02 capture-time CSV','verify sensor/Ed correction basis',
                           'transfer approved native-band polygons after dimension checks','fit L=a*rho+b'])
    (out/'roi_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    (ROOT/'reviewed_manifest_latest.json').write_text(json.dumps({'path':str(out/'roi_manifest.json')}),encoding='utf8')
    blocks=[]
    for shot in data['images']:
        header=''.join(f'<th>{band}<br>{next(r["wavelength_nm"] for r in rows if r["shot"]==shot and r["band_suffix"]==band):g}nm</th>' for band in range(1,11))
        body=''.join('<tr><th>'+html.escape(tarp)+'</th>'+''.join(f'<td>{next(r["filtered_mean"] for r in rows if r["shot"]==shot and r["tarp"]==tarp and r["band_suffix"]==band):.4f}</td>' for band in range(1,11))+'</tr>' for tarp in data['images'][shot]['tarps'])
        blocks.append('<h2>'+shot+'</h2><table><tr><th>타프</th>'+header+'</tr>'+body+'</table>')
    page='<meta charset="utf-8"><style>body{font:15px sans-serif;margin:24px}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #ccc;padding:8px;text-align:center}th:first-child{text-align:left}</style><h1>승인 ROI · 내부 화소 제외 / Trim / P'+str(data['percentile_low'])+'–P'+str(data['percentile_high'])+' 통계</h1>'
    role = ('원영상 DN 통계는 ROI 기하 QC 전용입니다. 같은 촬영번호·밴드·크기의 Ed/센서 교정 radiance에 ROI를 옮긴 뒤 ELC 값을 계산합니다.'
            if raw_geometry else '방사보정 radiance ROI 통계입니다. Ed·센서 교정 기준 일치를 추가로 확인해야 합니다.')
    page+='<p>'+html.escape(role)+' 반사도 검증 결과는 아닙니다.</p><p><a href="tarp_statistics.csv">CSV</a> · <a href="roi_manifest.json">ROI manifest</a> · <a href="band_transforms.json">밴드 변환</a></p>'
    (out/'index.html').write_text(page+''.join(blocks),encoding='utf8')


if __name__=='__main__':
    gdal.UseExceptions();main()
