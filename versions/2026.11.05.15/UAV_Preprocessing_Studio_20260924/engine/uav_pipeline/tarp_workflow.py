"""Launch the existing ROI workflow with explicit flight-scoped inputs."""
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import threading
from datetime import datetime
from pathlib import Path

from .dataset import FLIGHTS, discover, list_dates
from .preparation import initialize, locations, _gps_shots


def _reference_timing(cfg, output, progress=None):
    """Cache GPS-retained capture times used to pick the midpoint tarp frame."""
    from .time_ed import exif_times, image_rows, time_signature, timestamp
    cache = output / 'reference_timing.json'
    rows = []
    if cache.is_file():
        try:
            saved = json.loads(cache.read_text(encoding='utf-8'))
            rows = saved['rows']
            if json.loads(json.dumps(time_signature(cfg, rows))) != saved['signature']:
                rows = []
        except (OSError, ValueError, KeyError):
            rows = []
    if not rows:
        if progress:
            progress('원영상 촬영시간 확인 중 · 처음 열 때 시간이 걸립니다')
        rows = image_rows(cfg, read_times=lambda paths: exif_times(paths, progress=progress))
        saved = {'rows': rows, 'signature': json.loads(json.dumps(time_signature(cfg, rows)))}
        cache.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
    valid = [row for row in rows if row.get('status') == 'ok' and row.get('capture_time_utc')]
    if not valid:
        raise ValueError('GPS 유효 영상의 촬영시간을 읽지 못해 기준 영상을 자동 선택할 수 없습니다.')
    times = {row['shot']: row['capture_time_utc'] for row in valid}
    trajectory = {row['shot']: {'capture_time_utc': row['capture_time_utc'],
                                'longitude': row['longitude'], 'latitude': row['latitude']}
                  for row in valid if 'longitude' in row and 'latitude' in row}
    ordered = sorted(timestamp(value, 'UTC') for value in times.values())
    midpoint = ordered[0] + (ordered[-1] - ordered[0]) / 2
    return {'observation_start_utc': ordered[0].isoformat(), 'observation_end_utc': ordered[-1].isoformat(),
            'observation_midpoint_utc': midpoint.isoformat(), 'capture_time_utc_by_shot': times,
            'trajectory_by_shot': trajectory,
            'selection_rule': 'selected_tarp_frame_nearest_full_GPS_observation_midpoint'}


def defaults(cfg):
    initialize(cfg)
    cfg.setdefault('tarp_workflow', {}).setdefault('reference_mosaic', '')
    # ROI geometry is always defined on the native raw frames. Calibrated products
    # receive the approved per-band polygons later after an exact geometry check.
    cfg['tarp_workflow']['boundary_mode'] = '원영상 ROI'


def reference_mosaic_candidates(cfg):
    """List panel mosaics that can be used only as a tarp-location reference."""
    defaults(cfg)
    d = cfg.get("dataset", {})
    root = Path(d.get("root", "")).resolve()
    if not root.is_dir() or not d.get("date") or not d.get("flight"):
        raise ValueError("날짜·회차를 먼저 연결하세요.")
    current_day = datetime.strptime(d["date"][:10], "%Y_%m_%d").date()
    found = []
    seen = set()
    for date_name in list_dates(root):
        ymd = date_name[:10].replace("_", "")[2:]
        try:
            distance = abs((datetime.strptime(date_name[:10], "%Y_%m_%d").date() - current_day).days)
        except ValueError:
            continue
        for flight in FLIGHTS:
            panel = root / date_name / "02_Processed" / f"{flight}_{ymd}" / f"{flight}_{ymd}_01_Panel_Meta"
            if not panel.is_dir():
                continue
            for path in panel.rglob("*"):
                if (not path.is_file() or path.stat().st_size <= 0
                        or path.suffix.lower() not in (".tif", ".tiff")
                        or "mosaic" not in path.stem.lower()):
                    continue
                resolved = path.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError:
                    continue
                key = str(resolved).casefold()
                if key in seen:
                    continue
                seen.add(key)
                same_date = date_name == d["date"]
                same_flight = same_date and flight == d["flight"]
                scope = "해당 회차" if same_flight else "같은 날짜" if same_date else "다른 날짜·위치 참조"
                found.append({
                    "path": str(resolved),
                    "date": date_name[:10].replace("_", "-"),
                    "flight": flight,
                    "scope": scope,
                    "label": f"{date_name[:10].replace('_', '-')} · {flight[0]}회차 · {path.name}",
                    "score": (0 if same_flight else 1 if same_date else 2, distance,
                              FLIGHTS.index(flight), path.name.casefold()),
                })
    found.sort(key=lambda item: item["score"])
    for item in found:
        item.pop("score", None)
    return found


