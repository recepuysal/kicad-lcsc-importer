"""LCSC/JLCPCB arama, detay, easyeda2kicad ile ice aktarma ve
eeschema'ya yapistirilabilir pano metni uretimi. wx'e bagimli degil."""
import base64
import json
import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import requests

LIB_NAME = "LCSC"
LIB_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Kicad", "LCSC")
LIB_BASE = os.path.join(LIB_DIR, LIB_NAME)          # easyeda2kicad --output
LIB_SYM = LIB_BASE + ".kicad_sym"
IMG_DB = os.path.join(LIB_DIR, "images.json")      # {lcsc_kodu: resim_url}
# Calisan Python KiCad'in kendisi ya da onun venv'i: ikisinde de base_prefix KiCad\bin
KICAD_BIN = sys.base_prefix
KICAD_CLI = os.path.join(KICAD_BIN, "kicad-cli.exe")
if not os.path.exists(KICAD_CLI):
    KICAD_CLI = shutil.which("kicad-cli") or r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
_m = re.search(r"[\\/](\d+\.\d+)[\\/]bin[\\/]?$", KICAD_BIN)
KICAD_VER = _m.group(1) if _m else "10.0"
KICAD_CFG = os.path.join(os.environ.get("APPDATA", ""), "kicad", KICAD_VER)
SCH_VERSION = "20260306"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
JLC_SEARCH = ("https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/"
              "smtGood/selectSmtComponentList")
LCSC_DETAIL = "https://wmsc.lcsc.com/ftps/wm/product/detail"
EASYEDA_COMP = "https://easyeda.com/api/products/%s/components"

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json"})

CACHE_TTL = 15 * 60                     # sn; ayni sorgu bu sure icinde tekrar gonderilmez
_cache = {}
_cache_lock = threading.Lock()

log = logging.getLogger("lcsc_importer")


def detect_lang():
    """'tr' ya da 'en': once KiCad'in arayuz dili, 'Default' ise Windows dili."""
    try:
        import json as _json
        with open(os.path.join(KICAD_CFG, "kicad_common.json"), encoding="utf-8") as f:
            lang = (_json.load(f).get("system") or {}).get("language") or "Default"
    except (OSError, ValueError):
        lang = "Default"
    if lang and lang.lower() != "default":
        return "tr" if lang.lower().startswith(("tr", "türk", "turk")) else "en"
    try:
        import ctypes
        return "tr" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x1F else "en"
    except (AttributeError, OSError):
        return "en"


LANG = detect_lang()


def T(tr, en):
    """Kullaniciya gosterilen mesajin secili dildeki hali."""
    return en if LANG == "en" else tr


class ServiceError(Exception):
    """Kullaniciya oldugu gibi gosterilebilecek servis hatasi."""


class NoCadError(ServiceError):
    """Parcanin EasyEDA'da sembol/footprint cizimi yok."""


