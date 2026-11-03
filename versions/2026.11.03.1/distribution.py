"""Local installation checks and empty-project bootstrap; no campaign processing."""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from datetime import date
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
STUDIO = ROOT / "UAV_Preprocessing_Studio_20260924"
STATE = Path(os.environ.get("UAV_STUDIO_DATA", str(ROOT / ".local")))
FLIGHTS = ("1st", "2nd", "3rd", "4th", "5th", "6th")
WAVES = (444, 475, 531, 560, 650, 668, 705, 717, 740, 842)


def configure():
    for folder in (STATE, STUDIO / "projects", STUDIO / "verification"):
        folder.mkdir(parents=True, exist_ok=True)
    paths = [ROOT / "vendor", STUDIO / "engine", STUDIO / "code", ROOT / "tarp_center_roi_tool"]
    sys.path[:0] = [str(p) for p in paths]
    os.environ["PYTHONPATH"] = os.pathsep.join(map(str, paths))
    os.environ["ELC_STUDIO_STATE"] = str(STATE / "assistant")
    os.environ["NONGWON_RUNTIME_DIR"] = str(STATE / "native-runtime")
    prefix = Path(sys.prefix)
    binaries = [prefix / "bin", prefix / "Library/bin", prefix / "Scripts", prefix]
    tools = os.environ.get("UAV_STUDIO_TOOLS", "")
    os.environ["PATH"] = os.pathsep.join(filter(None, [tools, *map(str, binaries), os.environ.get("PATH", "")]))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def remember(project):
    STATE.mkdir(exist_ok=True)
    temp = STATE / "last_project.tmp"
    temp.write_text(json.dumps({"project": str(Path(project).resolve())}), encoding="utf-8")
    os.replace(temp, STATE / "last_project.json")


def preparation_template():
    configure()
    from uav_pipeline.config import DEFAULTS
    from uav_pipeline.preparation import CHOICES
    cfg = deepcopy(DEFAULTS)
    cfg["preparation"] = deepcopy(CHOICES)
    cfg["dataset"] = {"root": "", "date": "", "flight": "", "basis": "Panel",
                      "paths": {key: "" for key in ("raw", "ed_source", "photos", "tarp_photos", "mosaic",
                      "dem", "gps", "radiance", "roi", "time_ed", "ed_radiance", "coefficients",
                      "reflectance", "output", "mask")}, "warnings": [], "input_settings_reviewed": False}
    # No empirical calibration, input path, ROI approval or coefficient is inherited.
    cfg["preparation"].update(elc_only=True, worker_count=1, integrated_save_radiance=False,
                              raw_directory="", gps_path="", coefficient_basis_confirmed=False)
    return cfg


def project_path(work_root, observed_date, flight, site):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed_date):
        raise ValueError("관측 날짜를 YYYY-MM-DD 형식으로 입력하세요. 예: 2026-06-10")
    try:
        date.fromisoformat(observed_date)
    except ValueError:
        raise ValueError("존재하지 않는 관측 날짜입니다. 촬영한 날짜를 다시 확인하세요.") from None
    for label, value in (("현장 이름", site), ("촬영 회차", flight)):
        reserved = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
        if not re.fullmatch(r"[\w-]{1,50}", value) or value.upper() in reserved:
            raise ValueError(f"{label}: 한글·영문·숫자·_·-로 1~50자를 입력하세요. 공백·경로 기호·Windows 예약 이름은 사용할 수 없습니다.")
    if not str(work_root).strip():
        raise ValueError("프로젝트·결과를 저장할 폴더를 선택하세요. 원영상 폴더와는 별개입니다.")
    work = Path(work_root).expanduser()
    if not work.is_absolute():
        raise ValueError("저장 폴더는 전체 경로로 지정하세요. 오른쪽 폴더 선택 버튼을 사용할 수 있습니다.")
    if work.exists() and not work.is_dir():
        raise ValueError("저장 위치에 파일이 지정되었습니다. 폴더를 선택하세요.")
    work = work.resolve()
    return work / site / observed_date / flight / "project.json"


def create_project(work_root, observed_date, flight, site="Site"):
    project = project_path(work_root, observed_date, flight, site)
    if project.exists():
        raise FileExistsError("같은 프로젝트가 이미 있습니다. '기존 프로젝트 열기'로 열어 주세요: " + str(project))
    observed = date.fromisoformat(observed_date)
    cycle = project.parent
    cfg = preparation_template()
    cfg["project_name"] = f"{observed_date} {flight} {site}"
    cfg["general"] = dict(enabled=True, project_root=str(cycle), date=observed_date, session=flight, site=site)
    cfg["dataset"].update(root=str(cycle), date=observed.strftime("%Y_%m_%d_") + site, flight=flight)
    cfg["preparation"]["shot_list_csv"] = ""
    cfg["geometry"]["write_per_frame_geotiff"] = False
    cfg["output"]["directory"] = str(cycle / "Analysis")
    cycle.mkdir(parents=True, exist_ok=True)
    write_json(project, cfg)
    remember(project)
    return project


