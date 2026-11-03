"""ROI editor mode owned by an existing Studio project and stage-02 selection."""
import copy
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from app import App

ENGINE = Path(__file__).resolve().parent.parent / "UAV_Preprocessing_Studio_20260924" / "engine"
sys.path.insert(0, str(ENGINE))
from uav_pipeline.roi_studio_bridge import Session


class ProjectApp(App):
    def __init__(self, root, config_path):
        self.session = Session(config_path)
        self.shot = tk.StringVar(root, value=self.session.reference)
        self.reference = tk.StringVar(root, value=self.session.reference)
        self.loaded_shot = None
        self.loading_shot = None
        self.review_window = None
        self.select_after_load = None
        super().__init__(root)
        self.root.title(f"UAV Studio · 03 ROI · {self.session.context['label']}")
        self.root.after(100, self.open)

    def open(self, path=None):
        if self.busy or not self.discard_pending():
            if self.loaded_shot or self.loading_shot:
                self.shot.set(self.loaded_shot or self.loading_shot)
            return
        self.session.assert_current()
        self.dirty = False
        self.loading_shot = self.shot.get()
        super().open(self.session.files[self.shot.get() + "_6"])

    def image_loaded(self):
        self.loaded_shot = self.loading_shot
        self.shot.set(self.loaded_shot)
        self.records = copy.deepcopy(self.session.images.get(self.loaded_shot, []))
        self.current_strokes = []
        self.pixel_reviewed.set(False)
        self.role.set("calibration" if self.loaded_shot == self.reference.get() else "validation")
        self.refresh_list()
        self.redraw()
        self.note(f"02 선택 영상 {self.loaded_shot} · native Band6 · ROI {len(self.records)}개 자동 복원")
        if self.select_after_load:
            name, self.select_after_load = self.select_after_load, None
            for index, record in enumerate(self.records):
                if record["tarp_id"] == name:
                    self.listbox.selection_set(index)
                    self.select_record()
                    self.focus_roi()
                    break

    def persist(self):
        self.session.images[self.loaded_shot] = copy.deepcopy(self.records)
        self.session.reference = self.reference.get()
        self.session.last_manifest = None
        self.dirty = False
        self.note(f"{self.loaded_shot} ROI {len(self.records)}개 프로젝트 자동 저장 · 전달 대기")
        self.refresh_review_queue()

    def track_candidates(self):
        if self.busy or not self.discard_pending():
            return
        if not messagebox.askyesno("후보 전체 추적", "기준 영상의 확정 ROI로 다른 선정 영상을 추적합니다.\n"
                                  "기존 확정 ROI는 유지하고 새 후보는 미승인으로 저장합니다. 계속할까요?", parent=self.root):
            return
        def done(results):
            self.session.accept_tracking(results)
            self.current = self.edit_index = None
            self.pending_changed = False
            self.records = copy.deepcopy(self.session.images.get(self.loaded_shot, []))
            self.refresh_list()
            self.redraw()
            self.note("추적 완료 · 검토 목록에서 경계·내부 화소를 확인하세요.")
            self.show_review_queue()
        self.job("기준 ROI → 후보 전체 추적", lambda: self.session.propose_tracking(self.progress_notes.put), done)

    def show_review_queue(self):
        if self.review_window and self.review_window.winfo_exists():
            self.review_window.lift()
            self.refresh_review_queue()
            return
        window = self.review_window = tk.Toplevel(self.root)
        window.title("ROI 검토 목록")
        window.geometry("780x430")
        window.rowconfigure(0, weight=1)
        window.columnconfigure(0, weight=1)
        self.review_tree = ttk.Treeview(window, columns=("shot", "tarp", "state", "reason"), show="headings", selectmode="browse")
        for key, text, width in (("shot", "영상", 110), ("tarp", "타프", 100), ("state", "상태", 100), ("reason", "확인 사항", 400)):
            self.review_tree.heading(key, text=text)
            self.review_tree.column(key, width=width)
        self.review_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(window, command=self.review_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.review_tree.configure(yscrollcommand=scroll.set)
        ttk.Button(window, text="선택 항목 확인", command=self.open_review_item).grid(row=1, column=0, sticky="e", padx=8, pady=8)
        self.review_tree.bind("<Double-1>", lambda e: self.open_review_item())
        self.refresh_review_queue()

    def refresh_review_queue(self):
        if not self.review_window or not self.review_window.winfo_exists():
            return
        self.review_tree.delete(*self.review_tree.get_children())
        expected = self.session.tracking.get("expected_tarps", [r["tarp_id"] for r in self.session.images.get(self.reference.get(), [])])
        for shot in self.session.shots:
            records = {r["tarp_id"]: r for r in self.session.images.get(shot, [])}
            for name in dict.fromkeys([*expected, *records]):
                record = records.get(name)
                diagnostics = self.session.tracking.get("results", {}).get(shot, {}).get(name, {})
                if record and record.get("reviewed") and record.get("pixel_reviewed"):
                    continue
                state = "직접 지정 필요" if record is None else "확인 대기"
                self.review_tree.insert("", "end", values=(shot, name, state, diagnostics.get("reason", "경계·내부 화소 확인")))

    def open_review_item(self):
        if self.busy or not self.review_tree.selection():
            return
        shot, name, *_ = self.review_tree.item(self.review_tree.selection()[0], "values")
        self.shot.set(shot)
        self.select_after_load = name
        self.open()

    def change_reference(self):
        if self.busy:
            self.reference.set(self.session.reference)
            return
        self.session.reference = self.reference.get()
        self.session.save_draft()
        self.session.last_manifest = None
        self.session.tracking = {}
        self.session.save_draft()
        self.note(f"ELC·Ed 기준: {self.reference.get()} · ROI 용도를 확인한 뒤 다시 전달하세요.")

    def confirm(self):
        previous = self.current
        super().confirm()
        if previous is not None and self.current is None:
            self.persist()

    def delete(self):
        before = len(self.records)
        super().delete()
        if len(self.records) != before:
            self.persist()

    def save(self, next_step=None):
        if self.busy:
            return
        if self.current is not None and self.edit_index is not None and not self.pending_changed:
            self.cancel_edit()
        if self.current is not None:
            messagebox.showinfo("ROI 확정", "현재 수정 중인 ROI를 먼저 확정하거나 수정 취소하세요.")
            return
        self.session.save_draft()
        def done(path):
            self.last_output = Path(path).parent
            self.dirty = False
            self.note("승인 ROI 전달 완료 · 04·05에서 같은 기준·native ROI 사용")
            if next_step in (3, 4):
                self.session.navigate(next_step)
                self.root.destroy()
        self.job("10밴드 native 정합 · 승인 ROI 전달 중",
                 lambda: self.session.publish(self.progress_notes.put), done)

    def restore(self):
        self.open()

    def navigate(self, step):
        if self.busy:
            return
        if self.pending_changed:
            messagebox.showinfo("ROI 수정 중", "현재 수정한 ROI를 확정하거나 수정 취소한 뒤 이동하세요.")
            return
        if not self.session.last_manifest:
            self.save(next_step=step)
            return
        try:
            self.session.navigate(step)
            self.root.destroy()
        except Exception as exc:
            messagebox.showwarning("단계 연결", str(exc))
