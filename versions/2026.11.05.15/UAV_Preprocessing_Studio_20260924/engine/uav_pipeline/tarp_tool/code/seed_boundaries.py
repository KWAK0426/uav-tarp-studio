import cv2
import numpy as np

BANDS = (6, 4, 2, 10, 3)
VOTE_WEIGHTS = {6: 1.0, 4: 1.0, 2: 1.0, 10: 1.5}
PNU36_WEIGHTS = {4: .55, 3: .35, 6: .10}


def _clipped_fraction(image, points):
    mask = np.zeros(image.shape, 'uint8')
    cv2.fillConvexPoly(mask, np.rint(points).astype('int32'), 1)
    valid = (mask > 0) & np.isfinite(image)
    return float(np.mean(image[valid] >= 65520)) if valid.any() else 1.0


def candidates(image, seed, others):
    cx, cy, size = [float(seed[k]) for k in ('x', 'y', 'size')]
    width = float(seed.get('width', size))
    height = float(seed.get('height', size))
    if not all(np.isfinite(v) and 2 <= v <= 300 for v in (width, height)):
        return []
    expected_area = width * height
    short, long = sorted((width, height))
    h, w = image.shape
    radius = long * 1.1
    x0, y0 = max(0, int(cx-radius)), max(0, int(cy-radius))
    x1, y1 = min(w, int(cx+radius+1)), min(h, int(cy+radius+1))
    crop = image[y0:y1, x0:x1]
    valid = np.isfinite(crop) & (crop != 65535)
    if not valid.any(): return []
    lo, hi = np.percentile(crop[valid], [1, 99])
    if hi-lo < 1e-9: return []
    gray = (np.nan_to_num(np.clip((crop-lo)/(hi-lo),0,1))*255).astype('uint8')
    gray = cv2.GaussianBlur(gray,(3,3),0)
    edge = cv2.Canny(gray,35,90)
    distance = cv2.distanceTransform(255-edge,cv2.DIST_L2,3)
    masks = [cv2.morphologyEx(edge,cv2.MORPH_CLOSE,np.ones((3,3),'uint8'))]
    for threshold in [cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV]:
        masks.append(cv2.threshold(gray,0,255,threshold|cv2.THRESH_OTSU)[1])
    found = []
    for mask in masks:
        contours,_ = cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            if len(contour)<4:continue
            for epsilon in [.02,.04]:
                poly=cv2.approxPolyDP(contour,epsilon*cv2.arcLength(contour,True),True)
                if len(poly)!=4 or not cv2.isContourConvex(poly):continue
                local=poly[:,0,:].astype('float32'); points=local+np.array([x0,y0],dtype='float32')
                if cv2.pointPolygonTest(points,(cx,cy),False)<0:continue
                if any(cv2.pointPolygonTest(points,(float(o['x']),float(o['y'])),False)>=0 for o in others):continue
                area=cv2.contourArea(points)
                sides=np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1)
                if not .35*expected_area<area<1.8*expected_area or sides.min()<short*.35 or sides.max()>long*1.6:continue
                if np.linalg.norm(points.mean(axis=0)-[cx,cy])>long*.35:continue
                if (local[:,0]<=1).any() or (local[:,1]<=1).any() or (local[:,0]>=crop.shape[1]-2).any() or (local[:,1]>=crop.shape[0]-2).any():continue
                support=[]
                for a,b in zip(local,np.roll(local,-1,axis=0)):
                    samples=np.linspace(a,b,max(8,int(np.linalg.norm(b-a))))
                    ix=np.clip(samples[:,0].astype(int),0,crop.shape[1]-1);iy=np.clip(samples[:,1].astype(int),0,crop.shape[0]-1)
                    support.append(float(np.mean(distance[iy,ix]<=2)))
                if min(support)<.45:continue
                measured_ratio = sides.max()/max(sides.min(), 1e-9)
                ratio_penalty = abs(np.log(measured_ratio/(long/short)))
                region=np.zeros(crop.shape,'uint8');cv2.fillConvexPoly(region,np.int32(local),255)
                ring=cv2.dilate(region,np.ones((max(3,int(short*.2))|1,)*2,'uint8'))
                inside=crop[(region>0)&valid];outside=crop[(ring>0)&(region==0)&valid]
                if inside.size<9 or outside.size<9:continue
                spread=max((np.percentile(inside,75)-np.percentile(inside,25)+np.percentile(outside,75)-np.percentile(outside,25))/2,(hi-lo)*.02,1e-9)
                separation=float(abs(np.median(inside)-np.median(outside))/spread)
                score=float(np.mean(support))-.2*abs(np.log(area/expected_area))-.2*np.linalg.norm(points.mean(axis=0)-[cx,cy])/long-.15*ratio_penalty+.08*min(separation,4)
                vec=points[1]-points[0];angle=float(np.arctan2(vec[1],vec[0])%(np.pi/2))
                found.append({'points':points.tolist(),'score':float(score),'angle':angle,'support':support,'separability':separation})
    return sorted(found,key=lambda p:p['score'],reverse=True)