def _cached(key, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    val = fn()
    if val is not None:                 # basarisiz sonuc onbellege girmez
        with _cache_lock:
            _cache[key] = (now, val)
    return val


def _check(r, service):
    if r.status_code in (403, 429):
        log.warning("%s %s dondu: %s", service, r.status_code, r.url)
        raise ServiceError(T("%s şu an istekleri sınırlıyor (HTTP %d). Birkaç dakika sonra tekrar "
                             "deneyin. LCSC kodu (ör. C141632) ile arama bu sırada da çalışır.",
                             "%s is rate-limiting requests right now (HTTP %d). Try again in a few "
                             "minutes. Searching by LCSC code (e.g. C141632) still works meanwhile.")
                           % (service, r.status_code))
    r.raise_for_status()


# --------------------------------------------------------------- arama/detay

LCSC_CODE = re.compile(r"^\s*[Cc]\d{2,}\s*$")


def search(keyword, page=1, page_size=50):
    """Parca aramasi. (normalize edilmis liste, toplam sonuc) dondurur.
    LCSC kodu girilirse arama servisine gitmeden dogrudan urun detayindan doner."""
    keyword = keyword.strip()
    if LCSC_CODE.match(keyword):
        d = detail(keyword.upper())
        if d:
            return [_item_from_detail(d)], 1
    return _cached(("search", keyword.lower(), page, page_size),
                   lambda: _jlc_search(keyword, page, page_size))


def _jlc_search(keyword, page, page_size):
    body = {"keyword": keyword, "currentPage": page, "pageSize": page_size}
    try:
        r = _session.post(JLC_SEARCH, json=body, timeout=20)
    except requests.RequestException as ex:
        raise ServiceError(T("JLCPCB'ye bağlanılamadı: %s", "Could not connect to JLCPCB: %s")
                           % ex.__class__.__name__) from ex
    _check(r, "JLCPCB")
    data = r.json().get("data") or {}
    info = data.get("componentPageInfo") or {}
    out = []
    for it in info.get("list") or []:
        prices = it.get("componentPrices") or []
        out.append({
            "code": it.get("componentCode") or "",
            "model": it.get("componentModelEn") or "",
            "brand": it.get("componentBrandEn") or "",
            "package": it.get("componentSpecificationEn") or "",
            "stock": it.get("stockCount") or 0,
            "price": prices[0]["productPrice"] if prices else None,
            "basic": it.get("componentLibraryType") == "base",
            "desc": it.get("describe") or "",
            "category": it.get("componentTypeEn") or it.get("firstSortName") or "",
            "image": it.get("componentImageUrl") or it.get("minImageAccessIdUrl"),
            "datasheet": it.get("dataManualUrl"),
            "url": it.get("lcscGoodsUrl"),
            "prices": [(p["startNumber"], p["productPrice"]) for p in prices],
        })
    return out, info.get("total") or len(out)


def _item_from_detail(d):
    prices = d.get("productPriceList") or []
    imgs = d.get("productImages") or []
    return {
        "code": d.get("productCode") or "",
        "model": d.get("productModel") or "",
        "brand": d.get("brandNameEn") or "",
        "package": d.get("encapStandard") or "",
        "stock": d.get("stockNumber") or 0,
        "price": prices[0]["usdPrice"] if prices else None,
        "basic": (d.get("smtLabel") or "").upper() == "BASE",
        "desc": d.get("productIntroEn") or "",
        "category": d.get("catalogName") or "",
        "image": imgs[0] if imgs else None,
        "datasheet": d.get("pdfUrl"),
        "url": None,
        "prices": [(p["ladder"], p["usdPrice"]) for p in prices],
    }


def detail(code):
    """LCSC urun detayi (resimler, parametreler, pdf, svg). Yoksa None.
    Sembol/footprint SVG'leri '_svg' altinda data URI olarak eklenir."""
    return _cached(("detail", code), lambda: _lcsc_detail(code))


def _lcsc_detail(code):
    try:
        r = _session.get(LCSC_DETAIL, params={"productCode": code}, timeout=20)
        if r.status_code in (403, 429):
            log.warning("LCSC detay %s dondu (%s)", r.status_code, code)
            return None
        r.raise_for_status()
        d = r.json().get("result")
    except (requests.RequestException, ValueError) as ex:
        log.warning("LCSC detay alinamadi (%s): %s", code, ex)
        return None
    if d:
        d["_svg"] = {k: _svg_data_uri(u) for k, u in (d.get("edaSvgInfo") or {}).items() if u}
    return d


def has_cad(code):
    """EasyEDA'da bu LCSC kodu icin sembol/footprint var mi? Bilinemezse None."""
    def check():
        try:
            r = _session.get(EASYEDA_COMP % code, timeout=15)
            return bool(r.json().get("success"))
        except (requests.RequestException, ValueError) as ex:
            log.warning("EasyEDA kontrolu yapilamadi (%s): %s", code, ex)
            return None
    return _cached(("cad", code), check)


def detail_has_cad(code, d):
    """Detayda SVG varsa cizim var demektir; yoksa EasyEDA'ya sorulur."""
    if d and d.get("edaSvgInfo"):
        return True
    return has_cad(code)


def alternatives(code, model, limit=6, max_checks=10):
    """Ayni parca numarasina sahip, EasyEDA cizimi olan diger LCSC kodlari."""
    model = (model or "").strip()
    if not model:
        return []
    items, _ = search(model, page_size=30)
    key = re.sub(r"[^a-z0-9]", "", model.lower())
    cands = [i for i in items if i["code"] and i["code"] != code]
    # once ayni parca numarasi, sonra stogu fazla olan
    cands.sort(key=lambda i: (key not in re.sub(r"[^a-z0-9]", "", i["model"].lower()),
                              not i["basic"], -(i["stock"] or 0)))
    out = []
    for it in cands[:max_checks]:
        if has_cad(it["code"]):
            out.append(it)
            if len(out) >= limit:
                break
    return out


def _svg_data_uri(url):
    # EasyEDA SVG'yi bazen text/html olarak sunuyor; <img> bunu reddediyor
    try:
        r = _session.get("https:" + url if url.startswith("//") else url, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return None
    if b"<svg" not in r.content[:500]:
        return None
    return "data:image/svg+xml;base64," + base64.b64encode(r.content).decode("ascii")


_REF_RULES = [
    (r"resistor|potentiometer|thermistor|varistor", "R"),
    (r"capacitor", "C"),
    (r"inductor|ferrite|choke", "L"),
    (r"crystal|oscillator|resonator", "Y"),
    (r"\bled\b|light emitting", "D"),
    (r"diode|rectifier|tvs|zener|schottky", "D"),
    (r"transistor|mosfet|bjt|igbt|triode", "Q"),
    (r"connector|header|terminal|socket|usb|jack", "J"),
    (r"fuse|ptc", "F"),
    (r"switch|button|tact", "SW"),
    (r"relay", "K"),
    (r"buzzer|speaker", "BZ"),
]


def passive_value(d):
    """Direnc/kapasitor/bobin icin '10k', '100nF', '4.7uH' gibi deger; degilse None."""
    for p in (d or {}).get("paramVOList") or []:
        if p.get("paramNameEn") in ("Resistance", "Capacitance", "Inductance"):
            v = (p.get("paramValueEn") or "").replace("Ω", "").replace("µ", "u").strip()
            return v or None
    return None


def guess_ref(*texts):
    t = " ".join(x for x in texts if x).lower()
    for pat, ref in _REF_RULES:
        if re.search(pat, t):
            return ref
    return "U"


# ------------------------------------------------------------- ice aktarma

def library_parts():
    """LCSC kutuphanesindeki semboller: [{'symbol','code','value','desc'}]."""
    try:
        with open(LIB_SYM, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []

    def prop(block, key):
        m = re.search(r'\(property\s+"%s"\s+"((?:[^"\\]|\\.)*)"' % key, block)
        return m.group(1) if m else ""

    out = []
    for m in re.finditer(r'^\s{0,2}\(symbol\s+"([^"]+)"', text, re.M):
        block = text[m.start():_block_span(text, m.start())]
        out.append({"symbol": m.group(1), "code": prop(block, "LCSC Part"),
                    "value": prop(block, "Value"), "desc": prop(block, "Description"),
                    "brand": prop(block, "Manufacturer"), "ref": prop(block, "Reference")})
    return out


_img_lock = threading.Lock()


def _load_images():
    try:
        with open(IMG_DB, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def part_images(codes):
    """Kutuphanedeki parcalarin kucuk resimleri {kod: url}. Kayitli olmayanlar
    LCSC'den bir kez alinip images.json'a yazilir (bulunamayan '' olarak)."""
    with _img_lock:
        db = _load_images()
    missing = [c for c in codes if c and c not in db]
    for c in missing:
        d = detail(c)
        if d is None:                   # servis hatasi: sonra tekrar denenir
            continue
        imgs = d.get("productImages") or []
        db[c] = imgs[0] if imgs else ""
    if missing:
        with _img_lock:
            merged = _load_images()
            merged.update(db)
            os.makedirs(LIB_DIR, exist_ok=True)
            with open(IMG_DB, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=1)
            db = merged
    return {c: db.get(c) or "" for c in codes if c}


def find_easyeda2kicad():
    """easyeda2kicad komutu (liste). Once eklentinin kendi ortami (KiCad
    requirements.txt ile kurar), sonra sistemde kurulu olan."""
    try:
        import importlib.util
        if importlib.util.find_spec("easyeda2kicad"):
            py = os.path.join(os.path.dirname(sys.executable), "python.exe")
            return [py if os.path.exists(py) else sys.executable, "-m", "easyeda2kicad"]
    except (ImportError, ValueError):
        pass
    exe = shutil.which("easyeda2kicad")
    if exe:
        return [exe]
    pats = [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\Scripts\easyeda2kicad.exe"),
            os.path.expandvars(r"%APPDATA%\Python\Python3*\Scripts\easyeda2kicad.exe")]
    for p in pats:
        hits = sorted(glob.glob(p), reverse=True)
        if hits:
            return [hits[0]]
    raise RuntimeError(T("easyeda2kicad bulunamadı. KiCad'de Tercihler → Eklentiler'de API açık olmalı; "
                         "eklenti ilk açılışta gerekli paketleri kendisi kurar.",
                         "easyeda2kicad not found. Enable the KiCad API in Preferences → Plugins; "
                         "the plugin installs its packages on first start."))


def _add_lib_row(table_path, kind, uri):
    """Global kutuphane tablosuna LCSC satirini ekler (yoksa). Eklediyse True."""
    try:
        with open(table_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = "(%s_lib_table\n\t(version 7)\n)\n" % kind
    if re.search(r'\(name\s+"%s"\)' % LIB_NAME, text):
        return False
    row = '\t(lib (name "%s") (type "KiCad") (uri "%s") (options "") (descr "LCSC / easyeda2kicad"))\n' % (
        LIB_NAME, uri.replace("\\", "/"))
    end = text.rstrip().rfind(")")
    text = text[:end].rstrip() + "\n" + row + ")\n"
    if os.path.exists(table_path):
        shutil.copyfile(table_path, table_path + ".lcsc-bak")
    with open(table_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def ensure_lib_tables():
    """LCSC sembol ve footprint kutuphanelerini KiCad'e tanitir. Yeni eklendiyse
    True doner (KiCad'in bir kez yeniden baslatilmasi gerekir)."""
    if not os.path.isdir(KICAD_CFG):
        return False
    os.makedirs(LIB_BASE + ".pretty", exist_ok=True)
    if not os.path.exists(LIB_SYM):
        with open(LIB_SYM, "w", encoding="utf-8") as f:
            f.write("(kicad_symbol_lib\n  (version 20211014)\n  (generator https://github.com/uPesy/easyeda2kicad.py)\n)\n")
    a = _add_lib_row(os.path.join(KICAD_CFG, "sym-lib-table"), "sym", LIB_SYM)
    b = _add_lib_row(os.path.join(KICAD_CFG, "fp-lib-table"), "fp", LIB_BASE + ".pretty")
    if a or b:
        log.info("LCSC kutuphanesi KiCad tablolarina eklendi (%s)", KICAD_CFG)
    return a or b


def _clean_env():
    # KiCad icinden calisirken kendi PYTHONHOME/PYTHONPATH'i baska Python'u bozar
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PYTHONHOME", "PYTHONPATH")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def import_part(code, ref_prefix=None, description=None, value=None):
    """Parcayi global LCSC kutuphanesine ekler. {'symbol','footprint','log'} doner."""
    os.makedirs(LIB_DIR, exist_ok=True)
    cmd = find_easyeda2kicad() + ["--full", "--overwrite", "--output", LIB_BASE,
           "--lcsc_id", code]
    if description:
        desc = description.replace('"', "'").replace("\n", " ")
        cmd += ["--custom-field", "Description:" + desc]
    p = subprocess.run(cmd, capture_output=True, env=_clean_env(),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log = (p.stdout + p.stderr).decode("utf-8", "replace")
    sym = re.search(r"Symbol name\s*:\s*(.+)", log)
    fp = re.search(r"Footprint name\s*:\s*(.+)", log)
    if not sym:
        if "Failed to fetch data" in log and has_cad(code) is False:
            raise NoCadError(T("%s için EasyEDA'da sembol/footprint çizimi yok, bu yüzden otomatik "
                               "eklenemiyor. Aynı parçanın çizimi olan başka bir LCSC kodunu seçin.",
                               "%s has no symbol/footprint on EasyEDA, so it can't be imported "
                               "automatically. Pick another LCSC code for the same part.") % code)
        raise RuntimeError(T("İçe aktarma başarısız:\n", "Import failed:\n") + log.strip())
    name = sym.group(1).strip()
    _patch_symbol(name, ref_prefix, value)
    return {"symbol": name, "footprint": fp.group(1).strip() if fp else "", "log": log}


def _block_span(text, start):
    """text[start] == '(' olan s-ifadesinin bitis indeksini (dahil degil) bulur."""
    depth, i, n, in_str = 0, start, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("dengesiz parantez")


def _patch_symbol(name, ref_prefix, value=None):
    """Referans onekini/degeri duzeltir, uretici adindaki Cince eki temizler."""
    with open(LIB_SYM, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r'\(symbol\s+"%s"' % re.escape(name), text)
    if not m:
        return
    end = _block_span(text, m.start())
    block = text[m.start():end]
    if ref_prefix:
        block = re.sub(r'(\(property\s+"Reference"\s+")[^"]*(")',
                       lambda mm: mm.group(1) + ref_prefix + mm.group(2), block, count=1)
    if value:
        block = re.sub(r'(\(property\s+"Value"\s+")[^"]*(")',
                       lambda mm: mm.group(1) + value + mm.group(2), block, count=1)
    block = re.sub(r'(\(property\s+"Manufacturer"\s+")([^"]*)(")',
                   lambda mm: mm.group(1) + re.sub(r"\s*\([^)]*[^\x00-\x7f][^)]*\)", "",
                                                   mm.group(2)).strip() + mm.group(3),
                   block, count=1)
    text = text[:m.start()] + block + text[end:]
    with open(LIB_SYM, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# ------------------------------------------------- s-ifadesi (pano icin)

_TOK = re.compile(r'\s*(?:(\()|(\))|("(?:[^"\\]|\\.)*")|([^\s()"]+))')


def parse_sexpr(text):
    stack, cur, pos = [], [], 0
    n = len(text)
    while pos < n:
        m = _TOK.match(text, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        if m.group(1):
            stack.append(cur)
            cur = []
        elif m.group(2):
            done = cur
            cur = stack.pop()
            cur.append(done)
        else:
            cur.append(m.group(3) or m.group(4))
    return cur[0]


def dump_sexpr(node, ind=0):
    if not isinstance(node, list):
        return node
    atoms, kids = [], []
    for c in node:
        (kids if isinstance(c, list) or kids else atoms).append(c)
    head = "(" + " ".join(atoms)
    if not kids:
        return head + ")"
    tab = "\t" * (ind + 1)
    body = "\n".join(tab + (dump_sexpr(k, ind + 1) if isinstance(k, list) else k)
                     for k in kids)
    return head + "\n" + body + "\n" + "\t" * ind + ")"


def _q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _uq(s):
    return s[1:-1].replace('\\"', '"').replace("\\\\", "\\") if s.startswith('"') else s


def _num(v):
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _modern_symbol(name):
    """kicad-cli ile kutuphaneyi guncel bicime cevirip sembol dugumunu doner."""
    tmp = tempfile.mkdtemp(prefix="lcsc_")
    try:
        out = os.path.join(tmp, "lib.kicad_sym")
        subprocess.run([KICAD_CLI, "sym", "upgrade", "--force", "-o", out, LIB_SYM],
                       capture_output=True, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(out, encoding="utf-8") as f:
            lib = parse_sexpr(f.read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for node in lib:
        if isinstance(node, list) and node[0] == "symbol" and _uq(node[1]) == name:
            return node
    raise RuntimeError("Sembol kutuphanede yok: " + name)


def clipboard_text(name, x=101.6, y=101.6):
    """eeschema'da Ctrl+V ile yapistirilabilecek kicad_sch parcasi uretir."""
    sym = _modern_symbol(name)
    lib_id = "%s:%s" % (LIB_NAME, name)
    sym[1] = _q(lib_id)

    props, pins = [], []
    ref = "U"
    for c in sym:
        if not (isinstance(c, list) and c):
            continue
        if c[0] == "property":
            key = _uq(c[1])
            if key.startswith("ki_"):
                continue
            val = c[2]
            if key == "Reference":
                ref = _uq(val)
                val = _q(ref + "?")
            p = ["property", c[1], val]
            for sub in c[3:]:
                if isinstance(sub, list) and sub[0] == "at":
                    px, py = float(sub[1]), float(sub[2])
                    rot = sub[3] if len(sub) > 3 else "0"
                    sub = ["at", _num(x + px), _num(y - py), rot]
                p.append(sub)
            props.append(p)
        elif c[0] == "symbol":            # birim alt sembolleri -> pinler
            for g in c:
                if isinstance(g, list) and g and g[0] == "pin":
                    for h in g:
                        if isinstance(h, list) and h[0] == "number" and h[1] not in pins:
                            pins.append(h[1])

    inst = ["symbol", ["lib_id", _q(lib_id)], ["at", _num(x), _num(y), "0"],
            ["unit", "1"], ["body_style", "1"], ["exclude_from_sim", "no"],
            ["in_bom", "yes"], ["on_board", "yes"], ["in_pos_files", "yes"],
            ["dnp", "no"], ["uuid", _q(str(uuid.uuid4()))]]
    inst += props
    inst += [["pin", n, ["uuid", _q(str(uuid.uuid4()))]] for n in pins]

    # eeschema panosu dosya basligi (kicad_sch/version) icermez; yalnizca
    # "kopyalanabilir" ogeler: (lib_symbols ...) ve (symbol ...). Baslik olursa
    # KiCad ayristiramaz ve icerigi duz metin olarak yapistirir.
    return dump_sexpr(["lib_symbols", sym]) + "\n" + dump_sexpr(inst) + "\n"
