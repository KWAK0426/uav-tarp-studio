"""Feedback is explanatory only: no native input is opened or job started."""
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

import distribution

distribution.configure()
from legacy_studio import apply_studio_theme
from uav_pipeline.ui_feedback import ActionHint, primary_error, problem_text


class FeedbackTests(unittest.TestCase):
    def test_real_cause_is_not_replaced_by_subprocess_wrapper(self):
        log = "Traceback (most recent call last):\nValueError: 실측 Ed 매칭 시간차 초과\nsubprocess.CalledProcessError: Command returned code 1"
        error = primary_error(log)
        self.assertIn("실측 Ed 매칭 시간차 초과", error)
        self.assertNotIn("CalledProcessError", error)
        message = problem_text(error, log_path="failure.log")
        for value in ("확인된 내용", "다음 조치", "04 방사·센서·Ed", "failure.log", "임의 보간"):
            self.assertIn(value, message)

    def test_unknown_cause_stays_unknown(self):
        message = problem_text("Unexpected code ZXQ-17")
        self.assertIn("ZXQ-17", message)
        self.assertIn("원인을 단정할 수 없습니다", message)
        self.assertIn("자동 재시도", message)

    def test_input_and_disk_guidance_remain_separate(self):
        self.assertIn("01 자료 연결", problem_text("Target CSV requires one shot column"))
        self.assertIn("결과 저장 드라이브", problem_text("OSError: [Errno 28] No space left"))
        self.assertIn("원본을 자동 대체하지 않습니다", problem_text("Raw directory does not exist"))


class HintTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        apply_studio_theme(self.root)
        self.calls, self.popups = [], []
        self.reason = "처리 대상 CSV가 없습니다. 01에서 대상 목록을 등록하세요."
        self.button = ttk.Button(self.root, text="검사", command=lambda: self.calls.append("run"), state="disabled")
        self.button.pack(padx=20, pady=20)
        self.hint = ActionHint(self.button, "입력 연결 확인", lambda: self.reason, guard=True, popup=self.popups.append)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_disabled_hover_and_click_explain_without_running(self):
        self.assertIn("처리 대상 CSV", self.hint.text())
        self.hint.show()
        self.root.update()
        self.assertIsNotNone(self.hint.window)
        label = self.hint.window.winfo_children()[0]
        self.assertIn("진행 불가", label.cget("text"))
        self.button.event_generate("<Button-1>")
        self.root.update()
        self.assertEqual(self.popups, [self.reason])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.hint.window)

    def test_enabled_guard_updates_live_and_noop_is_never_executed(self):
        self.button.state(("!disabled",))
        self.button.invoke()
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.popups), 1)
        self.reason = ""
        self.assertEqual(self.hint.text(), "입력 연결 확인")
        self.button.invoke()
        self.assertEqual(self.calls, ["run"])

    def test_timer_cleanup_and_keyboard_help(self):
        self.hint.schedule()
        timer = self.hint.timer
        self.assertIsNotNone(timer)
        self.hint.hide()
        self.assertNotIn(timer, self.root.tk.call("after", "info"))
        self.hint.explain()
        self.assertEqual(self.popups, [self.reason])


if __name__ == "__main__":
    unittest.main(verbosity=2)
