"""Windows app identity, icon and one desktop session per signed-in user."""
import ctypes
import os
from pathlib import Path

APP_ID = 'PNU.UAVTarpStudio.Desktop'
ICON = Path(__file__).resolve().parent / 'assets' / 'studio.ico'
_mutex = None


def set_identity():
    if os.name == 'nt':
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def apply(root):
    set_identity()
    root.iconbitmap(default=str(ICON))
    root.iconbitmap(str(ICON))


def acquire_instance():
    global _mutex
    set_identity()
    if os.name != 'nt':
        return True
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel.CreateMutexW(None, False, 'Local\\' + APP_ID)
    if not _mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() != 183:
        return True
    user = ctypes.windll.user32
    user.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def focus(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(hwnd, title, len(title))
        if title.value.startswith('UAV Tarp Studio |'):
            user.ShowWindow(hwnd, 9)
            user.SetForegroundWindow(hwnd)
            return False
        return True

    user.EnumWindows(focus, 0)
    return False
