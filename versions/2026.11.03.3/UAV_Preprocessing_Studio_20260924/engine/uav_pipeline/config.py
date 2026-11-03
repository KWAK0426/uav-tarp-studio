from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parents[3]
DEFAULTS: dict[str, Any] = {
    "project_name": "UAV_ELC_BRDF",
    "timezone": "Asia/Seoul",
    "photos": {
        "directory": "",
        "glob": "*.tif",
        "band_count": 10,
        "alpha_band": 11,
        "reflectance_scale": 1.0,
        "nodata_values": [65535],
    },
    "geometry": {
        "gps_path": "",
        "gps_crs": "EPSG:4326",
        "dem_path": "",
        "frame_times_csv": "",
        "frame_time_source_directory": "",
        "frame_time_source_timezone": "UTC",
        "output_crs": "",
        "write_per_frame_geotiff": True,
    },
    "fields": {
        "mask_geojson": "",
        "pixel_mask_json": "",
        "pixel_mask_reference_geotiff": "",
        "id_field": "field_id",
    },
    "output": {"directory": "", "overwrite": False, "tile_size": 256},
    "mosaic": {"method": "minimum_vza", "resampling": "bilinear"},
    "brdf": {
        "fit_method": "nnls",
        "write_ols_diagnostic": True,
        "min_observations": 7,
        "min_vza_range_deg": 12.0,
        "min_raa_bin_count": 4,
        "raa_bin_width_deg": 15.0,
        "condition_number_max": 1000000.0,
        "nbar_vza_deg": 0.0,
        "nbar_raa_deg": 0.0,
        "principal_tolerance_deg": 10.0,
        "perpendicular_tolerance_deg": 10.0,
    },
    "viewer": {
        "enabled": True,
        "display_max_width": 2400,
        "rgb_bands": [6, 4, 2],
        "ndvi_red_band": 6,
        "ndvi_nir_band": 10,
        "cycle_id": "C01",
        "cycle_label": "1회차",
        "site_name": "",
        "ed_timeseries_csv": "",
        "skyfish_gif": "",
    },
    "band_names": {
        "1": "Blue_444nm",
        "2": "Blue_475nm",
        "3": "Green_531nm",
        "4": "Green_560nm",
        "5": "Red_650nm",
        "6": "Red_668nm",
        "7": "RedEdge_705nm",
        "8": "RedEdge_717nm",
        "9": "RedEdge_740nm",
        "10": "NIR_842nm",
    },
    "preprocess": {
        "python_executable": sys.executable,
        "radiometric_config": "",
        "ed_config": "",
        "elc_apply_config": "",
        "allow_nonunity_sensor_gains": True,
        "tools": {
            "radiometric": str(PACKAGE / "outputs/nongwon_preprocess_gui/nongwon_preprocess_gui.py"),
            "elc_gui": str(PACKAGE / "outputs/nongwon_preprocess_gui/generic_elc_tools/generic_elc_gui.py"),
            "ed": str(PACKAGE / "ed_sensor_correction_tools/apply_ed_sensor_correction.py"),
            "elc_apply": str(PACKAGE / "ed_sensor_correction_tools/apply_elc_reflectance_with_config.py"),
        },
    },
}


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    cfg = _deep_merge(DEFAULTS, data)
    cfg["_config_path"] = str(path.resolve())
    return cfg


def save_config(path: str | Path, data: dict[str, Any]) -> None:
    clean = {key: value for key, value in data.items() if not key.startswith("_")}
    Path(path).write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def output_dir(cfg: dict[str, Any]) -> Path:
    path = Path(cfg["output"]["directory"])
    if not str(path):
        raise ValueError("output.directory is empty")
    return path
