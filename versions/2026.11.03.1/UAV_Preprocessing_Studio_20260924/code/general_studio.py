"""General-project front end; all scientific processing stays in the shared engine."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ENGINE = Path(__file__).resolve().parents[1] / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from legacy_studio import PipelineGUI
from uav_pipeline.config import load_config, save_config
from uav_pipeline.preparation import SENSOR, generate, initialize, latest_candidate_selection
from uav_pipeline.ui_feedback import ActionHint, primary_error, problem_text

RAW_NAME = re.compile(r"^(IMG_\d{4})_(10|[1-9])$", re.IGNORECASE)
SOURCE_KEYS = ("preparation.raw_directory", "preparation.shot_list_csv")
BASIS_KEYS = SOURCE_KEYS + (
    "preparation.tarp_reflectance_csv", "preparation.ed_csv", "preparation.gain_csv",
    "preparation.source_timezone", "preparation.time_band", "preparation.reference_shot",
)


def general_api():
    from uav_pipeline import general_project
    return general_project


def destroy_owned_root(root):
    """Destroy only this Tk interpreter, including its owned Toplevels/timers."""
    if getattr(root, "_general_destroying", False):
        return
    root._general_destroying = True
    try:
        if not root.winfo_exists():
            return
        root.update_idletasks()
        for timer in root.tk.call("after", "info"):
            # Child widgets own callback commands. Each owner's destroy() must
            # delete its own command exactly once.
            root.tk.call("after", "cancel", timer)
        root.destroy()
    except tk.TclError:
        if root.tk.call("info", "commands", "."):
            raise


def inspect_raw(raw):
    """Inventory names, not pixels; retain every ambiguous/incomplete shot in the report."""
    root = Path(raw)
    if not raw or not root.is_dir():
        raise ValueError("원영상 폴더를 먼저 연결하세요.")
    groups, ignored = {}, []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (".tif", ".tiff"):
            continue
        match = RAW_NAME.fullmatch(path.stem)
        if not match or not 1 <= int(match[2]) <= 10:
            ignored.append(str(path))
            continue
        groups.setdefault(match[1].upper(), {}).setdefault(int(match[2]), []).append(path)
    duplicates, incomplete, complete = [], [], []
    for shot, bands in sorted(groups.items()):
        missing = [band for band in range(1, 11)
                   if band not in bands or any(path.stat().st_size == 0 for path in bands[band])]
        repeated = {band: paths for band, paths in bands.items() if len(paths) > 1}
        if missing:
            incomplete.append((shot, missing))
        for band, paths in repeated.items():
            duplicates.append((shot, band, paths))
        if not missing and not repeated:
            complete.append(shot)
    # The backend remains authoritative for source resolution. Never ask it to
    # choose one member of an ambiguous pair.
    files = general_api().scan_raw(root) if not duplicates else {}
    lines = [f"완전한 10밴드 촬영 {len(complete):,}개 / 누락 {len(incomplete):,}개 / 중복 {len(duplicates):,}쌍"]
    lines += [f"누락·빈 파일 {shot}: suffix {','.join(map(str, missing))}" for shot, missing in incomplete]
    for shot, band, paths in duplicates:
        lines.append(f"중복 {shot}_{band}: " + " | ".join(map(str, paths)))
    lines += [f"지원 이름 외 TIFF: {path}" for path in ignored]
    return dict(files=files, complete=complete, incomplete=incomplete,
                duplicates=duplicates, ignored=ignored, report="\n".join(lines))


def preview_image(path, limit=900):
    """Read just one downsampled native raster; never write/resample the source."""
    import numpy as np
    from osgeo import gdal
    from PIL import Image

    dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
    if dataset is None:
        raise ValueError(f"영상을 열 수 없습니다: {path}")
    scale = min(1.0, limit / max(dataset.RasterXSize, dataset.RasterYSize))
    width, height = (max(1, round(size * scale))
                     for size in (dataset.RasterXSize, dataset.RasterYSize))
    band = dataset.GetRasterBand(1)
    values = band.ReadAsArray(buf_xsize=width, buf_ysize=height).astype(np.float32)
    valid = np.isfinite(values)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        valid &= values != nodata
    gray = np.zeros(values.shape, dtype=np.uint8)
    if valid.any():
        low, high = np.percentile(values[valid], [2, 98])
        if high > low:
            gray[valid] = (np.clip((values[valid] - low) / (high - low), 0, 1) * 255).astype(np.uint8)
    dataset = None
    return Image.fromarray(gray)


class GeneralStudio(PipelineGUI):
    def __init__(self, root, config_path):
        try:
            config_path = Path(config_path).resolve()
            cfg = load_config(config_path)
            if not general_api().is_general(cfg):
                raise ValueError("General 프로젝트의 core preparation JSON을 선택하세요.")
        except Exception:
            destroy_owned_root(root)
            raise
        self._closing = False
        self._loading = False
        self._scan_pending = False
        self._preview_pending = False
        self._preview_request = None
        self._preview_after = None
        self._events = queue.Queue()
        self._inventory = None
        self._inventory_source = None
        self._basis_snapshot = None
        self._saved_candidates = set()
        self._optional_visible = False
        try:
            super().__init__(root, config_path)
        except Exception:
            destroy_owned_root(root)
            raise
        root.title("ELC Studio General")
        root.protocol("WM_DELETE_WINDOW", self.close)
        if getattr(root.report_callback_exception, "__func__", None) is tk.Tk.report_callback_exception:
            root.report_callback_exception = self._callback_error
        root.after(100, self._poll_general)

    def _build(self):
        super()._build()
        self.dataset_selector.pack_forget()
        outer = self.tabs.master
        header = self.last_updated_label.master
        for widget in header.winfo_children():
            if isinstance(widget, ttk.Label) and widget.cget("style") == "Title.TLabel":
                widget.configure(text="ELC Studio General")
        self.last_updated_label.configure(text="RedEdge-MX Dual · 10 native bands")
        self.update_button.pack_forget()  # Legacy restart targets the legacy entry point.
        self.identity = ttk.Frame(outer)
        self.identity.pack(fill="x", before=self.tabs, pady=(0, 8))
        self.identity_text = tk.StringVar()
        self.project_text = tk.StringVar()
        ttk.Label(self.identity, textvariable=self.identity_text,
                  font=("Malgun Gothic", 12, "bold")).pack(anchor="w")
        ttk.Label(self.identity, textvariable=self.project_text,
                  style="Subtitle.TLabel", wraplength=900).pack(anchor="w")
        self._optional_pages = self.tabs.tabs()[1:]
        self.tabs.tab(self.tab_pre, text="ELC 반사도")
        for page in self._optional_pages:
            self.tabs.forget(page)
        self._generalize_labels(self.root)
        self._install_action_help()

    def _install_action_help(self):
        self.action_hints = {}
        descriptions = {
            "원영상 스캔": "01에 연결한 원영상의 파일명·누락·중복을 확인합니다. 영상을 보정하지 않습니다.",
            "완전한 촬영분을 대상 목록에 등록": "원영상 스캔 후 완전한 10밴드 촬영분을 새 대상 CSV로 저장합니다. 원본은 이동하지 않습니다.",
            "대상 목록 갱신": "현재 설정을 저장하고 대상 CSV를 다시 읽습니다.",
            "원영상 파일로 선택": "등록된 처리 대상 중 ROI 후보 촬영번호를 선택합니다.",
            "전체 선택": "현재 등록된 모든 촬영번호를 후보로 선택합니다. 후보 저장은 별도입니다.",
            "선택 해제 저장": "후보와 기준·계수 적용 승인을 해제합니다. 기존 영상과 결과는 삭제하지 않습니다.",
            "후보 저장": "현재 선택한 촬영번호를 ROI 후보로 저장합니다. ROI·계수 승인은 별도입니다.",
            "ROI Studio 열기 · 02 선정 영상": "02에서 저장한 후보의 타프 ROI·내부 제외 화소·기준/검증 역할을 검토합니다.",
            "촬영시간·태양각 CSV 생성": "원영상 EXIF 시간으로 촬영시간 CSV를 생성합니다. 선택 GPS가 없으면 태양각은 비워 둡니다.",
            "실측 Ed 매칭": "촬영시간을 최근접 실측 Ed와 연결합니다. 허용 시간차·측정 범위를 실행 시 검사합니다.",
            "입력·용량 검사": "04의 실제 입력·촬영시간·Ed·gain·예상 용량을 검사합니다. 검사가 통과한 뒤 통합 처리를 실행하세요.",
            "통합 처리 실행": "검사한 계획으로 방사보정 → 센서 gain → Ed 정규화를 실행합니다. 클릭 시 입력 변경 여부를 다시 검사합니다.",
            "완료 결과 → ELC 입력 연결": "완료 검증된 통합 결과만 ELC 입력으로 연결합니다. 실행 중이거나 실패한 결과는 사용할 수 없습니다.",
            "기존 단계별 입력으로 복귀": "통합 결과 연결을 해제합니다. 기존 생성 결과는 삭제하지 않습니다.",
            "1. 자동 보정·계수 준비": "승인 ROI를 사용해 촬영시간·방사·센서·Ed·계수를 준비합니다. 계수 검토에서 멈추며 반사도는 아직 만들지 않습니다.",
            "2. 계수 확인·반사도 생성": "같은 실행의 품질 보고서를 검토·승인한 뒤 반사도 TIFF를 생성합니다. 처리 완료와 정확도 합격은 다릅니다.",
            "품질 보고서": "현재 회차의 최근 간편 ELC 보고서를 엽니다. 이전 실행 결과는 현재 입력 검증을 대신하지 않습니다.",
            "결과 폴더": "현재 회차의 최근 간편 ELC 실행 폴더를 엽니다.",
            "현재 작업 중지": "이 창에서 실행한 현재 작업의 중지를 요청합니다. 기존 완료 결과는 삭제하지 않습니다.",
            "실패 로그 저장": "최근 실패의 실제 traceback·설정 경로·실행 로그를 별도로 저장합니다.",
            "AI에 오류 묻기": "최근 실패 로그를 확인한 뒤 AI 도우미에서 질문할 수 있습니다. 자동으로 외부에 전송하지 않습니다.",
            "다음 단계": "다음 화면으로 이동합니다. 화면 이동은 처리 완료나 입력 검증 통과를 뜻하지 않습니다.",
            "이전 단계": "이전 화면으로 이동합니다. 저장한 ROI와 결과는 유지됩니다.",
            "불러오기": "다른 ELC 프로젝트를 불러옵니다. 처리·스캔·ROI 편집 중에는 전환할 수 없습니다.",
        }
        def visit(widget):
            if isinstance(widget, ttk.Button):
                text = str(widget.cget("text"))
                if text in descriptions:
                    self.action_hints[text] = ActionHint(
                        widget, descriptions[text], lambda name=text: self._action_reason(name), guard=True,
                        popup=lambda detail: messagebox.showwarning("버튼 상태 · 다음 조치", problem_text(
                            detail, action="위 안내의 입력 연결 또는 선행 작업을 확인하세요. 처리는 자동으로 재시작하지 않습니다."), parent=self.root))
                if text == "다음 단계":
                    self.help_button = ttk.Button(widget.master, text="진행이 안 되는 이유", command=self.explain_current_step)
                    self.help_button.pack(side="left", padx=8)
            for child in tuple(widget.winfo_children()):
                visit(child)
        visit(self.root)

    def _value(self, key):
        return self.vars[key].get().strip() if key in self.vars else str(self._get_nested(self.cfg, key, "")).strip()

    def _easy_record(self):
        from uav_pipeline.easy_elc import pointer_path, read_json
        try:
            record = read_json(pointer_path(self.cfg))
            if not isinstance(record, dict):
                raise ValueError("간편 ELC 상태 기록의 JSON 형식이 올바르지 않습니다.")
            return record
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, KeyError):
            return {"_read_error": "간편 ELC 상태 기록을 읽을 수 없습니다. 현재 프로젝트의 easy-elc.json과 로그를 확인하세요. 기존 결과는 삭제하지 마세요."}

    def _action_reason(self, name):
        if name == "현재 작업 중지":
            return "현재 이 창에서 실행 중인 처리 작업이 없습니다." if self.process is None and self.background_process is None and not self.launch_pending and not self.background_launch_pending else ""
        if name in {"실패 로그 저장", "AI에 오류 묻기"}:
            return "아직 기록된 실패가 없습니다. 실제 작업 실패 후 실패 로그를 확인할 수 있습니다." if not self.last_failure_log else ""
        if name in {"다음 단계", "이전 단계"}:
            return ""
        if name in {"품질 보고서", "결과 폴더", "2. 계수 확인·반사도 생성"}:
            record = self._easy_record()
            if record.get("_read_error"):
                return record["_read_error"]
            if not record:
                return "현재 회차에 연결된 간편 ELC 실행 기록이 없습니다. 05에서 '1. 자동 보정·계수 준비'를 먼저 실행하세요."
            if name == "품질 보고서":
                return "품질 보고서 파일이 아직 없습니다. 처리 상태와 실패 로그를 확인하세요." if not record.get("report") or not Path(record["report"]).is_file() else ""
            if name == "결과 폴더":
                return "실행 결과 폴더를 찾을 수 없습니다. 저장 드라이브와 현재 회차의 결과 경로를 확인하세요." if not record.get("config") or not Path(record["config"]).parent.is_dir() else ""
            if record.get("status") != "awaiting_review":
                return f"현재 실행 상태: {record.get('status', '확인 필요')}. 계수 준비가 끝나 '적용 승인 대기'가 되어야 생성할 수 있습니다. 이미 완료한 실행은 다시 적용하지 않습니다."
        if self._busy():
            if self._scan_pending:
                return "원영상 목록을 검사 중입니다. 스캔이 끝난 뒤 이 작업을 실행하세요."
            if self.roi_editor_open():
                return "ROI 편집창이 열려 있습니다. ROI를 저장하고 창을 닫은 뒤 진행하세요."
            return "다른 처리 작업이 진행 중입니다. 완료되거나 중지된 뒤 실행하세요. 중복 처리는 시작하지 않습니다."
        if name in {"불러오기", "기존 단계별 입력으로 복귀"}:
            return ""
        if name == "1. 자동 보정·계수 준비" and self.easy_full.get() and self.elc_only.get():
            return "선택 ELC 영상만 처리 모드와 전체 촬영분 생성이 동시에 선택되어 있습니다. 01의 선택 영상 모드를 해제하거나 05의 전체 생성 선택을 해제하세요."
        if name == "완전한 촬영분을 대상 목록에 등록":
            if not self._inventory or self._inventory_source != self._value("preparation.raw_directory"):
                return "현재 원영상 폴더의 스캔 결과가 없습니다. 01에서 '원영상 스캔'을 먼저 누르세요."
            if self._inventory["duplicates"]:
                return "같은 촬영번호·밴드의 원영상이 여러 개입니다. 01의 스캔 보고서에서 중복 경로를 확인한 뒤 다시 스캔하세요."
            if not self._inventory["complete"]:
                return "등록할 완전한 10밴드 촬영분이 없습니다. 누락·빈 파일과 원영상 폴더를 확인하세요."
            return ""
        raw = self._value("preparation.raw_directory")
        if not raw or not Path(raw).is_dir():
            return "원영상 폴더가 연결되지 않았거나 열 수 없습니다. 01의 '원영상 루트'를 먼저 지정하세요."
        if name == "원영상 스캔":
            return ""
        targets = self._value("preparation.shot_list_csv")
        if not targets or not Path(targets).is_file():
            return "처리 대상 촬영 CSV가 없습니다. 01에서 스캔 후 '완전한 촬영분을 대상 목록에 등록'하거나 대상 CSV를 선택하세요."
        if name == "대상 목록 갱신":
            return ""
        if not self.target_tree.get_children():
            return "현재 대상 목록이 비어 있거나 읽지 못했습니다. 02의 '대상 목록 갱신'과 표시된 CSV 오류를 확인하세요."
        selection_actions = {"원영상 파일로 선택", "전체 선택", "선택 해제 저장", "후보 저장"}
        if name in selection_actions:
            return ""
        if set(self.target_tree.selection()) != self._saved_candidates:
            return "02에서 후보 선택을 바꾼 뒤 저장하지 않았습니다. '후보 저장'을 누르거나 대상 목록을 다시 불러오세요."
        if not self._saved_candidates and (name == "ROI Studio 열기 · 02 선정 영상" or self.elc_only.get() and name in {"1. 자동 보정·계수 준비", "입력·용량 검사", "통합 처리 실행", "실측 Ed 매칭", "촬영시간·태양각 CSV 생성"}):
            return "저장된 ELC 후보가 없습니다. 02에서 타프가 있는 기준·검증 촬영분을 선택하고 후보를 저장하세요."
        if name in {"ROI Studio 열기 · 02 선정 영상", "1. 자동 보정·계수 준비"}:
            path = self._value("preparation.tarp_reflectance_csv")
            if not path or not Path(path).is_file():
                return "타프 실측 반사도 CSV가 없습니다. 01에서 실제 타프의 파장별 반사도 자료를 연결하세요."
        if name in {"실측 Ed 매칭", "입력·용량 검사", "통합 처리 실행", "1. 자동 보정·계수 준비"}:
            required = [("실측 Ed", "ed_csv")]
            if name != "실측 Ed 매칭":
                required += [("센서 gain", "gain_csv")]
            for label, key in required:
                path = self._value("preparation." + key)
                if not path or not Path(path).is_file():
                    return f"{label} CSV가 없습니다. 01 자료 연결에서 해당 파일을 지정하세요."
            if not self._value("preparation.reference_shot"):
                return "ELC·Ed 기준 영상이 정해지지 않았습니다. 03의 ROI Studio에서 기준 촬영을 확인·저장하세요."
            if self._value("preparation.reference_shot") not in self.target_tree.get_children():
                return "기준 촬영번호가 현재 처리 대상에 없습니다. 01의 대상 목록과 03의 기준 영상 선택이 같은 회차인지 확인하세요."
        if name == "통합 처리 실행":
            from uav_pipeline.preparation import locations
            pointer = locations(self.cfg)["configs"] / "integrated_latest.json"
            if not pointer.is_file():
                return "현재 회차의 통합 입력 검사 결과가 없습니다. 04에서 '입력·용량 검사'를 먼저 실행하세요."
        if name == "완료 결과 → ELC 입력 연결":
            from uav_pipeline.preparation import locations
            try:
                pointer = json.loads((locations(self.cfg)["configs"] / "integrated_latest.json").read_text(encoding="utf-8"))
                plan = json.loads(Path(pointer["plan"]).read_text(encoding="utf-8"))
                status = json.loads((Path(plan["run"]) / "state.json").read_text(encoding="utf-8"))["status"]
                if status != "complete":
                    return f"통합 처리 상태는 {status}입니다. 최종 검증이 complete인 결과만 연결할 수 있습니다."
            except (OSError, ValueError, KeyError, TypeError):
                return "완료된 통합 처리 기록이 없습니다. 04의 입력·용량 검사와 통합 처리를 완료하세요."
        return ""

    def explain_problem(self, title, detail):
        messagebox.showwarning(title, problem_text(detail), parent=self.root)

    def explain_current_step(self):
        current = self.pre_steps.index(self.pre_steps.select())
        stages = (
            ("원영상 스캔", "완전한 촬영분을 대상 목록에 등록"),
            ("대상 목록 갱신", "후보 저장"),
            ("ROI Studio 열기 · 02 선정 영상",),
            ("촬영시간·태양각 CSV 생성", "실측 Ed 매칭", "입력·용량 검사", "통합 처리 실행", "완료 결과 → ELC 입력 연결"),
            ("1. 자동 보정·계수 준비", "2. 계수 확인·반사도 생성", "품질 보고서"),
        )
        details = [self.pre_steps.tab(current, "text")]
        for name in stages[current]:
            details += ["", name, self._action_reason(name) or "기본 연결 조건 확인됨. 실제 내용·승인·서명·용량은 실행 시 상세 검사합니다."]
        details += ["", "원인 확인은 설정을 변경하거나 처리를 재시작하지 않습니다."]
        messagebox.showinfo("현재 단계 · 진행 조건", "\n".join(details), parent=self.root)

    def _callback_error(self, kind, error, tb):
        detail = "".join(traceback.format_exception(kind, error, tb))
        self._append(detail + "\n")
        self._record_failure("GUI 동작", "exception", detail)

    def _record_failure(self, stage, code, detail=""):
        try:
            path = super()._record_failure(stage, code, detail)
        except OSError as exc:
            path = None
            detail += f"\n실패 로그를 저장하지 못했습니다: {exc}"
        raw = detail or self.log.get("1.0", "end-1c")
        messagebox.showerror("작업 실패 · 원인과 조치", problem_text(primary_error(raw), log_path=path), parent=self.root)
        return path

    def _build_geometry_tab(self):
        self.inputs_reviewed = tk.BooleanVar(value=False)

    def _build_brdf_tab(self):
        pass

    def _build_preprocess_tab(self):
        super()._build_preprocess_tab()
        pane0, pane1 = (self.pre_steps.nametowidget(path) for path in self.pre_steps.tabs()[:2])
        # Keep legacy handles alive, but never retain hidden raw/GPS managed controls.
        for pane in (pane0, pane1):
            for child in pane.winfo_children():
                child.pack_forget()
        for key in SOURCE_KEYS + ("preparation.gps_path",):
            controls = self.managed_path_controls.pop(key, None)
            if controls:
                self.auto_path_controls = [w for w in self.auto_path_controls if w not in controls[:2]]
        self.pre_steps.tab(pane0, text="01 자료 연결")
        self.pre_steps.tab(pane1, text="02 원영상 후보")
        self.pre_steps.tab(2, text="03 타프 ROI·승인")
        self.pre_steps.tab(3, text="04 방사·센서·Ed")
        pane2 = self.pre_steps.nametowidget(self.pre_steps.tabs()[2])
        for child in pane2.winfo_children():
            if isinstance(child, ttk.Labelframe) and "우선 처리" in child.cget("text"):
                child.pack_forget()
        # Legacy standalone workers still assume GPS/campaign layouts. Their
        # handles survive for base callbacks, but General never advertises them.
        self._hide_legacy_actions(self.pre_steps)
        self.full_reflectance_button.master.pack_forget()
        self._build_general_inputs(self._scroll_content(pane0))
        self._build_candidates(pane1)

    def _hide_legacy_actions(self, widget):
        if isinstance(widget, ttk.Button) and "Ed 정규화·센서 상호교정" in widget.cget("text"):
            widget.pack_forget()
        if isinstance(widget, ttk.Labelframe) and widget.cget("text") == "기존 단계별 처리":
            widget.pack_forget()
        for child in widget.winfo_children():
            self._hide_legacy_actions(child)

    def _build_general_inputs(self, parent):
        ttk.Label(parent, text="프로젝트 입력", font=("Malgun Gothic", 14, "bold")).pack(anchor="w", pady=(0, 8))
        paths = ttk.Frame(parent)
        paths.pack(fill="x")
        self._path_row(paths, 0, "원영상 루트", "preparation.raw_directory", "directory")
        self._path_row(paths, 1, "처리 대상 촬영 CSV", "preparation.shot_list_csv")
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=8)
        self.scan_button = ttk.Button(actions, text="원영상 스캔", command=self.scan_sources)
        self.scan_button.pack(side="left")
        self.register_button = ttk.Button(actions, text="완전한 촬영분을 대상 목록에 등록",
                                         command=self.register_complete, state="disabled")
        self.register_button.pack(side="left", padx=6)
        self.scan_status = tk.StringVar(value="원영상 미검사")
        ttk.Label(parent, textvariable=self.scan_status, style="Subtitle.TLabel", wraplength=850).pack(anchor="w")
        self.scan_report = tk.Text(parent, height=3, wrap="word", state="disabled",
                                   background="#131518", foreground="#bbc4cc", borderwidth=0)
        self.scan_report.pack(fill="x", pady=(4, 10))
        calibration = ttk.Frame(parent)
        calibration.pack(fill="x")
        self._path_row(calibration, 0, "타프 실측 반사도 CSV", "preparation.tarp_reflectance_csv")
        self._path_row(calibration, 1, "센서 gain CSV", "preparation.gain_csv")
        self._path_row(calibration, 2, "실측 Ed CSV", "preparation.ed_csv")
        self._path_row(calibration, 3, "결과 폴더", "output.directory", "directory")
        options = ttk.Frame(parent)
        options.pack(fill="x", pady=10)
        ttk.Label(options, text=SENSOR).pack(side="left")
        self._var("preparation.sensor").set(SENSOR)
        ttk.Checkbutton(options, text="선택 ELC 영상만 처리", variable=self.elc_only,
                        command=self._update_elc_only_controls).pack(side="left", padx=16)
        ttk.Label(options, text="원본 시간대").pack(side="left")
        ttk.Combobox(options, textvariable=self._var("preparation.source_timezone"),
                     values=("UTC", "Asia/Seoul"), state="readonly", width=13).pack(side="left", padx=6)
        self.spatial_button = ttk.Button(parent, text="선택 입력 · 태양각 기록용 GPS",
                                        command=self.toggle_spatial)
        self.spatial_button.pack(anchor="w")
        self.spatial_inputs = ttk.Frame(parent)
        self._path_row(self.spatial_inputs, 0, "실제 GPS TXT/CSV (선택)", "preparation.gps_path")

    def _build_candidates(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 8))
        for text, command in (("대상 목록 갱신", self.reload_targets), ("원영상 파일로 선택", self.pick_raw_files),
                              ("전체 선택", self.select_all), ("선택 해제 저장", self.clear_candidates)):
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=(0, 6))
        body = ttk.Panedwindow(parent, orient="horizontal")
        body.pack(fill="both", expand=True)
        left, right = ttk.Frame(body), ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=3)
        self.target_tree = ttk.Treeview(left, columns=("shot",), show="headings", selectmode="extended", height=10)
        self.target_tree.heading("shot", text="등록된 처리 대상 촬영번호")
        self.target_tree.column("shot", width=230, minwidth=180)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.target_tree.yview)
        self.target_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.target_tree.pack(fill="both", expand=True)
        self.target_tree.bind("<<TreeviewSelect>>", self._selection_changed)
        preview_bar = ttk.Frame(right)
        preview_bar.pack(fill="x", padx=8)
        ttk.Label(preview_bar, text="원본 suffix").pack(side="left")
        self.preview_band = tk.StringVar(value="1")
        bands = ttk.Combobox(preview_bar, textvariable=self.preview_band,
                             values=tuple(map(str, range(1, 11))), state="readonly", width=4)
        bands.pack(side="left", padx=6)
        bands.bind("<<ComboboxSelected>>", self._selection_changed)
        self.preview_status = tk.StringVar(value="")
        ttk.Label(preview_bar, textvariable=self.preview_status, style="Subtitle.TLabel").pack(side="left")
        self.preview = tk.Canvas(right, background="#131518", highlightthickness=0, width=650, height=300)
        self.preview.pack(fill="both", expand=True, padx=8, pady=6)
        self.preview.bind("<Configure>", self._draw_preview)
        self._preview_image = None
        footer = ttk.Frame(parent)
        footer.pack(fill="x", pady=(8, 0))
        self.candidate_status = tk.StringVar()
        ttk.Label(footer, textvariable=self.candidate_status, wraplength=450).pack(side="left")
        ttk.Button(footer, text="후보 저장", style="Primary.TButton", command=self.save_candidates).pack(side="right")

    def _generalize_labels(self, widget):
        if "text" in widget.keys():
            text = str(widget.cget("text"))
            changed = text.replace("GPS 전체 촬영분", "등록된 전체 촬영분").replace("필지", "관심영역")
            changed = changed.replace("RAMSES 10밴드 Ed CSV (자동)", "실측 10밴드 Ed CSV")
            changed = changed.replace("센서 gain CSV (MX Dual 공통값 기본)", "센서 gain CSV")
            if changed != text:
                widget.configure(text=changed)
            if isinstance(widget, ttk.Label) and len(changed) > 65:
                widget.configure(wraplength=850)
        for child in widget.winfo_children():
            self._generalize_labels(child)

    def toggle_analysis(self):
        raise ValueError("이 배포본은 ELC 반사도 산출 전용입니다.")

    def toggle_spatial(self):
        if self.spatial_inputs.winfo_manager():
            self.spatial_inputs.pack_forget()
        else:
            self.spatial_inputs.pack(fill="x", pady=8)

    def _busy(self):
        return bool(self.process is not None or self.background_process is not None
                    or self.launch_pending or self.background_launch_pending or self._scan_pending
                    or self.roi_editor_open())

    def _idle(self):
        if self._busy():
            messagebox.showwarning("작업 중", "처리·스캔·ROI 편집을 마친 뒤 변경하세요.", parent=self.root)
            return False
        return True

    def close(self):
        if self._closing:
            return True
        if not self._idle():
            return False
        self._closing = True
        destroy_owned_root(self.root)
        return True

    def _fingerprint(self, cfg):
        result = []
        for key in BASIS_KEYS:
            value = str(self._get_nested(cfg, key, ""))
            digest = ""
            if key.endswith("_csv") and value and Path(value).is_file():
                digest = hashlib.sha256(Path(value).read_bytes()).hexdigest()
            result.append((value, digest))
        return tuple(result)

    def _load_to_form(self):
        self._loading = True
        try:
            initialize(self.cfg)
            if not general_api().is_general(self.cfg):
                raise ValueError("General 프로젝트가 아닙니다.")
            g = self.cfg["general"]
            # Dataset labels are compatibility identifiers, never discovery inputs.
            self.cfg["dataset"].update(root=g["project_root"], date=g["date"].replace("-", "_") + "_" + g["site"], flight=g["session"])
            self.identity_text.set(f"{g['site']}   ·   {g['date']}   ·   {g['session']}")
            self.project_text.set(g["project_root"])
            self.dataset_selector.root_var.set(g["project_root"])
            self.dataset_selector.date_var.set(self.cfg["dataset"]["date"])
            self.dataset_selector.flight_var.set(g["session"])
            for key, variable in self.vars.items():
                variable.set(str(self._get_nested(self.cfg, key, "")))
            p = self.cfg["preparation"]
            self.elc_only.set(bool(p.get("elc_only", True)))
            self.coefficient_confirmed.set(bool(p.get("coefficient_basis_confirmed", False)))
            self.integrated_save_radiance.set(bool(p.get("integrated_save_radiance", False)))
            self.allow_gains.set(bool(self.cfg["preprocess"].get("allow_nonunity_sensor_gains", False)))
            self.inputs_reviewed.set(bool(self.cfg["dataset"].get("input_settings_reviewed", False)))
            self.edit_pre_paths.set(True)
            self._toggle_pre_paths()
            self._update_reference_mosaic_controls()
            self._update_elc_only_controls()
            self.refresh_integrated_status()
            self._basis_snapshot = self._fingerprint(self.cfg)
            self._inventory = self._inventory_source = None
            self.register_button.configure(state="disabled")
            self._load_targets()
        finally:
            self._loading = False

    def _toggle_pre_paths(self):
        self.edit_pre_paths.set(True)
        super()._toggle_pre_paths()

    def _update_ed_csv_controls(self):
        if hasattr(self, "ed_csv_entry"):
            self.ed_csv_entry.configure(state="normal")
            self.ed_csv_browse.configure(state="normal")
            self.ed_csv_status.configure(text="실측 Ed · 시간대 KST 및 단위 확인 필요")

    def _update_reference_mosaic_controls(self):
        pass

    def connect_reference_mosaic(self):
        self.browse("tarp_workflow.reference_mosaic", "file")

    def browse(self, key, kind):
        if not self._idle():
            return
        if kind == "directory":
            value = filedialog.askdirectory(parent=self.root)
        else:
            value = filedialog.askopenfilename(parent=self.root, filetypes=[("지원 파일", "*.csv *.txt *.tif *.tiff *.json *.geojson"), ("모든 파일", "*.*")])
        if value:
            self._var(key).set(value)

    def choose_config(self):
        if not self._idle():
            return
        value = filedialog.askopenfilename(parent=self.root, filetypes=[("General 프로젝트", "*.json")])
        if value:
            self.load(Path(value))

    def load(self, path):
        """Also used by the inherited elc_fit completion callback."""
        if not self._idle():
            return False
        try:
            cfg = load_config(path)
            if not general_api().is_general(cfg):
                raise ValueError("General core preparation JSON을 선택하세요.")
            self.cfg, self.config_path = cfg, Path(path).resolve()
            self.config_var.set(str(self.config_path))
            self._load_to_form()
            self.refresh_status()
            return True
        except Exception as exc:
            self.explain_problem("프로젝트 불러오기", exc)
            return False

    def _reset_basis(self, clear_reference=False):
        p = self.cfg["preparation"]
        p["coefficient_basis_confirmed"] = False
        p["approved_roi_manifest"] = ""
        p["integrated_plan_file"] = ""
        self.coefficient_confirmed.set(False)
        if clear_reference:
            p["reference_shot"] = ""
            self._var("preparation.reference_shot").set("")
        self.roi_handoff_id = self.roi_manifest_applied = self.roi_handoff_path = None

    def save(self, quiet=False):
        if not self._idle():
            return False
        try:
            proposed = copy.deepcopy(self.cfg)
            for key, variable in self.vars.items():
                value = variable.get().strip()
                if key in {"photos.reflectance_scale", "brdf.min_vza_range_deg"}:
                    value = float(value)
                elif key in {"brdf.min_observations", "brdf.min_raa_bin_count"}:
                    value = int(value)
                self._set_nested(proposed, key, value)
            signature = self._fingerprint(proposed)
            changed = self._basis_snapshot is not None and signature != self._basis_snapshot
            source_changed = changed and signature[:2] != self._basis_snapshot[:2]
            if source_changed and (self._saved_candidates or self.cfg["preparation"].get("approved_roi_manifest")):
                if not messagebox.askyesno("원본·대상 변경", "후보 선택·기준 영상·계수 적용 승인을 해제합니다. 기존 결과는 보존합니다. 계속할까요?", parent=self.root):
                    return False
            self.cfg = proposed
            p = self.cfg["preparation"]
            p.update(coefficient_basis_confirmed=self.coefficient_confirmed.get(), elc_only=self.elc_only.get(),
                     integrated_save_radiance=self.integrated_save_radiance.get())
            self.cfg["preprocess"]["allow_nonunity_sensor_gains"] = self.allow_gains.get()
            self.cfg["dataset"]["input_settings_reviewed"] = self.inputs_reviewed.get()
            if changed:
                self._reset_basis(clear_reference=source_changed)
            if source_changed:
                general_api().save_candidates(self.cfg, [])
                self._saved_candidates = set()
                self._inventory = self._inventory_source = None
                self.register_button.configure(state="disabled")
            generate(self.cfg)
            requested = Path(self.config_var.get()).resolve()
            requested.parent.mkdir(parents=True, exist_ok=True)
            save_config(requested, self.cfg)
            self.config_path = requested
            self._basis_snapshot = self._fingerprint(self.cfg)
            if source_changed:
                self._load_targets()
            self._update_reference_mosaic_controls()
            self.refresh_status()
            self.refresh_integrated_status()
            if not quiet:
                self._append("General 프로젝트 설정 저장 완료\n")
            return True
        except Exception as exc:
            self.explain_problem("설정 저장 실패", exc)
            return False

    def _load_targets(self):
        self.target_tree.delete(*self.target_tree.get_children())
        self._preview_request = None
        self._preview_image = None
        self.preview.delete("all")
        self._saved_candidates = set()
        try:
            shots = sorted(general_api().source_shots(self.cfg))
            for shot in shots:
                self.target_tree.insert("", "end", iid=shot, values=(shot,))
            _, _, saved = latest_candidate_selection(self.cfg)
            self._saved_candidates = set(saved) & set(shots)
            self.target_tree.selection_set(sorted(self._saved_candidates))
            self._candidate_label()
        except (OSError, ValueError, KeyError) as exc:
            self.candidate_status.set(f"대상 목록 확인 필요: {exc}")

    def reload_targets(self):
        if self.save(quiet=True):
            self._load_targets()

    def _candidate_label(self):
        chosen = set(self.target_tree.selection())
        dirty = chosen != self._saved_candidates
        self.candidate_status.set(f"대상 {len(self.target_tree.get_children()):,}개 · 선택 {len(chosen):,}개 · " + ("미저장" if dirty else "저장됨"))

    def select_all(self):
        if self._idle():
            self.target_tree.selection_set(self.target_tree.get_children())

    def pick_raw_files(self):
        if not self._idle() or not self.save(quiet=True):
            return
        paths = filedialog.askopenfilenames(parent=self.root, filetypes=[("Native TIFF", "*.tif *.tiff")])
        if not paths:
            return
        raw = Path(self.cfg["preparation"].get("raw_directory", "")).resolve()
        targets = set(self.target_tree.get_children())
        shots, rejected = set(), []
        for value in paths:
            path = Path(value).resolve()
            match = RAW_NAME.fullmatch(path.stem)
            if not path.is_relative_to(raw) or not match or match[1].upper() not in targets or not 1 <= int(match[2]) <= 10:
                rejected.append(value)
            else:
                shots.add(match[1].upper())
        if rejected:
            messagebox.showwarning("대상 외 원영상", "선택을 변경하지 않았습니다. 원영상 루트·대상 CSV를 확인하세요.\n" + "\n".join(rejected), parent=self.root)
            return
        self.target_tree.selection_set(sorted(shots))

    def clear_candidates(self):
        if self._idle() and messagebox.askyesno("후보 선택 해제", "저장된 후보 선택·기준 영상·계수 적용 승인을 해제할까요?", parent=self.root):
            self.target_tree.selection_set(())
            self.save_candidates(confirm=False)

    def save_candidates(self, confirm=True):
        if not self.save(quiet=True):
            return False
        shots = sorted(self.target_tree.selection())
        if confirm and not messagebox.askyesno("원영상 후보 저장", f"등록된 대상 중 {len(shots):,}촬영을 ROI 후보로 저장할까요? ROI·계수는 승인하지 않습니다.", parent=self.root):
            return False
        try:
            reference = self.cfg["preparation"].get("reference_shot", "")
            path = general_api().save_candidates(self.cfg, shots, reference_shot=reference if reference in shots else "")
            self._reset_basis(clear_reference=reference not in shots)
            save_config(self.config_path, self.cfg)
            self._basis_snapshot = self._fingerprint(self.cfg)
            self._saved_candidates = set(shots)
            self._candidate_label()
            self._append(f"[02 후보] {len(shots)}촬영 저장: {path}\n")
            return True
        except Exception as exc:
            self.explain_problem("후보 저장", exc)
            return False

    def scan_sources(self):
        if not self.save(quiet=True):
            return
        raw = self.cfg["preparation"].get("raw_directory", "")
        self._scan_pending = True
        self._inventory = None
        self.register_button.configure(state="disabled")
        self.scan_status.set("파일 목록 검사 중")
        self._async("scan", raw, lambda: inspect_raw(raw))

    def register_complete(self):
        if not self._idle():
            return
        inventory = self._inventory
        if not inventory or self._inventory_source != self._var("preparation.raw_directory").get().strip():
            messagebox.showwarning("스캔 필요", "현재 원영상 루트를 다시 스캔하세요.", parent=self.root)
            return
        if inventory["duplicates"] or not inventory["complete"]:
            messagebox.showwarning("등록 불가", "중복을 해결하고 완전한 10밴드 촬영을 확인하세요.", parent=self.root)
            return
        summary = inventory["report"].splitlines()[0]
        if not messagebox.askyesno("처리 대상 명시 등록", summary + "\n\n누락 촬영·지원 이름 외 파일은 등록하지 않습니다. 보고서를 확인했나요?\n완전한 촬영분만 새 대상 CSV로 저장합니다. 원본은 이동하지 않습니다.", parent=self.root):
            return
        if not self.save(quiet=True):
            return
        try:
            path = general_api().save_shot_list(self.cfg, inventory["complete"])
            self._var("preparation.shot_list_csv").set(str(path))
            # The helper may update cfg in place; save still compares the old signature.
            if self.save(quiet=True):
                self._load_targets()
                self.pre_steps.select(1)
        except Exception as exc:
            self.explain_problem("대상 목록 등록", exc)

    def _async(self, kind, token, function):
        def worker():
            try:
                self._events.put((kind, token, function(), None))
            except Exception as exc:
                self._events.put((kind, token, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _selection_changed(self, _event=None):
        self._candidate_label()
        if self._preview_after:
            self.root.after_cancel(self._preview_after)
        self._preview_after = self.root.after(180, self._request_preview)

    def _request_preview(self):
        self._preview_after = None
        chosen = self.target_tree.selection()
        self._preview_request = (self._var("preparation.raw_directory").get().strip(),
                                 self.target_tree.focus() if self.target_tree.focus() in chosen else (chosen[0] if chosen else ""),
                                 int(self.preview_band.get()))
        if not chosen:
            self._preview_image = None
            self.preview.delete("all")
            self.preview_status.set("")
        elif not self._preview_pending:
            self._start_preview()

    def _start_preview(self):
        token = self._preview_request
        if not token or not token[1]:
            return
        self._preview_pending = True
        self.preview_status.set(f"{token[1]}_{token[2]} · 읽는 중")
        def read():
            if not token[0] or not Path(token[0]).is_dir():
                raise ValueError("원영상 루트를 연결하세요.")
            files = general_api().scan_raw(Path(token[0]), shots={token[1]})
            path = files.get((token[1], token[2]))
            if path is None:
                raise ValueError("선택한 원본 밴드가 없습니다.")
            return preview_image(path)
        self._async("preview", token, read)

    def _draw_preview(self, _event=None):
        if self._preview_image is None:
            return
        from PIL import ImageTk
        image = self._preview_image.copy()
        width, height = max(1, self.preview.winfo_width()), max(1, self.preview.winfo_height())
        image.thumbnail((width, height))
        self._photo = ImageTk.PhotoImage(image, master=self.root)
        self.preview.delete("all")
        self.preview.create_image(width / 2, height / 2, image=self._photo)

    def _poll_general(self):
        if self._closing:
            return
        try:
            while True:
                kind, token, result, error = self._events.get_nowait()
                if kind == "scan":
                    self._scan_pending = False
                    self._inventory_source = token
                    self._inventory = result
                    report = error or result["report"]
                    self.scan_status.set(report.splitlines()[0])
                    self.scan_report.configure(state="normal")
                    self.scan_report.delete("1.0", "end")
                    self.scan_report.insert("1.0", report)
                    self.scan_report.configure(state="disabled")
                    self.register_button.configure(state="normal" if result and result["complete"] and not result["duplicates"] else "disabled")
                    self._append("[원영상 스캔]\n" + report + "\n")
                    if error:
                        self.explain_problem("원영상 스캔 실패", error)
                else:
                    self._preview_pending = False
                    if token == self._preview_request:
                        self._preview_image = None if error else result
                        self.preview.delete("all")
                        self.preview_status.set(error or f"{token[1]}_{token[2]} · 축소 표시")
                        self._draw_preview()
                    elif self._preview_request and self._preview_request[1]:
                        self._start_preview()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_general)

    def _ready_for_processing(self):
        if not self._idle():
            return False
        if set(self.target_tree.selection()) != self._saved_candidates:
            messagebox.showwarning("후보 미저장", "02의 후보 선택을 저장하거나 다시 불러오세요.", parent=self.root)
            return False
        return True

    def open_tarp_roi_workflow(self):
        if self._ready_for_processing():
            super().open_tarp_roi_workflow()

    def _use_reference_shot(self, shot, method=""):
        super()._use_reference_shot(shot, method)
        self._basis_snapshot = self._fingerprint(self.cfg)

    def export_time_ed(self, with_ed):
        if self._ready_for_processing():
            super().export_time_ed(with_ed)

    def run(self, stage):
        messagebox.showwarning("ELC 전용", "04의 통합 처리 또는 05의 간편 ELC를 사용하세요.", parent=self.root)

    def run_background_stage(self, stage):
        if stage not in {"integrated", "integrated_preflight"}:
            messagebox.showwarning("General 처리", "04의 통합 처리를 사용하세요.", parent=self.root)
            return
        if self._ready_for_processing():
            super().run_background_stage(stage)

    def run_easy_elc(self, action):
        if not self._ready_for_processing() or not self.save(quiet=True):
            return
        from uav_pipeline.easy_elc import pointer_path, read_json, digest
        command = [sys.executable, "-m", "uav_pipeline.easy_elc", "--config", str(self.config_path), "--action", action]
        try:
            if action == "prepare":
                full = self.easy_full.get()
                if full and self._elc_only_enabled():
                    raise ValueError("선택 ELC 영상만 처리 모드에서는 전체 생성을 선택할 수 없습니다.")
                scope = "등록된 전체 촬영분" if full else "02 선정 기준·검증 촬영분"
                if not messagebox.askyesno("간편 ELC 시작", f"{scope}을 새 실행 폴더에 저장합니다.\n"
                        "촬영시간 → 방사·센서·Ed 보정 → ELC 계수까지 진행합니다.\n"
                        "입력·용량 검사 후 계수 검토를 기다립니다. 기존 결과는 보존합니다.\n계속할까요?", parent=self.root):
                    return
                if full:
                    command.append("--full")
            elif action == "finish":
                record = read_json(pointer_path(self.cfg))
                if record["status"] != "awaiting_review":
                    raise ValueError("먼저 계수를 준비하고 품질 보고서를 검토하세요.")
                config_hash = digest(record["config"])
                os.startfile(record["report"])
                if not messagebox.askyesno("계수·적용 기준 승인", "보고서의 타프·밴드별 계수와 보정 기준을 확인했나요?\n"
                        "확인하면 같은 실행의 Ed·센서 보정 영상에 해당 계수를 적용합니다.\n"
                        "독립 검증·품질 보고서를 생성합니다. 처리 완료는 품질 합격이 아닙니다.\n계속할까요?", parent=self.root):
                    return
                command += ["--approved-config-sha256", config_hash]
            else:
                raise ValueError("지원하지 않는 간편 ELC 작업입니다.")
        except (OSError, ValueError, KeyError) as exc:
            self.explain_problem("간편 ELC 확인", exc)
            return
        self.launch_pending = True
        self._reset_progress("간편 ELC · " + action)
        self.stop_button.configure(state="normal")
        self._begin_timing("easy_elc")
        threading.Thread(target=self._worker, args=(command, "easy_elc"), daemon=True).start()

    def refresh_status(self):
        super().refresh_status()
        for item in self.status_tree.get_children():
            values = self.status_tree.item(item, "values")
            if any(word in str(values[0]) for word in ("관측기하", "VZA", "필지", "BRDF", "뷰어용")):
                self.status_tree.delete(item)
                continue
            self.status_tree.item(item, values=tuple(str(v).replace("필지", "관심영역") for v in values))


def main(argv=None):
    parser = argparse.ArgumentParser(description="General MX Dual ELC Studio")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--stage", type=int, choices=range(1, 6), default=1, help="Visible ELC stage, 1 through 5")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args(argv)
    if args.screenshot and not args.smoke:
        parser.error("--screenshot requires --smoke (test-owned window only)")
    root = tk.Tk()
    errors = []
    if args.smoke:
        def callback_error(kind, value, traceback):
            errors.append(str(value))
            print(f"GUI_CALLBACK_ERROR {kind.__name__}: {value}", file=sys.stderr)
            root.after_idle(lambda: destroy_owned_root(root))
        root.report_callback_exception = callback_error
    try:
        app = GeneralStudio(root, args.config)
        app.pre_steps.select(args.stage - 1)
        if args.smoke:
            root.title(f"ELC Studio General [GUI smoke {os.getpid()}]")
            def finish():
                try:
                    if args.screenshot:
                        from PIL import ImageGrab
                        root.deiconify()
                        root.lift()
                        root.attributes("-topmost", True)
                        root.update()
                        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                        x, y = root.winfo_rootx(), root.winfo_rooty()
                        ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(args.screenshot)
                        root.attributes("-topmost", False)
                    print(f"GENERAL_GUI_SMOKE_OK stage={args.stage} {root.winfo_width()}x{root.winfo_height()}", flush=True)
                finally:
                    # Smoke owns this root and never starts processing. Do not
                    # use the interactive close guard or open a blocking dialog.
                    app._closing = True
                    destroy_owned_root(root)
            root.after(1800, finish)
        root.mainloop()
    finally:
        destroy_owned_root(root)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
