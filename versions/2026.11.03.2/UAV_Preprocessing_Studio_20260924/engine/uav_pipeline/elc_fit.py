"""Fit forward ELC coefficients from an approved native-band tarp ROI manifest."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
from pathlib import Path

import cv2
import numpy as np
from osgeo import gdal
from PIL import Image

from .preparation import locations
from .general_project import is_general, source_list_path, source_shots, scan_raw
from .tarp_tool.code.saturation_qc import scan_roi


def _latest_manifest(roi_root: Path) -> Path:
    latest_selections = sorted(roi_root.glob("workflow/*/selections/selected_*.json"),
                               key=lambda p: p.stat().st_mtime_ns, reverse=True)
    if latest_selections and json.loads(latest_selections[0].read_text(encoding="utf-8")).get("split_locations"):
        raise ValueError("두 위치 영상 쌍은 현재 단일 기준 영상 ELC 계수 경로에서 처리할 수 없습니다. "
                         "기존 단일 영상 ROI 계수를 재사용하지 않습니다.")
    pointers = sorted(roi_root.glob("workflow/*/reviewed_manifest_latest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pointers:
        raise ValueError("타프 추적 결과에서 기준 영상을 정하고 ROI를 승인한 뒤 '승인 ROI 통계 생성'을 먼저 실행하세요.")
    if latest_selections and latest_selections[0].stat().st_mtime_ns > pointers[0].stat().st_mtime_ns:
        raise ValueError("후보 영상 선택이 마지막 승인 ROI 통계 이후 변경됐습니다. 현재 선택으로 ROI를 다시 검수하세요.")
    pointer = json.loads(pointers[0].read_text(encoding="utf-8"))
    if pointer.get("draft_dirty"):
        raise ValueError("03 ROI가 변경되었습니다. 승인 ROI를 04·05로 다시 전달하세요.")
    path = Path(pointer["path"])
    if not path.is_file():
        raise ValueError("승인 ROI manifest를 찾을 수 없습니다. ROI 통계를 다시 생성하세요.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("editor") == "TarpROIStudio":
        for filename, expected in ((manifest["selection_file"], manifest["selection_sha256"]),
                                   (manifest["draft_path"], manifest["draft_sha256"]),
                                   (manifest["library_csv"], manifest["library_sha256"])):
            if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
                raise ValueError("후보 또는 ROI 작업이 마지막 승인 이후 변경되었습니다. 03에서 다시 전달하세요.")
        draft = json.loads(Path(manifest["draft_path"]).read_text(encoding="utf-8"))
        for source, size, modified in draft["sources"].values():
            stat = Path(source).stat()
            if (stat.st_size, stat.st_mtime_ns) != (size, modified):
                raise ValueError("승인 ROI의 원영상이 변경되었습니다. 03에서 다시 확인하세요.")
    return path


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _general_provenance(cfg):
    from .preparation import latest_candidate_selection
    selection, _, shots = latest_candidate_selection(cfg)
    selection = Path(selection).resolve()
    context_path = selection.parent.parent / "context.json"
    context = json.loads(context_path.read_text(encoding="utf-8-sig"))
    source = source_list_path(cfg)
    targets = source_shots(cfg)
    if (not shots or not shots <= targets or context.get("source_kind") != "raw_DN_boundary_only"
            or Path(context["image_source"]).resolve() != Path(cfg["preparation"]["raw_directory"]).resolve()):
        raise ValueError("General candidate context does not match the current target/raw inputs")
    return {"general_identity": dict(cfg["general"]), "source_list_path": str(source),
            "source_list_sha256": _sha(source), "target_shots": sorted(targets),
            "selected_shots": sorted(shots), "selection_file": str(selection), "selection_sha256": _sha(selection),
            "context_file": str(context_path), "context_sha256": _sha(context_path),
            "context_identity": context["identity"]}


def _current_manifest(cfg):
    """General approvals belong to this exact selection, never the newest other session."""
    if not is_general(cfg):
        return _latest_manifest(locations(cfg)["roi"])
    expected = _general_provenance(cfg)
    context = json.loads(Path(expected["context_file"]).read_text(encoding="utf-8-sig"))
    pointer_path = Path(context["output"]) / "reviewed_manifest_latest.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8-sig")) if pointer_path.is_file() else {}
    explicit = cfg["preparation"].get("approved_roi_manifest")
    if pointer.get("draft_dirty") or not pointer.get("path"):
        raise ValueError("Current General candidate context requires a published ROI approval")
    path = Path(explicit or pointer["path"]).resolve()
    if path != Path(pointer["path"]).resolve():
        raise ValueError("Pinned ROI approval is not the current General context approval")
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if (pointer.get("manifest_sha256") != _sha(path)
            or any(manifest.get(k) != v or pointer.get(k) != v for k, v in expected.items())
            or manifest.get("identity") != context["identity"]):
        raise ValueError("General ROI approval identity/source/context changed; approve again")
    for filename, key in (("draft_path", "draft_sha256"), ("library_csv", "library_sha256"),
                          ("transforms_json", "transforms_sha256")):
        if not manifest.get(key) or _sha(manifest[filename]) != manifest[key]:
            raise ValueError(f"General approved {filename} changed; approve again")
    if Path(manifest["library_csv"]).resolve() != Path(cfg["preparation"]["tarp_reflectance_csv"]).resolve():
        raise ValueError("General approved library differs from current configuration")
    draft = json.loads(Path(manifest["draft_path"]).read_text(encoding="utf-8-sig"))
    if any(draft.get(k) != v for k, v in expected.items()):
        raise ValueError("General ROI draft belongs to another source/context")
    selected = set(expected["selected_shots"])
    if (set(manifest.get("images", {})) != selected
            or any(not image.get("approved") for image in manifest["images"].values())
            or manifest.get("reference_shot") != cfg["preparation"].get("reference_shot")):
        raise ValueError("General ROI selected/reference shots changed")
    registry = scan_raw(cfg["preparation"]["raw_directory"], shots=selected)
    signatures = {}
    for shot in sorted(selected):
        for suffix in range(1, 11):
            source = registry.get((shot, suffix))
            if source is None:
                raise ValueError(f"Missing General approved source: {shot}_{suffix}")
            stat = source.stat()
            signatures[f"{shot}_{suffix}"] = [str(source.resolve()), stat.st_size, stat.st_mtime_ns]
    if draft.get("sources") != signatures:
        raise ValueError("General approved raw source signatures changed")
    transforms = json.loads(Path(manifest["transforms_json"]).read_text(encoding="utf-8-sig"))
    keys = [(entry["shot"], int(entry["band_suffix"])) for entry in transforms]
    if len(keys) != len(signatures) or set(keys) != {(s, b) for s in selected for b in range(1, 11)}:
        raise ValueError("General approved native transforms are incomplete or duplicated")
    for entry in transforms:
        source = registry[entry["shot"], int(entry["band_suffix"])]
        if Path(entry["source_path"]).resolve() != source.resolve():
            raise ValueError("General transform source differs from approved raw source")
    return path


def _checked_wavelength(path, expected_band, band_names):
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise ValueError(f"Cannot open normalized input: {path}")
    value = ds.GetMetadataItem("CENTRAL_WAVELENGTH")
    ds = None
    if value is None or not math.isfinite(float(value)) or _band_for_wavelength(float(value), band_names) != expected_band:
        raise ValueError(f"CENTRAL_WAVELENGTH missing or mismatched: {path}")
    return float(value)


def _library(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    return {row["key"]: row for row in rows}


def _library_key(value: str) -> str:
    return "PNU_03" if value == "PNU_3" else value


def _band_for_wavelength(wavelength: float, band_names: dict[str, str]) -> int:
    centers = {int(k): float(re.search(r"(\d+)nm$", name).group(1)) for k, name in band_names.items()}
    band = min(centers, key=lambda key: abs(centers[key] - wavelength))
    if abs(centers[band] - wavelength) > 3:
        raise ValueError(f"지원하지 않는 중심파장: {wavelength:g} nm")
    return band


def _read_values(path: Path, points: list[list[float]], expected_size: list[int], percentile_low: float, percentile_high: float,
                 exclusion: dict | None = None, sampling: str | None = None) -> np.ndarray:
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise ValueError(f"보정 radiance를 열 수 없습니다: {path}")
    if [ds.RasterXSize, ds.RasterYSize] != list(expected_size):
        raise ValueError(f"ROI 원영상과 보정 radiance 크기가 다릅니다: {path.name}")
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    array = band.ReadAsArray().astype("float32")
    ds = None
    if sampling == "pixel_center_v1":
        from .roi_studio_bridge import pixel_mask
        mask = pixel_mask(points, array.shape)
    else:
        mask = np.zeros(array.shape, "uint8")
        cv2.fillConvexPoly(mask, np.rint(np.asarray(points)).astype("int32"), 1)
    valid = (mask > 0) & np.isfinite(array)
    if nodata is not None:
        valid &= array != nodata
    if exclusion is not None:
        mask_path = Path(exclusion['path'])
        if hashlib.sha256(mask_path.read_bytes()).hexdigest() != exclusion['sha256']:
            raise ValueError(f"승인 제외 마스크가 변경됐습니다: {mask_path.name}")
        excluded = np.asarray(Image.open(mask_path).convert('L'))
        if excluded.shape != array.shape:
            raise ValueError(f"제외 마스크와 영상 크기가 다릅니다: {mask_path.name}")
        valid &= excluded == 0
    values = array[valid]
    if values.size < 5:
        raise ValueError(f"ROI 유효 화소가 부족합니다: {path.name}")
    lo, hi = np.percentile(values, [percentile_low, percentile_high])
    return values[(values >= lo) & (values <= hi)]


def fit(cfg: dict) -> dict[str, str]:
    paths = locations(cfg)
    manifest_path = _current_manifest(cfg)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    percentile_low = float(manifest.get("percentile_low", 5))
    percentile_high = float(manifest.get("percentile_high", 95))
    if not math.isfinite(percentile_low) or not math.isfinite(percentile_high) or not 0 <= percentile_low < percentile_high <= 100:
        raise ValueError("승인 ROI 값 필터 P 범위가 유효하지 않습니다.")
    reference = manifest.get("reference_shot")
    if manifest.get("editor") == "TarpROIStudio" and reference != cfg["preparation"].get("reference_shot"):
        raise ValueError("03 승인 기준 영상과 04 Ed 기준 영상이 다릅니다. 스튜디오에서 전달을 완료하세요.")
    if manifest.get("editor") == "TarpROIStudio" and Path(manifest["library_csv"]).resolve() != Path(cfg["preparation"]["tarp_reflectance_csv"]).resolve():
        raise ValueError("03 승인 타프 기준표와 현재 ELC 기준표가 다릅니다. ROI를 다시 전달하세요.")
    images = manifest.get("images", {})
    if not reference or reference not in images or not images[reference].get("approved"):
        raise ValueError("ELC·Ed 기준 영상을 선택하고 해당 영상 ROI를 승인하세요.")
    transforms = json.loads(Path(manifest["transforms_json"]).read_text(encoding="utf-8"))
    if manifest.get('pixel_mask_review_version') != 1:
        raise ValueError('ROI 내부 화소 검수 이전 manifest입니다. 그림자 마스크를 승인하고 ROI 통계를 다시 생성하세요.')
    entries = [row for row in transforms if row["shot"] == reference]
    if len(entries) != 10:
        raise ValueError("기준 영상의 10밴드 native ROI 변환이 필요합니다. 승인 ROI 통계를 다시 생성하세요.")
    library_path = Path(cfg["preparation"].get("tarp_reflectance_csv") or Path(__file__).resolve().parents[1] / "defaults" / "tarp_reflectance_library.csv")
    library = _library(library_path)
    output = paths["coef"]
    output.mkdir(parents=True, exist_ok=True)
    raw_qc = []
    for entry in entries:
        band = _band_for_wavelength(float(entry["wavelength_nm"]), cfg["band_names"])
        for tarp, points in entry["native_rois"].items():
            source = entry.get("source_path")
            if not source:
                raise ValueError(f"{reference} B{band} 원영상 경로가 없어 포화 검사를 할 수 없습니다.")
            from .roi_studio_bridge import is_fit_roi
            result = scan_roi(source, points, entry["native_image_size"], entry.get("sampling"))
            raw_qc.append({"shot": reference, "band": band, "tarp": tarp, "raw_path": source,
                           "use_for_fit": int(is_fit_roi(manifest, reference, tarp, library[_library_key(tarp)])), **result})
    (output / "reference_raw_roi_saturation_qc.json").write_text(
        json.dumps({"reference_shot": reference, "max_fit_saturation_pct": 1.0, "rows": raw_qc}, ensure_ascii=False, indent=2), encoding="utf-8")
    blocked = [r for r in raw_qc if r["use_for_fit"] and r["saturated_pct"] >= 1.0]
    if blocked:
        detail = ", ".join(f"B{r['band']} {r['tarp']} {r['saturated_pct']:.2f}%" for r in blocked)
        raise ValueError(f"기준 영상 타프 원영상 포화로 ELC 계수 산출을 중단했습니다: {detail}. reference_raw_roi_saturation_qc.json 확인 후 기준 영상을 다시 선택하세요.")
    stats: list[dict] = []
    normalized = scan_raw(paths["ed"], shots={reference}) if is_general(cfg) else None
    for entry in entries:
        suffix = int(entry["band_suffix"])
        band = _band_for_wavelength(float(entry["wavelength_nm"]), cfg["band_names"])
        hits = ([normalized[reference, suffix]] if (reference, suffix) in normalized else []) if normalized is not None else (
            list(paths["ed"].rglob(f"{reference}_{suffix}.tif")) + list(paths["ed"].rglob(f"{reference}_{suffix}.tiff")))
        if len(hits) != 1:
            raise ValueError(f"기준 영상의 Ed·센서 교정 radiance가 없거나 중복됩니다: {reference}_{suffix}")
        if is_general(cfg):
            _checked_wavelength(hits[0], band, cfg["band_names"])
        for tarp, points in entry["native_rois"].items():
            key = _library_key(tarp)
            if key not in library:
                raise ValueError(f"타프 실측 반사도 라이브러리에 {tarp}가 없습니다.")
            exclusion = entry.get('native_exclusion_masks', {}).get(tarp)
            if tarp in ('PNU_36', 'PNU_56') and not exclusion:
                raise ValueError(f'{reference} {tarp} B{band}: 승인된 내부 화소 마스크가 없습니다.')
            from .roi_studio_bridge import filter_limits, is_fit_roi
            low, high = filter_limits(manifest, reference, tarp)
            values = _read_values(hits[0], points, entry["native_image_size"], low, high, exclusion, entry.get("sampling"))
            stats.append({"shot": reference, "tarp": tarp, "band": band, "band_name": cfg["band_names"][str(band)],
                          "wavelength_nm": float(entry["wavelength_nm"]), "n": int(values.size),
                          "radiance_mean": float(values.mean()), "radiance_median": float(np.median(values)),
                          "radiance_std": float(values.std()), "measured_reflectance": float(library[key][f"B{band:02d}"]),
                          "use_for_fit": int(is_fit_roi(manifest, reference, tarp, library[key]))})
    coefficients: list[dict] = []
    details: list[dict] = []
    for band in range(1, 11):
        rows = [row for row in stats if row["band"] == band and row["use_for_fit"]]
        if len(rows) < 2:
            raise ValueError(f"Band {band}: ELC 적합 타프가 최소 2개 필요합니다.")
        rho = np.asarray([row["measured_reflectance"] for row in rows])
        radiance = np.asarray([row["radiance_mean"] for row in rows])
        design = np.column_stack([rho, np.ones_like(rho)])
        if np.linalg.matrix_rank(design) < 2:
            raise ValueError(f"Band {band}: 서로 다른 실측 반사도의 타프가 필요합니다.")
        a, b = np.linalg.lstsq(design, radiance, rcond=None)[0]
        if not math.isfinite(a) or a <= 0:
            raise ValueError(f"Band {band}: L=a*rho+b의 a가 양수가 아닙니다.")
        predicted = (radiance - b) / a
        residual = predicted - rho
        rmse = float(np.sqrt(np.mean(residual ** 2)))
        bias = float(np.mean(residual))
        denom = float(np.sum((rho - rho.mean()) ** 2))
        r2 = 1 - float(np.sum(residual ** 2)) / denom if denom else math.nan
        coefficients.append({"band": band, "band_name": cfg["band_names"][str(band)], "a": float(a), "b": float(b),
                             "coefficient_convention": "L=a*rho+b", "fit_tarp_count": len(rows),
                             "fit_tarps": ";".join(row["tarp"] for row in rows), "rmse_reflectance": rmse,
                             "bias_reflectance": bias, "r2_reflectance": r2, "reference_shot": reference,
                             "roi_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()})
        for row, pred, resid in zip(rows, predicted, residual):
            details.append(row | {"predicted_reflectance": float(pred), "residual": float(resid)})
    coef_path = output / "elc_coefficients.csv"
    for path, rows in ((output / "tarp_radiance_statistics.csv", stats), (output / "elc_fit_details.csv", details), (coef_path, coefficients)):
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    table = "".join(f"<tr><td>{r['band']}</td><td>{html.escape(r['band_name'])}</td><td>{r['a']:.6g}</td><td>{r['b']:.6g}</td><td>{r['rmse_reflectance']*100:.3f}</td><td>{r['bias_reflectance']*100:.3f}</td><td>{r['r2_reflectance']:.4f}</td></tr>" for r in coefficients)
    report = output / "elc_coefficient_validation.html"
    report.write_text("<meta charset='utf-8'><style>body{font:16px 'Malgun Gothic';margin:28px;color:#182027}table{border-collapse:collapse;width:100%}th,td{padding:9px;border-bottom:1px solid #ccd4d9;text-align:center}th:first-child,td:first-child{text-align:left}</style><h1>ELC a·b 산출·검증</h1>" +
                      f"<p>기준 영상: <b>{reference}</b> · 계수 정의: <b>L = aρ + b</b> · 공간 trim 및 ROI 내부 P{percentile_low:g}–P{percentile_high:g} 적용</p><table><tr><th>Band</th><th>파장대</th><th>a</th><th>b</th><th>%RMSE</th><th>%Bias</th><th>R²</th></tr>{table}</table>", encoding="utf-8")
    return {"coefficient_csv": str(coef_path), "report": str(report), "reference_shot": reference}
