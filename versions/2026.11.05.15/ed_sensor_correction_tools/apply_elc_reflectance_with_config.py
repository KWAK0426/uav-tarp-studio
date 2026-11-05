from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from multiprocessing import freeze_support
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
WORK_ROOT = ROOT.parent
GENERIC_ELC_DIR = WORK_ROOT / "outputs" / "nongwon_preprocess_gui" / "generic_elc_tools"
sys.path.insert(0, str(GENERIC_ELC_DIR))

from generic_elc_gui import apply_one_job, format_seconds, load_coefficients  # noqa: E402


def say(message: str) -> None:
    print(message, flush=True)


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _general_targets(config):
    general = config.get("general", {})
    if general.get("enabled") is not True:
        return None
    if any(not general.get(key) for key in ("project_root", "date", "session", "site")):
        raise ValueError("General ELC requires the complete project identity")
    path = Path(config["source_list_path"])
    if sha(path) != config["source_list_sha256"]:
        raise ValueError("General target manifest changed before ELC apply")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or reader.fieldnames.count("shot") != 1:
            raise ValueError("General target manifest requires one shot column")
        rows = [row.get("shot", "").strip().upper() for row in reader]
    targets = set(rows)
    selected = {str(s).upper() for s in config.get("selected_shots", [])}
    if (not targets or len(targets) != len(rows) or any(not re.fullmatch(r"IMG_\d{4}", s) for s in targets)
            or targets != set(config["target_shots"]) or not selected or not selected <= targets):
        raise ValueError("General ELC apply must be restricted to explicit target shots")
    if not config.get("band_names") or set(map(int, config.get("suffix_to_band", {}))) != set(range(1, 11)):
        raise ValueError("General ELC requires a complete actual-wavelength band mapping")
    if set(map(int, config["suffix_to_band"].values())) != set(range(1, 11)):
        raise ValueError("General ELC band mapping must be one-to-one")
    return selected


def collect_jobs(config: dict[str, Any], coefficients: dict[int, tuple[float, float]]) -> list[dict[str, Any]]:
    input_dir = Path(config["apply_radiance_dir"])
    output_dir = Path(config["output_root"]) / config["reflectance_dir_name"]
    jobs: list[dict[str, Any]] = []
    destinations: dict[str, Path] = {}
    recursive = bool(config.get("recursive", False))
    general_shots = _general_targets(config)
    selected_shots = general_shots or set(config.get("selected_shots") or [])
    suffix_to_band = {int(k): int(v) for k, v in (config.get("suffix_to_band") or {}).items()}
    found = set()
    if general_shots is not None:
        from osgeo import gdal
        output_bands=config.get("output_bands",list(range(1,11)))
        if (not output_bands or output_bands!=sorted(set(output_bands)) or
                any(type(b) is not int or b not in range(1,11) for b in output_bands) or set(coefficients)!=set(output_bands)):
            raise ValueError("계수 CSV와 승인된 산출 밴드가 다릅니다.")
        if load_coefficients(Path(config["coef_csv"])) != coefficients:
            raise ValueError("Coefficient CSV changed before General ELC job collection")
        coefficient_sha = sha(config["coef_csv"])
        identity = json.dumps(config["general"], sort_keys=True)
    sources = input_dir.rglob("*") if recursive else input_dir.glob("*")
    for src in sources:
        if not src.is_file() or src.suffix.lower() not in {".tif", ".tiff"}:
            continue
        if selected_shots:
            match = re.fullmatch(r"(IMG_\d{4})_(?:10|[1-9])", src.stem, re.I)
            if not match or match.group(1).upper() not in selected_shots:
                continue
        stem = src.stem
        if "_" not in stem:
            continue
        try:
            suffix = int(stem.rsplit("_", 1)[1])
        except ValueError:
            continue
        band = suffix_to_band[suffix] if general_shots is not None else suffix_to_band.get(suffix, suffix)
        if band not in coefficients:
            continue
        rel = Path(src.name)
        dst = output_dir / rel
        destination_key = str(rel).casefold()
        if destination_key in destinations:
            raise ValueError(f"Duplicate reflectance output name: {destinations[destination_key]} / {src}")
        destinations[destination_key] = src
        provenance = None
        if general_shots is not None:
            key = src.stem.rsplit("_", 1)[0].upper(), suffix
            if key in found:
                raise ValueError(f"Duplicate General normalized shot/suffix: {src}")
            found.add(key)
            ds = gdal.Open(str(src), gdal.GA_ReadOnly)
            if ds is None:
                raise ValueError(f"Cannot open General normalized input: {src}")
            tags = ds.GetMetadata()
            size = [ds.RasterXSize, ds.RasterYSize]
            ds = None
            wave = float(tags.get("CENTRAL_WAVELENGTH", "nan"))
            center = float(re.search(r"(\d+)nm$", config["band_names"][str(band)])[1])
            if not math.isfinite(wave) or abs(wave - center) > 3:
                raise ValueError(f"CENTRAL_WAVELENGTH missing or mismatched: {src}")
            stage = tags.get("PROCESSING_STAGE")
            if stage != "radiometric_sensor_ed_integrated" or tags.get("ED_NORMALIZATION_APPLIED") != "true":
                raise ValueError(f"General ELC requires provenance-tagged normalized radiance: {src}")
            provenance = {"PROCESSING_STAGE": "ELC_reflectance", "ELC_COEFFICIENT_PATH": str(Path(config["coef_csv"]).resolve()),
                          "ELC_COEFFICIENT_SHA256": coefficient_sha, "ELC_COEFFICIENT_CONVENTION": "L=a*rho+b",
                          "ELC_APPLY_FORMULA": "rho=(L-b)/a", "REFLECTANCE_UNITS": "unitless",
                          "INPUT_PATH": str(src.resolve()), "INPUT_PROCESSING_STAGE": stage, "SOURCE_PROCESSING_STAGE": stage,
                          "INPUT_SIZE_BYTES": str(src.stat().st_size), "INPUT_MTIME_NS": str(src.stat().st_mtime_ns),
                          "INPUT_PROVENANCE_MANIFEST_SHA256": tags.get("PROVENANCE_MANIFEST_SHA256", ""),
                          "GENERAL_IDENTITY": identity, "SOURCE_LIST_PATH": str(Path(config["source_list_path"]).resolve()),
                          "SOURCE_LIST_SHA256": config["source_list_sha256"]}
        reuse_after_ns = config.get("reuse_after_ns")
        jobs.append(
            {
                "src": str(src),
                "rel": str(rel),
                "band": band,
                "coefficients": {str(k): v for k, v in coefficients.items()},
                "outputs": {"reflectance": str(output_dir)},
                "overwrite": bool(config.get("overwrite", False) or
                                  (reuse_after_ns is not None and dst.exists() and dst.stat().st_mtime_ns < int(reuse_after_ns))),
                "compression": str(config.get("compression", "DEFLATE")),
                **({"elc_provenance": provenance, "input_stat": [src.stat().st_size, src.stat().st_mtime_ns],
                    "native_size": size} if provenance else {}),
            }
        )
    if general_shots is not None and found != {(s, suffix) for s in general_shots for suffix,band in suffix_to_band.items() if band in output_bands}:
        raise ValueError("산출 대상 밴드의 정규화 입력이 불완전합니다.")
    return sorted(jobs, key=lambda job: job["rel"])


