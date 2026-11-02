"""LCSC parca arama/ice aktarma penceresi.

Arayuzun tamami app.html (Edge WebView). JS -> Python: window.wx.postMessage(json),
Python -> JS: App.<fonksiyon>(json)."""
import json
import logging
import logging.handlers
import os
import pathlib
import threading

import wx
import wx.html2

from . import __version__, core, winpaste

TITLE = "LCSC Parça Ekle"
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "lcsc_importer", "lcsc_importer.log")


def _setup_log():
    log = logging.getLogger("lcsc_importer")
    if log.handlers:
        return
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


class LcscFrame(wx.Frame):
    def __init__(self, parent=None):
        _setup_log()
        super().__init__(parent, title="%s  v%s" % (TITLE, __version__), size=(1320, 860))
        self.SetMinSize((900, 560))
        icon = os.path.join(HERE, "icon64.png")
        if os.path.exists(icon):
            self.SetIcon(wx.Icon(icon, wx.BITMAP_TYPE_PNG))

        self.web = wx.html2.WebView.New(self, backend=wx.html2.WebViewBackendEdge)
        self.web.AddScriptMessageHandler("wx")
        self.web.Bind(wx.html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED, self.on_message)
        self.web.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self.on_nav)
        self.web.Bind(wx.html2.EVT_WEBVIEW_NEWWINDOW, self.on_newwin)
        self.web.EnableContextMenu(False)
        self.web.LoadURL(pathlib.Path(HERE, "app.html").as_uri())
        self.CentreOnScreen()

    # ------------------------------------------------------------- kopru
    def js(self, fn, arg):
        if self:
            self.web.RunScriptAsync("App.%s(%s)" % (fn, json.dumps(arg)))

    def _bg(self, fn, work):
        """work() arka planda calisir, sonucu App.<fn>(sonuc) olarak sayfaya gider."""
        def run():
            try:
                res = work()
            except core.ServiceError as ex:
                res = {"error": str(ex)}
            except Exception as ex:          # noqa: BLE001 - hata sayfada gosterilir
                logging.getLogger("lcsc_importer").exception("%s basarisiz", fn)
                res = {"error": "%s: %s" % (ex.__class__.__name__, ex)}
            wx.CallAfter(self.js, fn, res)
        threading.Thread(target=run, daemon=True).start()

    def on_message(self, evt):
        try:
            msg = json.loads(evt.GetString())
        except ValueError:
            return
        cmd = msg.get("cmd")
        if cmd == "ready":
            self.js("info", {"version": __version__, "lib_dir": core.LIB_DIR, "log": LOG_FILE})
            self._bg("onSetup", self._setup)
        if cmd in ("ready", "library"):
            self._bg("onLibrary", core.library_parts)
        elif cmd == "libimages":
            self._bg("onLibImages", lambda: core.part_images(msg.get("codes") or []))
        elif cmd == "search":
            self._bg("onSearch", lambda: self._search(msg))
        elif cmd == "detail":
            self._bg("onDetail", lambda: self._detail(msg["code"]))
        elif cmd == "alts":
            self._bg("onAlts", lambda: {"code": msg["code"],
                                        "items": core.alternatives(msg["code"], msg.get("model"))})
        elif cmd == "place":
            self._bg("onImport", lambda: self._place(msg["symbol"]))
        elif cmd == "import":
            self._bg("onImport", lambda: self._import(msg))
        elif cmd == "open":
            wx.LaunchDefaultBrowser(msg["url"])

    # ---------------------------------------------------------- isler
    @staticmethod
    def _setup():
        try:
            return {"added": core.ensure_lib_tables()}
        except Exception as ex:              # noqa: BLE001
            logging.getLogger("lcsc_importer").exception("kutuphane tablosu")
            return {"added": False, "error": str(ex)}

    @staticmethod
    def _search(msg):
        base = {"req": msg["req"], "page": msg["page"]}
        try:
            items, total = core.search(msg["kw"], page=msg["page"])
        except core.ServiceError as ex:
            return dict(base, error=str(ex))
        except Exception as ex:              # noqa: BLE001
            logging.getLogger("lcsc_importer").exception("arama basarisiz")
            return dict(base, error="%s: %s" % (ex.__class__.__name__, ex))
        return dict(base, items=items, total=total)

    @staticmethod
    def _detail(code):
        d = core.detail(code)
        dd = d or {}
        return {"code": code, "d": d, "cad": core.detail_has_cad(code, d),
                "ref": core.guess_ref(dd.get("catalogName"), dd.get("parentCatalogName"),
                                      dd.get("productNameEn")),
                "value": core.passive_value(d)}

    def _import(self, msg):
        try:
            r = core.import_part(msg["code"], ref_prefix=msg.get("ref") or "U",
                                 description=msg.get("desc"), value=msg.get("value") or None)
        except core.NoCadError as ex:
            logging.getLogger("lcsc_importer").info("cizim yok: %s", msg["code"])
            return {"error": str(ex), "nocad": True, "code": msg["code"]}
        logging.getLogger("lcsc_importer").info("eklendi %s -> %s:%s", msg["code"], core.LIB_NAME, r["symbol"])
        out = {"symbol": r["symbol"], "footprint": r["footprint"], "placed": False}
        if msg.get("place"):
            out.update(self._place(r["symbol"]))
        return out

    def _place(self, symbol):
        """Sembolu panoya koyar ve sematik aciksa imlece yapistirir."""
        out = {"symbol": symbol, "placed": False, "pasted": False}
        text = core.clipboard_text(symbol)
        done = threading.Event()

        def copy():
            if wx.TheClipboard.Open():
                wx.TheClipboard.SetData(wx.TextDataObject(text))
                wx.TheClipboard.Flush()
                wx.TheClipboard.Close()
                out["placed"] = True
            done.set()
        wx.CallAfter(copy)                # pano yalnizca arayuz thread'inde
        done.wait(5)
        if out["placed"]:
            try:
                out["pasted"] = winpaste.paste_to_schematic()
            except Exception:              # noqa: BLE001 - yapistirma olmazsa Ctrl+V mesaji kalir
                logging.getLogger("lcsc_importer").exception("otomatik yapistirma")
                out["pasted"] = False
        return out

    # ------------------------------------------------------- gezinme
    def on_nav(self, evt):
        url = evt.GetURL()
        if url.startswith(("http://", "https://")):
            evt.Veto()
            wx.LaunchDefaultBrowser(url)

    def on_newwin(self, evt):
        wx.LaunchDefaultBrowser(evt.GetURL())


_frame = None


def show(parent=None):
    """Tek pencere: aciksa one getirir."""
    global _frame
    if _frame:
        try:
            _frame.Iconize(False)
            _frame.Raise()
            return _frame
        except RuntimeError:     # pencere yok edilmis
            pass
    _frame = LcscFrame(parent)
    _frame.Show()
    return _frame
