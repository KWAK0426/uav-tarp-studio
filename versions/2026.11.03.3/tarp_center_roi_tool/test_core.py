import csv
import json
from pathlib import Path
import tempfile
import unittest
import time

import cv2
import numpy as np
from osgeo import gdal
from core import Raster, detect, inset, ordered, export, roi_window


class DetectionTests(unittest.TestCase):
    def test_bright_dark_rotated_perspective_and_noise(self):
        rng = np.random.default_rng(5)
        for polygon in ([[[90, 100], [230, 85], [245, 220], [100, 235]]],
                        [[[115, 55], [260, 125], [185, 270], [40, 190]]]):
            truth = ordered(polygon[0])
            for foreground, background in ((200, 40), (30, 160)):
                image = np.full((320, 320), background, np.float32)
                mask = np.zeros((320, 320), np.uint8)
                cv2.fillConvexPoly(mask, truth.astype(int), 1)
                image[mask > 0] = foreground
                image = np.clip(image + rng.normal(0, 3, image.shape), 0, 255).astype(np.uint8)
                before = time.perf_counter()
                results = detect(image, np.ones(image.shape, bool), truth.mean(axis=0))
                self.assertTrue(results)
                predicted = np.zeros_like(mask)
                cv2.fillConvexPoly(predicted, np.rint(results[0]["points"]).astype(int), 1)
                iou = ((mask > 0) & (predicted > 0)).sum() / ((mask > 0) | (predicted > 0)).sum()
                print(f"contrast={foreground-background} IoU={iou:.4f} seconds={time.perf_counter()-before:.3f}")
                self.assertGreater(iou, .93)

    def test_no_rectangle_and_nodata(self):
        image = np.full((150, 150), 80, np.uint8)
        self.assertEqual(detect(image, np.ones_like(image, bool), [75, 75]), [])
        with self.assertRaises(ValueError):
            detect(image, np.zeros_like(image, bool), [75, 75])

    def test_inset_each_edge(self):
        quad = [[0, 0], [100, 0], [100, 50], [0, 50]]
        np.testing.assert_allclose(inset(quad, 10), [[5, 5], [95, 5], [95, 45], [5, 45]])

    def test_raster_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"test.tif"
            ds = gdal.GetDriverByName("GTiff").Create(str(path), 200, 160, 2, gdal.GDT_Float32)
            ds.SetGeoTransform((100, 2, 0, 200, 0, -2))
            ds.SetProjection('LOCAL_CS["Test"]')
            for band, value in ((1, .2), (2, .4)):
                data = np.full((160, 200), .7, np.float32)
                data[50:110, 60:140] = value
                data[75, 85] = -9999
                ds.GetRasterBand(band).WriteArray(data)
                ds.GetRasterBand(band).SetNoDataValue(-9999)
            ds = None
            raster = Raster(path)
            result = raster.propose([100, 80], 1, 90)
            self.assertTrue(result)
            outer = result[0]["points"]
            inner = inset(outer, 8).tolist()
            record = {"name": "test", "outer": outer, "inner": inner, "reviewed": True}
            report = export(raster, [record], Path(directory)/"out")
            self.assertEqual(json.loads(report.read_text())["source"], str(path.resolve()))
            with (report.parent/"pixel_statistics.csv").open(encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertAlmostEqual(float(rows[0]["mean_stored"]), .2, places=6)
            self.assertAlmostEqual(float(rows[1]["mean_stored"]), .4, places=6)
            window, mask = roi_window(raster, inner)
            self.assertEqual(int(rows[0]["valid_pixels"]), int(mask.sum())-1)
            saved_mask = gdal.Open(str(report.parent/"roi_001_inner_mask.tif"))
            np.testing.assert_array_equal(saved_mask.ReadAsArray(), mask.astype(np.uint8))
            self.assertEqual(saved_mask.GetGeoTransform()[0], 100+2*window[0])
            saved_mask = None
            raster.ds = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
