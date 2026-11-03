from __future__ import annotations

import csv
import json
import math
import os
import shutil
import queue
import re
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from osgeo import gdal, osr
from PIL import Image, ImageDraw, ImageTk

APP_DIR = Path(__file__).resolve().parent
PARENT_DIR = APP_DIR.parent
sys.path.insert(0, str(PARENT_DIR))

try:
    from metashape_metadata import copy_metashape_metadata
except Exception:
    copy_metashape_metadata = None

BANDS = [
    (1, "Blue_444nm"),
    (2, "Blue_475nm"),
    (3, "Green_531nm"),
    (4, "Green_560nm"),
    (5, "Red_650nm"),
    (6, "Red_668nm"),
    (7, "RedEdge_705nm"),
    (8, "RedEdge_717nm"),
    (9, "RedEdge_740nm"),
    (10, "NIR_842nm"),
]

BAND_RE = re.compile(r"^(IMG_\d+)_([0-9]+)\.(tif|tiff)$", re.IGNORECASE)

DEFAULT_CONFIG = {
    "raw_root": "",
    "processed_root": "",
    "cycle_label": "1st",
    "date_tag": "",
    "cycle_dir_name": "",
    "roi_radiance_dir": "",
    "apply_radiance_dir": "",
    "input_radiance_dir": "",
    "output_root": "",
    "reference_mosaic": "",
    "gps_txt": "",
    "dem_tif": "",
    "candidate_band": 6,
    "candidate_max_vza_deg": 45.0,
    "qc_dir_name": r"0. Preprocess_QC\generic_elc",
    "reflectance_dir_name": "03_ELC Ref IMG",
    "elc_shot": "IMG_1554",
    "preview_band": 1,
    "trim_percent": 10.0,
    "worker_count": 3,
    "overwrite": False,
    "compression": "DEFLATE",
    "tarps_csv": str(APP_DIR / "tarp_reflectance_library.csv"),
    "roi_json": "",
}


@dataclass
class TarpSpec:
    key: str
    display: str
    use_for_fit: bool
    measured: list[float]


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    merged = dict(default)
    merged.update(data)
    return merged


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_tarps_csv(path: Path) -> list[TarpSpec]:
    if not path.exists():
        raise FileNotFoundError(f"타프 measured CSV가 없습니다: {path}")
    rows: list[TarpSpec] = []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row.get("key") or "").strip()
            if not key:
                continue
            measured = []
            for band, _ in BANDS:
                token = f"B{band:02d}"
                value = row.get(token, "")
                measured.append(float(value) if str(value).strip() else math.nan)
            use_text = str(row.get("use_for_fit", "1")).strip().lower()
            rows.append(
                TarpSpec(
                    key=key,
                    display=(row.get("display") or key).strip(),
                    use_for_fit=use_text in {"1", "true", "yes", "y", "사용", "fit"},
                    measured=measured,
                )
            )
    if len(rows) < 2:
        raise RuntimeError("ELC 계수 추정을 위해 타프가 최소 2개 이상 필요합니다.")
    return rows


def write_default_files() -> None:
    config_path = APP_DIR / "config_generic_elc.json"
    if not config_path.exists():
        save_json(config_path, DEFAULT_CONFIG)
    library_path = APP_DIR / "tarp_reflectance_library.csv"
    template_path = APP_DIR / "tarps_measured_template.csv"
    if not library_path.exists() and template_path.exists():
        shutil.copyfile(template_path, library_path)


def find_band_file(input_dir: Path, shot: str, band: int) -> Path:
    names = [f"{shot}_{band}.tif", f"{shot}_{band}.tiff"]
    for name in names:
        direct = input_dir / name
        if direct.exists():
            return direct
    for path in input_dir.rglob(f"{shot}_{band}.*"):
        if path.suffix.lower() in {".tif", ".tiff"}:
            return path
    raise FileNotFoundError(f"{shot}_{band}.tif 파일을 찾지 못했습니다: {input_dir}")


def read_band_array(path: Path) -> tuple[np.ndarray, Any, float | None]:
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"영상을 열 수 없습니다: {path}")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float32)
    nodata = band.GetNoDataValue()
    return arr, ds, nodata


def stretch_uint8(arr: np.ndarray) -> np.ndarray:
    valid = np.isfinite(arr)
    if not np.any(valid):
        return np.zeros(arr.shape, dtype=np.uint8)
    lo, hi = np.percentile(arr[valid], [2, 98])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        hi = lo + 1.0
    out = np.clip((arr - lo) / (hi - lo), 0, 1)
    out[~np.isfinite(out)] = 0
    return (out * 255).astype(np.uint8)


def parse_float_cell(value: Any) -> float:
    if value is None:
        return math.nan
    text = str(value).strip()
    if not text:
        return math.nan
    try:
        return float(text)
    except ValueError:
        return math.nan


def inv_geotransform(gt: tuple[float, ...]) -> tuple[float, ...] | None:
    inv = gdal.InvGeoTransform(gt)
    if inv is None:
        return None
    if isinstance(inv, tuple) and len(inv) == 2 and isinstance(inv[0], int):
        return inv[1] if inv[0] else None
    return inv


def pixel_to_geo(gt: tuple[float, ...], col: float, row: float) -> tuple[float, float]:
    x = gt[0] + (col + 0.5) * gt[1] + (row + 0.5) * gt[2]
    y = gt[3] + (col + 0.5) * gt[4] + (row + 0.5) * gt[5]
    return float(x), float(y)


def geo_to_pixel(gt: tuple[float, ...], x: float, y: float) -> tuple[float, float] | None:
    inv = inv_geotransform(gt)
    if inv is None:
        return None
    col = inv[0] + inv[1] * x + inv[2] * y
    row = inv[3] + inv[4] * x + inv[5] * y
    return float(col), float(row)


def srs_from_wkt(wkt: str) -> Any | None:
    if not wkt:
        return None
    srs = osr.SpatialReference()
    if srs.ImportFromWkt(wkt) != 0:
        return None
    try:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    return srs


def projection_is_geographic(wkt: str) -> bool:
    srs = srs_from_wkt(wkt)
    return bool(srs is None or srs.IsGeographic())


def transform_point(x: float, y: float, src_wkt: str, dst_wkt: str) -> tuple[float, float]:
    if not src_wkt or not dst_wkt or src_wkt == dst_wkt:
        return x, y
    src = srs_from_wkt(src_wkt)
    dst = srs_from_wkt(dst_wkt)
    if src is None or dst is None:
        return x, y
    tx = osr.CoordinateTransformation(src, dst)
    out_x, out_y, _ = tx.TransformPoint(float(x), float(y))
    return float(out_x), float(out_y)


def wgs84_wkt() -> str:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    try:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    return srs.ExportToWkt()


def maybe_project_gps_xy(x: float, y: float, target_wkt: str) -> tuple[float, float]:
    if not np.isfinite(x) or not np.isfinite(y):
        return math.nan, math.nan
    gps_looks_lonlat = -180.0 <= x <= 180.0 and -90.0 <= y <= 90.0
    if target_wkt and not projection_is_geographic(target_wkt) and gps_looks_lonlat:
        return transform_point(x, y, wgs84_wkt(), target_wkt)
    return x, y


def delta_xy_m(x0: float, y0: float, x1: float, y1: float, projection_wkt: str) -> tuple[float, float]:
    if projection_is_geographic(projection_wkt):
        mean_lat = math.radians((y0 + y1) / 2.0)
        east_m = (x1 - x0) * 111_320.0 * math.cos(mean_lat)
        north_m = (y1 - y0) * 111_320.0
    else:
        east_m = x1 - x0
        north_m = y1 - y0
    return float(east_m), float(north_m)


def read_gps_estimates(gps_txt: Path, band: int = 6) -> list[dict[str, Any]]:
    if not gps_txt.exists():
        raise FileNotFoundError(f"GPS.txt가 없습니다: {gps_txt}")
    rows: list[dict[str, Any]] = []
    header: list[str] | None = None
    with gps_txt.open("r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.startswith("#Label"):
                header = line.lstrip("#").split("\t")
                continue
            if not line or line.startswith("#") or header is None:
                continue
            parts = line.split("\t")
            if len(parts) < len(header):
                parts += [""] * (len(header) - len(parts))
            row = dict(zip(header, parts))
            label = (row.get("Label") or "").strip()
            match = BAND_RE.match(label)
            if not match:
                continue
            shot, band_text = match.group(1), match.group(2)
            if band > 0 and int(band_text) != int(band):
                continue
            rows.append(
                {
                    "label": label,
                    "shot": shot,
                    "band": int(band_text),
                    "x": parse_float_cell(row.get("X/Longitude")),
                    "y": parse_float_cell(row.get("Y/Latitude")),
                    "z": parse_float_cell(row.get("Z/Altitude")),
                    "x_est": parse_float_cell(row.get("X_est")),
                    "y_est": parse_float_cell(row.get("Y_est")),
                    "z_est": parse_float_cell(row.get("Z_est")),
                    "yaw_est": parse_float_cell(row.get("Yaw_est")),
                    "pitch_est": parse_float_cell(row.get("Pitch_est")),
                    "roll_est": parse_float_cell(row.get("Roll_est")),
                }
            )
    return rows


def sample_dem_z(dem_tif: Path, x: float, y: float, src_wkt: str) -> float:
    if str(dem_tif) in {"", "."} or not dem_tif.exists() or not dem_tif.is_file():
        return math.nan
    ds = gdal.Open(str(dem_tif), gdal.GA_ReadOnly)
    if ds is None:
        return math.nan
    try:
        px = transform_point(x, y, src_wkt, ds.GetProjection())
        pix = geo_to_pixel(ds.GetGeoTransform(), px[0], px[1])
        if pix is None:
            return math.nan
        col, row = int(math.floor(pix[0])), int(math.floor(pix[1]))
        if col < 0 or row < 0 or col >= ds.RasterXSize or row >= ds.RasterYSize:
            return math.nan
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray(col, row, 1, 1)
        if arr is None:
            return math.nan
        value = float(arr[0, 0])
        nodata = band.GetNoDataValue()
        if nodata is not None and value == nodata:
            return math.nan
        if not np.isfinite(value) or value < -1000:
            return math.nan
        return value
    finally:
        ds = None


def raster_point_status(tif_path: Path, x: float, y: float, src_wkt: str) -> tuple[bool, bool, float, float]:
    if not tif_path.exists() or not tif_path.is_file():
        return False, False, math.nan, math.nan
    ds = gdal.Open(str(tif_path), gdal.GA_ReadOnly)
    if ds is None:
        return False, False, math.nan, math.nan
    try:
        px = transform_point(x, y, src_wkt, ds.GetProjection())
        pix = geo_to_pixel(ds.GetGeoTransform(), px[0], px[1])
        if pix is None:
            return False, False, math.nan, math.nan
        col_f, row_f = pix
        col, row = int(math.floor(col_f)), int(math.floor(row_f))
        inside = 0 <= col < ds.RasterXSize and 0 <= row < ds.RasterYSize
        if not inside:
            return False, False, col_f, row_f
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray(col, row, 1, 1)
        valid = False
        if arr is not None:
            value = float(arr[0, 0])
            nodata = band.GetNoDataValue()
            valid = np.isfinite(value) and (nodata is None or value != nodata) and value != 65535.0
        return True, bool(valid), col_f, row_f
    finally:
        ds = None


def make_mosaic_preview(path: Path, max_dim: int = 2200) -> tuple[Image.Image, tuple[float, ...], str, tuple[int, int]]:
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"기준 모자이크를 열 수 없습니다: {path}")
    gt = ds.GetGeoTransform()
    projection = ds.GetProjection()
    full_w, full_h = ds.RasterXSize, ds.RasterYSize
    scale = max(full_w / max_dim, full_h / max_dim, 1.0)
    out_w = max(1, int(round(full_w / scale)))
    out_h = max(1, int(round(full_h / scale)))
    size = (out_w, out_h)
    preview_gt = (gt[0], gt[1] * scale, gt[2] * scale, gt[3], gt[4] * scale, gt[5] * scale)
    if ds.RasterCount >= 6:
        indices = [6, 4, 2]
    elif ds.RasterCount >= 3:
        indices = [3, 2, 1]
    else:
        indices = [1, 1, 1]
    channels = []
    for idx in indices:
        band = ds.GetRasterBand(idx)
        arr = band.ReadAsArray(buf_xsize=out_w, buf_ysize=out_h).astype(np.float32)
        nodata = band.GetNoDataValue()
        if nodata is not None:
            arr[arr == nodata] = np.nan
        arr[arr == 65535.0] = np.nan
        channels.append(stretch_uint8(arr))
    rgb = np.dstack(channels)
    ds = None
    return Image.fromarray(rgb, mode="RGB"), preview_gt, projection, size


