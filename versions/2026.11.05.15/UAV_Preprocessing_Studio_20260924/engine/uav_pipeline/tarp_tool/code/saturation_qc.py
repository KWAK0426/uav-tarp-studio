"""Raw-DN saturation checks; full-frame results are only a candidate screen."""
from pathlib import Path
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

import cv2
import numpy as np
from osgeo import gdal

gdal.UseExceptions()


SATURATION_DN = 65520
QC_VERSION = 2


def _valid(array, nodata):
    # Native UInt16 maximum is saturation unless the TIFF explicitly declares NoData.
    valid = np.isfinite(array)
    if nodata is not None:
        valid &= array != nodata
    return valid


def scan_frame_cached(paths, cache_dir):
    """Reuse exact scans only while every source fingerprint and algorithm matches."""
    try:
        fingerprint = [(str(Path(p).resolve()),Path(p).stat().st_size,Path(p).stat().st_mtime_ns) for p in paths]
    except (OSError,TypeError):
        return scan_frame(paths)
    signature = hashlib.sha256(json.dumps([QC_VERSION,fingerprint]).encode('utf8')).hexdigest()
    cache = Path(cache_dir)/f'{signature}.json'
    if cache.exists():
        try:
            record=json.loads(cache.read_text(encoding='utf8'))
            if record.get('signature')==signature:
                return record['result']
        except (OSError,ValueError,KeyError):
            pass
    result=scan_frame(paths)
    if result['status']!='unknown':
        after=[(str(Path(p).resolve()),Path(p).stat().st_size,Path(p).stat().st_mtime_ns) for p in paths]
        if after!=fingerprint:
            return dict(status='unknown',reason='검사 중 원영상 변경',bands=[])
        cache.parent.mkdir(parents=True,exist_ok=True)
        temp=cache.with_suffix('.tmp')
        temp.write_text(json.dumps(dict(signature=signature,result=result),ensure_ascii=False),encoding='utf8')
        os.replace(temp,cache)
    return result


def wavelength(path):
    import tifffile
    try:
        with tifffile.TiffFile(path) as tf:
            tag=tf.pages[0].tags.get('XMP')
            if tag:
                return float(next(e.text for e in ET.fromstring(tag.value).iter() if e.tag.endswith('}CentralWavelength')))
    except (OSError,ValueError,StopIteration,ET.ParseError):
        pass
    return None


def inspect_window(path, box=None):
    """Full-resolution native pixels; manual rectangle is diagnostic, not ELC approval."""
    from PIL import Image
    ds=gdal.Open(str(path),gdal.GA_ReadOnly)
    if ds.RasterCount!=1 or ds.GetRasterBand(1).DataType!=gdal.GDT_UInt16:
        raise ValueError('native UInt16 단밴드 원영상이 필요합니다.')
    width,height=ds.RasterXSize,ds.RasterYSize
    window=(0,0,width,height)
    if box is not None:
        coords=np.asarray(box,dtype=float)
        if coords.shape!=(4,) or not np.isfinite(coords).all() or (coords<0).any() or (coords>1).any():
            raise ValueError('검사 상자 좌표를 확인하세요.')
        x0,y0,x1,y1=coords
        if x1<=x0 or y1<=y0:raise ValueError('검사 상자는 양의 면적이어야 합니다.')
        left,top=int(np.floor(x0*width)),int(np.floor(y0*height))
        right,bottom=min(width,int(np.ceil(x1*width))),min(height,int(np.ceil(y1*height)))
        window=(left,top,right-left,bottom-top)
    band=ds.GetRasterBand(1);array=band.ReadAsArray(*window)
    valid=_valid(array,band.GetNoDataValue());values=array[valid]
    if not values.size:raise ValueError('검사 영역에 유효 원영상 화소가 없습니다.')
    clipped=valid & (array>=SATURATION_DN)
    lo,hi=np.percentile(values,[2,98])
    gray=(np.clip((array.astype(float)-lo)/max(1,hi-lo),0,1)*255).astype('uint8')
    rgb=np.repeat(gray[:,:,None],3,axis=2);rgb[~valid]=0;rgb[clipped]=[255,45,65]
    image=Image.fromarray(rgb);image.thumbnail((1200,900))
    stats=dict(valid_pixels=int(values.size),saturated_pixels=int(clipped.sum()),
               saturated_pct=100*int(clipped.sum())/int(values.size),max_dn=int(values.max()),
               threshold_dn=SATURATION_DN,wavelength_nm=wavelength(path),native_size=[width,height],
               window=list(window),scope='manual_rectangle_diagnostic' if box else 'full_native_frame',
               roi_approved=False)
    return image,stats


def save_check(config, candidate, band, path, box):
    if box is None:raise ValueError('타프 검사 상자를 먼저 지정하세요.')
    date=config['date'][:10].replace('_','-');flight=config['flight']
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date) or not re.fullmatch(r'[1-6](?:st|nd|rd|th)',flight):
        raise ValueError('날짜·회차를 확인하세요.')
    path=Path(path);before=path.stat()
    _,stats=inspect_window(path,box)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValueError('검사 중 원영상이 변경되었습니다.')
    record=dict(date=date,flight=flight,candidate=candidate,native_suffix=band,source=str(path),
                signature=[after.st_size,after.st_mtime_ns],box=box,stats=stats,
                saved_at=datetime.now(timezone.utc).isoformat(),role='manual_box_diagnostic_not_roi_approval')
    out=Path(config['location_library']).parent/'tarp_checks'/(date+'_'+flight)
    out.mkdir(parents=True,exist_ok=True)
    (out/(uuid.uuid4().hex+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
    return dict(saved=True,stats=stats)


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
            if ds.RasterCount!=1 or ds.GetRasterBand(1).DataType!=gdal.GDT_UInt16:
                raise ValueError('native UInt16 단밴드 원영상이 아닙니다')
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
            bands.append({"band": number, "native_suffix": number, "wavelength_nm": wavelength(path), "valid_pixels": count, "saturated_pixels": clipped,
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