def find_boundaries(image, seeds):
    pools={n:candidates(image,s,[o for k,o in seeds.items() if k!=n]) for n,s in seeds.items()}
    results={}
    accepted=[]
    for n in sorted(seeds,key=lambda n: pools[n][0]['score'] if pools[n] else -1,reverse=True):
        options=pools[n]
        group=n.split('_')[0]
        angles=[p['angle'] for name,p in accepted if name.split('_')[0]==group]
        if angles:
            def rank(p):
                delta=min(abs(p['angle']-angles[0]),np.pi/2-abs(p['angle']-angles[0]))
                return p['score']-.2*delta/(np.pi/4)
            options=sorted(options,key=rank,reverse=True)
        winner=None
        for p in options:
            if any(cv2.intersectConvexConvex(np.float32(p['points']),np.float32(q['points']))[0]>.5 for _,q in accepted):continue
            winner=p;break
        if winner:
            accepted.append((n,winner));results[n]={'status':'review','points':winner['points'],'edge_support':winner['support'],'separability':winner['separability'],'candidate_score':winner['score']}
        else:
            results[n]={'status':'failed','reason':'중심·크기 조건을 만족하는 네 변이 부족하거나 다른 타프와 겹칩니다.'}
    return results


def find_multiband_boundaries(stack, seeds, raw_dn=False):
    by_band = [find_boundaries(a, seeds) for a in stack]
    results = {}
    for name in seeds:
        options = [(i,r[name]) for i,r in enumerate(by_band) if r[name]['status']=='review']
        if name == 'PNU_36':
            options = [(i, p) for i, p in options if BANDS[i] in PNU36_WEIGHTS
                       and not (raw_dn and _clipped_fraction(stack[i], p['points']) >= .01)]
        else:
            options = [(i, p) for i, p in options if BANDS[i] in VOTE_WEIGHTS]
        best = None
        for i,p in options:
            a=np.float32(p['points']);votes=[]
            for j,q in options:
                b=np.float32(q['points'])
                intersection=cv2.intersectConvexConvex(a,b)[0]
                union=cv2.contourArea(a)+cv2.contourArea(b)-intersection
                if union>0 and intersection/union>=.75:votes.append(j)
            weights = PNU36_WEIGHTS if name == 'PNU_36' else VOTE_WEIGHTS
            rank=(sum(weights.get(BANDS[j], 0) for j in votes),
                  weights.get(BANDS[i], 0) if name == 'PNU_36' else 0,
                  p['separability'], float(np.mean(p['edge_support'])), BANDS[i] == 10)
            if best is None or rank>best[0]:best=(rank,i,p,votes)
        if best is not None and len(best[3])>=2:
            results[name]=dict(best[2],selected_band=BANDS[best[1]],supporting_bands=[BANDS[i] for i in best[3]],method='adaptive_multiband_IoU075',confirmed=False)
        elif options and name != 'PNU_36':
            qualified=[(i,p) for i,p in options if p['separability']>=2.5 and np.mean(p['edge_support'])>=.65]
            if qualified:
                i,p=max(qualified,key=lambda item:(item[1]['separability']*(1.1 if BANDS[item[0]] == 10 else 1),
                                                   np.mean(item[1]['edge_support'])))
                results[name]=dict(p,selected_band=BANDS[i],supporting_bands=[BANDS[i]],method='adaptive_single_band_high_separation',confirmed=False)
            else:
                results[name]=dict(status='failed',reason='밴드별 경계는 있으나 타프와 지면의 분리도가 부족합니다.',band_diagnostics=[{'band':BANDS[j],'separability':round(q['separability'],3),'edge_support':round(float(np.mean(q['edge_support'])),3)} for j,q in options])
        else:
            results[name]=dict(status='failed',reason='두 개 이상 사용 가능한 밴드에서 일치하는 경계를 찾지 못했습니다. 포화·정합 상태를 확인하세요.')
    names=list(results)
    for i,name in enumerate(names):
        if results[name]['status']!='review':continue
        for other in names[:i]:
            if results[other]['status']=='review' and cv2.intersectConvexConvex(np.float32(results[name]['points']),np.float32(results[other]['points']))[0]>.5:
                results[name]=dict(status='failed',reason='다른 타프의 경계와 겹쳐 확인이 필요합니다.')
                break
    return results
