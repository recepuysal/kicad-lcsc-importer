"""Panodaki sembolu acik KiCad sematik editorune yapistirir: pencereyi one getirir
ve Ctrl+V gonderir. KiCad sembolu imlece yapisik (secili, tasinir) olarak getirir.
Fare kullanilmaz. Pencere one gelmezse hicbir tus gonderilmez."""
import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)

# KiCad arayuz dillerine gore sematik editoru pencere basliklari
TITLES = ("Şematik Düzenleyici", "Schematic Editor", "Schaltplaneditor", "Éditeur de Schématique",
          "Editor de esquemas", "Editor schematico", "Edytor schematów", "Schema-editor",
          "Редактор схем", "原理图编辑器")
KICAD_EXES = ("kicad.exe", "eeschema.exe")
VK_CONTROL, VK_MENU, VK_V = 0x11, 0x12, 0x56
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9

_EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _exe_of(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)      # QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = wt.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def find_schematic():
    """Gorunur sematik editoru penceresi (hwnd) ya da None."""
    found = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if any(t.lower() in buf.value.lower() for t in TITLES) and _exe_of(hwnd) in KICAD_EXES:
                    found.append(hwnd)
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return found[0] if found else None


def _key(vk, up=False):
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _activate(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if not user32.SetForegroundWindow(hwnd):
        # Windows on plana gecisi engellerse: kisa bir Alt dokunusu izin verir
        _key(VK_MENU)
        _key(VK_MENU, up=True)
        user32.SetForegroundWindow(hwnd)
    for _ in range(20):
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return False


def paste_to_schematic():
    """True: Ctrl+V gonderildi. False: sematik acik degil ya da one gelmedi."""
    hwnd = find_schematic()
    if not hwnd or not _activate(hwnd):
        return False
    time.sleep(0.15)                       # KiCad tuvali odagi alsin
    if user32.GetForegroundWindow() != hwnd:
        return False
    _key(VK_CONTROL)
    _key(VK_V)
    _key(VK_V, up=True)
    _key(VK_CONTROL, up=True)
    return True
