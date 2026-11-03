"""Small synthetic regression suite; no real dataset or network access."""
import tempfile
import unittest
from pathlib import Path
import distribution

distribution.configure()
folder = distribution.STATE / "test_temp"
folder.mkdir(parents=True, exist_ok=True)
# Test projects must never replace the user's remembered working project.
test_state = tempfile.TemporaryDirectory(prefix="elc_test_")
distribution.STATE = Path(test_state.name)
tempfile.tempdir = str(folder)
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in (
    "test_distribution", "tarp_center_roi_tool.test_core", "tarp_center_roi_tool.test_automation",
    "test_general_backend", "test_general_provenance", "test_startup_ui", "test_ui_feedback"))
if __name__ == "__main__":
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
