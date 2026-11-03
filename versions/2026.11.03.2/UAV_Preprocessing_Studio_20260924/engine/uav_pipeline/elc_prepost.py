"""Compare raw-radiometric and Ed+sensor-corrected tarp ELC paths."""
from __future__ import annotations

import csv
import html
import json
import math
from pathlib import Path

import numpy as np

from .elc_fit import _band_for_wavelength, _library, _library_key, _read_values
from .elc_priority import approved_shots, checked_coefficients
from .preparation import locations


def fit_forward(rows, radiance_key):
    used = [row for row in rows if row["use_for_fit"]]
    rho = np.asarray([row["measured_reflectance"] for row in used], dtype=float)
    radiance = np.asarray([row[radiance_key] for row in used], dtype=float)
    if len(used) < 2 or np.linalg.matrix_rank(np.column_stack([rho, np.ones_like(rho)])) < 2:
        raise ValueError("ELC 적합에는 반사도가 서로 다른 타프 2개 이상이 필요합니다.")
    a, b = np.linalg.lstsq(np.column_stack([rho, np.ones_like(rho)]), radiance, rcond=None)[0]
    if not math.isfinite(a) or not math.isfinite(b) or a <= 0:
        raise ValueError(f"{radiance_key}: 양의 기울기를 가진 ELC 계수를 구할 수 없습니다.")
    return float(a), float(b)


def _files(root, expected):
    found = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in (".tif", ".tiff") and path.stem in expected:
            if path.stem in found:
                raise ValueError(f"영상 중복: {path.stem} ({root})")
            found[path.stem] = path
    missing = expected - found.keys()
    if missing:
        raise ValueError(f"영상 누락: {sorted(missing)[:5]} ({root})")
    return found