def reference_mosaic_status(cfg):
    """Resolve the mosaic without allowing a fallback to override a current-flight mosaic."""
    defaults(cfg)
    d = cfg.get("dataset", {})
    if not d.get("root") or not d.get("date") or not d.get("flight"):
        return {"available": False, "own": "", "selected": "", "role": "missing"}
    panel = discover(d["root"], d["date"], d["flight"], "Panel")
    own = panel["paths"].get("mosaic", "")
    if own and Path(own).is_file():
        return {"available": True, "own": str(Path(own).resolve()), "selected": str(Path(own).resolve()),
                "role": "same_flight"}
    fallback = cfg.get("tarp_workflow", {}).get("reference_mosaic", "")
    if fallback and Path(fallback).is_file():
        return {"available": True, "own": "", "selected": str(Path(fallback).resolve()),
                "role": "location_reference_only"}
    return {"available": False, "own": "", "selected": "", "role": "missing"}


def context(cfg, progress=None):
    defaults(cfg)
    from .general_project import is_general
    if is_general(cfg):
        from .general_tarp_review import map_context
        return map_context(cfg)
    d=cfg['dataset']
    panel=discover(d['root'],d['date'],d['flight'],'Panel')
    own=panel['paths']['mosaic']
    resolved = reference_mosaic_status(cfg)
    mosaic=resolved['selected']
    if not mosaic or not Path(mosaic).is_file():
        raise ValueError('해당 회차 모자이크가 없습니다. 02 단계에서 위치 참조용 모자이크를 선택하세요. 다른 회차·날짜도 가능합니다.')
    photos=cfg['tarp_workflow'].get('photos_override') or panel['paths']['photos']
    if not photos or not Path(photos).is_dir():
        raise ValueError('후보 검색에는 선택한 날짜·회차의 Panel Photos가 필요합니다. 참조 모자이크 날짜의 Photos를 대신 쓰지 않습니다.')
    raw=cfg['preparation']['raw_directory']
    if not raw or not Path(raw).is_dir():
        raise ValueError('선택한 회차의 원영상 폴더가 필요합니다.')
    gps=cfg['preparation']['gps_path']
    shots=sorted(_gps_shots(gps))
    p=locations(cfg)
    raw_geometry = True
    from .calibration_presets import expand_tarp_names
    tarps=expand_tarp_names(cfg['tarp_workflow'].get('tarp_names',['PNU_3','PNU_36','PNU_56']))
    if (not isinstance(tarps,list) or len(tarps)<2
            or any(not isinstance(n,str) or not n.strip() for n in tarps)
            or len(set(tarps))!=len(tarps)):
        raise ValueError('서로 다른 타프 이름을 두 개 이상 지정하세요.')
    result={'adapter_version':4,'label':d['date'][:10].replace('_','-')+' · '+d['flight'][0]+'회차',
            'date':d['date'],'flight':d['flight'],'mosaic':str(Path(mosaic).resolve()),
            'mosaic_role':resolved['role'],
            'photos':photos,'raw':raw,'radiance':str(p['rad']),'gps_shots':shots,
            'image_source':raw if raw_geometry else str(p['rad']),
            'source_kind':'raw_DN_boundary_only' if raw_geometry else 'radiometric_radiance',
            'candidate_preview_kind':'panel_photos' if raw_geometry else 'radiometric_radiance',
            'tarps':tarps}
    if d['date'][:10].replace('_','-') == '2026-06-10':
        raise ValueError('6/10의 PNU·NIAS 혼합 타프 명칭은 별도 연결이 필요합니다. 현재 연결은 PNU 세 타프용입니다.')
    stat=Path(mosaic).stat()
    result['mosaic_signature']=[stat.st_size,stat.st_mtime_ns]
    identity=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
    result['identity']=identity
    result['output']=str(p['roi']/'workflow'/identity)
    result['dataset_root']=str(Path(d['root']).resolve())
    result['location_library']=str(Path(__file__).resolve().parents[2]/'research'/'tarp_locations.json')
    package=Path(__file__).with_name('tarp_tool')
    revision=[(str(path.relative_to(package)),path.stat().st_size,path.stat().st_mtime_ns)
              for path in sorted(package.rglob('*')) if path.suffix in ('.py','.html','.js','.css')]
    result['code_revision']=hashlib.sha256(json.dumps(revision).encode()).hexdigest()[:16]
    output = Path(result['output'])
    output.mkdir(parents=True, exist_ok=True)
    try:
        result['reference_timing'] = _reference_timing(cfg, output, progress=progress)
    except Exception as exc:
        result['reference_timing'] = {'error': str(exc), 'selection_rule': 'manual_fallback_only'}
    return result


