"""Connect confirmed stage-02 candidates to existing native ROI/QC proposals."""
import copy
import json
import os
from pathlib import Path
import sys
import uuid
import re

from .roi_studio_bridge import Session, atomic_json, digest, read_json
from .tarp_batch_qc import signature, track


def consensus_regions(predictions, names, shape):
    """Only propose a missing edge when two approved sources agree within 2 native pixels."""
    import cv2
    import numpy as np
    size=np.array([shape[1],shape[0]],dtype='float32');regions={};sources={}
    for name in names:
        choices=predictions.get(name,[])
        for i,(source,a) in enumerate(choices):
            a=np.asarray(a,dtype='float32')*size
            for other,b in choices[i+1:]:
                if source==other:continue
                b=np.asarray(b,dtype='float32')*size
                variants=[np.roll(p,k,axis=0) for p in (b,b[::-1]) for k in range(4)]
                b=min(variants,key=lambda p:float(np.max(np.linalg.norm(a-p,axis=1))))
                intersection=cv2.intersectConvexConvex(a,b)[0]
                union=abs(cv2.contourArea(a))+abs(cv2.contourArea(b))-intersection
                if np.max(np.linalg.norm(a-b,axis=1))<=2 and intersection/max(union,1e-9)>=.8:
                    regions[name]=((a+b)/2/size).tolist();sources[name]=[source,other];break
            if name in regions:break
    if set(regions)!=set(names):return {},{}
    polygons=[np.asarray(p,dtype='float32')*size for p in regions.values()]
    if any(cv2.intersectConvexConvex(a,b)[0]>1e-6 for i,a in enumerate(polygons) for b in polygons[:i]):return {},{}
    return regions,sources


def track_references(references, target, path, shot, progress=lambda text:None):
    """Try nearby named frames; keep existing registration/boundary acceptance limits."""
    def number(name):
        digits=re.findall(r'\d+',name)
        return int(digits[-1]) if digits else 0
    attempts=[];predictions={}
    for reference in sorted(references,key=lambda r:(not r['approved'],abs(number(r['shot'])-number(shot)))):
        progress(f"{shot} ← {reference['shot']} 이름·경계 연결 중")
        try:
            regions,details=track(reference['image'],target,reference['regions'],path)
            attempts.append(dict(reference_shot=reference['shot'],details=details))
            if set(regions)==set(reference['regions']):
                return regions,dict(reference_shot=reference['shot'],attempts=attempts),reference['names_require_review']
            if reference['approved']:
                for name in reference['regions']:
                    proposed=regions.get(name) or details.get('tarps',{}).get(name,{}).get('predicted')
                    if proposed:predictions.setdefault(name,[]).append((reference['shot'],proposed))
                consensus,sources=consensus_regions(predictions,reference['regions'],target.shape)
                if consensus:
                    return consensus,dict(method='two_approved_reference_consensus',sources=sources,attempts=attempts,boundary_review_required=True),False
        except Exception as exc:
            attempts.append(dict(reference_shot=reference['shot'],error=str(exc)))
    return {},dict(attempts=attempts),False


def name_candidates(qc, paths, names):
    """Propose small PNU names only when two unclipped bands agree; never approve."""
    import cv2
    import numpy as np
    from review_statistics import read
    expected=['PNU_s_3','PNU_s_36','PNU_s_56']
    if set(names)!=set(expected) or len(qc['regions'])!=3:
        raise ValueError('타프 이름 자동 구분이 불확실합니다. 상세 보기에서 이름을 확인하세요.')
    orders=[]
    for row in qc['rows']:
        if row.get('error') or row['usable_tarps']!=3:continue
        a=read(paths[row['native_suffix']-1]);values={}
        for name,tarp in row['tarps'].items():
            mask=np.zeros(a.shape,'uint8')
            cv2.fillConvexPoly(mask,np.rint(tarp['native_points']).astype('int32'),1)
            pixels=a[(mask>0)&np.isfinite(a)&(a!=65535)]
            values[name]=float(np.median(pixels))
        ordered=sorted(values,key=values.get)
        if all(values[b]>values[a]*1.08 for a,b in zip(ordered,ordered[1:])):
            orders.append(tuple(ordered))
    if len(orders)<2 or len(set(orders))!=1:
        raise ValueError('포화 또는 밝기 구분 부족으로 타프 이름을 확실히 제안하지 못했습니다.')
    mapping=dict(zip(orders[0],expected));result=copy.deepcopy(qc)
    result['regions']={mapping[n]:p for n,p in qc['regions'].items()}
    for row in result['rows']:
        row['tarps']={mapping[n]:t for n,t in row['tarps'].items()}
        # Embedded diagnostic images still carry temporary names; do not display them.
        row.pop('preview',None)
    result['name_assignment']='unapproved_two_band_brightness_order'
    return result


