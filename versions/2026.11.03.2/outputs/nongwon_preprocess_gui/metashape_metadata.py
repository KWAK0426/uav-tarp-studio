from __future__ import annotations
from uav_pipeline.exif_support import executable as exif_executable

import subprocess
from pathlib import Path


METASHAPE_METADATA_COPY_ARGS = [
    "-XMP",
    "-EXIF:all",
    "-GPS:all",
    "-IFD0:Make",
    "-IFD0:Model",
    "-IFD0:DateTime",
    "-IFD0:Software",
]


def _run_exiftool(args: list[str], message: str) -> subprocess.CompletedProcess:
    # UTF-8 argument input avoids Windows command-line recoding of Korean paths.
    result = subprocess.run([args[0], "-charset", "filename=UTF8", "-@", "-"],
                            input="\n".join(args[1:]) + "\n", capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{message}: {detail}")
    return result


def copy_metashape_metadata(source_path: Path, output_path: Path) -> None:
    """Copy non-structural MicaSense metadata needed by Metashape.

    Do not copy full IFD0/TIFF tags because the processed outputs are Float32
    TIFFs. Copying source BitsPerSample/StripOffsets/ImageWidth-type tags from
    raw UInt16 files can corrupt or misdescribe the processed raster.

    MicaSense/Pix4D camera metadata uses custom XMP namespaces. ExifTool can
    read those tags but does not write them tag-by-tag without a custom config,
    so "-XMP" is used to copy the original XMP packet as a block.
    """

    source_path = Path(source_path)
    output_path = Path(output_path)
    if not source_path.exists():
        raise RuntimeError(f"Metadata source does not exist: {source_path}")
    if not output_path.exists():
        raise RuntimeError(f"Metadata output does not exist: {output_path}")

    _run_exiftool(
        [
            exif_executable(),
            "-overwrite_original",
            "-P",
            "-m",
            "-TagsFromFile",
            str(source_path),
            *METASHAPE_METADATA_COPY_ARGS,
            str(output_path),
        ],
        "Failed to copy Metashape/MicaSense metadata",
    )
