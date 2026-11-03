"""Synthetic 02 -> native ROI -> 04 settings -> real ELC/validation regression."""
import copy
import csv
import json
from pathlib import Path
import re
import sys
import time
import uuid

import cv2
import numpy as np
from osgeo import gdal
import tifffile

ROOT = Path(__file__).resolve().parent.parent
STUDIO = ROOT / "UAV_Preprocessing_Studio_20260924"
sys.path.insert(0, str(STUDIO / "engine"))
from uav_pipeline.roi_studio_bridge import Session, atomic_json, read_json, pixel_mask
from uav_pipeline.preparation import locations, build
from uav_pipeline.elc_fit import fit, _latest_manifest, _read_values
from uav_pipeline.elc_priority import approved_shots, write_report, checked_coefficients
from core import Raster, inset, statistics


def fixture(folder):
    folder.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from distribution import preparation_template
    cfg = preparation_template()
    cfg["dataset"].update(root=str(folder), date="2026_07_16_Synthetic", flight="1st")
    cycle = folder / "2026_07_16_Synthetic/02_Processed/1st_260716"
    cycle.mkdir(parents=True)
    raw = folder / "raw/000"
    raw.mkdir(parents=True)
    gps = folder / "GPS.txt"
    shots = ["IMG_0001", "IMG_0002"]
    gps.write_text("\n".join(shot+"_6.tif 127 35 100" for shot in shots), encoding="utf-8")
    library = folder / "tarp_reflectance.csv"
    with library.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["key", "use_for_fit"]+[f"B{i:02d}" for i in range(1, 11)])
        w.writeheader()
        for name, rho in (("A", .1), ("B", .4), ("C", .6)):
            w.writerow({"key": name, "use_for_fit": 1, **{f"B{i:02d}": rho for i in range(1, 11)}})
    cfg["preparation"].update(raw_directory=str(raw.parent), gps_path=str(gps), tarp_reflectance_csv=str(library), reference_shot=shots[0])
    gains = folder / "synthetic_gains.csv"
    gains.write_text("band,gain\n" + "".join(f"{i},1\n" for i in range(1, 11)), encoding="utf-8")
    cfg["preparation"]["gain_csv"] = str(gains)
    cfg["geometry"].update(gps_path=str(gps), dem_path="", frame_time_source_directory=str(raw.parent))
    cfg["photos"]["directory"] = str(folder / "photos")
    Path(cfg["photos"]["directory"]).mkdir()
    cfg["dataset"]["paths"] = {key: "" for key in cfg["dataset"]["paths"]}
    cfg["dataset"]["paths"].update(raw=str(raw.parent), gps=str(gps), photos=cfg["photos"]["directory"])
    p = locations(cfg)
    context_root = p["roi"] / "workflow/synthetic_context"
    context = {"identity": "synthetic_context", "date": cfg["dataset"]["date"], "flight": "1st",
               "label": "Synthetic only · 1st", "output": str(context_root), "image_source": str(raw.parent),
               "raw": str(raw.parent), "radiance": str(p["rad"]), "source_kind": "raw_DN_boundary_only"}
    atomic_json(context_root / "context.json", context)
    atomic_json(context_root / "selections/selected_test.json", {"selected": [{"shot": s} for s in shots]})
    rng = np.random.default_rng(42)
    noise = rng.integers(0, 8000, (384, 512), dtype=np.uint16)
    noise = cv2.GaussianBlur(noise, (3, 3), .5)
    polygons = [np.float32([[60, 60], [170, 60], [170, 150], [60, 150]]),
                np.float32([[230, 60], [350, 60], [350, 150], [230, 150]]),
                np.float32([[230, 230], [350, 230], [350, 320], [230, 320]])]
    native = noise.copy()
    corrected = noise.astype("float32") / 1000
    for i, points in enumerate(polygons):
        mask = np.zeros(noise.shape, np.uint8)
        cv2.fillConvexPoly(mask, points.astype("int32"), 1)
        native[mask > 0] = 9000*(i+1) + noise[mask > 0]//20
        corrected[mask > 0] = [15, 45, 95][i]
    # File suffix deliberately differs from canonical wavelength-band order.
    mapping = [475, 560, 668, 842, 717, 444, 531, 650, 705, 740]
    shifts = {}
    for shot in shots:
        for suffix, wave in enumerate(mapping, 1):
            dx, dy = (0, 0) if suffix == 6 else (suffix % 3-1, suffix//3 % 3-1)
            shifts[suffix] = [dx, dy]
            matrix = np.float32([[1, 0, dx], [0, 1, dy]])
            a = cv2.warpAffine(native, matrix, (512, 384))
            rad = cv2.warpAffine(corrected, matrix, (512, 384))
            xmp = f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:c="http://micasense.com/Camera/1.0/"><c:CentralWavelength>{wave}</c:CentralWavelength></rdf:Description></rdf:RDF></x:xmpmeta>'
            tifffile.imwrite(raw / f"{shot}_{suffix}.tif", a, extratags=[(700, 'B', len(xmp.encode()), xmp.encode(), False)])
            for target in (p["ed"], p["rad"]):
                target.mkdir(parents=True, exist_ok=True)
                ds = gdal.GetDriverByName("GTiff").Create(str(target/f"{shot}_{suffix}.tif"), 512, 384, 1, gdal.GDT_Float32)
                ds.GetRasterBand(1).WriteArray(rad)
                ds = None
    config = folder / "project_legacy.json"
    atomic_json(config, cfg)
    records = []
    for i, polygon in enumerate(polygons):
        records.append({"name": "Tarp_"+"ABC"[i], "tarp_id": "ABC"[i], "outer": polygon.tolist(),
                        "inner": inset(polygon, 8).tolist(), "margin_percent_shortest_altitude_per_edge": 8,
                        "detection_band": 1, "center": polygon.mean(0).tolist(), "reviewed": True,
                        "role": "calibration" if i < 2 else "validation", "pixel_reviewed": True,
                        "exclusion_strokes": [[100., 100., 4, True]] if i == 0 else [],
                        "percentile_filter": {"enabled": i != 1, "low": 5, "high": 95}})
    return config, records, shifts


def backend_check(folder):
    config, records, shifts = fixture(folder)
    session = Session(config)
    for shot in session.shots:
        session.images[shot] = copy.deepcopy(records)
    for record in session.images[session.shots[1]]:
        record["role"] = "validation"
    session.save_draft()
    # Explicit approval gate, including the valid 'no exclusions' case.
    session.images[session.reference][0]["pixel_reviewed"] = False
    try:
        session.publish()
        raise AssertionError("unapproved pixel mask accepted")
    except ValueError as exc:
        assert "화소" in str(exc)
    session.images[session.reference][0]["pixel_reviewed"] = True
    session.save_draft()
    manifest_path = session.publish(lambda text: print(text, flush=True))
    cfg = read_json(config)
    cfg["preparation"]["approved_roi_manifest"] = str(manifest_path)
    generated, _, p = build(cfg)
    assert generated["ed"]["reference_shot"] == session.reference
    assert generated["ed"]["sensor_correction_already_applied"]
    assert _latest_manifest(p["roi"]) == manifest_path
    manifest = read_json(manifest_path)
    entries = read_json(manifest["transforms_json"])
    assert len(entries) == 20
    for entry in entries:
        expected = records[0]["inner"] - np.array([.5, .5]) if isinstance(records[0]["inner"], np.ndarray) else np.array(records[0]["inner"]) - .5
        dx, dy = shifts[entry["band_suffix"]]
        np.testing.assert_allclose(entry["native_rois"]["A"], expected + [dx, dy], atol=.2)
    entry = next(e for e in entries if e["shot"] == session.reference and e["band_suffix"] == 6)
    raster = Raster(entry["source_path"])
    stat = statistics(raster, records[0])[0]
    values = _read_values(Path(entry["source_path"]), entry["native_rois"]["A"], [512, 384], 5, 95,
                          entry["native_exclusion_masks"]["A"], "pixel_center_v1")
    assert len(values) == stat["count"]
    assert abs(float(values.mean())-stat["mean"]) < .002
    result = fit(cfg)
    with Path(result["coefficient_csv"]).open(encoding="utf-8-sig") as f:
        coef = list(csv.DictReader(f))
    assert len(coef) == 10
    assert all(abs(float(r["a"])-100) < 1e-8 and abs(float(r["b"])-5) < 1e-8 for r in coef)
    assert all(r["fit_tarps"] == "A;B" for r in coef), "validation C leaked into fit"
    cfg["preparation"]["coefficient_csv"] = result["coefficient_csv"]
    atomic_json(config, cfg)
    for entry in entries:
        source = p["ed"] / f"{entry['shot']}_{entry['band_suffix']}.tif"
        ds = gdal.Open(str(source))
        array = ds.ReadAsArray()
        ds = None
        p["ref"].mkdir(exist_ok=True)
        dst = gdal.GetDriverByName("GTiff").Create(str(p["ref"]/source.name), 512, 384, 1, gdal.GDT_Float32)
        dst.GetRasterBand(1).WriteArray((array-5)/100)
        dst = None
    _, _, reference, shots = approved_shots(cfg)
    report = write_report(cfg, manifest_path, manifest, reference, shots)
    assert report.is_file()
    checked_coefficients(cfg, reference)
    session.navigate(4)
    handoff = read_json(session.handoff_path)
    assert handoff["reference_shot"] == reference and handoff["step"] == 4
    reopened = Session(config)
    assert reopened.images == session.images
    session.images[reference][0]["percentile_filter"]["low"] = 6
    session.save_draft()
    try:
        _latest_manifest(p["roi"])
        raise AssertionError("changed draft reused old approval")
    except ValueError:
        pass
    selection = read_json(session.selection)
    selection["selected"].append({"shot": "IMG_9999"})
    atomic_json(session.selection, selection)
    try:
        session.assert_current()
        raise AssertionError("changed candidate selection accepted")
    except ValueError:
        pass
    return {"passed": True, "synthetic_only": True, "native_band_transforms": len(entries),
            "a": 100, "b": 5, "fit_tarps": ["A", "B"], "held_out_roi": "C",
            "source_pixel_membership_equal": True, "stale_draft_and_selection_blocked": True,
            "report": str(report), "config": str(config)}


def gui_check(folder):
    import tkinter as tk
    from unittest.mock import patch
    from PIL import ImageGrab
    from project_ui import ProjectApp
    config, records, _ = fixture(folder / "data")
    root = tk.Tk()
    errors = []
    app = ProjectApp(root, config)
    root.report_callback_exception = lambda *error: errors.append(str(error))
    def settle():
        until = time.monotonic()+30
        while (app.busy or app.raster is None) and time.monotonic() < until:
            root.update()
            time.sleep(.01)
        root.update()
        assert not app.busy and not errors, errors
    def fail(*args, **kw):
        raise AssertionError(args)
    try:
        with patch("app.messagebox.showerror", side_effect=fail), patch("app.messagebox.showinfo", side_effect=fail):
            root.update()
            time.sleep(.2)
            root.update()
            settle()
            app.records = copy.deepcopy(records)
            app.persist()
            app.shot.set("IMG_0002")
            app.open()
            settle()
            assert not app.records and app.role.get() == "validation"
            app.records = copy.deepcopy(records)
            for r in app.records:
                r["role"] = "validation"
            app.persist()
            app.shot.set("IMG_0001")
            app.open()
            settle()
            assert len(app.records) == 3 and app.records[2]["role"] == "validation"
            app.refresh_list()
            for size in ("1500x960", "1100x780"):
                root.geometry(size+"+20+20")
                root.attributes("-topmost", True)
                root.update()
                app.fit()
                root.update()
                time.sleep(.5)
                root.update()
                assert app.canvas.winfo_width() >= 400 and app.canvas.winfo_height() >= 250
                ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx()+root.winfo_width(), root.winfo_rooty()+root.winfo_height())).save(folder/f"dark_project_{size}.png")
            app.save()
            settle()
            assert app.session.last_manifest.is_file()
            assert read_json(app.session.handoff_path)["reference_shot"] == "IMG_0001"
            return {"passed": True, "automatic_selected_shots": app.session.shots,
                    "restored_rois": len(app.records), "errors": errors}
    finally:
        root.after_cancel(app.poll_after)
        root.destroy()