def apply_checked_job(job):
    provenance = job.get("elc_provenance")
    if not provenance:
        return apply_one_job(job)
    from osgeo import gdal
    from metashape_metadata import copy_metashape_metadata
    source = Path(job["src"])
    try:
        def check_inputs():
            stat = source.stat()
            if ([stat.st_size, stat.st_mtime_ns] != job["input_stat"]
                    or sha(provenance["ELC_COEFFICIENT_PATH"]) != provenance["ELC_COEFFICIENT_SHA256"]
                    or sha(provenance["SOURCE_LIST_PATH"]) != provenance["SOURCE_LIST_SHA256"]):
                raise ValueError("General ELC source/coefficient/target manifest changed during apply")
        check_inputs()
        result = apply_one_job(job)
        if result.get("status") != "ok":
            return result
        check_inputs()
        output = Path(job["outputs"]["reflectance"]) / job["rel"]
        if str(output) in result.get("written", []):
            # The generic writer's metadata copy is best-effort; General must fail on copy errors.
            copy_metashape_metadata(source, output)
            gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
            ds = gdal.Open(str(output), gdal.GA_Update)
            for key, value in provenance.items():
                ds.SetMetadataItem(key, value)
            ds.FlushCache()
            ds = None
        ds = gdal.Open(str(output), gdal.GA_ReadOnly)
        tags = ds.GetMetadata()
        native = [ds.RasterXSize, ds.RasterYSize]
        valid_raster = ds.RasterCount == 1 and ds.GetRasterBand(1).DataType == gdal.GDT_Float32
        ds = None
        if native != job["native_size"] or not valid_raster or any(tags.get(k) != v for k, v in provenance.items() if v):
            raise ValueError(f"Existing General ELC output provenance mismatch: {output}")
        return result
    except Exception as exc:
        return {"status": "failed", "src": str(source), "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply ELC coefficients to corrected radiance images.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    if args.overwrite:
        config["overwrite"] = True
    coef_csv = Path(config["coef_csv"])
    input_dir = Path(config["apply_radiance_dir"])
    output_dir = Path(config["output_root"]) / config["reflectance_dir_name"]
    workers = max(1, int(config.get("worker_count", 3)))

    say(f"Input corrected radiance : {input_dir}")
    say(f"Coefficient CSV         : {coef_csv}")
    say(f"Output reflectance      : {output_dir}")
    say(f"Workers                 : {workers}")
    say(f"Overwrite               : {bool(config.get('overwrite', False))}")
    say("Loading coefficients...")
    coefficients = load_coefficients(coef_csv)
    say("Scanning input TIFF files...")
    jobs = collect_jobs(config, coefficients)
    if not jobs:
        say("ERROR: no matching TIFF jobs found.")
        return 2
    say(f"Files                   : {len(jobs)}")

    started = time.time()
    completed = 0
    failed = 0
    pending: set[Any] = set()
    job_iter = iter(jobs)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        for _ in range(workers * 2):
            try:
                pending.add(executor.submit(apply_checked_job, next(job_iter)))
            except StopIteration:
                break
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED, timeout=1.0)
            for future in done:
                result = future.result()
                completed += 1
                if result.get("status") != "ok":
                    failed += 1
                    say(f"ERROR: {result.get('src')}: {result.get('error')}")
                if completed == 1 or completed % 100 == 0 or completed == len(jobs):
                    elapsed = time.time() - started
                    rate = completed / elapsed if elapsed > 0 else 0.0
                    eta = (len(jobs) - completed) / rate if rate > 0 else math.nan
                    say(
                        f"ELC reflectance: {completed}/{len(jobs)} "
                        f"({completed / len(jobs) * 100:.2f}%), "
                        f"{rate:.2f} files/s, ETA {format_seconds(eta)}, failed={failed}"
                    )
            while len(pending) < workers * 2:
                try:
                    pending.add(executor.submit(apply_checked_job, next(job_iter)))
                except StopIteration:
                    break
    say(f"COMPLETE completed={completed}, failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
