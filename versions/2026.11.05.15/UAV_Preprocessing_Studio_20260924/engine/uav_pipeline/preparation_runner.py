"""Run reviewed generated settings through the existing scientific tools."""
import argparse
import csv
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uav_pipeline.config import load_config
from uav_pipeline.preparation import (
    latest_candidate_selection,
    locations,
    validate_radiometric_priority,
    validate_stage,
    verify_outputs,
    verify_priority_outputs,
)


def ed_time_rows(audit, selected, unmatched):
    source = audit / ("image_ed_matching.csv" if selected is not None else "image_time_solar.csv")
    with source.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if selected is not None:
        status = {row["shot"]: row["match_status"] for row in rows if row["shot"] in selected}
        bad = {shot: status.get(shot, "missing") for shot in selected if status.get(shot) != "matched"}
        if bad:
            raise ValueError(f"ELC 후보 영상의 시간·Ed 미매칭: {bad}. 결과: {audit}")
        return [row for row in rows if row["shot"] in selected]
    if unmatched:
        raise ValueError(f"시간·Ed 미매칭을 먼저 확인하세요: {audit}")
    return [row for row in rows if row["status"] == "ok"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--stage", choices=("radiometric_priority", "radiometric", "ed", "elc_fit", "elc_apply_priority", "elc_apply", "elc_gui"), required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    from uav_pipeline.general_project import is_general, source_shots, source_signature
    if is_general(cfg) and args.stage in ("radiometric_priority", "radiometric", "ed", "elc_gui"):
        raise ValueError("General ELC uses native candidate selection and integrated_preflight/integrated; legacy GPS-only workers are not used.")
    elc_only = bool(cfg.get("preparation", {}).get("elc_only"))
    if elc_only and args.stage in ("radiometric", "elc_apply"):
        raise ValueError("ELC 영상만 처리 모드에서는 전체 영상 단계를 실행할 수 없습니다.")
    if args.stage == 'elc_gui':
        from uav_pipeline.tarp_workflow import launch
        print(launch(cfg),flush=True)
        return
    if args.stage == "elc_fit":
        from uav_pipeline.elc_fit import fit
        from uav_pipeline.config import save_config
        result = fit(cfg)
        cfg["preparation"]["coefficient_csv"] = result["coefficient_csv"]
        cfg["preparation"]["coefficient_basis_confirmed"] = False
        save_config(Path(args.config), cfg)
        print("ELC_FIT_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
        print("COMPLETE: ELC a·b 산출·검증", flush=True)
        return
    stage = "elc_apply" if args.stage == "elc_apply_priority" else args.stage
    payload = validate_radiometric_priority(cfg) if args.stage == "radiometric_priority" else validate_stage(cfg, stage)
    if stage == "elc_apply":
        from uav_pipeline.elc_priority import approved_shots, checked_coefficients, suffix_band_mapping
        mapping = suffix_band_mapping(cfg)
        from uav_pipeline.band_scope import bands
        payload["output_bands"]=bands(cfg)
        payload["suffix_to_band"] = mapping
        checked_coefficients(cfg, approved_shots(cfg)[2])
        if is_general(cfg):
            payload.update(general=dict(cfg["general"]), band_names=dict(cfg["band_names"]),
                           selected_shots=approved_shots(cfg)[3] if args.stage == "elc_apply_priority" else sorted(source_shots(cfg)),
                           **source_signature(cfg))
        if args.stage == "elc_apply":
            from uav_pipeline.elc_priority import reuse_cutoff
            payload["reuse_after_ns"] = reuse_cutoff(cfg, mapping)
    if args.stage == "elc_apply_priority":
        from uav_pipeline.elc_priority import approved_shots, checked_coefficients, check_sources
        manifest_path, manifest, reference, shots = approved_shots(cfg)
        checked_coefficients(cfg, reference)
        check_sources(cfg, shots)
        payload["selected_shots"] = shots
        payload["overwrite"] = True
        payload["_studio"].update(stage=args.stage, roi_manifest=str(manifest_path), reference_shot=reference)
    folders = locations(cfg)
    free = shutil.disk_usage(folders["base"].parent).free
    print(f"Output disk free: {free / 1024**3:.2f} GiB", flush=True)
    if args.stage in ("radiometric_priority", "radiometric"):
        estimate = payload["_studio"]["selected_bytes"] * 2.5
        print(f"Conservative output estimate: {estimate / 1024**3:.2f} GiB", flush=True)
        if free < estimate:
            raise ValueError("방사보정 예상 출력 용량보다 여유 공간이 부족합니다.")
    run = folders["configs"] / "runs" / uuid.uuid4().hex
    run.mkdir(parents=True)
    labels = payload.pop("_gps_labels", None)
    if labels is not None:
        gps = run / "gps_all_bands.txt"
        gps.write_text("\n".join(labels) + "\n", encoding="utf-8")
        payload["gps_path"] = str(gps)
    snapshot = run / (args.stage + ".json")
    if args.stage == 'ed':
        from uav_pipeline.time_ed import export, write_csv, timestamp, KST
        audit, summary = export(cfg, with_ed=True)
        selected = latest_candidate_selection(cfg)[2] if elc_only else None
        time_rows = ed_time_rows(audit, selected, summary.get('unmatched'))
        for row in time_rows:
            row['capture_time_kst'] = timestamp(row['capture_time_kst'], 'Asia/Seoul').astimezone(KST).replace(tzinfo=None).isoformat(sep=' ')
        cache = run / 'verified_image_times.csv'
        write_csv(cache, time_rows)
        payload['time_mapping_csv'] = str(cache)
        payload['rebuild_time_mapping'] = False
        allowed = run / 'ed_allowed_shots.txt'
        allowed.write_text('\n'.join(row['shot'] + '_1.tif' for row in time_rows) + '\n', encoding='utf-8')
        payload['allowed_shots_file'] = str(allowed)
        payload['_studio']['time_ed_audit'] = str(audit)
        if selected is not None:
            payload['_studio']['selected_shots'] = sorted(selected)
    snapshot.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf8")
    tool_stage = "radiometric" if args.stage == "radiometric_priority" else stage
    script = cfg["preprocess"]["tools"][tool_stage]
    steps = ("scan", "metadata", "radiometric") if args.stage in ("radiometric_priority", "radiometric") else (None,)
    for step in steps:
        command = [sys.executable, script, "--config", str(snapshot)]
        if step:
            command += ["--stage", step]
        print(f"START {step or args.stage}", flush=True)
        subprocess.run(command, check=True)
    if args.stage == "radiometric_priority":
        _, _, shots = latest_candidate_selection(cfg)
        count = verify_priority_outputs(cfg, shots)
    else:
        count = verify_outputs(cfg, stage, allowed_shots=set(shots) if args.stage == "elc_apply_priority" else {row['shot'] for row in time_rows} if args.stage == 'ed' else None)
    if args.stage == "elc_apply_priority":
        from uav_pipeline.elc_priority import write_report, write_reuse_marker
        marker = write_reuse_marker(cfg, mapping, shots)
        report = write_report(cfg, manifest_path, manifest, reference, shots)
        print(f"VALIDATION_REPORT {report}", flush=True)
        print(f"OUTPUT_PROVENANCE {marker}", flush=True)
    elif args.stage == "elc_apply":
        from uav_pipeline.elc_priority import mark_full_complete
        mark_full_complete(cfg, mapping)
    print(f"COMPLETE: {count} nonempty output files (pixel QA separate)", flush=True)


if __name__ == "__main__":
    main()
