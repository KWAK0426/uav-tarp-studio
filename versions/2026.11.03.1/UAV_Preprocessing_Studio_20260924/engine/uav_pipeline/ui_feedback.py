"""Local, read-only explanations for unavailable actions and recorded failures."""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import messagebox, ttk


def problem_text(detail, *, action="", step="", log_path=None):
    raw = str(detail).strip() or "오류 세부 내용이 제공되지 않았습니다."
    lower = raw.lower()
    if not action:
        rules = (
            (("exiftool",), "02_CHECK.bat으로 ExifTool 실행을 확인하세요. 긴 경로 오류라면 문서의 버전·경로 제한도 확인하세요.", "설치 환경 / 04 방사·센서·Ed"),
            (("no space", "disk full", "errno 28", "not enough space", "디스크", "공간", "용량 부족"), "결과 저장 드라이브의 여유 공간을 확보하거나 결과 폴더를 변경한 뒤 입력·용량 검사를 다시 하세요.", "01 자료 연결 / 04 입력·용량 검사"),
            (("permission", "access is denied", "winerror 5", "errno 13", "권한", "액세스"), "해당 파일을 사용하는 프로그램을 확인하고, 쓰기 가능한 로컬 결과 폴더인지 확인하세요. 보안 설정은 임의로 해제하지 마세요.", "입력 경로 / 결과 저장 폴더"),
            (("shot_list", "target csv", "source list", "shot column", "대상 목록", "처리 대상"), "01에서 원영상을 스캔한 뒤 완전한 촬영분을 대상 목록에 등록하거나, shot 열이 있는 대상 CSV를 연결하세요.", "01 자료 연결"),
            (("raw_directory", "raw directory", "원영상 폴더", "원영상 루트", "missing or zero-byte raw", "duplicate shot"), "원영상 폴더·10밴드 파일·중복 촬영번호를 확인하고 다시 스캔하세요. 원본을 자동 대체하지 않습니다.", "01 자료 연결 / 02 원영상 후보"),
            (("signature", "sha256", "hash", "changed", "tamper", "서명", "변경"), "입력과 현재 설정이 같은지 확인하세요. 변경한 자료는 ROI 검토·입력 검사·계수 산출 및 승인을 해당 단계부터 다시 해야 합니다.", "01 입력 확인 → 03 ROI → 04 검사 → 05 계수"),
            (("tarp_reflectance", "타프 실측"), "사용할 타프의 실제 파장별 반사도 CSV를 연결하고 표 형식을 확인하세요. 빈 양식으로는 계산할 수 없습니다.", "01 타프 실측 반사도 CSV"),
            (("gain", "센서 교정"), "장비에 유효한 파장별 센서 gain CSV와 실제 파장 대응을 확인하세요.", "01 센서 gain CSV"),
            (("ed_csv", "irradiance", "ed 매칭", "ed 정규화", "시간차", "ed matching", "nearest", "실측 ed"), "실제 촬영시간대와 Ed의 KST 시간을 확인하세요. 측정 구간·허용 시간차 안의 실측값이 필요하며 임의 보간이나 허용오차 확대는 하지 않습니다.", "04 방사·센서·Ed"),
            (("roi", "polygon", "mask", "경계", "승인"), "선정 영상의 타프 경계·내부 제외 화소·기준/검증 역할을 확인하고 승인 상태를 저장하세요.", "02 원영상 후보 / 03 타프 ROI·승인"),
            (("coefficient", "계수"), "05에서 계수를 먼저 준비하고 품질 보고서를 검토한 뒤 반사도 생성을 승인하세요.", "05 ELC 계수·반사도"),
            (("wavelength", "파장", "band", "밴드"), "native MX Dual 10밴드와 실제 파장 메타데이터를 확인하세요. 파일 suffix만 보고 파장을 대신 정하지 않습니다.", "01 자료 연결"),
            (("yyyy-mm-dd", "관측 날짜", "현장 이름", "촬영 회차", "저장 폴더"), "표시된 항목의 입력 예시와 전체 저장 경로를 확인하세요. 이미 만든 작업은 기존 프로젝트 열기로 여세요.", "프로젝트 설정"),
            (("filenotfound", "file not found", "no such file", "없습니다", "누락"), "표시된 경로의 파일이 실제로 있는지와 드라이브 연결을 확인하세요. 다른 날짜·회차 자료로 임의 대체하지 않습니다.", "해당 입력 경로"),
        )
        for words, suggestion, location in rules:
            if any(word in lower for word in words):
                action, step = suggestion, step or location
                break
    if not action:
        action = "이 오류만으로 원인을 단정할 수 없습니다. 아래 실제 오류와 실패 로그를 확인하세요. 자동 재시도나 입력 대체는 하지 않습니다."
    lines = ["확인된 내용", raw, "", "다음 조치", action]
    if step:
        lines += ["", "확인할 위치", step]
    if log_path:
        lines += ["", "원문 실패 로그", str(log_path)]
    return "\n".join(lines)


