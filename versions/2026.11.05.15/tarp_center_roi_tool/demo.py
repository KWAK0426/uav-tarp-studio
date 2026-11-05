"""Synthetic input and executable-level QA, never used to fit detection parameters."""
import csv
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from osgeo import gdal
from PIL import Image, ImageDraw, ImageGrab


def create_demo(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "DEMO_only_3_tarps.tif"
    polygons = [np.array(p, np.float32) for p in (
        [[100, 125], [275, 95], [300, 260], [120, 285]],
        [[405, 160], [570, 135], [605, 305], [415, 325]],
        [[705, 280], [890, 320], [845, 490], [680, 445]])]
    complete = False
    if path.exists():
        try:
            existing = gdal.Open(str(path))
            complete = existing.GetMetadataItem("DEMO_COMPLETE") == "YES"
            existing = None
        except RuntimeError:
            pass
    if not complete:
        rng = np.random.default_rng(314)
        ds = gdal.GetDriverByName("GTiff").Create(str(path), 1024, 640, 3, gdal.GDT_Float32, options=["COMPRESS=DEFLATE"])
        ds.SetMetadata({"SYNTHETIC_DEMO": "YES", "VALUE_MEANING": "Arbitrary synthetic values, not calibrated radiance"})
        ds.SetGeoTransform((300000, .05, 0, 4000000, 0, -.05))
        from osgeo import osr
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32652)
        ds.SetProjection(srs.ExportToWkt())
        for band in range(1, 4):
            image = np.full((640, 1024), .40 + band*.025)
            for index, p in enumerate(polygons):
                region = np.zeros(image.shape, np.uint8)
                cv2.fillConvexPoly(region, p.astype(np.int32), 1)
                image[region > 0] = (.06, .66, .91)[index] * (1 + (band-1)*.015)
            image += rng.normal(0, .005, image.shape)
            b = ds.GetRasterBand(band)
            b.WriteArray(image.astype(np.float32))
            b.SetDescription(f"Synthetic_band_{band}")
        ds.SetMetadataItem("DEMO_COMPLETE", "YES")
        ds = None
    return path, polygons


def run_self_test(app, folder):
    """Exercise the exact GUI callbacks and export code shipped in the executable."""
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    root = app.root
    errors = []
    report = {"synthetic_only": True, "status": "running", "cases": []}
    root.report_callback_exception = lambda *exc: errors.append(str(exc))

    def settle():
        deadline = time.monotonic()+30
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        root.update()
        if app.busy or errors:
            raise RuntimeError(str(errors) if errors else "Worker timeout")

    def no_dialog(*args, **kwargs):
        raise RuntimeError("Unexpected dialog: " + repr(args))

    try:
        with patch("app.messagebox.showerror", side_effect=no_dialog), patch("app.messagebox.showinfo", side_effect=no_dialog):
            root.update()
            path, truth = create_demo(folder/"input")
            app.open(str(path))
            settle()
            if app.raster is None:
                raise RuntimeError("Input failed to open")
            app.radius.set(512)
            app.margin.set(8)
            for index, p in enumerate(truth):
                c = p.mean(axis=0)
                start = time.perf_counter()
                app.click(SimpleNamespace(x=c[0]*app.scale+app.ox, y=c[1]*app.scale+app.oy))
                settle()
                if app.current is None:
                    raise RuntimeError(f"No candidate for synthetic tarp {index+1}")
                predicted = app.current.copy()
                actual_mask = np.zeros((640, 1024), np.uint8)
                predicted_mask = np.zeros_like(actual_mask)
                cv2.fillConvexPoly(actual_mask, p.astype(np.int32), 1)
                cv2.fillConvexPoly(predicted_mask, np.rint(predicted-.5).astype(np.int32), 1)
                iou = float(((actual_mask > 0) & (predicted_mask > 0)).sum()/((actual_mask > 0) | (predicted_mask > 0)).sum())
                if iou < .93:
                    raise RuntimeError(f"Synthetic IoU below .93: {iou}")
                report["cases"].append({"tarp": index+1, "candidate_count": len(app.candidates), "seconds": time.perf_counter()-start, "outer_iou": iou})
                app.name.set(f"DEMO_{index+1:02d}")
                app.confirm()
                settle()
            # Also exercise source-resolution zoom, pan, editing and band switching.
            app.focus_roi()
            root.update()
            app.fit()
            app.band.set("2")
            app.change_band()
            settle()
            app.listbox.selection_set(0)
            app.select_record()
            settle()
            p = app.current[0].copy()
            app.click(SimpleNamespace(x=p[0]*app.scale+app.ox, y=p[1]*app.scale+app.oy))
            app.drag_vertex(SimpleNamespace(x=(p[0]+.5)*app.scale+app.ox, y=p[1]*app.scale+app.oy))
            app.drag = None
            if abs(app.current[0, 0]-p[0]-.5) > .01:
                raise RuntimeError("Vertex drag failed")
            app.confirm()
            settle()
            with patch("app.filedialog.askdirectory", return_value=str(folder/"results")):
                app.save()
                settle()
            saved = app.last_output / "roi.json"
            payload = json.loads(saved.read_text(encoding="utf-8"))
            if len(payload["rois"]) != 3 or not payload["synthetic_demo"]:
                raise RuntimeError("Export provenance or ROI count failed")
            with (app.last_output/"pixel_statistics.csv").open(encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            if len(rows) != 9 or min(int(r["valid_pixels"]) for r in rows) < 100:
                raise RuntimeError("Band statistics missing")
            for row in rows:
                expected = (.06, .66, .91)[int(row["roi_id"])-1]*(1+(int(row["raster_band"])-1)*.015)
                if abs(float(row["mean_stored"])-expected) > .002:
                    raise RuntimeError("Synthetic interior mean drift")
            app.records = []
            with patch("app.filedialog.askopenfilename", return_value=str(saved)):
                app.restore()
            if len(app.records) != 3:
                raise RuntimeError("Restore failed")
            report["export"] = str(saved)
            report["statistics_rows"] = len(rows)
            report["status"] = "passed"
            report["callback_errors"] = errors
            for label, size in (("desktop", "1240x820"), ("compact", "1024x720")):
                root.geometry(size)
                root.update()
                app.fit()
                root.update()
                time.sleep(.25)
                ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx()+root.winfo_width(), root.winfo_rooty()+root.winfo_height())).save(folder/f"gui_{label}.png")
            image = Image.fromarray(app.raster.preview(1)).convert("RGB")
            draw = ImageDraw.Draw(image)
            for r in app.records:
                for kind, color in (("outer", "#ffc94b"), ("inner", "#00bccf")):
                    points = list(map(tuple, r[kind]))
                    draw.line(points+points[:1], fill=color, width=3)
                draw.text(tuple(np.min(r["outer"], axis=0)-[0, 20]), r["name"], fill="white")
            image.save(folder/"synthetic_roi_overlay.png")
    except Exception:
        import traceback
        report["status"] = "failed"
        report["error"] = traceback.format_exc()
    finally:
        (folder/"self_test_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if app.raster is not None:
            app.raster.ds = None
        root.destroy()
    return 0 if report["status"] == "passed" else 1
