"""Common preparation schema; generated files belong to one date/flight."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from pathlib import Path

from .dataset import FLIGHTS
from .band_scope import bands
from .general_project import is_general, project_root, source_shots, source_signature, raw_for_shots

SENSOR = "RedEdge-MX Dual (10 bands)"
DEFAULT_GAIN_CSV = Path(__file__).resolve().parents[1] / "defaults" / "mx_dual_sensor_intercalibration_gain.csv"
CHOICES = {"sensor": SENSOR, "ed_csv": "", "reference_shot": "", "source_timezone": "UTC",
           "gain_csv": "", "coefficient_csv": "", "coefficient_basis_confirmed": False,
           "tarp_reflectance_csv": str(Path(__file__).resolve().parents[1] / "defaults" / "tarp_reflectance_library.csv"),
           "worker_count": 1, "max_ed_delta_seconds": 5, "time_band": 1, "elc_only": True,
           "integrated_save_radiance": False, "integrated_output_root": "", "integrated_plan_file": ""}
STAGES = ("radiometric", "ed", "elc_apply")


def initialize(cfg):
    if not cfg.get("dataset"):
        return
    from .calibration_presets import resolve
    resolve(cfg)
    settings = cfg.setdefault("preparation", {})
    for key, value in CHOICES.items():
        settings.setdefault(key, value)
    if settings.get("max_ed_delta_seconds") in (None, "", 10, 120, "10", "120"):
        settings["max_ed_delta_seconds"] = 5
    if not settings.get("sensor"):
        settings["sensor"] = SENSOR
    if not settings.get("source_timezone"):
        settings["source_timezone"] = "UTC"
    if not settings.get("gain_csv"):
        settings["gain_csv"] = str(DEFAULT_GAIN_CSV)
    cfg.setdefault("preprocess", {})["allow_nonunity_sensor_gains"] = True
    settings.setdefault("raw_directory", cfg["dataset"].get("paths", {}).get("raw", ""))
    settings.setdefault("gps_path", cfg.get("geometry", {}).get("gps_path", ""))
    if is_general(cfg):
        settings.setdefault("shot_list_csv", "")


def locations(cfg):
    d = cfg["dataset"]
    if is_general(cfg):
        root = project_root(cfg)
        cycle = root
        prefix = cfg["general"]["session"] + "_" + cfg["general"]["date"].replace("-", "")
    else:
        if d["flight"] not in FLIGHTS or not re.match(r"^2026_\d\d_\d\d_[^/\\]+$", d["date"]):
            raise ValueError("잘못된 날짜·회차 설정")
        prefix = d["flight"] + "_" + d["date"][:10].replace("_", "")[2:]
        root = Path(d["root"]).resolve()
        cycle = root / d["date"] / "02_Processed" / prefix
        cycle.resolve().relative_to(root)
        if not cycle.is_dir():
            raise ValueError("회차 폴더가 없거나 드라이브가 연결되지 않았습니다.")
    base = cycle / (prefix + "_00_ELC_Process")
    result = {"base": base, "configs": cycle / (prefix + "_90_App") / "Preparation",
              "qc": base / (prefix + "_00_QC"), "rad": base / (prefix + "_01_Radiance"),
              "ed": base / (prefix + "_04_Ed_Sensor_Rad"), "coef": base / (prefix + "_05_ELC_AB"), "ref": base / (prefix + "_06_ELC_Ref"),
              "roi": base / (prefix + "_02_Tarp_ROI")}
    easy = cfg.get("preparation", {}).get("easy_output_directory")
    if easy:
        isolated = Path(easy).resolve()
        if isolated.parent != cycle.resolve() or not isolated.name.startswith("Easy_ELC_"):
            raise ValueError("간편 ELC 결과는 현재 회차의 별도 Easy_ELC 실행 폴더여야 합니다.")
        for key, name in (("base", ""), ("configs", "Settings"), ("qc", "QC"), ("rad", "Radiance"),
                          ("ed", "Ed_Normalized"), ("coef", "Coefficients"), ("ref", "Reflectance")):
            result[key] = isolated / name
    for p in result.values():
        p.resolve().relative_to(root)
    if cfg.get('preparation',{}).get('integrated_plan_file'):
        from .integrated import linked_plan
        result['ed']=Path(linked_plan(cfg)['run'])/'Ed_Normalized'
    return result


def _file(value):
    return bool(value and Path(value).is_file() and Path(value).stat().st_size > 0)


def _gains(settings):
    values = {str(i): 1.0 for i in range(1, 11)}
    if not settings.get("gain_csv"):
        return values
    with Path(settings["gain_csv"]).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    values = {str(int(r["band"])): float(r["gain"]) for r in rows}
    if len(rows) != 10 or set(values) != {str(i) for i in range(1, 11)} or any(not math.isfinite(v) or v <= 0 for v in values.values()):
        raise ValueError("센서 계수 CSV는 band,gain 열과 1~10번 양의 계수가 필요합니다.")
    return values


def build(cfg):
    initialize(cfg)
    s, d, p = cfg["preparation"], cfg["dataset"], locations(cfg)
    elc_only = bool(s["elc_only"])
    qc = p["qc"].with_name(p["qc"].name + "_ELC_Priority") if elc_only else p["qc"]
    issues = {stage: [] for stage in STAGES}
    if s["sensor"] != SENSOR:
        for errors in issues.values():
            errors.append("센서를 확인하세요. 현재 자동 설정은 MX Dual 10밴드용입니다.")
    if not s["raw_directory"] or not Path(s["raw_directory"]).is_dir():
        issues["radiometric"].append("해당 회차 원영상 폴더 필요")
    try:
        source_shots(cfg)
    except (OSError, ValueError) as exc:
        issues["radiometric"].append(str(exc))
    gain_values = {str(i): 1.0 for i in range(1, 11)}
    try:
        gain_values = _gains(s)
    except (OSError, KeyError, ValueError) as exc:
        issues["radiometric"].append(str(exc))
    if s["source_timezone"] not in ("UTC", "Asia/Seoul"):
        issues["ed"].append("원영상 촬영시각의 시간대 확인 필요")
    if not re.fullmatch(r"IMG_\d{4}", s["reference_shot"]):
        issues["ed"].append("ELC 기준 영상 선택 필요 (예: IMG_0650)")
    if not _file(s["ed_csv"]):
        issues["ed"].append("밴드별 Ed CSV 필요 (RAMSES 원자료와 구분)")
    if not _file(str(qc / "frame_groups.csv")):
        issues["ed"].append("ELC 후보 방사보정 목록 생성 필요" if elc_only else "방사보정의 영상 목록 생성 필요")
    if not p["rad"].is_dir():
        issues["ed"].append("방사보정 영상 생성 필요")
    if not _file(s["coefficient_csv"]):
        issues["elc_apply"].append("밴드별 a·b CSV 필요")
    if not s["coefficient_basis_confirmed"]:
        issues["elc_apply"].append("a·b 산출과 적용 영상의 Ed·센서 보정 기준 일치 확인 필요")
    if not p["ed"].is_dir():
        issues["elc_apply"].append("Ed·센서 교정 영상 생성 필요")
    common = {"overwrite": False, "compression": "DEFLATE", "worker_count": max(1, min(8, int(s["worker_count"])))}
    rad = dict(common, raw_root=s["raw_directory"], gps_path="" if is_general(cfg) else str(p["configs"] / "gps_all_bands.txt"),
               output_root=str(p["base"]), qc_dir_name=p["qc"].name, radiometric_dir_name=p["rad"].name,
               aligned_dir_name=p["roi"].name, numeric_folders_only=not is_general(cfg), output_dtype="float32",
               apply_sensor_correction=True, sensor_gains=gain_values)
    ed = dict(common, cycle=d["flight"], ed_list_kind="", input_root=str(p["rad"]), output_root=str(p["ed"]),
              frame_groups_csv=str(qc / "frame_groups.csv"), allowed_shots_file=None,
              ed_csv=s["ed_csv"], time_mapping_csv=str(p["ed"] / "_image_time_mapping.csv"),
              time_source="exif", rebuild_time_mapping=True, reference_shot=s["reference_shot"],
              image_time_offset_hours=9 if s["source_timezone"] == "UTC" else 0,
              max_ed_time_delta_seconds=float(s["max_ed_delta_seconds"]),
              sensor_gains={str(i): 1.0 for i in range(1, 11)},
              sensor_correction_already_applied=True)
    elc = dict(common, apply_radiance_dir=str(p["ed"]), output_root=str(p["base"]),
               reflectance_dir_name=p["ref"].name, coef_csv=s["coefficient_csv"], recursive=True,
               coefficient_convention="L=a*rho+b")
    configs = dict(radiometric=rad, ed=ed, elc_apply=elc)
    for stage, payload in configs.items():
        payload["_studio"] = {"schema": 1, "date": d["date"], "flight": d["flight"], "stage": stage,
                              "source_gps": s["gps_path"], "blockers": issues[stage], "generated": True}
        if is_general(cfg):
            payload["_studio"]["source_list"] = s["shot_list_csv"]
    if is_general(cfg):
        rad["shot_list_csv"] = s["shot_list_csv"]
    return configs, issues, p


def generate(cfg):
    configs, issues, p = build(cfg)
    p["configs"].mkdir(parents=True, exist_ok=True)
    for stage, payload in configs.items():
        path = p["configs"] / (stage + ".json")
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8-sig"))
            if not old.get("_studio", {}).get("generated"):
                raise ValueError(f"사용자가 작성한 설정은 덮어쓰지 않습니다: {path}")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg["preprocess"][stage + "_config"] = str(path)
    return issues


def _gps_shots(value):
    shots = set()
    for line in Path(value).read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("#"):
            continue
        m = re.search(r"\b(IMG_\d{4})(?:_(?:10|[1-9]))?(?:\.tiff?)?\b", line, re.I)
        if m:
            shots.add(m[1].upper())
    if not shots:
        raise ValueError("GPS.txt에 유효한 IMG 촬영번호가 없습니다.")
    return shots


def validate_stage(cfg, stage, selected_shots=None):
    if is_general(cfg) and stage == "elc_apply" and not cfg['preparation'].get('elc_only', True):
        from .general_tarp_review import require_full_approval
        require_full_approval(cfg)
    configs, issues, p = build(cfg)
    if is_general(cfg) and stage in ("radiometric", "ed"):
        targets = source_shots(cfg)
        raw_for_shots(cfg["preparation"]["raw_directory"], targets)
        if selected_shots is not None and (not selected_shots or not set(selected_shots) <= targets):
            raise ValueError("Candidates must be a subset of the explicit target manifest")
        raise ValueError("General projects require integrated_preflight / integrated (or Easy ELC); standalone GPS adapters are not supported")
    if issues[stage]:
        raise ValueError("\n".join(issues[stage]))
    s, payload = cfg["preparation"], configs[stage]
    if stage == "radiometric":
        raw = Path(s["raw_directory"])
        roots = {f.parent.parent for f in raw.rglob("*") if f.is_file() and f.suffix.lower() in (".tif", ".tiff") and f.parent.name.isdigit()}
        if len(roots) != 1:
            raise ValueError("원영상의 숫자 폴더를 포함하는 SET를 하나 지정하세요. 여러 SET는 자동 합치지 않습니다.")
        actual = roots.pop()
        shots = source_shots(cfg)
        if selected_shots is not None:
            if not selected_shots or not set(selected_shots) <= shots:
                raise ValueError("ELC 후보 촬영번호가 현재 GPS 목록에 없습니다.")
            shots = set(selected_shots)
        found = {}
        for folder in actual.iterdir():
            if not folder.is_dir() or not folder.name.isdigit():
                continue
            for f in folder.iterdir():
                m = re.fullmatch(r"(IMG_\d{4})_(10|[1-9])\.tiff?", f.name, re.I)
                if m and m[1].upper() in shots:
                    key = (m[1].upper(), int(m[2]))
                    if key in found or f.stat().st_size == 0:
                        raise ValueError(f"중복 또는 0바이트 원영상: {f}")
                    found[key] = f
        missing = [(shot, b) for shot in sorted(shots) for b in range(1, 11) if (shot, b) not in found]
        if missing:
            raise ValueError(f"GPS 기준 원영상 {len(missing)}개 누락. 예: {missing[:5]}")
        payload["raw_root"] = str(actual)
        payload["_studio"]["selected_files"] = len(found)
        payload["_studio"]["selected_bytes"] = sum(f.stat().st_size for f in found.values())
        payload["_gps_labels"] = [found[k].name for k in sorted(found)]
    elif stage == "ed":
        if s["elc_only"]:
            _, _, selected = latest_candidate_selection(cfg)
            if s["reference_shot"] not in selected:
                raise ValueError("ELC 기준 영상이 저장된 후보 목록에 없습니다.")
        with Path(s["ed_csv"]).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        date = cfg["dataset"]["date"][:10].replace("_", "-")
        selected = [r for r in rows if r.get("cycle") == payload["cycle"] and r.get("datetime_kst", "").startswith(date)]
        kinds = {r.get("list_kind") for r in selected}
        if len(kinds) != 1 or not next(iter(kinds), None):
            raise ValueError("Ed CSV에 해당 날짜·회차의 단일 list_kind가 필요합니다. 중복 구분은 먼저 정리하세요.")
        for row in selected:
            for band, name in cfg["band_names"].items():
                value = float(row[f"Band{band}_{name}"])
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("Ed에는 1~10번 밴드의 양의 유효값이 필요합니다.")
        payload["ed_list_kind"] = next(iter(kinds))
        if any(r.get("cycle") == payload["cycle"] and r.get("list_kind") == payload["ed_list_kind"] and not r.get("datetime_kst", "").startswith(date) for r in rows):
            raise ValueError("Ed CSV에 다른 날짜의 같은 회차가 섞여 있습니다. 날짜별 CSV를 지정하세요.")
        with Path(payload["frame_groups_csv"]).open(encoding="utf-8-sig", newline="") as stream:
            frames = list(csv.DictReader(stream))
        if not frames or payload["reference_shot"] not in {r["shot"] for r in frames}:
            raise ValueError("ELC 기준 영상이 해당 회차 방사보정 목록에 없습니다.")
        for row in frames:
            rad = Path(row["radiometric_path"])
            if not rad.resolve().is_relative_to(p["rad"].resolve()) or not _file(str(rad)):
                raise ValueError("방사보정 목록의 경로 또는 출력 파일을 확인하세요.")
        checked_frames(cfg)
    else:
        if s.get("approved_roi_manifest"):
            from .elc_priority import checked_coefficients
            checked_coefficients(cfg, s["reference_shot"])
        with Path(s["coefficient_csv"]).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) != len(bands(cfg)) or {int(r["band"]) for r in rows} != set(bands(cfg)) or any(not math.isfinite(float(r[k])) for r in rows for k in ("a", "b")):
            raise ValueError("계수 CSV와 산출 밴드 목록이 다릅니다.")
        if any(r.get("coefficient_convention", "").strip() != "L=a*rho+b" or float(r["a"]) <= 0 for r in rows):
            raise ValueError("ELC 계수는 L=a*rho+b 기준입니다. CSV의 coefficient_convention 열과 양의 a를 확인하세요. 기존 계수는 방향 확인 또는 재산출이 필요합니다.")
        verify_outputs(cfg, "ed", reject_extra=not s["elc_only"])
        payload["_studio"]["coefficient_sha256"] = hashlib.sha256(Path(s["coefficient_csv"]).read_bytes()).hexdigest()
    return payload


def latest_candidate_selection(cfg):
    """Return the latest saved candidate selection for this date and flight."""
    p = locations(cfg)
    wanted = cfg["dataset"]
    choices = []
    for path in (p["roi"] / "workflow").glob("*/selections/selected_*.json"):
        try:
            context_path = path.parent.parent / "context.json"
            context = json.loads(context_path.read_text(encoding="utf-8-sig"))
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if context.get("date") != wanted["date"] or context.get("flight") != wanted["flight"]:
                continue
            shots = {row.get("shot", "").upper() for row in data.get("selected", [])}
            if shots or (is_general(cfg) and data.get("selection_cleared") is True):
                choices.append((path.stat().st_mtime_ns, path, data, shots))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not choices:
        raise ValueError("저장된 ELC 후보 영상이 없습니다. 02에서 DN 모드로 후보를 선택·저장하세요.")
    _, path, data, shots = max(choices, key=lambda item: item[0])
    if not shots:
        raise ValueError("후보 선택이 해제되었습니다. 02에서 다시 선택·저장하세요.")
    allowed = source_shots(cfg)
    outside = sorted(shots - allowed)
    if outside:
        raise ValueError(f"최신 후보에 현재 GPS 목록 밖의 촬영번호가 있습니다: {outside[:5]}")
    if is_general(cfg):
        context = json.loads((path.parent.parent / "context.json").read_text(encoding="utf-8-sig"))
        if any(context.get(key) != value for key, value in source_signature(cfg).items()):
            raise ValueError("Target manifest changed since candidate/ROI selection; select and approve ROI again")
        if (context.get("source_kind") != "raw_DN_boundary_only"
                or Path(context["image_source"]).resolve() != Path(cfg["preparation"]["raw_directory"]).resolve()):
            raise ValueError("Candidate raw source changed; select and approve ROI again")
        reference = data.get("reference_shot", "")
        if reference and reference not in shots:
            raise ValueError("Reference shot is not in the selected candidates")
    if is_general(cfg):
        from .general_tarp_review import require_location
        require_location(cfg, path)
    return path, data, shots


def validate_radiometric_priority(cfg):
    """Build a radiometric payload restricted to user-selected ELC shots."""
    selection_path, _, shots = latest_candidate_selection(cfg)
    payload = validate_stage(cfg, "radiometric", selected_shots=shots)
    labels = payload["_gps_labels"]
    selected = [label for label in labels if label.rsplit("_", 1)[0].upper() in shots]
    if len(selected) != len(shots) * 10:
        raise ValueError("ELC 후보 영상의 10개 밴드 원영상을 모두 찾지 못했습니다.")
    p = locations(cfg)
    payload["qc_dir_name"] = p["qc"].name + "_ELC_Priority"
    payload["_gps_labels"] = selected
    payload["_studio"].update(
        stage="radiometric_priority",
        selected_files=len(selected),
        selected_bytes=sum(
            f.stat().st_size
            for f in Path(payload["raw_root"]).rglob("*")
            if f.is_file() and f.name in set(selected)
        ),
        candidate_selection=str(selection_path),
        candidate_shots=sorted(shots),
    )
    return payload


def verify_priority_outputs(cfg, shots):
    target = locations(cfg)["rad"]
    found = {
        (m.group(1).upper(), int(m.group(2)))
        for f in target.rglob("*") if f.is_file() and f.stat().st_size > 0
        for m in [re.fullmatch(r"(IMG_\d{4})_(10|[1-9])\.tiff?", f.name, re.I)] if m
    }
    missing = [(shot, band) for shot in sorted(shots) for band in range(1, 11) if (shot, band) not in found]
    if missing:
        raise ValueError(f"ELC 우선 방사보정 출력 {len(missing)}개 누락. 예: {missing[:5]}")
    return len(shots) * 10


def checked_frames(cfg):
    p = locations(cfg)
    elc_only = bool(cfg["preparation"].get("elc_only"))
    shots = latest_candidate_selection(cfg)[2] if elc_only else source_shots(cfg)
    qc = p["qc"].with_name(p["qc"].name + "_ELC_Priority") if elc_only else p["qc"]
    if cfg['preparation'].get('integrated_plan_file'):
        from .integrated import linked_plan
        plan=linked_plan(cfg)
        rows=[{'shot':j['shot'],'band':int(Path(j['src']).stem.rsplit('_',1)[1]),
               'radiometric_path':str(p['rad']/Path(j['dst']).relative_to(Path(plan['run'])/'Ed_Normalized'))} for j in plan['jobs']]
    else:
        with (qc / "frame_groups.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    keys = [(r["shot"], int(r["band"])) for r in rows]
    if len(keys) != len(set(keys)) or set(keys) != {(s, b) for s in shots for b in range(1, 11)}:
        raise ValueError("방사보정 목록이 선택한 촬영번호·10개 밴드와 일치하지 않습니다. 방사보정 목록부터 다시 확인하세요.")
    for row in rows:
        Path(row["radiometric_path"]).resolve().relative_to(p["rad"].resolve())
    return rows


def verify_outputs(cfg, stage, reject_extra=False, allowed_shots=None):
    p = locations(cfg)
    target = p[{"radiometric": "rad", "ed": "ed", "elc_apply": "ref"}[stage]]
    rows = checked_frames(cfg)
    if allowed_shots is not None:
        rows = [row for row in rows if row["shot"] in allowed_shots]
    if stage=="elc_apply" and len(bands(cfg))!=10:
        from .elc_priority import suffix_band_mapping
        mapping=suffix_band_mapping(cfg)
        rows=[r for r in rows if mapping[int(r['band'])] in bands(cfg)]
    expected = {target / Path(r["radiometric_path"]).resolve().relative_to(p["rad"].resolve()) for r in rows}
    if stage == "elc_apply":
        flat = {target / path.name for path in expected}
        if len(flat) != len(rows):
            raise ValueError("Duplicate reflectance output names")
        # New runs are flat; completed legacy runs remain verifiable.
        if all(_file(str(f)) for f in flat) or not all(_file(str(f)) for f in expected):
            expected = flat
    missing = [str(f) for f in expected if not _file(str(f))]
    if missing:
        raise ValueError(f"{stage}: 출력 {len(missing)}개 누락 또는 0바이트. 예: {missing[:3]}")
    if reject_extra:
        actual = {f for f in target.rglob("*") if f.is_file() and f.suffix.lower() in (".tif", ".tiff")}
        if actual != expected:
            raise ValueError("Ed 출력 폴더에 현재 GPS 목록 밖의 영상이 있습니다. 자동 적용을 중단합니다.")
    return len(expected)
