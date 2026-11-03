from __future__ import annotations
from uav_pipeline.exif_support import executable as exif_executable

import csv
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from metashape_metadata import copy_metashape_metadata


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "config_1st.json"
BAND_RE = re.compile(r"^(IMG_\d+)_([0-9]+)\.(tif|tiff)$", re.IGNORECASE)
EXPECTED_ALIGNMENT_METHOD = "micasense_gradient_pyramid_ecc"
EXPECTED_UNDISTORT_MODEL = "micasense_opencv_optimal_matrix"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log_stdout(message: str) -> None:
    print(f"[{_now()}] {message}", flush=True)


def _require_packages() -> None:
    missing = []
    for package, import_name in [
        ("numpy", "numpy"),
        ("opencv-python", "cv2"),
        ("GDAL", "osgeo"),
        ("Pillow", "PIL"),
        ("micasense", "micasense"),
    ]:
        try:
            __import__(import_name)
        except Exception:
            missing.append(package)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            "Missing required packages: "
            + joined
            + "\nRun the distribution 00_INSTALL.bat, then 02_CHECK.bat."
        )


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


@dataclass
class Config:
    raw_root: str
    gps_path: str
    output_root: str
    qc_dir_name: str = "0. Preprocess_QC"
    radiometric_dir_name: str = "1. Radiometric_Calibration"
    aligned_dir_name: str = "2. Band_Alignment"
    numeric_folders_only: bool = True
    overwrite: bool = False
    metadata_batch_size: int = 200
    worker_count: int = 3
    process_limit_shots: int = 0
    benchmark_shots: int = 5
    benchmark_max_workers: int = 4
    alignment_sample_shots: int = 25
    alignment_downscale: float = 0.5
    alignment_warp_mode: str = "affine"
    alignment_pyramid_levels: int = 2
    alignment_gauss_filter_size: int = 5
    alignment_max_iterations: int = 80
    alignment_epsilon: float = 0.00001
    alignment_groups: str = "sensor_internal_green"
    use_lens_undistort_for_alignment: bool = True
    output_dtype: str = "float32"
    compression: str = "DEFLATE"
    apply_sensor_correction: bool = False
    sensor_gains: dict[str, float] | None = None

    @classmethod
    def from_file(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        data = _read_json(path)
        return cls(
            raw_root=data["raw_root"],
            gps_path=data["gps_path"],
            output_root=data["output_root"],
            qc_dir_name=data.get("qc_dir_name", "0. Preprocess_QC"),
            radiometric_dir_name=data.get("radiometric_dir_name", "1. Radiometric_Calibration"),
            aligned_dir_name=data.get("aligned_dir_name", "2. Band_Alignment"),
            numeric_folders_only=_parse_bool(data.get("numeric_folders_only", True)),
            overwrite=_parse_bool(data.get("overwrite", False)),
            metadata_batch_size=int(data.get("metadata_batch_size", 200)),
            worker_count=int(data.get("worker_count", 3)),
            process_limit_shots=int(data.get("process_limit_shots", 0)),
            benchmark_shots=int(data.get("benchmark_shots", 5)),
            benchmark_max_workers=int(data.get("benchmark_max_workers", 4)),
            alignment_sample_shots=int(data.get("alignment_sample_shots", 25)),
            alignment_downscale=float(data.get("alignment_downscale", 0.5)),
            alignment_warp_mode=data.get("alignment_warp_mode", "affine"),
            alignment_pyramid_levels=int(data.get("alignment_pyramid_levels", 2)),
            alignment_gauss_filter_size=int(data.get("alignment_gauss_filter_size", 5)),
            alignment_max_iterations=int(data.get("alignment_max_iterations", 80)),
            alignment_epsilon=float(data.get("alignment_epsilon", 0.00001)),
            alignment_groups=data.get("alignment_groups", "sensor_internal_green"),
            use_lens_undistort_for_alignment=_parse_bool(
                data.get("use_lens_undistort_for_alignment", True)
            ),
            output_dtype=data.get("output_dtype", "float32"),
            compression=data.get("compression", "DEFLATE"),
            apply_sensor_correction=_parse_bool(data.get("apply_sensor_correction", False)),
            sensor_gains={str(k): float(v) for k, v in data.get("sensor_gains", {}).items()} or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_root": self.raw_root,
            "gps_path": self.gps_path,
            "output_root": self.output_root,
            "qc_dir_name": self.qc_dir_name,
            "radiometric_dir_name": self.radiometric_dir_name,
            "aligned_dir_name": self.aligned_dir_name,
            "numeric_folders_only": self.numeric_folders_only,
            "overwrite": self.overwrite,
            "metadata_batch_size": self.metadata_batch_size,
            "worker_count": self.worker_count,
            "process_limit_shots": self.process_limit_shots,
            "benchmark_shots": self.benchmark_shots,
            "benchmark_max_workers": self.benchmark_max_workers,
            "alignment_sample_shots": self.alignment_sample_shots,
            "alignment_downscale": self.alignment_downscale,
            "alignment_warp_mode": self.alignment_warp_mode,
            "alignment_pyramid_levels": self.alignment_pyramid_levels,
            "alignment_gauss_filter_size": self.alignment_gauss_filter_size,
            "alignment_max_iterations": self.alignment_max_iterations,
            "alignment_epsilon": self.alignment_epsilon,
            "alignment_groups": self.alignment_groups,
            "use_lens_undistort_for_alignment": self.use_lens_undistort_for_alignment,
            "output_dtype": self.output_dtype,
            "compression": self.compression,
            "apply_sensor_correction": self.apply_sensor_correction,
            "sensor_gains": self.sensor_gains,
        }

    @property
    def raw_path(self) -> Path:
        return Path(self.raw_root)

    @property
    def gps_file(self) -> Path:
        return Path(self.gps_path)

    @property
    def output_path(self) -> Path:
        return Path(self.output_root)

    @property
    def qc_dir(self) -> Path:
        return self.output_path / self.qc_dir_name

    @property
    def radiometric_dir(self) -> Path:
        return self.output_path / self.radiometric_dir_name

    @property
    def aligned_dir(self) -> Path:
        return self.output_path / self.aligned_dir_name


def _stop_flag_path(config: Config) -> Path:
    return config.qc_dir / "STOP_REQUESTED.flag"


def _progress_path(config: Config) -> Path:
    return config.qc_dir / "progress_state.json"


def _clear_stop_flag(config: Config) -> None:
    flag = _stop_flag_path(config)
    if flag.exists():
        flag.unlink()


def _request_stop(config: Config) -> None:
    config.qc_dir.mkdir(parents=True, exist_ok=True)
    _stop_flag_path(config).write_text(
        f"Stop requested at {_now()}\n", encoding="utf-8"
    )


def _stop_requested(config_or_flag: Config | Path) -> bool:
    flag = _stop_flag_path(config_or_flag) if isinstance(config_or_flag, Config) else config_or_flag
    return flag.exists()


def _drive_free_gb(path: Path) -> float:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    usage = shutil.disk_usage(str(target))
    return usage.free / (1024**3)


def _format_eta(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "unknown"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def _load_benchmark_rows(config: Config) -> list[dict[str, str]]:
    path = config.qc_dir / "benchmark_summary.csv"
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _choose_benchmark_row(config: Config) -> dict[str, str] | None:
    rows = [
        row
        for row in _load_benchmark_rows(config)
        if str(row.get("status", "")).strip().lower() == "ok"
    ]
    if not rows:
        return None
    exact = [row for row in rows if _safe_int(row.get("workers")) == int(config.worker_count)]
    if exact:
        return exact[-1]
    return min(rows, key=lambda row: abs(_safe_int(row.get("workers")) - int(config.worker_count)))


def _planned_file_count(config: Config) -> int:
    scan_summary = _read_json(config.qc_dir / "scan_summary.json") if (config.qc_dir / "scan_summary.json").exists() else {}
    planned = _safe_int(scan_summary.get("planned_tif_count"))
    if planned:
        return planned
    if config.process_limit_shots > 0:
        return config.process_limit_shots * 10
    try:
        return len(_selected_raw_files(config))
    except Exception:
        return 0


def estimate_stage_times(config: Config) -> dict[str, Any]:
    planned = _planned_file_count(config)
    row = _choose_benchmark_row(config)
    result: dict[str, Any] = {
        "planned_files": planned,
        "workers": config.worker_count,
        "has_benchmark": bool(row),
        "lines": [],
        "total_seconds": math.nan,
    }
    if not row:
        result["lines"] = [
            "Stage estimates: run Benchmark / Auto Tune first.",
            f"Planned files: {planned or 'unknown'}",
        ]
        return result

    metadata_rate = _safe_float(row.get("metadata_rate_files_per_second"))
    radiometric_rate = _safe_float(row.get("radiometric_rate_files_per_second"))
    alignment_rate = _safe_float(row.get("alignment_rate_files_per_second"))
    alignment_estimate_elapsed = _safe_float(row.get("alignment_estimate_elapsed_seconds"))
    benchmark_shots = max(1, _safe_int(row.get("benchmark_shots")))
    estimate_sample_count = max(1, min(int(config.alignment_sample_shots), benchmark_shots))
    target_sample_count = max(1, int(config.alignment_sample_shots))

    metadata_seconds = planned / metadata_rate if planned and metadata_rate > 0 else math.nan
    radiometric_seconds = planned / radiometric_rate if planned and radiometric_rate > 0 else math.nan
    estimate_alignment_seconds = (
        alignment_estimate_elapsed / estimate_sample_count * target_sample_count
        if alignment_estimate_elapsed > 0
        else math.nan
    )
    apply_alignment_seconds = planned / alignment_rate if planned and alignment_rate > 0 else math.nan

    stage_seconds = [
        ("1 Scan/QC", math.nan),
        ("2 Extract Metadata", metadata_seconds),
        ("3 Run Radiometric", radiometric_seconds),
        ("4 Estimate Alignment", estimate_alignment_seconds),
        ("5 Apply Alignment", apply_alignment_seconds),
    ]
    total_seconds = sum(seconds for _label, seconds in stage_seconds if math.isfinite(seconds))
    result.update(
        {
            "metadata_seconds": metadata_seconds,
            "radiometric_seconds": radiometric_seconds,
            "estimate_alignment_seconds": estimate_alignment_seconds,
            "apply_alignment_seconds": apply_alignment_seconds,
            "total_seconds": total_seconds,
            "benchmark_workers": _safe_int(row.get("workers")),
        }
    )
    result["lines"] = [
        f"Stage estimates from benchmark workers={result['benchmark_workers']} | planned files={planned}",
        f"1 Scan/QC: usually under 1m",
        f"2 Extract Metadata: {_format_eta(metadata_seconds)}",
        f"3 Run Radiometric: {_format_eta(radiometric_seconds)}",
        f"4 Estimate Alignment: {_format_eta(estimate_alignment_seconds)}",
        f"5 Apply Alignment: {_format_eta(apply_alignment_seconds)}",
        f"Estimated stages 2-5 total: {_format_eta(total_seconds)}",
    ]
    return result


def _write_progress(
    config: Config,
    stage: str,
    completed: int,
    total: int,
    started: float,
    status: str,
    log_path: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    elapsed = max(time.time() - started, 1e-6)
    rate = completed / elapsed if completed else 0.0
    eta = (total - completed) / rate if rate > 0 else math.nan
    data = {
        "stage": stage,
        "status": status,
        "completed": completed,
        "total": total,
        "percent": round((completed / total * 100.0) if total else 0.0, 2),
        "elapsed_seconds": round(elapsed, 1),
        "eta_seconds": None if not math.isfinite(eta) else round(eta, 1),
        "eta_text": _format_eta(eta),
        "rate_files_per_second": round(rate, 3),
        "free_output_drive_gb": round(_drive_free_gb(config.output_path), 2),
        "updated_at": _now(),
    }
    if log_path:
        data["log"] = str(log_path)
    if extra:
        data.update(extra)
    _write_json(_progress_path(config), data)
    return data


def _parse_band_name(path: Path) -> tuple[str, int] | None:
    m = BAND_RE.match(path.name)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _load_gps_labels(gps_path: Path) -> list[str]:
    labels = []
    with gps_path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            label = line.split("\t")[0].strip()
            if label:
                labels.append(label)
    return labels


def _selected_raw_files(config: Config) -> list[Path]:
    gps_labels = _load_gps_labels(config.gps_file)
    gps_stems = {Path(name).stem.lower() for name in gps_labels}
    folders: list[Path]
    if config.numeric_folders_only:
        folders = [
            p
            for p in sorted(config.raw_path.iterdir())
            if p.is_dir() and re.fullmatch(r"\d+", p.name)
        ]
    else:
        folders = [config.raw_path]

    selected = []
    for folder in folders:
        for p in folder.glob("*.tif"):
            if p.stem.lower() in gps_stems and _parse_band_name(p):
                selected.append(p)
        for p in folder.glob("*.tiff"):
            if p.stem.lower() in gps_stems and _parse_band_name(p):
                selected.append(p)
    return sorted(selected, key=lambda p: (p.parent.name, p.name.lower()))


def scan_inputs(config: Config, log: Callable[[str], None] = _log_stdout) -> dict[str, Any]:
    started = time.time()
    _write_progress(config, "scan_qc", 0, 1, started, "running")
    log("Scanning raw images and GPS label list...")
    config.qc_dir.mkdir(parents=True, exist_ok=True)
    gps_labels = _load_gps_labels(config.gps_file)
    gps_stems = {Path(name).stem.lower() for name in gps_labels}

    if config.numeric_folders_only:
        folders = [
            p
            for p in sorted(config.raw_path.iterdir())
            if p.is_dir() and re.fullmatch(r"\d+", p.name)
        ]
    else:
        folders = [config.raw_path]
    raw_files = []
    for folder in folders:
        raw_files.extend([p for p in folder.glob("*.tif") if _parse_band_name(p)])
        raw_files.extend([p for p in folder.glob("*.tiff") if _parse_band_name(p)])

    selected = [p for p in raw_files if p.stem.lower() in gps_stems]
    extra = [p for p in raw_files if p.stem.lower() not in gps_stems]

    rows = []
    for p in selected:
        parsed = _parse_band_name(p)
        if parsed is None:
            continue
        shot, band = parsed
        rows.append(
            {
                "shot": shot,
                "band": band,
                "folder": p.parent.name,
                "file_name": p.name,
                "source_path": str(p),
                "radiometric_path": str(config.radiometric_dir / p.parent.name / p.name),
                "aligned_path": str(config.aligned_dir / p.parent.name / p.name),
            }
        )

    shot_groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        shot_groups.setdefault(row["shot"], []).append(row)

    partial_shots = []
    for shot, group in shot_groups.items():
        bands = sorted({int(row["band"]) for row in group})
        if bands != list(range(1, 11)):
            partial_shots.append({"shot": shot, "bands": ",".join(map(str, bands))})

    if config.process_limit_shots > 0:
        keep_shots = set(sorted(shot_groups.keys())[: config.process_limit_shots])
        rows = [row for row in rows if row["shot"] in keep_shots]

    planned_size_bytes = 0
    for row in rows:
        try:
            planned_size_bytes += Path(row["source_path"]).stat().st_size
        except OSError:
            pass
    planned_input_gb = planned_size_bytes / (1024**3)
    # Float32 intermediates are roughly 2x raw UInt16 before compression. DEFLATE
    # usually reduces this, but keep the estimate conservative for safety.
    estimated_each_stage_gb = planned_input_gb * 2.0
    estimated_two_stage_gb = estimated_each_stage_gb * 2.0
    free_gb = _drive_free_gb(config.output_path)

    frame_csv = config.qc_dir / "frame_groups.csv"
    with frame_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "shot",
                "band",
                "folder",
                "file_name",
                "source_path",
                "radiometric_path",
                "aligned_path",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "gps_label_count": len(gps_labels),
        "raw_tif_count": len(raw_files),
        "selected_tif_count": len(selected),
        "extra_tif_count": len(extra),
        "selected_shot_count": len(shot_groups),
        "partial_shot_count": len(partial_shots),
        "process_limit_shots": config.process_limit_shots,
        "planned_tif_count": len(rows),
        "planned_input_gb": round(planned_input_gb, 2),
        "estimated_each_stage_output_gb_uncompressed": round(estimated_each_stage_gb, 2),
        "estimated_two_stage_output_gb_uncompressed": round(estimated_two_stage_gb, 2),
        "free_output_drive_gb": round(free_gb, 2),
        "disk_risk": free_gb < estimated_two_stage_gb + 20.0,
        "numeric_folders": [p.name for p in folders],
        "frame_groups_csv": str(frame_csv),
    }
    _write_json(config.qc_dir / "scan_summary.json", summary)
    if partial_shots:
        _write_json(config.qc_dir / "partial_shots.json", partial_shots)
    log(f"Scan complete: {len(rows)} files planned from {summary['selected_shot_count']} GPS-listed shots.")
    log(
        "Storage estimate: "
        f"input {planned_input_gb:.2f} GB, two intermediate stages up to "
        f"{estimated_two_stage_gb:.2f} GB before compression, free {free_gb:.2f} GB."
    )
    if summary["disk_risk"]:
        log("Warning: output drive free space is close to the conservative estimate.")
    _write_progress(
        config,
        "scan_qc",
        1,
        1,
        started,
        "complete",
        extra={
            "planned_tif_count": len(rows),
            "disk_risk": summary["disk_risk"],
            "estimated_two_stage_output_gb_uncompressed": round(estimated_two_stage_gb, 2),
        },
    )
    return summary


def _load_frame_rows(config: Config) -> list[dict[str, str]]:
    frame_csv = config.qc_dir / "frame_groups.csv"
    if not frame_csv.exists():
        scan_inputs(config)
    with frame_csv.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


EXIFTOOL_TAGS = [
    "-SourceFile",
    "-FileName",
    "-Directory",
    "-Make",
    "-Model",
    "-SerialNumber",
    "-ImageWidth",
    "-ImageHeight",
    "-BitsPerSample",
    "-BlackLevel",
    "-ExposureTime",
    "-ISOSpeed",
    "-ISO",
    "-RadiometricCalibration",
    "-VignettingCenter",
    "-VignettingPolynomial",
    "-BandName",
    "-CentralWavelength",
    "-WavelengthFWHM",
    "-PerspectiveFocalLength",
    "-PerspectiveDistortion",
    "-PrincipalPoint",
    "-FocalPlaneXResolution",
    "-FocalPlaneYResolution",
    "-RigCameraIndex",
    "-RigRelativesReferenceRigCameraIndex",
    "-RigRelatives",
]


def extract_metadata(config: Config, log: Callable[[str], None] = _log_stdout) -> dict[str, Any]:
    started = time.time()
    rows = _load_frame_rows(config)
    paths = [row["source_path"] for row in rows]
    config.qc_dir.mkdir(parents=True, exist_ok=True)
    debug_args_path = config.qc_dir / "exiftool_args.txt"
    runtime_dir = Path(os.environ.get("NONGWON_RUNTIME_DIR", APP_DIR / "_runtime"))
    runtime_dir.mkdir(parents=True, exist_ok=True)
    batch_size = max(10, int(config.metadata_batch_size))
    log(f"Extracting metadata with exiftool for {len(paths)} files in batches of {batch_size}...")
    _write_progress(config, "metadata", 0, len(paths), started, "running")

    def write_exiftool_args(path: Path, image_paths: list[str]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write("-j\n")
            f.write("-n\n")
            f.write("-G1\n")
            f.write("-a\n")
            f.write("-s\n")
            for tag in EXIFTOOL_TAGS:
                f.write(tag + "\n")
            for image_path in image_paths:
                f.write(image_path + "\n")

    write_exiftool_args(debug_args_path, paths)

    partial_path = config.qc_dir / "metadata_partial.json"
    metadata_list: list[dict[str, Any]] = []
    total = len(paths)
    resume_start = 0
    if partial_path.exists():
        try:
            partial_metadata = _read_json(partial_path)
            while resume_start < total:
                item = partial_metadata.get(str(Path(paths[resume_start])))
                if item is None:
                    break
                metadata_list.append(item)
                resume_start += 1
            if resume_start:
                log(f"Resuming metadata from existing partial file: {resume_start}/{total} records.")
                _write_progress(
                    config,
                    "metadata",
                    resume_start,
                    total,
                    started,
                    "running",
                    extra={"batch_size": batch_size, "partial_metadata_json": str(partial_path)},
                )
        except Exception:
            metadata_list = []
            resume_start = 0

    for start_idx in range(resume_start, total, batch_size):
        if _stop_requested(config):
            log("Metadata stop requested; ending after previous completed batch.")
            _write_progress(
                config,
                "metadata",
                len(metadata_list),
                total,
                started,
                "stopped",
                extra={"partial_metadata_json": str(partial_path)},
            )
            return {
                "status": "stopped",
                "metadata_partial_json": str(partial_path),
                "records": len(metadata_list),
            }
        batch_paths = paths[start_idx : start_idx + batch_size]
        args_path = runtime_dir / f"exiftool_args_{os.getpid()}_{time.time_ns()}.txt"
        write_exiftool_args(args_path, batch_paths)
        result = subprocess.run(
            [exif_executable(), "-charset", "filename=UTF8", "-@", str(args_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        try:
            batch_metadata = json.loads(result.stdout)
        except Exception as exc:
            raise RuntimeError(
                "Could not parse exiftool JSON on metadata batch "
                f"{start_idx + 1}-{start_idx + len(batch_paths)}: {exc}\n"
                + result.stdout[:1000]
                + ("\n" + result.stderr[:4000] if result.returncode != 0 else "")
            ) from exc
        if result.returncode != 0:
            log(
                "Warning: exiftool returned non-zero status but JSON metadata was parsed "
                f"for batch {start_idx + 1}-{start_idx + len(batch_paths)}. "
                "Continuing."
            )
        metadata_list.extend(batch_metadata)
        metadata_partial = {str(Path(item["SourceFile"])): item for item in metadata_list}
        _write_json(partial_path, metadata_partial)
        progress = _write_progress(
            config,
            "metadata",
            len(metadata_list),
            total,
            started,
            "running",
            extra={"batch_size": batch_size, "partial_metadata_json": str(partial_path)},
        )
        log(
            f"Metadata: {len(metadata_list)}/{total} "
            f"({progress['percent']:.2f}%), ETA {progress['eta_text']}"
        )

    metadata_by_source = {str(Path(item["SourceFile"])): item for item in metadata_list}
    _write_json(config.qc_dir / "metadata.json", metadata_by_source)

    csv_path = config.qc_dir / "metadata.csv"
    fields = [
        "SourceFile",
        "FileName",
        "BandName",
        "CentralWavelength",
        "BitsPerSample",
        "BlackLevel",
        "ExposureTime",
        "ISOSpeed",
        "RadiometricCalibration",
        "VignettingCenter",
        "VignettingPolynomial",
        "PerspectiveFocalLength",
        "PerspectiveDistortion",
        "PrincipalPoint",
        "FocalPlaneXResolution",
        "FocalPlaneYResolution",
        "RigCameraIndex",
        "RigRelativesReferenceRigCameraIndex",
        "RigRelatives",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in metadata_list:
            writer.writerow({field: _meta_get(item, field, default="") for field in fields})

    log(f"Metadata extracted: {len(metadata_list)} records.")
    _write_progress(config, "metadata", len(metadata_list), len(paths), started, "complete")
    return {
        "metadata_json": str(config.qc_dir / "metadata.json"),
        "metadata_csv": str(csv_path),
        "records": len(metadata_list),
    }


def _meta_get(meta: dict[str, Any], name: str, default: Any = None) -> Any:
    if name in meta:
        return meta[name]
    suffix = ":" + name
    for key, value in meta.items():
        if key.endswith(suffix):
            return value
    return default


def _parse_float_list(value: Any) -> list[float]:
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, list):
        return [float(v) for v in value]
    return [float(v) for v in re.split(r"[,\s]+", str(value).strip()) if v != ""]


def _load_metadata_map(config: Config) -> dict[str, dict[str, Any]]:
    path = config.qc_dir / "metadata.json"
    if not path.exists():
        extract_metadata(config)
    data = _read_json(path)
    return {str(Path(k)): v for k, v in data.items()}


def radiometric_pixels(job: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    src = Path(job["src"])
    meta = job["meta"]
    apply_sensor_correction = bool(job.get("apply_sensor_correction", False))
    sensor_gains = job.get("sensor_gains") or {}
    ds = gdal.Open(str(src), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Could not open {src}")
    raw = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    ysize, xsize = raw.shape
    bits = int(float(_meta_get(meta, "BitsPerSample", 16)))
    dn_max = float(2**bits)
    black_values = _parse_float_list(_meta_get(meta, "BlackLevel", 0.0))
    black_level = float(np.mean(black_values)) if black_values else 0.0
    exposure = float(_meta_get(meta, "ExposureTime"))
    iso = float(_meta_get(meta, "ISOSpeed", _meta_get(meta, "ISO", 100.0)))
    gain = iso / 100.0
    coeffs = _parse_float_list(_meta_get(meta, "RadiometricCalibration"))
    if len(coeffs) < 3:
        raise RuntimeError(f"RadiometricCalibration has fewer than 3 coefficients: {src}")
    a1, a2, a3 = coeffs[:3]
    center = _parse_float_list(_meta_get(meta, "VignettingCenter"))
    poly = _parse_float_list(_meta_get(meta, "VignettingPolynomial"))
    if len(center) < 2 or len(poly) < 6:
        raise RuntimeError(f"Missing vignette metadata: {src}")

    x = np.arange(xsize, dtype=np.float32)[None, :]
    y = np.arange(ysize, dtype=np.float32)[:, None]
    r = np.sqrt((x - float(center[0])) ** 2 + (y - float(center[1])) ** 2)
    vignette_den = np.ones_like(r, dtype=np.float32)
    r_power = r.copy()
    for k in poly:
        vignette_den += float(k) * r_power
        r_power *= r
    vignette = 1.0 / vignette_den
    row_correction = 1.0 / (1.0 + float(a2) * y / exposure - float(a3) * y)
    dark_corrected = raw - black_level
    dark_corrected[dark_corrected < 0] = 0
    radiance = vignette * row_correction * dark_corrected
    radiance = radiance / (gain * exposure) * float(a1) / dn_max
    sensor_gain = 1.0
    spectral_band = ""
    if apply_sensor_correction:
        band_centers = {1: 444.0, 2: 475.0, 3: 531.0, 4: 560.0, 5: 650.0,
                        6: 668.0, 7: 705.0, 8: 717.0, 9: 740.0, 10: 842.0}
        wavelength = float(_meta_get(meta, "CentralWavelength"))
        band = min(band_centers, key=lambda key: abs(band_centers[key] - wavelength))
        if abs(band_centers[band] - wavelength) > 3.0:
            raise RuntimeError(f"Unsupported wavelength for sensor intercalibration: {wavelength:g} nm ({src})")
        spectral_band = str(band)
        sensor_gain = float(sensor_gains.get(spectral_band, 1.0))
        if not np.isfinite(sensor_gain) or sensor_gain <= 0:
            raise RuntimeError(f"Invalid sensor gain for spectral band {band}: {sensor_gain}")
        radiance *= np.float32(sensor_gain)
    radiance = radiance.astype(np.float32)
    ds = None
    return radiance, {
        "SOURCE_FILE": str(src),
        "PROCESSING_STAGE": "radiometric_calibration_sensor_intercalibration" if apply_sensor_correction else "radiometric_calibration",
        "BAND_NAME": str(_meta_get(meta, "BandName", "")),
        "CENTRAL_WAVELENGTH": str(_meta_get(meta, "CentralWavelength", "")),
        "BLACK_LEVEL": str(black_level), "EXPOSURE_TIME": str(exposure), "ISO": str(iso), "GAIN": str(gain),
        "RADIOMETRIC_CALIBRATION": ",".join(map(str, coeffs)),
        "SENSOR_INTERCALIBRATION_APPLIED": str(apply_sensor_correction).lower(),
        "SENSOR_INTERCALIBRATION_SPECTRAL_BAND": spectral_band,
        "SENSOR_INTERCALIBRATION_GAIN": str(sensor_gain),
    }


def write_radiometric_pixels(src: Path, dst: Path, radiance: Any, metadata: dict[str, str], compression: str = "DEFLATE") -> None:
    from osgeo import gdal
    ysize, xsize = radiance.shape

    dst.parent.mkdir(parents=True, exist_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    out = driver.Create(
        str(tmp),
        xsize,
        ysize,
        1,
        gdal.GDT_Float32,
        options=["TILED=YES", f"COMPRESS={compression}", "PREDICTOR=3", "BIGTIFF=IF_SAFER"],
    )
    out.GetRasterBand(1).WriteArray(radiance)
    out.GetRasterBand(1).SetNoDataValue(0.0)
    out.SetMetadata(metadata)
    out.FlushCache()
    out = None
    tmp.replace(dst)
    copy_metashape_metadata(src, dst)
    out = gdal.Open(str(dst), gdal.GA_Update)
    if out is None:
        raise RuntimeError(f"Could not reopen output metadata: {dst}")
    out.SetMetadata({**out.GetMetadata(), **metadata})
    out.FlushCache()
    out = None


def _radiometric_worker(job: dict[str, Any]) -> dict[str, Any]:
    from osgeo import gdal
    src, dst = Path(job["src"]), Path(job["dst"])
    if dst.exists() and not job["overwrite"]:
        if job.get("apply_sensor_correction", False):
            existing = gdal.Open(str(dst), gdal.GA_ReadOnly)
            applied = (existing.GetMetadataItem("SENSOR_INTERCALIBRATION_APPLIED") or "").lower() == "true" if existing else False
            existing = None
            if not applied:
                raise RuntimeError(f"Existing output has no sensor intercalibration marker; refusing mixed output: {dst}")
        return {"status": "skipped_exists", "source": str(src), "output": str(dst)}
    radiance, metadata = radiometric_pixels(job)
    write_radiometric_pixels(src, dst, radiance, metadata, job["compression"])
    return {"status": "written", "source": str(src), "output": str(dst)}


def _parallel_map(
    jobs: list[dict[str, Any]],
    worker_func: Any,
    worker_count: int,
    log_path: Path,
    config: Config,
    stage_name: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    results = []
    failures = []
    stopped = False
    started = time.time()
    total = len(jobs)
    stop_flag = _stop_flag_path(config)
    max_pending = max(1, worker_count * 3)
    next_index = 0
    completed = 0
    last_log_time = 0.0
    last_rate = 0.0
    _write_progress(config, stage_name, 0, total, started, "running", log_path)

    def log_progress(force: bool = False) -> None:
        nonlocal last_log_time, last_rate
        elapsed = max(time.time() - started, 1e-6)
        rate = completed / elapsed if completed else 0.0
        should_log = (
            force
            or completed == 1
            or completed % 100 == 0
            or completed == total
            or time.time() - last_log_time > 30
        )
        if not should_log:
            return
        warning = ""
        if last_rate > 0 and rate < last_rate * 0.35:
            warning = "Processing rate dropped sharply; possible disk bottleneck."
        elif rate and rate < 0.4:
            warning = "Processing is slow; possible disk I/O bottleneck."
        progress = _write_progress(
            config,
            stage_name,
            completed,
            total,
            started,
            "stopping" if stopped else "running",
            log_path,
            {"warning": warning, "failed": len(failures)},
        )
        log(
            f"{stage_name}: {completed}/{total} ({progress['percent']:.2f}%), "
            f"{rate:.2f} files/s, ETA {progress['eta_text']}, "
            f"free {progress['free_output_drive_gb']:.1f} GB"
            + (f" - {warning}" if warning else "")
        )
        last_log_time = time.time()
        last_rate = rate

    with ProcessPoolExecutor(max_workers=max(1, worker_count)) as executor:
        future_map = {}

        def submit_more() -> None:
            nonlocal next_index
            while next_index < total and len(future_map) < max_pending and not _stop_requested(stop_flag):
                job = jobs[next_index]
                future_map[executor.submit(worker_func, job)] = job
                next_index += 1

        submit_more()
        while future_map:
            if _stop_requested(stop_flag):
                stopped = True
                for future in future_map:
                    future.cancel()
            done, _pending = wait(future_map.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
            if not done:
                log_progress()
                continue

            for future in done:
                job = future_map.pop(future)
                completed += 1
                if future.cancelled():
                    result = {
                        "status": "cancelled",
                        "source": job.get("src", ""),
                        "output": job.get("dst", ""),
                    }
                    results.append(result)
                    continue
                try:
                    result = future.result()
                    results.append(result)
                except Exception as exc:
                    failure = {
                        "status": "failed",
                        "source": job.get("src", ""),
                        "output": job.get("dst", ""),
                        "error": repr(exc),
                    }
                    failures.append(failure)
                    results.append(failure)

                log_progress()

            if _stop_requested(stop_flag):
                stopped = True
            if not stopped:
                submit_more()

    with log_path.open("w", newline="", encoding="utf-8-sig") as f:
        fieldnames = ["status", "source", "output", "error"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    summary = {
        "total": len(jobs),
        "written": sum(1 for r in results if r.get("status") == "written"),
        "skipped_exists": sum(1 for r in results if r.get("status") == "skipped_exists"),
        "cancelled": sum(1 for r in results if r.get("status") == "cancelled"),
        "failed": len(failures),
        "stopped": stopped,
        "elapsed_seconds": round(max(time.time() - started, 1e-6), 2),
        "rate_files_per_second": round(completed / max(time.time() - started, 1e-6), 3),
        "log": str(log_path),
    }
    _write_progress(
        config,
        stage_name,
        completed,
        total,
        started,
        "stopped" if stopped else "complete",
        log_path,
        {"failed": len(failures)},
    )
    log(f"Stage complete: {summary}")
    return summary


def run_radiometric(config: Config, log: Callable[[str], None] = _log_stdout) -> dict[str, Any]:
    _require_packages()
    rows = _load_frame_rows(config)
    metadata = _load_metadata_map(config)
    jobs = []
    for row in rows:
        src = str(Path(row["source_path"]))
        meta = metadata.get(src)
        if meta is None:
            raise RuntimeError(f"No metadata for {src}")
        jobs.append(
            {
                "src": src,
                "dst": row["radiometric_path"],
                "meta": meta,
                "overwrite": config.overwrite,
                "compression": config.compression,
                "apply_sensor_correction": config.apply_sensor_correction,
                "sensor_gains": config.sensor_gains,
            }
        )
    log(f"Running radiometric calibration for {len(jobs)} files with {config.worker_count} workers...")
    return _parallel_map(
        jobs,
        _radiometric_worker,
        config.worker_count,
        config.qc_dir / "radiometric_log.csv",
        config,
        "radiometric",
        log,
    )


def _read_float_tif(path: Path) -> Any:
    import numpy as np
    from osgeo import gdal

    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Could not open {path}")
    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    ds = None
    return arr


def _write_float_tif(
    path: Path,
    arr: Any,
    compression: str,
    metadata: dict[str, str] | None = None,
    metadata_source: Path | None = None,
) -> None:
    from osgeo import gdal

    ysize, xsize = arr.shape
    path.parent.mkdir(parents=True, exist_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    out = driver.Create(
        str(tmp),
        xsize,
        ysize,
        1,
        gdal.GDT_Float32,
        options=["TILED=YES", f"COMPRESS={compression}", "PREDICTOR=3", "BIGTIFF=IF_SAFER"],
    )
    out.GetRasterBand(1).WriteArray(arr)
    out.GetRasterBand(1).SetNoDataValue(0.0)
    if metadata:
        out.SetMetadata(metadata)
    out.FlushCache()
    out = None
    tmp.replace(path)
    if metadata_source is not None:
        copy_metashape_metadata(metadata_source, path)


def _camera_matrix_and_dist(meta: dict[str, Any], shape: tuple[int, int]) -> tuple[Any, Any] | None:
    import numpy as np

    focal = _meta_get(meta, "PerspectiveFocalLength")
    pp = _parse_float_list(_meta_get(meta, "PrincipalPoint"))
    xres = _meta_get(meta, "FocalPlaneXResolution")
    yres = _meta_get(meta, "FocalPlaneYResolution")
    dist = _parse_float_list(_meta_get(meta, "PerspectiveDistortion"))
    if focal is None or len(pp) < 2 or xres is None or yres is None or len(dist) < 5:
        return None
    fx = float(focal) * float(xres)
    fy = float(focal) * float(yres)
    cx = float(pp[0]) * float(xres)
    cy = float(pp[1]) * float(yres)
    camera_matrix = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float32)
    # MicaSense stores [k1, k2, k3, p1, p2]; OpenCV expects [k1, k2, p1, p2, k3].
    dist_coeffs = np.array([dist[0], dist[1], dist[3], dist[4], dist[2]], dtype=np.float32)
    return camera_matrix, dist_coeffs


def _undistort_if_needed(arr: Any, meta: dict[str, Any], enabled: bool) -> Any:
    if not enabled:
        return arr
    import cv2
    import numpy as np

    params = _camera_matrix_and_dist(meta, arr.shape)
    if params is None:
        return arr
    camera_matrix, dist_coeffs = params
    height, width = arr.shape
    new_camera_matrix, _ = cv2.getOptimalNewCameraMatrix(
        camera_matrix,
        dist_coeffs,
        (width, height),
        1,
    )
    map1, map2 = cv2.initUndistortRectifyMap(
        camera_matrix,
        dist_coeffs,
        np.eye(3, dtype=np.float32),
        new_camera_matrix,
        (width, height),
        cv2.CV_32F,
    )
    return cv2.remap(arr, map1, map2, cv2.INTER_LINEAR)


def _rig_xy_offset_in_px(meta: dict[str, Any]) -> tuple[float, float]:
    focal = _meta_get(meta, "PerspectiveFocalLength")
    xres = _meta_get(meta, "FocalPlaneXResolution")
    yres = _meta_get(meta, "FocalPlaneYResolution")
    rig = _parse_float_list(_meta_get(meta, "RigRelatives"))
    if focal is None or xres is None or yres is None or len(rig) < 2:
        return 0.0, 0.0
    pixel_pitch_mm_x = 1.0 / float(xres)
    pixel_pitch_mm_y = 1.0 / float(yres)
    px_fov_x = 2.0 * math.atan2(pixel_pitch_mm_x / 2.0, float(focal))
    px_fov_y = 2.0 * math.atan2(pixel_pitch_mm_y / 2.0, float(focal))
    if px_fov_x == 0 or px_fov_y == 0:
        return 0.0, 0.0
    t_x = math.radians(float(rig[0])) / px_fov_x
    t_y = math.radians(float(rig[1])) / px_fov_y
    return t_x, t_y


def _normalize_for_ecc(arr: Any, downscale: float) -> Any:
    import cv2
    import numpy as np

    data = arr.astype(np.float32)
    finite = np.isfinite(data)
    if not np.any(finite):
        raise RuntimeError("Image has no finite pixels")
    lo, hi = np.percentile(data[finite], [2, 98])
    if hi <= lo:
        hi = lo + 1.0
    data = (data - lo) / (hi - lo)
    data[data < 0] = 0
    data[data > 1] = 1
    data = cv2.GaussianBlur(data, (5, 5), 0)
    if downscale and downscale != 1.0:
        data = cv2.resize(data, None, fx=downscale, fy=downscale, interpolation=cv2.INTER_AREA)
    return data.astype(np.float32)


def _complete_shot_groups(rows: list[dict[str, str]]) -> dict[str, dict[int, dict[str, str]]]:
    by_shot: dict[str, dict[int, dict[str, str]]] = {}
    for row in rows:
        by_shot.setdefault(row["shot"], {})[int(row["band"])] = row
    return {shot: bands for shot, bands in by_shot.items() if sorted(bands) == list(range(1, 11))}


def _choose_preview_shot(
    by_shot: dict[str, dict[int, dict[str, str]]],
    path_key: str,
    preferred_shot: str | None = None,
) -> tuple[str, dict[int, dict[str, str]]] | None:
    shot_order = list(by_shot.keys())
    if preferred_shot and preferred_shot in by_shot:
        shot_order = [preferred_shot] + [shot for shot in shot_order if shot != preferred_shot]
    for shot in shot_order:
        bands = by_shot[shot]
        if all(Path(bands[band][path_key]).exists() for band in range(1, 11)):
            return shot, bands
    return None


def _preview_uint8(arr: Any) -> Any:
    import numpy as np

    data = arr.astype(np.float32)
    finite = np.isfinite(data)
    valid = finite & (data > 0)
    if int(np.count_nonzero(valid)) < 100:
        valid = finite
    if not np.any(valid):
        return np.zeros(data.shape, dtype=np.uint8)
    lo, hi = np.percentile(data[valid], [2, 98])
    if hi <= lo:
        hi = lo + 1.0
    scaled = (data - lo) / (hi - lo) * 255.0
    scaled[~finite] = 0
    scaled[scaled < 0] = 0
    scaled[scaled > 255] = 255
    return scaled.astype(np.uint8)


def _write_alignment_contact_sheet(
    bands: dict[int, dict[str, str]],
    path_key: str,
    output_path: Path,
    title: str,
) -> Path:
    import cv2
    import numpy as np

    tile_w, tile_h = 220, 165
    cols, grid_rows = 5, 2
    margin, gap, header_h, label_h = 16, 10, 42, 26
    width = margin * 2 + cols * tile_w + (cols - 1) * gap
    height = header_h + margin + grid_rows * (tile_h + label_h) + (grid_rows - 1) * gap + margin
    sheet = np.full((height, width, 3), 245, dtype=np.uint8)

    cv2.putText(sheet, title, (margin, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (30, 30, 30), 2, cv2.LINE_AA)
    for band in range(1, 11):
        row_idx = (band - 1) // cols
        col_idx = (band - 1) % cols
        x = margin + col_idx * (tile_w + gap)
        y = header_h + margin + row_idx * (tile_h + label_h + gap)
        src = Path(bands[band][path_key])
        arr = _preview_uint8(_read_float_tif(src))
        tile = cv2.resize(arr, (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        tile_bgr = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
        sheet[y : y + tile_h, x : x + tile_w] = tile_bgr
        cv2.rectangle(sheet, (x, y), (x + tile_w - 1, y + tile_h - 1), (185, 185, 185), 1)
        label = f"Band {band}: {src.name}"
        cv2.putText(
            sheet,
            label[:34],
            (x, y + tile_h + 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (50, 50, 50),
            1,
            cv2.LINE_AA,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", sheet)
    if not ok:
        raise RuntimeError(f"Could not encode preview PNG: {output_path}")
    encoded.tofile(str(output_path))
    return output_path


def _write_alignment_animation(
    bands: dict[int, dict[str, str]],
    path_key: str,
    output_path: Path,
    title: str,
) -> Path:
    import cv2
    import numpy as np
    from PIL import Image

    frame_w, image_w, image_h = 760, 720, 540
    header_h, footer_h, margin = 54, 30, 20
    frame_h = header_h + image_h + footer_h + margin
    frames = []
    for band in range(1, 11):
        src = Path(bands[band][path_key])
        arr = _preview_uint8(_read_float_tif(src))
        tile = cv2.resize(arr, (image_w, image_h), interpolation=cv2.INTER_AREA)
        frame = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
        canvas = 245 * np.ones((frame_h, frame_w, 3), dtype=np.uint8)
        x = (frame_w - image_w) // 2
        y = header_h
        canvas[y : y + image_h, x : x + image_w] = frame
        cv2.rectangle(canvas, (x, y), (x + image_w - 1, y + image_h - 1), (170, 170, 170), 1)
        cv2.putText(
            canvas,
            title,
            (margin, 26),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (32, 32, 32),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            canvas,
            f"Band {band:02d} | {src.name}",
            (margin, 48),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (40, 40, 40),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            canvas,
            "Loop: 1 -> 10",
            (margin, frame_h - 12),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            (70, 70, 70),
            1,
            cv2.LINE_AA,
        )
        frames.append(Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=550,
        loop=0,
        optimize=False,
    )
    return output_path


def generate_alignment_previews(config: Config) -> dict[str, Any]:
    _require_packages()
    rows = _load_frame_rows(config)
    by_shot = _complete_shot_groups(rows)
    preview_dir = config.qc_dir / "alignment_preview"
    result: dict[str, Any] = {"preview_dir": str(preview_dir), "before": "", "after": "", "shot": ""}

    before_choice = _choose_preview_shot(by_shot, "radiometric_path")
    if before_choice:
        shot, bands = before_choice
        result["shot"] = shot
        before_gif = preview_dir / f"{shot}_before_alignment_bands_1_to_10.gif"
        before_sheet = preview_dir / f"{shot}_before_alignment_sheet.png"
        _write_alignment_animation(bands, "radiometric_path", before_gif, f"Before alignment | {shot}")
        _write_alignment_contact_sheet(bands, "radiometric_path", before_sheet, f"Before alignment | {shot}")
        result["before"] = str(before_gif)
        result["before_sheet"] = str(before_sheet)
    else:
        result["before_message"] = "Run radiometric calibration first."

    after_choice = _choose_preview_shot(by_shot, "aligned_path", result.get("shot") or None)
    if after_choice:
        after_shot, bands = after_choice
        if not result.get("shot"):
            result["shot"] = after_shot
        after_gif = preview_dir / f"{after_shot}_after_alignment_bands_1_to_10.gif"
        after_sheet = preview_dir / f"{after_shot}_after_alignment_sheet.png"
        _write_alignment_animation(bands, "aligned_path", after_gif, f"After alignment | {after_shot}")
        _write_alignment_contact_sheet(bands, "aligned_path", after_sheet, f"After alignment | {after_shot}")
        result["after"] = str(after_gif)
        result["after_sheet"] = str(after_sheet)
        if result.get("shot") and after_shot != result["shot"]:
            result["after_message"] = f"After preview used {after_shot}; before preview used {result['shot']}."
    else:
        result["after_message"] = "Run band alignment first."

    _write_json(preview_dir / "alignment_preview_summary.json", result)
    return result


def _band_groups(mode: str) -> dict[int, list[int]]:
    if mode == "sensor_internal_green":
        return {2: [1, 2, 3, 4, 5], 7: [6, 7, 8, 9, 10]}
    if mode == "single_green_2":
        return {2: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}
    raise ValueError(f"Unsupported alignment_groups: {mode}")


def _scale_warp_matrix(matrix: Any, mode: str, downscale: float) -> list[list[float]]:
    import numpy as np

    if downscale == 1.0:
        return matrix.tolist()
    scale = float(downscale)
    mat = np.array(matrix, dtype=np.float32).copy()
    if mode == "affine":
        mat[0, 2] /= scale
        mat[1, 2] /= scale
    else:
        s = np.array([[scale, 0, 0], [0, scale, 0], [0, 0, 1]], dtype=np.float32)
        mat = np.linalg.inv(s) @ mat @ s
    return mat.tolist()


def estimate_alignment(config: Config, log: Callable[[str], None] = _log_stdout) -> dict[str, Any]:
    _require_packages()
    import cv2
    import micasense.imageutils as micasense_imageutils
    import numpy as np

    started = time.time()
    rows = _load_frame_rows(config)
    metadata = _load_metadata_map(config)
    by_shot: dict[str, dict[int, dict[str, str]]] = {}
    for row in rows:
        by_shot.setdefault(row["shot"], {})[int(row["band"])] = row
    complete_shots = [shot for shot, bands in by_shot.items() if sorted(bands) == list(range(1, 11))]
    sample_shots = complete_shots[: max(1, config.alignment_sample_shots)]
    log(f"Estimating alignment from {len(sample_shots)} sample shots...")
    _write_progress(config, "estimate_alignment", 0, len(sample_shots), started, "running")

    warp_mode = cv2.MOTION_AFFINE if config.alignment_warp_mode == "affine" else cv2.MOTION_HOMOGRAPHY
    gauss_filter_size = max(1, int(config.alignment_gauss_filter_size))
    if gauss_filter_size % 2 == 0:
        gauss_filter_size += 1
    groups = _band_groups(config.alignment_groups)
    per_band: dict[str, list[Any]] = {}
    qc_rows = []
    processed_samples = 0

    for shot_idx, shot in enumerate(sample_shots, start=1):
        if _stop_requested(config):
            log("Alignment estimation stop requested; ending after previous sample.")
            break
        processed_samples = shot_idx
        band_rows = by_shot[shot]
        for ref_band, group_bands in groups.items():
            ref_row = band_rows[ref_band]
            ref_path = Path(ref_row["radiometric_path"])
            ref_meta = metadata[str(Path(ref_row["source_path"]))]
            ref_img = _undistort_if_needed(
                _read_float_tif(ref_path),
                ref_meta,
                config.use_lens_undistort_for_alignment,
            ).astype(np.float32)
            for moving_band in group_bands:
                if moving_band == ref_band:
                    continue
                mov_row = band_rows[moving_band]
                mov_path = Path(mov_row["radiometric_path"])
                mov_meta = metadata[str(Path(mov_row["source_path"]))]
                mov_img = _undistort_if_needed(
                    _read_float_tif(mov_path),
                    mov_meta,
                    config.use_lens_undistort_for_alignment,
                ).astype(np.float32)
                if warp_mode == cv2.MOTION_HOMOGRAPHY:
                    warp = np.eye(3, 3, dtype=np.float32)
                else:
                    tx_px, ty_px = _rig_xy_offset_in_px(mov_meta)
                    warp = np.array([[1, 0, ty_px], [0, 1, tx_px]], dtype=np.float32)
                try:
                    aligned_pair = micasense_imageutils.align(
                        {
                            "warp_mode": warp_mode,
                            "max_iterations": int(config.alignment_max_iterations),
                            "epsilon_threshold": float(config.alignment_epsilon),
                            "ref_index": ref_band,
                            "ref_image": ref_img,
                            "match_index": moving_band,
                            "match_image": mov_img,
                            "translations": _rig_xy_offset_in_px(mov_meta),
                            "warp_matrix_init": warp,
                            "debug": False,
                            "pyramid_levels": int(config.alignment_pyramid_levels),
                        }
                    )
                    full_matrix = np.array(aligned_pair["warp_matrix"], dtype=np.float32).tolist()
                    per_band.setdefault(str(moving_band), []).append(full_matrix)
                    qc_rows.append(
                        {
                            "shot": shot,
                            "ref_band": ref_band,
                            "moving_band": moving_band,
                            "status": "success",
                            "ecc": "",
                        }
                    )
                except Exception as exc:
                    qc_rows.append(
                        {
                            "shot": shot,
                            "ref_band": ref_band,
                            "moving_band": moving_band,
                            "status": "failed",
                            "ecc": "",
                            "error": repr(exc),
                        }
                    )
        _write_progress(
            config,
            "estimate_alignment",
            shot_idx,
            len(sample_shots),
            started,
            "running",
            extra={
                "successful_pairs": sum(1 for row in qc_rows if row["status"] == "success"),
                "failed_pairs": sum(1 for row in qc_rows if row["status"] == "failed"),
            },
        )
        log(f"Alignment sample {shot_idx}/{len(sample_shots)} processed")

    transforms: dict[str, Any] = {
        "alignment_groups": config.alignment_groups,
        "warp_mode": config.alignment_warp_mode,
        "method": EXPECTED_ALIGNMENT_METHOD,
        "undistort_model": EXPECTED_UNDISTORT_MODEL,
        "gauss_filter_size": gauss_filter_size,
        "use_lens_undistort_for_alignment": config.use_lens_undistort_for_alignment,
        "reference_bands": list(groups.keys()),
        "bands": {},
    }
    missing_transform_bands = []
    for ref_band, group_bands in groups.items():
        identity = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        if config.alignment_warp_mode == "homography":
            identity = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        transforms["bands"][str(ref_band)] = {
            "ref_band": ref_band,
            "matrix": identity,
            "sample_count": 0,
            "is_reference": True,
        }
        for moving_band in group_bands:
            if moving_band == ref_band:
                continue
            mats = per_band.get(str(moving_band), [])
            if not mats:
                log(f"Warning: no successful transform for band {moving_band}; using identity.")
                matrix = identity
                missing_transform_bands.append(moving_band)
            else:
                matrix = np.median(np.array(mats, dtype=np.float32), axis=0).tolist()
            transforms["bands"][str(moving_band)] = {
                "ref_band": ref_band,
                "matrix": matrix,
                "sample_count": len(mats),
                "is_reference": False,
            }

    transforms["sample_count"] = processed_samples
    transforms["successful_pairs"] = success_count = sum(1 for row in qc_rows if row["status"] == "success")
    transforms["failed_pairs"] = failed_count = sum(1 for row in qc_rows if row["status"] == "failed")
    transforms["missing_transform_bands"] = missing_transform_bands
    transforms["status"] = "failed" if missing_transform_bands else "ok"
    _write_json(config.qc_dir / "alignment_transforms.json", transforms)
    qc_path = config.qc_dir / "alignment_estimation_qc.csv"
    with qc_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["shot", "ref_band", "moving_band", "status", "ecc", "error"], extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(qc_rows)
    log(f"Alignment estimation complete: {success_count} success, {failed_count} failed.")
    if missing_transform_bands:
        log(f"Alignment estimation incomplete; missing transforms for bands: {missing_transform_bands}")
    _write_progress(
        config,
        "estimate_alignment",
        processed_samples,
        len(sample_shots),
        started,
        "stopped" if _stop_requested(config) else ("failed" if missing_transform_bands else "complete"),
        extra={"successful_pairs": success_count, "failed_pairs": failed_count},
    )
    return {
        "status": transforms["status"],
        "transforms": str(config.qc_dir / "alignment_transforms.json"),
        "qc": str(qc_path),
        "successful_pairs": success_count,
        "failed_pairs": failed_count,
        "missing_transform_bands": missing_transform_bands,
    }


def _alignment_worker(job: dict[str, Any]) -> dict[str, Any]:
    import cv2
    import numpy as np

    src = Path(job["src"])
    dst = Path(job["dst"])
    if dst.exists() and not bool(job["overwrite"]):
        return {"status": "skipped_exists", "source": str(src), "output": str(dst)}
    arr = _read_float_tif(src)
    arr = _undistort_if_needed(arr, job["meta"], bool(job["use_undistort"]))
    band_key = str(job["band"])
    info = job["transforms"]["bands"].get(band_key)
    if info is None:
        raise RuntimeError(f"No transform for band {band_key}")
    matrix = np.array(info["matrix"], dtype=np.float32)
    h, w = arr.shape
    if job["warp_mode"] == "homography":
        aligned = cv2.warpPerspective(
            arr,
            matrix,
            (w, h),
            flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    else:
        aligned = cv2.warpAffine(
            arr,
            matrix,
            (w, h),
            flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    aligned = aligned.astype(np.float32)
    _write_float_tif(
        dst,
        aligned,
        job["compression"],
        {
            "SOURCE_FILE": str(src),
            "PROCESSING_STAGE": "band_alignment",
            "BAND": str(job["band"]),
            "REFERENCE_BAND": str(info.get("ref_band", "")),
        },
        metadata_source=Path(job["source_original"]),
    )
    return {"status": "written", "source": str(src), "output": str(dst)}


def apply_alignment(config: Config, log: Callable[[str], None] = _log_stdout) -> dict[str, Any]:
    _require_packages()
    transforms_path = config.qc_dir / "alignment_transforms.json"
    if not transforms_path.exists():
        estimate_alignment(config, log)
    transforms = _read_json(transforms_path)
    if transforms.get("status") == "failed" or transforms.get("missing_transform_bands"):
        raise RuntimeError(
            "Alignment transforms are incomplete. "
            "Rerun `4 Estimate Alignment` successfully before applying alignment. "
            f"Missing bands: {transforms.get('missing_transform_bands', [])}"
        )
    if (
        transforms.get("method") != EXPECTED_ALIGNMENT_METHOD
        or transforms.get("undistort_model") != EXPECTED_UNDISTORT_MODEL
    ):
        raise RuntimeError(
            "Alignment transforms were made by an older alignment method. "
            "Rerun `4 Estimate Alignment` before `5 Apply Alignment`."
        )
    zero_sample_bands = [
        band
        for band, info in transforms.get("bands", {}).items()
        if not info.get("is_reference") and int(info.get("sample_count", 0) or 0) <= 0
    ]
    if zero_sample_bands:
        raise RuntimeError(
            "Alignment transforms contain no successful samples for bands: "
            + ", ".join(map(str, zero_sample_bands))
            + ". Rerun `4 Estimate Alignment` successfully before applying alignment."
        )
    band_log_path = config.qc_dir / "band_alignment_log.csv"
    if (
        not config.overwrite
        and band_log_path.exists()
        and transforms_path.stat().st_mtime > band_log_path.stat().st_mtime
    ):
        raise RuntimeError(
            "Alignment transforms are newer than the existing band-alignment outputs. "
            "Turn `Overwrite` on before rerunning `5 Apply Alignment`."
        )
    rows = _load_frame_rows(config)
    metadata = _load_metadata_map(config)
    jobs = []
    for row in rows:
        src_original = str(Path(row["source_path"]))
        jobs.append(
            {
                "src": row["radiometric_path"],
                "dst": row["aligned_path"],
                "source_original": src_original,
                "meta": metadata[src_original],
                "band": int(row["band"]),
                "transforms": transforms,
                "warp_mode": transforms["warp_mode"],
                "use_undistort": transforms.get("use_lens_undistort_for_alignment", True),
                "overwrite": config.overwrite,
                "compression": config.compression,
            }
        )
    log(f"Applying band alignment for {len(jobs)} files with {config.worker_count} workers...")
    return _parallel_map(
        jobs,
        _alignment_worker,
        config.worker_count,
        config.qc_dir / "band_alignment_log.csv",
        config,
        "band_alignment",
        log,
    )


PIPELINE_STAGES = [
    ("scan_qc", "1 Scan/QC", scan_inputs),
    ("metadata", "2 Extract Metadata", extract_metadata),
    ("radiometric", "3 Run Radiometric", run_radiometric),
    ("estimate_alignment", "4 Estimate Alignment", estimate_alignment),
    ("band_alignment", "5 Apply Alignment", apply_alignment),
]


def _stage_output_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file() and item.suffix.lower() in {".tif", ".tiff"})


def validate_stage_quality(config: Config, stage_key: str, result: Any) -> tuple[bool, str]:
    if not isinstance(result, dict):
        return False, "단계 결과가 비어 있거나 형식이 맞지 않습니다."
    if result.get("stopped") or result.get("status") in {"stopped", "failed", "cancelled"}:
        return False, f"단계 상태가 {result.get('status', 'stopped')}입니다."

    if stage_key == "scan_qc":
        if int(result.get("planned_tif_count", 0) or 0) <= 0:
            return False, "처리 예정 영상이 0개입니다."
        if int(result.get("partial_shot_count", 0) or 0) > 0:
            return False, f"10밴드가 완성되지 않은 촬영 묶음 {result.get('partial_shot_count')}개가 있습니다."
        if bool(result.get("disk_risk")):
            return False, "출력 드라이브 용량 위험이 감지됐습니다."
        return True, (
            f"{result.get('selected_shot_count')} shots, "
            f"{result.get('planned_tif_count')} files, disk OK"
        )

    if stage_key == "metadata":
        scan_summary = _read_json(config.qc_dir / "scan_summary.json")
        planned = int(scan_summary.get("planned_tif_count", 0) or 0)
        metadata_json = Path(result.get("metadata_json", config.qc_dir / "metadata.json"))
        metadata_csv = Path(result.get("metadata_csv", config.qc_dir / "metadata.csv"))
        records = int(result.get("records", 0) or 0)
        if records <= 0 and metadata_json.exists():
            records = len(_read_json(metadata_json))
        if not metadata_json.exists() or not metadata_csv.exists():
            return False, "metadata.json 또는 metadata.csv가 없습니다."
        if planned and records != planned:
            return False, f"메타데이터 개수 불일치: {records}/{planned}"
        return True, f"metadata {records} records"

    if stage_key == "radiometric":
        total = int(result.get("total", 0) or 0)
        written = int(result.get("written", 0) or 0)
        skipped = int(result.get("skipped_exists", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        cancelled = int(result.get("cancelled", 0) or 0)
        if failed or cancelled:
            return False, f"failed {failed}, cancelled {cancelled}"
        if total and written + skipped < total:
            return False, f"완료 파일 수 부족: {written + skipped}/{total}"
        output_count = _stage_output_count(config.radiometric_dir)
        if total and output_count < total:
            return False, f"방사보정 출력 수 부족: {output_count}/{total}"
        return True, f"radiometric outputs {output_count}/{total}, failed 0"

    if stage_key == "estimate_alignment":
        transforms_path = Path(result.get("transforms", config.qc_dir / "alignment_transforms.json"))
        if not transforms_path.exists():
            return False, "alignment_transforms.json이 없습니다."
        transforms = _read_json(transforms_path)
        if (
            transforms.get("method") != EXPECTED_ALIGNMENT_METHOD
            or transforms.get("undistort_model") != EXPECTED_UNDISTORT_MODEL
        ):
            return False, "alignment transforms were made by an older method; rerun step 4"
        missing = transforms.get("missing_transform_bands") or []
        if transforms.get("status") != "ok" or missing:
            return False, f"정합 변환 누락: {missing}"
        zero_sample_bands = [
            band
            for band, info in (transforms.get("bands") or {}).items()
            if not info.get("is_reference") and int(info.get("sample_count", 0) or 0) <= 0
        ]
        if zero_sample_bands:
            return False, f"성공 샘플이 없는 밴드: {zero_sample_bands}"
        success = int(transforms.get("successful_pairs", result.get("successful_pairs", 0)) or 0)
        failed = int(transforms.get("failed_pairs", result.get("failed_pairs", 0)) or 0)
        return True, f"alignment transforms OK, success {success}, failed {failed}"

    if stage_key == "band_alignment":
        total = int(result.get("total", 0) or 0)
        written = int(result.get("written", 0) or 0)
        skipped = int(result.get("skipped_exists", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        cancelled = int(result.get("cancelled", 0) or 0)
        if failed or cancelled:
            return False, f"failed {failed}, cancelled {cancelled}"
        if total and written + skipped < total:
            return False, f"완료 파일 수 부족: {written + skipped}/{total}"
        output_count = _stage_output_count(config.aligned_dir)
        if total and output_count < total:
            return False, f"밴드정합 출력 수 부족: {output_count}/{total}"
        transforms_path = config.qc_dir / "alignment_transforms.json"
        band_log_path = config.qc_dir / "band_alignment_log.csv"
        if transforms_path.exists():
            transforms = _read_json(transforms_path)
            if (
                transforms.get("method") != EXPECTED_ALIGNMENT_METHOD
                or transforms.get("undistort_model") != EXPECTED_UNDISTORT_MODEL
            ):
                return False, "band alignment outputs were made from old alignment transforms; rerun steps 4 and 5"
        if transforms_path.exists() and band_log_path.exists() and transforms_path.stat().st_mtime > band_log_path.stat().st_mtime:
            return False, "정합 변환 파일이 밴드정합 산출물보다 최신입니다."
        return True, f"aligned outputs {output_count}/{total}, failed 0"

    return True, "검증 규칙 없음"


def run_pipeline_with_quality_gates(config: Config, log: Callable[[str], None] = _log_stdout) -> dict[str, Any]:
    _clear_stop_flag(config)
    completed: list[str] = []
    results: dict[str, Any] = {}
    for stage_key, label, func in PIPELINE_STAGES:
        if _stop_requested(config):
            message = "Run 1-5 stopped before next stage."
            log(message)
            return {"status": "stopped", "completed": completed, "results": results, "message": message}
        log(f"시작: {label} 단계 시작")
        result = func(config, log)
        results[stage_key] = result
        ok, detail = validate_stage_quality(config, stage_key, result)
        if not ok:
            log(f"검증 실패: {label} | {detail}")
            return {"status": "failed", "failed_stage": stage_key, "completed": completed, "results": results, "message": detail}
        log(f"검증 통과: {label} | {detail}")
        completed.append(stage_key)
    return {"status": "ok", "completed": completed, "results": results}


def _clone_config(config: Config, **updates: Any) -> Config:
    data = config.to_dict()
    data.update(updates)
    return Config(**data)


def _folder_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_benchmark(config: Config, log: Callable[[str], None] = _log_stdout) -> dict[str, Any]:
    """Run a small real-processing benchmark and recommend stable settings."""

    _require_packages()
    _clear_stop_flag(config)
    started = time.time()
    benchmark_shots = max(1, int(config.benchmark_shots))
    max_workers = max(1, min(int(config.benchmark_max_workers), max(1, (os.cpu_count() or 2) - 1)))
    worker_values = list(range(1, max_workers + 1))
    full_file_count = len(_selected_raw_files(_clone_config(config, process_limit_shots=0)))
    full_shot_count = full_file_count // 10 if full_file_count else 0
    benchmark_root = config.qc_dir / "benchmark_runs"
    benchmark_root.mkdir(parents=True, exist_ok=True)
    log(
        f"Benchmark started: {benchmark_shots} shots, workers {worker_values}, "
        f"full dataset {full_shot_count} shots / {full_file_count} files."
    )
    _write_progress(
        config,
        "benchmark",
        0,
        len(worker_values),
        started,
        "running",
        extra={"benchmark_shots": benchmark_shots, "full_file_count": full_file_count},
    )

    rows = []
    for idx, workers in enumerate(worker_values, start=1):
        if _stop_requested(config):
            log("Benchmark stop requested; stopping before next worker value.")
            break
        sub_root = benchmark_root / f"workers_{workers}"
        sub_config = _clone_config(
            config,
            output_root=str(sub_root),
            worker_count=workers,
            process_limit_shots=benchmark_shots,
            alignment_sample_shots=min(max(1, config.alignment_sample_shots), benchmark_shots),
            overwrite=True,
        )
        log(f"Benchmark workers={workers}: scan/metadata/radiometric/alignment...")
        run_started = time.time()
        scan_inputs(sub_config, log)
        meta_started = time.time()
        meta_result = extract_metadata(sub_config, log)
        metadata_elapsed = time.time() - meta_started
        rad_result = run_radiometric(sub_config, log)
        est_started = time.time()
        estimate_alignment(sub_config, log)
        estimate_elapsed = time.time() - est_started
        align_result = apply_alignment(sub_config, log)
        total_elapsed = time.time() - run_started
        benchmark_files = int(rad_result.get("total", benchmark_shots * 10))
        radiometric_rate = float(rad_result.get("rate_files_per_second", 0.0))
        alignment_rate = float(align_result.get("rate_files_per_second", 0.0))
        metadata_rate = benchmark_files / metadata_elapsed if metadata_elapsed > 0 else 0.0
        output_gb = _folder_size_bytes(sub_root) / (1024**3)
        estimated_full_seconds = 0.0
        if metadata_rate > 0:
            estimated_full_seconds += full_file_count / metadata_rate
        if radiometric_rate > 0:
            estimated_full_seconds += full_file_count / radiometric_rate
        if alignment_rate > 0:
            estimated_full_seconds += full_file_count / alignment_rate
        if benchmark_shots > 0:
            target_samples = max(1, int(config.alignment_sample_shots))
            estimated_full_seconds += estimate_elapsed * target_samples / max(1, benchmark_shots)

        row = {
            "workers": workers,
            "benchmark_shots": benchmark_shots,
            "benchmark_files": benchmark_files,
            "metadata_elapsed_seconds": round(metadata_elapsed, 2),
            "radiometric_elapsed_seconds": rad_result.get("elapsed_seconds", ""),
            "alignment_estimate_elapsed_seconds": round(estimate_elapsed, 2),
            "alignment_apply_elapsed_seconds": align_result.get("elapsed_seconds", ""),
            "total_elapsed_seconds": round(total_elapsed, 2),
            "metadata_rate_files_per_second": round(metadata_rate, 3),
            "radiometric_rate_files_per_second": radiometric_rate,
            "alignment_rate_files_per_second": alignment_rate,
            "estimated_full_hours": round(estimated_full_seconds / 3600.0, 2),
            "benchmark_output_gb": round(output_gb, 3),
            "radiometric_failed": rad_result.get("failed", 0),
            "alignment_failed": align_result.get("failed", 0),
            "status": "ok"
            if rad_result.get("failed", 0) == 0 and align_result.get("failed", 0) == 0
            else "has_failures",
            "output_root": str(sub_root),
        }
        rows.append(row)
        _write_progress(
            config,
            "benchmark",
            idx,
            len(worker_values),
            started,
            "running",
            extra={
                "last_workers": workers,
                "last_estimated_full_hours": row["estimated_full_hours"],
                "last_total_elapsed_seconds": row["total_elapsed_seconds"],
            },
        )

    if not rows:
        summary = {"status": "stopped", "message": "No benchmark rows were completed."}
        _write_json(config.qc_dir / "benchmark_summary.json", summary)
        return summary

    ok_rows = [row for row in rows if row["status"] == "ok"]
    if ok_rows:
        def combined_rate(row: dict[str, Any]) -> float:
            rad = float(row["radiometric_rate_files_per_second"] or 0.0)
            align = float(row["alignment_rate_files_per_second"] or 0.0)
            if rad <= 0 or align <= 0:
                return 0.0
            return 1.0 / (1.0 / rad + 1.0 / align)

        best_rate = max(combined_rate(row) for row in ok_rows)
        stable_candidates = [row for row in ok_rows if combined_rate(row) >= best_rate * 0.9]
        recommended = sorted(stable_candidates, key=lambda row: int(row["workers"]))[0]
    else:
        recommended = sorted(rows, key=lambda row: int(row["workers"]))[0]

    free_gb = _drive_free_gb(config.output_path)
    full_input_gb = sum(p.stat().st_size for p in _selected_raw_files(_clone_config(config, process_limit_shots=0))) / (
        1024**3
    )
    conservative_two_stage_gb = full_input_gb * 4.0
    storage_warning = free_gb < conservative_two_stage_gb + 20.0
    summary = {
        "status": "complete" if not _stop_requested(config) else "stopped",
        "recommended_worker_count": int(recommended["workers"]),
        "recommended_alignment_sample_shots": min(max(25, config.alignment_sample_shots), 50),
        "recommended_downscale": config.alignment_downscale,
        "estimated_full_hours_at_recommended": recommended["estimated_full_hours"],
        "free_output_drive_gb": round(free_gb, 2),
        "conservative_two_stage_output_gb": round(conservative_two_stage_gb, 2),
        "storage_warning": storage_warning,
        "benchmark_csv": str(config.qc_dir / "benchmark_summary.csv"),
        "benchmark_json": str(config.qc_dir / "benchmark_summary.json"),
        "rows": rows,
    }
    fieldnames = [
        "workers",
        "benchmark_shots",
        "benchmark_files",
        "metadata_elapsed_seconds",
        "radiometric_elapsed_seconds",
        "alignment_estimate_elapsed_seconds",
        "alignment_apply_elapsed_seconds",
        "total_elapsed_seconds",
        "metadata_rate_files_per_second",
        "radiometric_rate_files_per_second",
        "alignment_rate_files_per_second",
        "estimated_full_hours",
        "benchmark_output_gb",
        "radiometric_failed",
        "alignment_failed",
        "status",
        "output_root",
    ]
    _write_csv(config.qc_dir / "benchmark_summary.csv", rows, fieldnames)
    _write_json(config.qc_dir / "benchmark_summary.json", summary)
    _write_progress(
        config,
        "benchmark",
        len(rows),
        len(worker_values),
        started,
        summary["status"],
        extra={
            "recommended_worker_count": summary["recommended_worker_count"],
            "estimated_full_hours_at_recommended": summary["estimated_full_hours_at_recommended"],
            "storage_warning": storage_warning,
        },
    )
    log(
        "Benchmark complete: "
        f"recommended Workers={summary['recommended_worker_count']}, "
        f"estimated full run={summary['estimated_full_hours_at_recommended']} h."
    )
    if storage_warning:
        log("Warning: conservative storage estimate is close to free output-drive space.")
    return summary


class PreprocessGUI:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title("Nongwon 1st Preprocess")
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.config_path = DEFAULT_CONFIG_PATH
        self.config_data = Config.from_file(self.config_path).to_dict()
        self.vars: dict[str, Any] = {}
        self.running = False
        self.running_stage_key: str | None = None
        self.completed_steps: set[str] = set()
        self.step_buttons: dict[str, Any] = {}
        self.preview_labels: dict[str, Any] = {}
        self.preview_status_vars: dict[str, Any] = {}
        self.preview_photo_images: dict[str, Any] = {}
        self.preview_animation_after_ids: dict[str, Any] = {}
        self.preview_refresh_running = False
        self.stage_order = ["scan_qc", "metadata", "radiometric", "estimate_alignment", "band_alignment"]
        self.saved_completion_signature = ""
        self.result_completion_signature = ""
        self.settings_dirty = False
        self._watching_settings = False
        self._build()
        self.saved_completion_signature = self._completion_signature_from_vars()
        self.result_completion_signature = self.saved_completion_signature
        self._watching_settings = True
        self._install_setting_watchers()
        self._update_step_buttons()
        self._poll_logs()
        self._poll_progress()

    def _build(self) -> None:
        tk = self.tk
        ttk = self.ttk
        self.root.columnconfigure(0, weight=1)
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        row = 0
        path_fields = [
            ("Raw Root", "raw_root", "dir"),
            ("GPS txt", "gps_path", "file"),
            ("Output Root", "output_root", "dir"),
        ]
        for label, key, kind in path_fields:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(self.config_data[key]))
            self.vars[key] = var
            ttk.Entry(frame, textvariable=var, width=92).grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Button(frame, text="Browse", command=lambda k=key, kind=kind: self._browse(k, kind)).grid(
                row=row, column=2, padx=(6, 0), pady=3
            )
            row += 1

        option_frame = ttk.LabelFrame(frame, text="Options", padding=8)
        option_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 6))
        for i in range(12):
            option_frame.columnconfigure(i, weight=1)

        self.vars["worker_count"] = tk.IntVar(value=int(self.config_data["worker_count"]))
        self.vars["process_limit_shots"] = tk.IntVar(value=int(self.config_data["process_limit_shots"]))
        self.vars["benchmark_shots"] = tk.IntVar(value=int(self.config_data.get("benchmark_shots", 5)))
        self.vars["benchmark_max_workers"] = tk.IntVar(value=int(self.config_data.get("benchmark_max_workers", 4)))
        self.vars["alignment_sample_shots"] = tk.IntVar(value=int(self.config_data["alignment_sample_shots"]))
        self.vars["alignment_downscale"] = tk.DoubleVar(value=float(self.config_data["alignment_downscale"]))
        self.vars["overwrite"] = tk.BooleanVar(value=bool(self.config_data["overwrite"]))
        self.vars["numeric_folders_only"] = tk.BooleanVar(
            value=bool(self.config_data.get("numeric_folders_only", True))
        )
        self.vars["use_lens_undistort_for_alignment"] = tk.BooleanVar(
            value=bool(self.config_data["use_lens_undistort_for_alignment"])
        )

        controls = [
            ("Workers", "worker_count"),
            ("Limit shots", "process_limit_shots"),
            ("Bench shots", "benchmark_shots"),
            ("Bench max workers", "benchmark_max_workers"),
            ("Align samples", "alignment_sample_shots"),
            ("Downscale", "alignment_downscale"),
        ]
        for i, (label, key) in enumerate(controls):
            ttk.Label(option_frame, text=label).grid(row=0, column=i * 2, sticky="w", padx=(0, 4))
            ttk.Entry(option_frame, textvariable=self.vars[key], width=8).grid(row=0, column=i * 2 + 1, sticky="w")

        ttk.Checkbutton(option_frame, text="Overwrite", variable=self.vars["overwrite"]).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(
            option_frame,
            text="Numeric folders only",
            variable=self.vars["numeric_folders_only"],
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            option_frame,
            text="Lens undistort before alignment",
            variable=self.vars["use_lens_undistort_for_alignment"],
        ).grid(row=1, column=4, columnspan=4, sticky="w", pady=(8, 0))

        row += 1
        stage_frame = ttk.LabelFrame(frame, text="Step Buttons", padding=8)
        stage_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        stage_buttons = [
            ("scan_qc", "1 Scan/QC", "Scan/QC", scan_inputs),
            ("metadata", "2 Extract Metadata", "Extract Metadata", extract_metadata),
            ("radiometric", "3 Run Radiometric", "Run Radiometric", run_radiometric),
            ("estimate_alignment", "4 Estimate Alignment", "Estimate Alignment", estimate_alignment),
            ("band_alignment", "5 Apply Alignment", "Apply Alignment", apply_alignment),
        ]
        for i, (stage_key, text, run_name, func) in enumerate(stage_buttons):
            button = tk.Button(
                stage_frame,
                text=text,
                width=18,
                relief="raised",
                command=lambda k=stage_key, n=run_name, f=func: self._run_async(n, f, k),
            )
            button.grid(row=0, column=i, padx=3, pady=3)
            self.step_buttons[stage_key] = button

        row += 1
        run_frame = ttk.LabelFrame(frame, text="Run Management", padding=8)
        run_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Button(run_frame, text="Save Settings", command=self.save_config).grid(row=0, column=0, padx=3, pady=3)
        ttk.Button(run_frame, text="Run 1-5 + QC Gate", command=self.run_all).grid(row=0, column=1, padx=3, pady=3)
        ttk.Button(run_frame, text="Benchmark / Auto Tune", command=lambda: self._run_async("Benchmark / Auto Tune", run_benchmark)).grid(
            row=0, column=2, padx=3, pady=3
        )
        ttk.Button(run_frame, text="Clear Stop Flag", command=self.clear_stop).grid(row=0, column=3, padx=3, pady=3)
        ttk.Label(
            run_frame,
            text="Use numbered buttons one by one, or run 1-5 with automatic QC gates.",
        ).grid(row=0, column=4, sticky="w", padx=(10, 0))

        row += 1
        safety_frame = ttk.LabelFrame(frame, text="Safety", padding=8)
        safety_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Button(safety_frame, text="Emergency Stop", command=self.emergency_stop).grid(row=0, column=0, padx=3, pady=3)
        ttk.Label(
            safety_frame,
            text="Stops submitting new files. Files already being written may finish safely.",
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

        row += 1
        progress_frame = ttk.LabelFrame(frame, text="Progress", padding=8)
        progress_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 6))
        progress_frame.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="No stage running")
        self.detail_var = tk.StringVar(value="ETA -, speed -, free space -")
        self.estimate_var = tk.StringVar(value="Stage estimates: run Benchmark / Auto Tune first.")
        ttk.Label(progress_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        ttk.Label(progress_frame, textvariable=self.detail_var).grid(row=1, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(progress_frame, maximum=100.0)
        self.progress_bar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(progress_frame, text="Refresh Progress", command=self.refresh_progress).grid(
            row=0, column=1, rowspan=3, padx=(8, 0)
        )
        ttk.Label(progress_frame, textvariable=self.estimate_var, justify="left", wraplength=980).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        row += 1
        output_tabs = ttk.Notebook(frame)
        output_tabs.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(6, 0))

        log_tab = ttk.Frame(output_tabs, padding=4)
        output_tabs.add(log_tab, text="Log")
        self.log_text = tk.Text(log_tab, height=22, width=120)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)

        for key, title, message in [
            ("before", "Before Alignment", "Run radiometric calibration, then refresh preview."),
            ("after", "After Alignment", "Run band alignment, then refresh preview."),
        ]:
            tab = ttk.Frame(output_tabs, padding=8)
            output_tabs.add(tab, text=title)
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(2, weight=1)

            top = ttk.Frame(tab)
            top.grid(row=0, column=0, sticky="ew")
            top.columnconfigure(0, weight=1)
            status_var = tk.StringVar(value=message)
            self.preview_status_vars[key] = status_var
            ttk.Label(top, textvariable=status_var).grid(row=0, column=0, sticky="w")
            ttk.Button(top, text="Refresh Preview", command=self.refresh_alignment_preview).grid(
                row=0, column=1, padx=(8, 0)
            )

            ttk.Label(
                tab,
                text="Bands 1-10 are played in order for the same IMG group when available.",
            ).grid(row=1, column=0, sticky="w", pady=(4, 6))
            image_label = ttk.Label(tab, anchor="center")
            image_label.grid(row=2, column=0, sticky="nsew")
            self.preview_labels[key] = image_label
        frame.rowconfigure(row, weight=1)

    def _browse(self, key: str, kind: str) -> None:
        from tkinter import filedialog

        if kind == "dir":
            value = filedialog.askdirectory(initialdir=self.vars[key].get() or str(Path.home()))
        else:
            value = filedialog.askopenfilename(initialdir=str(Path(self.vars[key].get()).parent))
        if value:
            self.vars[key].set(value)

    def _install_setting_watchers(self) -> None:
        for var in self.vars.values():
            try:
                var.trace_add("write", lambda *_args: self._settings_changed())
            except Exception:
                pass

    def _completion_payload_from_mapping(self, data: dict[str, Any]) -> dict[str, str]:
        keys = [
            "raw_root",
            "gps_path",
            "output_root",
            "worker_count",
            "process_limit_shots",
            "benchmark_shots",
            "benchmark_max_workers",
            "alignment_sample_shots",
            "alignment_downscale",
            "overwrite",
            "use_lens_undistort_for_alignment",
        ]
        return {key: str(data.get(key, "")) for key in keys}

    def _completion_signature_from_mapping(self, data: dict[str, Any]) -> str:
        payload = self._completion_payload_from_mapping(data)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _completion_signature_from_vars(self) -> str:
        data = dict(self.config_data)
        for key, var in self.vars.items():
            data[key] = var.get()
        return self._completion_signature_from_mapping(data)

    def _settings_changed(self) -> None:
        if not self._watching_settings:
            return
        self.settings_dirty = self._completion_signature_from_vars() != self.saved_completion_signature
        if self.settings_dirty:
            self._set_preview_message(
                "Settings changed. Save settings and rerun the affected step before trusting this preview."
            )
        self._refresh_stage_estimates()
        self._update_step_buttons()

    def _completion_display_enabled(self) -> bool:
        return (
            not self.settings_dirty
            and self._completion_signature_from_vars() == self.result_completion_signature
        )

    def _stage_succeeded(self, result: Any) -> bool:
        if not isinstance(result, dict):
            return True
        if result.get("stopped") or result.get("status") in {"stopped", "failed", "cancelled"}:
            return False
        try:
            return int(result.get("failed", 0) or 0) == 0
        except Exception:
            return False

    def _stage_label(self, stage_key: str | None, fallback: str) -> str:
        labels = {
            "scan_qc": "1 Scan/QC",
            "metadata": "2 Extract Metadata",
            "radiometric": "3 Run Radiometric",
            "estimate_alignment": "4 Estimate Alignment",
            "band_alignment": "5 Apply Alignment",
        }
        return labels.get(stage_key or "", fallback)

    def _status_label(self, status: str) -> str:
        labels = {
            "running": "진행 중",
            "complete": "완료",
            "stopped": "중지",
            "failed": "실패",
        }
        return labels.get(status, status or "-")

    def _stage_result_summary(self, stage_key: str | None, result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        if stage_key == "scan_qc":
            return (
                f"{result.get('selected_shot_count', '-')} shots, "
                f"{result.get('planned_tif_count', '-')} files planned"
            )
        if stage_key == "metadata":
            records = result.get("records")
            if records is None:
                metadata_path = result.get("metadata_json")
                try:
                    records = len(_read_json(Path(metadata_path))) if metadata_path else "-"
                except Exception:
                    records = "-"
            return f"metadata {records} records"
        if stage_key in {"radiometric", "band_alignment"}:
            return (
                f"total {result.get('total', '-')}, "
                f"written {result.get('written', '-')}, "
                f"skipped {result.get('skipped_exists', '-')}, "
                f"failed {result.get('failed', '-')}, "
                f"elapsed {result.get('elapsed_seconds', '-')}s"
            )
        if stage_key == "estimate_alignment":
            return f"transforms saved: {result.get('transforms', '-')}"
        if "recommended_worker_count" in result:
            return (
                f"recommended Workers={result.get('recommended_worker_count')}, "
                f"estimated full run={result.get('estimated_full_hours_at_recommended')}h"
            )
        return ", ".join(f"{key}={value}" for key, value in list(result.items())[:4])

    def _log_stage_finished(self, stage_key: str | None, name: str, result: Any) -> None:
        label = self._stage_label(stage_key, name)
        summary = self._stage_result_summary(stage_key, result)
        suffix = f" | {summary}" if summary else ""
        if isinstance(result, dict) and (result.get("stopped") or result.get("status") == "stopped"):
            self._log(f"중지: {label} 단계 중지{suffix}")
            return
        if not self._stage_succeeded(result):
            self._log(f"주의: {label} 단계 종료, 실패/중지 항목 확인 필요{suffix}")
            return
        self._log(f"완료: {label} 단계 완료{suffix}")

    def _update_step_buttons(self) -> None:
        enabled = self._completion_display_enabled()
        for stage_key, button in self.step_buttons.items():
            if stage_key == self.running_stage_key:
                button.configure(
                    bg="#d8e8ff",
                    fg="#173f73",
                    activebackground="#c8dcf8",
                    relief="raised",
                    state="normal",
                )
            elif enabled and stage_key in self.completed_steps:
                button.configure(
                    bg="#d6d9dd",
                    fg="#575d63",
                    activebackground="#c8ccd1",
                    relief="sunken",
                    state="normal",
                )
            else:
                button.configure(
                    bg="#f4f4f4",
                    fg="#202124",
                    activebackground="#e8e8e8",
                    relief="raised",
                    state="normal",
                )

    def _prepare_step_run(self, config: Config, stage_key: str) -> None:
        run_signature = self._completion_signature_from_mapping(config.to_dict())
        if run_signature != self.result_completion_signature:
            self.completed_steps.clear()
            self.result_completion_signature = run_signature
        self.running = True
        self.running_stage_key = stage_key
        self._update_step_buttons()

    def _set_running_stage(self, stage_key: str | None) -> None:
        self.running_stage_key = stage_key
        self._update_step_buttons()

    def _finish_step_run(self, stage_key: str | None, result: Any = None) -> None:
        self._mark_step_complete_from_result(stage_key, result)
        self.running = False
        self.running_stage_key = None
        self._update_step_buttons()

    def _mark_step_complete_from_result(self, stage_key: str | None, result: Any = None) -> None:
        succeeded = self._stage_succeeded(result)
        if stage_key and succeeded:
            self.completed_steps.add(stage_key)
        self._update_step_buttons()
        if stage_key == "band_alignment" and succeeded:
            self.refresh_alignment_preview(auto=True)

    def _mark_stage_complete_from_progress(self, state: dict[str, Any]) -> None:
        if self.settings_dirty:
            return
        stage = str(state.get("stage", ""))
        status = str(state.get("status", ""))
        if status == "complete" and stage in self.stage_order:
            self.completed_steps.add(stage)
            self._update_step_buttons()

    def _set_preview_message(self, message: str) -> None:
        for key, status_var in self.preview_status_vars.items():
            self._stop_preview_animation(key)
            self.preview_photo_images.pop(key, None)
            label = self.preview_labels.get(key)
            if label:
                label.configure(image="", text=message)
            if key == "before":
                status_var.set(f"Before Alignment: {message}")
            elif key == "after":
                status_var.set(f"After Alignment: {message}")

    def _stop_preview_animation(self, key: str) -> None:
        after_id = self.preview_animation_after_ids.pop(key, None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass

    def _load_gif_frames(self, path: Path) -> list[Any]:
        frames = []
        index = 0
        while True:
            try:
                frame = self.tk.PhotoImage(file=str(path), format=f"gif -index {index}")
            except Exception:
                break
            frames.append(frame)
            index += 1
        return frames

    def _animate_preview(self, key: str, frame_index: int = 0) -> None:
        frames = self.preview_photo_images.get(key)
        label = self.preview_labels.get(key)
        if not frames or not label:
            return
        if not isinstance(frames, list):
            return
        frame = frames[frame_index % len(frames)]
        label.configure(image=frame, text="")
        self.preview_animation_after_ids[key] = self.root.after(
            550,
            lambda: self._animate_preview(key, frame_index + 1),
        )

    def _set_preview_image(self, key: str, path_text: str, fallback: str) -> None:
        label = self.preview_labels[key]
        status_var = self.preview_status_vars[key]
        self._stop_preview_animation(key)
        path = Path(path_text) if path_text else None
        if not path or not path.exists():
            label.configure(image="", text=fallback)
            self.preview_photo_images.pop(key, None)
            status_var.set(fallback)
            return
        if path.suffix.lower() == ".gif":
            frames = self._load_gif_frames(path)
            if not frames:
                label.configure(image="", text=f"Preview GIF was created but could not be displayed:\n{path}")
                self.preview_photo_images.pop(key, None)
                status_var.set("Preview GIF display failed.")
                return
            self.preview_photo_images[key] = frames
            status_var.set(f"{path.name} | playing bands 1-10")
            self._animate_preview(key)
            return
        try:
            photo = self.tk.PhotoImage(file=str(path))
        except Exception as exc:
            label.configure(image="", text=f"Preview image was created but could not be displayed:\n{path}")
            self.preview_photo_images.pop(key, None)
            status_var.set(f"Preview display failed: {exc}")
            return
        self.preview_photo_images[key] = photo
        label.configure(image=photo, text="")
        status_var.set(f"{path.name}")

    def _load_alignment_preview_result(self, result: dict[str, Any]) -> None:
        before_message = str(result.get("before_message") or "Before preview is not ready.")
        after_message = str(result.get("after_message") or "After preview is not ready.")
        self._set_preview_image("before", str(result.get("before") or ""), before_message)
        self._set_preview_image("after", str(result.get("after") or ""), after_message)

    def refresh_alignment_preview(self, auto: bool = False) -> None:
        if self.settings_dirty:
            self._set_preview_message(
                "Settings changed. Save settings and rerun the affected step before refreshing."
            )
            return
        if self.preview_refresh_running:
            if not auto:
                self._log("Alignment preview refresh is already running.")
            return
        try:
            config = self._current_config()
        except Exception as exc:
            self._set_preview_message(f"Check option values first: {exc}")
            return
        self.preview_refresh_running = True
        self._set_preview_message("Building preview sheet...")

        def target() -> None:
            try:
                result = generate_alignment_previews(config)
                self.root.after(0, lambda r=result: self._load_alignment_preview_result(r))
                if not auto:
                    self._log(f"Alignment preview refreshed: {result}")
            except Exception:
                error = traceback.format_exc()
                self.root.after(0, lambda e=error: self._set_preview_message("Preview failed. Check log."))
                if not auto:
                    self._log("Preview ERROR:\n" + error)
            finally:
                self.preview_refresh_running = False

        threading.Thread(target=target, daemon=True).start()

    def _current_config(self) -> Config:
        data = dict(self.config_data)
        for key, var in self.vars.items():
            data[key] = var.get()
        return Config(
            raw_root=data["raw_root"],
            gps_path=data["gps_path"],
            output_root=data["output_root"],
            qc_dir_name=data.get("qc_dir_name", "0. Preprocess_QC"),
            radiometric_dir_name=data.get("radiometric_dir_name", "1. Radiometric_Calibration"),
            aligned_dir_name=data.get("aligned_dir_name", "2. Band_Alignment"),
            numeric_folders_only=bool(data.get("numeric_folders_only", True)),
            overwrite=bool(data["overwrite"]),
            metadata_batch_size=int(data.get("metadata_batch_size", 200)),
            worker_count=int(data["worker_count"]),
            process_limit_shots=int(data["process_limit_shots"]),
            benchmark_shots=int(data.get("benchmark_shots", 5)),
            benchmark_max_workers=int(data.get("benchmark_max_workers", 4)),
            alignment_sample_shots=int(data["alignment_sample_shots"]),
            alignment_downscale=float(data["alignment_downscale"]),
            alignment_warp_mode="affine",
            alignment_pyramid_levels=int(data.get("alignment_pyramid_levels", 2)),
            alignment_gauss_filter_size=int(data.get("alignment_gauss_filter_size", 5)),
            alignment_max_iterations=int(data.get("alignment_max_iterations", 80)),
            alignment_epsilon=float(data.get("alignment_epsilon", 0.00001)),
            alignment_groups="sensor_internal_green",
            use_lens_undistort_for_alignment=bool(data["use_lens_undistort_for_alignment"]),
            output_dtype=data.get("output_dtype", "float32"),
            compression=data.get("compression", "DEFLATE"),
        )

    def save_config(self) -> None:
        config = self._current_config()
        config_dict = config.to_dict()
        new_signature = self._completion_signature_from_mapping(config_dict)
        signature_changed = new_signature != self.saved_completion_signature
        _write_json(self.config_path, config_dict)
        self.config_data = config_dict
        self.saved_completion_signature = new_signature
        self.settings_dirty = False
        if signature_changed and new_signature != self.result_completion_signature:
            self.completed_steps.clear()
            self._log("Settings changed. Step completion marks were reset.")
        self._refresh_stage_estimates(config)
        self._update_step_buttons()
        self._log(f"Saved config: {self.config_path}")

    def emergency_stop(self) -> None:
        config = self._current_config()
        _request_stop(config)
        self._log(
            "Emergency stop requested. New files will stop being submitted; "
            "files already being written may finish first."
        )

    def clear_stop(self) -> None:
        config = self._current_config()
        _clear_stop_flag(config)
        self._log("Stop flag cleared.")

    def _refresh_stage_estimates(self, config: Config | None = None) -> None:
        try:
            config = config or self._current_config()
            estimate = estimate_stage_times(config)
            self.estimate_var.set("\n".join(estimate["lines"]))
        except Exception as exc:
            self.estimate_var.set(f"Stage estimates unavailable: {exc}")

    def refresh_progress(self) -> None:
        try:
            config = self._current_config()
        except Exception as exc:
            self.status_var.set("Check option values")
            self.detail_var.set(str(exc))
            self.progress_bar["value"] = 0
            self._update_step_buttons()
            return
        self._refresh_stage_estimates(config)
        path = _progress_path(config)
        if not path.exists():
            self.status_var.set("No progress file yet")
            self.detail_var.set(f"Free output space {round(_drive_free_gb(config.output_path), 1)} GB")
            self.progress_bar["value"] = 0
            self._update_step_buttons()
            return
        try:
            state = _read_json(path)
            self._mark_stage_complete_from_progress(state)
            percent = float(state.get("percent", 0.0))
            self.progress_bar["value"] = max(0.0, min(100.0, percent))
            stage_key = str(state.get("stage", "-"))
            status_key = str(state.get("status", "-"))
            stage_label = self._stage_label(stage_key, stage_key)
            status_label = self._status_label(status_key)
            self.status_var.set(
                f"{status_label}: {stage_label} 단계 {status_label} | "
                f"{state.get('completed', 0)}/{state.get('total', 0)} ({percent:.2f}%)"
            )
            warning = state.get("warning") or ""
            self.detail_var.set(
                f"ETA {state.get('eta_text', '-')} | "
                f"{state.get('rate_files_per_second', 0)} files/s | "
                f"free {state.get('free_output_drive_gb', '-')} GB"
                + (f" | {warning}" if warning else "")
            )
        except Exception as exc:
            self.status_var.set("Could not read progress")
            self.detail_var.set(str(exc))
            self._update_step_buttons()

    def _log(self, message: str) -> None:
        self.log_queue.put(f"[{_now()}] {message}")

    def _poll_logs(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
        self.root.after(200, self._poll_logs)

    def _poll_progress(self) -> None:
        self.refresh_progress()
        self.root.after(3000, self._poll_progress)

    def _run_async(
        self,
        name: str,
        func: Callable[[Config, Callable[[str], None]], Any],
        stage_key: str | None = None,
    ) -> None:
        if self.running:
            self._log("A stage is already running. Use Emergency Stop first if needed.")
            return
        try:
            config = self._current_config()
        except Exception as exc:
            self._log(f"Check option values before starting: {exc}")
            return
        self.save_config()
        self._prepare_step_run(config, stage_key or "")

        def target() -> None:
            result: Any = None
            try:
                _clear_stop_flag(config)
                self._log(f"시작: {self._stage_label(stage_key, name)} 단계 시작")
                result = func(config, self._log)
                self._log_stage_finished(stage_key, name, result)
                if stage_key:
                    ok, detail = validate_stage_quality(config, stage_key, result)
                    if ok:
                        self._log(f"검증 통과: {self._stage_label(stage_key, name)} | {detail}")
                    else:
                        self._log(f"검증 실패: {self._stage_label(stage_key, name)} | {detail}")
            except Exception:
                self._log("ERROR:\n" + traceback.format_exc())
            finally:
                self.root.after(0, lambda r=result, k=stage_key: self._finish_step_run(k, r))

        threading.Thread(target=target, daemon=True).start()

    def run_all(self) -> None:
        if self.running:
            self._log("A stage is already running. Use Emergency Stop first if needed.")
            return
        try:
            config = self._current_config()
        except Exception as exc:
            self._log(f"Check option values before starting: {exc}")
            return
        self.save_config()
        self._prepare_step_run(config, "scan_qc")

        def target() -> None:
            try:
                _clear_stop_flag(config)
                for stage_key, label, func in PIPELINE_STAGES:
                    self.root.after(0, lambda k=stage_key: self._set_running_stage(k))
                    if _stop_requested(config):
                        self._log("Run All stopped before next stage.")
                        break
                    self._log(f"시작: {label} 단계 시작")
                    result = func(config, self._log)
                    self._log_stage_finished(stage_key, label, result)
                    ok, detail = validate_stage_quality(config, stage_key, result)
                    if ok:
                        self._log(f"검증 통과: {label} | {detail}")
                    else:
                        self._log(f"검증 실패: {label} | {detail}")
                        self._log("일괄 실행 중지: QC 게이트를 통과하지 못해서 다음 단계로 넘어가지 않습니다.")
                        break
                    self.root.after(0, lambda k=stage_key, r=result: self._mark_step_complete_from_result(k, r))
                    if isinstance(result, dict) and result.get("stopped"):
                        self._log("Run All stopped by request.")
                        break
            except Exception:
                self._log("ERROR:\n" + traceback.format_exc())
            finally:
                self.root.after(0, lambda: self._finish_step_run(None, None))

        threading.Thread(target=target, daemon=True).start()

    def mainloop(self) -> None:
        self.root.mainloop()


def main() -> None:
    import argparse
    import multiprocessing

    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Nongwon 1st radiometric and band-alignment GUI")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument(
        "--stage",
        choices=[
            "gui",
            "scan",
            "metadata",
            "radiometric",
            "estimate_alignment",
            "apply_alignment",
            "benchmark",
            "all_qc",
            "all",
        ],
        default="gui",
    )
    args = parser.parse_args()

    globals()["DEFAULT_CONFIG_PATH"] = Path(args.config).resolve()

    if args.stage == "gui":
        PreprocessGUI().mainloop()
        return

    config = Config.from_file(DEFAULT_CONFIG_PATH)
    _clear_stop_flag(config)
    if args.stage == "scan":
        scan_inputs(config)
    elif args.stage == "metadata":
        extract_metadata(config)
    elif args.stage == "radiometric":
        run_radiometric(config)
    elif args.stage == "estimate_alignment":
        estimate_alignment(config)
    elif args.stage == "apply_alignment":
        apply_alignment(config)
    elif args.stage == "benchmark":
        run_benchmark(config)
    elif args.stage == "all_qc":
        result = run_pipeline_with_quality_gates(config)
        if result.get("status") != "ok":
            raise SystemExit(1)
    elif args.stage == "all":
        result = run_pipeline_with_quality_gates(config)
        if result.get("status") != "ok":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
