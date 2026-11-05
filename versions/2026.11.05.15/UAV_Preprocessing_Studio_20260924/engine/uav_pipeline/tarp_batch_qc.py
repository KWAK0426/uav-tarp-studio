"""Track one named native boundary set and inspect candidate frames, without approvals."""
import html
import json
import os
from pathlib import Path
import sys
import time


def signature(paths):
    return [[str(Path(p).resolve()), Path(p).stat().st_size, Path(p).stat().st_mtime_ns] for p in paths]


def action(row):
    if row.get('error'):
        return '보류: 경계·밴드 정합 확인'
    if row['usable_tarps'] < 2:
        return '이 영상·밴드 사용 불가: 다른 영상 선택, 없으면 밴드 제외'
    return '포화 기준 충족: 경계 확인 후 ROI·그림자 검토로 진행'


def track(source, target, regions, path):
    import importlib.util
    import cv2
    import numpy as np
    from review_statistics import transform
    spec = importlib.util.spec_from_file_location('batch_boundary_core', Path(__file__).resolve().parents[3]/'tarp_center_roi_tool/core.py')
    core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)
    h, quality = transform(source, target, False)
    reverse, _ = transform(target, source, False)
    raster = core.Raster(path)
    found, details = {}, {}
    try:
        for name, polygon in regions.items():
            proposal=None
            try:
                # QC polygons use pixel-center coordinates, unlike formal ROI edge coordinates.
                native = np.float32(polygon) * [source.shape[1], source.shape[0]]
                native = native.astype('float32')
                predicted = cv2.perspectiveTransform(native[None], h)[0]
                back = cv2.perspectiveTransform(predicted[None], reverse)[0]
                error = float(np.max(np.linalg.norm(back-native, axis=1)))
                if error > 2:
                    raise ValueError(f'왕복 정합 오차 {error:.2f}px > 2px')
                if (not np.isfinite(predicted).all() or not cv2.isContourConvex(predicted)
                        or (predicted < 0).any() or (predicted[:,0] >= raster.width).any()
                        or (predicted[:,1] >= raster.height).any()):
                    raise ValueError('예측 경계가 영상 밖이거나 잘못 정합됨')
                proposal=(predicted/[raster.width,raster.height]).tolist()
                size = float(np.sqrt(abs(cv2.contourArea(predicted))))
                candidates = raster.propose(predicted.mean(0)+.5, 1, max(32,min(512,int(size*3))))
                ranked = []
                for candidate in candidates:
                    points = np.float32(candidate['points'])-.5
                    intersection = cv2.intersectConvexConvex(predicted,points)[0]
                    union = abs(cv2.contourArea(predicted))+abs(cv2.contourArea(points))-intersection
                    ranked.append((float(intersection/max(union,1e-9)),points))
                if not ranked or max(r[0] for r in ranked) < .5:
                    raise ValueError('예측 위치에 일치하는 경계 없음')
                overlap, points = max(ranked,key=lambda r:r[0])
                found[name] = (points/[raster.width,raster.height]).tolist()
                details[name] = dict(roundtrip_max_px=error,boundary_iou=overlap,status='review_required')
            except (ValueError,cv2.error) as exc:
                details[name] = dict(status='failed',reason=str(exc))
                if proposal is not None:
                    details[name].update(predicted=proposal,roundtrip_max_px=error)
    finally:
        raster.ds = None
    return found, dict(registration=quality,tarps=details)


