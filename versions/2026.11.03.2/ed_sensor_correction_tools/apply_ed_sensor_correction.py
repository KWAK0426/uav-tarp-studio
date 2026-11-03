from __future__ import annotations
from uav_pipeline.exif_support import executable as exif_executable

import argparse
import csv
import json
import math
import re
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timedelta
from multiprocessing import freeze_support
from pathlib import Path
from typing import Any

import numpy as np
from osgeo import gdal


SHOT_RE = re.compile(r"^(IMG_\d{4})_(\d+)\.(tif|tiff)$", re.IGNORECASE)

BAND_COLUMNS = {
    1: "Band1_Blue_444nm",
    2: "Band2_Blue_475nm",
    3: "Band3_Green_531nm",
    4: "Band4_Green_560nm",
    5: "Band5_Red_650nm",
    6: "Band6_Red_668nm",
    7: "Band7_RedEdge_705nm",
    8: "Band8_RedEdge_717nm",
    9: "Band9_RedEdge_740nm",
    10: "Band10_NIR_842nm",
}

BAND_NAMES = {
    1: "Blue_444nm",
    2: "Blue_475nm",
    3: "Green_531nm",
    4: "Green_560nm",
    5: "Red_650nm",
    6: "Red_668nm",
    7: "RedEdge_705nm",
    8: "RedEdge_717nm",
    9: "RedEdge_740nm",
    10: "NIR_842nm",
}

BAND_CENTERS = {band: float(re.search(r"(\d+)nm$", name).group(1)) for band, name in BAND_NAMES.items()}

DEFAULT_MX_GAINS = {
    1: 1.10,
    2: 1.17,
    3: 1.11,
    4: 1.07,
    5: 1.14,
    6: 1.06,
    7: 1.18,
    8: 1.08,
    9: 1.03,
    10: 1.11,
}

METASHAPE_METADATA_COPY_ARGS = [
    "-XMP",
    "-EXIF:all",
    "-GPS:all",
    "-IFD0:Make",
    "-IFD0:Model",
    "-IFD0:DateTime",
    "-IFD0:Software",
]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_exif_time(value: str) -> datetime:
    value = value.strip().split("+")[0].replace("T", " ")
    if "." in value:
        main, frac = value.split(".", 1)
        frac = re.sub(r"\D.*$", "", frac)[:6].ljust(6, "0")
        return datetime.strptime(f"{main}.{frac}", "%Y:%m:%d %H:%M:%S.%f")
    return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")


def parse_csv_time(value: str) -> datetime:
    value = value.strip()
    if "." in value:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def fmt_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f").rstrip("0").rstrip(".")


def format_seconds(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds}s"