def primary_error(detail):
    lines = [line.strip() for line in str(detail).splitlines() if line.strip()]
    specific = [line for line in lines if re.match(r"(?:[\w.]+(?:Error|Exception)|오류|FAILED|실패)\s*[:：]", line)]
    useful = [line for line in specific if not line.startswith("subprocess.CalledProcessError:")]
    return (useful or specific)[-1] if useful or specific else "\n".join(lines[-6:]) or "오류 세부 내용이 제공되지 않았습니다."


class ActionHint:
    """A disabled ttk button still explains its live reason on hover or click."""
    def __init__(self, widget, help_text, reason=lambda: "", *, guard=False, popup=None):
        self.widget, self.help_text, self.reason = widget, help_text, reason
        self.popup = popup or (lambda detail: messagebox.showwarning("진행할 수 없는 이유", problem_text(detail), parent=widget.winfo_toplevel()))
        self.timer = self.window = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<FocusIn>", self.schedule, add="+")
        for event in ("<Leave>", "<FocusOut>", "<Destroy>"):
            widget.bind(event, self.hide, add="+")
        widget.bind("<F1>", self.explain, add="+")
        widget.bind("<Button-1>", self.disabled_click, add="+")
        if guard:
            original = str(widget.cget("command"))
            def invoke():
                self.hide()
                if blocked := self.reason():
                    self.popup(blocked)
                elif original:
                    widget.tk.call(original)
            widget.configure(command=invoke)
        widget._action_hint = self

    def text(self):
        reason = self.reason()
        return ("진행 불가\n" + reason) if reason else self.help_text

    def schedule(self, _event=None):
        self.hide()
        self.timer = self.widget.after(450, self.show)

    def show(self):
        self.timer = None
        if not self.widget.winfo_exists() or not self.widget.winfo_ismapped():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        ttk.Label(self.window, text=self.text(), wraplength=430, justify="left", padding=12,
                  background="#202327", foreground="#edf0f2", borderwidth=1, relief="solid").pack()
        self.window.update_idletasks()
        x = min(self.widget.winfo_rootx(), self.widget.winfo_screenwidth() - self.window.winfo_reqwidth() - 12)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        if y + self.window.winfo_reqheight() > self.widget.winfo_screenheight() - 12:
            y = self.widget.winfo_rooty() - self.window.winfo_reqheight() - 8
        self.window.geometry(f"+{max(0, x)}+{max(0, y)}")

    def hide(self, _event=None):
        if self.timer:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window:
            self.window.destroy()
            self.window = None

    def explain(self, _event=None):
        self.hide()
        self.popup(self.reason() or self.help_text)
        return "break"

    def disabled_click(self, _event=None):
        self.hide()
        if isinstance(self.widget, ttk.Button) and self.widget.instate(("disabled",)):
            return self.explain()
