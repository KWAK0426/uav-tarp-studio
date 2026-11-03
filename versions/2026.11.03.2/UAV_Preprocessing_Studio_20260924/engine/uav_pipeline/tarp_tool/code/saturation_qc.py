"""Raw-DN saturation checks; full-frame results are only a candidate screen."""
from pathlib import Path

import cv2
import numpy as np
from osgeo import gdal

gdal.UseExceptions()


SATURATION_DN = 65520
QC_VERSION = 1


def _valid(array, nodata):
    valid = np.isfinite(array) & (array != 65535)
    if nodata is not None:
        valid &= array != nodata
    return valid


def scan_frame(paths):
    """No clipped pixels anywhere proves no clipped tarp pixels; the converse does not."""
    if len(paths) != 10:
        return {"status": "unknown", "reason": "10밴드 원영상 목록이 불완전합니다", "bands": []}
    bands = []
    for number, path in enumerate(paths, 1):
        path = Path(path) if path else None
        if path is None or not path.is_file():
            return {"status": "unknown", "reason": f"B{number} 원영상 없음", "bands": bands}
        try:
            ds = gdal.Open(str(path), gdal.GA_ReadOnly)
            band = ds.GetRasterBand(1)
            nodata = band.GetNoDataValue()
            count = clipped = 0
            for y in range(0, ds.RasterYSize, 256):
                array = band.ReadAsArray(0, y, ds.RasterXSize, min(256, ds.RasterYSize - y))
                valid = _valid(array, nodata)
                count += int(np.count_nonzero(valid))
                clipped += int(np.count_nonzero(valid & (array >= SATURATION_DN)))
            if count == 0:
                raise ValueError("유효 화소 없음")
            bands.append({"band": number, "valid_pixels": count, "saturated_pixels": clipped,
                          "saturated_pct": round(100 * clipped / count, 4)})
            ds = None
        except Exception as exc:
            return {"status": "unknown", "reason": f"B{number} 검사 실패: {exc}", "bands": bands}
    return {"status": "review" if any(b["saturated_pixels"] for b in bands) else "clear",
            "reason": "원영상 일부 포화; 타프 ROI 확정 후 재검사" if any(b["saturated_pixels"] for b in bands) else "원영상 10밴드 포화 없음",
            "bands": bands, "threshold_dn": SATURATION_DN, "scope": "full_native_frame", "version": QC_VERSION}


def scan_roi(path, points, expected_size, sampling=None):
    """Exact raw-DN saturation fraction inside one approved native-band ROI."""
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None or [ds.RasterXSize, ds.RasterYSize] != list(expected_size):
        raise ValueError(f"원영상 또는 ROI 크기 불일치: {path}")
    array = ds.GetRasterBand(1).ReadAsArray()
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    if sampling == "pixel_center_v1":
        from ...roi_studio_bridge import pixel_mask
        mask = pixel_mask(points, array.shape)
    else:
        mask = np.zeros(array.shape, np.uint8)
        cv2.fillConvexPoly(mask, np.rint(np.asarray(points)).astype(np.int32), 1)
    valid = (mask > 0) & _valid(array, nodata)
    count = int(np.count_nonzero(valid))
    if not count:
        raise ValueError(f"원영상 ROI 유효 화소 없음: {path}")
    clipped = int(np.count_nonzero(valid & (array >= SATURATION_DN)))
    return {"valid_pixels": count, "saturated_pixels": clipped,
            "saturated_pct": round(100 * clipped / count, 4), "threshold_dn": SATURATION_DN,
            "scope": "approved_native_tarp_roi"}
