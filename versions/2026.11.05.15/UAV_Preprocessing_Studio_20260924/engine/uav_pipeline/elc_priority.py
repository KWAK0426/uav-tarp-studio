"""Apply ELC first to approved reference and held-out tarp shots, then audit them."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
from pathlib import Path

import numpy as np
from .band_scope import bands, describe

from .elc_fit import _band_for_wavelength, _current_manifest, _checked_wavelength, _sha, _library, _library_key, _read_values
from .preparation import locations, latest_candidate_selection
from .general_project import is_general, source_list_path, source_shots, scan_raw


def approved_shots(cfg):
    manifest_path = _current_manifest(cfg)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get('pixel_mask_review_version') != 1:
        raise ValueError('ROI 내부 화소 검수 이전 결과입니다. 제외 마스크를 승인하고 통계·ELC 계수를 다시 생성하세요.')
    reference = manifest.get("reference_shot")
    approved = sorted(shot for shot, image in manifest.get("images", {}).items() if image.get("approved"))
    if not reference or reference not in approved:
        raise ValueError("ELC 기준 영상의 ROI 승인이 필요합니다.")
    if len(approved) < 2:
        raise ValueError("독립 검증용으로 기준 영상 외의 ROI를 최소 한 장 승인하세요.")
    return manifest_path, manifest, reference, approved


def checked_coefficients(cfg, reference):
    coefficient_path = Path(cfg["preparation"]["coefficient_csv"])
    qc_path = locations(cfg)["coef"] / "reference_raw_roi_saturation_qc.json"
    if not qc_path.is_file():
        raise ValueError("기준 영상의 원영상 타프 포화 검사가 없습니다. ELC a·b를 다시 산출하세요.")
    qc = json.loads(qc_path.read_text(encoding="utf-8"))
    if (qc.get("reference_shot") != reference or qc_path.stat().st_mtime_ns >= coefficient_path.stat().st_mtime_ns
            or any(row["use_for_fit"] and row["saturated_pct"] >= qc["max_fit_saturation_pct"] for row in qc["rows"])):
        raise ValueError("기준 영상 타프 포화 검사에 실패했거나 기존 ELC 계수입니다. 기준 영상을 바꿔 ELC a·b를 다시 산출하세요.")
    with coefficient_path.open(encoding="utf-8-sig", newline="") as stream:
        coefficients = list(csv.DictReader(stream))
    if len(coefficients)!=len(bands(cfg)) or {int(row["band"]) for row in coefficients}!=set(bands(cfg)) or {row.get("reference_shot") for row in coefficients} != {reference}:
        raise ValueError("ELC 계수 기준 영상이 승인 ROI 기준 영상과 다릅니다. 계수를 다시 산출하세요.")
    approved_path = cfg["preparation"].get("approved_roi_manifest")
    if approved_path or is_general(cfg):
        manifest_path = _current_manifest(cfg)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if manifest.get("editor") == "TarpROIStudio" and any(row.get("roi_manifest_sha256") != sha for row in coefficients):
            raise ValueError("현재 승인 ROI로 산출한 ELC 계수가 아닙니다. a·b를 다시 산출하세요.")
    return coefficients


def check_sources(cfg, shots):
    root = locations(cfg)["ed"]
    if is_general(cfg):
        selected = {str(shot).upper() for shot in shots}
        if not selected or not selected <= source_shots(cfg):
            raise ValueError("General ELC inputs must be an explicit target subset")
        found = scan_raw(root, shots=selected)
        mapping = suffix_band_mapping(cfg)
        for shot in sorted(selected):
            for suffix, band in mapping.items():
                path = found.get((shot, suffix))
                if path is None or not path.stat().st_size:
                    raise ValueError(f"Missing General normalized input: {shot}_{suffix}")
                _checked_wavelength(path, band, cfg["band_names"])
        return found
    found = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (".tif", ".tiff"):
            continue
        parts = path.stem.rsplit("_", 1)
        if len(parts) == 2 and parts[0] in shots and parts[1].isdigit():
            key = (parts[0], int(parts[1]))
            if key in found:
                raise ValueError(f"우선 영상의 Ed·센서 교정 파일이 중복됩니다: {path.name}")
            found[key] = path
    expected = {(shot, band) for shot in shots for band in range(1, 11)}
    missing = sorted(key for key in expected if key not in found or found[key].stat().st_size == 0)
    if missing:
        raise ValueError(f"우선 영상의 Ed·센서 교정 10밴드가 부족합니다: {missing[:5]}")


def suffix_band_mapping(cfg):
    manifest_path = _current_manifest(cfg)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reference = manifest.get("reference_shot")
    transforms = json.loads(Path(manifest["transforms_json"]).read_text(encoding="utf-8"))
    entries = [entry for entry in transforms if entry["shot"] == reference]
    mapping = {int(entry["band_suffix"]): _band_for_wavelength(float(entry["wavelength_nm"]), cfg["band_names"])
               for entry in entries}
    if len(entries) != 10 or set(mapping) != set(range(1, 11)) or set(mapping.values()) != set(range(1, 11)):
        raise ValueError("기준 영상의 파일 접미번호와 10밴드 파장 대응이 완전하지 않습니다.")
    return mapping


def _signature(cfg, mapping):
    signature = {"coefficient_sha256": _sha(cfg["preparation"]["coefficient_csv"]),
                 "suffix_to_band": {str(k): v for k, v in sorted(mapping.items())}}
    if is_general(cfg):
        targets = source_shots(cfg)
        shots = latest_candidate_selection(cfg)[2] if cfg["preparation"].get("elc_only") else targets
        inputs = check_sources(cfg, shots)
        signature.update(general_identity=dict(cfg["general"]), source_list_path=str(source_list_path(cfg)),
                         source_list_sha256=_sha(source_list_path(cfg)), target_shots=sorted(targets),
                         roi_manifest_sha256=_sha(_current_manifest(cfg)),
                         normalized_inputs={f"{s}_{b}": [str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns]
                                            for (s, b), p in sorted(inputs.items()) if s in shots})
        plan = cfg["preparation"].get("integrated_plan_file")
        signature["integrated_plan_sha256"] = _sha(plan) if plan else None
    return signature


def _tiffs(root):
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".tif", ".tiff")]


def write_reuse_marker(cfg, mapping, shots):
    root = locations(cfg)["ref"]
    marker = root / "_elc_output_mapping_provenance.json"
    signature = _signature(cfg, mapping)
    existing = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
    if existing and all(existing.get(key) == value for key, value in signature.items()):
        return marker
    outputs = _tiffs(root)
    selected = [path for path in outputs if path.stem.rsplit("_", 1)[0].upper() in shots]
    if is_general(cfg):
        registry = scan_raw(root, shots=shots, required_suffixes=[s for s,b in mapping.items() if b in bands(cfg)])
        if set(registry) != {(s, b) for s in shots for b in mapping if mapping[b] in bands(cfg)}:
            raise ValueError("General priority reflectance outputs are incomplete or duplicated")
    if len(selected) != len(shots) * len(bands(cfg)):
        raise ValueError("우선 반사도 출력 10밴드가 완전해야 재개 기준을 기록할 수 있습니다.")
    cutoff = min(path.stat().st_mtime_ns for path in selected)
    legacy = [path for path in outputs if path not in selected]
    if existing and cutoff <= marker.stat().st_mtime_ns:
        raise ValueError("계수 변경 후 우선 영상을 모두 재생성해야 생성 기록을 갱신할 수 있습니다.")
    if any(path.stat().st_mtime_ns >= cutoff for path in legacy):
        raise ValueError("기존 출력과 우선 재생성 출력의 시각이 섞여 자동 재개 기준을 정할 수 없습니다.")
    marker.write_text(json.dumps(signature | {"reuse_after_ns": cutoff, "recomputed_shots": shots,
                                              "legacy_count": len(legacy)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return marker


def reuse_cutoff(cfg, mapping):
    root = locations(cfg)["ref"]
    marker = root / "_elc_output_mapping_provenance.json"
    if not marker.exists():
        if _tiffs(root):
            raise ValueError("기존 ELC 영상의 계수·파일번호 대응 기록이 없습니다. 기준·검증 영상을 우선 재생성하고 재개 기준을 확인하세요.")
        return None
    existing = json.loads(marker.read_text(encoding="utf-8"))
    if any(existing.get(key) != value for key, value in _signature(cfg, mapping).items()):
        raise ValueError("계수 또는 파일번호 대응이 기존 출력과 다릅니다. 기존 파일을 건너뛸 수 없습니다.")
    return int(existing["reuse_after_ns"])


def mark_full_complete(cfg, mapping):
    root = locations(cfg)["ref"]
    marker = root / "_elc_output_mapping_provenance.json"
    if not marker.exists():
        return
    existing = json.loads(marker.read_text(encoding="utf-8"))
    if any(existing.get(key) != value for key, value in _signature(cfg, mapping).items()):
        raise ValueError("전체 ELC 출력의 계수·밴드 대응이 재개 기록과 다릅니다.")
    cutoff = int(existing["reuse_after_ns"])
    outputs = _tiffs(root)
    if is_general(cfg):
        targets = source_shots(cfg)
        registry = scan_raw(root, shots=targets, required_suffixes=[s for s,b in mapping.items() if b in bands(cfg)])
        if set(registry) != {(s, b) for s in targets for b in mapping if mapping[b] in bands(cfg)}:
            raise ValueError("General full reflectance outputs do not cover the explicit target list")
        outputs = list(registry.values())
    stale = [path.name for path in outputs if path.stat().st_mtime_ns < cutoff]
    if stale:
        raise ValueError(f"이전의 잘못된 밴드 대응 TIFF가 아직 남아 있습니다: {stale[:5]}")
    existing["full_generation_complete"] = True
    marker.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "README_ELC_OUTPUT_STATUS.txt").write_text(
        "All GPS-retained ELC TIFFs were regenerated or verified with the recorded "
        "MX Dual suffix-to-wavelength mapping. Pixel/tarp scientific validation "
        "is separate; review elc_priority_reference_validation.html before downstream use.\n",
        encoding="utf-8")


def write_report(cfg, manifest_path, manifest, reference, shots):
    paths = locations(cfg)
    checked_coefficients(cfg, reference)
    library_path = Path(cfg["preparation"]["tarp_reflectance_csv"])
    library = _library(library_path)
    transforms = json.loads(Path(manifest["transforms_json"]).read_text(encoding="utf-8"))
    low = float(manifest.get("percentile_low", 5))
    high = float(manifest.get("percentile_high", 95))
    rows = []
    normalized = (scan_raw(paths["ref"], shots=shots, required_suffixes=[
        s for s,b in suffix_band_mapping(cfg).items() if b in bands(cfg)]) if is_general(cfg) else None)
    for entry in transforms:
        shot = entry["shot"]
        if shot not in shots:
            continue
        band = _band_for_wavelength(float(entry["wavelength_nm"]), cfg["band_names"])
        if band not in bands(cfg):continue
        suffix = int(entry["band_suffix"])
        hits = ([normalized[shot, suffix]] if (shot, suffix) in normalized else []) if normalized is not None else (
            list(paths["ref"].rglob(f"{shot}_{suffix}.tif")) + list(paths["ref"].rglob(f"{shot}_{suffix}.tiff")))
        if len(hits) != 1:
            raise ValueError(f"우선 반사도 영상이 없거나 중복됩니다: {shot}_{suffix}")
        for tarp, points in entry["native_rois"].items():
            key = _library_key(tarp)
            measured = float(library[key][f"B{band:02d}"])
            exclusion = entry.get('native_exclusion_masks', {}).get(tarp)
            if tarp in ('PNU_36', 'PNU_56') and not exclusion:
                raise ValueError(f'{shot} {tarp} B{band}: 승인 제외 마스크가 없습니다.')
            from .roi_studio_bridge import filter_limits
            roi_low, roi_high = filter_limits(manifest, shot, tarp)
            values = _read_values(hits[0], points, entry["native_image_size"], roi_low, roi_high, exclusion, entry.get("sampling"))
            observed = float(values.mean())
            from .tarp_tool.code.saturation_qc import scan_roi
            raw_qc = scan_roi(entry['source_path'], entry.get('native_outer_rois', {}).get(tarp, points),
                              entry['native_image_size'], entry.get('sampling'))
            settings = manifest.get("images", {}).get(shot, {}).get("roi_settings", {}).get(tarp, {})
            role = "held_out_reference_roi" if shot == reference and settings.get("role") == "validation" else ("fit_reference" if shot == reference else "independent_validation")
            if raw_qc['saturated_pct'] >= 1.0:
                role = 'excluded_saturated_' + role
            rows.append({"role": role, "raw_outer_saturation_pct": raw_qc['saturated_pct'],
                         "shot": shot, "tarp": tarp, "band": band, "band_name": cfg["band_names"][str(band)],
                         "n_pixels": len(values), "measured_reflectance": measured,
                         "observed_reflectance": observed, "bias": observed - measured,
                         "source_tif": str(hits[0])})
    if not rows or {row["shot"] for row in rows} != set(shots):
        raise ValueError("승인된 기준·검증 영상의 반사도 결과가 모두 필요합니다.")
    output = paths["coef"]
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / "elc_priority_reference_validation.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    marker = paths["ref"] / "_elc_output_mapping_provenance.json"
    provenance_warning = ""
    if marker.exists():
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        signature = _signature(cfg, suffix_band_mapping(cfg))
        if any(recorded.get(key) != value for key, value in signature.items()):
            provenance_warning = "<p class='warning'>출력 이력의 계수와 현재 계수가 다릅니다. 전체 반사도 생성·후속 분석에 사용하지 마세요.</p>"
    limit = math.ceil(max(max(row["measured_reflectance"], row["observed_reflectance"]) for row in rows) * 11) / 10
    floor = math.floor(min(0.0, min(row["observed_reflectance"] for row in rows)) * 10) / 10
    colors = {"PNU_3": "#304f94", "PNU_36": "#008b70", "PNU_56": "#b95720"}
    reference_values = {(row["tarp"], row["band"]): row["observed_reflectance"]
                        for row in rows if row["shot"] == reference}
    def x_coord(value):
        return 150 + 400 * (value - floor) / (limit - floor)
    def y_coord(value):
        return 490 - 400 * (value - floor) / (limit - floor)
    def scatter_svg(current, against_reference=False):
        current_rows = [row for row in rows if row["shot"] == current]
        if against_reference:
            current_rows = [row for row in current_rows if (row["tarp"], row["band"]) in reference_values]
        background = []
        title = (f"{current} | 기준 {reference} 대비" if against_reference
                 else f"{current} | RAMSES 실측 대비")
        ticks = "".join(
            f"<path d='M{x_coord(tick):.1f} 90V490M150 {y_coord(tick):.1f}H550' stroke='#e4e9eb'/>"
            f"<text x='{x_coord(tick):.1f}' y='523' text-anchor='middle' font-size='18'>{tick:.2f}</text>"
            f"<text x='125' y='{y_coord(tick)+8:.1f}' text-anchor='end' font-size='18'>{tick:.2f}</text>"
            for tick in sorted(set(np.linspace(0, limit, 5)) | {floor}))
        circles = []
        for row in background + current_rows:
            color = colors.get(row["tarp"], "#594782")
            is_reference = row["shot"] == reference and not against_reference
            x_value = reference_values[(row["tarp"], row["band"])] if against_reference else row["measured_reflectance"]
            caption = html.escape(
                f"{row['shot']} | {row['tarp']} | B{row['band']} {row['band_name']} | "
                f"{'기준 영상' if against_reference else 'RAMSES 실측'} {x_value:.4f} | "
                f"현재 영상 {row['observed_reflectance']:.4f}")
            circles.append(
                f"<circle cx='{x_coord(x_value):.1f}' "
                f"cy='{y_coord(row['observed_reflectance']):.1f}' r='{'8' if is_reference else '5.5'}' "
                f"fill='{'white' if is_reference else color}' stroke='{color}' stroke-width='2' opacity='.85'>"
                f"<title>{caption}</title></circle>")
        tarp_legend = "".join(
            f"<circle cx='{x}' cy='608' r='8' fill='{colors.get(tarp, '#594782')}'/>"
            f"<text x='{x+15}' y='617' font-size='19'>{html.escape(tarp)}</text>"
            for x, tarp in zip((80, 265, 460), sorted({row["tarp"] for row in current_rows})))
        role_legend = ("<path d='M540 650h35' stroke='#24343b' stroke-width='3' stroke-dasharray='9 7'/>"
                       "<text x='585' y='659' font-size='19'>1:1</text>")
        return (
            f"<svg viewBox='0 0 700 680' role='img' aria-label='{html.escape(current)} 전체 밴드 "
            f"{'기준 영상 대비' if against_reference else '실측 대비'} ELC 반사도 산점도'>"
            f"<text x='350' y='49' text-anchor='middle' font-size='38' font-weight='700'>{html.escape(title)}</text>"
            f"{ticks}<rect x='150' y='90' width='400' height='400' fill='none' stroke='#52656c' stroke-width='2'/>"
            f"<path d='M{x_coord(0):.1f} 90V490M150 {y_coord(0):.1f}H550' "
            "stroke='#697f89' stroke-width='2' stroke-dasharray='3 5'/>"
            f"<path d='M{x_coord(floor):.1f} {y_coord(floor):.1f}L{x_coord(limit):.1f} {y_coord(limit):.1f}' "
            "fill='none' stroke='#24343b' stroke-width='3' stroke-dasharray='9 7'/>"
            f"{''.join(circles)}<text x='350' y='570' text-anchor='middle' font-size='17'>"
            f"X: {'기준 영상 ROI 반사도' if against_reference else 'RAMSES 실측 반사도'} (무차원)</text>"
            f"<text transform='translate(42 290) rotate(-90)' text-anchor='middle' font-size='17'>"
            f"Y: {'검증 영상' if against_reference else 'ELC'} 반사도 ROI 평균 (무차원)</text>"
            f"{tarp_legend}{role_legend}</svg>")
    shot_sections = []
    band_headers = "".join(
        f"<th>{html.escape(cfg['band_names'][str(band)].split('_')[-1])}"
        f"<small>B{band} {html.escape(cfg['band_names'][str(band)].rsplit('_', 1)[0])}</small></th>"
        for band in bands(cfg)) + "<th>Mean<small>산출 밴드 평균</small></th>"
    def matrix_table(matrix_rows, title):
        return (f"<h3>{html.escape(title)}</h3><div class='matrix'><table>"
                f"<thead><tr><th>타프</th><th>지표</th>{band_headers}</tr></thead>"
                f"<tbody>{''.join(matrix_rows)}</tbody></table></div>")
    for shot in [reference] + [value for value in shots if value != reference]:
        shot_rows = [row for row in rows if row["shot"] == shot]
        role = "계수 적합용 (독립 검증 아님)" if shot == reference else "독립 검증"
        reference_chart = f"<figure>{scatter_svg(shot, True)}</figure>" if shot != reference else ""
        measured_rows = []
        reference_rows = []
        for tarp in sorted({row["tarp"] for row in shot_rows}):
            by_band = {row["band"]: row for row in shot_rows if row["tarp"] == tarp}
            if set(by_band) != set(bands(cfg)):
                raise ValueError(f"{shot} {tarp}: 검증 표에 필요한 산출 밴드가 완전하지 않습니다.")
            ordered = [by_band[band] for band in bands(cfg)]
            measured_metrics = [
                ("RAMSES 실측", [row["measured_reflectance"] for row in ordered]),
                ("ELC 반사도 (ROI 평균)", [row["observed_reflectance"] for row in ordered]),
                ("실측 대비 %Bias", [row["bias"] * 100 for row in ordered]),
                ("실측 대비 %RMSE", [abs(row["bias"]) * 100 for row in ordered]),
            ]
            for index, (label, values) in enumerate(measured_metrics):
                group = f"<th rowspan='{len(measured_metrics)}'>{html.escape(tarp)}</th>" if index == 0 else ""
                cells = "".join(f"<td>{value:+.3f}</td>" if "%Bias" in label else f"<td>{value:.3f}</td>"
                                for value in [*values, sum(values) / len(values)])
                measured_rows.append(f"<tr>{group}<th>{html.escape(label)}</th>{cells}</tr>")
            if shot != reference and all((tarp, row["band"]) in reference_values for row in ordered):
                differences = [row["observed_reflectance"] - reference_values[(tarp, row["band"])]
                               for row in ordered]
                reference_metrics = [
                    ("기준 대비 %Bias", [value * 100 for value in differences]),
                    ("기준 대비 %RMSE", [abs(value) * 100 for value in differences]),
                ]
                for index, (label, values) in enumerate(reference_metrics):
                    group = f"<th rowspan='2'>{html.escape(tarp)}</th>" if index == 0 else ""
                    cells = "".join(f"<td>{value:+.3f}</td>" if "%Bias" in label else f"<td>{value:.3f}</td>"
                                    for value in [*values, sum(values) / len(values)])
                    reference_rows.append(f"<tr>{group}<th>{html.escape(label)}</th>{cells}</tr>")
        shot_sections.append(
            f"<section class='shot'><h2>{html.escape(shot)} <small>{role}</small></h2>"
            "<p>산점도에는 승인 타프의 산출 밴드 관측을 표시합니다. 기준 영상 대비는 같은 타프가 있을 때만 표시합니다. 점선은 1:1이며, "
            "점에 마우스를 올리면 타프·밴드·값이 표시됩니다.</p>"
            f"<div class='charts'><figure>{scatter_svg(shot)}</figure>{reference_chart}</div>"
            f"{matrix_table(measured_rows, 'RAMSES 실측 대비')}"
            f"{matrix_table(reference_rows, '기준 영상 대비 (기준값은 기준 영상 표 참조)') if reference_rows else ''}"
            "</section>")
    report = output / "elc_priority_reference_validation.html"
    prepost = output / "elc_raw_vs_ed_sensor_validation.html"
    prepost_link = ("<p><a href='elc_raw_vs_ed_sensor_validation.html'>"
                    "현재 a,b 동일 적용: 방사보정만 vs Ed·센서 교정 비교 보기</a></p>") if prepost.is_file() else ""
    report.write_text(
        "<!doctype html><html lang='ko'><meta charset='utf-8'><title>ELC 우선 검증</title>"
        "<style>body{font:16px 'Malgun Gothic',sans-serif;color:#19252a;background:#f7f9fa;margin:24px auto;max-width:1250px}"
        "h1{font-size:26px}h2{font-size:20px;margin-top:30px}p{line-height:1.5}"
        "table{border-collapse:collapse;width:100%;background:white}th,td{padding:9px;border-bottom:1px solid #dce3e6;text-align:right}"
        "th:first-child,td:first-child{text-align:left}th{background:#e7eef0}thead th{position:sticky;top:0}"
        ".matrix{overflow-x:auto;margin:18px 0}.matrix table{min-width:1200px;font-size:14px}"
        ".matrix th:first-child{min-width:90px}.matrix th:nth-child(2){min-width:150px}"
        ".matrix thead small{display:block;font-size:11px;font-weight:400;color:#52656c}"
        ".shot{border-top:1px solid #cbd8dc;padding:12px 0 20px}.shot h2 small{font-size:14px;color:#556b73}"
        ".charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}"
        ".charts figure{margin:0}.charts svg{width:100%;background:white}"
        "@media(max-width:800px){.charts{grid-template-columns:1fr}}"
        ".warning{padding:12px;border-left:4px solid #bd3c2b;background:#fff1ee;color:#782519}</style>"
        f"<h1>ELC 기준·독립 검증 영상</h1><p>기준(적합용): <b>{html.escape(reference)}</b> · "
        f"독립 검증: <b>{html.escape(', '.join(s for s in shots if s != reference))}</b><br>"
        f"ROI 내부 P{low:g}–P{high:g} · 계수 L = aρ + b · 영상별 타프 ROI 평균 반사도와 RAMSES 실측 비교</p>"
        f"{describe(cfg)}{provenance_warning}"
        f"{prepost_link}"
        "<h2>영상·타프별 검증</h2>"
        "<p>산점도는 음수값까지 실제 좌표에 표시하며 x=0·y=0 기준선은 점선으로 표시합니다. "
        f"산출 {len(bands(cfg))}밴드·{len({row['tarp'] for row in rows})}타프를 표시하며 색은 타프를 구분합니다. "
        "기준 영상 대비 오차는 같은 타프·밴드의 기준 영상 ROI 값과 비교합니다. "
        "실측 대비와 기준 영상 대비 표는 분리하고, 파장대는 가로 열로 표시합니다. 기준 영상 값은 반복하지 않습니다. "
        "Mean은 해당 행의 산출 밴드 산술평균입니다. %RMSE 행의 Mean도 밴드별 값의 평균이며 통합 RMSE가 아닙니다. "
        "각 타프·밴드의 ROI 평균 1개를 비교하므로 "
        "해당 셀의 %RMSE는 |%Bias|입니다. 오차 단위는 반사도 백분율포인트(p.p.)입니다.</p>"
        f"{''.join(shot_sections)}"
        "<p>기준 영상은 계수 적합에 사용됐으며 독립 검증이 아닙니다. "
        "%RMSE/%Bias는 실측값 대비 상대오차(%)가 아닙니다. "
        f"ROI 출처: {html.escape(str(manifest_path))}<br>전체 데이터 CSV: {html.escape(str(csv_path))}</p></html>",
        encoding="utf-8")
    return report
