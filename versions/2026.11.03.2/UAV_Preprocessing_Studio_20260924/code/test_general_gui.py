"""Synthetic GUI contract checks. No real data, processing, or ROI approval.

Run with the configured Studio Python: python code/test_general_gui.py
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
import subprocess
import sys
import threading

import general_studio as gui
from uav_pipeline.config import DEFAULTS, save_config
from uav_pipeline.preparation import CHOICES, latest_candidate_selection


class GeneralGuiTest(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / "verification" / "general_gui"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic_", dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.assertTrue(self.directory.resolve().is_relative_to(test_root.resolve()))
        self.raw = self.directory / "raw"
        self.raw.mkdir()
        self.cfg = deepcopy(DEFAULTS)
        self.cfg["general"] = dict(enabled=True, project_root=str(self.directory),
                                   date="2031-03-04", session="Dawn_02", site="Test")
        self.cfg["dataset"] = dict(root=str(self.directory), date="2031_03_04_Test",
                                   flight="Dawn_02", basis="Panel", paths={}, input_settings_reviewed=False)
        self.cfg["preparation"] = deepcopy(CHOICES)
        # Header-only inputs intentionally block scientific processing.
        for key, header in (("gain_csv", "band,gain"), ("ed_csv", "datetime_kst,B01"),
                            ("tarp_reflectance_csv", "key,B01")):
            path = self.directory / (key + ".csv")
            path.write_text(header + "\n", encoding="utf-8")
            self.cfg["preparation"][key] = str(path)
        self.cfg["preparation"].update(raw_directory="", gps_path="", shot_list_csv="")
        self.cfg["output"]["directory"] = str(self.directory / "Analysis")
        self.path = self.directory / "project.json"
        save_config(self.path, self.cfg)
        self.errors = []
        self.root = tk.Tk()
        self.addCleanup(self.cleanup_root)
        error_dialog = patch.object(gui.messagebox, "showerror", side_effect=lambda *args, **kwargs: self.fail(str(args)))
        error_dialog.start()
        self.addCleanup(error_dialog.stop)
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.app = gui.GeneralStudio(self.root, self.path)
        self.pump()

    def cleanup_root(self):
        try:
            if self.root.winfo_exists():
                if hasattr(self, "app"):
                    self.app.close()
                else:
                    self.root.destroy()
        except tk.TclError:
            pass

    def tearDown(self):
        self.app.process = self.app.background_process = None
        self.app.launch_pending = self.app.background_launch_pending = self.app._scan_pending = False
        self.app.close()
        self.assertEqual(self.errors, [])

    def test_cli_smoke(self):
        self.root.withdraw()
        directory = Path(__file__).resolve().parents[1] / "verification" / "general_gui"
        for stage in (1, 3, 4, 5):
            screenshot = directory / f"stage{stage:02d}.png"
            result = subprocess.run([sys.executable, "-u", str(Path(gui.__file__)), "--config", str(self.path),
                                     "--smoke", "--stage", str(stage), "--screenshot", str(screenshot)],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("GENERAL_GUI_SMOKE_OK", result.stdout, f"stage={stage}, exit={result.returncode}, stderr={result.stderr}")
            self.assertNotIn("GUI_CALLBACK_ERROR", result.stderr)
            self.assertTrue(screenshot.is_file())

    def test_blocked_action_has_live_reason_and_does_not_run(self):
        hint = self.app.action_hints["완전한 촬영분을 대상 목록에 등록"]
        self.assertIn("스캔", hint.text())
        self.assertTrue(self.app.register_button.instate(("disabled",)))
        with patch.object(gui.messagebox, "showwarning") as dialog, patch.object(gui, "inspect_raw") as scan:
            self.app.register_button.event_generate("<Button-1>")
            self.pump()
            dialog.assert_called_once()
            self.assertIn("다음 조치", dialog.call_args.args[1])
            scan.assert_not_called()
        self.app._scan_pending = True
        self.assertIn("검사 중", hint.text())
        self.app._scan_pending = False

    def test_hover_and_help_summary_never_read_native_pixels(self):
        with patch.object(gui, "inspect_raw", side_effect=AssertionError("hover scanned native inputs")), \
                patch.object(gui, "preview_image", side_effect=AssertionError("hover read native pixels")), \
                patch.object(gui.messagebox, "showinfo") as dialog:
            for hint in self.app.action_hints.values():
                self.assertIsInstance(hint.text(), str)
            self.app.help_button.invoke()
            self.assertIn("진행 조건", dialog.call_args.args[0])
            self.assertIn("원영상 스캔", dialog.call_args.args[1])
            self.assertIn("처리를 재시작하지 않습니다", dialog.call_args.args[1])

    def test_failure_popup_keeps_true_error_and_full_log(self):
        self.app._append("ValueError: 실측 Ed 매칭 시간차 초과\nsubprocess.CalledProcessError: code 1\n")
        with patch.object(gui.messagebox, "showerror") as dialog:
            path = self.app._record_failure("easy_elc", 1)
        self.assertTrue(path.is_file())
        self.assertIn("CalledProcessError", path.read_text(encoding="utf-8"))
        self.assertIn("실측 Ed 매칭 시간차 초과", dialog.call_args.args[1])
        self.assertIn("원문 실패 로그", dialog.call_args.args[1])
        self.assertIn("04 방사·센서·Ed", dialog.call_args.args[1])

    def test_scope_conflict_and_corrupt_state_are_not_silent(self):
        self.app.easy_full.set(True)
        self.app.elc_only.set(True)
        self.assertIn("동시에 선택", self.app._action_reason("1. 자동 보정·계수 준비"))
        from uav_pipeline.easy_elc import pointer_path
        pointer_path(self.app.cfg).write_text("[]", encoding="utf-8")
        self.assertIn("상태 기록을 읽을 수 없습니다", self.app._action_reason("2. 계수 확인·반사도 생성"))

    def pump(self, seconds=0.1):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.root.update()
            time.sleep(0.01)

    def screenshot(self, name):
        from PIL import ImageGrab
        self.root.title("ELC Studio General [synthetic GUI test]")
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.pump()
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        path = Path(__file__).resolve().parents[1] / "verification" / "general_gui" / name
        ImageGrab.grab(bbox=(x, y, x + self.root.winfo_width(), y + self.root.winfo_height())).save(path)
        self.root.attributes("-topmost", False)

    def raw_shot(self, shot, folder=""):
        from osgeo import gdal
        import numpy as np
        directory = self.raw / folder
        directory.mkdir(exist_ok=True)
        for band in range(1, 11):
            dataset = gdal.GetDriverByName("GTiff").Create(str(directory / f"{shot}_{band}.tif"), 240, 160, 1, gdal.GDT_UInt16)
            dataset.GetRasterBand(1).WriteArray(np.arange(240 * 160, dtype=np.uint16).reshape(160, 240))
            dataset = None

    def connect(self):
        self.raw_shot("IMG_0001")
        self.raw_shot("IMG_0002", "001")
        # Exercise the actual cold-start buttons, not a prepopulated manifest.
        self.assertEqual(self.app._var("preparation.shot_list_csv").get(), "")
        with patch.object(gui.filedialog, "askdirectory", return_value=str(self.raw)):
            self.app.browse("preparation.raw_directory", "directory")
        self.app.scan_sources()
        until = time.monotonic() + 10
        while self.app._scan_pending and time.monotonic() < until:
            self.pump()
        self.assertFalse(self.app._scan_pending)
        self.assertEqual(self.app._inventory["complete"], ["IMG_0001", "IMG_0002"])
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            self.app.register_complete()
        self.assertEqual(gui.general_api().source_shots(self.app.cfg), {"IMG_0001", "IMG_0002"})
        self.assertTrue(gui.general_api().source_list_path(self.app.cfg).is_file())
        self.assertEqual(self.app.cfg["preparation"]["gps_path"], "")
        self.assertEqual(self.app.pre_steps.index(self.app.pre_steps.select()), 1)
        self.pump()

    def test_initial_handles_optional_tabs_reload_and_busy(self):
        self.assertFalse(self.app.dataset_selector.winfo_ismapped())
        self.assertEqual(self.app.dataset_selector.date_var.get(), "2031_03_04_Test")
        self.assertIn("Dawn_02", self.app.identity_text.get())
        self.assertEqual(len(self.app.tabs.tabs()), 1)
        self.assertFalse(hasattr(self.app, "analysis_button"))
        for _ in range(2):
            self.app._load_to_form()
            self.app._toggle_pre_paths()
        for controls in self.app.managed_path_controls.values():
            self.assertTrue(all(widget.winfo_exists() for widget in controls[:2]))
        self.assertNotIn("preparation.raw_directory", self.app.managed_path_controls)
        self.assertFalse(self.app.coefficient_confirmed.get())
        self.assertFalse(self.app.inputs_reviewed.get())
        self.assertEqual(self.app.cfg["preparation"]["gps_path"], "")
        with patch.object(gui.messagebox, "showwarning"):
            self.app.background_launch_pending = True
            self.assertFalse(self.app.save(quiet=True))
            self.assertFalse(self.app.close())
            self.app.background_launch_pending = False
        for stage in range(5):
            self.app.pre_steps.select(stage)
            self.pump()
            self.assertFalse(self.app.full_radiometric_button.winfo_ismapped())
            self.assertFalse(self.app.full_reflectance_button.winfo_ismapped())
        self.root.geometry("1100x780+20+20")
        for stage in (0, 3, 4):
            self.app.pre_steps.select(stage)
            self.screenshot(f"small_stage{stage + 1:02d}.png")
            for widget in (self.app.pre_steps, self.app.progress, self.app.stop_button):
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())
        self.assertEqual(self.app.cfg["preparation"]["gps_path"], "")

    def test_inventory_candidates_clear_preview_and_source_invalidation(self):
        self.connect()
        report = gui.inspect_raw(str(self.raw))
        self.assertEqual(report["complete"], ["IMG_0001", "IMG_0002"])
        self.app.target_tree.selection_set(["IMG_0001"])
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            self.assertTrue(self.app.save_candidates())
        self.assertEqual(latest_candidate_selection(self.app.cfg)[2], {"IMG_0001"})
        self.app._request_preview()
        until = time.monotonic() + 10
        while self.app._preview_pending and time.monotonic() < until:
            self.pump()
        self.assertIsNotNone(self.app._preview_image)
        self.assertEqual(self.app._preview_image.size, (240, 160))
        self.assertEqual(gui.preview_image(self.raw / "IMG_0001_1.tif", limit=24).size, (24, 16))
        self.screenshot("stage02_native_preview.png")
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            self.app.clear_candidates()
        try:
            self.assertEqual(latest_candidate_selection(self.app.cfg)[2], set())
        except ValueError:
            pass  # An explicit empty latest selection may be a deliberate blocker.
        self.assertEqual(self.app._saved_candidates, set())
        self.assertTrue(self.app.load(self.path))
        self.assertEqual(self.app.target_tree.selection(), ())
        self.app.target_tree.selection_set(["IMG_0002"])
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            self.assertTrue(self.app.save_candidates())
        # Same manifest path, different bytes must invalidate source-dependent state.
        manifest = gui.general_api().source_list_path(self.app.cfg)
        manifest.write_text("shot\nIMG_0001\n", encoding="utf-8")
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            self.assertTrue(self.app.save(quiet=True))
        self.assertEqual(self.app.target_tree.get_children(), ("IMG_0001",))
        self.assertEqual(self.app._saved_candidates, set())
        self.assertFalse(self.app.coefficient_confirmed.get())
        self.assertEqual(self.app.cfg["preparation"]["reference_shot"], "")
        self.assertEqual(self.app.cfg["preparation"]["gps_path"], "")

    def test_inventory_reports_incomplete_and_duplicate(self):
        self.raw_shot("IMG_0001")
        (self.raw / "IMG_0002_1.tif").write_bytes(b"synthetic-name-only")
        duplicate = self.raw / "001"
        duplicate.mkdir()
        (duplicate / "IMG_0001_1.tif").write_bytes(b"duplicate-name-only")
        (self.raw / "IMG_10000_1.tif").write_bytes(b"unsupported-name-only")
        (self.raw / "IMG_0003_1.tif").touch()
        result = gui.inspect_raw(str(self.raw))
        self.assertTrue(result["duplicates"])
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["complete"], [])
        self.assertIn("IMG_0002", result["report"])
        self.assertIn(str(duplicate), result["report"])
        self.assertEqual(len(result["ignored"]), 1)
        self.assertIn("IMG_10000", result["report"])

    def test_preview_callback_after_close_and_init_failure(self):
        ready, release = threading.Event(), threading.Event()
        def delayed_preview():
            ready.set()
            release.wait(5)
            return None
        self.app._preview_pending = True
        self.app._async("preview", ("", "IMG_0001", 1), delayed_preview)
        self.assertTrue(ready.wait(2))
        self.assertTrue(self.app.close())
        release.set()
        kind, _, _, error = self.app._events.get(timeout=5)
        self.assertEqual(kind, "preview")
        self.assertIsNone(error)
        root = tk.Tk()
        with patch.object(gui.GeneralStudio, "_load_to_form", side_effect=RuntimeError("synthetic init failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic init failure"):
                gui.GeneralStudio(root, self.path)
        with self.assertRaises(tk.TclError):
            root.winfo_exists()
        root = tk.Tk()
        with self.assertRaises(FileNotFoundError):
            gui.GeneralStudio(root, self.directory / "missing.json")
        with self.assertRaises(tk.TclError):
            root.winfo_exists()


if __name__ == "__main__":
    unittest.main()
