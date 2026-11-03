"""Read-only capability probe; stdout is one JSON object, never credentials."""
import importlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

MODULES = {
    'tkinter': 'tk', 'numpy': 'numpy', 'scipy': 'scipy', 'cv2': 'opencv',
    'PIL': 'pillow', 'pandas': 'pandas', 'matplotlib': 'matplotlib',
    'osgeo.gdal': 'gdal', 'pyproj': 'pyproj', 'shapely': 'shapely',
    'tifffile': 'tifffile', 'pysolar': 'pysolar', 'skimage': 'scikit-image',
    'imageio': 'imageio', 'psutil': 'psutil', 'packaging': 'packaging',
    'pytz': 'pytz', 'requests': 'requests', 'win32cred': 'pywin32',
    'exiftool': 'PyExifTool',
}


def probe():
    prefix = Path(sys.prefix)
    extra = os.environ.get('UAV_STUDIO_TOOLS', '')
    os.environ['PATH'] = os.pathsep.join(filter(None, [extra, str(prefix/'bin'), str(prefix/'Library/bin'),
        str(prefix/'Scripts'), str(prefix), os.environ.get('PATH', '')]))
    handles = []
    if hasattr(os, 'add_dll_directory'):
        for path in (prefix/'Library/bin', prefix):
            if path.is_dir(): handles.append(os.add_dll_directory(str(path)))
    errors, versions, missing = [], {}, []
    if not (3, 10) <= sys.version_info[:2] <= (3, 12) or struct.calcsize('P') != 8:
        errors.append('Python 3.10–3.12 x64 required')
    for name, package in MODULES.items():
        try:
            module = importlib.import_module(name)
            versions[package] = str(getattr(module, '__version__', 'available'))
        except Exception as exc:
            missing.append(package)
            errors.append(name + ': ' + type(exc).__name__ + ': ' + str(exc)[:200])
    if not missing:
        try:
            import numpy as np
            import cv2
            from osgeo import gdal
            from pyproj import Transformer
            assert int(np.__version__.split('.')[0]) == 1, 'NumPy 1.x required by this release'
            cv2.SIFT_create()
            gdal.UseExceptions()
            ds = gdal.GetDriverByName('MEM').Create('', 3, 2, 1, gdal.GDT_Float32)
            ds.GetRasterBand(1).WriteArray(np.ones((2, 3), dtype=np.float32))
            assert float(ds.ReadAsArray().sum()) == 6
            x, y = Transformer.from_crs(4326, 32652, always_xy=True).transform(129, 35)
            assert abs(x - 500000) < 1 and 3800000 < y < 4000000
        except Exception as exc:
            errors.append('native capabilities: ' + str(exc))
    exif = shutil.which('exiftool')
    exif_version = None
    exif_error = None
    if exif:
        try:
            result = subprocess.run([exif, '-ver'], capture_output=True, text=True,
                timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            exif_version = result.stdout.strip()
            parts = tuple(int(p) for p in exif_version.split('.')[:2])
            assert result.returncode == 0 and parts >= (13, 7), 'ExifTool >=13.07 required'
            code = 'import exiftool\nwith exiftool.ExifToolHelper() as e: print(e.version)'
            result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True,
                text=True, timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            assert result.returncode == 0, 'ExifTool stay-open wrapper failed'
        except Exception as exc:
            exif_error = type(exc).__name__ + ': ' + str(exc)
    else:
        exif_error = 'ExifTool executable missing'
    return dict(python=sys.executable, version=sys.version.split()[0], prefix=sys.prefix,
                python_ok=not errors, ok=not errors and not exif_error,
                errors=errors, missing=missing, versions=versions,
                exiftool=exif, exiftool_version=exif_version, exiftool_error=exif_error)


if __name__ == '__main__':
    print(json.dumps(probe(), ensure_ascii=True))