def host_check(folder):
    """Exercise the actual preparation host's candidate callback and handoff poll."""
    import tkinter as tk
    from unittest.mock import patch
    from types import SimpleNamespace
    from legacy_studio import PipelineGUI
    config = folder / "data/project_legacy.json"
    session = Session(config)
    pointer = read_json(Path(session.context["output"]) / "reviewed_manifest_latest.json")
    session.last_manifest = Path(pointer["path"])
    session.navigate(4)
    root = tk.Tk()
    root.withdraw()
    try:
        host = PipelineGUI(root, config)
        host.roi_handoff_path = session.handoff_path
        host.roi_config_path = config
        host.coefficient_confirmed.set(True)
        host._var("preparation.reference_shot").set("IMG_0002")
        host._poll_roi_handoff()
        assert host._var("preparation.reference_shot").get() == "IMG_0001"
        assert not host.coefficient_confirmed.get()
        assert host.pre_steps.index(host.pre_steps.select()) == 4
        updated = read_json(config)
        assert updated["preparation"]["approved_roi_manifest"] == str(session.last_manifest)
        assert updated["preparation"]["reference_shot"] == "IMG_0001"
        with patch.object(host, "open_tarp_roi_workflow") as launch:
            host._candidate_roi_ready({})
            root.update()
            launch.assert_called_once()
        assert host.pre_steps.index(host.pre_steps.select()) == 2
        host.roi_editor = SimpleNamespace(poll=lambda: None)
        with patch("legacy_studio.messagebox.showwarning") as warning:
            host.run("elc_fit")
            warning.assert_called_once()
        return {"passed": True, "candidate_callback_opens_new_editor": True,
                "ed_reference_auto_synced": True, "coefficient_approval_invalidated": True,
                "stage05_navigation": True, "processing_during_edit_blocked": True}
    finally:
        root.destroy()


if __name__ == "__main__":
    folder = Path(__file__).parent / "checks" / ("project_v13_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    result = {"backend": backend_check(folder/"backend"), "gui": gui_check(folder/"gui"), "host": host_check(folder/"gui")}
    atomic_json(folder/"result.json", result)
    print(json.dumps({"passed": True, "result": str(folder/"result.json")}), flush=True)
