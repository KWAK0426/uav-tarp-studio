"""Project-start dialog checks; no field imagery or processing is opened."""
import json
import os
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

import distribution as d

d.configure()


class StartupTests(unittest.TestCase):
    def setUp(self):
        warning = patch("tkinter.messagebox.showwarning")
        self.warning = warning.start()
        self.addCleanup(warning.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.work = Path(self.folder.name) / "results"
        self.root = tk.Tk()
        self.window = d.ProjectWizard(self.root)
        self.root.update()

    def tearDown(self):
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except tk.TclError:
            pass
        self.folder.cleanup()

    def fill(self):
        for key, value in dict(date="2027-06-10", site="농과원", flight="3rd", work=str(self.work)).items():
            self.window.values[key].set(value)
        self.root.update()

    def test_style_and_incomplete_inputs(self):
        self.assertEqual(self.root.cget("background"), "#17191c")
        self.assertEqual(ttk.Style(self.root).lookup("Primary.TButton", "background"), "#75d4bc")
        self.assertEqual(self.window.values["site"].get(), "")
        self.assertTrue(self.window.create_button.instate(("disabled",)))
        self.assertIn("YYYY-MM-DD", self.window.status.get())
        self.assertFalse(self.work.exists())

    def test_preview_validation_and_no_creation_until_confirmed(self):
        self.fill()
        self.assertTrue(self.window.create_button.instate(("!disabled",)))
        self.assertEqual(self.window.preview.get(), str(self.work / "농과원/2027-06-10/3rd"))
        self.assertIn("아직 영상 처리는 시작하지 않습니다", self.window.status.get())
        self.assertFalse(self.work.exists())
        for key, bad in (("date", "2027-02-30"), ("date", "20270610"), ("site", "../bad"),
                         ("site", "CON"), ("flight", "a/b"), ("work", "relative"), ("work", "")):
            with self.subTest(key=key, value=bad):
                before = self.window.values[key].get()
                self.window.values[key].set(bad)
                self.assertTrue(self.window.create_button.instate(("disabled",)))
                self.window.values[key].set(before)

    def test_create_is_empty_and_does_not_overwrite(self):
        self.fill()
        with patch.object(d, "remember") as remember:
            self.window.create()
        path = self.work / "농과원/2027-06-10/3rd/project.json"
        self.assertEqual(self.window.result, path)
        remember.assert_called_once_with(path)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["preparation"]["raw_directory"], "")
        self.assertFalse(cfg["preparation"]["coefficient_basis_confirmed"])
        with self.assertRaises(FileExistsError):
            d.create_project(self.work, "2027-06-10", "3rd", "농과원")

    def test_existing_project_is_opened_without_rewriting(self):
        self.fill()
        with patch.object(d, "remember"):
            path = d.create_project(self.work, "2027-06-10", "3rd", "농과원")
        before = path.read_bytes()
        self.window.refresh()
        self.assertTrue(self.window.create_button.instate(("disabled",)))
        self.assertIn("이미 있습니다", self.window.status.get())
        with patch("tkinter.filedialog.askopenfilename", return_value=str(path)):
            self.window.open_existing()
        self.assertEqual(self.window.result, path)
        self.assertEqual(path.read_bytes(), before)

    def test_wrong_existing_project_and_cancel(self):
        wrong = Path(self.folder.name) / "wrong.json"
        wrong.write_text("[]", encoding="utf-8")
        with patch("tkinter.filedialog.askopenfilename", return_value=str(wrong)):
            self.window.open_existing()
        self.assertIsNone(self.window.result)
        self.assertIn("project.json", self.window.status.get())
        with patch("tkinter.filedialog.askopenfilename", return_value=""):
            self.window.open_existing()
        self.assertIsNone(self.window.result)

    def test_new_project_entry_point_initializes_theme_import_paths(self):
        real_wizard = d.ProjectWizard
        def cancel(root):
            window = real_wizard(root)
            def close():
                root.quit()
                root.destroy()
            root.after(20, close)
            return window
        with patch.object(d, "configure", wraps=d.configure) as configure, patch.object(d, "ProjectWizard", side_effect=cancel):
            self.assertIsNone(d.wizard())
        configure.assert_called_once_with()

    def test_responsive_layout_and_screenshots(self):
        from PIL import ImageGrab
        self.fill()
        for width, height in ((920, 750), (760, 630)):
            self.root.geometry(f"{width}x{height}+30+30")
            self.root.update()
            for widget in (*self.window.entries.values(), self.window.preview_label,
                           self.window.status_label, self.window.create_button):
                x = widget.winfo_rootx() - self.root.winfo_rootx()
                y = widget.winfo_rooty() - self.root.winfo_rooty()
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width())
                self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height())
            if folder := os.environ.get("ELC_BOOTSTRAP_SCREENSHOTS"):
                target = Path(folder)
                target.mkdir(parents=True, exist_ok=True)
                self.root.attributes("-topmost", True)
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
                self.root.update()
                self.root.after(800, self.root.quit)
                self.root.mainloop()
                x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
                screenshot = ImageGrab.grab(bbox=(x, y, x + self.root.winfo_width(), y + self.root.winfo_height()))
                self.assertEqual(screenshot.getpixel((10, 10))[:3], (23, 25, 28), "Test window is occluded")
                screenshot.save(target / f"startup_{width}x{height}.png")


if __name__ == "__main__":
    unittest.main(verbosity=2)
