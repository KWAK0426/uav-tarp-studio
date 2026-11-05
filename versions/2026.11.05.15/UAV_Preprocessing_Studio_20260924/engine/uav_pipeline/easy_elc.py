"""Two explicit review gates around the existing calibration and validation tools."""
from __future__ import annotations

import argparse
import csv
from copy import deepcopy
from datetime import datetime
import hashlib
import html
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

from .config import load_config, save_config
from .band_scope import bands,describe
from .preparation import initialize, locations
from .roi_studio_bridge import atomic_json, digest, read_json
from .elc_fit import _current_manifest, fit
from .elc_priority import approved_shots, checked_coefficients
from .general_project import is_general, source_signature, raw_for_shots, source_shots, project_root


def pointer_path(cfg):
    # The pointer is project-local, while all numerical outputs are run-isolated.
    return Path(cfg["_config_path"]).with_suffix(".easy-elc.json")


def input_signature(cfg):
    settings = cfg["preparation"]
    hashes = {}
    keys = ("ed_csv", "gain_csv", "tarp_reflectance_csv")
    for key in keys if is_general(cfg) else ("gps_path", *keys):
        if not settings.get(key) or not Path(settings[key]).is_file():
            raise ValueError(f"필수 입력 파일을 먼저 연결하세요: {key}")
        hashes[key] = digest(settings[key])
    names = ("raw_directory", "gps_path", "ed_csv", "gain_csv", "reference_shot",
             "source_timezone", "time_band", "max_ed_delta_seconds", "tarp_reflectance_csv")
    value = {"dataset": cfg["dataset"], "band_names": cfg["band_names"],
             "settings": {key: settings.get(key) for key in names},
             "hashes": hashes}
    if is_general(cfg):
        from .preparation import latest_candidate_selection
        path, _, shots = latest_candidate_selection(cfg)
        raw = raw_for_shots(settings["raw_directory"], source_shots(cfg))
        value.update(general=cfg["general"], source_manifest=source_signature(cfg),
                     selection_sha256=digest(path), selected_shots=sorted(shots),
                     raw_sources=[(str(p), p.stat().st_size, p.stat().st_mtime_ns) for _, p in sorted(raw.items())])
        if settings.get("gps_path") and Path(settings["gps_path"]).is_file():
            hashes["gps_path"] = digest(settings["gps_path"])
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def state(cfg, value):
    path = pointer_path(cfg)
    atomic_json(path, value)
    if value.get("config"):
        atomic_json(Path(value["config"]).parent / "easy_status.json", value)
    print("EASY_STATUS " + json.dumps(value, ensure_ascii=False), flush=True)


def stage(cfg, name):
    print("EASY_STAGE " + name, flush=True)
    command = [sys.executable, "-m", "uav_pipeline.runner", "--config", cfg["_config_path"], "--stage", name]
    subprocess.run(command, cwd=Path(__file__).resolve().parents[1], check=True)


