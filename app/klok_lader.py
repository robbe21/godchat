"""Laadt de klok voor geplande taken (klok.py) en laadt ze opnieuw zodra klok.py wijzigt, zonder de app te herstarten.

Klein en stabiel gehouden, zelfde opzet als push_lader.py: server.py laadt dit bestand één keer bij de start; alles wat
verandert staat in klok.py (de logica) en klok.js (het scherm, bij elke aanvraag opnieuw gelezen).
- Eén draad roept elke 30 s tik() aan van de versie van klok.py die dan geladen is. `staat` overleeft elke herlading
  (het slot, de app, wat de tik onthoudt).
- Haken voor motor.py: ouder_regel, ronde_klaar, beurt_einde. Ze geven nooit een uitzondering terug:
  niets uit de klok mag een chat of de app laten vallen.
- Adressen: GET /klok.js, GET /api/klok, POST /api/klok/zet {id, aan} en POST /api/klok/nu {id}. De twee POST's zijn
  voor het scherm van de eigenaar (de schakelaar en "Nu draaien"): geen enkele chat roept ze aan, en geen tool van
  godchat_mcp.py verwijst ernaar. Van buiten vallen ze vanzelf achter het slot (slot.py); een POST gaat in server.py
  eerst langs _vreemd (Host, Origin, Sec-Fetch-Site).
"""
import importlib.util
import json
import os
import threading
import time
import traceback

HIER = os.path.dirname(os.path.abspath(__file__))
PAD = os.path.join(HIER, "klok.py")
JS = os.path.join(HIER, "klok.js")
ELKE_S = 30
MAX_BODY = 4096

staat = {"slot": threading.RLock()}   # overleeft elke herlading van klok.py
_laadslot = threading.Lock()
_mod = None
_mtime = None
_fout = None
_gelogd = None
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
            spec = importlib.util.spec_from_file_location("klok_blok", PAD)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _mod, _fout = mod, None
            if _mtime is not None:
                print("Klok: klok.py opnieuw geladen", flush=True)
        except (Exception, SystemExit) as fout:   # de vorige versie blijft dienen
            _fout = f"klok.py laadt niet: {type(fout).__name__}: {fout}"
            print("Klok: " + _fout + ("; de vorige versie blijft dienen" if _mod else ""), flush=True)
            traceback.print_exc()
        _mtime = mt
        return _mod


def _lus():
    global _gelogd
    time.sleep(10)   # de app eerst laten opstarten (Motor.start levert eerst de beurten die liepen)
    while True:
        try:
            mod = _laad()
            if mod is not None:
                mod.tik(staat)
        except Exception as fout:
            tekst = f"{type(fout).__name__}: {fout}"
            if tekst != _gelogd:
                _gelogd = tekst
                print("Klok: tik mislukt: " + tekst, flush=True)
                traceback.print_exc()
        time.sleep(ELKE_S)


def start(app, nep=False):
    global _draad
    staat["app"], staat["nep"] = app, bool(nep)
    _laad()
    if _draad is None and not nep:
        _draad = threading.Thread(target=_lus, name="godchat-klok", daemon=True)
        _draad.start()


# --- haken voor motor.py ---------------------------------------------------------------------------------------------
def _haak(naam, *args, standaard=None):
    try:
        mod = _laad()
        fn = getattr(mod, naam, None) if mod is not None else None
        if fn is None:
            return standaard
        return fn(staat, *args)
    except Exception as fout:
        try:
            print(f"Klok: {naam} mislukt: {type(fout).__name__}: {fout}", flush=True)
            traceback.print_exc()
        except Exception:
            pass
        return standaard


def _roep(naam, *args):
    """Een functie van klok.py waarvan de fout (ValueError) bij de chat of het scherm moet komen."""
    mod = _laad()
    fn = getattr(mod, naam, None) if mod is not None else None
    if fn is None:
        raise ValueError(_fout or "de klok is niet geladen")
    return fn(staat, *args)


def wijzig_taak(chat_id, args):
    return _roep("wijzig_taak", chat_id, args)


def praat(cid, tekst):
    return _roep("praat", cid, tekst)


