"""Explicit flight context; no legacy-date defaults."""
import json
import os
from pathlib import Path

CONFIG = json.loads(Path(os.environ['UAV_TARP_CONTEXT']).read_text(encoding='utf-8'))
ROOT = Path(CONFIG['output'])


def index_files(root, recursive=True):
    result = {}
    if not root or not Path(root).is_dir():
        return result
    paths=Path(root).rglob('*') if recursive else Path(root).iterdir()
    for path in paths:
        if path.suffix.lower() not in ('.tif', '.tiff') or not path.is_file():
            continue
        key = path.stem
        if key in result:
            raise ValueError(f'Duplicate image identifier: {key} in {root}')
        if path.stat().st_size:
            result[key] = path
    return result
