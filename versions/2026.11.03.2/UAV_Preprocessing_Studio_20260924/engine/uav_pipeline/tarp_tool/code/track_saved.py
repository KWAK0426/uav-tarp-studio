from pathlib import Path
import json
import time
import html
import cv2
import numpy as np
from osgeo import gdal
from PIL import Image, ImageDraw
from seed_boundaries import find_multiband_boundaries
from aligned_rgb import get_stack, preview

from context import ROOT, CONFIG
from tracking_jobs import tarps_for_shot
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--snapshot',required=True)
args=parser.parse_args()
snapshot_path=Path(args.snapshot)
snapshot=json.loads(snapshot_path.read_text(encoding='utf-8'))
OUT=snapshot_path.parent
gdal.UseExceptions()
selection_path=ROOT/'selections'/snapshot['selection_file']
selection=snapshot['selection']
refs=snapshot['references']
sources={s:str(snapshot_path) for s in refs}
(OUT/'recovered_manual_references.json').write_text(json.dumps({'sources':sources,'references':refs},ensure_ascii=False,indent=2),encoding='utf-8')
shots=list(dict.fromkeys(r['shot'] for r in selection['selected']))
images={};features={}
sift=cv2.SIFT_create(nfeatures=6500)
for shot in shots:
    print('align',shot,flush=True)
    stack=get_stack(shot);grays=[]
    for a in stack:
        valid=np.isfinite(a)&(a!=65535);lo,hi=np.percentile(a[valid],[1,99])
        grays.append((np.nan_to_num(np.clip((a-lo)/max(hi-lo,1e-9),0,1))*255).astype('uint8'))
    images[shot]=(stack,grays[0]);features[shot]=[sift.detectAndCompute(gray,None) for gray in grays]
    print('PROGRESS '+json.dumps({'done':len(images),'message':'정합 완료: '+shot}),flush=True)

def mapping(source,target):
    source_points=[];target_points=[]
    for (ka,da),(kb,db) in zip(features[source],features[target]):
        if da is None or db is None:continue
        pairs=cv2.BFMatcher().knnMatch(da,db,k=2)
        good=[p[0] for p in pairs if len(p)==2 and p[0].distance<.7*p[1].distance]
        source_points.extend(ka[m.queryIdx].pt for m in good)
        target_points.extend(kb[m.trainIdx].pt for m in good)
    if len(source_points)<16:return None
    a=np.float32(source_points);b=np.float32(target_points)
    H,mask=cv2.findHomography(a,b,cv2.RANSAC,2.5)
    if H is None or int(mask.sum())<16:return None
    return H,a[mask.ravel()>0],int(mask.sum())

def warp(points,H):return cv2.perspectiveTransform(np.float32(points)[None],H)[0]
def iou(a,b):
    a=cv2.convexHull(np.float32(a));b=cv2.convexHull(np.float32(b));inter=cv2.intersectConvexConvex(a,b)[0]
    return float(inter/max(cv2.contourArea(a)+cv2.contourArea(b)-inter,1e-9))

results={}
for target in shots:
    predictions={};matches={}
    expected=set(tarps_for_shot(selection,target))
    for source,seeds in refs.items():
        if source==target:continue
        if not expected.intersection(seeds):continue
        match=mapping(source,target)
        if match is None:continue
        H,inlierxy,count=match;matches[source]=count
        for name,seed in seeds.items():
            if name not in expected:continue
            if not seed.get('points'):continue
            points=warp(seed['points'],H);center=points.mean(axis=0);area=abs(cv2.contourArea(points))
            if not np.isfinite(points).all() or not cv2.isContourConvex(points) or not 20<area<50000:continue
            height,width=images[target][0][0].shape
            if (points[:,0]<0).any() or (points[:,0]>=width).any() or (points[:,1]<0).any() or (points[:,1]>=height).any():continue
            # Require matched scene features near the tarp, not just elsewhere in the image.
            local=int((np.linalg.norm(inlierxy-np.array([seed['x'],seed['y']]),axis=1)<max(100,seed['size']*3)).sum())
            if local<6:continue
            predictions.setdefault(name,[]).append({'source':source,'points':points.tolist(),'center':center.tolist(),'size':float(np.sqrt(area)),'local_inliers':local})
    seeds={};diagnostics={}
    for name,preds in predictions.items():
        centers=np.array([p['center'] for p in preds]);center=np.median(centers,axis=0);size=float(np.median([p['size'] for p in preds]));spread=float(np.max(np.linalg.norm(centers-center,axis=1)))
        agree=len(preds)>=2 and spread<=max(3,size*.2)
        diagnostics[name]={'sources':[p['source'] for p in preds],'spread_px':spread,'agreement':agree,'predictions':preds}
        sides=np.sort(np.linalg.norm(np.roll(np.float32(preds[0]['points']),-1,axis=0)-np.float32(preds[0]['points']),axis=1))
        seeds[name]={'x':float(center[0]),'y':float(center[1]),'size':size,'width':float(sides[2:].mean()),'height':float(sides[:2].mean())}
    boundaries=find_multiband_boundaries(images[target][0],seeds,raw_dn=CONFIG['source_kind']=='raw_DN_boundary_only')
    records={}
    for name in CONFIG['tarps']:
        if name not in expected:continue
        d=diagnostics.get(name,{});b=boundaries.get(name,{'status':'failed'})
        status='review' if d.get('agreement') and b.get('points') else 'needs_correction'
        if target in refs and len(d.get('sources',[]))==1 and b.get('points'):
            status='crosscheck_only'
        points=b.get('points')
        if points and name in predictions:
            overlap=max(iou(points,p['points']) for p in predictions[name])
            if overlap<.5:status='needs_correction'
        if not points and name in predictions:points=predictions[name][0]['points']
        rec={'status':status,'points':points,'seed':seeds.get(name),'tracking':d,'boundary':b,'confirmed':False}
        if target in refs and points and refs[target].get(name,{}).get('points'):
            rec['manual_iou']=iou(points,refs[target][name]['points'])
        records[name]=rec
    results[target]={'manual_reference':target in refs,'matches':matches,'tarps':records}
    print('PROGRESS '+json.dumps({'done':len(shots)+len(results),'message':'추적 완료: '+target}),flush=True)
    a=Image.fromarray(preview(images[target][0]));draw=ImageDraw.Draw(a)
    allpoints=[]
    for name,r in records.items():
        if not r['points']:continue
        pts=[tuple(v) for v in r['points']];allpoints+=pts
        color='#23ded0' if r['status'] in ('review','crosscheck_only') else '#ffab42'
        draw.line(pts+[pts[0]],fill=color,width=2);draw.text(pts[0],name,fill=color)
    if allpoints:
        xy=np.array(allpoints);low=np.maximum(0,xy.min(axis=0)-50);high=np.minimum(a.size,xy.max(axis=0)+50)
        a=a.crop(tuple(map(int,[*low,*high])))
    a.save(OUT/f'{target}.png')
    print(target,{s:sum(r['status']==s for r in records.values()) for s in ['review','needs_correction']},flush=True)
    (OUT/'tracking_results.json').write_text(json.dumps({'reference_sources':sources,'selection':str(selection_path),'results':results,'identity':CONFIG['identity'],'source_kind':CONFIG['source_kind'],'confirmed':False,'trim_applied':False},ensure_ascii=False,indent=2),encoding='utf-8')