def report(result, folder):
    rows=[]
    for entry in result['entries']:
        shot=html.escape(entry['shot'])
        if entry.get('error'):
            rows.append(f'<tr><td>{shot}</td><td colspan="4">보류 · {html.escape(entry["error"])}</td></tr>')
            continue
        qc=json.loads(Path(entry['result_path']).read_text(encoding='utf-8'))
        for band in qc['rows']:
            percentages=' / '.join(f'{html.escape(n)}: {t["saturated_pct"]:.2f}%' for n,t in band['tarps'].items())
            rows.append(f'<tr><td>{shot}</td><td>{band["wavelength_nm"]} nm</td><td>{band["usable_tarps"]}</td><td>{percentages}</td><td>{action(band)}</td></tr>')
    page='<!doctype html><meta charset="utf-8"><title>타프 포화 사전검사</title><style>body{font:16px sans-serif;margin:40px;line-height:1.6}table{border-collapse:collapse}td,th{padding:9px;border:1px solid #bbb}th{background:#e1f3ee}</style>'
    page+='<h1>타프 포화 사전검사</h1><p>한 장에서 지정한 이름과 경계를 자동 추적했습니다. 자동 경계는 아직 승인되지 않았습니다. 영상별 경계를 확인하고 잘못 잡힌 것만 수정하세요.</p><p>포화 화소 1% 이상인 타프는 해당 밴드에서 제외합니다. 유효 타프가 두 개 미만이면 다른 영상을 선택하고, 대체 영상이 없으면 해당 밴드 산출을 보류합니다. 통과 후에도 내부 그림자 제외·Ed 확인·ELC 검증이 필요합니다.</p><p>이름은 기준 영상에서 그대로 전달하며 밝기로 다시 배정하지 않습니다. 그림자 마스크와 과학적 승인은 복사하지 않습니다.</p><table><tr><th>영상</th><th>파장</th><th>유효 타프 수</th><th>타프별 포화율</th><th>다음 조치</th></tr>'+''.join(rows)+'</table>'
    (folder/'report.html').write_text(page,encoding='utf-8')


def run(request):
    os.environ['UAV_TARP_CONTEXT']=request['context']
    sys.path.insert(0,str(Path(__file__).parent/'tarp_tool/code'))
    from review_statistics import read
    from quick_tarp_qc import inspect
    reference=request['reference']; rows=request['candidates']
    source_paths=reference['source_files']
    before=signature(source_paths)
    if before != request['source_signature']:
        raise ValueError('기준 영상이 검사 이후 변경되었습니다. 기준 영상을 다시 확인하세요.')
    source=read(source_paths[5]);regions=request['regions']
    folder=Path(request['output']);folder.mkdir(parents=True,exist_ok=False)
    (folder/'request.json').write_text(json.dumps(request,ensure_ascii=False,indent=2),encoding='utf-8')
    result=dict(reference_shot=reference['shot'],entries=[],roi_approved=False,boundary_review_required=True,
                selection_snapshot=[r['id'] for r in rows],created=time.strftime('%Y-%m-%dT%H:%M:%S'))
    for index,row in enumerate(rows,1):
        entry=dict(id=row['id'],shot=row['shot'],source_files=row['source_files'],reviewed=False)
        try:
            target_before=signature(row['source_files'])
            if row['id']==reference['id']:
                found,details=regions,dict(method='user_reference')
            else:
                found,details=track(source,read(row['source_files'][5]),regions,row['source_files'][5])
            entry.update(regions=found,tracking=details,source_signature=target_before)
            if len(found)<2:
                raise ValueError('타프 두 개 이상 추적하지 못함: 이 영상의 경계만 직접 수정하세요.')
            qc=inspect(row['source_files'],found,request['names'],folder/'native_qc')
            if signature(row['source_files'])!=target_before or signature(source_paths)!=before:
                raise ValueError('추적·검사 중 입력 영상 변경됨')
            record=folder/(row['shot']+'_qc.json')
            record.write_text(json.dumps(qc,ensure_ascii=False),encoding='utf-8')
            entry.update(result_path=str(record),summary=qc['summary'],bands=[dict(wavelength_nm=b['wavelength_nm'],usable_tarps=b['usable_tarps'],error=b.get('error'),action=action(b)) for b in qc['rows']])
        except Exception as exc:
            entry['error']=str(exc)
        result['entries'].append(entry)
        temporary=folder/'batch.tmp'
        temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(folder/'batch.json')
        print(f'{index}/{len(rows)} {row["shot"]}: {entry.get("error",entry.get("summary"))}',flush=True)
    report(result,folder)
    return folder/'batch.json'


if __name__=='__main__':
    request=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    print(run(request),flush=True)
