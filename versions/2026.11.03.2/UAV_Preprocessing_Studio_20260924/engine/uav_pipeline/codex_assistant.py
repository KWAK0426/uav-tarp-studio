"""Isolated, subscription-only Codex app-server transport."""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from .assistant_session import SessionLease

HOME = Path(os.environ.get("ELC_STUDIO_STATE", str(Path.home() / "ELCStudioShare"))) / "codex_plus"


def find_codex():
    app_root = os.environ.get("UAV_STUDIO_ROOT")
    if app_root:
        try:
            record = json.loads((Path(app_root)/"settings/codex.json").read_text(encoding="utf-8"))
            candidate = Path(record["executable"]).resolve()
            candidate.relative_to((Path(app_root)/"tools/codex").resolve())
            if candidate.is_file():
                return str(candidate)
        except (OSError, ValueError, KeyError):
            pass
    executable = shutil.which("codex.exe") or shutil.which("codex")
    if executable:
        return executable
    # Explorer-launched apps do not inherit the Codex terminal's PATH.
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    candidates = list(root.glob("*/codex.exe"))
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    candidates += [root / "codex.exe"]
    return next((str(p) for p in candidates if p.is_file()), None)


def isolated_environment(home):
    # Never inherit desktop tokens, API keys, or another Codex session's routing.
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("CODEX", "OPENAI", "CHATGPT"))}
    env["CODEX_HOME"] = str(home)
    return env


def require_plus(account):
    if not account or account.get("type") != "chatgpt":
        raise RuntimeError("앱 전용 ChatGPT 로그인이 필요합니다. 이용 가능 모델과 한도는 계정에 따릅니다.")


def economical_model(models):
    visible = [m for m in models if not m.get("hidden")]
    default = next((m.get("model") for m in visible if m.get("isDefault")), None)
    return default or next((m.get("model") for m in visible), None)


def lowest_effort(models, model):
    entry = next((m for m in models if m.get("model") == model), {})
    supported = {x.get("reasoningEffort") for x in entry.get("supportedReasoningEfforts", [])}
    return next((e for e in ("none", "minimal", "low", "medium", "high") if e in supported),
                entry.get("defaultReasoningEffort", "low"))


