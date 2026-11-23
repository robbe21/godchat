"""Laadt de WhatsApp-tab (wa.py) en laadt hem opnieuw zodra wa.py wijzigt, zonder de app te herstarten.

Klein en stabiel gehouden: dit bestand laadt server.py één keer bij de start; alles wat verandert staat in wa.py.
- Elke versie van wa.py is een vers moduleobject. Wat een herlading moet overleven (tik-sleutel, droogloopberichten,
  sloten, caches) zit in de dict `staat` hieronder; wa.start() krijgt hem mee.
- Een fout bij het laden houdt de vorige versie in dienst en logt één keer per wijzigingstijd. Is er nog geen versie,
  dan antwoorden de adressen van de tab 503 met de fouttekst.
- get() en post() laten nooit een uitzondering naar server.py ontsnappen.
"""
import importlib.util
import os
import threading
import traceback

HIER = os.path.dirname(os.path.abspath(__file__))
PAD = os.path.join(HIER, "wa.py")

staat = {}            # overleeft elke herlading van wa.py
_slot = threading.Lock()
_mod = None           # de versie van wa.py die nu dient
_mtime = None         # wijzigingstijd van de laatst geprobeerde versie
_fout = None          # fouttekst van de laatste mislukte poging
_args = None          # (app, nep, poort) uit start()


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
            spec = importlib.util.spec_from_file_location("wa_tab", PAD)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if _args is not None:
                mod.start(*_args, staat)
            _mod, _fout = mod, None
            if _mtime is not None:
                print("WhatsApp-tab: wa.py opnieuw geladen", flush=True)
        except Exception as fout:   # de vorige versie blijft dienen
            _fout = f"wa.py laadt niet: {type(fout).__name__}: {fout}"
            print("WhatsApp-tab: " + _fout + ("; de vorige versie blijft dienen" if _mod else ""), flush=True)
            traceback.print_exc()
        _mtime = mt
        return _mod


def start(app, nep=False, poort=8742):
    global _args, _mtime
    _args = (app, bool(nep), int(poort))
    with _slot:
        _mtime = None   # (opnieuw) laden met deze instellingen
    _laad()


def van_mij(pad):
    return pad == "/wa" or pad.startswith("/wa/") or pad.startswith("/api/wa/")


def _antwoord(h, code, tekst):
    try:
        body = tekst.encode()
        h.send_response(code)
        h.send_header("content-type", "text/plain; charset=utf-8")
        h.send_header("x-content-type-options", "nosniff")
        h.send_header("cache-control", "no-store")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    except Exception:
        pass


def _doe(h, naam, *args):
    mod = _laad()
    if mod is None:
        return _antwoord(h, 503, _fout or "de WhatsApp-tab is niet geladen")
    try:
        getattr(mod, naam)(h, *args)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as fout:
        print(f"WhatsApp-tab: fout in {naam} {h.path.split('?')[0]}: {type(fout).__name__}: {fout}", flush=True)
        traceback.print_exc()
        _antwoord(h, 500, f"Interne fout in de WhatsApp-tab ({type(fout).__name__}).")


def get(handler, u):
    _doe(handler, "get", u)


def post(handler):
    _doe(handler, "post")
