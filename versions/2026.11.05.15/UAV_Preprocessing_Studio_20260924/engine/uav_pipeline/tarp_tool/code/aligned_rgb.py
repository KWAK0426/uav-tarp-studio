from pathlib import Path
import json
import xml.etree.ElementTree as ET
import cv2
import numpy as np
import tifffile
from osgeo import gdal

from context import ROOT, CONFIG, index_files
RAD = Path(CONFIG.get('image_source', CONFIG['radiance']))
RAW = Path(CONFIG['raw'])
CHANNELS = (6, 4, 2, 10, 3)
WAVELENGTHS = (668, 560, 475, 842, 531)

def gray(a):
    ok=np.isfinite(a)&(a!=65535)
    if not ok.any():return np.zeros(a.shape,dtype='uint8')
    lo,hi=np.percentile(a[ok],[1,99])
    result=(np.nan_to_num(np.clip((a-lo)/max(hi-lo,1e-9),0,1))*255).astype('uint8')
    result[~ok]=0
    return result

def get_stack(shot):
    folder=ROOT/'cache/aligned_rgb';folder.mkdir(exist_ok=True)
    dest=folder/(shot+'.b234610_v3.npz')
    if dest.exists():
        with np.load(dest) as z:return z['stack'].copy()
    files={k+'.tif':v for k,v in index_files(RAD).items() if k.startswith(shot+'_')}
    raw={k+'.tif':v for k,v in index_files(RAW).items() if k.startswith(shot+'_')}
    waves={}
    for b in range(1,11):
        with tifffile.TiffFile(raw[f'{shot}_{b}.tif']) as t:
            meta={e.tag.split('}')[-1]:e.text for e in ET.fromstring(t.pages[0].tags['XMP'].value).iter()}
            waves[b]=float(meta['CentralWavelength'])
    selected=[min(waves,key=lambda b:abs(waves[b]-wavelength)) for wavelength in WAVELENGTHS]
    ref=gdal.Open(str(files[f'{shot}_6.tif'])).ReadAsArray().astype('float32')
    sift=cv2.SIFT_create(nfeatures=12000)
    kr,dr=sift.detectAndCompute(gray(ref),None)
    stack=[];audit=[];source_arrays={};transforms={}
    for channel,wavelength,b in zip(CHANNELS,WAVELENGTHS,selected):
        a=gdal.Open(str(files[f'{shot}_{b}.tif'])).ReadAsArray().astype('float32')
        if b==6:
            stack.append(a)
            source_arrays[channel]=a;transforms[channel]=np.eye(3)
            audit.append(dict(analysis_band=channel,source_suffix=b,wavelength=waves[b],method='native',inliers=None,median_error_px=0,transform=np.eye(3).tolist()))
            continue
        if channel==10:
            red=gray(source_arrays[6]).astype('float32');nir=gray(a).astype('float32')
            red=cv2.magnitude(cv2.Sobel(red,cv2.CV_32F,1,0),cv2.Sobel(red,cv2.CV_32F,0,1))
            nir=cv2.magnitude(cv2.Sobel(nir,cv2.CV_32F,1,0),cv2.Sobel(nir,cv2.CV_32F,0,1))
            (dx,dy),response=cv2.phaseCorrelate(red,nir)
            if response<.1 or max(abs(dx),abs(dy))>100:raise ValueError(f'{shot} Band10 (842nm): phase registration failed')
            H=transforms[6]@np.array([[1,0,-dx],[0,1,-dy],[0,0,1]],dtype=float)
            inliers=None;median=None;method='phase_via_Band6';extra={'phase_response':float(response),'phase_shift_px':[float(dx),float(dy)]}
        else:
            km,dm=sift.detectAndCompute(gray(a),None)
            pairs=cv2.BFMatcher().knnMatch(dm,dr,k=2)
            good=[p[0] for p in pairs if len(p)==2 and p[0].distance<.72*p[1].distance]
            if len(good)<20:raise ValueError(f'{shot} Band{channel} ({wavelength}nm): insufficient alignment matches')
            src=np.float32([km[m.queryIdx].pt for m in good]);dst=np.float32([kr[m.trainIdx].pt for m in good])
            H,mask=cv2.findHomography(src,dst,cv2.RANSAC,2.5)
            if H is None or mask.sum()<20:raise ValueError(f'{shot} Band{channel} ({wavelength}nm): registration failed')
            errors=np.linalg.norm(cv2.perspectiveTransform(src[:,None,:],H)[:,0]-dst,axis=1)
            median=float(np.median(errors[mask.ravel()>0]))
            if median>1.5:raise ValueError(f'{shot} Band{channel} ({wavelength}nm): registration residual too large')
            inliers=int(mask.sum());method='SIFT_RANSAC';extra={}
        warped=cv2.warpPerspective(a,H,(ref.shape[1],ref.shape[0]),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT,borderValue=float('nan'))
        stack.append(warped)
        source_arrays[channel]=a;transforms[channel]=H
        audit.append(dict(analysis_band=channel,source_suffix=b,wavelength=waves[b],method=method,inliers=inliers,median_error_px=median,transform=H.tolist(),**extra))
    stack=np.stack(stack)
    np.savez_compressed(dest,stack=stack)
    (folder/(shot+'.b234610_v3.json')).write_text(json.dumps(dict(reference='file suffix6 native pixels',channels=list(CHANNELS),source_kind=CONFIG['source_kind'],image_source=str(RAD),image_size=[ref.shape[1],ref.shape[0]],purpose='ROI proposal and tracking only; local parallax still requires review',bands=audit),indent=2),encoding='utf-8')
    return stack

def preview(stack, band='rgb'):
    if band == 'rgb':
        rgb=np.stack([gray(a) for a in stack[:3]],axis=2)
        valid=np.isfinite(stack[:3]).all(0)&(stack[:3]!=65535).all(0)
    else:
        source=stack[CHANNELS.index(int(band))]
        channel=gray(source)
        rgb=np.repeat(channel[:,:,None],3,axis=2)
        valid=np.isfinite(source)&(source!=65535)
    rgb=(255*(rgb.astype(float)/255)**.65).astype('uint8')
    rgb[~valid]=0
    return rgb
