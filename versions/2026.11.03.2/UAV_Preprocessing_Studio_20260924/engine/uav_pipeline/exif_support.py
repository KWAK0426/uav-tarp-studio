"""Fail before raster writes when an older Windows ExifTool cannot preserve metadata."""
from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import subprocess


def executable():
    path = shutil.which("exiftool.exe") or shutil.which("exiftool")
    if not path:
        raise FileNotFoundError("ExifTool 실행 파일이 없습니다. 앱 환경 검사를 실행하세요.")
    return str(Path(path).resolve())


@lru_cache(maxsize=1)
def version():
    text = subprocess.run([executable(), "-ver"], check=True, capture_output=True,
                          text=True, timeout=15).stdout.strip()
    match = re.match(r"(\d+)\.(\d+)", text)
    if not match:
        raise ValueError("ExifTool 버전을 확인할 수 없습니다.")
    return int(match[1]), int(match[2])


def require_paths_supported(paths):
    if os.name != "nt":
        return
    long_paths = [str(Path(path).resolve()) for path in paths if len(str(Path(path).resolve())) >= 240]
    if long_paths and version() < (13, 7):
        raise ValueError("긴 Windows 경로에는 ExifTool 13.07 이상이 필요합니다. 전용 환경을 설치하거나 결과 루트를 짧게 지정하세요. 예: D:\\ELC_Result. " + long_paths[0])
