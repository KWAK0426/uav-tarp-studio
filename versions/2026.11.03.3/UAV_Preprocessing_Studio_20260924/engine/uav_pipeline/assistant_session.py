"""Non-secret activity lease; OAuth credentials stay in the OS keyring."""
import json
import math
import os
import threading
import time
from pathlib import Path

IDLE_SECONDS = 6 * 60 * 60


class SessionLease:
    def __init__(self, home, clock=time.time):
        self.path = Path(home) / "session_activity.json"
        self.clock = clock
        self.lock = threading.RLock()
        self.data = None
        self.last_flush = 0
        self.dirty = False
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                stamp = float(data["last_activity"])
                if not math.isfinite(stamp) or stamp > clock() + 60:
                    raise ValueError("Invalid clock")
                self.data = {"last_activity": stamp, "approved_email": data.get("approved_email")}
            except (ValueError, OSError, KeyError, TypeError):
                self.data = {"last_activity": 0, "approved_email": None}

    def expired(self):
        with self.lock:
            return self.data is not None and self.clock() - self.data["last_activity"] >= IDLE_SECONDS

    def begin(self, email=None):
        with self.lock:
            self.data = {"last_activity": self.clock(), "approved_email": email}
            self.dirty = True
            self.flush()

    def touch(self):
        with self.lock:
            if self.data is None or self.expired():
                return False
            self.data["last_activity"] = self.clock()
            self.dirty = True
            if self.clock() - self.last_flush >= 15:
                self.flush()
            return True

    def invalidate(self):
        with self.lock:
            self.data = {"last_activity": 0, "approved_email": None}
            self.dirty = True
            self.flush()

    def flush(self):
        with self.lock:
            if self.data is None or not self.dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(f".{os.getpid()}.tmp")
            temp.write_text(json.dumps(self.data), encoding="utf-8")
            temp.replace(self.path)
            self.last_flush = self.clock()
            self.dirty = False