def polygon_mask(shape: tuple[int, int], points: list[list[float]], trim_percent: float = 0.0) -> np.ndarray:
    if len(points) < 3:
        return np.zeros(shape, dtype=bool)
    pts = np.asarray(points, dtype=np.float32)
    if trim_percent > 0:
        centroid = pts.mean(axis=0)
        factor = max(0.01, 1.0 - trim_percent / 100.0)
        pts = centroid + (pts - centroid) * factor
    mask_img = Image.new("L", (shape[1], shape[0]), 0)
    ImageDraw.Draw(mask_img).polygon([tuple(map(float, p)) for p in pts], fill=1)
    return np.asarray(mask_img, dtype=np.uint8) > 0


def format_seconds(seconds: float) -> str:
    if not np.isfinite(seconds) or seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def calculate_roi_stats(
    input_dir: Path,
    shot: str,
    tarps: list[TarpSpec],
    polygons: dict[str, list[list[float]]],
    trim_percent: float,
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stats_rows: list[dict[str, Any]] = []
    mean_rows: list[dict[str, Any]] = []
    arrays: dict[int, np.ndarray] = {}
    shape: tuple[int, int] | None = None
    for band, band_name in BANDS:
        arr, ds, nodata = read_band_array(find_band_file(input_dir, shot, band))
        if nodata is not None:
            arr[arr == nodata] = np.nan
        arrays[band] = arr
        shape = arr.shape
        ds = None
    if shape is None:
        raise RuntimeError("통계 계산용 영상을 읽지 못했습니다.")

    for tarp in tarps:
        points = polygons.get(tarp.key) or []
        mask = polygon_mask(shape, points, trim_percent)
        row = {"key": tarp.key, "display": tarp.display, "use_for_fit": int(tarp.use_for_fit)}
        for band, band_name in BANDS:
            arr = arrays[band]
            values = arr[mask]
            values = values[np.isfinite(values)]
            mean = float(np.mean(values)) if values.size else math.nan
            median = float(np.median(values)) if values.size else math.nan
            std = float(np.std(values)) if values.size else math.nan
            row[f"B{band:02d}_mean"] = mean
            stats_rows.append(
                {
                    "key": tarp.key,
                    "display": tarp.display,
                    "band": band,
                    "band_name": band_name,
                    "n": int(values.size),
                    "mean": mean,
                    "median": median,
                    "std": std,
                    "measured_reflectance": tarp.measured[band - 1],
                    "use_for_fit": int(tarp.use_for_fit),
                }
            )
        mean_rows.append(row)

    stats_csv = output_dir / f"{shot}_roi_radiance_stats.csv"
    mean_csv = output_dir / f"{shot}_roi_radiance_means_wide.csv"
    with stats_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(stats_rows[0].keys()))
        writer.writeheader()
        writer.writerows(stats_rows)
    with mean_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(mean_rows[0].keys()))
        writer.writeheader()
        writer.writerows(mean_rows)
    return {"stats_csv": stats_csv, "means_csv": mean_csv}


def build_elc_coefficients(
    stats_csv: Path,
    tarps: list[TarpSpec],
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    by_key_band: dict[tuple[str, int], dict[str, str]] = {}
    with stats_csv.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            by_key_band[(row["key"], int(row["band"]))] = row
    coef_rows: list[dict[str, Any]] = []
    fit_rows: list[dict[str, Any]] = []
    fit_tarps = [t for t in tarps if t.use_for_fit]
    if len(fit_tarps) < 2:
        raise RuntimeError("use_for_fit=1인 타프가 최소 2개 필요합니다.")

    for band, band_name in BANDS:
        x_vals: list[float] = []
        y_vals: list[float] = []
        labels: list[str] = []
        for tarp in fit_tarps:
            row = by_key_band.get((tarp.key, band))
            if not row:
                continue
            x = float(row["mean"])
            y = float(tarp.measured[band - 1])
            if np.isfinite(x) and np.isfinite(y):
                x_vals.append(x)
                y_vals.append(y)
                labels.append(tarp.key)
        if len(x_vals) < 2:
            a = b = rmse = bias = r2 = math.nan
        else:
            x_arr = np.asarray(x_vals, dtype=np.float64)
            y_arr = np.asarray(y_vals, dtype=np.float64)
            design = np.vstack([y_arr, np.ones_like(y_arr)]).T
            if np.linalg.matrix_rank(design) < 2:
                raise ValueError("ELC requires distinct measured reflectances")
            a, b = np.linalg.lstsq(design, x_arr, rcond=None)[0]
            if not np.isfinite(a) or a <= 0:
                raise ValueError("ELC forward slope must be positive")
            pred = (x_arr - b) / a
            resid = pred - y_arr
            rmse = float(np.sqrt(np.mean(resid**2)))
            bias = float(np.mean(resid))
            ss_res = float(np.sum(resid**2))
            ss_tot = float(np.sum((y_arr - y_arr.mean()) ** 2))
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else math.nan
            for label, x, y, p, e in zip(labels, x_arr, y_arr, pred, resid):
                fit_rows.append(
                    {
                        "band": band,
                        "band_name": band_name,
                        "key": label,
                        "radiance_mean": float(x),
                        "measured_reflectance": float(y),
                        "predicted_reflectance": float(p),
                        "residual": float(e),
                    }
                )
        coef_rows.append(
            {
                "band": band,
                "band_name": band_name,
                "a": float(a),
                "b": float(b),
                "coefficient_convention": "L=a*rho+b",
                "fit_tarp_count": len(x_vals),
                "fit_tarps": ";".join(labels),
                "rmse_reflectance": float(rmse),
                "bias_reflectance": float(bias),
                "r2": float(r2),
            }
        )

    coef_csv = output_dir / "elc_coefficients.csv"
    fit_csv = output_dir / "elc_fit_residuals.csv"
    with coef_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(coef_rows[0].keys()))
        writer.writeheader()
        writer.writerows(coef_rows)
    with fit_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(fit_rows[0].keys()) if fit_rows else ["empty"])
        writer.writeheader()
        if fit_rows:
            writer.writerows(fit_rows)
    return {"coefficients_csv": coef_csv, "fit_residuals_csv": fit_csv}


def load_coefficients(path: Path) -> dict[int, tuple[float, float]]:
    coefficients: dict[int, tuple[float, float]] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            band = int(row["band"])
            a = float(row["a"])
            b = float(row["b"])
            convention = row.get("coefficient_convention", "").strip()
            if convention not in ("L=a*rho+b", "rho=a*L+b"):
                raise ValueError(f"ELC CSV band {band}: coefficient_convention required; regenerate or explicitly identify legacy coefficients")
            if not np.isfinite(a) or not np.isfinite(b) or a <= 0:
                raise ValueError(f"ELC CSV band {band}: invalid coefficients")
            if band in coefficients:
                raise ValueError(f"ELC CSV duplicate band {band}")
            if convention == "rho=a*L+b":
                a, b = 1.0 / a, -b / a
            coefficients[band] = (a, b)
    if not coefficients:
        raise ValueError("ELC coefficient CSV is empty")
    return coefficients


def write_float_tif_like(
    src_path: Path,
    dst_path: Path,
    arr: np.ndarray,
    compression: str,
    nodata: float | None,
) -> None:
    src = gdal.Open(str(src_path), gdal.GA_ReadOnly)
    if src is None:
        raise RuntimeError(f"원본 영상을 열 수 없습니다: {src_path}")
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
        raise RuntimeError(f"출력 영상을 만들 수 없습니다: {tmp}")
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
    if copy_metashape_metadata is not None:
        try:
            copy_metashape_metadata(src_path, dst_path)
        except Exception:
            pass


def apply_one_job(job: dict[str, Any]) -> dict[str, Any]:
    src_path = Path(job["src"])
    rel = Path(job["rel"])
    band = int(job["band"])
    coefficients = job["coefficients"]
    outputs = job["outputs"]
    overwrite = bool(job["overwrite"])
    compression = str(job["compression"])
    a, b = coefficients[str(band)]
    ds = gdal.Open(str(src_path), gdal.GA_ReadOnly)
    if ds is None:
        return {"status": "failed", "src": str(src_path), "error": "cannot open source"}
    rb = ds.GetRasterBand(1)
    nodata = rb.GetNoDataValue()
    arr = rb.ReadAsArray().astype(np.float32)
    invalid = ~np.isfinite(arr)
    if nodata is not None:
        invalid |= arr == nodata
    if not np.isfinite(a) or not np.isfinite(b) or a <= 0:
        raise ValueError("ELC forward coefficients must be finite with positive slope")
    out = (arr - np.float32(b)) / np.float32(a)
    out[invalid] = np.nan if nodata is None else np.float32(nodata)
    written: list[str] = []
    for mode, root in outputs.items():
        dst = Path(root) / rel
        if dst.exists() and not overwrite:
            continue
        mode_arr = out
        if mode == "clipped":
            mode_arr = np.array(out, copy=True)
            valid = np.isfinite(mode_arr)
            if nodata is not None:
                valid &= mode_arr != nodata
            mode_arr[valid] = np.clip(mode_arr[valid], 0, 1)
        write_float_tif_like(src_path, dst, mode_arr, compression, nodata)
        written.append(str(dst))
    ds = None
    return {"status": "ok", "src": str(src_path), "written": written}


