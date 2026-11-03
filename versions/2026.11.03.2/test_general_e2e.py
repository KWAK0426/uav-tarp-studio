"""Opt-in real synthetic DN -> gain -> measured Ed -> ELC, without GPS/DEM/Photos.

Run with this package's Python. Writes only synthetic fixtures under .local.
Unlike the short unit suite, this executes ExifTool and real TIFF subprocesses.
"""
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import distribution as d

d.configure()
from uav_pipeline.exif_support import executable as exif_executable


def fixture(folder):
    import cv2
    import numpy as np
    import tifffile
    from uav_pipeline.config import load_config, save_config
    from uav_pipeline.general_project import save_shot_list, save_candidates
    from core import inset

    config = d.create_project(folder, "2027-06-10", "flight12", "Synthetic")
    cfg = load_config(config)
    raw = folder / "arbitrary raw layout"
    raw.mkdir()
    polygons = [np.float32([[60,60],[170,60],[170,150],[60,150]]),
                np.float32([[230,60],[350,60],[350,150],[230,150]]),
                np.float32([[230,230],[350,230],[350,320],[230,320]])]
    rng = np.random.default_rng(31)
    base = rng.integers(1000, 5000, (384, 512), dtype=np.uint16)
    for points, value in zip(polygons, (6000, 18000, 26000)):
        cv2.fillConvexPoly(base, points.astype("int32"), value)
    shots = ("IMG_0001", "IMG_0002")
    # Suffix order intentionally differs from canonical spectral-band order.
    waves = (475,560,668,842,717,444,531,650,705,740)
    for index, shot in enumerate(shots):
        paths = []
        for suffix, wave in enumerate(waves, 1):
            path = raw / f"{shot}_{suffix}.tif"
            xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                   '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                   '<rdf:Description xmlns:Camera="http://micasense.com/Camera/1.0/" '
                   f'Camera:CentralWavelength="{wave}" Camera:RadiometricCalibration="1,0,0" '
                   'Camera:VignettingCenter="256,192" Camera:VignettingPolynomial="0,0,0,0,0,0" '
                   'Camera:SerialNumber="SYNTHETIC-NOT-A-REAL-CAMERA"/>'
                   '</rdf:RDF></x:xmpmeta>').encode()
            tifffile.imwrite(path, base * (index + 1),
                             extratags=[(700, "B", len(xmp), xmp, False)])
            paths.append(path)
        subprocess.run([exif_executable(), "-overwrite_original", "-ExposureTime=0.01", "-ISO=100",
                        f"-DateTimeOriginal=2027:06:10 03:00:0{index*2}", *map(str, paths)],
                       check=True, capture_output=True, timeout=45)
    library, gains, ed = folder / "measured_tarps.csv", folder / "sensor_gains.csv", folder / "ed.csv"
    with library.open("w", newline="", encoding="utf-8") as stream:
        w = csv.writer(stream)
        w.writerow(["key", "use_for_fit", *[f"B{i:02d}" for i in range(1,11)]])
        for name, rho in (("A", .1), ("B", .4), ("C", .6)):
            w.writerow([name, 1, *([rho] * 10)])
    with gains.open("w", newline="", encoding="utf-8") as stream:
        w = csv.writer(stream); w.writerow(["band", "gain"])
        w.writerows((b, 1.1 + (b-1)*.01) for b in range(1,11))
    with ed.open("w", newline="", encoding="utf-8") as stream:
        w = csv.writer(stream)
        w.writerow(["datetime_kst", *[f"Band{b}_{name}" for b,name in cfg["band_names"].items()]])
        w.writerow(["2027-06-10 12:00:00", *([1]*10)])
        w.writerow(["2027-06-10 12:00:02", *([2]*10)])
    cfg["preparation"].update(raw_directory=str(raw), tarp_reflectance_csv=str(library),
                              gain_csv=str(gains), ed_csv=str(ed), reference_shot=shots[0], worker_count=1)
    save_shot_list(cfg, shots)
    save_config(config, cfg)
    save_candidates(cfg, shots, shots[0])
    save_config(config, cfg)
    records = [{"name": "Tarp_" + name, "tarp_id": name, "outer": points.tolist(),
                "inner": inset(points, 8).tolist(), "center": points.mean(0).tolist(),
                "margin_percent_shortest_altitude_per_edge":8, "detection_band":1,
                "reviewed":True, "pixel_reviewed":True, "exclusion_strokes":[],
                "role":"calibration" if name in ("A","B") else "validation",
                "percentile_filter":{"enabled":True,"low":5,"high":95}}
               for name, points in zip("ABC", polygons)]
    return config, records, raw


def run():
    import copy
    import numpy as np
    from osgeo import gdal
    from uav_pipeline.config import load_config, save_config
    from uav_pipeline.roi_studio_bridge import Session, digest
    from uav_pipeline import easy_elc
    from uav_pipeline.preparation import locations

    started = time.monotonic()
    temp = Path(os.environ.get("ELC_E2E_ROOT", str(d.STATE / "e2e"))).resolve()
    temp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native_no_gps_", dir=temp) as directory:
        config, records, raw = fixture(Path(directory))
        before = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.glob("*.tif")}
        session = Session(config)
        for shot in session.shots:
            session.images[shot] = copy.deepcopy(records)
        for record in session.images["IMG_0002"]:
            record["role"] = "validation"
        session.reference = "IMG_0001"
        session.save_draft()
        manifest = session.publish()
        cfg = load_config(config)
        cfg["preparation"]["approved_roi_manifest"] = str(manifest)
        save_config(config, cfg)
        record = easy_elc.prepare(cfg)
        assert record["status"] == "awaiting_review"
        working = load_config(record["config"])
        assert not working["preparation"]["coefficient_basis_confirmed"]
        assert not locations(working)["ref"].exists()
        with Path(working["preparation"]["coefficient_csv"]).open(encoding="utf-8-sig") as stream:
            coefficients = list(csv.DictReader(stream))
        assert len(coefficients) == 10
        for row in coefficients:
            band = int(row["band"])
            gain = 1.1 + (band-1)*.01
            np.testing.assert_allclose([float(row["a"]),float(row["b"])],
                                       [40000/655.36*gain,2000/655.36*gain], rtol=2e-5)
        finished = easy_elc.finish(cfg, digest(record["config"]))
        assert finished["status"] == "complete"
        ref = locations(load_config(record["config"]))["ref"]
        outputs = sorted(ref.rglob("*.tif"))
        assert len(outputs) == 20, len(outputs)
        for path in outputs:
            ds = gdal.Open(str(path))
            assert (ds.RasterXSize,ds.RasterYSize,ds.RasterCount)==(512,384,1)
            assert ds.GetRasterBand(1).DataType == gdal.GDT_Float32
            np.testing.assert_allclose(ds.ReadAsArray()[250:300,250:330], .6, atol=3e-6)
            ds = None
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==sha for p,sha in before.items())
        result = dict(passed=True, synthetic_only=True, processing_stages_mocked=False,
                      date="2027-06-10", session="flight12", gps=False, dem=False, photos=False,
                      coefficients=10, native_reflectance_tiffs=20, source_hashes_unchanged=True,
                      seconds=round(time.monotonic()-started,2))
        (d.STATE / "e2e_result.json").write_text(json.dumps(result,indent=2), encoding="utf-8")
        print(json.dumps(result),flush=True)
        return result


if __name__ == "__main__":
    run()
