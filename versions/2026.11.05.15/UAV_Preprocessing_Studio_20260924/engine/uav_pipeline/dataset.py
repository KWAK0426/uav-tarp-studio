"""Read-only discovery for the user's date/flight dataset layout."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

from .config import DEFAULTS

DEFAULT_ROOT = os.environ.get("ELC_STUDIO_WORK", str(Path(__file__).resolve().parents[3] / "Work"))
FLIGHTS = ("1st", "2nd", "3rd", "4th", "5th", "6th")
ROLES = {
    "raw": "다분광 원영상", "ed_source": "RAMSES Ed 원자료",
    "photos": "분석용 정사 Photos", "tarp_photos": "타프 후보용 Panel Photos",
    "mosaic": "정사 모자이크", "dem": "DEM",
    "gps": "GPS.txt", "radiance": "방사보정 영상", "roi": "타프 ROI 자료",
    "time_ed": "시간·Ed 매칭", "ed_radiance": "Ed·센서 교정 영상",
    "coefficients": "ELC 계수 자료", "reflectance": "ELC 반사도 영상",
    "output": "분석 결과 저장", "mask": "공통 필지 마스크",
}


def selection_file() -> Path:
    return Path(os.environ.get("ELC_STUDIO_STATE", str(Path.home() / "ELCStudioShare"))) / "dataset_selection.json"


def _inside(root: Path, path: Path) -> Path:
    path.resolve().relative_to(root.resolve())
    return path


def list_dates(root: str | Path) -> list[str]:
    root = Path(root)
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and re.match(r"^2026_\d{2}_\d{2}_", p.name)
                  and p.resolve().is_relative_to(root.resolve()))


def _files(root: Path, path: Path):
    if not path.is_dir():
        return
    _inside(root, path)
    for base, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()
                   and (Path(base) / d).resolve().is_relative_to(root.resolve())]
        for name in names:
            p = Path(base) / name
            if p.is_file() and p.resolve().is_relative_to(root.resolve()) and p.stat().st_size > 0:
                yield p


def _has(root: Path, path: Path, suffixes=()) -> bool:
    return any(not suffixes or p.suffix.lower() in suffixes for p in _files(root, path))


def discover(root: str | Path, date: str, flight: str, basis: str) -> dict:
    root = Path(root).resolve()
    if date not in list_dates(root) or flight not in FLIGHTS or basis not in ("Panel", "ELC"):
        raise ValueError("날짜·회차·처리 기준을 확인하세요.")
    ymd = date[:10].replace("_", "")[2:]
    prefix = f"{flight}_{ymd}"
    day = root / date
    cycle = _inside(root, day / "02_Processed" / prefix)
    if not cycle.is_dir():
        raise ValueError(f"회차 폴더가 없습니다: {cycle}")
    process = cycle / f"{prefix}_00_ELC_Process"
    panel = cycle / f"{prefix}_01_Panel_Meta"
    meta = panel if basis == "Panel" else cycle / f"{prefix}_02_ELC_Meta"
    rows, paths, warnings = [], {}, []

    def add(role, candidates=(), target=None):
        candidates = sorted(set(str(_inside(root, Path(p))) for p in candidates))
        status = "있음" if len(candidates) == 1 else "선택 필요" if candidates else "없음"
        if role == "output":
            status = "저장 예정"
            paths[role] = str(target)
        else:
            paths[role] = candidates[0] if len(candidates) == 1 else ""
        rows.append({"role": role, "label": ROLES[role], "status": status,
                     "path": paths[role], "candidates": candidates,
                     "expected": str(target) if target else ""})

    raw_base = day / "01_Raw_data" / f"{ymd}_01_Multispectral"
    n = flight[0]
    raw = []
    if raw_base.is_dir():
        for p in raw_base.iterdir():
            if "excluded_not_in_gps" in p.name.lower():
                continue
            if p.is_dir() and re.match(rf"^{n}(?:\.|회차|st_|nd_|rd_|th_)", p.name):
                if _has(root, p, (".tif", ".tiff")):
                    raw.append(p)
    add("raw", raw, raw_base)
    ed = day / "01_Raw_data" / f"{ymd}_03_RAMSES_Ed"
    add("ed_source", [ed] if _has(root, ed) else [], ed)
    # Only named Photos containers: do not reinterpret radiance or panel_Ref.
    panel_files = list(_files(root, panel))
    mf = panel_files if meta == panel else list(_files(root, meta))
    photos = {p.parent for p in mf if p.suffix.lower() in (".tif", ".tiff")
              and re.search(r"photos$", p.parent.name, re.I)}
    add("photos", photos, meta)
    if basis == "ELC":
        panel_photos = {p.parent for p in panel_files if p.suffix.lower() in (".tif", ".tiff")
                        and re.search(r"photos$", p.parent.name, re.I)}
        add("tarp_photos", panel_photos, panel)
    add("mosaic", [p for p in mf if p.suffix.lower() in (".tif", ".tiff")
                   and re.search(r"mosaic", p.stem, re.I)], meta)
    # DEM/GPS are shared within this flight, never borrowed from a sibling.
    shared = [p for p in cycle.iterdir() if p.is_file() and p.stat().st_size > 0]
    shared += panel_files
    if meta != panel:
        shared += mf
    add("dem", [p for p in shared if p.suffix.lower() in (".tif", ".tiff")
                and re.search(r"(?:^|_)DEM(?:_|$)", p.stem, re.I)])
    gps = []
    for p in shared:
        if p.suffix.lower() not in (".txt", ".csv") or not re.search(r"GPS", p.stem, re.I):
            continue
        dates_in_name = re.findall(r"(?<!\d)(\d{8}|\d{6})(?!\d)", p.stem)
        if dates_in_name and any(d[-6:] != ymd for d in dates_in_name):
            warnings.append(f"GPS 날짜 표기 불일치: {p.name} (자동 연결 제외)")
        else:
            gps.append(p)
    add("gps", gps)
    leaves = {"radiance": "01_Radiance", "roi": "02_Tarp_ROI", "time_ed": "03_Time_Ed",
              "ed_radiance": "04_Ed_Sensor_Rad", "coefficients": "05_ELC_AB", "reflectance": "06_ELC_Ref"}
    for role, leaf in leaves.items():
        p = process / f"{prefix}_{leaf}"
        suffixes = (".tif", ".tiff") if role in ("radiance", "ed_radiance", "reflectance") else ()
        add(role, [p] if _has(root, p, suffixes) else [], p)
    masks = [p for p in _files(root, root / "00_common_Masking") if p.suffix.lower() == ".geojson"]
    # A sole mask is still not proof of matching geography: require manual choice.
    add("mask", [], root / "00_common_Masking")
    if masks:
        warnings.append("공통 GeoJSON 마스크가 있습니다. 공간 범위 확인 후 필지 탭에서 지정하세요.")
    out = cycle / f"{prefix}_03_Analysis" / basis
    add("output", target=out)
    config = cycle / f"{prefix}_90_App" / basis / "studio.json"
    warnings.append("파일 유무만 확인했습니다. 반사도 배율·GPS 좌표계·촬영 시간대를 확인하세요.")
    if not paths["raw"]:
        warnings.append("촬영시각 원영상 자동 연결 불가: 수동 지정이 필요합니다.")
    if meta.is_dir() and any(p.name == "01_panel_Ref" for p in meta.iterdir()):
        warnings.append("01_panel_Ref는 Photos 여부 미확정이므로 자동 연결하지 않았습니다.")
    return {"root": str(root), "date": date, "flight": flight, "basis": basis,
            "prefix": prefix, "paths": paths, "rows": rows, "warnings": warnings,
            "config_path": str(config)}


def config_from_discovery(found: dict) -> dict:
    # Never carry another flight's paths, coefficients, gains, or masks forward.
    cfg = copy.deepcopy(DEFAULTS)
    p = found["paths"]
    cfg["project_name"] = found["prefix"] + "_" + found["basis"]
    cfg["dataset"] = {k: copy.deepcopy(found[k]) for k in ("root", "date", "flight", "basis", "paths", "warnings")}
    cfg["dataset"]["expected_directories"] = {r["role"]: r["expected"] for r in found["rows"] if r["expected"]}
    cfg["dataset"]["input_settings_reviewed"] = False
    cfg["photos"]["directory"] = p["photos"]
    cfg["geometry"].update(dem_path=p["dem"], gps_path=p["gps"], frame_time_source_directory=p["raw"])
    cfg["output"]["directory"] = p["output"]
    cfg["viewer"].update(cycle_id=found["flight"], cycle_label=found["flight"][0] + "회차")
    return cfg
