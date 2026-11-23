#!/usr/bin/env python3
"""Test van slot.py, los van de app: een nep-handler, tijdelijke poorten en een tijdelijke slotmap.

  python3 -I tools/god-chat/app/slot_test.py

Raakt de echte app, de echte slotmap (~/.config/godchat) en de motor niet. Elke regel is een weg naar binnen die dicht
moet zijn, of een weg die open moet blijven.
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAP = tempfile.mkdtemp(prefix="godchat-slot-test-")
os.environ["GODCHAT_SLOT_MAP"] = MAP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slot  # noqa: E402

WW = "appelboom-4-fietsen"
gezien = []      # wat de nep-app binnenkreeg: (methode, pad, Host, Origin)
uitslag = {"goed": 0, "fout": 0}


class NepApp(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _antwoord(self):
        lengte = int(self.headers.get("content-length") or 0)
        if lengte:
            self.rfile.read(lengte)
        gezien.append((self.command, self.path, self.headers.get("Host"), self.headers.get("Origin")))
        self.send_response(200)
        self.send_header("content-length", "3")
        self.end_headers()
        self.wfile.write(b"app")

    do_GET = do_POST = do_PROPFIND = _antwoord


def vraag(poort, methode, pad, koppen=None, body=b""):
    """Een rauwe aanvraag, zodat ook kromme paden en dubbele koppen te testen zijn. Geeft (code, koppen, inhoud)."""
    koppen = dict(koppen or {})
    koppen.setdefault("Host", f"127.0.0.1:{poort}")
    if body:
        koppen["Content-Length"] = str(len(body))
    regels = [f"{methode} {pad} HTTP/1.0"]
    for k, v in koppen.items():
        regels += [f"{k}: {w}" for w in (v if isinstance(v, list) else [v])]
    with socket.create_connection(("127.0.0.1", poort), timeout=10) as s:
        s.sendall(("\r\n".join(regels) + "\r\n\r\n").encode() + body)
        buf = b""
        while True:
            deel = s.recv(65536)
            if not deel:
                break
            buf += deel
    kop, _, inhoud = buf.partition(b"\r\n\r\n")
    rijen = kop.decode("latin-1").split("\r\n")
    uit = {}
    for r in rijen[1:]:
        k, _, v = r.partition(":")
        uit.setdefault(k.strip().lower(), []).append(v.strip())
    return int(rijen[0].split()[1]), uit, inhoud


def klopt(naam, voorwaarde, extra=""):
    uitslag["goed" if voorwaarde else "fout"] += 1
    if not voorwaarde:
        print(f"FOUT  {naam} {extra}")


def server(klasse):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), klasse)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    lokaal_srv = server(NepApp)
    lp = lokaal_srv.server_address[1]
    lokaal_srv.RequestHandlerClass = slot.lokaal(NepApp, lp)
    buiten_srv = server(slot._maak_buiten(NepApp, lp))
    bp = buiten_srv.server_address[1]
    adres = f"http://127.0.0.1:{bp}"
    eigen = {"Origin": adres}
    formulier = {"Origin": adres, "Content-Type": "application/x-www-form-urlencoded"}

    def app_geraakt():
        n = len(gezien)
        gezien.clear()
        return n

    try:
        slot._maak_buiten(slot.lokaal(NepApp, lp), lp)
        klopt("de lokale klasse mag niet achter de buitenpoort", False)
    except ValueError:
        klopt("de lokale klasse mag niet achter de buitenpoort", True)

    # 1. Niets ingesteld: de buitenpoort weigert alles, de lokale poort werkt als altijd
    klopt("dicht zonder wachtwoord (GET)", vraag(bp, "GET", "/")[0] == 503)
    klopt("dicht zonder wachtwoord (POST)", vraag(bp, "POST", "/api/zeg", eigen, b"{}")[0] == 503)
    klopt("dicht zonder wachtwoord (login)", vraag(bp, "POST", "/login", formulier, b"wachtwoord=x")[0] == 503)
    klopt("niets bij de app", app_geraakt() == 0)
    code, _, inhoud = vraag(lp, "GET", "/api/stand")
    klopt("lokaal ongewijzigd", code == 200 and inhoud == b"app" and gezien[-1][2] == f"127.0.0.1:{lp}", str(code))
    code, _, inhoud = vraag(lp, "POST", "/api/zeg", {"Origin": f"http://127.0.0.1:{lp}"}, b"{}")
    klopt("lokaal POST ongewijzigd", code == 200 and gezien[-1][3] == f"http://127.0.0.1:{lp}")
    app_geraakt()

    # 1b. Een proxy die per vergissing aan de lokale poort hangt: alles geweigerd, ook /slot
    for kop in ("X-Forwarded-For", "X-Forwarded-Proto", "Forwarded", "Via", "X-Real-IP", "Tailscale-User-Login", "tailscale-headers-info"):
        klopt(f"lokaal met proxykop {kop}", vraag(lp, "GET", "/api/stand", {kop: "x"})[0] == 403)
    klopt("lokaal POST met proxykop", vraag(lp, "POST", "/api/zeg", {"Origin": f"http://127.0.0.1:{lp}", "X-Forwarded-For": "100.64.0.9"}, b"{}")[0] == 403)
    klopt("/slot met proxykop", vraag(lp, "GET", "/slot", {"X-Forwarded-For": "100.64.0.9"})[0] == 403)
    klopt("niets daarvan bij de app", app_geraakt() == 0)
    klopt("lokaal zonder proxykop werkt", vraag(lp, "GET", "/api/stand", {"X-Iets-Anders": "x", "Viaduct": "x"})[0] == 200 and app_geraakt() == 1)

    # 2. Het lokale scherm /slot
    code, _, pagina = vraag(lp, "GET", "/slot")
    klopt("/slot lokaal toont de pagina", code == 200 and b"Wachtwoord kiezen" in pagina and slot._TIK.encode() in pagina)
    klopt("/slot met een vreemde Host", vraag(lp, "GET", "/slot", {"Host": "kwaad.example:1"})[0] == 403)
    klopt("/slot van een andere site", vraag(lp, "GET", "/slot", {"Sec-Fetch-Site": "cross-site"})[0] == 403)
    lok = {"Origin": f"http://127.0.0.1:{lp}", "Content-Type": "application/x-www-form-urlencoded"}
    zet = f"actie=zet&wachtwoord={WW}&herhaal={WW}"
    klopt("/slot zonder tik", vraag(lp, "POST", "/slot", lok, zet.encode())[0] == 403)
    klopt("/slot zonder Origin", vraag(lp, "POST", "/slot", {"Content-Type": lok["Content-Type"]}, f"tik={slot._TIK}&{zet}".encode())[0] == 403)
    klopt("/slot met vreemde Origin", vraag(lp, "POST", "/slot", dict(lok, Origin="http://kwaad.example"), f"tik={slot._TIK}&{zet}".encode())[0] == 403)
    klopt("nog steeds geen wachtwoord", not slot._lees(slot.INSTELLING).get("hash"))
    klopt("/slot te kort wachtwoord", vraag(lp, "POST", "/slot", lok, f"tik={slot._TIK}&actie=zet&wachtwoord=kort&herhaal=kort".encode())[0] == 400)
    klopt("/slot ongelijke wachtwoorden", vraag(lp, "POST", "/slot", lok, f"tik={slot._TIK}&actie=zet&wachtwoord={WW}&herhaal={WW}x".encode())[0] == 400)
    code, _, pagina = vraag(lp, "POST", "/slot", lok, f"tik={slot._TIK}&{zet}".encode())
    inst = slot._lees(slot.INSTELLING)
    klopt("/slot zet het wachtwoord", code == 200 and inst.get("hash", {}).get("soort") == "scrypt")
    klopt("alleen een hash op schijf", WW not in open(slot.INSTELLING, encoding="utf-8").read())
    klopt("bestand alleen voor de eigenaar", oct(os.stat(slot.INSTELLING).st_mode & 0o777) == "0o600" and oct(os.stat(MAP).st_mode & 0o777) == "0o700")
    klopt("/slot niet bij de app beland", app_geraakt() == 0)

    # 3. Wachtwoord gezet, maar nog geen buitenadres: nog altijd dicht
    klopt("dicht zonder buitenadres", vraag(bp, "GET", "/")[0] == 503 and vraag(bp, "POST", "/login", formulier, f"wachtwoord={WW}".encode())[0] == 503)
    try:
        slot.zet_buiten("https://naam.example.net/pad")
        klopt("buitenadres met pad geweigerd", False)
    except ValueError:
        klopt("buitenadres met pad geweigerd", True)
    slot.zet_buiten(adres)

    # 4. Opengezet, niet ingelogd
    code, k, _ = vraag(bp, "GET", "/", {"Accept": "text/html"})
    klopt("pagina zonder cookie -> login", code == 303 and k.get("location") == ["/login"])
    klopt("api zonder cookie -> 401", vraag(bp, "GET", "/api/stand")[0] == 401)
    klopt("POST zonder cookie -> 401", vraag(bp, "POST", "/api/zeg", eigen, b"{}")[0] == 401)
    klopt("vreemde Host", vraag(bp, "GET", "/", {"Host": "kwaad.example"})[0] == 403)
    klopt("lokale Host op de buitenpoort", vraag(bp, "GET", "/api/stand", {"Host": f"127.0.0.1:{lp}"})[0] == 403)
    klopt("dubbele Host", vraag(bp, "GET", "/", {"Host": [f"127.0.0.1:{bp}", "kwaad.example"]})[0] in (400, 403))
    code, k, pagina = vraag(bp, "GET", "/login")
    klopt("loginpagina", code == 200 and b'name="wachtwoord"' in pagina and "set-cookie" not in k)
    klopt("beveiligingskoppen", k.get("x-frame-options") == ["SAMEORIGIN"] and k.get("x-content-type-options") == ["nosniff"] and "content-security-policy" in k)
    klopt("valse cookie", vraag(bp, "GET", "/api/stand", {"Cookie": "godchat=" + "a" * 43})[0] == 401)
    klopt("niets bij de app", app_geraakt() == 0)

    # 5. Inloggen
    klopt("login zonder Origin", vraag(bp, "POST", "/login", {"Content-Type": formulier["Content-Type"]}, f"wachtwoord={WW}".encode())[0] == 403)
    klopt("login van een andere site", vraag(bp, "POST", "/login", dict(formulier, Origin="http://kwaad.example"), f"wachtwoord={WW}".encode())[0] == 403)
    code, k, pagina = vraag(bp, "POST", "/login", formulier, b"wachtwoord=verkeerd-wachtwoord")
    klopt("fout wachtwoord", code == 401 and "set-cookie" not in k and b"klopt niet" in pagina)
    code, k, _ = vraag(bp, "POST", "/login", formulier, f"wachtwoord={WW}".encode())
    koek = (k.get("set-cookie") or [""])[0]
    klopt("goed wachtwoord", code == 303 and k.get("location") == ["/"] and koek.startswith("godchat="), str(code))
    klopt("cookie-eigenschappen", "HttpOnly" in koek and "SameSite=Strict" in koek and "Path=/" in koek and "Secure" not in koek)
    cookie = {"Cookie": koek.split(";")[0]}
    token = koek.split(";")[0].split("=", 1)[1]
    klopt("token niet op schijf", token not in open(slot.SESSIES, encoding="utf-8").read())
    app_geraakt()

    # 6. Ingelogd: door naar de app, met vertaalde Host en Origin
    code, _, inhoud = vraag(bp, "GET", "/api/stand?x=1", cookie)
    klopt("GET met cookie", code == 200 and inhoud == b"app" and gezien[-1] == ("GET", "/api/stand?x=1", f"127.0.0.1:{lp}", None), str(gezien[-1:]))
    code, _, _ = vraag(bp, "POST", "/api/zeg", dict(cookie, **eigen), b"{}")
    klopt("POST met cookie en Origin", code == 200 and gezien[-1] == ("POST", "/api/zeg", f"127.0.0.1:{lp}", f"http://127.0.0.1:{lp}"), str(gezien[-1:]))
    klopt("van buiten mogen proxykoppen wel", vraag(bp, "GET", "/api/stand", dict(cookie, **{"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "eigenaar@example.org"}))[0] == 200)
    klopt("de naam waaraan wa.py de buitenpoort herkent", getattr(buiten_srv.RequestHandlerClass, "_slot_lokale_poort", None) == lp and not hasattr(lokaal_srv.RequestHandlerClass, "_slot_lokale_poort"))
    klopt("POST met Sec-Fetch-Site same-origin", vraag(bp, "POST", "/api/zeg", dict(cookie, **{"Sec-Fetch-Site": "same-origin"}), b"{}")[0] == 200)
    klopt("ingelogd op /login -> /", vraag(bp, "GET", "/login", cookie)[0] == 303)
    code, _, inhoud = vraag(bp, "GET", "/api/slot", cookie)
    klopt("/api/slot", code == 200 and b'"buiten": true' in inhoud and b'"wa": false' in inhoud)
    n = app_geraakt()
    klopt("POST zonder Origin", vraag(bp, "POST", "/api/zeg", cookie, b"{}")[0] == 403)
    klopt("POST met vreemde Origin", vraag(bp, "POST", "/api/zeg", dict(cookie, Origin="http://kwaad.example"), b"{}")[0] == 403)
    klopt("POST met Origin null", vraag(bp, "POST", "/api/zeg", dict(cookie, Origin="null"), b"{}")[0] == 403)
    klopt("GET met vreemde Origin", vraag(bp, "GET", "/api/stand", dict(cookie, Origin="http://kwaad.example"))[0] == 403)
    klopt("toestel in hetzelfde netwerk (same-site)", vraag(bp, "GET", "/api/stand", dict(cookie, **{"Sec-Fetch-Site": "same-site"}))[0] == 403)
    klopt("plaatje van een andere site", vraag(bp, "GET", "/api/wa/media?x=1", dict(cookie, **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}))[0] == 403)
    klopt("in een kader van een andere site", vraag(bp, "GET", "/", dict(cookie, **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"}))[0] == 403)
    code, _, pagina = vraag(bp, "GET", "/api/stand", dict(cookie, **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}))
    klopt("link van elders -> tussenpagina, niet de app", code == 200 and b'http-equiv="refresh" content="0;url=/"' in pagina and not gezien)
    klopt("PROPFIND zonder cookie", vraag(bp, "PROPFIND", "/x", eigen)[0] == 401 and vraag(bp, "PROPFIND", "/x")[0] == 403)
    klopt("PROPFIND met cookie gaat door", vraag(bp, "PROPFIND", "/x", dict(cookie, **eigen))[0] == 200)
    klopt("HEAD kent de app niet", vraag(bp, "HEAD", "/", cookie)[0] == 405)
    klopt("OPTIONS van elders", vraag(bp, "OPTIONS", "/api/zeg", dict(cookie, Origin="http://kwaad.example"))[0] == 403)
    klopt("alleen de PROPFIND bij de app", app_geraakt() == 1)

    # 7. Paden die van buiten dicht zijn, ook als je ingelogd bent
    for pad in ("/slot", "/slot/", "/SLOT", "/api/tool", "/api/tool/x", "/wa", "/wa/", "/WA", "/api/wa/chats", "/api/wa/media?a=1",
                "/api/%77a/chats", "/%77a"):
        code = vraag(bp, "GET", pad, cookie)[0]
        klopt(f"dicht van buiten: {pad}", code == 404, str(code))
    klopt("POST /api/tool dicht", vraag(bp, "POST", "/api/tool", dict(cookie, **eigen), b"{}")[0] == 404)
    klopt("dubbele schuine streep vooraan (http.server maakt er /wa van)", vraag(bp, "GET", "//wa", cookie)[0] == 404)
    for pad in ("/wa;x=1", "/api/wa/chats;x", "/api/tool;x", "/wa#x", "/wa%3bx", "/wa%23x", "/api/wa/media;a?b=1",
                "/api//wa/x", "/api/../wa", "/api/wa%2fchats", "/x%2e%2e/wa", "/a\\b", "/a%00b", "wa", "http://kwaad.example/wa", "/café"):
        code = vraag(bp, "GET", pad, cookie)[0]
        klopt(f"krom pad geweigerd: {pad!r}", code == 400, str(code))
    klopt("niets bij de app", app_geraakt() == 0)
    klopt("hekje in de vraag mag (de app leest hetzelfde pad)", vraag(bp, "GET", "/api/stand?a=1#b;c", cookie)[0] == 200 and app_geraakt() == 1)
    klopt("/watten is geen WhatsApp-pad", vraag(bp, "GET", "/watten", cookie)[0] == 200 and app_geraakt() == 1)
    slot.zet_wa(True)
    klopt("WhatsApp-tab aan: /wa door", vraag(bp, "GET", "/wa", cookie)[0] == 200 and vraag(bp, "GET", "/api/wa/chats", cookie)[0] == 200)
    klopt("WhatsApp-tab aan: /slot en /api/tool blijven dicht", vraag(bp, "GET", "/slot", cookie)[0] == 404 and vraag(bp, "GET", "/api/tool", cookie)[0] == 404)
    slot.zet_wa(False)
    klopt("WhatsApp-tab weer uit", vraag(bp, "GET", "/wa", cookie)[0] == 404)
    app_geraakt()

    # 8. Uitloggen, en een nieuw wachtwoord logt iedereen uit
    klopt("uitloggen alleen met POST", vraag(bp, "GET", "/loguit", cookie)[0] == 405)
    code, k, _ = vraag(bp, "POST", "/loguit", dict(cookie, **eigen))
    klopt("uitloggen", code == 303 and "Max-Age=0" in (k.get("set-cookie") or [""])[0])
    klopt("cookie na uitloggen waardeloos", vraag(bp, "GET", "/api/stand", cookie)[0] == 401)
    _, k, _ = vraag(bp, "POST", "/login", formulier, f"wachtwoord={WW}".encode())
    cookie = {"Cookie": k["set-cookie"][0].split(";")[0]}
    klopt("opnieuw ingelogd", vraag(bp, "GET", "/api/stand", cookie)[0] == 200)
    slot.zet_wachtwoord(WW + "-nieuw", "test")
    klopt("nieuw wachtwoord logt uit", vraag(bp, "GET", "/api/stand", cookie)[0] == 401)
    klopt("oud wachtwoord werkt niet meer", vraag(bp, "POST", "/login", formulier, f"wachtwoord={WW}".encode())[0] == 401)
    _, k, _ = vraag(bp, "POST", "/login", formulier, f"wachtwoord={WW}-nieuw".encode())
    cookie = {"Cookie": k["set-cookie"][0].split(";")[0]}
    code, _, pagina = vraag(lp, "GET", "/slot")
    klopt("toestel staat op /slot", code == 200 and b"Ingelogde toestellen" in pagina and b"Log uit" in pagina)
    vinger = next(iter(slot._lees(slot.SESSIES)))[:16]
    code, _, _ = vraag(lp, "POST", "/slot", lok, f"tik={slot._TIK}&actie=uitloggen&sessie={vinger}".encode())
    klopt("uitloggen vanaf /slot", code == 200 and vraag(bp, "GET", "/api/stand", cookie)[0] == 401)

    # 9. Te veel pogingen: pauze, ook voor het goede wachtwoord
    slot.zet_wachtwoord(WW, "test")
    klok = [1_000_000.0]
    slot._nu = lambda: klok[0]
    codes = [vraag(bp, "POST", "/login", formulier, b"wachtwoord=gok-nummer-%d" % i)[0] for i in range(slot.VRIJE_POGINGEN)]
    klopt("vijfde misser start de pauze", codes == [401] * (slot.VRIJE_POGINGEN - 1) + [429], str(codes))
    code, k, pagina = vraag(bp, "POST", "/login", formulier, f"wachtwoord={WW}".encode())
    klopt("in de pauze helpt het goede wachtwoord niet", code == 429 and "set-cookie" not in k and b"Te veel pogingen" in pagina)
    klok[0] += 31
    klopt("na de pauze weer een kans (mis verdubbelt)", vraag(bp, "POST", "/login", formulier, b"wachtwoord=nog-een-gok")[0] == 429)
    klok[0] += 59
    klopt("pauze is nu een minuut", vraag(bp, "POST", "/login", formulier, f"wachtwoord={WW}".encode())[0] == 429)
    klok[0] += 2
    code, k, _ = vraag(bp, "POST", "/login", formulier, f"wachtwoord={WW}".encode())
    klopt("na de pauze werkt het goede wachtwoord", code == 303 and "set-cookie" in k)
    klopt("teller terug op nul", slot._pogingen["mis"] == 0)
    klopt("te lang formulier", vraag(bp, "POST", "/login", formulier, b"wachtwoord=" + b"a" * 5000)[0] == 400)

    # 10. Verlopen en vernieuwen
    cookie = {"Cookie": k["set-cookie"][0].split(";")[0]}
    s = dict(slot._lees(slot.SESSIES))
    sleutel = next(iter(s))
    slot._schrijf(slot.SESSIES, {sleutel: dict(s[sleutel], gezien=s[sleutel]["gezien"] - 2 * 86400)})
    code, k, _ = vraag(bp, "GET", "/api/stand", cookie)
    klopt("na een dag wordt de cookie vernieuwd", code == 200 and (k.get("set-cookie") or [""])[0].startswith(cookie["Cookie"]))
    klopt("daarna niet telkens opnieuw", "set-cookie" not in vraag(bp, "GET", "/api/stand", cookie)[1])
    s = dict(slot._lees(slot.SESSIES))
    slot._schrijf(slot.SESSIES, {sleutel: dict(s[sleutel], gezien=s[sleutel]["gezien"] - (slot.SESSIE_DAGEN + 1) * 86400)})
    klopt("verlopen sessie", vraag(bp, "GET", "/api/stand", cookie)[0] == 401)
    with open(slot.SESSIES, "w", encoding="utf-8") as fh:
        fh.write("{kapot")
    klopt("kapot sessiebestand = niemand ingelogd", vraag(bp, "GET", "/api/stand", cookie)[0] == 401)

    # 11. Een https-adres (zoals achter tailscale serve): Secure-cookie met __Host-, en HSTS
    slot.zet_buiten("https://Laptop.Voorbeeld.example.net/")
    ts = {"Host": "laptop.voorbeeld.example.net", "Origin": "https://laptop.voorbeeld.example.net", "Content-Type": "application/x-www-form-urlencoded"}
    klopt("oude adres werkt niet meer", vraag(bp, "GET", "/login")[0] == 403)
    code, k, _ = vraag(bp, "POST", "/login", ts, f"wachtwoord={WW}".encode())
    koek = (k.get("set-cookie") or [""])[0]
    klopt("https: cookie met __Host- en Secure", code == 303 and koek.startswith("__Host-godchat=") and "Secure" in koek)
    klopt("https: HSTS", "strict-transport-security" in k)
    hcookie = {"Host": ts["Host"], "Cookie": koek.split(";")[0]}
    code, _, _ = vraag(bp, "POST", "/api/zeg", dict(hcookie, Origin=ts["Origin"]), b"{}")
    klopt("https: door naar de app als lokaal", code == 200 and gezien[-1][2:] == (f"127.0.0.1:{lp}", f"http://127.0.0.1:{lp}"), str(gezien[-1:]))
    klopt("https: http-Origin geweigerd", vraag(bp, "POST", "/api/zeg", dict(hcookie, Origin="http://laptop.voorbeeld.example.net"), b"{}")[0] == 403)
    klopt("https: cookie zonder __Host- telt niet", vraag(bp, "GET", "/api/stand", {"Host": ts["Host"], "Cookie": "godchat=" + koek.split(";")[0].split("=", 1)[1]})[0] == 401)

    # 12. Buitenadres uit: alles weer dicht; kapotte instelling = dicht
    slot.zet_buiten(None)
    klopt("buitenadres uit = dicht", vraag(bp, "GET", "/api/stand", hcookie)[0] == 503)
    with open(slot.INSTELLING, "w", encoding="utf-8") as fh:
        fh.write("[1, 2")
    klopt("kapotte instelling = dicht", vraag(bp, "GET", "/api/stand", hcookie)[0] == 503)
    klopt("lokaal werkt nog altijd", vraag(lp, "GET", "/api/stand")[0] == 200 and vraag(lp, "GET", "/slot")[0] == 200)
    slot.zet_buiten(adres)
    slot.zet_wachtwoord(WW, "test")
    _, k, _ = vraag(bp, "POST", "/login", dict(formulier, **{"X-Forwarded-For": "6.6.6.6, 100.64.0.7", "User-Agent": "Mozilla/5.0 (iPhone) <b>Safari/605"}), f"wachtwoord={WW}".encode())
    token = k["set-cookie"][0].split(";")[0].split("=", 1)[1]
    laatste = open(slot.LOGBOEK, encoding="utf-8").read().strip().splitlines()[-1]
    klopt("logboek: het adres dat de proxy zet, niet dat van de klant", "van 100.64.0.7" in laatste and "6.6.6.6" not in laatste and "iPhone" in laatste, laatste)
    code, _, pagina = vraag(lp, "GET", "/slot")
    klopt("/slot toont het toestel zonder opmaak van buiten", b"100.64.0.7" in pagina and b"<b>" not in pagina)

    # 13. Een uitlog uit de terminal (ander proces) mag niet ongedaan raken door een 'gezien' dat de app net schrijft
    sleutel = slot._vinger(token)
    s = dict(slot._lees(slot.SESSIES))
    slot._schrijf(slot.SESSIES, {sleutel: dict(s[sleutel], gezien=s[sleutel]["gezien"] - 600)})
    echt_lees, tel = slot._lees, [0]

    def lees_met_uitlog(pad):
        data = echt_lees(pad)
        if pad == slot.SESSIES:
            tel[0] += 1
            if tel[0] == 1:
                slot._schrijf(slot.SESSIES, {})   # alsof de terminal net na het eerste lezen iedereen uitlogde
        return data

    slot._lees = lees_met_uitlog
    geldig = slot._sessie_ok(token)
    slot._lees = echt_lees
    klopt("uitlog tussendoor blijft uitlog", echt_lees(slot.SESSIES) == {} and geldig == (False, False), str(geldig))
    logboek = open(slot.LOGBOEK, encoding="utf-8").read()
    klopt("logboek zonder wachtwoorden", WW not in logboek and "gok-nummer" not in logboek and "mislukte login" in logboek and "ingelogd" in logboek)

    shutil.rmtree(MAP, ignore_errors=True)
    print(f"{uitslag['goed']} goed, {uitslag['fout']} fout")
    sys.exit(1 if uitslag["fout"] else 0)


if __name__ == "__main__":
    main()
