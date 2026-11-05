"""Untrained, center-guided quadrilateral proposals in source pixel coordinates."""
from pathlib import Path
import csv
import json
import uuid
import sys

import cv2
import numpy as np
from osgeo import gdal

gdal.UseExceptions()
if getattr(sys, "frozen", False):
    # Pass UTF-8 through the API: Windows narrow getenv can corrupt Korean paths.
    from osgeo import osr
    osr.SetPROJSearchPaths([str(Path(sys._MEIPASS)/"proj")])


def stretch(data, valid):
    values = data[valid]
    if not values.size:
        raise ValueError("No valid pixels in this window.")
    lo, hi = np.percentile(values, [1, 99])
    if hi <= lo:
        lo, hi = float(values.min()), float(values.max())
    result = np.zeros(data.shape, np.uint8)
    if hi > lo:
        result[valid] = np.clip((data[valid] - lo) * 255 / (hi - lo), 0, 255)
    return result


def ordered(points):
    p = np.asarray(points, np.float32).reshape(4, 2)
    center = p.mean(axis=0)
    p = p[np.argsort(np.arctan2(p[:, 1] - center[1], p[:, 0] - center[0]))]
    return np.roll(p, -int(np.argmin(p.sum(axis=1))), axis=0)


def valid_quad(points):
    p = np.asarray(points, np.float32)
    return (p.shape == (4, 2) and np.isfinite(p).all()
            and cv2.isContourConvex(p) and abs(cv2.contourArea(p)) >= 9)


def inset(points, percent):
    """Inset each edge by percent of the shortest altitude, not a centroid shrink."""
    p = ordered(points)
    if not 0 <= percent <= 30:
        raise ValueError("Inner margin must be 0 to 30 percent.")
    edges = np.roll(p, -1, axis=0) - p
    lengths = np.linalg.norm(edges, axis=1)
    normals = np.column_stack([-edges[:, 1], edges[:, 0]]) / lengths[:, None]
    heights = np.array([np.max((p - p[i]) @ normals[i]) for i in range(4)])
    distance = float(heights.min() * percent / 100)
    constants = np.sum(normals * p, axis=1) + distance
    q = np.array([np.linalg.solve(normals[[i - 1, i]], constants[[i - 1, i]])
                  for i in range(4)], np.float32)
    if not valid_quad(q) or any(cv2.pointPolygonTest(p, tuple(map(float, v)), False) < 0 for v in q):
        raise ValueError("Inner ROI collapsed; reduce the margin.")
    return q


class Raster:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.ds = gdal.Open(str(self.path))
        self.width, self.height = self.ds.RasterXSize, self.ds.RasterYSize
        self.bands = [i for i in range(1, self.ds.RasterCount + 1)
                      if self.ds.GetRasterBand(i).GetColorInterpretation() != gdal.GCI_AlphaBand]
        self.transform = self.ds.GetGeoTransform(can_return_null=True)
        self.projection = self.ds.GetProjection()

    def read(self, band, window=None, max_side=None):
        x, y, w, h = window or (0, 0, self.width, self.height)
        ratio = min(1, max_side / max(w, h)) if max_side else 1
        bw, bh = max(1, round(w * ratio)), max(1, round(h * ratio))
        b = self.ds.GetRasterBand(band)
        data = b.ReadAsArray(x, y, w, h, buf_xsize=bw, buf_ysize=bh)
        if np.iscomplexobj(data):
            raise ValueError("Complex rasters are not supported.")
        data = data.astype(np.float64)
        valid = b.GetMaskBand().ReadAsArray(x, y, w, h, buf_xsize=bw, buf_ysize=bh) > 0
        valid &= np.isfinite(data)
        nd = b.GetNoDataValue()
        if nd is not None:
            valid &= data != nd
        return data, valid

    def preview(self, band):
        data, valid = self.read(band, max_side=1800)
        return stretch(data, valid)

    def propose(self, center, band, radius):
        x, y = center
        x0, y0 = max(0, int(x - radius)), max(0, int(y - radius))
        x1, y1 = min(self.width, int(x + radius + 1)), min(self.height, int(y + radius + 1))
        window = (x0, y0, x1 - x0, y1 - y0)
        data, valid = self.read(band, window, max_side=1500)
        scale = np.array([window[2] / data.shape[1], window[3] / data.shape[0]])
        local = (np.array(center) - [x0, y0]) / scale - .5
        candidates = detect(stretch(data, valid), valid, local)
        for c in candidates:
            # GDAL resampling samples pixel centers; exported coordinates use pixel edges.
            c["points"] = ((np.asarray(c["points"]) + .5) * scale + [x0, y0]).tolist()
        return candidates


