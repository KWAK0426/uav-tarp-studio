"""Flight-scoped, snapshot-based jobs adapted from the reviewed side workflow."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from context import ROOT, CONFIG

LOCK = threading.RLock()
PROCESS = None
WORKER_ACTIVE = False
STATE_FILE = ROOT / 'tracking_state.json'
STATE = {'status': 'idle', 'done': 0, 'total': 0, 'message': '자동 추적 대기'}
if STATE_FILE.exists():
    STATE.update(json.loads(STATE_FILE.read_text(encoding='utf-8')))


def persist():
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(STATE_FILE)


def selection_path(name):
    if not isinstance(name, str) or Path(name).name != name or not name.startswith('selected_') or not name.endswith('.json'):
        raise ValueError('잘못된 선택 파일')
    path = ROOT / 'selections' / name
    if not path.is_file():
        raise ValueError('선택 파일이 없습니다.')
    return path


def tarps_for_shot(selection, shot):
    row = next((r for r in selection['selected'] if r['shot'] == shot), None)
    if row is None:
        raise ValueError(f'{shot}: 선택되지 않은 영상입니다.')
    locations = selection.get('locations') or []
    if not locations:
        return list(CONFIG['tarps'])
    covered = set(row.get('coverage_locations') or [loc['id'] for loc in locations])
    names = {name for loc in locations if loc['id'] in covered for name in loc['tarps']}
    if not names or not names.issubset(CONFIG['tarps']):
        raise ValueError(f'{shot}: 타프 위치 배정이 유효하지 않습니다.')
    return [name for name in CONFIG['tarps'] if name in names]


def references(name):
    selection = json.loads(selection_path(name).read_text(encoding='utf-8'))
    allowed = {r['shot'] for r in selection['selected']}
    paths = list((ROOT/'selections').glob('roi_history_*.json')) + list((ROOT/'selections').glob('roi_seeds_*.json'))
    refs = {}
    for path in sorted(paths, key=lambda p: p.stat().st_mtime_ns):
        d = json.loads(path.read_text(encoding='utf-8'))
        if d.get('selection_file') == name and d.get('reference') in allowed and set(d.get('tarps', {})) == set(tarps_for_shot(selection, d['reference'])):
            refs[d['reference']] = d['tarps']
    return refs


def status():
    with LOCK:
        if STATE['status'] == 'running' and PROCESS is None and not WORKER_ACTIVE:
            import psutil
            alive = False
            try:
                cmd = psutil.Process(STATE.get('pid', -1)).cmdline()
                alive = STATE.get('snapshot') in cmd
            except (psutil.Error, ValueError):
                pass
            if not alive:
                report = ROOT/'web'/'tracking'/STATE['job_id']/'index.html'
                STATE.update(status='complete' if report.is_file() else 'interrupted',
                             message='서버 재시작 후 결과 복구 · 검수 필요' if report.is_file() else '중단된 작업 · 입력은 보존됨. 다시 실행하세요.')
                if report.is_file():
                    STATE.update(done=STATE['total'], report='/tracking/'+STATE['job_id']+'/index.html')
                persist()
        result = dict(STATE)
        result.update(running=STATE['status'] == 'running', url=STATE.get('report'),
                      error=STATE['message'] if STATE['status'] in ('failed', 'interrupted') else None)
        return result


def start(name):
    global PROCESS, WORKER_ACTIVE
    with LOCK:
        if status()['running']:
            raise ValueError('이미 자동 추적 중입니다.')
        selection = json.loads(selection_path(name).read_text(encoding='utf-8'))
        shots = [r['shot'] for r in selection['selected']]
        if len(shots) != len(set(shots)) or not set(shots).issubset(CONFIG['gps_shots']):
            raise ValueError('선택 영상 중복 또는 현재 회차 GPS 불일치')
        refs = {s: t for s, t in references(name).items() if all(
            len(t.get(n, {}).get('points', [])) == 4 and t[n].get('status') != 'failed'
            for n in tarps_for_shot(selection, s))}
        groups = {tuple(tarps_for_shot(selection, shot)) for shot in shots}
        missing = [group for group in groups if sum(set(group).issubset(ref) for ref in refs.values()) < 2]
        if missing:
            raise ValueError('타프 위치별 기준 영상 최소 2장씩 경계를 저장하세요: ' + ', '.join('/'.join(g) for g in missing))
        run = time.strftime('%Y%m%d_%H%M%S') + '_' + str(time.time_ns())[-9:]
        out = ROOT/'web'/'tracking'/run
        out.mkdir(parents=True)
        snapshot = out/'input_snapshot.json'
        snapshot.write_text(json.dumps({'selection': selection, 'selection_file': name, 'references': refs,
                                        'context': CONFIG, 'confirmed': False}, ensure_ascii=False, indent=2), encoding='utf-8')
        STATE.clear()
        STATE.update(status='running', done=0, total=len(shots)*2, message='입력 스냅샷 저장 · 정합 준비',
                     report=None, selection_file=name, job_id=run, snapshot=str(snapshot))
        persist()
        WORKER_ACTIVE = True
        threading.Thread(target=run_job, args=(snapshot, out), daemon=True).start()
        return dict(STATE, running=True)


def cancel(job_id):
    with LOCK:
        if job_id != STATE.get('job_id') or not status()['running']:
            raise ValueError('현재 실행 작업이 아닙니다.')
        if PROCESS is None:
            raise ValueError('서버 재시작 이전 작업은 이 화면에서 취소하지 않습니다. 기존 처리 종료를 기다리세요.')
        STATE['cancel_requested'] = True
        PROCESS.terminate()
        persist()
        return status()


def start_statistics(job_id, trim, percentile_low=5, percentile_high=95):
    global WORKER_ACTIVE
    with LOCK:
        if status()['running']: raise ValueError('현재 작업 종료 후 실행하세요.')
        from review import statistics_snapshot
        data = statistics_snapshot(job_id, trim, percentile_low, percentile_high)
        run = time.strftime('%Y%m%d_%H%M%S')+'_'+str(time.time_ns())[-9:]
        out=ROOT/'web'/'tracking'/run;out.mkdir(parents=True)
        snapshot=out/'input_snapshot.json';snapshot.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
        STATE.clear();STATE.update(status='running',done=0,total=len(data['images'])*10,message='승인 ROI 통계 준비',
                                   report=None,job_id=run,snapshot=str(snapshot),source_tracking_job=job_id,
                                   job_kind='statistics')
        WORKER_ACTIVE=True;persist()
        threading.Thread(target=run_job,args=(snapshot,out,'review_statistics.py'),daemon=True).start()
        return dict(STATE,running=True)


def run_job(snapshot, out, program='track_saved.py'):
    global PROCESS, WORKER_ACTIVE
    try:
        with (out/'run.log').open('w', encoding='utf-8') as log:
            with LOCK:
                PROCESS = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name(program)), '--snapshot', str(snapshot)],
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                STATE['pid'] = PROCESS.pid
                persist()
            for line in PROCESS.stdout:
                log.write(line)
                log.flush()
                if line.startswith('PROGRESS '):
                    item = json.loads(line[9:])
                    with LOCK:
                        STATE.update(done=min(int(item['done']), STATE['total']), message=str(item['message']))
                        persist()
            rc = PROCESS.wait()
        with LOCK:
            if STATE.get('cancel_requested'):
                STATE.update(status='cancelled', message='취소됨 · 기준 ROI와 입력 스냅샷 보존')
            elif rc or not (out/'index.html').is_file():
                STATE.update(status='failed', message=('ROI 통계 생성 실패' if program=='review_statistics.py' else '자동 추적 실패')+' · '+str(out/'run.log'))
            else:
                STATE.update(status='complete', done=STATE['total'], message='추적 완료 · ROI 검수 필요' if program=='track_saved.py' else 'ROI 통계 완료 · ELC 보정 기준 확인 필요', report='/tracking/'+out.name+'/index.html')
            persist()
    except Exception as exc:
        with LOCK:
            STATE.update(status='failed', message=('ROI 통계 생성 실패' if program=='review_statistics.py' else '자동 추적 실패')+' · '+str(exc))
            persist()
    finally:
        with LOCK:
            PROCESS = None
            WORKER_ACTIVE = False