def check_metadata_tool():
    executable = shutil.which("exiftool")
    if not executable:
        raise ValueError("촬영시각·방사보정 metadata를 읽는 ExifTool이 없습니다. 설치·PATH를 확인하세요.")
    try:
        subprocess.run([executable, "-ver"], check=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("ExifTool 실행 검사 실패. Windows 실행 환경을 확인한 뒤 다시 시작하세요. " + str(exc)) from exc


def report(cfg, record):
    root = Path(record["config"]).parent
    paths = locations(cfg)
    links = [("계수·기준 타프 적합", paths["coef"] / "elc_coefficient_validation.html"),
             ("독립 영상·타프 검증", paths["coef"] / "elc_priority_reference_validation.html"),
             ("Ed·센서 보정 전후", paths["coef"] / "elc_raw_vs_ed_sensor_validation.html"),
             ("검증 수치 CSV", paths["coef"] / "elc_priority_reference_validation.csv"),
             ("실행 설정", Path(record["config"]))]
    linked = ([f'<li><a href="{html.escape(paths["ref"].resolve().as_uri(),quote=True)}">반사도 TIFF 폴더 열기</a></li>']
              if record['status']=='complete' else [])
    for label, path in links:
        if path.is_file():
            linked.append(f'<li><a href="{html.escape(path.resolve().as_uri(), quote=True)}">{label}</a></li>')
    if cfg["preparation"].get("integrated_plan_file"):
        plan = read_json(cfg["preparation"]["integrated_plan_file"])
        for label, name in (("촬영시각·Ed 매칭", "image_ed_matching.csv"), ("파장별 gain·Ed 비율", "image_band_factors.csv")):
            path = Path(plan["run"]) / name
            linked.append(f'<li><a href="{html.escape(path.as_uri(), quote=True)}">{label}</a></li>')
    roi_blocks = []
    for preview in read_json(record["roi_manifest"]).get("preview_images", []):
        path = Path(preview["path"])
        if path.is_file() and digest(path) == preview["sha256"]:
            roi_blocks.append(f'<figure><figcaption>{html.escape(preview["shot"])}</figcaption>'
                              f'<img style="max-width:100%;height:auto" src="{html.escape(path.as_uri(), quote=True)}"></figure>')
    summary = "<p>반사도 생성 후 독립 검증 수치가 추가됩니다.</p>"
    values_path = paths["coef"] / "elc_priority_reference_validation.csv"
    if values_path.is_file():
        with values_path.open(encoding="utf-8-sig", newline="") as stream:
            values = [r for r in csv.DictReader(stream) if r["role"] == "independent_validation"]
        body = []
        for band in sorted({int(r["band"]) for r in values}):
            pairs = [r for r in values if int(r["band"]) == band]
            errors = [float(r["observed_reflectance"]) - float(r["measured_reflectance"]) for r in pairs]
            rmse = math.sqrt(sum(e*e for e in errors) / len(errors))
            bias = sum(errors) / len(errors)
            measured = sum(float(r["measured_reflectance"]) for r in pairs) / len(pairs)
            relative = f"{100*rmse/measured:.2f}" if measured > 0 else "—"
            body.append(f'<tr><td>{html.escape(cfg["band_names"][str(band)])}</td><td>{len(pairs)}</td>'
                        f'<td>{rmse*100:.3f}</td><td>{bias*100:+.3f}</td><td>{relative}</td></tr>')
        summary = ('<table><thead><tr><th>대역</th><th>비교쌍(개)</th><th>RMSE(pp)</th><th>Bias(pp)</th><th>rRMSE(%)</th></tr></thead>'
                   '<tbody>' + ''.join(body) + '</tbody></table><p>기준 영상은 제외하고, 다른 영상의 타프 ROI 평균 반사도를 실측값과 비교합니다. '
                   '비교쌍은 영상×타프이며 화소별 오차가 아닙니다. RMSE·Bias는 반사도 백분율 포인트(pp), '
                   'rRMSE는 RMSE를 해당 대역 실측 반사도 평균으로 나눈 백분율입니다. Bias는 관측−실측입니다.</p>')
    title = {"awaiting_review": "계수 산출 완료 · 적용 승인 대기", "complete": "반사도 생성·검증 완료",
             "failed": "처리 중단 · 기록 확인", "running": "처리 중"}.get(record["status"], record["status"])
    page = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ELC 품질 보고서</title><style>body{{font:17px/1.65 'Malgun Gothic',sans-serif;max-width:1050px;margin:32px auto;padding:0 20px;color:#20252b}}h1{{font-size:27px}}h2{{font-size:21px}}a{{color:#136c57}}code{{overflow-wrap:anywhere}}li{{margin:10px 0}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}table{{border-collapse:collapse;width:100%}}td,th{{padding:9px;border-bottom:1px solid #d5dadd;text-align:right}}td:first-child,th:first-child{{text-align:left}}</style>
<h1>ELC 반사도 · 품질 보고서</h1><p>{html.escape(str(cfg['dataset']['date']))} · {html.escape(str(cfg['dataset']['flight']))}</p>
<h2>{title}</h2><p>범위: {'GPS 전체 촬영분' if record['full'] else '02에서 선정한 기준·검증 촬영분'}</p>
{describe(cfg)}
<p>기준 영상: <b>{html.escape(cfg['preparation']['reference_shot'])}</b></p>
<h2>다른 영상 독립 검증</h2>{summary}
<h2>품질 확인</h2><ul>{''.join(linked)}</ul>
<details><summary>사용한 원영상·승인 ROI 확인</summary><p>원영상 native B6의 표시용 확대입니다. 노랑은 외곽, 청록은 안쪽 ROI입니다. 다른 밴드에는 기록된 정합 변환으로 전달합니다. 이 이미지는 반사도 결과가 아닙니다.</p>{''.join(roi_blocks) or '<p>이전 승인 파일에는 ROI 확인 이미지가 없습니다.</p>'}</details>
<p>계수에 사용한 타프의 적합 오차와 다른 영상·검증용 타프의 오차를 구분합니다. 처리 완료는 과학적 품질 합격을 뜻하지 않습니다.</p>
<p>정의: L = aρ + b, ρ = (L × Ed(t₀)/Ed(t) − b)/a. 센서 gain은 실제 파장에 맞춰 한 번 적용합니다.</p>
<p>Ed는 실측 최근접 시각, 최대 5초 이내만 사용합니다. 시간 보간·외삽은 하지 않습니다. Ed 정규화는 관측각 BRDF 보정이 아닙니다.</p>
<details><summary>실행·출력 기록</summary><p>반사도 폴더: <code>{html.escape(str(paths['ref']))}</code></p>
<pre>{html.escape(json.dumps(record, ensure_ascii=False, indent=2))}</pre></details></html>'''
    path = root / "quality_report.html"
    path.write_text(page, encoding="utf-8")
    return str(path)


def prepare(source, full=False, apply_requested=False):
    from . import integrated, time_ed
    initialize(source)
    if full and source["preparation"].get("elc_only"):
        raise ValueError("ELC 영상만 처리 모드에서는 전체 생성을 선택할 수 없습니다.")
    approval = None
    if is_general(source):
        from .general_tarp_review import raw_tarp_scope, require_full_approval
        qc_rows,scope=raw_tarp_scope(source)
        if full:
            approval = require_full_approval(source)
    manifest, _, _, _ = approved_shots(source)
    signature = input_signature(source)
    base = locations(source)["base"]
    parent = project_root(source) if is_general(source) else base.parent
    run = parent / ("Easy_ELC_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8])
    cfg = deepcopy(source)
    cfg["output"]["directory"] = str(run / "Pipeline")
    settings = cfg["preparation"]
    settings.update(easy_output_directory=str(run), elc_only=not full, coefficient_csv="",
                    coefficient_basis_confirmed=False, integrated_plan_file="",
                    integrated_output_root=str(run / "Integrated"), approved_roi_manifest=str(manifest))
    path = run / "project.json"
    run.mkdir(parents=True, exist_ok=False)
    if is_general(source):
        from .config import DEFAULTS
        settings['auto_band_scope']=scope
        cfg['preprocess']['tools']['elc_apply']=DEFAULTS['preprocess']['tools']['elc_apply']
        atomic_json(run/'band_decision.json',dict(scope=scope,roi_sha256=digest(manifest),rows=qc_rows))
    save_config(path, cfg)
    cfg = load_config(path)
    record = {"status": "running", "phase": "time", "config": str(path), "full": bool(full),
              "source_config": source["_config_path"], "input_signature": signature,
              "roi_manifest": str(manifest), "roi_sha256": digest(manifest)}
    state(source, record)
    if is_general(source):
        request = 'review_coefficients_first'
        if apply_requested:
            request = 'operator_requested_full_scope' if full else 'operator_requested_selected_scope'
        record.update(auto_band_scope=scope,application_request=request)
    try:
        if full and is_general(cfg):
            # Fast conservative estimate from the approved native frame sizes.
            entries = read_json(read_json(manifest)['transforms_json'])
            reference = settings['reference_shot']
            per_frame = sum(4*entry['native_image_size'][0]*entry['native_image_size'][1]
                            for entry in entries if entry['shot']==reference)
            count = len(source_shots(cfg))
            required = int(count*per_frame*(1+len(bands(cfg))/10+
                            int(settings.get('integrated_save_radiance',False)))*1.2)
            free = shutil.disk_usage(run).free
            if free < required:
                raise ValueError(f'전체 {count}장 처리 공간 부족: 약 {required/1024**3:.1f} GiB 필요, '
                                 f'현재 {free/1024**3:.1f} GiB 남음. 결과 드라이브에 공간을 확보한 뒤 다시 실행하세요.')
        check_metadata_tool()
        print("EASY_STAGE capture_time", flush=True)
        time_ed.export(cfg, with_ed=False)
        record["phase"] = "integrated_preflight"
        state(source, record)
        plan = integrated.preflight(cfg)
        # Reserve reflectance plus normalized radiance (and optional cache) together.
        reflectance_bytes = sum(j["width"] * j["height"] * 4 for j in plan["jobs"]
                                if not is_general(cfg) or j['band'] in bands(cfg))
        required = plan["recommended_free_bytes"] + int(reflectance_bytes * 1.2)
        free = shutil.disk_usage(run).free
        print(f"간편 처리 권장 여유 {required / 1024**3:.2f} GiB / 남음 {free / 1024**3:.2f} GiB", flush=True)
        if free < required:
            raise ValueError("정규화 radiance와 반사도를 함께 저장할 여유 공간이 부족합니다.")
        record.update(phase="radiometric_sensor_ed", recommended_free_bytes=required, free_bytes=free)
        state(source, record)
        integrated.run(cfg)
        integrated.connect_result(cfg)
        save_config(path, cfg)
        record["phase"] = "elc_fit"
        state(source, record)
        if approval and approval['pilot'].get('kind') == 'connected_partial_result_v1':
            from .completed_result import reuse_coefficients
            result = reuse_coefficients(cfg,approval)
            record['coefficient_origin'] = 'approved_connected_result'
        else:
            result = fit(cfg)
        settings = cfg["preparation"]
        settings["coefficient_csv"] = result["coefficient_csv"]
        settings["coefficient_basis_confirmed"] = False
        save_config(path, cfg)
        record.update(status="awaiting_review", phase="review_coefficients", coefficient_sha256=digest(result["coefficient_csv"]))
        record["report"] = report(cfg, record)
        state(source, record)
        return record
    except Exception as exc:
        record.update(status="failed", error=str(exc))
        record["report"] = report(cfg, record)
        state(source, record)
        raise


def finish(source, approved_config_sha256):
    record = read_json(pointer_path(source))
    if record["status"] != "awaiting_review":
        raise ValueError("계수 산출 완료·승인 대기인 실행만 이어서 처리할 수 있습니다.")
    path = Path(record["config"])
    if not approved_config_sha256 or digest(path) != approved_config_sha256:
        raise ValueError("승인 화면 이후 실행 설정이 변경되었습니다. 다시 확인하세요.")
    cfg = load_config(path)
    if record["full"] and source["preparation"].get("elc_only"):
        raise ValueError("현재 프로젝트가 ELC 영상만 처리 모드입니다. 전체 실행 승인을 진행할 수 없습니다.")
    if input_signature(source) != record["input_signature"] or input_signature(cfg) != record["input_signature"]:
        raise ValueError("입력·기준·보정 자료가 바뀌었습니다. 새 간편 처리를 시작하세요.")
    manifest = _current_manifest(cfg)
    if str(manifest) != record["roi_manifest"] or digest(manifest) != record["roi_sha256"]:
        raise ValueError("승인 ROI가 바뀌었습니다. 새 간편 처리를 시작하세요.")
    if digest(cfg["preparation"]["coefficient_csv"]) != record["coefficient_sha256"]:
        raise ValueError("검토 이후 계수가 변경되었습니다.")
    checked_coefficients(cfg, cfg["preparation"]["reference_shot"])
    if is_general(cfg):
        from .general_tarp_review import check_raw_tarps
        check_raw_tarps(cfg)
    if cfg["preparation"].get("integrated_plan_file"):
        from .integrated import linked_plan
        plan = linked_plan(cfg)
        required = int(sum(j["width"] * j["height"] * 4 for j in plan["jobs"]
                           if not is_general(cfg) or j['band'] in bands(cfg)) * 1.2) + 16 * 1024**2
        if shutil.disk_usage(path.parent).free < required:
            raise ValueError("반사도 생성 전에 여유 공간이 줄었습니다. 공간을 확보한 뒤 다시 승인하세요.")
    cfg["preparation"]["coefficient_basis_confirmed"] = True
    save_config(path, cfg)
    record.update(status="running", phase="independent_validation", approved_config_sha256=approved_config_sha256)
    state(source, record)
    try:
        stage(cfg, "elc_apply_priority")
        if record["full"]:
            record["phase"] = "full_reflectance"
            state(source, record)
            stage(cfg, "elc_apply")
        if is_general(cfg):
            from .general_tarp_review import raw_tarp_scope
            _,current_scope=raw_tarp_scope(cfg)
            if current_scope!=cfg["preparation"]["auto_band_scope"]:
                raise ValueError('반사도 검증 중 밴드 판정이 변경되었습니다.')
            with (locations(cfg)['coef']/'elc_priority_reference_validation.csv').open(encoding='utf-8-sig') as stream:
                held=[r for r in csv.DictReader(stream) if r['role']=='independent_validation']
            for b in bands(cfg):
                rows=[r for r in held if int(r['band'])==b]
                if len({r['tarp'] for r in rows})<2 or any(int(r['n_pixels'])<5 or not math.isfinite(float(r['bias'])) for r in rows):
                    raise ValueError(f'B{b}: 유효한 독립 검증 타프가 부족합니다.')
        record.update(status="complete", phase="quality_report")
        record["report"] = report(cfg, record)
        state(source, record)
        return record
    except Exception as exc:
        record.update(status="failed", error=str(exc))
        record["report"] = report(cfg, record)
        state(source, record)
        raise


def auto_selected(source):
    if not is_general(source):raise ValueError('현재 회차 프로젝트가 필요합니다.')
    record=prepare(source,full=False,apply_requested=True)
    if record!=read_json(pointer_path(source)):raise ValueError('현재 실행이 변경되었습니다.')
    return finish(source,digest(record['config']))


def auto_full(source):
    from .general_tarp_review import require_full_approval
    require_full_approval(source)
    record = prepare(source,full=True,apply_requested=True)
    if record != read_json(pointer_path(source)):
        raise ValueError('현재 실행이 변경되었습니다.')
    return finish(source,digest(record['config']))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--action", choices=("prepare", "finish", "auto_selected", "auto_full"), required=True)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--approved-config-sha256", default="")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.action=="auto_selected":
        if args.full:raise ValueError('정상 밴드 자동 산출은 선택 영상만 처리합니다.')
        auto_selected(cfg)
    elif args.action=="auto_full":
        auto_full(cfg)
    elif args.action=="prepare":prepare(cfg,args.full)
    else:finish(cfg,args.approved_config_sha256)


if __name__ == "__main__":
    main()