def _render_web(data, root):
    package=Path(__file__).with_name('tarp_tool')
    web=root/'web';web.mkdir(exist_ok=True)
    for name in ('index.html','roi.html','studio.js','tracking_ui.js','review.html'):
        text=(package/'web'/name).read_text(encoding='utf8')
        text=text.replace('7월 16일 · 김해 1회차 타프 후보 검증','타프 위치·후보 영상').replace('김해 7/16 1회차','위치 참조').replace('김해 7/16 1회차 · 타프 중심 지정','타프 중심·경계 설정')
        text=text.replace('원영상 DN · 경계 설정용','선택 회차 방사보정 영상').replace('선택 번호의 원영상 DN','선택 번호의 방사보정 영상')
        text=text.replace(' · 원영상 누락',' · 방사보정 누락').replace('추적 결과 · 공간 trim 5%','자동 추적 결과 · 검수 전')
        if name in ('index.html','roi.html'): text+='\n<script src="/studio.js"></script>'
        if name == 'roi.html': text+='\n<script src="/tracking_ui.js"></script>'
        if data['source_kind']=='raw_DN_boundary_only':
            text=text.replace('방사보정 RGB','Panel Photos 후보 확인 RGB').replace('선택 회차 방사보정 영상','현재 회차 Panel Photos · 후보 선정 전용').replace('선택 번호의 방사보정 영상','선택 번호의 Panel Photo').replace('방사보정 누락','연결할 원영상 누락')
            text=text.replace('3 ROI 방식', '3 후보 저장').replace('<h2>ROI 설정 방식</h2>', '<h2>선택 후보 저장</h2>')
        (web/name).write_text(text,encoding='utf8')


def launch(cfg, page="", progress=None):
    if page not in ("", "/", "/roi.html"):
        raise ValueError("타프 도구 화면 경로를 확인하세요.")
    data=context(cfg, progress=progress)
    if progress:
        progress('촬영시간 확인 완료 · 타프 화면 준비 중')
    root=Path(data['output']);root.mkdir(parents=True,exist_ok=True)
    _render_web(data, root)
    suffix = "" if page in ("", "/") else page
    status=root/'server.json'
    if status.exists():
        try:
            old=json.loads(status.read_text(encoding='utf8'))
            url=old['url']
            if not url.startswith('http://127.0.0.1:'): raise ValueError('Invalid server URL')
            with urllib.request.urlopen(url+'/context',timeout=2) as r:
                live=json.load(r)
            if (live['identity']==data['identity'] and live.get('reference_timing') == data.get('reference_timing')
                    and live.get('code_revision') == data['code_revision']):
                return url.rstrip('/')+suffix
            if live['identity']==data['identity']:
                with urllib.request.urlopen(url+'/tracking/status', timeout=2) as r:
                    if json.load(r).get('running'):
                        raise RuntimeError('타프 자동 추적이 진행 중입니다. 완료 후 ROI 도구를 다시 열어 업데이트하세요.')
                os.kill(int(old['pid']), signal.SIGTERM)
                time.sleep(.5)
        except (OSError,ValueError,KeyError):
            pass
    package=Path(__file__).with_name('tarp_tool')
    config=root/'context.json'
    config.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    env=os.environ.copy();env['UAV_TARP_CONTEXT']=str(config);env['PYTHONIOENCODING']='utf-8'
    started=time.time()
    with (root/'server.log').open('a',encoding='utf8') as log:
        proc=subprocess.Popen([sys.executable,str(package/'code/server.py')],env=env,stdout=log,stderr=subprocess.STDOUT,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError('타프 도구 시작 실패: '+str(root/'server.log'))
        if status.exists() and status.stat().st_mtime>=started:
            value=json.loads(status.read_text(encoding='utf8'))
            if value['pid']==proc.pid:
                threading.Thread(target=proc.wait,daemon=True).start()
                return value['url'].rstrip('/')+suffix
        time.sleep(.25)
    proc.terminate();proc.wait(timeout=10)
    raise RuntimeError('타프 도구 시작 시간 초과. '+str(root/'server.log'))