class ProjectWizard:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        from legacy_studio import apply_studio_theme
        from uav_pipeline.ui_feedback import ActionHint

        self.root = root
        self.result = None
        apply_studio_theme(root)
        root.title("ELC Studio General | 프로젝트 시작")
        width, height = min(920, root.winfo_screenwidth() - 80), min(750, root.winfo_screenheight() - 100)
        root.geometry(f"{width}x{height}+30+30")
        root.minsize(min(760, width), min(630, height))
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        body = ttk.Frame(root, padding=24)
        body.grid(sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(14, weight=1)
        ttk.Label(body, text="ELC Studio General", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text="새 작업 설정", font=("Malgun Gothic", 13, "bold")).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(10, 4))
        ttk.Label(body, text="한 관측일의 한 회차를 하나의 프로젝트로 관리합니다.",
                  style="Subtitle.TLabel").grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 20))
        self.values = {"date": tk.StringVar(root), "site": tk.StringVar(root),
                       "flight": tk.StringVar(root, value="1st"), "work": tk.StringVar(root)}
        self.entries = {}
        rows = (
            ("date", "관측 날짜", "실제 촬영일을 입력하세요. 예: 2026-06-10 (YYYY-MM-DD)"),
            ("site", "현장 이름 (폴더명)", "예: 현장A, 현장B. 한글·영문·숫자·_·- 사용, 공백 제외"),
            ("flight", "촬영 회차", "같은 날 촬영한 묶음의 이름입니다. 예: 1st, 2nd, 오전"),
        )
        for index, (key, label, hint) in enumerate(rows):
            row = 3 + index * 2
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 20))
            entry = ttk.Entry(body, textvariable=self.values[key])
            entry.grid(row=row, column=1, columnspan=2, sticky="ew")
            self.entries[key] = entry
            ttk.Label(body, text=hint, style="Subtitle.TLabel").grid(
                row=row + 1, column=1, columnspan=2, sticky="w", pady=(4, 14))
        ttk.Label(body, text="프로젝트·결과 저장 폴더").grid(row=9, column=0, sticky="w", padx=(0, 20))
        self.entries["work"] = ttk.Entry(body, textvariable=self.values["work"])
        self.entries["work"].grid(row=9, column=1, sticky="ew")
        ttk.Button(body, text="폴더 선택...", command=self.browse).grid(row=9, column=2, padx=(8, 0))
        ttk.Label(body, text="원영상 위치가 아닙니다. 여유 공간이 있는 결과 저장 위치를 선택하세요.",
                  style="Subtitle.TLabel").grid(row=10, column=1, columnspan=2, sticky="w", pady=(4, 18))
        ttk.Separator(body).grid(row=11, column=0, columnspan=3, sticky="ew")
        self.preview = tk.StringVar(root)
        ttk.Label(body, text="생성될 작업 폴더", font=("Malgun Gothic", 10, "bold")).grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(14, 6))
        self.preview_label = ttk.Label(body, textvariable=self.preview, foreground="#75d4bc", anchor="nw")
        self.preview_label.grid(row=13, column=0, columnspan=3, sticky="ew")
        self.status = tk.StringVar(root)
        self.status_label = ttk.Label(body, textvariable=self.status, style="Subtitle.TLabel", anchor="nw")
        self.status_label.grid(row=15, column=0, columnspan=3, sticky="ew", pady=(8, 14))
        footer = ttk.Frame(body)
        footer.grid(row=16, column=0, columnspan=3, sticky="ew")
        footer.columnconfigure(1, weight=1)
        ttk.Button(footer, text="기존 프로젝트 열기...", command=self.open_existing).grid(row=0, column=0)
        ttk.Button(footer, text="취소", command=root.destroy).grid(row=0, column=2, padx=(0, 8))
        self.create_button = ttk.Button(footer, text="프로젝트 만들고 시작", style="Primary.TButton", command=self.create)
        self.create_button.grid(row=0, column=3)
        self.create_hint = ActionHint(self.create_button, "입력한 관측 정보로 빈 프로젝트를 만들고 자료 연결 화면을 엽니다. 영상 처리는 시작하지 않습니다.",
                                      lambda: self.status.get() if self.create_button.instate(("disabled",)) else "")
        for value in self.values.values():
            value.trace_add("write", self.refresh)
        body.bind("<Configure>", lambda event: self.resize(event.width))
        root.bind("<Return>", lambda event: self.create() if self.create_button.instate(("!disabled",)) else None)
        self.refresh()
        self.entries["date"].focus_set()

    def resize(self, width):
        for label in (self.preview_label, self.status_label):
            label.configure(wraplength=max(400, width - 48))

    def inputs(self):
        return {key: value.get().strip() for key, value in self.values.items()}

    def refresh(self, *_):
        values = self.inputs()
        folder = values["work"] or "[저장 폴더]"
        self.preview.set(str(Path(folder) / (values["site"] or "[현장]") /
                             (values["date"] or "[관측 날짜]") / (values["flight"] or "[회차]")))
        try:
            path = project_path(values["work"], values["date"], values["flight"], values["site"])
            if path.exists():
                raise ValueError("같은 프로젝트가 이미 있습니다. 왼쪽 '기존 프로젝트 열기'를 선택하세요.")
        except (ValueError, OSError) as exc:
            self.status.set(str(exc))
            self.create_button.state(("disabled",))
        else:
            self.status.set("다음 화면에서 원영상·타프 실측값·센서 교정값·Ed 자료를 연결합니다. 아직 영상 처리는 시작하지 않습니다.")
            self.create_button.state(("!disabled",))

    def browse(self):
        from tkinter import filedialog
        selected = filedialog.askdirectory(parent=self.root, title="프로젝트·결과를 저장할 폴더 선택")
        if selected:
            self.values["work"].set(selected)

    def create(self):
        values = self.inputs()
        try:
            project = create_project(values["work"], values["date"], values["flight"], values["site"])
        except (ValueError, OSError) as exc:
            self.status.set(str(exc))
            from tkinter import messagebox
            from uav_pipeline.ui_feedback import problem_text
            messagebox.showwarning("프로젝트 생성 실패", problem_text(exc), parent=self.root)
            return
        self.result = project
        self.root.destroy()

    def open_existing(self):
        from tkinter import filedialog
        selected = filedialog.askopenfilename(parent=self.root, title="기존 ELC 프로젝트 설정 열기 (project.json)",
                                              filetypes=(("ELC 프로젝트 설정", "*.json"),))
        if not selected:
            return
        try:
            path = Path(selected).resolve()
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(cfg, dict) or not isinstance(cfg.get("general"), dict) or not cfg["general"].get("enabled"):
                raise ValueError("이 배포본에서 만든 ELC 프로젝트의 project.json을 선택하세요.")
        except (ValueError, OSError) as exc:
            self.status.set(str(exc))
            from tkinter import messagebox
            from uav_pipeline.ui_feedback import problem_text
            messagebox.showwarning("프로젝트 열기 실패", problem_text(exc), parent=self.root)
            return
        self.result = path
        self.root.destroy()