blocks=[]
for shot,d in results.items():
    table=''.join(f"<tr><td>{html.escape(n)}</td><td>{'검토 가능' if r['status']=='review' else '단일 기준 교차시험' if r['status']=='crosscheck_only' else '수정 필요'}</td><td>{format(r['manual_iou'],'.3f') if 'manual_iou' in r else '—'}</td><td>{len(r['tracking'].get('sources',[]))}</td></tr>" for n,r in d['tarps'].items())
    blocks.append(f"<section><h2>{shot} {'· 기준 영상 교차시험' if d['manual_reference'] else '· 자동 추적'}</h2><img src='{shot}.png' onclick='window.open(this.src)'><table><tr><th>타프</th><th>상태</th><th>수동 ROI IoU*</th><th>기준 수</th></tr>{table}</table></section>")
page="""<!doctype html><html lang='ko'><meta charset='utf-8'><title>7/16 두 기준 영상 추적 시험</title><style>body{font:14px 'Malgun Gothic',sans-serif;margin:24px;color:#223333;background:#f6f8f8}h1{font-size:22px}h2{font-size:17px}section{padding:18px 0;border-top:1px solid #bcc8c8;display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:18px}h2{grid-column:1/-1}img{max-width:100%;max-height:600px;cursor:zoom-in}table{border-collapse:collapse;align-self:start}td,th{padding:8px;border-bottom:1px solid #ccd}a{color:#146b62}</style><a href='/roi.html'>중심·경계 설정으로</a><h1>7/16 · 기준 2장 → 선택 영상 6장 추적</h1><p>청록: 두 기준 이상 위치 일치 + 경계 검출 / 주황: 수정 필요. 모든 결과는 승인 전이며 trim을 적용하지 않았습니다.</p><p>* IoU는 기준 영상 2장의 교차시험에만 의미가 있습니다. 기준 영상 자체는 나머지 한 기준으로만 교차시험합니다. 다른 영상은 수동 정답이 없어 IoU를 표시하지 않습니다. 이미지를 누르면 확대합니다.</p>"""+''.join(blocks)
page=page.replace('7/16 두 기준 영상 추적 시험',html.escape(CONFIG['label'])+' 타프 추적').replace('7/16 · 기준 2장 → 선택 영상 6장 추적',html.escape(CONFIG['label'])+f' · 기준 {len(refs)}장 → 선택 영상 {len(shots)}장 추적')
page += '<p>' + ('원영상 DN 경계 시험 · radiance/반사도 검증 아님' if CONFIG['source_kind']=='raw_DN_boundary_only' else '방사보정 radiance 경계 추적 · 센서교정·정량 검증은 별도') + '</p>'
page += '<p><a href="/review.html?job='+OUT.name+'">ROI 수정·승인 → Trim / P05–P95 통계</a></p>'
(OUT/'index.html').write_text(page,encoding='utf-8')
(ROOT/'web/tracking_latest.json').write_text(json.dumps({'url':'/tracking/'+OUT.name+'/index.html'}),encoding='utf-8')
print('REPORT',OUT,flush=True)