def gesprek_buiten(cid):
    """/api/gesprek voor een oud rondegesprek of een taak zonder chat; None als het niet van de klok is."""
    return _haak("gesprek_buiten", cid, standaard=None)


def plan_taak(chat_id, args):
    """De tool plan_taak (via motor.py). Anders dan de haken hierboven komt een fout wel bij de chat: ValueError."""
    mod = _laad()
    fn = getattr(mod, "plan_taak", None) if mod is not None else None
    if fn is None:
        raise ValueError(_fout or "de klok is niet geladen: er is niets gepland")
    return fn(staat, chat_id, args)


def ouder_regel(ouder_id):
    tekst = _haak("ouder_regel", ouder_id, standaard="")
    return tekst if isinstance(tekst, str) else ""


def ronde_klaar(sleutel, samenvatting):
    return _haak("ronde_klaar", sleutel, samenvatting, standaard=False)


def beurt_einde(chat):
    return _haak("beurt_einde", chat)


def koppelingen_bij_start(sleutel, servers):
    return _haak("koppelingen_bij_start", sleutel, servers)


# --- adressen --------------------------------------------------------------------------------------------------------
def van_mij(pad):
    return pad in ("/klok.js", "/api/klok", "/api/klok/taak", "/api/klok/zet", "/api/klok/nu")


def _stuur(h, code, body, soort):
    h.send_response(code)
    h.send_header("content-type", soort)
    h.send_header("cache-control", "no-store")
    h.send_header("x-content-type-options", "nosniff")
    h.send_header("content-length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def _json(h, data, code=200):
    _stuur(h, code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")


def _veilig(h, doe):
    try:
        doe()
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as fout:
        print(f"Klok: fout in {getattr(h, 'path', '?')}: {type(fout).__name__}: {fout}", flush=True)
        traceback.print_exc()
        try:
            _json(h, {"ok": False, "fout": "interne fout"}, 500)
        except Exception:
            pass


def get(h, u):
    def doe():
        if u.path == "/klok.js":
            try:
                with open(JS, "rb") as fh:
                    body = fh.read()
            except OSError:
                return _stuur(h, 404, b"klok.js ontbreekt", "text/plain; charset=utf-8")
            return _stuur(h, 200, body, "text/javascript; charset=utf-8")
        if u.path not in ("/api/klok", "/api/klok/taak"):
            return _json(h, {"ok": False, "fout": "niet gevonden"}, 404)
        mod = _laad()
        if mod is None:
            return _json(h, {"ok": False, "fout": _fout or "klok.py is niet geladen", "taken": []})
        if u.path == "/api/klok/taak":
            from urllib.parse import parse_qs
            tid = (parse_qs(u.query).get("id") or [""])[0]
            try:
                return _json(h, mod.taak_detail(staat, tid))
            except ValueError as fout:
                return _json(h, {"ok": False, "fout": str(fout)}, 404)
        _json(h, mod.overzicht(staat))
    _veilig(h, doe)


def post(h):
    def doe():
        from urllib.parse import urlparse
        pad = urlparse(h.path).path
        lengte = int(h.headers.get("content-length") or 0)
        if not 0 <= lengte <= MAX_BODY:
            return _json(h, {"ok": False, "fout": "te groot"}, 413)
        try:
            body = json.loads(h.rfile.read(lengte) or b"{}")
        except ValueError:
            return _json(h, {"ok": False, "fout": "geen JSON"}, 400)
        if not isinstance(body, dict) or not isinstance(body.get("id"), str):
            return _json(h, {"ok": False, "fout": "id ontbreekt"}, 400)
        mod = _laad()
        if mod is None:
            return _json(h, {"ok": False, "fout": _fout or "klok.py is niet geladen"}, 503)
        if staat.get("nep"):
            return _json(h, {"ok": False, "fout": "in de nepmodus wijzigt de klok niets"}, 400)
        try:
            if pad == "/api/klok/zet":
                return _json(h, mod.zet(staat, body["id"], body.get("aan")))
            if pad == "/api/klok/nu":
                return _json(h, mod.nu_draaien(staat, body["id"]))
        except ValueError as fout:
            return _json(h, {"ok": False, "fout": str(fout)}, 400)
        _json(h, {"ok": False, "fout": "niet gevonden"}, 404)
    _veilig(h, doe)