def wizard():
    import tkinter as tk
    configure()
    root = tk.Tk()
    window = ProjectWizard(root)
    root.mainloop()
    return window.result


def verify_manifest():
    manifest = json.loads((ROOT / "PACKAGE_MANIFEST.json").read_text(encoding="utf-8"))
    errors = []
    for row in manifest["files"]:
        path = ROOT / row["path"]
        try:
            path.resolve().relative_to(ROOT)
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                errors.append(row["path"])
        except (OSError, ValueError):
            errors.append(row["path"])
    return errors


def bounded(command, timeout):
    import psutil
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out, error = process.communicate(timeout=timeout)
        if process.returncode:
            raise RuntimeError(error.strip() or f"Exit code {process.returncode}")
        return out
    except subprocess.TimeoutExpired:
        # Kill only this diagnostic's process tree, never another active ExifTool job.
        children = psutil.Process(process.pid).children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        process.kill()
        process.communicate()
        raise RuntimeError(f"Diagnostic timed out after {timeout}s: {command[0]}") from None


def doctor(skip_exif=False):
    configure()
    results = []
    def record(name, action):
        try:
            detail = action()
            results.append(dict(check=name, ok=True, detail=str(detail)))
        except Exception as exc:
            results.append(dict(check=name, ok=False, detail=f"{type(exc).__name__}: {exc}"))
    for name in ("tkinter", "PIL", "numpy", "scipy", "cv2", "osgeo.gdal", "pyproj", "shapely",
                 "pandas", "matplotlib", "tifffile", "pysolar", "skimage", "imageio", "psutil",
                 "win32cred", "exiftool", "micasense.capture"):
        record("import " + name, lambda n=name: getattr(importlib.import_module(n), "__version__", "OK"))
    def raster_check():
        import numpy as np
        from osgeo import gdal
        from pyproj import Transformer
        gdal.UseExceptions()
        STATE.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=STATE) as folder:
            path = Path(folder) / "test.tif"
            ds = gdal.GetDriverByName("GTiff").Create(str(path), 3, 2, 1, gdal.GDT_Float32, ["COMPRESS=DEFLATE"])
            ds.GetRasterBand(1).SetNoDataValue(65535)
            ds.GetRasterBand(1).WriteArray(np.ones((2, 3), np.float32))
            ds = None
            ds = gdal.Open(str(path))
            assert ds.GetRasterBand(1).GetNoDataValue() == 65535
            assert float(ds.ReadAsArray().sum()) == 6
            ds = None
        x, y = Transformer.from_crs(4326, 32652, always_xy=True).transform(129, 35)
        assert abs(x - 500000) < 1 and 3800000 < y < 4000000
        return "Float32/DEFLATE/NoData roundtrip and PROJ OK"
    record("GDAL + PROJ", raster_check)
    from uav_pipeline.config import DEFAULTS
    for key, path in DEFAULTS["preprocess"]["tools"].items():
        def check(p=path):
            Path(p).resolve().relative_to(ROOT)
            assert Path(p).is_file(), p
            import importlib.util
            sys.path.insert(0, str(Path(p).parent))
            spec = importlib.util.spec_from_file_location("distribution_" + Path(p).stem, p)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return str(Path(p).relative_to(ROOT))
        record("local adapter " + key, check)
    if not skip_exif:
        def exif():
            executable = shutil.which("exiftool")
            if not executable:
                raise RuntimeError("ExifTool missing; rerun 00_INSTALL.bat.")
            version = bounded([executable, "-ver"], 15)
            if not version.strip():
                raise RuntimeError("ExifTool returned no version.")
            # Also exercise the stay-open wrapper used by the MicaSense SDK.
            code = "import exiftool;\nwith exiftool.ExifToolHelper() as e: print(e.version)"
            bounded([sys.executable, "-c", code], 20)
            return executable + " | " + version.strip()
        record("ExifTool executable + SDK wrapper", exif)
    if (ROOT / "PACKAGE_MANIFEST.json").exists():
        def hashes():
            errors = verify_manifest()
            if errors:
                raise ValueError("Changed/missing package files: " + ", ".join(errors[:8]))
            return "All shipped hashes match"
        record("package integrity", hashes)
    result = dict(ok=all(r["ok"] for r in results), python=sys.executable,
                  exiftool_skipped=skip_exif, free_gib=round(shutil.disk_usage(ROOT).free / 2**30, 2),
                  scientific_validation=False, checks=results)
    STATE.mkdir(exist_ok=True)
    (STATE / "doctor.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result["ok"]


def start(project=None, smoke=False, screenshot=None, stage=None):
    configure()
    if not project:
        try:
            candidate = Path(json.loads((STATE / "last_project.json").read_text())["project"])
            if candidate.is_file():
                project = candidate
        except (OSError, ValueError, KeyError):
            pass
    if not project:
        project = wizard()
    if not project:
        return 0
    project = Path(project).resolve()
    cfg = json.loads(project.read_text(encoding="utf-8-sig"))
    if not cfg.get("general", {}).get("enabled"):
        raise ValueError("범용 ELC 프로젝트를 선택하세요. 기존 배포본의 프로젝트는 원래 실행기로 열어 주세요.")
    prep = cfg
    os.environ["ELC_STUDIO_WORK"] = prep["dataset"]["root"]
    if not smoke:
        remember(project)
    command = [sys.executable, str(STUDIO / "code/general_studio.py"), "--config", str(project)]
    if smoke:
        command.append("--smoke")
    if screenshot:
        command += ["--screenshot", str(screenshot)]
    if stage is not None:
        command += ["--stage", str(stage)]
    return subprocess.call(command, cwd=STUDIO)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("check", "new", "start", "verify"))
    p.add_argument("--project", type=Path)
    p.add_argument("--date")
    p.add_argument("--session", "--flight", dest="flight", default="Session01")
    p.add_argument("--site", default="Site")
    p.add_argument("--work-root", type=Path)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--screenshot", type=Path)
    p.add_argument("--stage", type=int)
    p.add_argument("--skip-exif", action="store_true", help="Development diagnostics only; NOT a full installation check")
    a = p.parse_args()
    if a.action == "check":
        return 0 if doctor(a.skip_exif) else 1
    if a.action == "verify":
        errors = verify_manifest()
        print(json.dumps({"changed_or_missing": errors}))
        return int(bool(errors))
    if a.action == "new":
        if a.date and a.work_root:
            print(create_project(a.work_root, a.date, a.flight, a.site))
        else:
            project = wizard()
            if project:
                print(project)
        return 0
    return start(a.project, a.smoke, a.screenshot, a.stage)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        STATE.mkdir(exist_ok=True)
        (STATE / "startup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        if sys.stdout is not None:
            traceback.print_exc()
        else:
            import tkinter.messagebox
            tkinter.messagebox.showerror("ELC Studio", "시작 실패: 배포 폴더 .local/startup_error.log를 확인하세요.")
        raise SystemExit(1)
