"""Synthetic general-project constraints; never reads or processes real imagery."""
from contextlib import redirect_stdout
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import tifffile

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "UAV_Preprocessing_Studio_20260924/engine"),
                str(ROOT / "tarp_center_roi_tool"), str(ROOT / "vendor")]
from uav_pipeline import easy_elc, general_project as general, integrated, preparation, time_ed
from uav_pipeline.config import DEFAULTS, save_config
from uav_pipeline.roi_studio_bridge import Session


class GeneralBackendTests(unittest.TestCase):
    def setUp(self):
        version = patch("uav_pipeline.exif_support.version", return_value=(13, 59))
        version.start()
        self.addCleanup(version.stop)
        parent = ROOT / ".local/general_backend_tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic_", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.cfg = deepcopy(DEFAULTS)
        self.cfg.update(general={"enabled": True, "project_root": str(self.root),
                                 "date": "2027-06-15", "session": "flight12", "site": "Test"},
                        dataset={"root": str(self.root), "date": "2027_06_15_Test", "flight": "flight12",
                                 "basis": "Panel", "paths": {}},
                        preparation=deepcopy(preparation.CHOICES), _config_path=str(self.root / "project.json"))
        self.shots = {"IMG_0001", "IMG_0002"}
        # Suffix is deliberately not canonical spectral-band order.
        self.waves = [475, 560, 668, 842, 717, 444, 531, 650, 705, 740]
        for shot in sorted(self.shots):
            for band in range(1, 11):
                tifffile.imwrite(self.raw / f"{shot}_{band}.tif", np.full((8, 12), 1000, np.uint16))
        gain = self.root / "gains.csv"
        gain.write_text("band,gain\n" + "".join(f"{i},2\n" for i in range(1, 11)), encoding="utf-8")
        library = self.root / "tarps.csv"
        library.write_text("key,use_for_fit," + ",".join(f"B{i:02d}" for i in range(1, 11)) + "\n" +
                           "A,1," + ",".join(["0.1"] * 10) + "\nB,1," + ",".join(["0.4"] * 10) + "\n", encoding="utf-8")
        self.ed = self.root / "ed.csv"
        self.write_ed()
        self.cfg["preparation"].update(raw_directory=str(self.raw), gps_path="", source_timezone="Asia/Seoul",
                                       ed_csv=str(self.ed), gain_csv=str(gain), tarp_reflectance_csv=str(library),
                                       reference_shot="IMG_0001")
        preparation.initialize(self.cfg)
        self.manifest = general.save_shot_list(self.cfg, self.shots)
        self.selection = general.save_candidates(self.cfg, self.shots, "IMG_0001")
        save_config(self.cfg["_config_path"], self.cfg)

    def write_ed(self):
        bands = [f"Band{k}_{v}" for k, v in self.cfg["band_names"].items()]
        with self.ed.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["datetime_kst", "cycle", *bands])
            writer.writeheader()
            for second, value in ((0, 100), (10, 200), (20, 300)):
                writer.writerow({"datetime_kst": f"2027-06-15 12:00:{second:02d}", "cycle": "flight12",
                                 **{band: value for band in bands}})

    def image_rows(self):
        def times(paths):
            return {str(path.resolve()): ("2027:06:15 12:00:05" if path.name.startswith("IMG_0001")
                                         else "2027:06:15 12:00:15") for path in paths}
        return time_ed.image_rows(self.cfg, read_times=times)

    def prepared(self):
        rows = self.image_rows()
        with patch.object(time_ed, "image_rows", return_value=rows), redirect_stdout(io.StringIO()):
            run, _ = time_ed.export(self.cfg)
        return run

    def metadata(self, paths, native):
        return {str(path): {"CentralWavelength": self.waves[int(path.stem.rsplit("_", 1)[1]) - 1],
                            "RadiometricCalibration": "65536,0,0", "VignettingCenter": "6,4",
                            "VignettingPolynomial": "0,0,0,0,0,0", "ExposureTime": 1,
                            "ISOSpeed": 100, "BitsPerSample": 16, "SerialNumber": "synthetic-only"}
                for path in paths}

    def plan(self):
        self.prepared()
        with patch.object(integrated, "metadata_rows", side_effect=self.metadata), redirect_stdout(io.StringIO()):
            return integrated.preflight(self.cfg)

    def test_2027_flight12_flat_inputs_without_geometry(self):
        self.assertTrue(general.is_general(self.cfg))
        self.assertEqual(general.source_list_path(self.cfg), self.manifest)
        self.assertEqual(general.source_shots(self.cfg), self.shots)
        self.assertEqual(len(general.scan_raw(self.raw)), 20)
        configs, issues, paths = preparation.build(self.cfg)
        payload = configs["radiometric"]
        self.assertFalse(payload["numeric_folders_only"])
        self.assertEqual(payload["gps_path"], "")
        self.assertNotIn("_gps_labels", payload)
        for stage in ("radiometric", "ed"):
            with self.assertRaisesRegex(ValueError, "integrated_preflight"):
                preparation.validate_stage(self.cfg, stage)
        self.assertEqual(issues["radiometric"], [])
        self.assertEqual(set(paths), {"base", "configs", "qc", "rad", "ed", "coef", "ref", "roi"})
        self.assertTrue(all(path.is_relative_to(self.root) for path in paths.values()))
        self.assertTrue(all(v == 1 for v in configs["ed"]["sensor_gains"].values()))
        self.assertEqual(self.cfg["geometry"]["gps_path"], "")
        self.assertEqual(self.cfg["geometry"]["dem_path"], "")
        self.assertEqual(self.cfg["photos"]["directory"], "")

    def test_old_exiftool_blocks_long_paths_before_writing(self):
        from uav_pipeline.exif_support import require_paths_supported
        with patch("uav_pipeline.exif_support.os.name", "nt"), patch("uav_pipeline.exif_support.version", return_value=(12,52)):
            with self.assertRaisesRegex(ValueError, "13.07"):
                require_paths_supported([self.root / ("x"*180) / "output.tif"])

    def test_recursive_raw_and_case_insensitive_ambiguity(self):
        nested = self.raw / "arbitrary/depth"
        nested.mkdir(parents=True)
        path = self.raw / "IMG_0001_1.tif"
        path.rename(nested / path.name)
        self.assertEqual(len(general.raw_for_shots(self.raw, self.shots)), 20)
        shutil.copy2(nested / path.name, self.raw / "img_0001_1.TIFF")
        for action in (lambda: general.scan_raw(self.raw), lambda: preparation.validate_stage(self.cfg, "radiometric"), self.image_rows):
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                action()

    def test_empty_raw_path_is_not_the_current_directory(self):
        with self.assertRaisesRegex(ValueError, "explicitly selected"):
            general.scan_raw("")

    def test_missing_zero_and_absent_targets_block(self):
        path = self.raw / "IMG_0002_10.tif"
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing"):
            preparation.validate_stage(self.cfg, "radiometric", selected_shots={"IMG_0001"})
        path.touch()
        with self.assertRaisesRegex(ValueError, "zero-byte"):
            self.image_rows()
        general.save_shot_list(self.cfg, {"IMG_9999"})
        with self.assertRaisesRegex(ValueError, "Missing"):
            preparation.validate_stage(self.cfg, "radiometric")

    def test_manifest_validation_and_fresh_history(self):
        second = general.save_shot_list(self.cfg, self.shots)
        self.assertNotEqual(second, self.manifest)
        self.assertTrue(self.manifest.is_file())
        for contents in ("other\nIMG_0001\n", "shot\n", "shot\nIMG_0001\nimg_0001\n", "shot\n../bad\n"):
            second.write_text(contents, encoding="utf-8")
            with self.assertRaises(ValueError):
                general.source_shots(self.cfg)
        self.cfg["preparation"]["shot_list_csv"] = ""
        self.cfg["preparation"]["gps_path"] = str(self.manifest)
        with self.assertRaisesRegex(ValueError, "shot_list_csv"):
            general.source_list_path(self.cfg)

    def test_candidate_subset_reference_and_session_handoff(self):
        with self.assertRaisesRegex(ValueError, "subset"):
            general.save_candidates(self.cfg, {"IMG_9999"})
        with self.assertRaisesRegex(ValueError, "Reference"):
            general.save_candidates(self.cfg, {"IMG_0001"}, "IMG_0002")
        path, _, shots = preparation.latest_candidate_selection(self.cfg)
        self.assertEqual(path, self.selection)
        self.assertEqual(shots, self.shots)
        session = Session(self.cfg["_config_path"])
        self.assertEqual(session.reference, "IMG_0001")
        self.assertEqual(set(session.shots), self.shots)
        with self.assertRaises(ValueError):
            session.publish()  # No automatic ROI approval.

    def test_cleared_candidates_do_not_revive_older_selection(self):
        session = Session(self.cfg["_config_path"])
        cleared = general.save_candidates(self.cfg, [])
        self.assertTrue(json.loads(cleared.read_text())["selection_cleared"])
        self.assertEqual(self.cfg["preparation"]["reference_shot"], "")
        with self.assertRaisesRegex(ValueError, "해제"):
            preparation.latest_candidate_selection(self.cfg)
        save_config(self.cfg["_config_path"], self.cfg)
        with self.assertRaises(ValueError):
            session.assert_current()

    def test_clear_before_target_registration_is_allowed(self):
        self.cfg["preparation"].update(raw_directory="", shot_list_csv="")
        path = general.save_candidates(self.cfg, [])
        self.assertTrue(json.loads(path.read_text())["selection_cleared"])
        with self.assertRaisesRegex(ValueError, "해제"):
            preparation.latest_candidate_selection(self.cfg)

    def test_target_bytes_changed_after_roi_session_are_blocked(self):
        session = Session(self.cfg["_config_path"])
        self.manifest.write_text("shot\nIMG_0002\nIMG_0001\n", encoding="utf-8")
        for action in (session.assert_current, lambda: preparation.latest_candidate_selection(self.cfg),
                       lambda: easy_elc.input_signature(self.cfg)):
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                action()

    def test_replaced_target_path_and_changed_raw_directory_are_blocked(self):
        general.save_shot_list(self.cfg, self.shots)
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            preparation.latest_candidate_selection(self.cfg)
        self.cfg["preparation"]["shot_list_csv"] = str(self.manifest)
        self.cfg["preparation"]["raw_directory"] = str(self.root)
        with self.assertRaisesRegex(ValueError, "raw source changed"):
            preparation.latest_candidate_selection(self.cfg)

    def test_easy_isolation_keeps_roi_and_rejects_escapes(self):
        before = preparation.locations(self.cfg)
        clone = deepcopy(self.cfg)
        run = self.root / "Easy_ELC_synthetic"
        clone["preparation"]["easy_output_directory"] = str(run)
        after = preparation.locations(clone)
        self.assertEqual(before["roi"], after["roi"])
        for name in ("base", "configs", "qc", "rad", "ed", "coef", "ref"):
            self.assertTrue(after[name].is_relative_to(run))
        self.assertEqual(easy_elc.input_signature(self.cfg), easy_elc.input_signature(clone))
        for escaped in (self.root.parent / "Easy_ELC_escape", self.root / "nested/Easy_ELC_escape", self.root / "Other"):
            clone["preparation"]["easy_output_directory"] = str(escaped)
            with self.assertRaises(ValueError):
                preparation.locations(clone)

    def test_bad_date_session_and_identity_are_blocked(self):
        for key, value in (("date", "2027-02-30"), ("session", "../escape"), ("site", "NUL"),
                           ("project_root", "relative"), ("date", "20270615")):
            cfg = deepcopy(self.cfg)
            cfg["general"][key] = value
            with self.assertRaises(ValueError):
                preparation.locations(cfg)
        cfg = deepcopy(self.cfg)
        cfg["dataset"]["flight"] = "1st"
        with self.assertRaisesRegex(ValueError, "identity"):
            preparation.locations(cfg)

    def test_no_gps_times_and_measured_ed_are_valid_without_fake_solar(self):
        rows = self.image_rows()
        for row in rows:
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["solar_status"], "unavailable")
            self.assertEqual(row["gps_status"], "no_gps")
            for key in ("sza_deg", "saa_deg", "longitude", "latitude", "X_est", "Y_est", "Z_est"):
                self.assertEqual(row[key], "")
            self.assertTrue(row["capture_time_utc"])
        matches = time_ed.match_ed(rows, self.ed, self.cfg)
        self.assertTrue(all(row["match_status"] == "matched" for row in matches))
        self.assertTrue(all(row["ed_abs_delta_seconds"] == 5 for row in matches))
        self.assertEqual(matches[0]["ed_time_kst"], "2027-06-15T12:00:00+09:00")

    def test_optional_real_gps_supplies_true_location_only(self):
        gps = self.root / "real_optional_GPS.txt"
        gps.write_text('# CoordinateSystem: EPSG:4326\n#Label\tX_est\tY_est\tZ_est\n'
                       'IMG_0001_1.tif\t127\t35\t100\n', encoding="utf-8")
        self.cfg["preparation"]["gps_path"] = str(gps)
        rows = self.image_rows()
        self.assertEqual(rows[0]["longitude"], 127)
        self.assertEqual(rows[0]["latitude"], 35)
        self.assertIsInstance(rows[0]["sza_deg"], float)
        self.assertEqual(rows[1]["solar_status"], "unavailable")
        self.assertTrue(all(row["match_status"] == "matched" for row in time_ed.match_ed(rows, self.ed, self.cfg)))

    def test_missing_optional_gps_does_not_block_time_or_ed(self):
        self.cfg["preparation"]["gps_path"] = str(self.root / "absent_GPS.txt")
        rows = self.image_rows()
        self.assertTrue(all(row["status"] == "ok" and row["solar_status"] == "unavailable" for row in rows))
        self.assertTrue(all(row["match_status"] == "matched" for row in time_ed.match_ed(rows, self.ed, self.cfg)))
        self.assertIsNone(time_ed.time_signature(self.cfg, rows)["gps_sha256"])

    def test_real_timestamp_date_mismatch_and_missing_exif_are_invalid(self):
        for value, state in (("2026:06:15 12:00:05", "capture_date_mismatch"), (None, "missing_or_invalid_capture_time")):
            rows = time_ed.image_rows(self.cfg, read_times=lambda paths: {str(path.resolve()): value for path in paths})
            self.assertTrue(all(row["status"] == state for row in rows))
            self.assertTrue(all(row["match_status"] == "invalid_image" for row in time_ed.match_ed(rows, self.ed, self.cfg)))

    def test_ed_does_not_extrapolate_or_accept_tolerance_above_five(self):
        rows = self.image_rows()
        rows[0]["capture_time_kst"] = "2027-06-15T11:59:59+09:00"
        rows[1]["capture_time_kst"] = "2027-06-15T12:00:21+09:00"
        self.assertTrue(all(row["match_status"] == "outside_measured_range" for row in time_ed.match_ed(rows, self.ed, self.cfg)))
        self.cfg["preparation"]["max_ed_delta_seconds"] = 6
        with self.assertRaises(ValueError):
            time_ed.match_ed(rows, self.ed, self.cfg)

    def test_time_signature_invalidates_manifest_bytes_raw_and_timezone(self):
        directory = self.prepared().parent
        time_ed.load_prepared_times(self.cfg, directory)
        original = self.manifest.read_bytes()
        self.manifest.write_bytes(original + b"\n")
        with self.assertRaises(ValueError):
            time_ed.load_prepared_times(self.cfg, directory)
        self.manifest.write_bytes(original)
        path = self.raw / "IMG_0001_1.tif"
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaises(ValueError):
            time_ed.load_prepared_times(self.cfg, directory)
        self.cfg["preparation"]["source_timezone"] = "UTC"
        with self.assertRaises(ValueError):
            time_ed.load_prepared_times(self.cfg, directory)

    def test_integrated_plan_manifest_provenance_native_gain_once_and_ed(self):
        plan = self.plan()
        self.assertEqual(plan["files"], 20)
        self.assertEqual(plan["source_manifest"], general.source_signature(self.cfg))
        self.assertNotIn("gps_sha256", plan)
        self.assertEqual(self.cfg["preparation"]["gps_path"], "")
        loaded, plan_hash = integrated.load_plan(self.cfg)
        native = integrated.core(self.cfg["preprocess"]["tools"]["radiometric"])
        written = []
        def write(src, dst, array, tags):
            written.append((array.copy(), dict(tags)))
        with patch.object(native, "write_radiometric_pixels", side_effect=write):
            for job in loaded["jobs"]:
                integrated.apply_integrated({**job, "plan_hash": plan_hash})
        self.assertEqual(len(written), 20)
        for job, (array, tags) in zip(loaded["jobs"], written):
            self.assertEqual(integrated.CENTERS[job["band"]], self.waves[int(Path(job["src"]).stem.rsplit("_", 1)[1]) - 1])
            np.testing.assert_array_equal(array, np.full((8, 12), 2000 if job["shot"] == "IMG_0001" else 1000, np.float32))
            self.assertEqual(tags["SENSOR_INTERCALIBRATION_GAIN"], "2.0")
            self.assertEqual(tags["SOURCE_TARGET_MANIFEST_SHA256"], plan["source_list_sha256"])
            self.assertEqual(tags["PROVENANCE_MANIFEST_SHA256"], plan_hash)
        self.manifest.write_bytes(self.manifest.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            integrated.load_plan(self.cfg)

    def test_integrated_inventory_change_and_full_target_scope(self):
        self.cfg["preparation"]["elc_only"] = False
        general.save_candidates(self.cfg, {"IMG_0001"}, "IMG_0001")
        plan = self.plan()
        self.assertEqual({job["shot"] for job in plan["jobs"]}, self.shots)
        shutil.copy2(self.raw / "IMG_0001_1.tif", self.raw / "img_0001_1.TIFF")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            integrated.load_plan(self.cfg)

    def test_integrated_rejects_out_of_date_and_external_output(self):
        self.prepared()
        self.cfg["preparation"]["integrated_output_root"] = str(self.root.parent / "outside")
        with self.assertRaises(ValueError):
            integrated.preflight(self.cfg)

    def test_legacy_gps_fallback_and_path_limits_unchanged(self):
        cfg = deepcopy(self.cfg)
        cfg.pop("general")
        gps = self.root / "legacy_GPS.txt"
        gps.write_text("IMG_0001_6.tif\t127\t35\t100\n", encoding="utf-8")
        cfg["preparation"]["gps_path"] = str(gps)
        self.assertEqual(general.source_list_path(cfg), gps)
        self.assertEqual(general.source_shots(cfg), {"IMG_0001"})
        with self.assertRaises(ValueError):
            preparation.locations(cfg)
        cfg["dataset"].update(date="2026_06_15_Test", flight="1st")
        cycle = self.root / "2026_06_15_Test/02_Processed/1st_260615"
        cycle.mkdir(parents=True)
        self.assertEqual(preparation.locations(cfg)["base"].parent, cycle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
