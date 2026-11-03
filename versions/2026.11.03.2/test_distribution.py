import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

import distribution as d

d.configure()


class DistributionTests(unittest.TestCase):
    def test_smoke_does_not_replace_remembered_project(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(d, "remember") as remember:
            project = d.create_project(folder, "2027-06-10", "3rd", "Test")
            remember.reset_mock()
            with patch.object(d.subprocess, "call", return_value=0):
                self.assertEqual(d.start(project, smoke=True), 0)
            remember.assert_not_called()

    def test_brdf_dispatch_is_not_part_of_this_edition(self):
        proc = subprocess.run([sys.executable, "-m", "uav_pipeline.runner", "--config", "unused.json", "--stage", "brdf"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid choice", proc.stderr)

    def test_fresh_project_has_no_sources_or_approvals(self):
        from uav_pipeline.preparation import locations, build
        with tempfile.TemporaryDirectory(prefix="ELC portable ") as folder:
            p = d.create_project(folder, "2026-06-10", "3rd", "검증")
            prep = json.loads(p.read_text(encoding="utf-8"))
            self.assertTrue(prep["general"]["enabled"])
            self.assertEqual(prep["preparation"]["gps_path"], "")
            self.assertEqual(prep["preparation"]["shot_list_csv"], "")
            self.assertTrue(prep["preparation"]["elc_only"])
            self.assertEqual(prep["preparation"]["worker_count"], 1)
            self.assertFalse(prep["preparation"]["coefficient_basis_confirmed"])
            self.assertTrue(locations(prep)["base"].is_relative_to(Path(folder)))
            _, issues, _ = build(prep)
            self.assertTrue(issues["radiometric"])
            self.assertTrue(issues["ed"])
            self.assertTrue(issues["elc_apply"])
            with self.assertRaises(FileExistsError):
                d.create_project(folder, "2026-06-10", "3rd", "검증")

    def test_bad_identity_and_nonportable_paths_rejected(self):
        with self.assertRaises(ValueError):
            d.create_project(Path.cwd(), "2027-02-30", "flight12")
        with self.assertRaises(ValueError):
            d.create_project("relative", "2026-06-10", "1st")
        with self.assertRaises(ValueError):
            d.create_project(Path.cwd(), "2026-06-10", "1st", "../other")
        cfg = d.preparation_template()
        for path in cfg["preprocess"]["tools"].values():
            self.assertTrue(Path(path).is_relative_to(d.ROOT))
            self.assertTrue(Path(path).is_file())

    def test_candidate_web_assets_are_shipped(self):
        from uav_pipeline.tarp_workflow import _render_web
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _render_web({"source_kind": "raw_DN_boundary_only"}, root)
            for name in ("index.html", "roi.html", "review.html", "studio.js", "tracking_ui.js"):
                self.assertTrue((root / "web" / name).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