class CodexAssistant:
    def __init__(self, home=HOME):
        self.home = Path(home).resolve()
        self.process = None
        self.events = queue.Queue()
        self.lock = threading.RLock()
        self.serial = 0
        self.approved_email = None
        self.model_catalog = []
        self.last_usage_details = {}
        self.lease = SessionLease(self.home)
        self.login_pending = False
        self.idle_logged_out = False
        atexit.register(self.close)

    def start(self):
        if self.process and self.process.poll() is None:
            return
        executable = find_codex()
        if not executable:
            raise RuntimeError("Codex 실행 파일을 찾지 못했습니다. Codex 설치를 확인하세요.")
        self.home.mkdir(parents=True, exist_ok=True)
        work = self.home / "empty_workspace"
        work.mkdir(exist_ok=True)
        options = {
            "forced_login_method": "chatgpt", "cli_auth_credentials_store": "keyring",
            "web_search": "disabled", "features.shell_tool": False,
            "features.unified_exec": False, "features.apply_patch_freeform": False,
            "features.multi_agent": False, "features.memories": False,
            "features.code_mode": False, "apps._default.enabled": False,
            "tools.view_image": False, "project_doc_max_bytes": 0,
        }
        command = [executable, "app-server", "--listen", "stdio://"]
        for key, value in options.items():
            command += ["-c", key + "=" + json.dumps(value)]
        self.events = queue.Queue()
        self.process = subprocess.Popen(command, cwd=work, env=isolated_environment(self.home),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process, events = self.process, self.events
        def read():
            try:
                for line in process.stdout:
                    try:
                        events.put(json.loads(line))
                    except ValueError:
                        continue
            except (OSError, ValueError):
                pass
            finally:
                events.put({"_closed": True})
        threading.Thread(target=read, daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": "uav_reflectance_studio", "version": "0.2.0"}})
        self.write({"method": "initialized", "params": {}})

    def write(self, data):
        self.process.stdin.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def next_event(self, timeout):
        try:
            event = self.events.get(timeout=max(.01, timeout))
        except queue.Empty:
            raise RuntimeError("Codex 응답 시간이 초과되었습니다. 자동 재시도하지 않습니다.") from None
        if event.get("_closed"):
            raise RuntimeError("앱 전용 Codex 연결이 종료되었습니다. 다시 로그인하세요.")
        if "method" in event and "id" in event:
            # This assistant proposes operations; it never grants model-side tools.
            self.write({"id": event["id"], "error": {"code": -32601, "message": "Tools disabled in this client"}})
        return event

    def call(self, method, params=None, timeout=30):
        with self.lock:
            self.serial += 1
            request_id = self.serial
            self.write({"id": request_id, "method": method, "params": params or {}})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                event = self.next_event(deadline - time.monotonic())
                if event.get("id") == request_id and "method" not in event:
                    if "error" in event:
                        raise RuntimeError("Codex " + method + " 실패: " + str(event["error"].get("message", ""))[:500])
                    return event.get("result", {})
            raise RuntimeError("Codex 요청 시간 초과")

    def account(self):
        if not self.login_pending and self.lease.expired():
            self.logout()
            raise RuntimeError("6시간 미사용으로 앱 로그인이 만료되었습니다. 다시 로그인하세요.")
        self.start()
        return self.call("account/read", {"refreshToken": False}).get("account")

    def approve(self, account):
        require_plus(account)
        current = self.account()
        require_plus(current)
        if current.get("email") != account.get("email"):
            raise RuntimeError("계정이 변경되었습니다. 다시 확인하세요.")
        self.lease.begin(account["email"])
        self.approved_email = account["email"]
        self.idle_logged_out = False

    def restore(self):
        with self.lock:
            if self.lease.data is None:
                return None
            account = self.account()
            if account is None:
                self.approved_email = None
                return None
            require_plus(account)
            if account.get("email") == self.lease.data.get("approved_email"):
                self.approved_email = account["email"]
            self.models()
            return account

    def activity(self):
        return self.lease.touch()

    def expire_if_idle(self):
        if self.login_pending or self.idle_logged_out or not self.lease.expired():
            return False
        with self.lock:
            if self.login_pending or not self.lease.expired():
                return False
            self.logout()
            self.idle_logged_out = True
            return True

    def logout(self):
        with self.lock:
            self.approved_email = None
            self.lease.invalidate()
            try:
                self.start()
                self.call("account/logout")
            finally:
                self.close()

    def login(self, on_url, cancel):
        self.idle_logged_out = False
        self.login_pending = True
        try:
            return self._login(on_url, cancel)
        finally:
            self.login_pending = False

    def _login(self, on_url, cancel):
        with self.lock:
            self.start()
            self.approved_email = None
            self.lease.invalidate()
            self.call("account/logout")
            result = self.call("account/login/start", {"type": "chatgpt"})
            login_id = result["loginId"]
            on_url(result["authUrl"])
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline and not cancel.is_set():
                try:
                    event = self.next_event(1)
                except RuntimeError:
                    if self.process is None or self.process.poll() is not None:
                        raise
                    continue
                params = event.get("params", {})
                if event.get("method") == "account/login/completed" and params.get("loginId") == login_id:
                    if not params.get("success"):
                        raise RuntimeError("ChatGPT 로그인에 실패했습니다. 다시 시도하세요.")
                    # A successful callback can precede the auth cache update.
                    # Do not leave the old expired lease armed during verification.
                    self.lease.begin()
                    return self.verify_login_result()
            self.call("account/login/cancel", {"loginId": login_id})
            raise RuntimeError("로그인이 취소되었거나 5분이 지났습니다.")

    def verify_login_result(self):
        with self.lock:
            self.start()
            for attempt in range(3):
                account = self.call("account/read", {"refreshToken": attempt == 1}).get("account")
                if account is not None:
                    require_plus(account)
                    return account
                if attempt == 0:
                    time.sleep(.5)
                elif attempt == 1:
                    # Reload persisted auth, without starting another browser flow.
                    self.close()
                    self.start()
            raise RuntimeError("브라우저 로그인 완료 후에도 저장된 계정을 읽지 못했습니다. "
                               "로그인 결과 다시 확인을 눌러주세요. 계속 실패하면 Windows 인증 저장 단계 점검이 필요합니다.")

    def models(self):
        self.model_catalog = self.call("model/list", {"includeHidden": False}).get("data", [])
        return self.model_catalog

    def connection_status(self):
        with self.lock:
            account = self.account()
            require_plus(account)
            approved = bool(self.approved_email and self.approved_email == account.get("email"))
            return ("ChatGPT 인증 확인됨" if approved else "ChatGPT 로그인됨 · 연결 설정에서 이메일 승인 필요") + \
                "\n모델 호출 0회 · 토큰 0개 (새 답변 생성 테스트는 아님)"

    def reply(self, model, messages, length_target):
        with self.lock:
            self.last_usage_details = {}
            account = self.account()
            require_plus(account)
            if not self.approved_email or self.approved_email != account.get("email"):
                raise RuntimeError("연결 설정에서 ChatGPT 계정 이메일을 확인하고 승인하세요.")
            params = {"cwd": str(self.home / "empty_workspace"), "ephemeral": True,
                      "sandbox": "read-only", "approvalPolicy": "never",
                      "baseInstructions": messages[0]["content"] +
                          "\nDo not call tools. Follow the requested response format. " +
                          f"Keep the answer short, aiming below {length_target} output tokens.",
                      "config": {"model_reasoning_effort": lowest_effort(self.model_catalog, model)}}
            if model:
                params["model"] = model
            thread = self.call("thread/start", params)["thread"]["id"]
            try:
                self.call("turn/start", {"threadId": thread, "input": [{"type": "text",
                    "text": json.dumps(messages[1:], ensure_ascii=False, separators=(",", ":"))}]})
                deadline = time.monotonic() + 120
                final, usage = [], (None, None)
                while time.monotonic() < deadline:
                    event = self.next_event(deadline - time.monotonic())
                    p = event.get("params", {})
                    if p.get("threadId") != thread:
                        continue
                    if event.get("method") == "item/completed":
                        item = p.get("item", {})
                        if item.get("type") == "agentMessage":
                            final.append(item.get("text", ""))
                    elif event.get("method") == "thread/tokenUsage/updated":
                        counts = p.get("tokenUsage", {}).get("last", {})
                        usage = (counts.get("inputTokens"), counts.get("outputTokens"))
                        self.last_usage_details = {k: counts.get(k) for k in
                            ("inputTokens", "outputTokens", "cachedInputTokens", "reasoningOutputTokens")}
                    elif event.get("method") == "turn/completed":
                        if p.get("turn", {}).get("status") != "completed":
                            raise RuntimeError("Codex 응답 실패 또는 한도 도달. API로 전환하지 않습니다.")
                        if not final:
                            raise RuntimeError("Codex 텍스트 응답이 없습니다.")
                        return "\n".join(final), usage
                raise RuntimeError("Codex 120초 시간 초과")
            finally:
                try:
                    self.call("thread/unsubscribe", {"threadId": thread}, timeout=5)
                except Exception:
                    self.close()

    def close(self):
        try:
            self.lease.flush()
        except OSError:
            pass
        process = self.process
        self.process = None
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