def _table(rows, title, metrics, band_names):
    heads = "".join(
        f"<th>{html.escape(band_names[str(band)].split('_')[-1])}<small>B{band}</small></th>"
        for band in range(1, 11)) + "<th>Mean<small>10밴드 평균</small></th>"
    body = []
    for tarp in sorted({row["tarp"] for row in rows}):
        by_band = {row["band"]: row for row in rows if row["tarp"] == tarp}
        if set(by_band) != set(range(1, 11)):
            raise ValueError(f"{tarp}: 10밴드 검증값이 완전하지 않습니다.")
        for index, (label, key) in enumerate(metrics):
            values = [by_band[band][key] for band in range(1, 11)]
            values.append(sum(values) / 10)
            cells = "".join(f"<td>{value:+.3f}</td>" if "Bias" in label else f"<td>{value:.3f}</td>"
                            for value in values)
            tarp_cell = f"<th rowspan='{len(metrics)}'>{html.escape(tarp)}</th>" if index == 0 else ""
            body.append(f"<tr>{tarp_cell}<th>{html.escape(label)}</th>{cells}</tr>")
    return (f"<h3>{html.escape(title)}</h3><div class='scroll'><table>"
            f"<thead><tr><th>타프</th><th>지표</th>{heads}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def _scatter(rows, key, title, colors, floor, limit):
    def x(value):
        return 150 + 400 * (value - floor) / (limit - floor)
    def y(value):
        return 490 - 400 * (value - floor) / (limit - floor)
    ticks = "".join(
        f"<path d='M{x(t):.1f} 90V490M150 {y(t):.1f}H550' stroke='#e4e9eb'/>"
        f"<text x='{x(t):.1f}' y='523' text-anchor='middle' font-size='18'>{t:.2f}</text>"
        f"<text x='125' y='{y(t)+7:.1f}' text-anchor='end' font-size='18'>{t:.2f}</text>"
        for t in sorted(set(np.linspace(0, limit, 5)) | {floor}))
    dots = "".join(
        f"<circle cx='{x(row['measured_reflectance']):.1f}' cy='{y(row[key]):.1f}' r='5.5' "
        f"fill='{colors[row['tarp']]}' stroke='{colors[row['tarp']]}' stroke-width='2'>"
        f"<title>{html.escape(row['shot'])} | {html.escape(row['tarp'])} | "
        f"B{row['band']} {html.escape(row['band_name'])} | 실측 {row['measured_reflectance']:.4f} | "
        f"ELC {row[key]:.4f}</title></circle>" for row in rows)
    legend = "".join(
        f"<circle cx='{at}' cy='610' r='6' fill='{color}'/>"
        f"<text x='{at+14}' y='617' font-size='19'>{html.escape(tarp)}</text>"
        for at, (tarp, color) in zip((80, 265, 460), colors.items()))
    return (f"<svg viewBox='0 0 700 670' role='img' aria-label='{html.escape(title)}'>"
            f"<text x='350' y='49' text-anchor='middle' font-size='28' font-weight='700'>{html.escape(title)}</text>"
            f"{ticks}<rect x='150' y='90' width='400' height='400' fill='none' stroke='#52656c'/>"
            f"<path d='M{x(0):.1f} 90V490M150 {y(0):.1f}H550' stroke='#697f89' stroke-width='2' stroke-dasharray='3 5'/>"
            f"<path d='M{x(floor):.1f} {y(floor):.1f}L{x(limit):.1f} {y(limit):.1f}' "
            "fill='none' stroke='#24343b' stroke-width='3' stroke-dasharray='9 7'/>"
            f"{dots}<text x='350' y='566' text-anchor='middle' font-size='17'>X: RAMSES 실측 반사도</text>"
            "<text transform='translate(42 290) rotate(-90)' text-anchor='middle' font-size='17'>"
            f"Y: ELC 반사도 ROI 평균</text>{legend}</svg>")


def compare(cfg):
    paths = locations(cfg)
    manifest_path, manifest, reference, shots = approved_shots(cfg)
    current_ab = {int(row["band"]): (float(row["a"]), float(row["b"]))
                  for row in checked_coefficients(cfg, reference)}
    transforms = json.loads(Path(manifest["transforms_json"]).read_text(encoding="utf-8"))
    entries = [entry for entry in transforms if entry["shot"] in shots]
    expected = {f"{entry['shot']}_{int(entry['band_suffix'])}" for entry in entries}
    if len(expected) != len(shots) * 10:
        raise ValueError("승인 영상마다 native ROI 10밴드가 필요합니다.")
    raw_files = _files(paths["rad"], expected)
    corrected_files = _files(paths["ed"], expected)
    library = _library(Path(cfg["preparation"]["tarp_reflectance_csv"]))
    low = float(manifest.get("percentile_low", 5))
    high = float(manifest.get("percentile_high", 95))
    rows = []
    for entry in entries:
        shot = entry["shot"]
        suffix = int(entry["band_suffix"])
        band = _band_for_wavelength(float(entry["wavelength_nm"]), cfg["band_names"])
        stem = f"{shot}_{suffix}"
        for tarp, points in entry["native_rois"].items():
            target = library[_library_key(tarp)]
            exclusion = entry.get('native_exclusion_masks', {}).get(tarp)
            if tarp in ('PNU_36', 'PNU_56') and not exclusion:
                raise ValueError(f'{shot} {tarp} B{band}: 승인 제외 마스크가 없습니다.')
            from .roi_studio_bridge import filter_limits
            roi_low, roi_high = filter_limits(manifest, shot, tarp)
            raw = _read_values(raw_files[stem], points, entry["native_image_size"], roi_low, roi_high, exclusion, entry.get("sampling"))
            corrected = _read_values(corrected_files[stem], points, entry["native_image_size"], roi_low, roi_high, exclusion, entry.get("sampling"))
            rows.append({"shot": shot, "role": "fit_reference" if shot == reference else "independent_validation",
                         "tarp": tarp, "band": band, "band_name": cfg["band_names"][str(band)],
                         "measured_reflectance": float(target[f"B{band:02d}"]),
                         "use_for_fit": int(target["use_for_fit"]),
                         "raw_radiance": float(raw.mean()), "corrected_radiance": float(corrected.mean()),
                         "raw_pixels": len(raw), "corrected_pixels": len(corrected),
                         "raw_tif": str(raw_files[stem]), "corrected_tif": str(corrected_files[stem])})
    if len(rows) != len(shots) * 10 * 3:
        raise ValueError("선택한 영상의 타프 3개 x 10밴드가 모두 필요합니다.")
    for row in rows:
        a, b = current_ab[row["band"]]
        row["fixed_raw_reflectance"] = (row["raw_radiance"] - b) / a
        row["fixed_corrected_reflectance"] = (row["corrected_radiance"] - b) / a
        row["fixed_raw_bias_pp"] = 100 * (row["fixed_raw_reflectance"] - row["measured_reflectance"])
        row["fixed_corrected_bias_pp"] = 100 * (row["fixed_corrected_reflectance"] - row["measured_reflectance"])
    coefficients = []
    for band in range(1, 11):
        base = [row for row in rows if row["shot"] == reference and row["band"] == band]
        a_raw, b_raw = fit_forward(base, "raw_radiance")
        a_corrected, b_corrected = fit_forward(base, "corrected_radiance")
        coefficients.append({"band": band, "band_name": cfg["band_names"][str(band)],
                             "reference_shot": reference, "raw_a": a_raw, "raw_b": b_raw,
                             "ed_sensor_a": a_corrected, "ed_sensor_b": b_corrected,
                             "coefficient_convention": "L=a*rho+b"})
        for row in (row for row in rows if row["band"] == band):
            row["raw_elc_reflectance"] = (row["raw_radiance"] - b_raw) / a_raw
            row["ed_sensor_elc_reflectance"] = (row["corrected_radiance"] - b_corrected) / a_corrected
            row["raw_bias_pp"] = 100 * (row["raw_elc_reflectance"] - row["measured_reflectance"])
            row["ed_sensor_bias_pp"] = 100 * (row["ed_sensor_elc_reflectance"] - row["measured_reflectance"])
    baseline = {(row["tarp"], row["band"]): row for row in rows if row["shot"] == reference}
    for row in rows:
        base = baseline[(row["tarp"], row["band"])]
        row["raw_vs_reference_pp"] = 100 * (row["raw_elc_reflectance"] - base["raw_elc_reflectance"])
        row["ed_sensor_vs_reference_pp"] = 100 * (row["ed_sensor_elc_reflectance"] - base["ed_sensor_elc_reflectance"])
    output = paths["coef"]
    output.mkdir(parents=True, exist_ok=True)
    data_path = output / "elc_raw_vs_ed_sensor_validation.csv"
    coef_path = output / "elc_raw_vs_ed_sensor_coefficients.csv"
    for path, records in ((data_path, rows), (coef_path, coefficients)):
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
    floor = math.floor(min(0.0, *(row[key] for row in rows for key in
                                  ("raw_elc_reflectance", "ed_sensor_elc_reflectance",
                                   "fixed_raw_reflectance", "fixed_corrected_reflectance"))) * 10) / 10
    limit = math.ceil(max(row[key] for row in rows for key in
                          ("measured_reflectance", "raw_elc_reflectance", "ed_sensor_elc_reflectance",
                           "fixed_raw_reflectance", "fixed_corrected_reflectance")) * 11) / 10
    colors = {"PNU_3": "#304f94", "PNU_36": "#008b70", "PNU_56": "#b95720"}
    fixed_sections = []
    sections = []
    for shot in [reference] + [item for item in shots if item != reference]:
        subset = [row for row in rows if row["shot"] == shot]
        fixed_charts = (f"<div class='charts'><figure>{_scatter(subset, 'fixed_raw_reflectance', '방사보정만 · 현재 a,b', colors, floor, limit)}</figure>"
                        f"<figure>{_scatter(subset, 'fixed_corrected_reflectance', 'Ed+센서 교정 · 현재 a,b', colors, floor, limit)}</figure></div>")
        fixed_metrics = [("RAMSES 실측", "measured_reflectance"),
                         ("방사보정만 · 현재 a,b", "fixed_raw_reflectance"),
                         ("Ed+센서 교정 · 현재 a,b", "fixed_corrected_reflectance"),
                         ("방사보정만 %Bias", "fixed_raw_bias_pp"),
                         ("Ed+센서 교정 %Bias", "fixed_corrected_bias_pp")]
        fixed_sections.append(f"<section><h2>{html.escape(shot)} <small>{'계수 적합용' if shot == reference else '독립 검증'}</small></h2>"
                              f"{fixed_charts}{_table(subset, '같은 a,b 적용 · RAMSES 실측 대비', fixed_metrics, cfg['band_names'])}</section>")
        measured = [("RAMSES 실측", "measured_reflectance"),
                    ("방사보정만: ELC 반사도", "raw_elc_reflectance"),
                    ("Ed+센서 교정: ELC 반사도", "ed_sensor_elc_reflectance"),
                    ("방사보정만 %Bias", "raw_bias_pp"),
                    ("Ed+센서 교정 %Bias", "ed_sensor_bias_pp")]
        reference_metrics = [("방사보정만 기준 대비 %Bias", "raw_vs_reference_pp"),
                             ("Ed+센서 교정 기준 대비 %Bias", "ed_sensor_vs_reference_pp")]
        charts = (f"<div class='charts'><figure>{_scatter(subset, 'raw_elc_reflectance', '방사보정만', colors, floor, limit)}</figure>"
                  f"<figure>{_scatter(subset, 'ed_sensor_elc_reflectance', 'Ed+센서 교정', colors, floor, limit)}</figure></div>")
        sections.append(f"<section><h2>{html.escape(shot)} "
                        f"<small>{'계수 적합용 (독립 검증 아님)' if shot == reference else '독립 검증'}</small></h2>"
                        f"{charts}{_table(subset, 'RAMSES 실측 대비', measured, cfg['band_names'])}"
                        f"{_table(subset, '기준 영상 대비', reference_metrics, cfg['band_names']) if shot != reference else ''}"
                        "</section>")
    report = output / "elc_raw_vs_ed_sensor_validation.html"
    report.write_text(
        "<!doctype html><html lang='ko'><meta charset='utf-8'><title>ELC 보정 전후 비교</title>"
        "<style>body{font:16px 'Malgun Gothic',sans-serif;color:#19252a;background:#f7f9fa;margin:24px auto;max-width:1250px}"
        "h1{font-size:26px}h2{font-size:20px}h2 small{font-size:14px;color:#52656c}h3{font-size:18px}"
        "p{line-height:1.5}section{border-top:1px solid #cbd8dc;padding:16px 0}"
        ".charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}"
        ".charts figure{margin:0}.charts svg{width:100%;background:white}"
        ".scroll{overflow-x:auto;margin:14px 0}table{border-collapse:collapse;min-width:1250px;width:100%;background:white}"
        "th,td{padding:9px;border-bottom:1px solid #dce3e6;text-align:right}th:first-child,td:first-child{text-align:left}"
        "th{background:#e7eef0}th small{display:block;font-weight:400;font-size:11px}"
        ".warning{padding:12px;border-left:4px solid #bd3c2b;background:#fff1ee;color:#782519}"
        "@media(max-width:800px){.charts{grid-template-columns:1fr}}</style>"
        "<h1>타프 ELC: 방사보정만 vs Ed·센서 교정</h1>"
        f"<p>기준(적합용): <b>{html.escape(reference)}</b> · 독립 검증: "
        f"<b>{html.escape(', '.join(shot for shot in shots if shot != reference))}</b><br>"
        f"동일한 승인 ROI, 같은 타프 실측값, ROI 내부 P{low:g}–P{high:g}를 두 경로에 적용했습니다. "
        "1절은 현재 생산용 a,b를 양쪽에 동일 적용합니다. 2절은 경로별로 기준 영상에서 "
        "L=aρ+b 계수를 별도로 적합해 비교합니다.</p>"
        "<p class='warning'>같은 a,b를 원 방사보정 radiance에 적용하는 것은 보정 기준이 다른 입력에 "
        "계수를 대입하는 진단용 비교입니다. 그 결과를 정식 반사도로 해석하거나 Ed 단독 효과로 해석하지 마세요. "
        "Ed+센서 경로에는 두 보정이 함께 포함됩니다.</p>"
        "<p>그래프는 전체 10밴드·3타프를 표시합니다. 타프별·밴드별 수치는 표와 CSV에서 확인하세요. "
        "Mean은 해당 행의 10밴드 산술평균입니다. %Bias와 기준 대비 값은 반사도 백분율포인트(p.p.)입니다.</p>"
        "<h2>1. 현재 a,b를 양쪽에 동일 적용</h2>"
        f"{''.join(fixed_sections)}"
        "<h2>2. 각 경로에서 a,b를 별도로 적합한 진단</h2>"
        f"{''.join(sections)}<p>ROI: {html.escape(str(manifest_path))}<br>"
        f"상세 CSV: {html.escape(str(data_path))}<br>별도 적합 계수 CSV: {html.escape(str(coef_path))}</p></html>",
        encoding="utf-8")
    return {"report": report, "data_csv": data_path, "coefficients_csv": coef_path, "rows": len(rows)}
