"""Explicit retained-shot inputs for non-campaign, native ten-band ELC projects."""
from __future__ import annotations

import csv
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

SHOT = re.compile(r"IMG_\d{4}", re.I)
RAW = re.compile(r"(IMG_\d{4})_(10|[1-9])\.tiff?", re.I)


def is_general(cfg):
    return cfg.get("general", {}).get("enabled") is True


def project_root(cfg):
    general = cfg["general"]
    root = Path(general["project_root"])
    if not root.is_absolute():
        raise ValueError("general.project_root must be absolute")
    day = general["date"]
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("general.date must be YYYY-MM-DD")
    for key in ("session", "site"):
        value = general[key]
        if (not isinstance(value, str) or not re.fullmatch(r"[\w-]{1,80}", value)
                or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value, re.I)):
            raise ValueError(f"Unsafe general.{key}")
    identity = cfg["dataset"]
    if (Path(identity["root"]).resolve() != root.resolve()
            or identity["date"] != day.replace("-", "_") + "_" + general["site"]
            or identity["flight"] != general["session"] or identity["basis"] != "Panel"):
        raise ValueError("General project and compatibility dataset identity differ")
    return root.resolve()


def source_list_path(cfg) -> Path:
    settings = cfg.get("preparation", {})
    key = "shot_list_csv" if is_general(cfg) else "gps_path"
    value = settings.get(key, "")
    if not value and not is_general(cfg):
        value = cfg.get("geometry", {}).get("gps_path", "")
    if not value:
        raise ValueError(f"Explicit source list required: {key}")
    path = Path(value)
    if is_general(cfg):
        project_root(cfg)
        if not path.is_absolute():
            raise ValueError("shot_list_csv must be absolute")
    return path.resolve()


def _shots(shots):
    values = [str(shot).strip().upper() for shot in shots]
    if not values or any(not SHOT.fullmatch(shot) for shot in values):
        raise ValueError("Nonempty explicit IMG_0000 shot list required")
    if len(values) != len(set(values)):
        raise ValueError("Duplicate shot in explicit target list")
    return set(values)


def source_shots(cfg) -> set:
    path = source_list_path(cfg)
    if not is_general(cfg):
        from .preparation import _gps_shots
        return _gps_shots(path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or reader.fieldnames.count("shot") != 1:
            raise ValueError("Target CSV requires one shot column")
        return _shots(row.get("shot", "") for row in reader)


def source_signature(cfg):
    """Bind exact manifest bytes and path, not just its currently parsed subset."""
    path = source_list_path(cfg)
    shots = source_shots(cfg)
    return {"source_list_path": str(path),
            "source_list_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "target_shots": sorted(shots)}


def scan_raw(raw, shots=None) -> dict[tuple[str, int], Path]:
    if not raw or not str(raw).strip():
        raise ValueError("Raw directory must be explicitly selected")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise ValueError("Raw directory does not exist")
    shots = _shots(shots) if shots is not None else None
    shots = _shots(shots) if shots is not None else None
    found = {}
    for path in sorted(root.rglob("*")):
        match = RAW.fullmatch(path.name)
        if not path.is_file() or not match:
            continue
        key = match[1].upper(), int(match[2])
        if shots is not None and key[0] not in shots:
            continue
        if shots is not None and key[0] not in shots:
            continue
        if key in found:
            raise ValueError(f"Duplicate shot/suffix (case-insensitive): {found[key]} / {path}")
        found[key] = path.resolve()
    if shots is None:
        return found
    missing = [(shot, band) for shot in sorted(shots) for band in range(1, 11)
               if (shot, band) not in found or found[shot, band].stat().st_size == 0]
    if missing:
        raise ValueError(f"Missing or zero-byte raw bands: {missing[:10]}")
    return found


def raw_for_shots(raw, shots):
    return scan_raw(raw, shots=shots)


def _fresh():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8]


def save_shot_list(cfg, shots) -> Path:
    from .preparation import locations
    if not is_general(cfg):
        raise ValueError("Explicit target CSV creation requires a general project")
    path = locations(cfg)["configs"] / "targets" / ("shots_" + _fresh() + ".csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["shot"])
        writer.writerows([shot] for shot in sorted(shots))
    cfg.setdefault("preparation", {})["shot_list_csv"] = str(path)
    return path


def save_candidates(cfg, shots, reference_shot="") -> Path:
    from .preparation import locations
    if not is_general(cfg):
        raise ValueError("Manual native candidates require a general project")
    shots = list(shots)
    shots = _shots(shots) if shots else set()
    targets = source_shots(cfg) if shots else set()
    if not shots <= targets:
        raise ValueError("Candidate shots must be a subset of the explicit target manifest")
    reference = str(reference_shot).strip().upper()
    if reference and reference not in shots:
        raise ValueError("Reference shot must be in the selected candidates")
    settings = cfg["preparation"]
    if shots:
        raw_for_shots(settings["raw_directory"], targets)
    paths = locations(cfg)
    identity = "general_" + _fresh()
    output = paths["roi"] / "workflow" / identity
    raw = str(Path(settings["raw_directory"]).resolve()) if settings.get("raw_directory") else ""
    context = {"identity": identity, "date": cfg["dataset"]["date"], "flight": cfg["dataset"]["flight"],
               "label": cfg["general"]["site"] + " " + cfg["general"]["session"], "output": str(output),
               "image_source": raw, "raw": raw, "radiance": str(paths["rad"]),
               "source_kind": "raw_DN_boundary_only", **(source_signature(cfg) if shots else {})}
    path = output / "selections" / ("selected_" + _fresh() + ".json")
    path.parent.mkdir(parents=True, exist_ok=False)
    (output / "context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    path.write_text(json.dumps({"selected": [{"shot": shot} for shot in sorted(shots)],
                                "reference_shot": reference, "selection_cleared": not bool(shots)}, indent=2), encoding="utf-8")
    if reference:
        settings["reference_shot"] = reference
    if not shots:
        settings["reference_shot"] = ""
        settings["coefficient_basis_confirmed"] = False
        settings.pop("approved_roi_manifest", None)
    return path
