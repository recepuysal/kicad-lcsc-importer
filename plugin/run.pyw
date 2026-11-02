"""Uygulamayi baslatir (KiCad'deki eklenti dugmesi launch.py uzerinden bunu
calistirir). Pencere zaten aciksa yenisini acmaz, mevcut pencereyi one getirir."""
import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wx  # noqa: E402

from lcsc_importer import __version__, gui  # noqa: E402

app = wx.App(False)
checker = wx.SingleInstanceChecker("lcsc_importer-" + wx.GetUserId())
if checker.IsAnotherRunning():
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(None, "%s  v%s" % (gui.TITLE, __version__))
    if hwnd:
        u.ShowWindow(hwnd, 9)              # SW_RESTORE
        u.SetForegroundWindow(hwnd)
    sys.exit(0)
gui.show()
app.MainLoop()
