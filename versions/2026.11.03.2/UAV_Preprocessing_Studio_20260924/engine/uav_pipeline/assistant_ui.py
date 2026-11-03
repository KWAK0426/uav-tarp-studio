from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from . import assistant as ai
from .codex_assistant import CodexAssistant, economical_model


def dark_text(parent, **options):
    return tk.Text(parent, background="#131518", foreground="#edf0f2", insertbackground="#edf0f2",
                   selectbackground="#31554e", relief="flat", padx=10, pady=10,
                   font=("Malgun Gothic", 10), wrap="word", **options)


class AssistantPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=(16, 12), width=380)
        self.app = app
        self.prefs = ai.load_preferences()
        self.provider = tk.StringVar(value="ChatGPT (Codex)")
        self.codex = CodexAssistant()
        self.login_cancel = threading.Event()
        self.account_window = None
        self.plus_account = None
        self.idle_check_pending = False
        self.bind("<Destroy>", lambda e: self.codex.close() if e.widget is self else None)
        self.events = queue.Queue()
        self.history, self.candidates = [], []
        self.mode = tk.StringVar(value="work")
        self.confirm_send = tk.BooleanVar(value=self.prefs.get("confirm_send", True))
        self.busy = False
        self.cancelled = False
        self.session_input = self.session_output = 0
        self.attach_status = tk.BooleanVar(value=True)
        self.attach_paths = tk.BooleanVar(value=False)
        self.attach_logs = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="ChatGPT (Codex) 로그인 필요 · API 사용 안 함")
        head = ttk.Frame(self)
        head.pack(fill="x")
        ttk.Label(head, text="AI 도우미", font=("Malgun Gothic", 15, "bold")).pack(side="left")
        ttk.Button(head, text="연결 설정", command=self.settings).pack(side="right")
        ttk.Label(self, textvariable=self.status, style="Subtitle.TLabel", wraplength=340).pack(fill="x", pady=(8, 12))
        local = ttk.Frame(self)
        local.pack(fill="x")
        ttk.Button(local, text="폴더 찾기", command=self.scan).pack(side="left")
        ttk.Button(local, text="현재 상태", command=self.local_status).pack(side="left", padx=4)
        ttk.Button(local, text="앱 스킬", command=self.show_skill).pack(side="left")
        modes = ttk.Frame(self)
        modes.pack(fill="x", pady=(8, 0))
        ttk.Radiobutton(modes, text="가벼운 대화", variable=self.mode, value="chat", command=self.change_mode).pack(side="left")
        ttk.Radiobutton(modes, text="작업 상태·로그", variable=self.mode, value="work", command=self.change_mode).pack(side="left", padx=6)
        ttk.Button(modes, text="연결 확인", command=self.check_connection).pack(side="right")
        self.transcript = dark_text(self, height=12, state="disabled")
        self.transcript.pack(fill="both", expand=True, pady=12)
        self.transcript.tag_configure("user", foreground="#75d4bc")
        self.transcript.tag_configure("local", foreground="#edcc82")
        self.proposal_box = ttk.Frame(self)
        self.proposal_box.pack(fill="x")
        self.attach_controls = []
        for label, variable in [("현재 단계·상태 첨부", self.attach_status),
                                ("설정 경로·검색 후보 첨부", self.attach_paths),
                                ("최근 실행 로그 자동 첨부 (최대 2,500자)", self.attach_logs)]:
            check = ttk.Checkbutton(self, text=label, variable=variable,
                                    state="normal" if self.mode.get() == "work" else "disabled")
            check.pack(anchor="w", pady=2)
            self.attach_controls.append(check)
        delivery = ttk.Frame(self)
        delivery.pack(fill="x", pady=(6, 0))
        ttk.Radiobutton(delivery, text="매번 확인", variable=self.confirm_send, value=True,
                        command=self.save_send_mode).pack(side="left")
        ttk.Radiobutton(delivery, text="바로 전송", variable=self.confirm_send, value=False,
                        command=self.save_send_mode).pack(side="left", padx=10)
        self.input = dark_text(self, height=3)
        self.input.pack(fill="x", pady=(10, 8))
        self.input.bind("<Return>", self.on_enter)
        self.input.bind("<Control-Return>", self.on_enter)
        self.input.bind("<Shift-Return>", self.on_newline)
        buttons = ttk.Frame(self)
        buttons.pack(fill="x")
        self.send_button = ttk.Button(buttons, text="보내기", style="Primary.TButton", command=self.send)
        self.send_button.pack(side="right")
        self.cancel_button = ttk.Button(buttons, text="수신 중단", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=4)
        ttk.Button(buttons, text="대화 비우기", command=self.clear).pack(side="left")
        self.append("로컬", "기본값은 작업 상태·로그 + 간결 응답입니다. 진행 상태를 첨부하며 로그·경로는 선택한 경우에만 전달합니다. ChatGPT 로그인은 앱을 6시간 사용하지 않으면 만료됩니다.", "local")
        self.after(120, self.poll)
        self.after(250, self.restore_plus)
        self.after(30000, self.check_idle)
        for sequence in ("<KeyPress>", "<ButtonPress>", "<MouseWheel>"):
            self.app.root.bind_all(sequence, self.record_activity, add="+")

    def record_activity(self, event=None):
        try:
            self.codex.activity()
        except OSError:
            self.status.set("로그인 유지 시간 저장 실패 · 연결 설정을 확인하세요")

    def restore_plus(self):
        if self.busy or self.codex.lease.data is None:
            return
        self.busy = True
        self.send_button.configure(state="disabled")
        self.status.set("저장된 ChatGPT 로그인 확인 중")
        def work():
            try:
                self.events.put(("restore_done", self.codex.restore()))
            except Exception as exc:
                self.events.put(("restore_error", ai.redact(str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def check_idle(self):
        if not self.busy and not self.idle_check_pending and self.codex.lease.expired() and not self.codex.idle_logged_out:
            self.idle_check_pending = True
            self.busy = True
            self.send_button.configure(state="disabled")
            def work():
                try:
                    self.events.put(("idle_done", self.codex.expire_if_idle()))
                except Exception as exc:
                    self.events.put(("idle_error", ai.redact(str(exc))))
            threading.Thread(target=work, daemon=True).start()
        self.after(30000, self.check_idle)

    def append(self, speaker, text, tag=""):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"{speaker}\n{text}\n\n", tag)
        if int(self.transcript.index("end-1c").split(".")[0]) > 700:
            self.transcript.delete("1.0", "250.0")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def on_enter(self, event=None):
        self.send()
        return "break"

    def on_newline(self, event=None):
        self.input.insert("insert", "\n")
        return "break"

    def save_send_mode(self):
        try:
            ai.save_preferences(self.provider.get(), self.prefs["models"], self.prefs["max_output"],
                                confirm_send=self.confirm_send.get())
            self.prefs = ai.load_preferences()
        except Exception:
            self.confirm_send.set(self.prefs.get("confirm_send", True))
            messagebox.showerror("설정 저장 실패", "전송 확인 설정을 저장하지 못했습니다.", parent=self.app.root)

    def clear(self):
        if self.busy:
            return
        self.history.clear()
        self.clear_proposals()
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    def clear_proposals(self):
        for widget in self.proposal_box.winfo_children():
            widget.destroy()

    def dialog(self, title, content, approve=False):
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(background="#17191c")
        win.geometry("760x560")
        win.transient(self.app.root)
        accepted = [False]
        box = dark_text(win)
        box.pack(fill="both", expand=True, padx=16, pady=16)
        box.insert("1.0", content)
        box.configure(state="disabled")
        row = ttk.Frame(win, padding=12)
        row.pack(fill="x")
        ttk.Button(row, text="닫기", command=win.destroy).pack(side="right")
        if approve:
            def accept():
                accepted[0] = True
                win.destroy()
            ttk.Button(row, text="확인 후 전송", style="Primary.TButton", command=accept).pack(side="right", padx=8)
        win.grab_set()
        self.wait_window(win)
        return accepted[0]

    def show_skill(self):
        self.dialog("AI 앱 스킬", ai.CHAT_GUIDE if self.mode.get() == "chat" else ai.WORK_GUIDE)

    def change_mode(self):
        self.history.clear()
        self.clear_proposals()
        for control in self.attach_controls:
            control.configure(state="normal" if self.mode.get() == "work" else "disabled")
        self.status.set("일반 대화 · 작업 정보 미첨부" if self.mode.get() == "chat" else "작업 도움 · 선택한 정보만 첨부")

    def check_connection(self):
        if self.busy:
            return
        self.busy, self.cancelled = True, False
        self.send_button.configure(state="disabled")
        self.status.set("ChatGPT 인증 확인 중 · 모델 호출 없음")
        def work():
            try:
                self.events.put(("connection", self.codex.connection_status()))
            except Exception as exc:
                self.events.put(("connection", ai.redact(str(exc)) + "\n모델 호출 0회 · 토큰 0개"))
        threading.Thread(target=work, daemon=True).start()

    def settings(self):
        if self.busy:
            return
        if self.account_window is not None and self.account_window.winfo_exists():
            self.account_window.lift()
            return
        win = tk.Toplevel(self)
        self.account_window = win
        win.title("ChatGPT (Codex) 연결")
        win.configure(background="#17191c")
        x = max(20, self.app.root.winfo_x() + 120)
        y = max(40, self.app.root.winfo_y() + 100)
        win.geometry(f"650x580+{x}+{y}")
        win.transient(self.app.root)
        body = ttk.Frame(win, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="ChatGPT (Codex)", font=("Malgun Gothic", 18, "bold")).pack(anchor="w")
        ttk.Label(body, text="앱 전용 로그인 · 기존 Pro 계정 유지 · API 결제 없음",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(6, 18))
        self.account_info = tk.StringVar(value="로그인 후 이메일과 Plus 요금제를 확인하세요.")
        ttk.Label(body, textvariable=self.account_info, wraplength=590).pack(anchor="w", pady=8)
        self.login_url = tk.StringVar()
        ttk.Entry(body, textvariable=self.login_url, state="readonly").pack(fill="x", pady=8)
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Button(row, text="ChatGPT 계정 로그인", command=self.start_plus_login).pack(side="left")
        def open_login():
            import webbrowser
            url = self.login_url.get()
            if url.startswith("https://auth.openai.com/") or url.startswith("https://chatgpt.com/"):
                webbrowser.open(url)
        ttk.Button(row, text="로그인 페이지 열기", command=open_login).pack(side="left", padx=6)
        def copy_url():
            win.clipboard_clear()
            win.clipboard_append(self.login_url.get())
        ttk.Button(row, text="주소 복사", command=copy_url).pack(side="left")
        ttk.Button(body, text="로그인 결과 다시 확인", command=self.recheck_login).pack(anchor="w", pady=(6, 0))
        ttk.Label(body, text="브라우저가 Pro로 열리면 주소를 시크릿 창에 붙여 넣고 Plus로 로그인하세요.\n"
                  "Windows 보안 저장소에 로그인 유지 · 앱 6시간 미사용 시 만료\n앱이 꺼져 있는 동안 만료되면 다음 실행 시 로그인 정보를 해제합니다.",
                  wraplength=590, style="Subtitle.TLabel").pack(anchor="w", pady=14)
        ttk.Label(body, text="모델 (로그인 후 계정에서 조회)").pack(anchor="w")
        self.plus_model = tk.StringVar(value=self.prefs["models"].get("ChatGPT (Codex)", ""))
        self.plus_models = ttk.Combobox(body, textvariable=self.plus_model, state="readonly")
        self.plus_models.pack(fill="x", pady=6)
        def use_economy():
            model = economical_model(self.codex.model_catalog)
            if model:
                self.plus_model.set(model)
            else:
                messagebox.showinfo("모델 목록", "로그인 후 Luna가 목록에 있는지 확인하세요. 자동 대체하지 않습니다.", parent=win)
        ttk.Button(body, text="절약 모델 선택 · Luna", command=use_economy).pack(anchor="w")
        if self.codex.approved_email:
            self.account_info.set(self.codex.approved_email + " · plus (승인된 계정)")
            self.plus_models.configure(values=[m["model"] for m in self.codex.model_catalog if not m.get("hidden")])
        ttk.Label(body, text="최저 지원 추론 강도 · 짧은 응답 · 120초 제한",
                  style="Subtitle.TLabel").pack(anchor="w", pady=6)
        bottom = ttk.Frame(body)
        bottom.pack(side="bottom", fill="x")
        def disconnect():
            self.login_cancel.set()
            self.busy = True
            self.send_button.configure(state="disabled")
            def work():
                try:
                    self.codex.logout()
                    self.codex.idle_logged_out = True
                    self.events.put(("idle_done", True))
                except Exception as exc:
                    self.events.put(("idle_error", ai.redact(str(exc))))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(bottom, text="앱 연결 해제", command=disconnect).pack(side="left")
        def confirm():
            account = getattr(self, "plus_account", None)
            from .codex_assistant import require_plus
            try:
                require_plus(account)
                if not self.plus_model.get():
                    raise ValueError("로그인 완료 후 모델을 선택하세요.")
                self.codex.approve(account)
                models = dict(self.prefs["models"])
                models["ChatGPT (Codex)"] = self.plus_model.get()
                ai.save_preferences("ChatGPT (Codex)", models, self.prefs["max_output"])
                self.prefs = ai.load_preferences()
                self.provider.set("ChatGPT (Codex)")
                self.history.clear()
                self.status.set("Plus · " + account["email"] + "\n계정 확인 완료 / 대화 미검증")
                win.destroy()
            except Exception as exc:
                messagebox.showerror("계정 확인", str(exc), parent=win)
        ttk.Button(bottom, text="이 ChatGPT 계정 사용", style="Primary.TButton", command=confirm).pack(side="right")
        def dismiss():
            self.login_cancel.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", dismiss)
        win.grab_set()

    def start_plus_login(self):
        if self.busy:
            return
        self.busy = True
        self.send_button.configure(state="disabled")
        self.login_cancel = threading.Event()
        self.plus_account = None
        self.account_info.set("로그인 주소 준비 중…")
        def work():
            try:
                self.codex.close()
                account = self.codex.login(lambda url: self.events.put(("login_url", url)), self.login_cancel)
                models = self.codex.models()
                self.events.put(("login_done", (account, models)))
            except Exception as exc:
                self.events.put(("login_error", ai.redact(str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def recheck_login(self):
        if self.busy:
            return
        self.busy = True
        self.send_button.configure(state="disabled")
        self.account_info.set("저장된 로그인 결과 확인 중 · 모델 호출 없음")
        def work():
            try:
                if self.codex.lease.expired():
                    raise RuntimeError("로그인 시도가 만료되었습니다. ChatGPT 계정 로그인을 다시 눌러주세요.")
                account = self.codex.verify_login_result()
                self.events.put(("login_done", (account, self.codex.models())))
            except Exception as exc:
                self.events.put(("login_error", ai.redact(str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def api_settings(self):
        if self.busy:
            return
        win = tk.Toplevel(self)
        win.title("AI 연결 설정")
        win.configure(background="#17191c")
        x = max(20, self.app.root.winfo_x() + (self.app.root.winfo_width() - 580) // 2)
        y = max(40, self.app.root.winfo_y() + 90)
        win.geometry(f"580x520+{x}+{y}")
        win.transient(self.app.root)
        body = ttk.Frame(win, padding=22)
        body.pack(fill="both", expand=True)
        provider = tk.StringVar(value=self.provider.get())
        model = tk.StringVar(value=self.prefs["models"].get(provider.get(), ""))
        key = tk.StringVar()
        limit = tk.StringVar(value=str(self.prefs["max_output"]))
        info = tk.StringVar()
        def change(*_):
            key.set("")
            model.set(self.prefs["models"].get(provider.get(), ""))
            try:
                info.set("저장된 키 있음 · 비워두면 유지" if ai.credential(provider.get()) else "저장된 키 없음")
            except Exception:
                info.set("Windows 자격 증명에 접근할 수 없습니다")
        for label in ["제공자"]:
            ttk.Label(body, text=label).pack(anchor="w")
        choice = ttk.Combobox(body, textvariable=provider, values=ai.PROVIDERS, state="readonly")
        choice.pack(fill="x", pady=(4, 12))
        choice.bind("<<ComboboxSelected>>", change)
        ttk.Label(body, text="모델 ID (해당 API 계정에서 사용 가능한 모델)").pack(anchor="w")
        ttk.Entry(body, textvariable=model).pack(fill="x", pady=(4, 12))
        ttk.Label(body, text="API 키 · Windows 자격 증명에 저장").pack(anchor="w")
        ttk.Entry(body, textvariable=key, show="*").pack(fill="x", pady=(4, 4))
        ttk.Label(body, textvariable=info, style="Subtitle.TLabel").pack(anchor="w")
        ttk.Label(body, text="응답 토큰 상한").pack(anchor="w", pady=(12, 4))
        ttk.Spinbox(body, from_=128, to=2048, increment=128, textvariable=limit).pack(fill="x")
        ttk.Label(body, text="채팅 구독 로그인과 별개인 API 키가 필요합니다.\n호출 비용은 선택한 제공자의 API 계정에 청구됩니다.",
                  wraplength=510, style="Subtitle.TLabel").pack(anchor="w", pady=12)
        row = ttk.Frame(body)
        row.pack(fill="x", side="bottom")
        def remove():
            if messagebox.askyesno("API 키 삭제", provider.get()+" 저장 키를 삭제할까요?", parent=win):
                try:
                    ai.credential(provider.get(), delete=True)
                    change()
                except Exception:
                    messagebox.showerror("삭제 실패", "Windows 자격 증명을 확인하세요.", parent=win)
        def save():
            try:
                if not model.get().strip():
                    raise ValueError("모델 ID를 입력하세요.")
                import re
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model.get().strip()):
                    raise ValueError("모델 ID 형식을 확인하세요.")
                if model.get().strip().lower().startswith(("key_", "key-", "sk-", "aiza")):
                    raise ValueError("모델 ID에 키 식별자가 들어갔습니다. API 키가 아닌 모델명을 입력하세요.")
                n = int(limit.get())
                if not 128 <= n <= 2048:
                    raise ValueError("응답 상한은 128~2048입니다.")
                if key.get().strip():
                    ai.credential(provider.get(), key.get().strip())
                elif not ai.credential(provider.get()):
                    raise ValueError("API 키를 입력하세요.")
                models = dict(self.prefs["models"])
                models[provider.get()] = model.get().strip()
                ai.save_preferences(provider.get(), models, n)
                self.prefs = ai.load_preferences()
                self.provider.set(provider.get())
                self.clear()
                self.status.set(provider.get()+" · 설정 저장됨 / 연결 미검증")
                key.set("")
                win.destroy()
            except ValueError as exc:
                messagebox.showerror("설정 확인", str(exc), parent=win)
            except Exception as exc:
                messagebox.showerror("저장 실패", f"키 또는 연결 설정을 저장하지 못했습니다.\n오류 종류: {type(exc).__name__}\n키 내용은 로그에 기록하지 않습니다.", parent=win)
        ttk.Button(row, text="키 삭제", command=remove).pack(side="left")
        ttk.Button(row, text="저장", style="Primary.TButton", command=save).pack(side="right")
        change()
        win.grab_set()

    def context(self):
        data = {}
        if self.attach_status.get():
            self.app.refresh_status()
            data["tab"] = self.app.tabs.tab(self.app.tabs.select(), "text")
            data["job_state"] = self.app.running_label.cget("text")
            data["progress_percent"] = round(float(self.app.progress["value"]), 2)
            data["progress_detail"] = self.app.progress_detail.cget("text")
            data["foreground_running"] = bool(self.app.process is not None or getattr(self.app, "launch_pending", False))
            data["background_running"] = bool(self.app.background_process is not None or getattr(self.app, "background_launch_pending", False))
            data["config_file"] = str(self.app.config_path) if self.attach_paths.get() else Path(self.app.config_path).name
            if hasattr(self.app, "pre_steps"):
                data["step"] = self.app.pre_steps.tab(self.app.pre_steps.select(), "text")
            data["required_inputs_present"] = {key: bool(self.app.vars[key].get().strip())
                for key in ai.PATHS if key in self.app.vars}
            data["status_rows"] = [self.app.status_tree.item(i, "values") for i in self.app.status_tree.get_children()]
        if self.attach_paths.get():
            data["paths"] = {k: v.get()[:350] for k, v in self.app.vars.items() if k in ai.PATHS and v.get()}
            data["candidates"] = self.candidates[:8]
        if self.attach_logs.get():
            excerpt = self.app.log.get("end-2500c", "end-1c").strip()
            data["log_excerpt"] = ai.redact(excerpt) if excerpt else "(현재 실행 로그 없음)"
        return data

    def local_status(self):
        self.app.refresh_status()
        rows = [self.app.status_tree.item(i, "values") for i in self.app.status_tree.get_children()]
        self.append("로컬 상태 · API 사용 없음", "\n".join(" · ".join(r) for r in rows), "local")

    def send(self):
        if self.busy:
            return
        question = self.input.get("1.0", "end-1c").strip()
        if not question:
            return
        if ai.connection_question(question):
            self.append("나", question, "user")
            self.input.delete("1.0", "end")
            self.check_connection()
            return
        try:
            provider = self.provider.get()
            model = self.prefs["models"].get(provider, "")
            plus = provider == "ChatGPT (Codex)"
            key = "" if plus else ai.credential(provider)
            if (plus and not self.codex.approved_email) or (not plus and (not key or not model)):
                self.settings()
                return
            mode = self.mode.get()
            context = self.context() if mode == "work" else {}
            messages = ai.messages_for(question, self.history, context, mode=mode)
            limit = min(int(self.prefs["max_output"]), 192 if mode == "chat" else 384)
            if not plus:
                ai.build_request(provider, model, key, messages, limit)
            preview = (f"외부 전송 대상: {provider} / {model}\n" +
                       f"모드: {'일반 대화' if mode == 'chat' else '작업 도움'} · 앱 전달 본문 {sum(len(m['content']) for m in messages):,}자\nCodex 기본 지침/도구 정보는 별도로 포함될 수 있습니다.\n" +
                       (f"계정: {self.codex.approved_email}\nPlus 구독 사용량 차감 · API 전환 없음\n응답 길이는 목표값이며 강제 토큰 상한이 아닙니다.\n" if plus else f"응답 상한: {limit} tokens\n") +
                       "아래 대화·규칙·선택 정보만 전송합니다. 이미지/파일 본문/API 키는 대화에 첨부하지 않습니다.\n\n"
                       + json.dumps(messages, ensure_ascii=False, indent=2))
            if self.confirm_send.get() and not self.dialog("전송 확인 · Plus 사용량" if plus else "전송 내용 확인 · API 비용 발생 가능", preview, approve=True):
                return
        except Exception as exc:
            messagebox.showerror("전송 준비 실패", ai.redact(str(exc)), parent=self.app.root)
            return
        self.busy, self.cancelled = True, False
        self.send_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.clear_proposals()
        self.status.set(provider+(" · 응답 대기 (최대 120초)" if plus else " · 응답 대기 (최대 60초)"))
        self.append("나", question, "user")
        self.input.delete("1.0", "end")
        snapshot = list(context.get("candidates", []))
        def work():
            try:
                text, usage = self.codex.reply(model, messages, limit) if plus else ai.request_reply(provider, model, key, messages, limit)
                details = dict(self.codex.last_usage_details) if plus else {}
                self.events.put(("reply", (question, text, usage, snapshot, provider, mode, details)))
            except Exception as exc:
                detail = ai.redact(str(exc))
                self.events.put(("error", detail.replace(key, "[REDACTED]") if key else detail))
        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        self.cancel_button.configure(state="disabled")
        if self.provider.get() == "ChatGPT (Codex)":
            self.codex.close()
            self.status.set("Plus 응답 중단 · 로그인은 보관됨 / 이미 사용한 한도는 복구되지 않음")
        else:
            self.status.set("응답 폐기 요청 · 이미 전송된 API 비용은 발생할 수 있음")

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind.startswith("restore_") or kind.startswith("idle_"):
                    self.busy = False
                    self.idle_check_pending = False
                    self.send_button.configure(state="normal")
                    if kind == "restore_done" and value:
                        self.plus_account = value
                        self.status.set("ChatGPT 로그인 복원됨 · " + value.get("email", "") if self.codex.approved_email
                                        else "ChatGPT 로그인됨 · 연결 설정에서 계정 승인 필요")
                    elif kind == "idle_done" and value:
                        self.plus_account = None
                        self.history.clear()
                        self.clear_proposals()
                        self.status.set("앱 Plus 로그아웃됨 · 다시 로그인하세요")
                        if self.account_window is not None and self.account_window.winfo_exists():
                            self.account_info.set("앱 Plus 로그아웃됨")
                    elif kind.endswith("error"):
                        self.status.set("로그인 확인 필요")
                        self.append("로그인", value, "local")
                    elif kind == "restore_done":
                        self.status.set("ChatGPT 로그인 필요")
                    continue
                if kind == "connection":
                    self.busy = False
                    self.send_button.configure(state="normal")
                    self.status.set("인증 확인 결과 · 모델 호출 0회")
                    self.append("로컬 연결 확인", value, "local")
                    continue
                if kind.startswith("login_"):
                    alive = self.account_window is not None and self.account_window.winfo_exists()
                    if kind == "login_url":
                        if alive:
                            self.login_url.set(value)
                            self.account_info.set("로그인 페이지를 열고 ChatGPT 계정으로 로그인하세요.")
                        continue
                    self.busy = False
                    self.send_button.configure(state="normal")
                    if kind == "login_error":
                        if alive:
                            self.account_info.set(value)
                        self.status.set("ChatGPT 로그인 미완료")
                    elif alive:
                        self.plus_account, models = value
                        self.account_info.set(self.plus_account.get("email", "") + " · " + self.plus_account.get("planType", ""))
                        choices = [m["model"] for m in models if not m.get("hidden")]
                        self.plus_models.configure(values=choices)
                        default = next((m["model"] for m in models if m.get("isDefault") and m["model"] in choices), choices[0] if choices else "")
                        if self.plus_model.get() not in choices:
                            self.plus_model.set(default)
                    continue
                if kind == "scan":
                    self.busy = False
                    self.send_button.configure(state="normal")
                    self.candidates, limited = value
                    self.status.set("로컬 검색 완료" + (" · 일부만 검색됨" if limited else ""))
                    self.show_candidates(limited)
                    continue
                self.busy = False
                self.send_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                if self.cancelled:
                    self.status.set("응답 폐기됨 · 공급자 사용량은 확인 필요")
                    continue
                if kind == "error":
                    self.status.set("요청 실패 · 자동 재시도 없음")
                    self.append("오류", value, "local")
                    continue
                question, text, usage, candidates, provider, mode, details = value
                reply, actions = ai.parse_proposals(text, candidates) if mode == "work" else (text, [])
                if self.mode.get() == mode:
                    self.history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": reply}])
                    self.history = self.history[-(2 if mode == "chat" else 4):]
                else:
                    actions = []
                self.append(provider, reply)
                a, b = usage
                if a is not None and b is not None:
                    self.session_input += a
                    self.session_output += b
                    cached, reasoning = details.get("cachedInputTokens"), details.get("reasoningOutputTokens")
                    cached_text = f"{cached:,}" if cached is not None else "미제공"
                    reasoning_text = f"{reasoning:,}" if reasoning is not None else "미제공"
                    self.status.set(f"입력 {a:,} (캐시 {cached_text}) · 출력 {b:,}\n출력 중 추론 {reasoning_text} · 합계 {a+b:,}")
                else:
                    self.status.set("응답 완료 · 사용량 미제공")
                for action in actions:
                    title = ai.PATHS[action["key"]]+" 경로 검토" if action["type"] == "set_path" else ai.STAGES[action["stage"]]+" 실행 검토"
                    ttk.Button(self.proposal_box, text=title, command=lambda a=action: self.apply(a)).pack(fill="x", pady=3)
        except queue.Empty:
            pass
        self.after(120, self.poll)

    def scan(self):
        if self.busy:
            return
        root = filedialog.askdirectory(title="검색할 날짜·회차 폴더", parent=self.app.root)
        if not root:
            return
        self.busy, self.cancelled = True, False
        self.send_button.configure(state="disabled")
        self.status.set("선택한 폴더를 로컬에서 검색 중 · API 사용 없음")
        def work():
            try:
                self.events.put(("scan", ai.scan_paths(root)))
            except Exception:
                self.events.put(("error", "폴더 검색 실패: 드라이브 접근 권한을 확인하세요."))
        threading.Thread(target=work, daemon=True).start()

    def show_candidates(self, limited):
        win = tk.Toplevel(self)
        win.title("경로 후보 · 이름 기반 제안 / 자료 내용 미검증")
        win.geometry("940x480")
        win.configure(background="#17191c")
        win.transient(self.app.root)
        table = ttk.Treeview(win, columns=("role", "path"), show="headings")
        table.heading("role", text="자료 역할")
        table.heading("path", text="경로")
        table.column("role", width=200, stretch=False)
        table.column("path", width=680)
        table.pack(fill="both", expand=True, padx=16, pady=16)
        for c in self.candidates:
            table.insert("", "end", iid=c["id"], values=(ai.PATHS[c["key"]], c["path"]))
        row = ttk.Frame(win, padding=12)
        row.pack(fill="x")
        ttk.Label(row, text=f"{len(self.candidates)}개 후보" + (" · 검색 제한 도달" if limited else "")).pack(side="left")
        def choose():
            ids = table.selection()
            if ids:
                c = next(c for c in self.candidates if c["id"] == ids[0])
                self.apply({"type": "set_path", "key": c["key"], "path": c["path"]})
        ttk.Button(row, text="선택 경로 적용", command=choose).pack(side="right")

    def apply(self, action):
        if (self.app.process is not None or getattr(self.app, "launch_pending", False)
                or (hasattr(self.app, "_busy") and self.app._busy())):
            messagebox.showwarning("실행 중", "현재 처리가 끝난 뒤 변경하세요.", parent=self.app.root)
            return
        if action["type"] == "set_path":
            key, path = action["key"], Path(action["path"])
            if key not in ai.PATHS or key not in self.app.vars:
                return
            if not (path.is_dir() if key in ai.DIRECTORIES else path.is_file()):
                messagebox.showerror("경로 확인", "대상 파일/폴더가 없거나 종류가 맞지 않습니다.", parent=self.app.root)
                return
            old = self.app.vars[key].get()
            if not messagebox.askyesno("경로 변경 승인", f"{ai.PATHS[key]}\n\n기존: {old or '(미설정)'}\n변경: {path}\n\n화면 설정에 적용할까요? 파일 저장은 상단 저장 버튼으로 합니다.", parent=self.app.root):
                return
            self.app.vars[key].set(str(path))
            self.append("로컬", ai.PATHS[key]+" 경로를 화면에 반영했습니다. 아직 저장 전입니다.", "local")
        elif action["type"] == "run_stage" and action.get("stage") in ai.STAGES:
            stage = action["stage"]
            output = self.app.vars["output.directory"].get().strip()
            if not output:
                messagebox.showerror("출력 폴더 필요", "결과 저장 폴더를 먼저 지정하세요.", parent=self.app.root)
                return
            # Registered workflow methods own the concrete processing/ROI approvals.
            self.app.run(stage)
