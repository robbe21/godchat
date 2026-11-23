#!/usr/bin/env python3
"""GodChat: lokale web-app boven de God-chat. Luistert alleen op 127.0.0.1.

  python3 -I tools/god-chat/app/server.py          de app (poort uit config.json, standaard 8742)
  python3 -I tools/god-chat/app/server.py --nep    het scherm met nepgegevens (nep.json), zonder motor

Zonder config.json gelden de standaardwaarden (STANDAARD_CONFIG). Zie CLAUDE.md in deze map.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HIER)
try:
    import wa_lader   # WhatsApp-tab (wa.py, wa.html); een fout daar mag de app niet tegenhouden
except Exception as fout:
    wa_lader = None
    print(f"WhatsApp-tab niet geladen: {fout}", flush=True)
try:
    import klanten_lader   # Klanten in één blik (klanten.py, klanten.js); een fout daar mag de app niet tegenhouden
except Exception as fout:
    klanten_lader = None
    print(f"Klanten-blok niet geladen: {fout}", flush=True)
try:
    import push_lader   # meldingen op je toestellen (push.py, sw.js); een fout daar mag de app niet tegenhouden
except Exception as fout:
    push_lader = None
    print(f"Meldingen niet geladen: {fout}", flush=True)
try:
    import klok_lader   # de klok voor geplande taken (klok.py, klok.js); een fout daar mag de app niet tegenhouden
except Exception as fout:
    klok_lader = None
    print(f"Klok niet geladen: {fout}", flush=True)
SPRAAK = os.path.join(os.path.dirname(HIER), "spraak.py")
# de Python met mlx-whisper (dicteren); ontbreekt hij, dan werkt alles behalve dicteren
WHISPER_PY = os.path.expanduser(os.environ.get("GODCHAT_WHISPER_PY") or "~/.venvs/mlx-whisper/bin/python")
STANDAARD_CONFIG = {"poort": 8742, "chats": {"god": {"model": "opus", "effort": "high"}},
                    "compact": {"aan": True, "tokens": 200000}}
STATISCH = os.path.join(HIER, "statisch")   # vaste bestanden van het scherm (/s/<naam>): het app-icoon en het manifest
SOORTEN = {".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
           ".webmanifest": "application/manifest+json; charset=utf-8"}


def uur(ts=None):
    return datetime.fromtimestamp(ts or time.time()).strftime("%H:%M")


class Nep:
    """Het scherm met nepgegevens: alles in het geheugen, knoppen doen alsof."""

    modus = "nep"

    def __init__(self):
        with open(os.path.join(HIER, "nep.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        self.chats = d["chats"]
        self.vragen = d["vragen"]
        self.verloop = d["verloop"]
        self.gesprekken = d["gesprekken"]
        self.versie = 0
        self.slot = threading.Lock()

    def _gebeurd(self, soort, chat, route, kop, tekst):
        self.verloop.insert(0, {"id": f"n{time.time_ns()}", "uur": uur(), "soort": soort, "chat": chat,
                                "route": route, "kop": kop, "tekst": tekst})
        self.versie += 1

    def _naam(self, cid):
        return next((c["naam"] for c in self.chats if c["id"] == cid), cid)

    def stand(self):
        with self.slot:
            chats = [dict(c, versie=self.versie) for c in self.chats]
            return {"modus": self.modus, "nu": uur(), "chats": chats, "vragen": self.vragen, "verloop": self.verloop}

    def gesprek(self, cid):
        return {"beurten": self.gesprekken.get(cid, [])}

    def zeg(self, cid, tekst):
        with self.slot:
            self.gesprekken.setdefault(cid, []).append({"rol": "user", "bron": "eigenaar", "uur": uur(), "tekst": tekst})
            self._gebeurd("eigenaar", cid, f"Jij → {self._naam(cid)}", tekst[:80], tekst)
        return {"ok": True}

    def antwoord(self, nr, tekst):
        with self.slot:
            v = next((v for v in self.vragen if v["nr"] == nr and v["status"] == "open"), None)
            if not v:
                raise ValueError(f"geen open vraag {nr}")
            v["status"], v["antwoord"] = "beantwoord", tekst
            beurt = f"Antwoord van de eigenaar op vraag {nr}:\n{tekst}"
            self.gesprekken.setdefault(v["chat"], []).append({"rol": "user", "bron": "antwoord", "uur": uur(), "tekst": beurt})
            for c in self.chats:
                if c["id"] == v["chat"] and not any(x["chat"] == c["id"] and x["status"] == "open" for x in self.vragen):
                    c["toestand"], c["uur"], c["regel"] = "werkt", uur(), f"Voert uit na jouw antwoord op vraag {nr}."
            self._gebeurd("antwoord", v["chat"], f"Jij → {self._naam(v['chat'])}", f"vraag {nr}: {tekst[:60]}", beurt)
        return {"ok": True}

    def neem_over(self, cid):
        with self.slot:
            for c in self.chats:
                if c["id"] == cid:
                    c["toestand"], c["uur"], c["regel"] = "werkt", uur(), "Overgenomen in de app; doet zijn start."
            self._gebeurd("start", cid, self._naam(cid), "overgenomen in de app", "")
        return {"ok": True}

    def nieuwe_dag(self):
        with self.slot:
            self._gebeurd("start", "god", "App", "nieuwe dag: verse sessies", "")
        return {"ok": True}

    def tool(self, body):
        raise ValueError("geen motor in de nepmodus")

    def door(self, naar, tekst):
        raise ValueError("geen motor in de nepmodus")

    def nieuwe_taak(self, ouder, naam, opdracht):
        with self.slot:
            tid = f"{ouder}--nep{len(self.chats)}"
            i = next(i for i, c in enumerate(self.chats) if c["id"] == ouder)
            self.chats.insert(i + 1, {"id": tid, "naam": naam, "soort": "taak", "ouder": ouder, "toestand": "werkt",
                                      "uur": uur(), "regel": "Gestart: " + opdracht[:100]})
            self._gebeurd("start", tid, f"Jij → {self._naam(ouder)} › {naam}", "taakchat gestart", opdracht)
        return {"ok": True, "chat": tid}

    def taak_status(self, tid, status):
        with self.slot:
            for c in self.chats:
                if c["id"] == tid:
                    c["toestand"] = "klaar" if status == "klaar" else "slaapt"
            self.versie += 1
        return {"ok": True}

    def zet_model(self, cid, model, effort):
        with self.slot:
            for c in self.chats:
                if c["id"] == cid:
                    c["model"], c["effort"] = model or "standaard", effort or "standaard"
            self.versie += 1
        return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "GodChat"

    def log_message(self, *a):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fout(self, code, tekst):
        body = tekst.encode()
        self.send_response(code)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _vreemd(self):
        """Komt deze aanvraag van een vreemde pagina? Op deze poort staat geen login, dus:
        - Host is altijd 127.0.0.1:<poort> of localhost:<poort>. Anders kan een website de app uitlezen met
          DNS-rebinding: haar eigen naam wijst dan plots naar 127.0.0.1 en de browser ziet de app als die site.
        - Een POST die Origin of Sec-Fetch-Site draagt (een browser) komt van de eigen pagina: zelfde host én poort.
          Zo stuurt geen andere site, en ook geen pagina op een andere lokale poort, een beurt naar een chat.
          Zonder die koppen (curl, de MCP-server, herstart.py) komt ze van deze computer zelf.
        - Een /api/-GET van een andere site (Sec-Fetch-Site cross-site of same-site) mag ook niet.
        Achter het slot vertaalde slot.py Host en Origin al naar lokaal. Geeft de reden, of None."""
        poort = getattr(self, "_slot_lokale_poort", None) or self.server.server_address[1]
        eigen = (f"127.0.0.1:{poort}", f"localhost:{poort}")
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in eigen:
            return "onbekende host"
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if self.command in ("GET", "HEAD"):
            if sfs in ("cross-site", "same-site") and urlparse(self.path).path.startswith("/api/"):
                return "alleen vanaf de eigen pagina"
            return None
        herkomst = self.headers.get("Origin")
        if herkomst is not None and herkomst.strip().lower() not in tuple("http://" + e for e in eigen):
            return "alleen vanaf de eigen pagina"
        if sfs and sfs != "same-origin":
            return "alleen vanaf de eigen pagina"
        return None

    def _statisch(self, naam):
        """Een vast bestand uit app/statisch/: alleen een kale bestandsnaam met een bekende extensie, nooit een pad."""
        soort = SOORTEN.get(os.path.splitext(naam)[1].lower())
        if (not soort or not 0 < len(naam) <= 60 or naam.startswith(".")
                or any(not (c.isascii() and (c.isalnum() or c in "._-")) for c in naam)):
            return self._fout(404, "niet gevonden")
        try:
            with open(os.path.join(STATISCH, naam), "rb") as fh:
                body = fh.read()
        except OSError:
            return self._fout(404, "niet gevonden")
        self.send_response(200)
        self.send_header("content-type", soort)
        self.send_header("cache-control", "no-cache" if naam.endswith(".webmanifest") else "max-age=3600")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        nee = self._vreemd()
        if nee:
            return self._fout(403, nee)
        u = urlparse(self.path)
        if wa_lader and wa_lader.van_mij(u.path):
            return wa_lader.get(self, u)
        if klanten_lader and klanten_lader.van_mij(u.path):
            return klanten_lader.get(self, u)
        if push_lader and push_lader.van_mij(u.path):
            return push_lader.get(self, u)
        if klok_lader and klok_lader.van_mij(u.path):
            return klok_lader.get(self, u)
        if u.path.startswith("/s/"):
            return self._statisch(u.path[3:])
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HIER, "index.html"), "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("cache-control", "no-store")
            # nooit in een frame: anders legt een andere site haar eigen knoppen over de antwoordknoppen (clickjacking)
            self.send_header("x-frame-options", "DENY")
            self.send_header("content-security-policy", "frame-ancestors 'none'")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/stand":
            self._json(self.app.stand())
        elif u.path == "/api/gesprek":
            self._json(self.app.gesprek(parse_qs(u.query).get("chat", ["god"])[0]))
        else:
            self._fout(404, "niet gevonden")

    def do_POST(self):
        nee = self._vreemd()
        if nee:
            return self._fout(403, nee)
        if wa_lader and wa_lader.van_mij(urlparse(self.path).path):
            return wa_lader.post(self)
        if push_lader and push_lader.van_mij(urlparse(self.path).path):
            return push_lader.post(self)
        if klok_lader and klok_lader.van_mij(urlparse(self.path).path):
            return klok_lader.post(self)
        if urlparse(self.path).path == "/api/dicteer":
            return self._dicteer()
        try:
            lengte = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(lengte) or b"{}")
            pad = urlparse(self.path).path
            if pad == "/api/zeg":
                return self._json(self.app.zeg(body["chat"], str(body["tekst"])))
            if pad == "/api/antwoord":
                return self._json(self.app.antwoord(int(body["nr"]), str(body["tekst"])))
            if pad == "/api/neem-over":
                return self._json(self.app.neem_over(body["chat"]))
            if pad == "/api/nieuwe-dag":
                return self._json(self.app.nieuwe_dag())
            if pad == "/api/door":
                return self._json(self.app.door(body["naar"], str(body["tekst"])))
            if pad == "/api/taak":
                return self._json(self.app.nieuwe_taak(body["ouder"], str(body["naam"]), str(body["opdracht"])))
            if pad == "/api/taak-status":
                return self._json(self.app.taak_status(body["chat"], body["status"]))
            if pad == "/api/model":
                return self._json(self.app.zet_model(body["chat"], body.get("model"), body.get("effort")))
            if pad == "/api/tool":
                return self._json(self.app.tool(body))
            self._fout(404, "niet gevonden")
        except (KeyError, ValueError) as fout:
            self._fout(400, str(fout))


    def _dicteer(self):
        """Spraak uit het invoerveld naar tekst, lokaal met mlx-whisper (spraak.py), zonder dat audio de Mac verlaat."""
        lengte = int(self.headers.get("content-length") or 0)
        if not 0 < lengte <= 50_000_000:
            return self._fout(400, "geen of te veel audio")
        if not os.path.isfile(WHISPER_PY):
            return self._fout(501, f"dicteren staat uit: geen mlx-whisper op {WHISPER_PY} (zie GODCHAT_WHISPER_PY)")
        soort = (self.headers.get("content-type") or "").split(";")[0]
        ext = {"audio/webm": ".webm", "audio/mp4": ".m4a", "audio/ogg": ".ogg", "audio/wav": ".wav"}.get(soort, ".webm")
        fd, pad = tempfile.mkstemp(prefix="godchat-dicteer-", suffix=ext)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(self.rfile.read(lengte))
            r = subprocess.run([WHISPER_PY, "-I", SPRAAK, pad], capture_output=True, text=True, timeout=180)
            if r.returncode:
                return self._fout(500, "uitschrijven mislukt: " + (r.stderr.strip().splitlines() or ["?"])[-1])
            regels = r.stdout.strip().splitlines()
            self._json({"tekst": " ".join(x.strip() for x in regels[1:] if x.strip())})
        except subprocess.TimeoutExpired:
            self._fout(504, "uitschrijven duurde te lang")
        finally:
            try:
                os.remove(pad)
            except OSError:
                pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nep", action="store_true", help="het scherm met nepgegevens, zonder motor")
    p.add_argument("--poort", type=int, default=None)
    a = p.parse_args()
    try:
        with open(os.path.join(HIER, "config.json"), encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        config = json.loads(json.dumps(STANDAARD_CONFIG))   # een eigen kopie: een modelwissel schrijft config.json
    poort = a.poort or config.get("poort", 8742)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    klasse = Handler
    try:   # het slot (slot.py): login op een eigen poort voor wat van buiten komt; een fout daar mag de app niet tegenhouden
        import slot
        klasse = slot.lokaal(Handler, poort)
    except Exception as fout:
        slot = None
        print(f"Slot niet geladen: {fout}", flush=True)
    try:   # eerst de poort: draait de app al, dan stopt deze tweede hier, vóór ze wachters of sessies van de eerste raakt
        srv = ThreadingHTTPServer(("127.0.0.1", poort), klasse)
    except OSError as fout:
        raise SystemExit(f"Poort {poort} is niet vrij ({fout}). Draait de GodChat-app al? Kijk op "
                         f"http://127.0.0.1:{poort}; herstarten gaat met app/herstart.py.") from None
    if a.nep:
        Handler.app = Nep()
    else:
        from motor import Motor
        Handler.app = Motor(config)
        Handler.app.poort = poort   # ook met --poort: de tools van de sessies (godchat_mcp.py) praten met déze poort
        Handler.app.start()
    if wa_lader:
        wa_lader.start(Handler.app, nep=a.nep, poort=poort)
    if klanten_lader:
        klanten_lader.start(Handler.app)
    if push_lader:
        push_lader.start(Handler.app, nep=a.nep)
    if klok_lader:
        klok_lader.start(Handler.app, nep=a.nep)
    print(f"GodChat ({Handler.app.modus}) op http://127.0.0.1:{poort}", flush=True)
    if slot:
        try:
            slot.start_buiten(Handler, poort, nep=a.nep)
        except Exception as fout:
            print(f"Slot: buitenpoort niet gestart: {fout}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if hasattr(Handler.app, "stop"):
            Handler.app.stop()


if __name__ == "__main__":
    main()