def load_frame_groups(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError(f"Empty frame_groups.csv: {path}")
    return rows


def load_allowed_shots(path_text: str | None) -> set[str] | None:
    if not path_text:
        return None
    path = Path(path_text)
    if not path.exists():
        raise RuntimeError(f"allowed_shots_file does not exist: {path}")
    shots: set[str] = set()
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.search(r"(IMG_\d{4})_(?:[1-9]|10)\.tiff?", line, re.IGNORECASE)
            if match:
                shots.add(match.group(1).upper())
    if not shots:
        raise RuntimeError(f"No IMG labels found in allowed_shots_file: {path}")
    return shots


def load_ed_rows(path: Path, cycle: str, list_kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("cycle") != cycle or row.get("list_kind") != list_kind:
                continue
            item: dict[str, Any] = {
                "datetime": parse_csv_time(row["datetime_kst"]),
                "source_file": row.get("source_file", ""),
            }
            for band, col in BAND_COLUMNS.items():
                item[band] = float(row[col])
            out.append(item)
    if not out:
        raise RuntimeError(f"No Ed rows for {cycle}/{list_kind}: {path}")
    out.sort(key=lambda x: x["datetime"])
    return out


def nearest_ed_row(ed_rows: list[dict[str, Any]], when: datetime) -> tuple[dict[str, Any], float]:
    best = min(ed_rows, key=lambda r: abs((r["datetime"] - when).total_seconds()))
    return best, abs((best["datetime"] - when).total_seconds())


def run_exiftool_times(paths: list[str], chunk_size: int) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for start in range(0, len(paths), chunk_size):
        chunk = paths[start : start + chunk_size]
        cmd = [
            exif_executable(),
            "-csv",
            "-DateTimeOriginal",
            "-CreateDate",
            "-ModifyDate",
            "-SubSecDateTimeOriginal",
            "-SubSecCreateDate",
            "-SubSecModifyDate",
            *chunk,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if result.returncode != 0 and not result.stdout.strip():
            raise RuntimeError((result.stderr or result.stdout or "exiftool failed").strip())
        for row in csv.DictReader(result.stdout.splitlines()):
            src = str(Path(row["SourceFile"]))
            value = (
                row.get("SubSecDateTimeOriginal")
                or row.get("SubSecCreateDate")
                or row.get("SubSecModifyDate")
                or row.get("DateTimeOriginal")
                or row.get("CreateDate")
                or row.get("ModifyDate")
            )
            if value:
                out[src] = parse_exif_time(value)
    return out


def build_time_mapping(config: dict[str, Any], frame_rows: list[dict[str, str]]) -> dict[str, datetime]:
    cache_path = Path(config["time_mapping_csv"])
    offset = timedelta(hours=float(config.get("image_time_offset_hours", 9)))
    if cache_path.exists() and not bool(config.get("rebuild_time_mapping", False)):
        mapping: dict[str, datetime] = {}
        with cache_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                mapping[row["shot"]] = parse_csv_time(row["capture_time_kst"])
        if mapping:
            return mapping

    one_per_shot: dict[str, str] = {}
    for row in frame_rows:
        if str(row.get("band")) == "1" and row.get("shot") and row.get("source_path"):
            one_per_shot[row["shot"]] = row["source_path"]
    if not one_per_shot:
        raise RuntimeError("Could not find band 1 source paths in frame_groups.csv")

    time_source = str(config.get("time_source", "exif")).strip().lower()
    if time_source in {"linear", "linear_from_observation_summary"}:
        summary_path = Path(config["observation_summary_csv"])
        first_time = None
        last_time = None
        with summary_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("cycle") == str(config["cycle"]) and row.get("list_kind") == str(config["ed_list_kind"]):
                    first_time = parse_csv_time(row["first_time_kst_plus9"])
                    last_time = parse_csv_time(row["last_time_kst_plus9"])
                    break
        if first_time is None or last_time is None:
            raise RuntimeError(f"Could not find observation time summary for {config['cycle']}/{config['ed_list_kind']}")
        shots = sorted(one_per_shot, key=lambda x: int(x.replace("IMG_", "")))
        span = (last_time - first_time).total_seconds()
        denom = max(1, len(shots) - 1)
        mapping = {}
        rows_out = []
        for i, shot in enumerate(shots):
            capture_kst = first_time + timedelta(seconds=span * i / denom)
            mapping[shot] = capture_kst
            rows_out.append(
                {
                    "shot": shot,
                    "source_path": one_per_shot[shot],
                    "capture_time_raw": "",
                    "capture_time_kst": fmt_time(capture_kst),
                    "time_source": "linear_from_observation_summary",
                "time_note": "Uses remaining/allowed shots only; prefer exif when intermediate images may be missing.",
                }
            )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
            writer.writeheader()
            writer.writerows(rows_out)
        return mapping

    if time_source not in {"exif", "metadata", "image_exif", "exiftool"}:
        raise RuntimeError(f"Unsupported time_source: {config.get('time_source')}. Use 'exif' or 'linear'.")

    times_raw = run_exiftool_times(list(one_per_shot.values()), int(config.get("exiftool_batch_size", 300)))
    mapping = {}
    rows_out = []
    for shot, src in sorted(one_per_shot.items()):
        key = str(Path(src))
        when = times_raw.get(key)
        if when is None:
            continue
        capture_kst = when + offset
        mapping[shot] = capture_kst
        rows_out.append(
            {
                "shot": shot,
                "source_path": src,
                "capture_time_raw": fmt_time(when),
                "capture_time_kst": fmt_time(capture_kst),
                "time_source": "exif_metadata_plus_offset",
            }
        )
    if not mapping:
        raise RuntimeError("No image capture times could be extracted.")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        writer.writeheader()
        writer.writerows(rows_out)
    return mapping


def copy_metashape_metadata(source_path: Path, output_path: Path) -> None:
    result = subprocess.run(
        [
            exif_executable(),
            "-overwrite_original",
            "-P",
            "-m",
            "-TagsFromFile",
            str(source_path),
            *METASHAPE_METADATA_COPY_ARGS,
            str(output_path),
        ],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Failed to copy metadata: {detail}")


def write_float_tif_like(src_path: Path, dst_path: Path, arr: np.ndarray, compression: str, nodata: float | None) -> None:
    src = gdal.Open(str(src_path), gdal.GA_ReadOnly)
    if src is None:
        raise RuntimeError(f"Cannot open source: {src_path}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_path.with_suffix(dst_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    options = ["TILED=YES", "BIGTIFF=IF_SAFER", "PREDICTOR=3"]
    if compression:
        options.append(f"COMPRESS={compression}")
    driver = gdal.GetDriverByName("GTiff")
    out = driver.Create(str(tmp), src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32, options)
    if out is None:
        raise RuntimeError(f"Cannot create output: {tmp}")
    out.SetGeoTransform(src.GetGeoTransform())
    out.SetProjection(src.GetProjection())
    out.SetMetadata(src.GetMetadata())
    band = out.GetRasterBand(1)
    if nodata is not None and np.isfinite(nodata):
        band.SetNoDataValue(float(nodata))
    band.WriteArray(arr.astype(np.float32))
    band.FlushCache()
    out.FlushCache()
    out = None
    src = None
    if dst_path.exists():
        dst_path.unlink()
    tmp.replace(dst_path)
    copy_metashape_metadata(src_path, dst_path)


def resolve_radiometric_path(input_root: Path, row: dict[str, str]) -> Path:
    file_name = row.get("file_name", "")
    folder = row.get("folder", "")
    direct = input_root / folder / file_name
    if direct.exists():
        return direct
    hits = list(input_root.rglob(file_name))
    if hits:
        return hits[0]
    return direct


def spectral_band_for_wavelength(wavelength: float) -> int:
    band = min(BAND_CENTERS, key=lambda key: abs(BAND_CENTERS[key] - wavelength))
    if abs(BAND_CENTERS[band] - wavelength) > 3.0:
        raise RuntimeError(f"Unsupported MX Dual central wavelength: {wavelength:g} nm")
    return band


def central_wavelength(path: Path) -> float:
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Cannot open radiometric image metadata: {path}")
    xmp = "\n".join(ds.GetMetadata("xml:XMP") or [])
    ds = None
    match = re.search(r"CentralWavelength[^>]*>([^<]+)", xmp)
    if not match:
        raise RuntimeError(f"CentralWavelength missing from radiometric image: {path}")
    return float(match.group(1))


def collect_jobs(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    input_root = Path(config["input_root"])
    output_root = Path(config["output_root"])
    frame_rows = load_frame_groups(Path(config["frame_groups_csv"]))
    allowed_shots = load_allowed_shots(config.get("allowed_shots_file"))
    if allowed_shots is not None:
        frame_rows = [row for row in frame_rows if row.get("shot", "").upper() in allowed_shots]
    ed_rows = load_ed_rows(Path(config["ed_csv"]), str(config["cycle"]), str(config["ed_list_kind"]))
    shot_times = build_time_mapping(config, frame_rows)
    gains = {int(k): float(v) for k, v in dict(DEFAULT_MX_GAINS, **config.get("sensor_gains", {})).items()}
    reference_shot = str(config["reference_shot"])
    if not reference_shot.startswith("IMG_"):
        reference_shot = f"IMG_{int(reference_shot):04d}"
    if reference_shot not in shot_times:
        raise RuntimeError(f"Reference shot not found in time mapping: {reference_shot}")
    ref_time = shot_times[reference_shot]
    ref_ed_row, ref_delta = nearest_ed_row(ed_rows, ref_time)
    max_delta = float(config.get("max_ed_time_delta_seconds", 120))
    if ref_delta > max_delta:
        raise RuntimeError(f"Reference Ed time is too far: {ref_delta:.1f}s > {max_delta:.1f}s")

    suffix_to_band: dict[int, int] = {}
    for row in frame_rows:
        suffix = int(row.get("band", 0) or 0)
        if suffix and suffix not in suffix_to_band:
            path = resolve_radiometric_path(input_root, row)
            if path.exists():
                suffix_to_band[suffix] = spectral_band_for_wavelength(central_wavelength(path))
    if set(suffix_to_band) != set(range(1, 11)) or set(suffix_to_band.values()) != set(range(1, 11)):
        raise RuntimeError(f"MX Dual suffix/wavelength mapping is incomplete or duplicated: {suffix_to_band}")

    jobs: list[dict[str, Any]] = []
    map_rows: list[dict[str, Any]] = []
    for row in frame_rows:
        shot = row.get("shot", "")
        band_text = row.get("band", "")
        if not shot or not band_text:
            continue
        file_suffix = int(band_text)
        band = suffix_to_band.get(file_suffix)
        if band not in BAND_COLUMNS or shot not in shot_times:
            continue
        src_path = resolve_radiometric_path(input_root, row)
        if not src_path.exists():
            map_rows.append({"shot": shot, "band": band, "status": "missing_radiometric", "source": str(src_path)})
            continue
        capture_time = shot_times[shot]
        ed_row, delta = nearest_ed_row(ed_rows, capture_time)
        if delta > max_delta:
            map_rows.append({"shot": shot, "band": band, "status": "ed_time_delta_too_large", "source": str(src_path), "delta_seconds": delta})
            continue
        image_ed = float(ed_row[band])
        ref_ed = float(ref_ed_row[band])
        ed_scale = ref_ed / image_ed if image_ed else math.nan
        gain = float(gains.get(band, 1.0))
        total_scale = ed_scale * gain
        dst_path = output_root / src_path.relative_to(input_root)
        job = {
            "src": str(src_path),
            "dst": str(dst_path),
            "shot": shot,
            "band": band,
            "file_suffix": file_suffix,
            "central_wavelength_nm": BAND_CENTERS[band],
            "band_name": BAND_NAMES[band],
            "capture_time_kst": fmt_time(capture_time),
            "ed_time_kst": ed_row["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
            "reference_shot": reference_shot,
            "reference_time_kst": fmt_time(ref_time),
            "reference_ed_time_kst": ref_ed_row["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
            "image_ed": image_ed,
            "reference_ed": ref_ed,
            "ed_scale": ed_scale,
            "sensor_gain": gain,
            "total_scale": total_scale,
            "overwrite": bool(config.get("overwrite", False)),
            "compression": str(config.get("compression", "DEFLATE")),
        }
        jobs.append(job)
        map_rows.append({k: job[k] for k in job if k not in {"overwrite", "compression"}} | {"status": "planned"})

    summary = {
        "cycle": config["cycle"],
        "ed_list_kind": config["ed_list_kind"],
        "input_root": str(input_root),
        "output_root": str(output_root),
        "reference_shot": reference_shot,
        "reference_time_kst": fmt_time(ref_time),
        "reference_ed_time_kst": ref_ed_row["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
        "planned_files": len(jobs),
        "allowed_shot_count": len(allowed_shots) if allowed_shots is not None else None,
        "sensor_gains": gains,
        "file_suffix_to_spectral_band": suffix_to_band,
    }
    return jobs, map_rows, summary


def apply_one(job: dict[str, Any]) -> dict[str, Any]:
    src_path = Path(job["src"])
    dst_path = Path(job["dst"])
    if dst_path.exists() and not job["overwrite"]:
        return {"status": "skipped_exists", "src": str(src_path), "dst": str(dst_path)}
    ds = gdal.Open(str(src_path), gdal.GA_ReadOnly)
    if ds is None:
        return {"status": "failed", "src": str(src_path), "error": "cannot open source"}
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    arr = band.ReadAsArray().astype(np.float32)
    ds = None
    invalid = ~np.isfinite(arr)
    if nodata is not None:
        invalid |= arr == nodata
    out = arr * np.float32(job["total_scale"])
    out[invalid] = np.nan if nodata is None else np.float32(nodata)
    write_float_tif_like(src_path, dst_path, out, job["compression"], nodata)
    return {"status": "written", "src": str(src_path), "dst": str(dst_path)}


def write_mapping_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    keys = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply RAMSES Ed normalization and inter-sensor gain to radiometric TIFFs.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    config = read_json(Path(args.config))
    if args.overwrite:
        config["overwrite"] = True
    jobs, map_rows, summary = collect_jobs(config)
    output_root = Path(config["output_root"])
    map_path = Path(config.get("mapping_csv_path") or output_root / "_ed_sensor_correction_mapping.csv")
    summary_path = Path(config.get("summary_json_path") or output_root / "_ed_sensor_correction_summary.json")
    write_mapping_csv(map_path, map_rows)
    write_json(summary_path, summary)

    print(f"Cycle      : {summary['cycle']}")
    print(f"Input root : {summary['input_root']}")
    print(f"Output root: {summary['output_root']}")
    print(f"Reference : {summary['reference_shot']} / image {summary['reference_time_kst']} / Ed {summary['reference_ed_time_kst']}")
    print(f"Files      : {len(jobs)}")
    print(f"Mapping CSV: {map_path}")
    print(f"Summary    : {summary_path}")
    if args.dry_run:
        for job in jobs[:10]:
            print(f"DRY {job['shot']} band{job['band']} ed_scale={job['ed_scale']:.6f} gain={job['sensor_gain']:.4f} total={job['total_scale']:.6f}")
        return 0
    if not jobs:
        print("ERROR: no jobs found.")
        return 2

    workers = max(1, int(config.get("worker_count", 3)))
    total = len(jobs)
    completed = 0
    counts: dict[str, int] = {}
    started = time.time()
    pending: set[Any] = set()
    job_iter = iter(jobs)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        for _ in range(workers * 2):
            try:
                pending.add(executor.submit(apply_one, next(job_iter)))
            except StopIteration:
                break
        while pending:
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                completed += 1
                status = str(result.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
                if completed == 1 or completed % 100 == 0 or completed == total:
                    elapsed = time.time() - started
                    rate = completed / elapsed if elapsed > 0 else 0.0
                    eta = (total - completed) / rate if rate > 0 else math.nan
                    print(
                        f"Ed+sensor correction: {completed}/{total} ({completed / total * 100:.2f}%), "
                        f"{rate:.2f} files/s, ETA {format_seconds(eta)}, {counts}"
                    )
            while len(pending) < workers * 2:
                try:
                    pending.add(executor.submit(apply_one, next(job_iter)))
                except StopIteration:
                    break
    print(f"COMPLETE {counts}")
    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
