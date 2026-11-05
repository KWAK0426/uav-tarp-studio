"""Reuse diagnostic boundaries as unapproved native ROI/pixel proposals."""
import json
from pathlib import Path
import cv2
import numpy as np
from osgeo import gdal


def dark_pixels(images, valid_masks, core):
    """Existing pixel_exclusions upper-half MAD rule; require two-band agreement."""
    votes=np.zeros(core.shape,np.uint8);used=0
    for image,valid in zip(images,valid_masks):
        valid=valid & core & np.isfinite(image) & (image!=65535)
        values=image[valid]
        if len(values)<20 or np.mean(values>=65520)>=.01:continue
        upper=values[values>=np.median(values)];center=float(np.median(upper))
        spread=max(1.4826*float(np.median(np.abs(upper-center))),.02*center,1.)
        votes+=(valid & (image<center-3.*spread)).astype(np.uint8);used+=1
    candidate=np.zeros(core.shape,np.uint8)
    if used>=2:
        count,labels,stats,_=cv2.connectedComponentsWithStats((votes>=2).astype(np.uint8),8)
        for label in range(1,count):
            if stats[label,cv2.CC_STAT_AREA]>=4:candidate[labels==label]=1
    candidate &= core.astype(np.uint8)
    fraction=float(candidate.sum()/max(1,core.sum()))
    return candidate,dict(status='proposal' if used>=2 and fraction<=.25 else 'review_required',
                          used_bands=used,excluded_pixels=int(candidate.sum()),excluded_fraction=fraction,
                          method='upper_half_MAD_3_two_band_agreement',approved=False)


def shadow_proposal(record,qc,paths,raster):
    from core import roi_window
    window,core=roi_window(raster,record['inner']);x,y,w,h=window
    target=np.float32(record['outer'])-.5-[x,y]
    name=record['tarp_id'];rows=[r for r in qc['rows'] if not r.get('error') and r.get('tarps',{}).get(name,{}).get('usable')]
    # Prefer the same green/red/NIR wavelengths used by the older aligned-RGB detector.
    rows=sorted(rows,key=lambda r:min(abs(float(r['wavelength_nm'])-v) for v in (560,668,842)))[:3]
    images=[];masks=[];bands=[]
    for row in rows:
        ds=gdal.Open(str(paths[row['native_suffix']-1]));b=ds.GetRasterBand(1);a=b.ReadAsArray().astype('float32')
        valid=(b.GetMaskBand().ReadAsArray()>0)&np.isfinite(a)&(a!=65535)
        nd=b.GetNoDataValue()
        if nd is not None:valid &= a!=nd
        transform=cv2.getPerspectiveTransform(np.float32(row['tarps'][name]['native_points']),target.astype('float32'))
        images.append(cv2.warpPerspective(a,transform,(w,h),flags=cv2.INTER_NEAREST))
        masks.append(cv2.warpPerspective(valid.astype('uint8'),transform,(w,h),flags=cv2.INTER_NEAREST)>0)
        bands.append(row['wavelength_nm'])
    mask,meta=dark_pixels(images,masks,core);meta['wavelengths_nm']=bands
    yy,xx=np.where(mask>0)
    # Radius zero means exactly one native pixel, compatible with the existing export/statistics mask.
    return [[float(px+x+.5),float(py+y+.5),0,True] for py,px in zip(yy,xx)],meta


def import_proposals(session):
    from core import Raster,inset,ordered
    from .roi_studio_bridge import _library_key
    gdal.UseExceptions()
    imported=0;seen=set();diagnostics={}
    records=sorted((Path(session.context['output'])/'cache').glob('batch_qc_*/batch.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in records:
        batch=json.loads(path.read_text(encoding='utf-8'))
        for entry in batch['entries']:
            shot=entry['shot']
            if shot not in session.shots or shot in seen:continue
            seen.add(shot)
            if entry.get('error') or entry.get('user_boundary_decision')=='no':
                diagnostics[shot]=entry.get('error','사용자가 경계 수정 요청');continue
            expected=[session.signatures[f'{shot}_{b}'] for b in range(1,11)]
            if expected!=entry.get('source_signature'):
                diagnostics[shot]='원영상 서명 불일치';continue
            qc=json.loads(Path(entry['result_path']).read_text(encoding='utf-8'))
            if qc.get('source_signature')!=expected or qc.get('regions')!=entry.get('regions'):
                diagnostics[shot]='검사 결과와 경계 불일치';continue
            raster=Raster(session.files[shot+'_6']);existing=session.images.setdefault(shot,[])
            retained={r.get('tarp_id') for r in existing}
            for name,points in entry['regions'].items():
                if name in retained or _library_key(name) not in session.library:continue
                outer=ordered(np.float32(points)*[raster.width,raster.height]+.5)
                inner=inset(outer,15)
                record=dict(name=name,tarp_id=name,outer=outer.tolist(),inner=inner.tolist(),center=outer.mean(0).tolist(),
                            margin_percent_shortest_altitude_per_edge=15,detection_band=1,
                            role='calibration' if shot==session.reference else 'validation',reviewed=False,pixel_reviewed=False,
                            percentile_filter=dict(enabled=True,low=5,high=95),exclusion_strokes=[],
                            proposal_source=str(path),source_boundary_reviewed=entry.get('reviewed',False))
                try:record['exclusion_strokes'],record['pixel_proposal']=shadow_proposal(record,qc,entry['source_files'],raster)
                except Exception as exc:record['pixel_proposal']=dict(status='review_required',reason=str(exc),approved=False)
                existing.append(record);imported+=1
            raster.ds=None
    return imported,diagnostics
