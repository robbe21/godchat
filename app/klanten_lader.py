"""Laadt het klantenblok (klanten.py) en laadt het opnieuw zodra klanten.py wijzigt, zonder de app te herstarten.

Klein en stabiel gehouden, zelfde opzet als wa_lader.py: dit bestand laadt server.py één keer bij de start; alles wat
verandert staat in klanten.py.
- Elke versie van klanten.py is een vers moduleobject. Wat een herlading moet overleven (de sloten, de caches, de
  laatste momentopname) zit hier: `staat` gaat mee naar bouw() en meld().
- Een fout bij het laden houdt de vorige versie in dienst en logt één keer per wijzigingstijd.
- GET /api/klanten bouwt hoogstens één keer per 30 s; wie tijdens een opbouw vraagt, krijgt de vorige momentopname.
- get() en meld() laten nooit een uitzondering naar server.py of motor.py ontsnappen.
"""
import importlib.util
import json
import os
import threading
import time
import traceback

HIER = os.path.dirname(os.path.abspath(__file__))
PAD = os.path.join(HIER, "klanten.py")
JS = os.path.join(HIER, "klanten.js")
VERS_S = 30

staat = {"meld_slot": threading.Lock()}   # overleeft elke herlading van klanten.py; één slot voor klantblik.json
_slot = threading.Lock()                  # laden
_bouw_slot = threading.Lock()             # één opbouw tegelijk
_mod = None           # de versie van klanten.py die nu dient
_mtime = None         # wijzigingstijd van de laatst geprobeerde versie
_fout = None          # fouttekst van de laatste mislukte poging
_app = None           # Motor of Nep, uit start()
_snap = {"t": 0.0, "data": None}   # de laatste momentopname van /api/klanten


def _laad():
    global _mod, _mtime, _fout
    try:
        mt = os.stat(PAD).st_mtime_ns
    except OSError:
        mt = None
    if mt is None or mt == _mtime:
        return _mod
    with _slot:
        if mt == _mtime:
            return _mod
        try:
            spec = importlib.util.spec_from_file_location("klanten_blok", PAD)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _mod, _fout = mod, None
            _snap["t"] = 0.0   # de nieuwe versie bouwt meteen
            if _mtime is not None:
                print("Klanten-blok: klanten.py opnieuw geladen", flush=True)
        except (Exception, SystemExit) as fout:   # de vorige versie blijft dienen (ook als het bestand zelf wil stoppen)
            _fout = f"klanten.py laadt niet: {type(fout).__name__}: {fout}"
            print("Klanten-blok: " + _fout + ("; de vorige versie blijft dienen" if _mod else ""), flush=True)
            traceback.print_exc()
        _mtime = mt
        return _mod


def start(app):
    global _app
    _app = app
    _laad()


def van_mij(pad):
    return pad in ("/api/klanten", "/klanten.js")


def _bron(mod):
    """GODCHAT_KLANT_BRON (een map met een momentopname) alleen voor tests; anders de echte paden (None)."""
    pad = os.environ.get("GODCHAT_KLANT_BRON")
    return mod.bron_uit_map(pad) if pad else None


def _leeg(fout):
    return {"ok": False, "fout": fout, "klanten": [], "app": {"taken": [], "werktijd_s": 0}, "werktijd": [], "bronnen": {}}


def _bouw():
    mod = _laad()
    if mod is None:
        return _snap["data"] or _leeg(_fout or "klanten.py is niet geladen")
    try:
        stand_fn = getattr(_app, "stand", None) if _app is not None else None
        data = mod.bouw(_bron(mod), stand_fn, staat=staat)
        if not isinstance(data, dict):
            raise TypeError("bouw() gaf geen dict")
    except Exception as fout:
        print(f"Klanten-blok: bouw mislukt: {type(fout).__name__}: {fout}", flush=True)
        traceback.print_exc()
        data = _snap["data"] or _leeg(f"bouw mislukt: {type(fout).__name__}")
    _snap.update(t=time.time(), data=data)
    return data


def _momentopname():
    if _snap["data"] is not None and time.time() - _snap["t"] < VERS_S:
        return _snap["data"]
    if _bouw_slot.acquire(blocking=False):
        try:
            return _bouw()
        finally:
            _bouw_slot.release()
    if _snap["data"] is not None:
        return _snap["data"]   # een ander bouwt al: de vorige momentopname
    with _bouw_slot:           # er is er nog geen: wachten op die opbouw
        if _snap["data"] is not None:
            return _snap["data"]
        return _bouw()


def _stuur(h, code, body, soort):
    h.send_response(code)
    h.send_header("content-type", soort)
    h.send_header("cache-control", "no-store")
    h.send_header("x-content-type-options", "nosniff")
    h.send_header("content-length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def get(handler, u):
    try:
        if u.path == "/klanten.js":
            try:
                with open(JS, "rb") as fh:
                    body = fh.read()
            except OSError:
                return _stuur(handler, 404, "klanten.js ontbreekt".encode(), "text/plain; charset=utf-8")
            return _stuur(handler, 200, body, "text/javascript; charset=utf-8")
        try:
            data = _momentopname()
        except Exception as fout:
            print(f"Klanten-blok: fout in /api/klanten: {type(fout).__name__}: {fout}", flush=True)
            traceback.print_exc()
            data = _snap["data"] or _leeg(f"interne fout ({type(fout).__name__})")
        _stuur(handler, 200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as fout:
        print(f"Klanten-blok: fout in {getattr(u, 'path', '?')}: {type(fout).__name__}: {fout}", flush=True)


def meld(chat_id, soort, ouder, velden):
    """Voor de motor (meld_stand, taak_klaar): de velden naar klanten.meld. Altijd een tekst, nooit een uitzondering."""
    try:
        mod = _laad()
        if mod is None:
            return ""
        tekst = mod.meld(chat_id, soort, ouder, velden, bron=_bron(mod), staat=staat)
        return tekst if isinstance(tekst, str) else ""
    except Exception as fout:
        try:
            print(f"Klanten-blok: meld van {chat_id} mislukt: {type(fout).__name__}: {fout}", flush=True)
        except Exception:
            pass
        return ""
    finally:
        _snap["t"] = 0.0   # de volgende aanvraag bouwt vers
