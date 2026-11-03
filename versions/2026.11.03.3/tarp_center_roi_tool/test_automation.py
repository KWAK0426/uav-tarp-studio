"""Synthetic checks only; never reads campaign inputs or launches bulk campaign jobs."""
import copy
import csv
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
import tifffile
from osgeo import gdal

from test_project import fixture, ROOT, STUDIO
from core import inset
from uav_pipeline import easy_elc
from uav_pipeline.config import load_config
from uav_pipeline.preparation import locations
from uav_pipeline.roi_studio_bridge import Session, atomic_json, digest, read_json


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roi_easy_", dir=ROOT)
        self.folder = Path(self.temp.name)
        self.config, self.records, _ = fixture(self.folder)
        self.cfg = load_config(self.config)
        ed = self.folder / "ed.csv"
        ed.write_text("synthetic Ed placeholder; processing mocked in this test", encoding="utf-8")
        self.cfg["preparation"]["ed_csv"] = str(ed)
        from uav_pipeline.preparation import initialize
        initialize(self.cfg)
        atomic_json(self.config, self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def approved(self):
        session = Session(self.config)
        session.images = {shot: copy.deepcopy(self.records) for shot in session.shots}
        for r in session.images[session.shots[1]]:
            r["role"] = "validation"
        session.save_draft()
        manifest = session.publish()
        self.cfg["preparation"]["approved_roi_manifest"] = str(manifest)
        atomic_json(self.config, self.cfg)
        return session

    def test_tracking_translation_rotation_and_review_gate(self):
        session = Session(self.config)
        session.images[session.reference] = copy.deepcopy(self.records)
        matrix = cv2.getRotationMatrix2D((256, 192), 4, .97)
        matrix[:, 2] += [8, -5]
        h = np.vstack([matrix, [0, 0, 1]])
        target = session.shots[1]
        for band in range(1, 11):
            path = session.files[f"{target}_{band}"]
            array = tifffile.imread(path)
            with tifffile.TiffFile(path) as tf:
                xmp = tf.pages[0].tags[700].value
            tifffile.imwrite(path, cv2.warpAffine(array, matrix, (512, 384)),
                             extratags=[(700, "B", len(xmp), xmp, False)])
        # Re-open source signatures after intentionally preparing the fixture.
        session = Session(self.config)
        session.images[session.reference] = copy.deepcopy(self.records)
        results = session.propose_tracking()
        self.assertEqual(len(results[target]["records"]), 3, results[target]["diagnostics"])
        for original, record in zip(self.records, results[target]["records"]):
            expected = cv2.perspectiveTransform((np.float32(original["outer"]) - .5)[None], h)[0] + .5
            np.testing.assert_allclose(np.mean(record["outer"], 0), expected.mean(0), atol=3)
            self.assertFalse(record["reviewed"])
            self.assertFalse(record["pixel_reviewed"])
            self.assertEqual(record["role"], "validation")
            self.assertEqual(record["exclusion_strokes"], [])
        session.accept_tracking(results)
        with self.assertRaisesRegex(ValueError, "確認|확인"):
            session.publish()
        # A confirmed target must never be overwritten on a repeated tracking pass.
        session.images[target][0]["reviewed"] = True
        session.images[target][0]["pixel_reviewed"] = True
        kept = copy.deepcopy(session.images[target][0])
        results = session.propose_tracking()
        self.assertIn(kept, results[target]["records"])
        self.assertEqual(Session(self.config).tracking["reference_shot"], session.reference)

    def test_tracking_failure_and_missing_tarp_block_publication(self):
        session = Session(self.config)
        session.images[session.reference] = copy.deepcopy(self.records)
        tool = session._transforms()
        with patch.object(tool, "transform", side_effect=ValueError("no features")):
            results = session.propose_tracking()
        target = session.shots[1]
        self.assertEqual(len(results[target]["diagnostics"]), 3)
        self.assertEqual(results[target]["records"], [])
        session.accept_tracking(results)
        with self.assertRaisesRegex(ValueError, "누락"):
            session.publish()

    def test_isolated_paths_keep_original_roi_and_reject_escape(self):
        before = locations(self.cfg)
        cfg = copy.deepcopy(self.cfg)
        run = before["base"].parent / "Easy_ELC_test"
        cfg["preparation"]["easy_output_directory"] = str(run)
        after = locations(cfg)
        self.assertEqual(before["roi"], after["roi"])
        for name in ("coef", "ref", "rad", "qc", "configs"):
            self.assertTrue(after[name].is_relative_to(run))
        cfg["preparation"]["easy_output_directory"] = str(self.folder / "escaped")
        with self.assertRaises(ValueError):
            locations(cfg)

    def test_easy_two_gates_real_fit_and_report_preserve_source(self):
        self.approved()
        original_hash = digest(self.config)
        original_paths = locations(self.cfg)
        original_tiff_hash = digest(next(original_paths["ed"].glob("*.tif")))
        # Only expensive DN/Ed processing is replaced. Fit and native ROI validation
        # use the real synthetic corrected arrays and measured tarp reference table.
        def preflight(cfg):
            p = locations(cfg)
            return {"jobs": [{"width": 512, "height": 384} for _ in range(20)], "recommended_free_bytes": 1000000}
        def run(cfg):
            dst = locations(cfg)["ed"]
            shutil.copytree(original_paths["ed"], dst)
        with patch.object(easy_elc, "check_metadata_tool"), patch("uav_pipeline.time_ed.export"), patch("uav_pipeline.integrated.preflight", side_effect=preflight), \
             patch("uav_pipeline.integrated.run", side_effect=run), patch("uav_pipeline.integrated.connect_result"):
            record = easy_elc.prepare(self.cfg)
        self.assertEqual(record["status"], "awaiting_review")
        self.assertTrue(Path(record["report"]).is_file())
        cfg = load_config(record["config"])
        self.assertFalse(cfg["preparation"]["coefficient_basis_confirmed"])
        self.assertFalse(locations(cfg)["ref"].exists())
        with self.assertRaisesRegex(ValueError, "설정"):
            easy_elc.finish(self.cfg, "wrong")
        # Review token is bound to the exact run config. Here the test provides it.
        def apply_stage(cfg, stage):
            self.assertEqual(stage, "elc_apply_priority")
            from uav_pipeline.elc_priority import approved_shots, write_report
            p = locations(cfg)
            p["ref"].mkdir()
            for path in p["ed"].rglob("*.tif"):
                ds = gdal.Open(str(path))
                array = ds.ReadAsArray()
                ds = None
                dst = gdal.GetDriverByName("GTiff").Create(str(p["ref"] / path.name), 512, 384, 1, gdal.GDT_Float32)
                dst.GetRasterBand(1).WriteArray((array - 5) / 100)
                dst = None
            manifest, data, reference, shots = approved_shots(cfg)
            write_report(cfg, manifest, data, reference, shots)
        with patch.object(easy_elc, "stage", side_effect=apply_stage):
            finished = easy_elc.finish(self.cfg, digest(record["config"]))
        self.assertEqual(finished["status"], "complete")
        text = Path(finished["report"]).read_text(encoding="utf-8")
        self.assertIn("독립 영상·타프 검증", text)
        self.assertIn("품질 합격을 뜻하지", text)
        self.assertEqual(digest(self.config), original_hash)
        self.assertEqual(digest(next(original_paths["ed"].glob("*.tif"))), original_tiff_hash)
        with (locations(cfg)["coef"] / "elc_coefficients.csv").open(encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(r["fit_tarps"] == "A;B" for r in rows))

    def test_metadata_failure_stops_before_bulk_and_records_error(self):
        self.approved()
        with patch.object(easy_elc, "check_metadata_tool", side_effect=ValueError("ExifTool unavailable")), \
             patch("uav_pipeline.integrated.run") as worker:
            with self.assertRaisesRegex(ValueError, "ExifTool"):
                easy_elc.prepare(self.cfg)
            worker.assert_not_called()
        record = read_json(easy_elc.pointer_path(self.cfg))
        self.assertEqual(record["status"], "failed")
        self.assertTrue(Path(record["report"]).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
