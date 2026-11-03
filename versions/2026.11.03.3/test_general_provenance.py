"""General-only approval/apply regressions using small synthetic native rasters."""
from copy import deepcopy
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np
import tifffile
from osgeo import gdal

import distribution

distribution.configure()
from uav_pipeline.exif_support import executable as exif_executable
from uav_pipeline import elc_fit, elc_priority, general_project as general, preparation
from uav_pipeline.roi_studio_bridge import Session, atomic_json, digest, read_json
from ed_sensor_correction_tools import apply_elc_reflectance_with_config as apply


class GeneralProvenanceTests(unittest.TestCase):
    def setUp(self):
        parent = distribution.STATE / "provenance_tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic_", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.shots = {"IMG_0001", "IMG_0002"}
        self.waves = [475, 560, 668, 842, 717, 444, 531, 650, 705, 740]
        self.cfg = distribution.preparation_template()
        self.cfg.update(general={"enabled": True, "project_root": str(self.root), "date": "2027-06-15",
                                 "session": "flight12", "site": "Synthetic"},
                        dataset={"root": str(self.root), "date": "2027_06_15_Synthetic", "flight": "flight12",
                                 "basis": "Panel", "paths": {}})
        self.config = self.root / "project.json"
        library = self.root / "library.csv"
        library.write_text("key,use_for_fit," + ",".join(f"B{i:02d}" for i in range(1, 11)) + "\n"
                           + "A,1," + ",".join(["0.1"] * 10) + "\nB,1," + ",".join(["0.4"] * 10) + "\n",
                           encoding="utf-8")
        self.cfg["preparation"].update(raw_directory=str(self.raw), reference_shot="IMG_0001",
                                       tarp_reflectance_csv=str(library), gps_path="")
        self.records = []
        for index, tarp in enumerate(("A", "B")):
            x = 2 + index * 10
            points = [[x, 2], [x + 6, 2], [x + 6, 9], [x, 9]]
            self.records.append(dict(name=tarp, tarp_id=tarp, outer=points, inner=points,
                                     role="calibration", reviewed=True, pixel_reviewed=True,
                                     exclusion_strokes=[], percentile_filter={"enabled": False}))
        for shot in self.shots:
            for suffix, wave in enumerate(self.waves, 1):
                xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                       '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                       '<rdf:Description xmlns:Camera="http://micasense.com/Camera/1.0/" '
                       f'Camera:CentralWavelength="{wave}"/></rdf:RDF></x:xmpmeta>').encode()
                tifffile.imwrite(self.raw / f"{shot.lower()}_{suffix}.TiFf", np.full((16, 28), 1000, np.uint16),
                                 extratags=[(700, "B", len(xmp), xmp, False)])
        self.target = general.save_shot_list(self.cfg, self.shots)
        self.selection = general.save_candidates(self.cfg, self.shots, "IMG_0001")
        atomic_json(self.config, self.cfg)
        self.paths = preparation.locations(self.cfg)
        self.paths["ed"].mkdir(parents=True, exist_ok=True)
        for shot in self.shots:
            for suffix, wave in enumerate(self.waves, 1):
                path = self.paths["ed"] / f"{shot.lower()}_{suffix}.TiFf"
                ds = gdal.GetDriverByName("GTiff").Create(str(path), 28, 16, 1, gdal.GDT_Float32)
                ds.GetRasterBand(1).WriteArray(np.full((16, 28), 15, np.float32))
                ds.SetMetadata({"CENTRAL_WAVELENGTH": str(wave), "PROCESSING_STAGE": "radiometric_sensor_ed_integrated",
                                "ED_NORMALIZATION_APPLIED": "true", "SENSOR_INTERCALIBRATION_APPLIED": "true"})
                ds = None
        coefficient = self.root / "coefficients.csv"
        with coefficient.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["band", "a", "b", "coefficient_convention"])
            writer.writerows([i, 100, 5, "L=a*rho+b"] for i in range(1, 11))
        self.cfg["preparation"]["coefficient_csv"] = str(coefficient)
        atomic_json(self.config, self.cfg)

    def approve(self):
        session = Session(self.config)
        session.images = {shot: deepcopy(self.records) for shot in session.shots}
        for record in session.images["IMG_0002"]:
            record["role"] = "validation"
        session.save_draft()
        tool = SimpleNamespace(read=tifffile.imread,
                               band_transform=lambda *args: (np.eye(3), {"method": "synthetic_identity"}))
        with patch.object(session, "_transforms", return_value=tool):
            manifest = session.publish()
        self.cfg["preparation"]["approved_roi_manifest"] = str(manifest)
        atomic_json(self.config, self.cfg)
        return session, manifest

    def payload(self):
        return {"general": self.cfg["general"], **general.source_signature(self.cfg),
                "selected_shots": sorted(self.shots), "suffix_to_band": elc_priority.suffix_band_mapping(self.cfg),
                "band_names": self.cfg["band_names"], "coef_csv": self.cfg["preparation"]["coefficient_csv"],
                "apply_radiance_dir": str(self.paths["ed"]), "output_root": str(self.root),
                "reflectance_dir_name": "reflectance", "recursive": True, "overwrite": False}

    def test_nested_inputs_flat_outputs_and_duplicate_guard(self):
        self.approve()
        ed = self.paths["ed"]
        for source in list(ed.glob("*")):
            folder = ed / ("000" if "0001" in source.name else "001")
            folder.mkdir(exist_ok=True)
            source.rename(folder / source.name)
        jobs = apply.collect_jobs(self.payload(), {i: (100., 5.) for i in range(1, 11)})
        self.assertEqual(len(jobs), 20)
        self.assertTrue(all(Path(job["rel"]).parent == Path(".") for job in jobs))
        source = next((ed / "000").glob("*"))
        shutil.copy2(source, ed / "001" / source.name.upper())
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            apply.collect_jobs(self.payload(), {i: (100., 5.) for i in range(1, 11)})

    def test_scoped_case_insensitive_sources_and_selected_ambiguity(self):
        session = Session(self.config)
        for folder in (self.raw / "unrelated1", self.raw / "unrelated2"):
            folder.mkdir()
            shutil.copy2(next(self.raw.glob("*")), folder / "IMG_9999_1.tif")
            (folder / "unrelated.tif").write_bytes(b"not a selected raw file")
        self.assertEqual(Session(self.config).signatures, session.signatures)
        self.assertEqual(len(general.raw_for_shots(self.raw, self.shots)), 20)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            general.scan_raw(self.raw)
        shutil.copy2(session.files["IMG_0001_1"], self.raw / "unrelated1/IMG_0001_1.tif")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            session.assert_current()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            general.raw_for_shots(self.raw, self.shots)

    def test_source_list_bytes_and_path_invalidate_session_and_approval(self):
        session, _ = self.approve()
        original = self.target.read_bytes()
        self.target.write_bytes(original + b"\n")
        for action in (session.assert_current, lambda: elc_fit._current_manifest(self.cfg)):
            with self.assertRaises(ValueError):
                action()
        self.target.write_bytes(original)
        general.save_shot_list(self.cfg, self.shots)
        atomic_json(self.config, self.cfg)
        with self.assertRaises(ValueError):
            elc_fit._current_manifest(self.cfg)

    def test_selected_raw_stat_change_invalidates_session_and_approval(self):
        session, _ = self.approve()
        source = session.files["IMG_0002_6"]
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        with self.assertRaises(ValueError):
            session.assert_current()
        with self.assertRaisesRegex(ValueError, "source signatures changed"):
            elc_fit._current_manifest(self.cfg)

    def test_transform_and_library_tampering_block_all_general_consumers(self):
        _, path = self.approve()
        self.assertEqual(elc_fit._current_manifest(self.cfg), path)
        manifest = read_json(path)
        for field in ("transforms_json", "library_csv"):
            source = Path(manifest[field])
            original = source.read_bytes()
            source.write_bytes(original + b"\n")
            for action in (lambda: elc_fit.fit(self.cfg), lambda: elc_priority.approved_shots(self.cfg),
                           lambda: elc_priority.suffix_band_mapping(self.cfg)):
                with self.assertRaisesRegex(ValueError, "changed"):
                    action()
            source.write_bytes(original)

    def test_identity_context_and_selection_changes_never_revive_old_approval(self):
        session, _ = self.approve()
        self.cfg["general"]["session"] = "other_session"
        self.cfg["dataset"]["flight"] = "other_session"
        atomic_json(self.config, self.cfg)
        with self.assertRaises(ValueError):
            session.assert_current()
        self.cfg["general"]["session"] = self.cfg["dataset"]["flight"] = "flight12"
        atomic_json(self.config, self.cfg)
        context = session.context_path
        original = context.read_bytes()
        changed = read_json(context)
        changed["identity"] = "different_context"
        atomic_json(context, changed)
        with self.assertRaises(ValueError):
            elc_fit._current_manifest(self.cfg)
        context.write_bytes(original)
        newer = self.paths["roi"] / "workflow/another_session/reviewed_manifest_latest.json"
        atomic_json(newer, {"path": "unrelated/nonexistent.json", "draft_dirty": False})
        self.assertEqual(elc_fit._current_manifest(self.cfg), session.last_manifest)
        general.save_candidates(self.cfg, self.shots, "IMG_0001")
        with self.assertRaisesRegex(ValueError, "published ROI"):
            elc_fit._current_manifest(self.cfg)

    def test_changed_normalized_input_prevents_reuse_with_same_coefficients(self):
        self.approve()
        mapping = elc_priority.suffix_band_mapping(self.cfg)
        before = elc_priority._signature(self.cfg, mapping)
        self.paths["ref"].mkdir(parents=True, exist_ok=True)
        marker = self.paths["ref"] / "_elc_output_mapping_provenance.json"
        atomic_json(marker, {**before, "reuse_after_ns": 0})
        source = next(self.paths["ed"].glob("*"))
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        with self.assertRaises(ValueError):
            elc_priority.reuse_cutoff(self.cfg, mapping)
        self.assertNotEqual(before, elc_priority._signature(self.cfg, mapping))

    def test_all_selected_wavelengths_are_checked_without_suffix_fallback(self):
        self.approve()
        payload = self.payload()
        coefficients = apply.load_coefficients(Path(payload["coef_csv"]))
        self.assertEqual(len(apply.collect_jobs(payload, coefficients)), 20)
        source = self.paths["ed"] / "img_0002_1.TiFf"
        for value in ("444", None):
            ds = gdal.Open(str(source), gdal.GA_Update)
            ds.SetMetadataItem("CENTRAL_WAVELENGTH", value)
            ds = None
            with self.assertRaisesRegex(ValueError, "CENTRAL_WAVELENGTH"):
                elc_priority.check_sources(self.cfg, self.shots)
            with self.assertRaisesRegex(ValueError, "CENTRAL_WAVELENGTH"):
                apply.collect_jobs(payload, coefficients)

    def test_apply_is_always_restricted_and_requires_explicit_mapping(self):
        self.approve()
        payload = self.payload()
        coefficients = apply.load_coefficients(Path(payload["coef_csv"]))
        shutil.copy2(next(self.paths["ed"].glob("*")), self.paths["ed"] / "IMG_9999_1.tiff")
        payload["selected_shots"] = ["IMG_0001"]
        jobs = apply.collect_jobs(payload, coefficients)
        self.assertEqual(len(jobs), 10)
        self.assertTrue(all("img_0001" in job["rel"] for job in jobs))
        for key in ("selected_shots", "suffix_to_band"):
            broken = dict(payload, **{key: [] if key == "selected_shots" else {}})
            with self.assertRaises(ValueError):
                apply.collect_jobs(broken, coefficients)

    @unittest.skipUnless(shutil.which("exiftool"), "ExifTool required for native metadata copy")
    def test_output_stage_metadata_native_pixels_and_existing_input_reuse_guard(self):
        self.approve()
        payload = self.payload()
        coefficients = apply.load_coefficients(Path(payload["coef_csv"]))
        source = self.paths["ed"] / "img_0001_1.TiFf"
        subprocess.run([exif_executable(), "-overwrite_original", "-DateTimeOriginal=2027:06:15 12:00:00",
                        str(source)], check=True, capture_output=True)
        with tifffile.TiffFile(next(self.raw.glob("img_0001_1.*"))) as tif:
            xmp = tif.pages[0].tags[700].value
        ds = gdal.Open(str(source), gdal.GA_Update)
        ds.SetMetadata([xmp.decode()], "xml:XMP")
        ds = None
        with tifffile.TiffFile(source) as tif:
            xmp = tif.pages[0].tags[700].value
        jobs = apply.collect_jobs(payload, coefficients)
        job = next(j for j in jobs if Path(j["src"]) == source)
        before = digest(source)
        result = apply.apply_checked_job(job)
        self.assertEqual(result["status"], "ok", result)
        output = self.root / "reflectance" / job["rel"]
        ds = gdal.Open(str(output))
        self.assertEqual([ds.RasterXSize, ds.RasterYSize], [28, 16])
        self.assertEqual(ds.GetMetadataItem("PROCESSING_STAGE"), "ELC_reflectance")
        self.assertEqual(ds.GetMetadataItem("ELC_COEFFICIENT_SHA256"), digest(payload["coef_csv"]))
        self.assertEqual(ds.GetMetadataItem("INPUT_PROCESSING_STAGE"), "radiometric_sensor_ed_integrated")
        np.testing.assert_allclose(ds.ReadAsArray(), .1, atol=1e-7)
        ds = None
        with tifffile.TiffFile(output) as tif:
            self.assertEqual(ET.tostring(ET.fromstring(tif.pages[0].tags[700].value)),
                             ET.tostring(ET.fromstring(xmp)))
            self.assertEqual(tif.pages[0].tags["ExifTag"].value["DateTimeOriginal"], "2027:06:15 12:00:00")
        self.assertEqual(digest(source), before)
        self.assertEqual(apply.apply_checked_job(job)["status"], "ok")
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        changed = next(j for j in apply.collect_jobs(payload, coefficients) if Path(j["src"]) == source)
        self.assertEqual(apply.apply_checked_job(changed)["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