class GenericElcGUI:
    def __init__(self, root: Any) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.root.title("Generic Tarp ROI + ELC")
        self.root.geometry("1420x900")

        write_default_files()
        self.config_path = APP_DIR / "config_generic_elc.json"
        self.state_path = APP_DIR / "generic_elc_state.json"
        self.config = load_json(self.config_path, DEFAULT_CONFIG)
        self.state = load_json(self.state_path, {"cycles": {}, "last_cycle_key": ""})
        self.tarps: list[TarpSpec] = []
        self.polygons: dict[str, list[list[float]]] = {}
        self.preview_arr: np.ndarray | None = None
        self.preview_image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.preview_size = (1, 1)
        self.tarp_points_geo: dict[str, dict[str, float]] = {}
        self.candidate_rows: list[dict[str, Any]] = []
        self.candidate_window: Any | None = None
        self.mosaic_canvas: Any | None = None
        self.mosaic_image: Image.Image | None = None
        self.mosaic_photo: ImageTk.PhotoImage | None = None
        self.mosaic_gt: tuple[float, ...] | None = None
        self.mosaic_projection = ""
        self.mosaic_size = (1, 1)
        self.mosaic_zoom = 1.0
        self.mosaic_pan_x = 20.0
        self.mosaic_pan_y = 20.0
        self.mosaic_drag_last: tuple[float, float] | None = None
        self.zoom = 1.0
        self.pan_x = 20.0
        self.pan_y = 20.0
        self.mode = tk.StringVar(value="draw")
        self.current_tarp = tk.StringVar(value="")
        self.drag_vertex: tuple[str, int] | None = None
        self.drag_pan_last: tuple[float, float] | None = None
        self.worker_queue: queue.Queue[Any] = queue.Queue()
        self.stop_requested = False
        self.active_cycle_key = ""
        self.cycle_label_to_dir_name: dict[str, str] = {}
        self.progress_window: Any | None = None
        self.progress_log_text: Any | None = None
        self.stage_defs = [
            ("paths", "경로"),
            ("preview", "Preview"),
            ("candidates", "후보 영상"),
            ("roi", "ROI"),
            ("stats", "ROI 평균"),
            ("coeff", "ELC 계수"),
            ("apply", "반사도 적용"),
        ]
        self.stage_text_vars: dict[str, Any] = {}
        self.stage_progress_vars: dict[str, Any] = {}
        self.stage_status: dict[str, dict[str, Any]] = {
            key: {"status": "대기", "progress": 0.0, "message": ""} for key, _ in self.stage_defs
        }

        self._build_ui()
        self._load_tarps()
        self._load_roi_if_exists()
        self.load_cycle_state()
        self.root.after(200, self._drain_queue)

    def _build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        settings = ttk.LabelFrame(main, text="경로/설정", padding=8)
        settings.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(4, weight=1)

        self.vars: dict[str, Any] = {
            "raw_root": tk.StringVar(value=self.config.get("raw_root", DEFAULT_CONFIG["raw_root"])),
            "processed_root": tk.StringVar(value=self.config.get("processed_root", DEFAULT_CONFIG["processed_root"])),
            "cycle_label": tk.StringVar(value=self.config.get("cycle_label", DEFAULT_CONFIG["cycle_label"])),
            "date_tag": tk.StringVar(value=self.config.get("date_tag", DEFAULT_CONFIG["date_tag"])),
            "cycle_dir_name": tk.StringVar(value=self.config.get("cycle_dir_name", DEFAULT_CONFIG["cycle_dir_name"])),
            "roi_radiance_dir": tk.StringVar(value=self.config.get("roi_radiance_dir", self.config.get("input_radiance_dir", ""))),
            "apply_radiance_dir": tk.StringVar(value=self.config.get("apply_radiance_dir", self.config.get("input_radiance_dir", ""))),
            "output_root": tk.StringVar(value=self.config["output_root"]),
            "reference_mosaic": tk.StringVar(value=self.config.get("reference_mosaic", DEFAULT_CONFIG["reference_mosaic"])),
            "gps_txt": tk.StringVar(value=self.config.get("gps_txt", DEFAULT_CONFIG["gps_txt"])),
            "dem_tif": tk.StringVar(value=self.config.get("dem_tif", DEFAULT_CONFIG["dem_tif"])),
            "candidate_band": tk.IntVar(value=int(self.config.get("candidate_band", DEFAULT_CONFIG["candidate_band"]))),
            "candidate_max_vza_deg": tk.DoubleVar(value=float(self.config.get("candidate_max_vza_deg", DEFAULT_CONFIG["candidate_max_vza_deg"]))),
            "elc_shot": tk.StringVar(value=self.config["elc_shot"]),
            "preview_band": tk.IntVar(value=int(self.config["preview_band"])),
            "trim_percent": tk.DoubleVar(value=float(self.config["trim_percent"])),
            "worker_count": tk.IntVar(value=int(self.config["worker_count"])),
            "overwrite": tk.BooleanVar(value=bool(self.config["overwrite"])),
            "tarps_csv": tk.StringVar(value=self.config["tarps_csv"]),
        }
        self.vars["cycle_label"].trace_add("write", lambda *_: self.sync_cycle_dir_name())

        self._path_row(settings, 0, "Raw data root", "raw_root")
        self._path_row(settings, 1, "Processed root", "processed_root")
        ttk.Label(settings, text="회차").grid(row=2, column=0, sticky="w", pady=3)
        self.cycle_combo = ttk.Combobox(settings, textvariable=self.vars["cycle_label"], width=10, state="normal")
        self.cycle_combo.grid(row=2, column=1, sticky="w", pady=3)
        self.cycle_combo.bind("<<ComboboxSelected>>", lambda event: self.on_cycle_selected())
        ttk.Label(settings, text="날짜").grid(row=2, column=2, sticky="w", padx=(10, 2))
        ttk.Entry(settings, textvariable=self.vars["date_tag"], width=10).grid(row=2, column=3, sticky="w")
        ttk.Label(settings, text="회차 폴더").grid(row=2, column=4, sticky="e", padx=(10, 2))
        ttk.Entry(settings, textvariable=self.vars["cycle_dir_name"], width=16).grid(row=2, column=5, sticky="w")
        ttk.Button(settings, text="회차 검색", command=lambda: self.scan_cycle_folders(auto_apply=True)).grid(row=2, column=6, sticky="ew", padx=(10, 0))
        ttk.Button(settings, text="경로 적용", command=self.apply_workflow_template).grid(row=2, column=7, sticky="ew", padx=(6, 0))
        ttk.Button(settings, text="폴더 검사", command=self.inspect_workflow_paths).grid(row=2, column=8, sticky="ew", padx=(6, 0))
        ttk.Button(settings, text="폴더 생성", command=self.create_workflow_dirs).grid(row=2, column=9, sticky="ew", padx=(6, 0))
        ttk.Button(settings, text="원본 후보 스캔", command=self.scan_raw_candidates).grid(row=2, column=10, sticky="ew", padx=(6, 0))

        self._path_row(settings, 3, "ROI/계수 영상(정합)", "roi_radiance_dir")
        self._path_row(settings, 4, "ELC 적용 대상(방사보정)", "apply_radiance_dir")
        self._path_row(settings, 5, "출력 Root", "output_root")
        self._path_row(settings, 6, "기준 모자이크", "reference_mosaic", file_mode=True)
        self._path_row(settings, 7, "GPS.txt", "gps_txt", file_mode=True)
        self._path_row(settings, 8, "DEM.tif", "dem_tif", file_mode=True)
        self._path_row(settings, 9, "타프 measured CSV", "tarps_csv", file_mode=True)
        ttk.Label(settings, text="ELC 기준 영상").grid(row=10, column=0, sticky="w", pady=3)
        ttk.Entry(settings, textvariable=self.vars["elc_shot"], width=16).grid(row=10, column=1, sticky="w", pady=3)
        ttk.Label(settings, text="Preview Band").grid(row=10, column=2, sticky="w", padx=(10, 2))
        ttk.Combobox(
            settings,
            textvariable=self.vars["preview_band"],
            values=[band for band, _ in BANDS],
            width=8,
            state="readonly",
        ).grid(row=10, column=3, sticky="w")
        ttk.Label(settings, text="후보 Band").grid(row=10, column=4, sticky="e", padx=(10, 2))
        ttk.Combobox(
            settings,
            textvariable=self.vars["candidate_band"],
            values=[band for band, _ in BANDS],
            width=6,
            state="readonly",
        ).grid(row=10, column=5, sticky="w")
        ttk.Label(settings, text="후보 max VZA").grid(row=10, column=6, sticky="e", padx=(10, 2))
        ttk.Entry(settings, textvariable=self.vars["candidate_max_vza_deg"], width=6).grid(row=10, column=7, sticky="w")
        ttk.Label(settings, text="Trim %").grid(row=10, column=8, sticky="e", padx=(10, 2))
        ttk.Entry(settings, textvariable=self.vars["trim_percent"], width=7).grid(row=10, column=9, sticky="w")
        ttk.Label(settings, text="Workers").grid(row=10, column=10, sticky="e", padx=(10, 2))
        ttk.Entry(settings, textvariable=self.vars["worker_count"], width=5).grid(row=10, column=11, sticky="w")
        ttk.Checkbutton(settings, text="Overwrite", variable=self.vars["overwrite"]).grid(row=10, column=12, sticky="w", padx=8)

        left = ttk.Frame(main)
        left.grid(row=1, column=0, sticky="ns", padx=(0, 8))
        left.rowconfigure(5, weight=1)

        actions = ttk.LabelFrame(left, text="처리 버튼", padding=8)
        actions.grid(row=0, column=0, sticky="ew")
        ttk.Button(actions, text="1 설정 저장", command=self.save_config).grid(row=0, column=0, sticky="ew", pady=2)
        ttk.Button(actions, text="2 타프 위치/후보 영상", command=self.open_candidate_window).grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Button(actions, text="3 ROI Preview 로드", command=self.load_preview).grid(row=2, column=0, sticky="ew", pady=2)
        ttk.Button(actions, text="4 ROI 저장", command=self.save_roi).grid(row=3, column=0, sticky="ew", pady=2)
        ttk.Button(actions, text="5 ROI 평균 계산", command=self.run_roi_stats).grid(row=4, column=0, sticky="ew", pady=2)
        ttk.Button(actions, text="6 ELC 계수 추정", command=self.run_coefficients).grid(row=5, column=0, sticky="ew", pady=2)
        ttk.Button(actions, text="7 전체 반사도 적용", command=self.run_apply_all).grid(row=6, column=0, sticky="ew", pady=2)
        ttk.Button(actions, text="8 ROI 평균+계수+적용", command=self.run_full_pipeline).grid(row=7, column=0, sticky="ew", pady=2)
        ttk.Button(actions, text="진행상황 창", command=self.open_progress_window).grid(row=8, column=0, sticky="ew", pady=(10, 2))
        ttk.Button(actions, text="긴급 정지", command=self.request_stop).grid(row=9, column=0, sticky="ew", pady=2)

        roi_box = ttk.LabelFrame(left, text="ROI 편집", padding=8)
        roi_box.grid(row=1, column=0, sticky="ew", pady=8)
        ttk.Label(roi_box, text="타프").grid(row=0, column=0, sticky="w")
        self.tarp_combo = ttk.Combobox(roi_box, textvariable=self.current_tarp, width=25, state="readonly")
        self.tarp_combo.grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Radiobutton(roi_box, text="점 찍기/추가", value="draw", variable=self.mode).grid(row=2, column=0, sticky="w")
        ttk.Radiobutton(roi_box, text="점 수정", value="edit", variable=self.mode).grid(row=3, column=0, sticky="w")
        ttk.Radiobutton(roi_box, text="이동", value="pan", variable=self.mode).grid(row=4, column=0, sticky="w")
        ttk.Button(roi_box, text="마지막 점 취소", command=self.undo_point).grid(row=5, column=0, sticky="ew", pady=(8, 2))
        ttk.Button(roi_box, text="선택 ROI 삭제", command=self.clear_selected_roi).grid(row=6, column=0, sticky="ew", pady=2)
        ttk.Button(roi_box, text="타프 CSV 열기", command=self.open_tarps_csv).grid(row=7, column=0, sticky="ew", pady=(8, 2))
        ttk.Button(roi_box, text="새 타프 추가", command=self.add_tarp_dialog).grid(row=8, column=0, sticky="ew", pady=2)
        ttk.Button(roi_box, text="Tarps CSV 다시 읽기", command=self._load_tarps).grid(row=9, column=0, sticky="ew", pady=2)

        fit_box = ttk.LabelFrame(left, text="ELC 사용 타프", padding=8)
        fit_box.grid(row=2, column=0, sticky="ew")
        self.fit_list = tk.Listbox(fit_box, width=32, height=9)
        self.fit_list.grid(row=0, column=0, sticky="ew")
        ttk.Button(fit_box, text="선택 타프 사용/해제", command=self.toggle_fit_tarp).grid(row=1, column=0, sticky="ew", pady=4)

        candidate_box = ttk.LabelFrame(left, text="타프 후보 영상", padding=8)
        candidate_box.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.candidate_list = tk.Listbox(candidate_box, width=42, height=8)
        self.candidate_list.grid(row=0, column=0, sticky="ew")
        ttk.Button(candidate_box, text="선택 후보를 ELC 기준 영상으로", command=self.use_selected_candidate_as_preview).grid(row=1, column=0, sticky="ew", pady=(4, 2))
        ttk.Button(candidate_box, text="후보 CSV 열기", command=self.open_candidate_csv).grid(row=2, column=0, sticky="ew", pady=2)

        log_box = ttk.LabelFrame(left, text="진행 로그", padding=8)
        log_box.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        self.log_text = tk.Text(log_box, width=42, height=18)
        self.log_text.pack(fill="both", expand=True)

        canvas_frame = ttk.Frame(main)
        canvas_frame.grid(row=1, column=1, sticky="nsew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(canvas_frame, background="#171717", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Button-1>", self.on_left_down)
        self.canvas.bind("<B1-Motion>", self.on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_up)
        self.canvas.bind("<Button-2>", self.on_middle_down)
        self.canvas.bind("<B2-Motion>", self.on_middle_drag)
        self.canvas.bind("<ButtonRelease-2>", self.on_middle_up)
        self.canvas.bind("<Button-3>", self.on_right_down)
        self.canvas.bind("<B3-Motion>", self.on_right_drag)
        self.canvas.bind("<ButtonRelease-3>", self.on_right_up)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Double-Button-1>", lambda event: self.fit_preview_to_canvas(redraw=True))

    def _path_row(self, parent: Any, row: int, label: str, key: str, file_mode: bool = False) -> None:
        ttk = self.ttk
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=self.vars[key]).grid(row=row, column=1, columnspan=8, sticky="ew", pady=3)
        command = (lambda k=key: self.browse_file(k)) if file_mode else (lambda k=key: self.browse_dir(k))
        ttk.Button(parent, text="찾기", command=command).grid(row=row, column=9, columnspan=2, sticky="ew", padx=(6, 0))

    def inferred_cycle_dir_name(self) -> str:
        cycle = self.vars["cycle_label"].get().strip() or "1st"
        return cycle

    def sync_cycle_dir_name(self) -> None:
        current = self.vars["cycle_dir_name"].get().strip()
        if not current or re.fullmatch(r"\d+(st|nd|rd|th)(_UAV)?", current, flags=re.IGNORECASE):
            self.vars["cycle_dir_name"].set(self.inferred_cycle_dir_name())

    def extract_cycle_label(self, name: str) -> str | None:
        match = re.search(r"(\d+(?:st|nd|rd|th))", name, flags=re.IGNORECASE)
        return match.group(1) if match else None

    def cycle_sort_key(self, cycle: str) -> tuple[int, str]:
        match = re.match(r"(\d+)", cycle)
        return (int(match.group(1)) if match else 9999, cycle.lower())

    def scan_cycle_folders(self, auto_apply: bool = False) -> None:
        from tkinter import messagebox

        processed_root = Path(self.vars["processed_root"].get())
        if not processed_root.exists():
            messagebox.showerror("회차 검색 실패", f"Processed root가 없습니다:\n{processed_root}")
            return
        found: dict[str, list[Path]] = {}
        root_cycle = self.extract_cycle_label(processed_root.name)
        if root_cycle:
            found.setdefault(root_cycle, []).append(processed_root)
        for child in processed_root.iterdir():
            if not child.is_dir():
                continue
            cycle = self.extract_cycle_label(child.name)
            if cycle:
                found.setdefault(cycle, []).append(child)
        self.cycle_label_to_dir_name.clear()
        labels = sorted(found, key=self.cycle_sort_key)
        for label in labels:
            candidates = sorted(
                found[label],
                key=lambda p: (
                    0 if p.name.lower() == label.lower() else 1,
                    0 if p.name.lower() == f"{label.lower()}_uav" else 1,
                    len(p.name),
                    p.name.lower(),
                ),
            )
            self.cycle_label_to_dir_name[label] = candidates[0].name
        self.cycle_combo["values"] = labels
        self.log(f"회차 검색: {len(labels)}개 / {', '.join(labels) if labels else '없음'}")
        if not labels:
            messagebox.showwarning("회차 검색", "폴더명에 1st/2nd 형식이 들어간 회차 폴더를 찾지 못했습니다.")
            return
        current = self.vars["cycle_label"].get().strip()
        if current not in labels:
            self.vars["cycle_label"].set(labels[0])
        self.on_cycle_selected(auto_apply=auto_apply)

    def on_cycle_selected(self, auto_apply: bool = True) -> None:
        cycle = self.vars["cycle_label"].get().strip()
        if cycle in self.cycle_label_to_dir_name:
            self.vars["cycle_dir_name"].set(self.cycle_label_to_dir_name[cycle])
        if auto_apply:
            self.apply_workflow_template(load_saved=True)

    def cycle_dir_candidates(self, processed_root: Path, cycle: str) -> list[Path]:
        explicit = self.vars["cycle_dir_name"].get().strip()
        names = []
        if explicit and cycle.lower() in explicit.lower():
            names.append(explicit)
        names.extend([cycle, f"{cycle}_UAV", f"{cycle}_uav"])
        unique_names = []
        for name in names:
            if name and name not in unique_names:
                unique_names.append(name)
        candidates = [processed_root / name for name in unique_names]
        if cycle.lower() in processed_root.name.lower():
            candidates.insert(0, processed_root)
        if processed_root.exists():
            matched_children = []
            for child in processed_root.iterdir():
                if child.is_dir() and cycle.lower() in child.name.lower():
                    matched_children.append(child)
            matched_children.sort(key=lambda p: (0 if p.name.lower() == cycle.lower() else 1, len(p.name), p.name.lower()))
            candidates.extend(matched_children)
        unique_candidates = []
        seen = set()
        for path in candidates:
            key = str(path).lower()
            if key not in seen:
                unique_candidates.append(path)
                seen.add(key)
        return unique_candidates

    def resolve_cycle_dir(self) -> Path:
        processed_root = Path(self.vars["processed_root"].get())
        cycle = self.vars["cycle_label"].get().strip() or "1st"
        candidates = self.cycle_dir_candidates(processed_root, cycle)
        for path in candidates:
            if path.exists():
                return path
        return candidates[0]

    def first_existing_path(self, candidates: list[Path]) -> Path | None:
        for path in candidates:
            if path.exists():
                return path
        return None

    def infer_elc_container(self, cycle_dir: Path) -> Path:
        candidates = [
            cycle_dir / "02_ELC_Ref",
            cycle_dir / "2. Tarp_Ref",
            cycle_dir / "Tarp_Ref",
            cycle_dir,
        ]
        existing = self.first_existing_path(candidates)
        return existing or candidates[0]

    def infer_panel_container(self, cycle_dir: Path) -> Path:
        candidates = [
            cycle_dir / "01_panel_Ref",
            cycle_dir / "1. Panel_Ref",
            cycle_dir / "Panel_Ref",
        ]
        existing = self.first_existing_path(candidates)
        return existing or candidates[0]

    def infer_radiometric_dir(self, elc_dir: Path) -> Path:
        candidates = [
            elc_dir / "01_radiometric calibration IMG",
            elc_dir / "1. Radiometric_Calibration",
            elc_dir / "Radiometric_Calibration",
            elc_dir / "radiometric calibration IMG",
        ]
        existing = self.first_existing_path(candidates)
        return existing or candidates[0]

    def infer_alignment_dir(self, elc_dir: Path, radiometric_dir: Path) -> Path:
        candidates = [
            elc_dir / "02_ELC ROI_Band Aligned IMG",
            elc_dir / "2. Band_Alignment",
            elc_dir / "Band_Alignment",
            elc_dir / "ELC ROI_Band Aligned IMG",
        ]
        existing = self.first_existing_path(candidates)
        return existing or radiometric_dir

    def first_existing_glob(self, roots: list[Path], patterns: list[str]) -> Path | None:
        for root in roots:
            if not root.exists():
                continue
            for pattern in patterns:
                matches = sorted(root.glob(pattern), key=lambda p: (len(p.name), p.name.lower()))
                for path in matches:
                    if path.is_file():
                        return path
        return None

    def infer_reference_mosaic(self, cycle_dir: Path, panel_dir: Path, elc_dir: Path, paths: dict[str, Path]) -> Path:
        direct = self.first_existing_path([paths["panel_mosaic"], paths["elc_mosaic"]])
        if direct:
            return direct
        found = self.first_existing_glob(
            [panel_dir, cycle_dir, elc_dir],
            ["*mosaic*.tif", "*mosaic*.tiff", "*Mosaic*.tif", "*Mosaic*.tiff", "*ortho*.tif", "*ortho*.tiff"],
        )
        return found or paths["panel_mosaic"]

    def infer_gps_txt(self, cycle_dir: Path, panel_dir: Path, elc_dir: Path, paths: dict[str, Path]) -> Path:
        direct = self.first_existing_path(
            [
                paths["elc_gps"],
                paths["panel_gps"],
                cycle_dir / "GPS.txt",
                cycle_dir / f"{self.vars['cycle_label'].get().strip()}_GPS.txt",
                elc_dir / "GPS.txt",
                panel_dir / "GPS.txt",
            ]
        )
        if direct:
            return direct
        found = self.first_existing_glob([elc_dir, panel_dir, cycle_dir], ["*GPS*.txt", "*gps*.txt"])
        return found or paths["elc_gps"]

    def infer_dem_tif(self, cycle_dir: Path, panel_dir: Path, elc_dir: Path, paths: dict[str, Path]) -> Path:
        direct = self.first_existing_path(
            [
                paths["panel_dem"],
                panel_dir / "DEM.tif",
                panel_dir / "DEM.tiff",
                cycle_dir / "DEM.tif",
                cycle_dir / "DEM.tiff",
                paths["elc_dem"],
                elc_dir / "DEM.tif",
                Path(self.vars["processed_root"].get()) / "DEM.tif",
            ]
        )
        if direct:
            return direct
        found = self.first_existing_glob([panel_dir, cycle_dir, elc_dir, Path(self.vars["processed_root"].get())], ["*DEM*.tif", "*DEM*.tiff"])
        return found or paths["panel_dem"]

    def workflow_paths(self) -> dict[str, Path]:
        raw_root = Path(self.vars["raw_root"].get())
        processed_root = Path(self.vars["processed_root"].get())
        cycle = self.vars["cycle_label"].get().strip() or "1st"
        date_tag = self.vars["date_tag"].get().strip() or "yymmdd"
        cycle_dir = self.resolve_cycle_dir()
        panel_dir = self.infer_panel_container(cycle_dir)
        elc_dir = self.infer_elc_container(cycle_dir)
        radiometric_dir = self.infer_radiometric_dir(elc_dir)
        alignment_dir = self.infer_alignment_dir(elc_dir, radiometric_dir)
        paths = {
            "raw_root": raw_root,
            "raw_ramses": raw_root / "00_RAMSES",
            "raw_re": raw_root / "01_RE",
            "raw_rgb": raw_root / "02_RGB",
            "cycle_dir": cycle_dir,
            "panel_dir": panel_dir,
            "panel_photos": panel_dir / "photos",
            "panel_mosaic": panel_dir / f"{cycle}_{date_tag}_Ortho_mosaics_Ref_(panel).tiff",
            "panel_dem": panel_dir / f"{cycle}_{date_tag}_DEM.tiff",
            "panel_gps": panel_dir / f"{cycle}_{date_tag}_GPS.txt",
            "elc_dir": elc_dir,
            "elc_radiometric": radiometric_dir,
            "elc_alignment": alignment_dir,
            "elc_reflectance": elc_dir / "03_ELC Ref IMG",
            "elc_photos": elc_dir / "photos",
            "elc_mosaic": elc_dir / f"{cycle}_{date_tag}_Ortho_mosaics_Ref_(ELC).tiff",
            "elc_dem": elc_dir / f"{cycle}_{date_tag}_DEM.tiff",
            "elc_gps": elc_dir / f"{cycle}_{date_tag}_GPS.txt",
            "qc": elc_dir / str(self.config.get("qc_dir_name", DEFAULT_CONFIG["qc_dir_name"])),
        }
        paths["reference_mosaic"] = self.infer_reference_mosaic(cycle_dir, panel_dir, elc_dir, paths)
        paths["gps_txt"] = self.infer_gps_txt(cycle_dir, panel_dir, elc_dir, paths)
        paths["dem_tif"] = self.infer_dem_tif(cycle_dir, panel_dir, elc_dir, paths)
        return paths

    def apply_workflow_template(self, load_saved: bool = True) -> None:
        self.sync_cycle_dir_name()
        paths = self.workflow_paths()
        self.vars["cycle_dir_name"].set(paths["cycle_dir"].name)
        self.vars["roi_radiance_dir"].set(str(paths["elc_alignment"]))
        self.vars["apply_radiance_dir"].set(str(paths["elc_radiometric"]))
        self.vars["output_root"].set(str(paths["elc_dir"]))
        self.vars["reference_mosaic"].set(str(paths["reference_mosaic"]))
        self.vars["gps_txt"].set(str(paths["gps_txt"]))
        self.vars["dem_tif"].set(str(paths["dem_tif"]))
        self.config["reflectance_dir_name"] = "03_ELC Ref IMG"
        self.set_stage("paths", "완료", 100, "회차 경로 적용")
        self.log("회차 경로 적용 완료")
        self.log(f"ROI/계수 영상: {paths['elc_alignment']}")
        self.log(f"ELC 적용 대상: {paths['elc_radiometric']}")
        self.log(f"ELC 출력 Root: {paths['elc_dir']}")
        self.log(f"기준 모자이크: {paths['reference_mosaic']}")
        self.log(f"GPS.txt: {paths['gps_txt']}")
        self.log(f"DEM.tif: {paths['dem_tif']}")
        if load_saved:
            self.load_cycle_state()
            for name in ["reference_mosaic", "gps_txt", "dem_tif"]:
                current = Path(self.vars[name].get())
                if not self.vars[name].get().strip() or not current.exists():
                    self.vars[name].set(str(paths[name]))
            self.save_cycle_state()
        else:
            self.save_cycle_state()

    def inspect_workflow_paths(self) -> None:
        paths = self.workflow_paths()
        self.log("폴더/파일 검사 시작")
        for key in [
            "raw_root",
            "raw_ramses",
            "raw_re",
            "raw_rgb",
            "cycle_dir",
            "panel_dir",
            "panel_photos",
            "panel_mosaic",
            "panel_dem",
            "panel_gps",
            "elc_dir",
            "elc_radiometric",
            "elc_alignment",
            "elc_reflectance",
            "elc_photos",
            "reference_mosaic",
            "gps_txt",
            "dem_tif",
        ]:
            path = paths[key]
            mark = "OK" if path.exists() else "없음"
            self.log(f"{mark} {key}: {path}")

    def create_workflow_dirs(self) -> None:
        paths = self.workflow_paths()
        for key in [
            "cycle_dir",
            "panel_dir",
            "panel_photos",
            "elc_dir",
            "elc_radiometric",
            "elc_alignment",
            "elc_reflectance",
            "elc_photos",
            "qc",
        ]:
            paths[key].mkdir(parents=True, exist_ok=True)
        self.apply_workflow_template()
        self.save_config()
        self.log("필수 폴더 생성/설정 저장 완료")

    def scan_raw_candidates(self) -> None:
        paths = self.workflow_paths()
        self.log("원본 후보 스캔")
        for key in ["raw_ramses", "raw_re", "raw_rgb"]:
            root = paths[key]
            if not root.exists():
                self.log(f"없음 {key}: {root}")
                continue
            children = [p.name for p in root.iterdir() if p.is_dir()]
            if not children:
                self.log(f"비어있음 {key}: {root}")
                continue
            preview = ", ".join(children[:8])
            suffix = " ..." if len(children) > 8 else ""
            self.log(f"{key}: {len(children)}개 / {preview}{suffix}")

    def cycle_state_key(self) -> str:
        root = self.vars["processed_root"].get().strip()
        cycle_dir = self.vars["cycle_dir_name"].get().strip()
        cycle = self.vars["cycle_label"].get().strip()
        return f"{root}::{cycle_dir or cycle}"

    def save_cycle_state(self, key: str | None = None) -> None:
        key = key or self.cycle_state_key()
        if not key.strip(":"):
            return
        data = self.current_config()
        self.state.setdefault("cycles", {})
        self.state["cycles"][key] = {
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "config": data,
            "polygons": self.polygons,
            "tarp_points_geo": self.tarp_points_geo,
            "candidate_rows": self.candidate_rows,
            "stage_status": self.stage_status,
        }
        self.state["last_cycle_key"] = key
        save_json(self.state_path, self.state)

    def load_cycle_state(self) -> None:
        key = self.cycle_state_key()
        cycle_data = self.state.get("cycles", {}).get(key)
        self.active_cycle_key = key
        if not cycle_data:
            return
        config = cycle_data.get("config", {})
        for name in [
            "raw_root",
            "processed_root",
            "cycle_label",
            "date_tag",
            "cycle_dir_name",
            "roi_radiance_dir",
            "apply_radiance_dir",
            "output_root",
            "reference_mosaic",
            "gps_txt",
            "dem_tif",
            "candidate_band",
            "candidate_max_vza_deg",
            "elc_shot",
            "preview_band",
            "trim_percent",
            "worker_count",
            "overwrite",
            "tarps_csv",
        ]:
            if name in self.vars and name in config:
                try:
                    self.vars[name].set(config[name])
                except Exception:
                    pass
        polygons = cycle_data.get("polygons")
        if isinstance(polygons, dict):
            self.polygons = {str(k): v for k, v in polygons.items() if isinstance(v, list)}
            for tarp in self.tarps:
                self.polygons.setdefault(tarp.key, [])
        tarp_points = cycle_data.get("tarp_points_geo")
        if isinstance(tarp_points, dict):
            self.tarp_points_geo = {
                str(key): {str(k): float(v) for k, v in value.items() if isinstance(v, (int, float))}
                for key, value in tarp_points.items()
                if isinstance(value, dict)
            }
        candidates = cycle_data.get("candidate_rows")
        if isinstance(candidates, list):
            self.candidate_rows = [row for row in candidates if isinstance(row, dict)]
            self.refresh_candidate_list()
        saved_stage = cycle_data.get("stage_status")
        if isinstance(saved_stage, dict):
            for stage_key, value in saved_stage.items():
                if stage_key in self.stage_status and isinstance(value, dict):
                    self.stage_status[stage_key].update(value)
                    self.set_stage(
                        stage_key,
                        str(self.stage_status[stage_key].get("status", "대기")),
                        float(self.stage_status[stage_key].get("progress", 0.0) or 0.0),
                        str(self.stage_status[stage_key].get("message", "")),
                        persist=False,
                    )
        self.log(f"회차 상태 복원: {key}")
        if self.preview_image is not None:
            self.redraw()

    def set_stage(
        self,
        key: str,
        status: str,
        progress: float | None = None,
        message: str = "",
        persist: bool = False,
    ) -> None:
        if key not in self.stage_status:
            return
        if progress is None:
            progress = float(self.stage_status[key].get("progress", 0.0) or 0.0)
        progress = max(0.0, min(100.0, float(progress)))
        self.stage_status[key] = {"status": status, "progress": progress, "message": message}
        if key in self.stage_text_vars:
            label = dict(self.stage_defs).get(key, key)
            tail = f" - {message}" if message else ""
            self.stage_text_vars[key].set(f"{label}: {status} ({progress:.0f}%){tail}")
        if key in self.stage_progress_vars:
            self.stage_progress_vars[key].set(progress)
        if persist:
            self.save_cycle_state()

    def queue_stage(
        self,
        key: str,
        status: str,
        progress: float | None = None,
        message: str = "",
        persist: bool = False,
    ) -> None:
        self.worker_queue.put(
            {
                "type": "stage",
                "key": key,
                "status": status,
                "progress": progress,
                "message": message,
                "persist": persist,
            }
        )

    def open_progress_window(self) -> None:
        tk = self.tk
        ttk = self.ttk
        if self.progress_window is not None and self.progress_window.winfo_exists():
            self.progress_window.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("ELC 단계별 진행상황")
        win.geometry("720x520")
        self.progress_window = win
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        for row, (key, label) in enumerate(self.stage_defs):
            text_var = tk.StringVar()
            progress_var = tk.DoubleVar()
            self.stage_text_vars[key] = text_var
            self.stage_progress_vars[key] = progress_var
            ttk.Label(frame, textvariable=text_var, width=52).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Progressbar(frame, variable=progress_var, maximum=100).grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=3)
            current = self.stage_status.get(key, {"status": "대기", "progress": 0.0, "message": ""})
            self.set_stage(key, str(current.get("status", "대기")), float(current.get("progress", 0.0) or 0.0), str(current.get("message", "")))
        ttk.Label(frame, text="로그").grid(row=len(self.stage_defs), column=0, sticky="w", pady=(10, 3))
        self.progress_log_text = tk.Text(frame, height=16)
        self.progress_log_text.grid(row=len(self.stage_defs) + 1, column=0, columnspan=2, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(len(self.stage_defs) + 1, weight=1)

        def on_close() -> None:
            self.progress_log_text = None
            self.progress_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        if self.progress_log_text is not None:
            try:
                self.progress_log_text.insert("end", f"[{stamp}] {message}\n")
                self.progress_log_text.see("end")
            except Exception:
                self.progress_log_text = None
        self.root.update_idletasks()

    def _drain_queue(self) -> None:
        try:
            while True:
                item = self.worker_queue.get_nowait()
                if isinstance(item, dict) and item.get("type") == "stage":
                    self.set_stage(
                        str(item.get("key")),
                        str(item.get("status")),
                        item.get("progress"),
                        str(item.get("message", "")),
                        bool(item.get("persist", False)),
                    )
                else:
                    self.log(str(item))
        except queue.Empty:
            pass
        self.root.after(200, self._drain_queue)

    def browse_dir(self, key: str) -> None:
        from tkinter import filedialog

        path = filedialog.askdirectory(initialdir=self.vars[key].get() or str(APP_DIR))
        if path:
            self.vars[key].set(path)
            if key == "processed_root":
                self.scan_cycle_folders(auto_apply=True)
            elif key == "raw_root":
                self.scan_raw_candidates()

    def browse_file(self, key: str) -> None:
        from tkinter import filedialog

        current = self.vars[key].get()
        initial = str(Path(current).parent) if current else str(APP_DIR)
        filetypes = [("All", "*.*")]
        if key == "tarps_csv":
            filetypes = [("CSV", "*.csv"), ("All", "*.*")]
        elif key in {"reference_mosaic", "dem_tif"}:
            filetypes = [("GeoTIFF", "*.tif *.tiff"), ("All", "*.*")]
        elif key == "gps_txt":
            filetypes = [("Text", "*.txt"), ("All", "*.*")]
        path = filedialog.askopenfilename(initialdir=initial, filetypes=filetypes)
        if path:
            self.vars[key].set(path)
            if key in {"reference_mosaic", "gps_txt", "dem_tif"}:
                self.save_cycle_state()

    def current_config(self) -> dict[str, Any]:
        data = dict(self.config)
        for key, var in self.vars.items():
            data[key] = var.get()
        if not data.get("roi_radiance_dir"):
            data["roi_radiance_dir"] = data.get("input_radiance_dir", "")
        if not data.get("apply_radiance_dir"):
            data["apply_radiance_dir"] = data.get("roi_radiance_dir", data.get("input_radiance_dir", ""))
        data["input_radiance_dir"] = data["roi_radiance_dir"]
        data["preview_band"] = int(data["preview_band"])
        data["candidate_band"] = int(data["candidate_band"])
        data["candidate_max_vza_deg"] = float(data["candidate_max_vza_deg"])
        data["trim_percent"] = float(data["trim_percent"])
        data["worker_count"] = int(data["worker_count"])
        data["overwrite"] = bool(data["overwrite"])
        if not data.get("roi_json"):
            data["roi_json"] = str(self.qc_dir(data) / f"{data['elc_shot']}_manual_roi.json")
        return data

    def qc_dir(self, config: dict[str, Any] | None = None) -> Path:
        config = config or self.current_config()
        return Path(config["output_root"]) / str(config["qc_dir_name"])

    def elc_dir(self, config: dict[str, Any] | None = None) -> Path:
        return self.qc_dir(config) / "elc_coefficients"

    def save_config(self) -> None:
        data = self.current_config()
        save_json(self.config_path, data)
        self.config = data
        self.save_cycle_state()
        self.log(f"설정 저장: {self.config_path}")

    def _load_tarps(self) -> None:
        try:
            self.tarps = load_tarps_csv(Path(self.vars["tarps_csv"].get()))
            for tarp in self.tarps:
                self.polygons.setdefault(tarp.key, [])
            displays = [f"{t.key} | {t.display}" for t in self.tarps]
            self.tarp_combo["values"] = displays
            if hasattr(self, "candidate_tarp_combo"):
                try:
                    if self.candidate_tarp_combo.winfo_exists():
                        self.candidate_tarp_combo["values"] = displays
                except Exception:
                    pass
            if displays and not self.current_tarp.get():
                self.current_tarp.set(displays[0])
            self.refresh_fit_list()
            self.log(f"타프 CSV 로드: {len(self.tarps)}개")
        except Exception as exc:
            self.log(f"타프 CSV 로드 실패: {exc}")

    def selected_key(self) -> str:
        value = self.current_tarp.get()
        return value.split("|", 1)[0].strip() if value else ""

    def refresh_fit_list(self) -> None:
        self.fit_list.delete(0, "end")
        for tarp in self.tarps:
            mark = "x" if tarp.use_for_fit else " "
            self.fit_list.insert("end", f"[{mark}] {tarp.key}  {tarp.display}")

    def toggle_fit_tarp(self) -> None:
        idxs = self.fit_list.curselection()
        if not idxs:
            return
        idx = int(idxs[0])
        self.tarps[idx].use_for_fit = not self.tarps[idx].use_for_fit
        self.refresh_fit_list()

    def ensure_tarp_library(self) -> Path:
        path_text = self.vars["tarps_csv"].get().strip()
        if not path_text:
            path = APP_DIR / "tarp_reflectance_library.csv"
            self.vars["tarps_csv"].set(str(path))
        else:
            path = Path(path_text)
        if not path.exists():
            template = APP_DIR / "tarps_measured_template.csv"
            if template.exists():
                shutil.copyfile(template, path)
            else:
                header = ["key", "display", "group", "use_for_fit"] + [f"B{band:02d}" for band, _ in BANDS] + ["note"]
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", newline="", encoding="utf-8-sig") as f:
                    csv.DictWriter(f, fieldnames=header).writeheader()
        return path

    def normalized_tarp_csv_rows(self, path: Path) -> tuple[list[str], list[dict[str, str]]]:
        header = ["key", "display", "group", "use_for_fit"] + [f"B{band:02d}" for band, _ in BANDS] + ["note"]
        rows: list[dict[str, str]] = []
        if path.exists():
            with path.open("r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                source_fields = reader.fieldnames or []
                merged_fields = list(header)
                for field in source_fields:
                    if field not in merged_fields:
                        merged_fields.append(field)
                for row in reader:
                    rows.append({field: str(row.get(field, "") or "") for field in merged_fields})
                header = merged_fields
        return header, rows

    def write_tarp_csv_rows(self, path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def open_tarps_csv(self) -> None:
        from tkinter import messagebox

        try:
            path = self.ensure_tarp_library()
            self.save_config()
            os.startfile(str(path))
            self.log(f"타프 CSV 열기: {path}")
        except Exception as exc:
            self.log(traceback.format_exc())
            messagebox.showerror("타프 CSV 열기 실패", str(exc))

    def add_tarp_dialog(self) -> None:
        tk = self.tk
        ttk = self.ttk
        from tkinter import messagebox

        dialog = tk.Toplevel(self.root)
        dialog.title("새 타프 추가")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill="both", expand=True)

        key_var = tk.StringVar()
        display_var = tk.StringVar()
        group_var = tk.StringVar()
        use_var = tk.BooleanVar(value=False)
        note_var = tk.StringVar()
        band_vars: dict[str, Any] = {}

        ttk.Label(frame, text="key").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(frame, textvariable=key_var, width=24).grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Label(frame, text="display").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(frame, textvariable=display_var, width=24).grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Label(frame, text="group").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(frame, textvariable=group_var, width=16).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Checkbutton(frame, text="ELC fit에 사용", variable=use_var).grid(row=2, column=2, columnspan=2, sticky="w", pady=3)
        ttk.Label(frame, text="note").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(frame, textvariable=note_var, width=24).grid(row=3, column=1, columnspan=3, sticky="ew", pady=3)

        band_frame = ttk.LabelFrame(frame, text="밴드별 measured reflectance", padding=8)
        band_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8, 4))
        for idx, (band, band_name) in enumerate(BANDS):
            row = idx // 2
            col = (idx % 2) * 2
            token = f"B{band:02d}"
            band_vars[token] = tk.StringVar()
            ttk.Label(band_frame, text=f"{token} {band_name}").grid(row=row, column=col, sticky="w", padx=(0, 6), pady=2)
            ttk.Entry(band_frame, textvariable=band_vars[token], width=14).grid(row=row, column=col + 1, sticky="w", pady=2)

        def save_new_tarp() -> None:
            try:
                path = self.ensure_tarp_library()
                fieldnames, rows = self.normalized_tarp_csv_rows(path)
                key = key_var.get().strip()
                if not key:
                    raise ValueError("key를 입력해야 합니다.")
                if any((row.get("key") or "").strip() == key for row in rows):
                    raise ValueError(f"이미 존재하는 key입니다: {key}")
                new_row = {field: "" for field in fieldnames}
                new_row["key"] = key
                new_row["display"] = display_var.get().strip() or key
                new_row["group"] = group_var.get().strip()
                new_row["use_for_fit"] = "1" if use_var.get() else "0"
                new_row["note"] = note_var.get().strip()
                for band, _ in BANDS:
                    token = f"B{band:02d}"
                    text = band_vars[token].get().strip()
                    if text:
                        float(text)
                    new_row[token] = text
                rows.append(new_row)
                self.write_tarp_csv_rows(path, fieldnames, rows)
                self.log(f"새 타프 추가: {key}")
                self._load_tarps()
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("새 타프 추가 실패", str(exc))

        button_row = 5
        ttk.Button(frame, text="추가", command=save_new_tarp).grid(row=button_row, column=2, sticky="ew", padx=(6, 0), pady=(8, 0))
        ttk.Button(frame, text="닫기", command=dialog.destroy).grid(row=button_row, column=3, sticky="ew", padx=(6, 0), pady=(8, 0))
        frame.columnconfigure(1, weight=1)
        key_var.set("")

    def candidate_output_csv(self, config: dict[str, Any] | None = None) -> Path:
        config = config or self.current_config()
        cycle = str(config.get("cycle_label", "cycle")).strip() or "cycle"
        date_tag = str(config.get("date_tag", "")).strip()
        suffix = f"_{date_tag}" if date_tag else ""
        return self.qc_dir(config) / "tarp_image_candidates" / f"{cycle}{suffix}_tarp_image_candidates.csv"

    def collect_candidate_image_paths(self, input_dir: Path, band: int) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        if not input_dir.exists():
            return paths
        for path in input_dir.rglob("*.tif*"):
            match = BAND_RE.match(path.name)
            if not match:
                continue
            shot, band_text = match.group(1), match.group(2)
            if int(band_text) == int(band):
                paths.setdefault(shot, path)
        return paths

    def refresh_candidate_list(self) -> None:
        if not hasattr(self, "candidate_list"):
            return
        self.candidate_list.delete(0, "end")
        self.candidate_list_indices: list[int] = []
        for idx, row in enumerate(self.candidate_rows):
            if str(row.get("candidate", "1")) != "1":
                continue
            self.candidate_list_indices.append(idx)
            self.candidate_list.insert(
                "end",
                f"{row.get('tarp_key', '')} | {row.get('shot', '')} | "
                f"VZA {float(row.get('vza_deg', math.nan)):.1f} | "
                f"{float(row.get('distance_m', math.nan)):.1f} m | "
                f"valid {row.get('valid_pixel', '')}",
            )

    def open_candidate_csv(self) -> None:
        from tkinter import messagebox

        try:
            path = self.candidate_output_csv()
            if not path.exists():
                raise FileNotFoundError(f"후보 CSV가 아직 없습니다:\n{path}")
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showerror("후보 CSV 열기 실패", str(exc))

    def use_selected_candidate_as_preview(self) -> None:
        from tkinter import messagebox

        idxs = self.candidate_list.curselection()
        if not idxs:
            messagebox.showinfo("후보 선택", "후보 영상 목록에서 하나를 먼저 선택하세요.")
            return
        source_idx = self.candidate_list_indices[int(idxs[0])]
        row = self.candidate_rows[source_idx]
        shot = str(row.get("shot", "")).strip()
        if not shot:
            return
        self.vars["elc_shot"].set(shot)
        self.save_cycle_state()
        self.log(f"ELC 기준 영상 설정: {shot}")
        self.load_preview()

    def open_candidate_window(self) -> None:
        tk = self.tk
        ttk = self.ttk
        if self.candidate_window is not None and self.candidate_window.winfo_exists():
            self.candidate_window.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("기준 모자이크 타프 위치 / 후보 영상 선정")
        win.geometry("1180x820")
        self.candidate_window = win

        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="타프").pack(side="left")
        self.candidate_tarp_combo = ttk.Combobox(top, textvariable=self.current_tarp, width=26, state="readonly")
        self.candidate_tarp_combo["values"] = list(self.tarp_combo["values"])
        self.candidate_tarp_combo.pack(side="left", padx=(6, 12))
        ttk.Button(top, text="기준 모자이크 로드", command=self.load_reference_mosaic).pack(side="left", padx=3)
        ttk.Button(top, text="선택 타프 점 삭제", command=self.clear_selected_tarp_point).pack(side="left", padx=3)
        ttk.Button(top, text="후보 영상 추림", command=self.run_tarp_candidate_search).pack(side="left", padx=3)
        ttk.Label(top, text="좌클릭: 타프 중심점 지정 / 휠: 확대축소 / 가운데 또는 우클릭 드래그: 이동").pack(side="left", padx=(14, 0))

        self.mosaic_canvas = tk.Canvas(win, background="#171717", highlightthickness=0)
        self.mosaic_canvas.pack(fill="both", expand=True)
        self.mosaic_canvas.bind("<Button-1>", self.on_mosaic_left_down)
        self.mosaic_canvas.bind("<Button-2>", self.on_mosaic_pan_down)
        self.mosaic_canvas.bind("<B2-Motion>", self.on_mosaic_pan_drag)
        self.mosaic_canvas.bind("<ButtonRelease-2>", self.on_mosaic_pan_up)
        self.mosaic_canvas.bind("<Button-3>", self.on_mosaic_pan_down)
        self.mosaic_canvas.bind("<B3-Motion>", self.on_mosaic_pan_drag)
        self.mosaic_canvas.bind("<ButtonRelease-3>", self.on_mosaic_pan_up)
        self.mosaic_canvas.bind("<MouseWheel>", self.on_mosaic_wheel)
        self.mosaic_canvas.bind("<Double-Button-1>", lambda event: self.fit_mosaic_to_canvas(redraw=True))

        def on_close() -> None:
            self.candidate_window = None
            self.mosaic_canvas = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        if Path(self.vars["reference_mosaic"].get()).exists():
            self.load_reference_mosaic()
        else:
            self.redraw_mosaic()

    def load_reference_mosaic(self) -> None:
        from tkinter import messagebox

        try:
            path = Path(self.vars["reference_mosaic"].get())
            if not path.exists():
                raise FileNotFoundError(f"기준 모자이크가 없습니다:\n{path}")
            self.set_stage("candidates", "진행중", 10, "기준 모자이크 로드")
            image, gt, projection, size = make_mosaic_preview(path)
            self.mosaic_image = image
            self.mosaic_gt = gt
            self.mosaic_projection = projection
            self.mosaic_size = size
            self.fit_mosaic_to_canvas()
            self.set_stage("candidates", "대기", 20, "타프 위치 지정", persist=True)
            self.log(f"기준 모자이크 로드: {path.name} / preview {size[0]}x{size[1]}")
            self.redraw_mosaic()
        except Exception as exc:
            self.set_stage("candidates", "실패", 0, str(exc), persist=True)
            self.log(traceback.format_exc())
            messagebox.showerror("기준 모자이크 로드 실패", str(exc))

    def mosaic_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return x * self.mosaic_zoom + self.mosaic_pan_x, y * self.mosaic_zoom + self.mosaic_pan_y

    def canvas_to_mosaic(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.mosaic_pan_x) / self.mosaic_zoom, (y - self.mosaic_pan_y) / self.mosaic_zoom

    def fit_mosaic_to_canvas(self, redraw: bool = False) -> None:
        if self.mosaic_canvas is None or self.mosaic_image is None:
            return
        self.root.update_idletasks()
        canvas_w = max(100, int(self.mosaic_canvas.winfo_width()))
        canvas_h = max(100, int(self.mosaic_canvas.winfo_height()))
        image_w, image_h = self.mosaic_size
        pad = 24
        self.mosaic_zoom = min((canvas_w - pad * 2) / image_w, (canvas_h - pad * 2) / image_h)
        self.mosaic_zoom = min(max(float(self.mosaic_zoom), 0.02), 12.0)
        self.mosaic_pan_x = (canvas_w - image_w * self.mosaic_zoom) / 2
        self.mosaic_pan_y = (canvas_h - image_h * self.mosaic_zoom) / 2
        if redraw:
            self.redraw_mosaic()

    def redraw_mosaic(self) -> None:
        if self.mosaic_canvas is None:
            return
        self.mosaic_canvas.delete("all")
        if self.mosaic_image is None:
            self.mosaic_canvas.create_text(30, 30, anchor="nw", fill="#ddd", text="기준 모자이크를 먼저 로드하세요.")
            return
        w = max(1, int(self.mosaic_size[0] * self.mosaic_zoom))
        h = max(1, int(self.mosaic_size[1] * self.mosaic_zoom))
        image = self.mosaic_image.resize((w, h), Image.Resampling.BILINEAR)
        self.mosaic_photo = ImageTk.PhotoImage(image)
        self.mosaic_canvas.create_image(self.mosaic_pan_x, self.mosaic_pan_y, anchor="nw", image=self.mosaic_photo)
        if self.mosaic_gt is None:
            return
        inv = inv_geotransform(self.mosaic_gt)
        colors = ["#ff3b30", "#00c7ff", "#ffe600", "#7cff45", "#b56cff", "#ff8a00", "#ffffff", "#00ffd5"]
        for idx, tarp in enumerate(self.tarps):
            point = self.tarp_points_geo.get(tarp.key)
            if not point or inv is None:
                continue
            pix = geo_to_pixel(self.mosaic_gt, point["x_geo"], point["y_geo"])
            if pix is None:
                continue
            cx, cy = self.mosaic_to_canvas(pix[0], pix[1])
            color = colors[idx % len(colors)]
            r = 8 if tarp.key == self.selected_key() else 6
            self.mosaic_canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="#111", width=2)
            self.mosaic_canvas.create_text(cx + 10, cy - 10, text=tarp.display, anchor="sw", fill=color)

    def on_mosaic_left_down(self, event: Any) -> None:
        if self.mosaic_image is None or self.mosaic_gt is None:
            return
        key = self.selected_key()
        if not key:
            return
        mx, my = self.canvas_to_mosaic(event.x, event.y)
        if not (0 <= mx < self.mosaic_size[0] and 0 <= my < self.mosaic_size[1]):
            return
        x_geo, y_geo = pixel_to_geo(self.mosaic_gt, mx, my)
        z_ground = sample_dem_z(Path(self.vars["dem_tif"].get()), x_geo, y_geo, self.mosaic_projection)
        self.tarp_points_geo[key] = {"x_geo": x_geo, "y_geo": y_geo, "z_ground": z_ground}
        self.set_stage("candidates", "진행중", 35, f"{key} 위치 지정", persist=True)
        self.log(f"타프 위치 지정: {key} / x={x_geo:.8f}, y={y_geo:.8f}, z={z_ground if np.isfinite(z_ground) else math.nan:.3f}")
        self.redraw_mosaic()

    def on_mosaic_pan_down(self, event: Any) -> None:
        self.mosaic_drag_last = (event.x, event.y)

    def on_mosaic_pan_drag(self, event: Any) -> None:
        if not self.mosaic_drag_last:
            return
        last_x, last_y = self.mosaic_drag_last
        self.mosaic_pan_x += event.x - last_x
        self.mosaic_pan_y += event.y - last_y
        self.mosaic_drag_last = (event.x, event.y)
        self.redraw_mosaic()

    def on_mosaic_pan_up(self, event: Any) -> None:
        self.mosaic_drag_last = None

    def on_mosaic_wheel(self, event: Any) -> None:
        if self.mosaic_image is None:
            return
        mx, my = self.canvas_to_mosaic(event.x, event.y)
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self.mosaic_zoom = min(max(self.mosaic_zoom * factor, 0.05), 12.0)
        self.mosaic_pan_x = event.x - mx * self.mosaic_zoom
        self.mosaic_pan_y = event.y - my * self.mosaic_zoom
        self.redraw_mosaic()

    def clear_selected_tarp_point(self) -> None:
        key = self.selected_key()
        if key and key in self.tarp_points_geo:
            del self.tarp_points_geo[key]
            self.save_cycle_state()
            self.redraw_mosaic()
            self.log(f"타프 위치 삭제: {key}")

    def run_tarp_candidate_search(self) -> None:
        from tkinter import messagebox

        try:
            self.open_progress_window()
            config = self.current_config()
            if not self.tarp_points_geo:
                raise RuntimeError("기준 모자이크에서 타프 중심점을 먼저 찍어야 합니다.")
            gps_txt = Path(config["gps_txt"])
            image_dir = Path(config["roi_radiance_dir"])
            band = int(config["candidate_band"])
            max_vza = float(config["candidate_max_vza_deg"])
            reference_mosaic = Path(config["reference_mosaic"])
            if self.mosaic_projection:
                ref_projection = self.mosaic_projection
            elif reference_mosaic.exists():
                ds = gdal.Open(str(reference_mosaic), gdal.GA_ReadOnly)
                ref_projection = ds.GetProjection() if ds else ""
                ds = None
            else:
                ref_projection = ""
            self.set_stage("candidates", "진행중", 45, "GPS/영상 매칭")
            gps_rows = read_gps_estimates(gps_txt, band)
            image_paths = self.collect_candidate_image_paths(image_dir, band)
            if not gps_rows:
                raise RuntimeError(f"GPS.txt에서 band {band} 영상 행을 찾지 못했습니다.")
            ground_values = [row["z"] for row in gps_rows if np.isfinite(row.get("z", math.nan))]
            fallback_ground_z = float(np.nanmedian(ground_values)) if ground_values else 0.0
            rows: list[dict[str, Any]] = []
            for tarp_key, point in self.tarp_points_geo.items():
                x_geo = float(point["x_geo"])
                y_geo = float(point["y_geo"])
                z_ground = float(point.get("z_ground", math.nan))
                if not np.isfinite(z_ground):
                    z_ground = sample_dem_z(Path(config["dem_tif"]), x_geo, y_geo, ref_projection)
                if not np.isfinite(z_ground):
                    z_ground = fallback_ground_z
                for gps in gps_rows:
                    cam_x, cam_y = maybe_project_gps_xy(float(gps["x_est"]), float(gps["y_est"]), ref_projection)
                    cam_z = float(gps["z_est"])
                    if not (np.isfinite(cam_x) and np.isfinite(cam_y) and np.isfinite(cam_z)):
                        continue
                    east_m, north_m = delta_xy_m(x_geo, y_geo, cam_x, cam_y, ref_projection)
                    distance = math.hypot(east_m, north_m)
                    rel_alt = cam_z - z_ground
                    if not np.isfinite(rel_alt) or rel_alt <= 0:
                        continue
                    vza = math.degrees(math.atan2(distance, rel_alt))
                    vaa = (math.degrees(math.atan2(east_m, north_m)) + 360.0) % 360.0
                    path = image_paths.get(str(gps["shot"]))
                    inside = False
                    valid_pixel = False
                    px = py = math.nan
                    if path:
                        inside, valid_pixel, px, py = raster_point_status(path, x_geo, y_geo, ref_projection)
                    candidate = bool((inside and valid_pixel) or (not path and vza <= max_vza))
                    if not candidate:
                        continue
                    score = 100.0 - vza * 1.4 - distance * 0.03
                    if inside:
                        score += 25.0
                    if valid_pixel:
                        score += 50.0
                    rows.append(
                        {
                            "tarp_key": tarp_key,
                            "shot": gps["shot"],
                            "image_label": gps["label"],
                            "band": band,
                            "image_path": str(path or ""),
                            "x_geo": x_geo,
                            "y_geo": y_geo,
                            "z_ground": z_ground,
                            "sensor_x_est": cam_x,
                            "sensor_y_est": cam_y,
                            "sensor_z_est": cam_z,
                            "distance_m": distance,
                            "relative_altitude_m": rel_alt,
                            "vza_deg": vza,
                            "vaa_deg": vaa,
                            "yaw_est": gps.get("yaw_est", math.nan),
                            "pitch_est": gps.get("pitch_est", math.nan),
                            "roll_est": gps.get("roll_est", math.nan),
                            "point_in_image": int(inside),
                            "valid_pixel": int(valid_pixel),
                            "candidate": int(candidate),
                            "score": score,
                        }
                    )
            rows.sort(key=lambda r: (str(r["tarp_key"]), -float(r["score"]), float(r["vza_deg"])))
            out_csv = self.candidate_output_csv(config)
            out_csv.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = [
                "tarp_key",
                "shot",
                "image_label",
                "band",
                "image_path",
                "x_geo",
                "y_geo",
                "z_ground",
                "sensor_x_est",
                "sensor_y_est",
                "sensor_z_est",
                "distance_m",
                "relative_altitude_m",
                "vza_deg",
                "vaa_deg",
                "yaw_est",
                "pitch_est",
                "roll_est",
                "point_in_image",
                "valid_pixel",
                "candidate",
                "score",
            ]
            with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            self.candidate_rows = rows
            self.refresh_candidate_list()
            self.set_stage("candidates", "완료", 100, f"{len(rows)} candidates", persist=True)
            self.log(f"후보 영상 추림 완료: {len(rows)}개 / {out_csv}")
            if not rows:
                messagebox.showwarning("후보 없음", "조건에 맞는 후보 영상을 찾지 못했습니다. 타프 위치, GPS, DEM, 후보 Band를 확인하세요.")
        except Exception as exc:
            self.set_stage("candidates", "실패", 0, str(exc), persist=True)
            self.log(traceback.format_exc())
            messagebox.showerror("후보 영상 추림 실패", str(exc))

    def _load_roi_if_exists(self) -> None:
        config = self.current_config()
        roi_path = Path(config["roi_json"])
        if roi_path.exists():
            data = json.loads(roi_path.read_text(encoding="utf-8-sig"))
            self.polygons.update({k: v for k, v in data.get("polygons", {}).items() if isinstance(v, list)})
            self.log(f"기존 ROI 로드: {roi_path}")

    def load_preview(self) -> None:
        try:
            self.set_stage("preview", "진행중", 20, "영상 찾는 중")
            config = self.current_config()
            input_dir = Path(config["roi_radiance_dir"])
            shot = str(config["elc_shot"]).strip()
            band = int(config["preview_band"])
            if not input_dir.exists():
                raise FileNotFoundError(f"ROI/계수 영상 폴더가 없습니다:\n{input_dir}")
            path = find_band_file(input_dir, shot, band)
            arr, ds, nodata = read_band_array(path)
            if nodata is not None:
                arr[arr == nodata] = np.nan
            self.preview_arr = arr
            self.preview_size = (arr.shape[1], arr.shape[0])
            self.preview_image = Image.fromarray(stretch_uint8(arr), mode="L").convert("RGB")
            self.fit_preview_to_canvas()
            ds = None
            self.log(f"Preview 로드: {path.name} / {self.preview_size[0]}x{self.preview_size[1]}")
            self.set_stage("preview", "완료", 100, path.name, persist=True)
            self.redraw()
        except Exception as exc:
            from tkinter import messagebox

            self.set_stage("preview", "실패", 0, str(exc), persist=True)
            self.log(traceback.format_exc())
            messagebox.showerror("Preview 로드 실패", str(exc))

    def image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return x * self.zoom + self.pan_x, y * self.zoom + self.pan_y

    def canvas_to_image(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.pan_x) / self.zoom, (y - self.pan_y) / self.zoom

    def fit_preview_to_canvas(self, redraw: bool = False) -> None:
        if self.preview_image is None:
            return
        self.root.update_idletasks()
        canvas_w = max(100, int(self.canvas.winfo_width()))
        canvas_h = max(100, int(self.canvas.winfo_height()))
        image_w, image_h = self.preview_size
        pad = 24
        usable_w = max(20, canvas_w - pad * 2)
        usable_h = max(20, canvas_h - pad * 2)
        fit_zoom = min(usable_w / image_w, usable_h / image_h)
        if not np.isfinite(fit_zoom) or fit_zoom <= 0:
            fit_zoom = 1.0
        self.zoom = min(max(float(fit_zoom), 0.02), 12.0)
        drawn_w = image_w * self.zoom
        drawn_h = image_h * self.zoom
        self.pan_x = (canvas_w - drawn_w) / 2
        self.pan_y = (canvas_h - drawn_h) / 2
        if redraw:
            self.redraw()

    def redraw(self) -> None:
        self.canvas.delete("all")
        if self.preview_image is None:
            self.canvas.create_text(40, 40, anchor="nw", fill="#ddd", text="2 Preview 로드를 먼저 누르세요.")
            return
        w = max(1, int(self.preview_size[0] * self.zoom))
        h = max(1, int(self.preview_size[1] * self.zoom))
        image = self.preview_image.resize((w, h), Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(self.pan_x, self.pan_y, anchor="nw", image=self.photo)

        colors = ["#ff3b30", "#00c7ff", "#ffe600", "#7cff45", "#b56cff", "#ff8a00", "#ffffff", "#00ffd5"]
        for idx, tarp in enumerate(self.tarps):
            pts = self.polygons.get(tarp.key) or []
            if not pts:
                continue
            color = colors[idx % len(colors)]
            cpts = [self.image_to_canvas(p[0], p[1]) for p in pts]
            flat = [coord for p in cpts for coord in p]
            if len(cpts) >= 2:
                self.canvas.create_line(*flat, fill=color, width=2)
            if len(cpts) >= 3:
                self.canvas.create_polygon(*flat, outline=color, fill="", width=2)
            for i, (cx, cy) in enumerate(cpts):
                r = 4 if tarp.key != self.selected_key() else 6
                self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="#111")
            if cpts:
                self.canvas.create_text(cpts[0][0] + 8, cpts[0][1] - 8, text=tarp.display, anchor="sw", fill=color)

    def on_left_down(self, event: Any) -> None:
        if self.preview_image is None:
            return
        mode = self.mode.get()
        if mode == "pan":
            self.drag_pan_last = (event.x, event.y)
            return
        key = self.selected_key()
        if not key:
            return
        ix, iy = self.canvas_to_image(event.x, event.y)
        if mode == "edit":
            self.drag_vertex = self.nearest_vertex(event.x, event.y)
        else:
            if 0 <= ix < self.preview_size[0] and 0 <= iy < self.preview_size[1]:
                self.polygons.setdefault(key, []).append([float(ix), float(iy)])
                self.redraw()

    def on_left_drag(self, event: Any) -> None:
        if self.mode.get() == "pan" and self.drag_pan_last:
            last_x, last_y = self.drag_pan_last
            self.pan_x += event.x - last_x
            self.pan_y += event.y - last_y
            self.drag_pan_last = (event.x, event.y)
            self.redraw()
            return
        if self.drag_vertex:
            key, idx = self.drag_vertex
            ix, iy = self.canvas_to_image(event.x, event.y)
            ix = min(max(ix, 0), self.preview_size[0] - 1)
            iy = min(max(iy, 0), self.preview_size[1] - 1)
            self.polygons[key][idx] = [float(ix), float(iy)]
            self.redraw()

    def on_left_up(self, event: Any) -> None:
        self.drag_vertex = None
        self.drag_pan_last = None
        self.save_cycle_state()

    def on_right_down(self, event: Any) -> None:
        self.drag_pan_last = (event.x, event.y)

    def on_right_drag(self, event: Any) -> None:
        if not self.drag_pan_last:
            return
        last_x, last_y = self.drag_pan_last
        self.pan_x += event.x - last_x
        self.pan_y += event.y - last_y
        self.drag_pan_last = (event.x, event.y)
        self.redraw()

    def on_right_up(self, event: Any) -> None:
        self.drag_pan_last = None

    def on_middle_down(self, event: Any) -> None:
        self.drag_pan_last = (event.x, event.y)

    def on_middle_drag(self, event: Any) -> None:
        self.on_right_drag(event)

    def on_middle_up(self, event: Any) -> None:
        self.drag_pan_last = None

    def on_wheel(self, event: Any) -> None:
        if self.preview_image is None:
            return
        ix, iy = self.canvas_to_image(event.x, event.y)
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self.zoom = min(max(self.zoom * factor, 0.05), 12.0)
        self.pan_x = event.x - ix * self.zoom
        self.pan_y = event.y - iy * self.zoom
        self.redraw()

    def nearest_vertex(self, canvas_x: float, canvas_y: float) -> tuple[str, int] | None:
        best: tuple[str, int] | None = None
        best_dist = 14.0
        key = self.selected_key()
        for idx, point in enumerate(self.polygons.get(key, [])):
            cx, cy = self.image_to_canvas(point[0], point[1])
            dist = math.hypot(cx - canvas_x, cy - canvas_y)
            if dist < best_dist:
                best = (key, idx)
                best_dist = dist
        return best

    def undo_point(self) -> None:
        key = self.selected_key()
        if key and self.polygons.get(key):
            self.polygons[key].pop()
            self.redraw()
            self.save_cycle_state()

    def clear_selected_roi(self) -> None:
        key = self.selected_key()
        if key:
            self.polygons[key] = []
            self.redraw()
            self.save_cycle_state()

    def save_roi(self) -> None:
        config = self.current_config()
        roi_path = Path(config["roi_json"])
        data = {
            "elc_shot": config["elc_shot"],
            "preview_band": config["preview_band"],
            "roi_radiance_dir": config["roi_radiance_dir"],
            "apply_radiance_dir": config["apply_radiance_dir"],
            "input_radiance_dir": config["roi_radiance_dir"],
            "image_width": self.preview_size[0],
            "image_height": self.preview_size[1],
            "trim_percent": config["trim_percent"],
            "polygons": self.polygons,
            "tarps": [
                {
                    "key": t.key,
                    "display": t.display,
                    "use_for_fit": t.use_for_fit,
                    "measured": t.measured,
                }
                for t in self.tarps
            ],
        }
        save_json(roi_path, data)
        self.config["roi_json"] = str(roi_path)
        save_json(self.config_path, self.current_config())
        self.set_stage("roi", "완료", 100, roi_path.name, persist=True)
        self.log(f"ROI 저장: {roi_path}")

    def run_thread(self, target: Any) -> None:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()

    def run_roi_stats(self) -> None:
        self.open_progress_window()
        self.save_roi()
        config = self.current_config()
        self.set_stage("stats", "대기", 0, "시작 대기")

        def task() -> None:
            try:
                self.queue_stage("stats", "진행중", 10, "ROI 평균 계산")
                output_dir = self.elc_dir(config)
                result = calculate_roi_stats(
                    Path(config["roi_radiance_dir"]),
                    str(config["elc_shot"]),
                    self.tarps,
                    self.polygons,
                    float(config["trim_percent"]),
                    output_dir,
                )
                self.queue_stage("stats", "완료", 100, Path(result["stats_csv"]).name, persist=True)
                self.worker_queue.put(f"ROI 평균 계산 완료: {result['stats_csv']}")
            except Exception:
                self.queue_stage("stats", "실패", 0, "ROI 평균 계산 실패", persist=True)
                self.worker_queue.put(traceback.format_exc())

        self.run_thread(task)

    def run_coefficients(self) -> None:
        self.open_progress_window()
        config = self.current_config()
        self.set_stage("coeff", "대기", 0, "시작 대기")

        def task() -> None:
            try:
                self.queue_stage("coeff", "진행중", 10, "계수 입력 확인")
                output_dir = self.elc_dir(config)
                stats_csv = output_dir / f"{config['elc_shot']}_roi_radiance_stats.csv"
                if not stats_csv.exists():
                    self.worker_queue.put("ROI 평균 파일이 없어 먼저 계산합니다.")
                    self.queue_stage("stats", "진행중", 20, "계수 전 ROI 평균 자동 계산")
                    calculate_roi_stats(
                        Path(config["roi_radiance_dir"]),
                        str(config["elc_shot"]),
                        self.tarps,
                        self.polygons,
                        float(config["trim_percent"]),
                        output_dir,
                    )
                    self.queue_stage("stats", "완료", 100, stats_csv.name, persist=True)
                self.queue_stage("coeff", "진행중", 60, "밴드별 a/b 계산")
                result = build_elc_coefficients(stats_csv, self.tarps, output_dir)
                self.queue_stage("coeff", "완료", 100, Path(result["coefficients_csv"]).name, persist=True)
                self.worker_queue.put(f"ELC 계수 추정 완료: {result['coefficients_csv']}")
            except Exception:
                self.queue_stage("coeff", "실패", 0, "계수 추정 실패", persist=True)
                self.worker_queue.put(traceback.format_exc())

        self.run_thread(task)

    def collect_apply_jobs(self, config: dict[str, Any], coef_csv: Path) -> list[dict[str, Any]]:
        input_dir = Path(config["apply_radiance_dir"])
        coefficients = load_coefficients(coef_csv)
        outputs: dict[str, str] = {
            "reflectance": str(Path(config["output_root"]) / config["reflectance_dir_name"])
        }
        jobs: list[dict[str, Any]] = []
        for src in input_dir.rglob("*"):
            if not src.is_file() or src.suffix.lower() not in {".tif", ".tiff"}:
                continue
            match = BAND_RE.match(src.name)
            if not match:
                continue
            band = int(match.group(2))
            if band not in coefficients:
                continue
            rel = src.relative_to(input_dir)
            jobs.append(
                {
                    "src": str(src),
                    "rel": str(rel),
                    "band": band,
                    "coefficients": {str(k): v for k, v in coefficients.items()},
                    "outputs": outputs,
                    "overwrite": config["overwrite"],
                    "compression": config["compression"],
                }
            )
        return jobs

    def run_apply_all(self) -> None:
        self.open_progress_window()
        config = self.current_config()
        self.stop_requested = False
        self.set_stage("apply", "대기", 0, "시작 대기")

        def task() -> None:
            try:
                self.queue_stage("apply", "진행중", 5, "계수/입력 확인")
                coef_csv = self.elc_dir(config) / "elc_coefficients.csv"
                if not coef_csv.exists():
                    self.worker_queue.put("ELC 계수 파일이 없어 먼저 계수를 추정합니다.")
                    stats_csv = self.elc_dir(config) / f"{config['elc_shot']}_roi_radiance_stats.csv"
                    if not stats_csv.exists():
                        self.queue_stage("stats", "진행중", 20, "적용 전 ROI 평균 자동 계산")
                        calculate_roi_stats(
                            Path(config["roi_radiance_dir"]),
                            str(config["elc_shot"]),
                            self.tarps,
                            self.polygons,
                            float(config["trim_percent"]),
                            self.elc_dir(config),
                        )
                        self.queue_stage("stats", "완료", 100, stats_csv.name, persist=True)
                    self.queue_stage("coeff", "진행중", 50, "적용 전 계수 자동 계산")
                    build_elc_coefficients(stats_csv, self.tarps, self.elc_dir(config))
                    self.queue_stage("coeff", "완료", 100, coef_csv.name, persist=True)
                jobs = self.collect_apply_jobs(config, coef_csv)
                if not jobs:
                    raise RuntimeError("적용할 입력 TIFF를 찾지 못했습니다.")
                total = len(jobs)
                workers = max(1, int(config["worker_count"]))
                self.queue_stage("apply", "진행중", 10, f"{total} files")
                self.worker_queue.put(f"전체 반사도 적용 시작: {total} files / workers={workers}")
                started = time.time()
                completed = 0
                failed = 0
                pending: set[Any] = set()
                job_iter = iter(jobs)
                with ProcessPoolExecutor(max_workers=workers) as executor:
                    for _ in range(workers * 2):
                        try:
                            pending.add(executor.submit(apply_one_job, next(job_iter)))
                        except StopIteration:
                            break
                    while pending:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED, timeout=1.0)
                        for future in done:
                            result = future.result()
                            completed += 1
                            if result.get("status") != "ok":
                                failed += 1
                            if completed == 1 or completed % 100 == 0 or completed == total:
                                elapsed = time.time() - started
                                rate = completed / elapsed if elapsed > 0 else 0
                                eta = (total - completed) / rate if rate > 0 else math.nan
                                self.queue_stage("apply", "진행중", completed / total * 100, f"{completed}/{total}, ETA {format_seconds(eta)}")
                                self.worker_queue.put(
                                    f"ELC apply: {completed}/{total} ({completed / total * 100:.2f}%), "
                                    f"{rate:.2f} files/s, ETA {format_seconds(eta)}, failed={failed}"
                                )
                        if self.stop_requested:
                            self.queue_stage("apply", "중지중", completed / total * 100, f"{completed}/{total}")
                            self.worker_queue.put("긴급 정지 요청: 새 작업 투입을 멈췄습니다.")
                            break
                        while len(pending) < workers * 2:
                            try:
                                pending.add(executor.submit(apply_one_job, next(job_iter)))
                            except StopIteration:
                                break
                final_status = "중지" if self.stop_requested else "완료"
                self.queue_stage("apply", final_status, completed / total * 100, f"completed={completed}, failed={failed}", persist=True)
                self.worker_queue.put(f"전체 반사도 적용 종료: completed={completed}, failed={failed}")
            except Exception:
                self.queue_stage("apply", "실패", 0, "반사도 적용 실패", persist=True)
                self.worker_queue.put(traceback.format_exc())

        self.run_thread(task)

    def run_full_pipeline(self) -> None:
        self.open_progress_window()
        self.save_roi()
        config = self.current_config()
        self.stop_requested = False
        self.set_stage("stats", "대기", 0, "전체 실행 대기")
        self.set_stage("coeff", "대기", 0, "전체 실행 대기")
        self.set_stage("apply", "대기", 0, "전체 실행 대기")

        def task() -> None:
            try:
                output_dir = self.elc_dir(config)
                self.queue_stage("stats", "진행중", 10, "ROI 평균 계산")
                self.worker_queue.put("전체 ELC 처리 시작: ROI 평균 계산")
                calculate_roi_stats(
                    Path(config["roi_radiance_dir"]),
                    str(config["elc_shot"]),
                    self.tarps,
                    self.polygons,
                    float(config["trim_percent"]),
                    output_dir,
                )
                self.queue_stage("stats", "완료", 100, f"{config['elc_shot']}_roi_radiance_stats.csv", persist=True)
                self.queue_stage("coeff", "진행중", 30, "밴드별 a/b 계산")
                self.worker_queue.put("전체 ELC 처리: ELC 계수 추정")
                coef_result = build_elc_coefficients(
                    output_dir / f"{config['elc_shot']}_roi_radiance_stats.csv",
                    self.tarps,
                    output_dir,
                )
                self.queue_stage("coeff", "완료", 100, "elc_coefficients.csv", persist=True)
                jobs = self.collect_apply_jobs(config, coef_result["coefficients_csv"])
                if not jobs:
                    raise RuntimeError("적용할 입력 TIFF를 찾지 못했습니다.")
                total = len(jobs)
                workers = max(1, int(config["worker_count"]))
                self.queue_stage("apply", "진행중", 10, f"{total} files")
                self.worker_queue.put(
                    f"전체 ELC 처리: 반사도 적용 시작 / target={config['apply_radiance_dir']} / "
                    f"{total} files / workers={workers}"
                )
                started = time.time()
                completed = 0
                failed = 0
                pending: set[Any] = set()
                job_iter = iter(jobs)
                with ProcessPoolExecutor(max_workers=workers) as executor:
                    for _ in range(workers * 2):
                        try:
                            pending.add(executor.submit(apply_one_job, next(job_iter)))
                        except StopIteration:
                            break
                    while pending:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED, timeout=1.0)
                        for future in done:
                            result = future.result()
                            completed += 1
                            if result.get("status") != "ok":
                                failed += 1
                            if completed == 1 or completed % 100 == 0 or completed == total:
                                elapsed = time.time() - started
                                rate = completed / elapsed if elapsed > 0 else 0
                                eta = (total - completed) / rate if rate > 0 else math.nan
                                self.queue_stage("apply", "진행중", completed / total * 100, f"{completed}/{total}, ETA {format_seconds(eta)}")
                                self.worker_queue.put(
                                    f"ELC apply: {completed}/{total} ({completed / total * 100:.2f}%), "
                                    f"{rate:.2f} files/s, ETA {format_seconds(eta)}, failed={failed}"
                                )
                        if self.stop_requested:
                            self.queue_stage("apply", "중지중", completed / total * 100, f"{completed}/{total}")
                            self.worker_queue.put("긴급 정지 요청: 새 작업 투입을 멈췄습니다.")
                            break
                        while len(pending) < workers * 2:
                            try:
                                pending.add(executor.submit(apply_one_job, next(job_iter)))
                            except StopIteration:
                                break
                final_status = "중지" if self.stop_requested else "완료"
                self.queue_stage("apply", final_status, completed / total * 100, f"completed={completed}, failed={failed}", persist=True)
                self.worker_queue.put(f"전체 ELC 처리 종료: completed={completed}, failed={failed}")
            except Exception:
                self.queue_stage("apply", "실패", 0, "전체 처리 실패", persist=True)
                self.worker_queue.put(traceback.format_exc())

        self.run_thread(task)

    def request_stop(self) -> None:
        self.stop_requested = True
        self.log("정지 요청됨. 이미 실행 중인 파일 몇 개는 마무리 후 멈춥니다.")


def main() -> None:
    import tkinter as tk

    root = tk.Tk()
    app = GenericElcGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