def prepare(config_path, progress=lambda text:None):
    progress('선택 영상 연결 중…')
    session=Session(config_path)
    selection=read_json(session.selection); rows=selection['selected']
    names=session.context['tarps'];output=Path(session.context['output'])
    os.environ['UAV_TARP_CONTEXT']=str(session.context_path)
    sys.path.insert(0,str(Path(__file__).parent/'tarp_tool/code'))
    from review_statistics import read
    from quick_tarp_qc import inspect
    from .tarp_native_proposal import propose
    from .roi_batch_import import import_proposals
    import numpy as np
    cache=output/'cache';cache.mkdir(exist_ok=True)
    pointer=cache/'roi_auto_latest.json'
    basis=dict(version=3,selection=session.selection_hash,sources=session.signatures,
               context=digest(session.context_path),library=session.library_hash,
               draft=digest(session.draft_path) if session.draft_path.exists() else None)
    old=read_json(pointer) if pointer.exists() else {}
    if old.get('basis')==basis and Path(old.get('batch','')).is_file():
        session.auto_result=read_json(old['batch']);return session
    folder=cache/('batch_qc_auto_'+uuid.uuid4().hex[:12]);folder.mkdir()
    result=dict(entries=[],roi_approved=False,selection_snapshot=[r['id'] for r in rows],
                selection_sha256=session.selection_hash,automatic=True)
    references=[]
    # Reuse existing named geometry first, without modifying its approval or pixel mask.
    ordered=sorted(rows,key=lambda r:not bool(session.images.get(r['shot'])))
    for i,row in enumerate(ordered,1):
        shot=row['shot'];paths=row['source_files'];entry=dict(id=row['id'],shot=shot,source_files=paths,reviewed=False)
        progress(f'자동 처리 {i-1}/{len(rows)} 완료 · {shot} 처리 중')
        try:
            before=signature(paths)
            if before!=[session.signatures[f'{shot}_{b}'] for b in range(1,11)]:
                raise ValueError('선택 영상과 원영상 경로가 일치하지 않습니다.')
            records=session.images.get(shot,[]);qc=None;regions={}
            if any(r.get('user_review')=='rejected_in_results' for r in records):
                raise ValueError('사용자가 아니다로 표시한 영상입니다. 상세 확인이 필요합니다.')
            native=read(paths[5]);height,width=native.shape
            if records:
                regions={r['tarp_id']:((np.asarray(r['outer'])-.5)/[width,height]).tolist() for r in records}
                saved=records[0].get('proposal_source')
                if saved and Path(saved).is_file():
                    candidate=next((e for e in read_json(saved)['entries'] if e['shot']==shot and e.get('source_signature')==before and e.get('result_path')),None)
                    if candidate:
                        previous=read_json(candidate['result_path'])
                        def normalized(p):
                            a=np.asarray(p);return a[np.lexsort((a[:,1],a[:,0]))]
                        if (previous.get('source_signature')==before and set(previous['regions'])==set(regions)
                                and all(np.allclose(normalized(regions[n]),normalized(previous['regions'][n]),atol=1e-7,rtol=0) for n in regions)):
                            qc=previous
            elif references:
                regions,entry['tracking'],entry['names_require_review']=track_references(references,native,paths[5],shot,progress)
            if not regions:
                proposed=propose(dict(context=str(session.context_path),row=row,state=selection,names=names))
                regions=proposed['regions']
                if not regions:
                    candidates=proposed['candidates']
                    if len(candidates)!=3:raise ValueError(f'자동 경계 {len(candidates)}개: 타프 구분이 애매합니다.')
                    temporary={f'candidate_{n}':p for n,p in enumerate(candidates)}
                    qc=name_candidates(inspect(paths,temporary,list(temporary),folder/'native_qc'),paths,names)
                    regions=qc['regions']
                    entry['names_require_review']=True
            if qc is None:qc=inspect(paths,regions,names,folder/'native_qc')
            if signature(paths)!=before:raise ValueError('자동 처리 중 원영상이 변경되었습니다.')
            entry['names_require_review']=entry.get('names_require_review',False) or bool(qc.get('name_assignment'))
            # Prefer original approved seeds; a recovered target is not silently promoted to approved.
            references.append(dict(shot=shot,image=native,regions=regions,
                                   approved=bool(records) and all(r.get('reviewed') for r in records),
                                   names_require_review=entry['names_require_review']))
            qc_path=folder/(shot+'_qc.json');atomic_json(qc_path,qc)
            entry.update(regions=regions,source_signature=before,result_path=str(qc_path),
                         summary=qc['summary'],bands=[dict(wavelength_nm=b['wavelength_nm'],usable_tarps=b['usable_tarps'],error=b.get('error')) for b in qc['rows']])
        except Exception as exc:
            entry['error']=str(exc)
        result['entries'].append(entry)
        atomic_json(folder/'batch.json',result)
        progress(f'자동 처리 {i}/{len(rows)} 완료')
    session.assert_current()
    progress('Trim·그림자 제외 결과 준비 중…')
    imported,session.import_holds=import_proposals(session)
    if imported:session.save_draft()
    basis['draft']=digest(session.draft_path) if session.draft_path.exists() else None
    atomic_json(pointer,dict(basis=basis,batch=str(folder/'batch.json')))
    session.auto_result=result
    return session