def detect(image, valid, center):
    h, w = image.shape
    cx, cy = map(lambda v: int(round(v)), center)
    if not (0 <= cx < w and 0 <= cy < h and valid[cy, cx]):
        raise ValueError("The selected center is outside valid image pixels.")
    smooth = cv2.GaussianBlur(image, (3, 3), .7)
    candidates = []
    contours = []
    # Fixed-range growing prevents gradually leaking through textured backgrounds.
    for tolerance in (5, 10, 18, 28, 42, 60):
        mask = np.zeros((h + 2, w + 2), np.uint8)
        mask[1:-1, 1:-1][~valid] = 1
        cv2.floodFill(smooth.copy(), mask, (cx, cy), 0, tolerance, tolerance,
                      cv2.FLOODFILL_FIXED_RANGE | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
        region = (mask[1:-1, 1:-1] == 255).astype(np.uint8) * 255
        region = cv2.morphologyEx(region, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours.extend(cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0])
    for low, high in ((15, 45), (35, 100), (65, 170)):
        edge = cv2.Canny(smooth, low, high)
        edge[~valid] = 0
        edge = cv2.morphologyEx(edge, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours.extend(cv2.findContours(edge, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[0])
    for contour in contours:
        area = abs(cv2.contourArea(contour))
        if area < 36 or area > .85 * w * h:
            continue
        if cv2.pointPolygonTest(contour, (float(cx), float(cy)), False) <= 0:
            continue
        hull = cv2.convexHull(contour)
        for epsilon in (.015, .025, .04, .06):
            approx = cv2.approxPolyDP(hull, epsilon * cv2.arcLength(hull, True), True)
            if len(approx) != 4:
                continue
            p = ordered(approx)
            if not valid_quad(p) or p[:, 0].min() < 2 or p[:, 1].min() < 2 or p[:, 0].max() > w - 3 or p[:, 1].max() > h - 3:
                continue
            qa = abs(cv2.contourArea(p))
            if not .72 < area / qa < 1.18:
                continue
            sides = np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1)
            if sides.min() < 5 or sides.max() / sides.min() > 6:
                continue
            centerness = np.linalg.norm(p.mean(axis=0) - [cx, cy]) / np.sqrt(qa)
            if centerness > .4:
                continue
            inner = np.zeros_like(image)
            cv2.fillConvexPoly(inner, np.rint(inset(p, 12)).astype(np.int32), 1)
            if np.count_nonzero((inner > 0) & valid) < .95 * np.count_nonzero(inner):
                continue
            # Score all four edges; a strong single shadow line is insufficient.
            contrasts = []
            for i in range(4):
                start, end = p[i], p[(i + 1) % 4]
                tangent = end - start
                normal = np.array([-tangent[1], tangent[0]]) / np.linalg.norm(tangent)
                samples = start + np.linspace(.15, .85, 25)[:, None] * tangent
                step = max(2, min(5, sides.min() * .08))
                inside, outside = np.rint(samples + normal * step).astype(int), np.rint(samples - normal * step).astype(int)
                if any(np.any((a[:, 0] < 0) | (a[:, 0] >= w) | (a[:, 1] < 0) | (a[:, 1] >= h)) for a in (inside, outside)):
                    contrasts.append(0)
                    continue
                good = valid[inside[:, 1], inside[:, 0]] & valid[outside[:, 1], outside[:, 0]]
                diff = smooth[inside[:, 1], inside[:, 0]].astype(float) - smooth[outside[:, 1], outside[:, 0]].astype(float)
                contrasts.append(float(np.median(np.abs(diff[good]))) if good.sum() > 18 else 0)
            minimum = min(contrasts)
            if minimum < 5:
                continue
            variation = float(np.std(smooth[inner > 0]))
            score = float(np.mean(contrasts) + minimum - variation * .3 - centerness * 60)
            if any(np.mean(np.linalg.norm(p - np.array(c["points"]), axis=1)) < max(3, np.sqrt(qa) * .08) for c in candidates):
                continue
            candidates.append({"points": p.tolist(), "score": score, "min_edge_contrast_u8": minimum,
                               "interior_std_u8": variation, "method": "center_contour_quad", "reviewed": False})
            break
    return sorted(candidates, key=lambda c: c["score"], reverse=True)[:12]


def roi_window(raster, points):
    p = np.asarray(points)
    x, y = np.maximum(0, np.floor(p.min(axis=0)).astype(int))
    xmax, ymax = np.minimum([raster.width, raster.height], np.ceil(p.max(axis=0)).astype(int))
    w, h = int(xmax - x), int(ymax - y)
    if w <= 0 or h <= 0:
        raise ValueError("Empty ROI.")
    # Pixel centers, not rounded polygon vertices, decide statistical membership.
    yy, xx = np.mgrid[y:y+h, x:x+w]
    mask = np.ones((h, w), bool)
    for a, b in zip(p, np.roll(p, -1, axis=0)):
        mask &= (b[0] - a[0]) * (yy + .5 - a[1]) - (b[1] - a[1]) * (xx + .5 - a[0]) >= 0
    return (int(x), int(y), w, h), mask


def statistics(raster, roi):
    """Bandwise stored-value statistics; spatial trim and value filtering stay distinct."""
    window, inside = roi_window(raster, roi["inner"])
    if roi.get("exclusion_strokes"):
        yy, xx = np.mgrid[window[1]:window[1]+window[3], window[0]:window[0]+window[2]]
        excluded = np.zeros(inside.shape, bool)
        for x, y, radius, remove in roi["exclusion_strokes"]:
            circle = (xx-round(x-.5))**2 + (yy-round(y-.5))**2 <= int(radius)**2
            excluded[circle] = bool(remove)
        inside &= ~excluded
    _, outer = roi_window(raster, roi["outer"])
    rule = roi.get("percentile_filter", {})
    low, high = float(rule.get("low", 5)), float(rule.get("high", 95))
    if not 0 <= low < high <= 100:
        raise ValueError("Invalid percentile limits")
    rows = []
    for band in raster.bands:
        data, valid = raster.read(band, window)
        values = data[inside & valid]
        count_before = int(values.size)
        lower = upper = None
        if values.size and rule.get("enabled", False):
            lower, upper = np.percentile(values, [low, high])
            values = values[(values >= lower) & (values <= upper)]
        rows.append({"band": band, "outer_count": int(outer.sum()), "before_count": count_before,
                     "count": int(values.size), "low_value": None if lower is None else float(lower),
                     "high_value": None if upper is None else float(upper),
                     "mean": float(values.mean()) if values.size else None,
                     "std": float(values.std()) if values.size else None})
    return rows


def export(raster, records, folder):
    """Publish only complete exports; failed writes remain marked .partial."""
    destination = Path(folder).resolve()
    if destination.exists():
        raise FileExistsError(f"Output already exists: {destination}")
    if not records or any(not r.get("reviewed") or not valid_quad(r["outer"]) or not valid_quad(r["inner"]) for r in records):
        raise ValueError("Only valid, confirmed ROIs may be exported.")
    staging = destination.parent / ("." + destination.name + ".partial-" + uuid.uuid4().hex[:8])
    _export_files(raster, records, staging)
    staging.rename(destination)
    return destination / "roi.json"


def _export_files(raster, records, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "center_tarp_roi_v1", "source": str(raster.path),
               "synthetic_demo": raster.ds.GetMetadataItem("SYNTHETIC_DEMO") == "YES",
               "source_size": raster.path.stat().st_size, "source_mtime_ns": raster.path.stat().st_mtime_ns,
               "width": raster.width, "height": raster.height,
               "coordinate_system": "source_pixel_edges_x_right_y_down; pixel center=(column+0.5,row+0.5)",
               "geotransform": raster.transform, "projection_wkt": raster.projection,
               "statistics": "unmodified stored raster values; scale/offset metadata NOT applied; no cross-file band alignment",
               "rois": records}
    vertices, stats = [], []
    for index, roi in enumerate(records, 1):
        for kind in ("outer", "inner"):
            for vertex, (x, y) in enumerate(roi[kind], 1):
                vertices.append([index, roi["name"], kind, vertex, x, y])
        window, inside = roi_window(raster, roi["inner"])
        for band in raster.bands:
            values, valid = raster.read(band, window)
            vals = values[valid & inside].astype(float)
            b = raster.ds.GetRasterBand(band)
            stats.append([index, roi["name"], band, b.GetDescription(), b.GetUnitType(), b.GetScale(), b.GetOffset(),
                          vals.size, *([float(vals.mean()), float(np.median(vals)), float(vals.std()), float(vals.min()), float(vals.max())]
                                      if vals.size else ["", "", "", "", ""])])
        dst = gdal.GetDriverByName("GTiff").Create(str(folder / f"roi_{index:03d}_inner_mask.tif"), window[2], window[3], 1, gdal.GDT_Byte, options=["COMPRESS=DEFLATE"])
        if raster.transform is not None and raster.projection:
            gt = raster.transform
            dst.SetGeoTransform((gt[0]+window[0]*gt[1]+window[1]*gt[2], gt[1], gt[2], gt[3]+window[0]*gt[4]+window[1]*gt[5], gt[4], gt[5]))
            dst.SetProjection(raster.projection)
        dst.SetMetadata({"SOURCE_IMAGE": str(raster.path), "SOURCE_XOFF": str(window[0]), "SOURCE_YOFF": str(window[1]), "MASK_MEANING": "1=inner polygon; source nodata validity is band-specific"})
        dst.GetRasterBand(1).WriteArray(inside.astype(np.uint8))
        dst = None
    (folder / "roi.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, header, rows in (("vertices.csv", ["roi_id", "name", "polygon", "vertex", "x", "y"], vertices),
                               ("pixel_statistics.csv", ["roi_id", "name", "raster_band", "band_description", "unit_metadata", "scale_metadata", "offset_metadata", "valid_pixels", "mean_stored", "median_stored", "std_stored", "min_stored", "max_stored"], stats)):
        with (folder / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
    filtered = []
    for roi in records:
        for row in statistics(raster, roi):
            filtered.append({"roi_name": roi["name"], "role": roi.get("role", "unassigned"),
                             "tarp_id": roi.get("tarp_id", ""), **row})
    if filtered:
        with (folder / "filtered_pixel_statistics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(filtered[0]))
            writer.writeheader()
            writer.writerows(filtered)
    return folder / "roi.json"
