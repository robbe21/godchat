"""Laadt de meldingen (push.py) en laadt ze opnieuw zodra push.py wijzigt, zonder de app te herstarten.

Klein en stabiel gehouden, zelfde opzet als klanten_lader.py: server.py laadt dit bestand één keer bij de start; alles
wat verandert staat in push.py (de logica) en sw.js (de service worker, bij elke aanvraag opnieuw gelezen).
- Eén draad roept elke 2 s ronde() aan van de versie van push.py die dan geladen is: een nieuwe versie neemt vanzelf
  over. `staat` overleeft elke herlading (het slot, de app, wat een ronde onthoudt).
- Een fout bij het laden houdt de vorige versie in dienst; een fout in een ronde of een aanvraag komt nooit bij
  server.py of de motor terecht.
- Adressen: GET /sw.js (het bestand) en alles onder /api/push/ (push.py → aanvraag() beslist wat een adres doet,
  dus een nieuw adres vraagt geen herstart). Van buiten vallen ze vanzelf achter het slot (slot.py); een POST gaat in
  server.py eerst langs _vreemd (Host, Origin, Sec-Fetch-Site).
"""
import importlib.util
import json
import os
import threading
import time
import traceback
from urllib.parse import urlparse

HIER = os.path.dirname(os.path.abspath(__file__))
PAD = os.path.join(HIER, "push.py")
SW = os.path.join(HIER, "sw.js")
ELKE_S = 2
MAX_BODY = 16 * 1024

staat = {"slot": threading.Lock()}   # overleeft elke herlading van push.py
_laadslot = threading.Lock()
_mod = None       # de versie van push.py die nu dient
_mtime = None     # wijzigingstijd van de laatst geprobeerde versie
_fout = None      # fouttekst van de laatste mislukte poging
_gelogd = None    # de laatste fout uit een ronde (één keer loggen, niet elke twee seconden)
_draad = None


def _laad():
    global _mod, _mtime, _fout
    try:
        mt = os.stat(PAD).st_mtime_ns
    except OSError:
        mt = None
    if mt is None or mt == _mtime:
        return _mod
    with _laadslot:
        if mt == _mtime:
            return _mod
        try:
            spec = importlib.util.spec_from_file_location("push_blok", PAD)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _mod, _fout = mod, None
            if _mtime is not None:
                print("Meldingen: push.py opnieuw geladen", flush=True)
        except (Exception, SystemExit) as fout:   # de vorige versie blijft dienen
            _fout = f"push.py laadt niet: {type(fout).__name__}: {fout}"
            print("Meldingen: " + _fout + ("; de vorige versie blijft dienen" if _mod else ""), flush=True)
            traceback.print_exc()
        _mtime = mt
        return _mod


def _lus():
    global _gelogd
    time.sleep(3)   # de app eerst laten opstarten
    while True:
        try:
            mod = _laad()
            if mod is not None:
                mod.ronde(staat)
        except Exception as fout:
            tekst = f"{type(fout).__name__}: {fout}"
            if tekst != _gelogd:
                _gelogd = tekst
                print("Meldingen: ronde mislukt: " + tekst, flush=True)
                traceback.print_exc()
        time.sleep(ELKE_S)


def start(app, nep=False):
    global _draad
    staat["app"], staat["nep"] = app, bool(nep)
    _laad()
    if _draad is None:
        _draad = threading.Thread(target=_lus, name="godchat-meldingen", daemon=True)
        _draad.start()


def van_mij(pad):
    return pad == "/sw.js" or pad.startswith("/api/push/")


def _stuur(h, code, body, soort):
    h.send_response(code)
    h.send_header("content-type", soort)
    h.send_header("cache-control", "no-cache" if soort.startswith("text/javascript") else "no-store")
    h.send_header("x-content-type-options", "nosniff")
    h.send_header("content-length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def _json(h, data, code=200):
    _stuur(h, code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")


def _veilig(h, doe):
    """Voer `doe` uit; wat er ook misgaat, server.py merkt er niets van."""
    try:
        doe()
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as fout:
        print(f"Meldingen: fout in {getattr(h, 'path', '?')}: {type(fout).__name__}: {fout}", flush=True)
        traceback.print_exc()
        try:
            _json(h, {"ok": False, "fout": "interne fout"}, 500)
        except Exception:
            pass


def _vraag(h, methode, pad, body):
    """Geef de aanvraag aan de versie van push.py die nu dient. push.py beslist wat een adres doet: een nieuw adres
    onder /api/push/ vraagt dus geen herstart."""
    mod = _laad()
    if mod is None:
        return _json(h, {"ok": False, "beschikbaar": False, "reden": _fout or "push.py is niet geladen"})
    code, data = mod.aanvraag(staat, methode, pad, body, h.headers.get("User-Agent") or "")
    _json(h, data, code)


def get(h, u):
    def doe():
        if u.path == "/sw.js":
            try:
                with open(SW, "rb") as fh:
                    body = fh.read()
            except OSError:
                return _stuur(h, 404, b"sw.js ontbreekt", "text/plain; charset=utf-8")
            return _stuur(h, 200, body, "text/javascript; charset=utf-8")
        _vraag(h, "GET", u.path, None)
    _veilig(h, doe)


def post(h):
    def doe():
        lengte = int(h.headers.get("content-length") or 0)
        if not 0 <= lengte <= MAX_BODY:
            return _json(h, {"ok": False, "fout": "te groot"}, 413)
        try:
            body = json.loads(h.rfile.read(lengte) or b"{}")
        except ValueError:
            return _json(h, {"ok": False, "fout": "geen JSON"}, 400)
        if not isinstance(body, dict):
            return _json(h, {"ok": False, "fout": "geen object"}, 400)
        _vraag(h, "POST", urlparse(h.path).path, body)
    _veilig(h, doe)
