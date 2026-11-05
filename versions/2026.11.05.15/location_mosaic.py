"""Location-only RGB mosaic, adapted from code/build_multispectral_rgb_mosaic.py.

Retains its center-weighted display blending; never used as calibrated imagery.
"""
from datetime import datetime
import json
import math
from pathlib import Path
import re
import shutil
from uuid import uuid4

import numpy as np
from osgeo import gdal, osr


def build(photos, output, shots, progress=lambda text:None, limit=4096, cancelled=lambda:False):
    photos = Path(photos).resolve()
    if not photos.is_dir():
        raise ValueError('현재 회차의 Photos 폴더를 먼저 선택하세요.')
    files = []
    for path in sorted(photos.iterdir()):
        match = re.fullmatch(r'(IMG_\d{4})_(?:10|[1-9])', path.stem, re.IGNORECASE)
        if path.is_file() and path.suffix.lower() in ('.tif', '.tiff') and match and match[1].upper() in shots:
            files.append(path)
    if not files:
        raise ValueError('등록한 촬영번호와 일치하는 Photos가 없습니다. 현재 회차의 정사보정된 Photos가 직접 들어 있는 폴더를 선택하세요.')
    bounds, records, projection, pixel = [], [], None, None
    for number, path in enumerate(files, 1):
        if cancelled(): raise ValueError('모자이크 생성을 중지했습니다. 원본은 변경하지 않았습니다.')
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        if ds is None: raise ValueError('읽을 수 없는 Photos: '+path.name)
        gt, crs = ds.GetGeoTransform(), ds.GetProjection()
        if not crs or gt[2] or gt[4] or gt[1] <= 0 or gt[5] >= 0:
            raise ValueError('좌표가 있는 북쪽 정렬 Photos가 필요합니다: '+path.name+'\n원영상은 Metashape에서 정사보정·Photos 내보내기를 먼저 해주세요.')
        alpha = [i for i in range(1,ds.RasterCount+1) if ds.GetRasterBand(i).GetColorInterpretation()==gdal.GCI_AlphaBand]
        if ds.RasterCount-len(alpha) != 10 or len(alpha)>1:
            raise ValueError('MX Dual 10밴드 정사 Photos가 필요합니다: '+path.name)
        if any(ds.GetRasterBand(i).DataType != gdal.GDT_UInt16 for i in (6,4,2)):
            raise ValueError('위치 확인용 원본 UInt16 Panel Photos를 선택하세요: '+path.name)
        if projection:
            a, b = osr.SpatialReference(), osr.SpatialReference()
            a.ImportFromWkt(projection); b.ImportFromWkt(crs)
            if not a.IsSame(b): raise ValueError('Photos 좌표계가 서로 다릅니다: '+path.name)
        else:
            projection, pixel = crs, (gt[1], -gt[5])
        bounds.append((gt[0],gt[3]+gt[5]*ds.RasterYSize,gt[0]+gt[1]*ds.RasterXSize,gt[3]))
        stat = path.stat()
        records.append(dict(path=str(path),size=stat.st_size,mtime_ns=stat.st_mtime_ns,alpha=alpha[0] if alpha else None))
        ds = None
        if number % 50 == 0: progress(f'Photos 좌표 확인 {number}/{len(files)}')
    left, bottom = min(b[0] for b in bounds), min(b[1] for b in bounds)
    right, top = max(b[2] for b in bounds), max(b[3] for b in bounds)
    factor = max(1, (right-left)/pixel[0]/limit, (top-bottom)/pixel[1]/limit)
    width, height = max(1,math.ceil((right-left)/pixel[0]/factor)), max(1,math.ceil((top-bottom)/pixel[1]/factor))
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output).free < width*height*8 + 128*1024**2:
        raise ValueError('위치 확인용 모자이크를 저장할 여유 공간이 부족합니다.')
    value_sum = np.zeros((3,height,width),dtype=np.float32)
    weight_sum = np.zeros((height,width),dtype=np.float32)
    for number,(path,box,record) in enumerate(zip(files,bounds,records),1):
        if cancelled(): raise ValueError('모자이크 생성을 중지했습니다. 원본은 변경하지 않았습니다.')
        x0 = max(0,int(np.floor((box[0]-left)/(right-left)*width)))
        x1 = min(width,int(np.ceil((box[2]-left)/(right-left)*width)))
        y0 = max(0,int(np.floor((top-box[3])/(top-bottom)*height)))
        y1 = min(height,int(np.ceil((top-box[1])/(top-bottom)*height)))
        ds = gdal.Open(str(path),gdal.GA_ReadOnly)
        bands = [6,4,2]+([record['alpha']] if record['alpha'] else [])
        data = ds.ReadAsArray(buf_xsize=x1-x0,buf_ysize=y1-y0,band_list=bands,
                              buf_type=gdal.GDT_UInt16,resample_alg=gdal.GRIORA_NearestNeighbour)
        if data is None: raise ValueError('Photos 화소 읽기 실패: '+path.name)
        valid = np.all(data[:3]!=65535,axis=0)
        for index, band_number in enumerate((6,4,2)):
            nodata = ds.GetRasterBand(band_number).GetNoDataValue()
            if nodata is not None: valid &= data[index]!=nodata
        alpha = data[3].astype(np.float32)/65535 if record['alpha'] else np.ones(valid.shape,dtype=np.float32)
        valid &= alpha>0
        yy,xx = np.mgrid[0:y1-y0,0:x1-x0]
        score = ((xx+.5)/(x1-x0)-.5)**2+((yy+.5)/(y1-y0)-.5)**2
        weight = np.exp(-score/.125).astype(np.float32)*alpha
        weight[~valid] = 0
        value_sum[:,y0:y1,x0:x1] += data[:3].astype(np.float32)*weight[None]
        weight_sum[y0:y1,x0:x1] += weight
        ds = None
        stat = path.stat()
        if (stat.st_size,stat.st_mtime_ns)!=(record['size'],record['mtime_ns']):
            raise ValueError('생성 도중 Photos가 변경되었습니다. 다시 실행하세요.')
        if number % 25 == 0 or number==len(files): progress(f'위치 확인용 모자이크 생성 {number}/{len(files)}')
    valid = weight_sum>0
    if not valid.any(): raise ValueError('Photos에 표시할 유효 화소가 없습니다.')
    run = output/('Location_Mosaic_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:6])
    run.mkdir(exist_ok=False)
    path = run/'tarp_location_RGB.tif'
    out = gdal.GetDriverByName('GTiff').Create(str(path),width,height,4,gdal.GDT_UInt16,
                                            options=['COMPRESS=DEFLATE','TILED=YES','PREDICTOR=2'])
    out.SetGeoTransform((left,(right-left)/width,0,top,0,-(top-bottom)/height)); out.SetProjection(projection)
    for i in range(3):
        values = np.full(valid.shape,65535,dtype=np.uint16)
        values[valid] = np.clip(np.rint(value_sum[i][valid]/weight_sum[valid]),0,65534).astype(np.uint16)
        band = out.GetRasterBand(i+1); band.WriteArray(values); band.SetNoDataValue(65535)
        band.SetColorInterpretation((gdal.GCI_RedBand,gdal.GCI_GreenBand,gdal.GCI_BlueBand)[i])
    out.GetRasterBand(4).WriteArray(np.where(valid,65535,0).astype(np.uint16))
    out.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
    out.SetMetadataItem('PURPOSE','TARP_LOCATION_DISPLAY_ONLY_NOT_REFLECTANCE')
    out.SetMetadataItem('METHOD','Reduced RGB 6/4/2 center-weighted display blend')
    out.FlushCache(); out = None
    record = dict(mosaic=str(path),photos=str(photos),source_count=len(files),width=width,height=height,
                  purpose='location_display_only',rgb_bands=[6,4,2],sources=records,
                  original_builder='code/build_multispectral_rgb_mosaic.py',registered_shots=sorted(shots))
    (run/'manifest.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    return record
