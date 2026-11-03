"""Convert date-local RAMSES irradiance spectra to the app's 10-band Ed CSV."""
from __future__ import annotations

import csv
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .preparation import locations


def _sources(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.dat")
                  if any(part.lower().startswith("irr") for part in p.relative_to(directory).parts[:-1]))


def _signature(paths: list[Path], root: Path) -> list[list[object]]:
    return [[str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns] for path in paths]


def _read(path: Path, bands: dict[str, str]) -> dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    unit = re.search(r"^Unit2\s*=\s*(.+)$", text, re.MULTILINE)
    if not unit or "mW/(m^2 nm)" not in unit.group(1) or "sr" in unit.group(1).lower():
        raise ValueError(f"RAMSES irradiance 단위를 확인하세요: {path.name}")
    match = re.search(r"^DateTime\s*=\s*(.+)$", text, re.MULTILINE)
    if not match:
        raise ValueError(f"RAMSES 측정시각이 없습니다: {path.name}")
    measured = datetime.strptime(match.group(1).strip(), "%Y-%m-%d %H:%M:%S")
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip().upper() == "[DATA]") + 1
    except StopIteration as exc:
        raise ValueError(f"RAMSES 스펙트럼 구간이 없습니다: {path.name}") from exc
    spectrum = []
    for line in lines[start:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            wavelength, value = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if math.isfinite(wavelength) and math.isfinite(value):
            spectrum.append((wavelength, value))
    if not spectrum:
        raise ValueError(f"유효한 RAMSES irradiance 값이 없습니다: {path.name}")
    row: dict[str, object] = {
        "datetime_kst": measured.isoformat(sep=" "),
        "source_file": str(path.resolve()),
        "unit": "mW/(m^2 nm)",
    }
    for band, name in bands.items():
        center = float(re.search(r"(\d+)nm$", name).group(1))
        wavelength, value = min(spectrum, key=lambda item: abs(item[0] - center))
        if abs(wavelength - center) > 2.5:
            raise ValueError(f"Band{band} {center:g} nm 인접 RAMSES 파장을 찾지 못했습니다: {path.name}")
        row[f"Band{band}_{name}"] = value
        row[f"Band{band}_nearest_wavelength_nm"] = wavelength
    return row


def managed_path(cfg: dict) -> Path:
    prefix = cfg["dataset"]["flight"] + "_" + cfg["dataset"]["date"][:10].replace("_", "")[2:]
    return locations(cfg)["base"] / (prefix + "_03_Time_Ed") / (prefix + "_RAMSES_Ed_10band.csv")


def connect(cfg: dict) -> str:
    """Create/reuse a flight-scoped 10-band CSV and assign preparation.ed_csv."""
    source_value = cfg.get("dataset", {}).get("paths", {}).get("ed_source", "")
    if not source_value:
        return ""
    source = Path(source_value)
    if not source.is_dir():
        raise ValueError("연결된 RAMSES Ed 원자료 폴더가 없습니다.")
    files = _sources(source)
    if not files:
        raise ValueError("RAMSES Ed 원자료에서 .dat 파일을 찾지 못했습니다.")
    output = managed_path(cfg)
    metadata = output.with_suffix(".meta.json")
    signature = _signature(files, source)
    expected = {
        "schema": 1,
        "date": cfg["dataset"]["date"],
        "flight": cfg["dataset"]["flight"],
        "source_root": str(source.resolve()),
        "source_signature": signature,
        "method": "nearest_measured_wavelength_no_srf",
        "timezone": "Asia/Seoul",
    }
    if output.is_file() and output.stat().st_size > 0 and metadata.is_file():
        try:
            if json.loads(metadata.read_text(encoding="utf-8")) == expected:
                cfg.setdefault("preparation", {})["ed_csv"] = str(output)
                return str(output)
        except (OSError, ValueError):
            pass
    date = cfg["dataset"]["date"][:10].replace("_", "-")
    rows = []
    with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
        parsed = pool.map(lambda path: _read(path, cfg["band_names"]), files)
        for row in parsed:
            if str(row["datetime_kst"]).startswith(date):
                rows.append({
                    "cycle": cfg["dataset"]["flight"],
                    "list_kind": "RAMSES_irradiance_measured",
                    **row,
                })
    rows.sort(key=lambda row: (str(row["datetime_kst"]), str(row["source_file"])))
    if not rows:
        raise ValueError(f"RAMSES 원자료에 선택 날짜({date})의 Ed 측정값이 없습니다.")
    times = [row["datetime_kst"] for row in rows]
    if len(times) != len(set(times)):
        raise ValueError("RAMSES 원자료에 같은 측정시각이 중복됩니다. 자동 연결을 중단합니다.")
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0])
    temporary = output.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output)
    metadata.write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg.setdefault("preparation", {})["ed_csv"] = str(output)
    return str(output)


def is_connected(cfg: dict) -> bool:
    value = cfg.get("preparation", {}).get("ed_csv", "")
    try:
        return bool(value and Path(value).resolve() == managed_path(cfg).resolve()
                    and Path(value).is_file() and Path(value).stat().st_size > 0)
    except (KeyError, OSError, ValueError):
        return False
