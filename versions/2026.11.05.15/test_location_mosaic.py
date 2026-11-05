import hashlib
from pathlib import Path
import tempfile
import unittest

import numpy as np
from osgeo import gdal, osr
from location_mosaic import build

gdal.UseExceptions()


def photo(path, offset=0, transparent=False, bands=11):
    sr = osr.SpatialReference(); sr.ImportFromEPSG(32652)
    ds = gdal.GetDriverByName('GTiff').Create(str(path),16,16,bands,gdal.GDT_UInt16)
    ds.SetProjection(sr.ExportToWkt()); ds.SetGeoTransform((500000,1,0,3900000,0,-1))
    for i in range(1,bands+1):
        ds.GetRasterBand(i).WriteArray(np.full((16,16),i*100+offset,dtype=np.uint16))
    if bands==11:
        ds.GetRasterBand(11).WriteArray(np.full((16,16),0 if transparent else 65535,dtype=np.uint16))
        ds.GetRasterBand(11).SetColorInterpretation(gdal.GCI_AlphaBand)
    ds.FlushCache(); ds = None


class MosaicTests(unittest.TestCase):
    def test_georeference_rgb_alpha_target_filter_and_no_source_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); raw = root/'photos'; raw.mkdir()
            photo(raw/'IMG_0001_6.tif')
            photo(raw/'IMG_0002_6.tif',offset=4000,transparent=True)
            photo(raw/'IMG_9999_6.tif',offset=9000)
            hashes = {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.iterdir()}
            result = build(raw,root/'out',{'IMG_0001','IMG_0002'},limit=16)
            ds = gdal.Open(result['mosaic'])
            self.assertEqual(ds.RasterCount,4)
            self.assertEqual(ds.GetGeoTransform(),(500000,1,0,3900000,0,-1))
            self.assertTrue(np.all(ds.GetRasterBand(1).ReadAsArray()==600))
            self.assertTrue(np.all(ds.GetRasterBand(2).ReadAsArray()==400))
            self.assertTrue(np.all(ds.GetRasterBand(3).ReadAsArray()==200))
            self.assertEqual(ds.GetMetadataItem('PURPOSE'),'TARP_LOCATION_DISPLAY_ONLY_NOT_REFLECTANCE')
            ds = None
            self.assertEqual(result['source_count'],2)
            self.assertEqual(hashes,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes})

    def test_original_raw_is_rejected_and_cancellation_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); raw = root/'photos'; raw.mkdir()
            photo(raw/'IMG_0001_6.tif',bands=1)
            with self.assertRaisesRegex(ValueError,'10밴드'):
                build(raw,root/'out',{'IMG_0001'})
            with self.assertRaisesRegex(ValueError,'중지'):
                build(raw,root/'out',{'IMG_0001'},cancelled=lambda:True)
            self.assertFalse((root/'out').exists())

    def test_reduced_grid_preserves_extent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); raw = root/'photos'; raw.mkdir(); photo(raw/'IMG_0001_6.tif')
            result = build(raw,root/'out',{'IMG_0001'},limit=4)
            ds = gdal.Open(result['mosaic'])
            self.assertEqual((ds.RasterXSize,ds.RasterYSize),(4,4))
            self.assertEqual(ds.GetGeoTransform(),(500000,4,0,3900000,0,-4))
            ds = None


if __name__=='__main__': unittest.main()
