#!/usr/bin/env python3
"""Het slot van de GodChat-app: een wachtwoord-login voor alles wat van buiten deze Mac komt.

De app blijft op 127.0.0.1:<poort> luisteren zoals altijd, zonder login. Daarnaast luistert een tweede poort, de
buitenpoort (<poort> + 20, ook alleen op 127.0.0.1). Alleen die poort mag ooit achter een tunnel hangen (bv.
`tailscale serve`). Alles
wat daar binnenkomt gaat eerst langs dit bestand en komt pas met een geldige sessie-cookie bij de app, die het dan ziet
als een gewone lokale aanvraag (Host en Origin worden hier vertaald). server.py kent dit bestand alleen in main().

  python3 -I tools/god-chat/app/slot.py --status                  wat er ingesteld is en wie ingelogd is
  python3 -I tools/god-chat/app/slot.py --zet                     wachtwoord kiezen (typen in de terminal, niet in een chat)
  python3 -I tools/god-chat/app/slot.py --buiten https://<naam>   het adres waarop de app van buiten bereikbaar wordt
  python3 -I tools/god-chat/app/slot.py --buiten uit              geen adres meer: de buitenpoort weigert alles
  python3 -I tools/god-chat/app/slot.py --wa aan|uit              de WhatsApp-tab van buiten toelaten (standaard uit)
  python3 -I tools/god-chat/app/slot.py --uitloggen               alle toestellen uitloggen

Het wachtwoord kiezen kan ook op het scherm: http://127.0.0.1:<poort>/slot (alleen op de Mac zelf).
Bewaard wordt alleen een scrypt-hash, in ~/.config/godchat/ (buiten de werkmap). Zonder wachtwoord of zonder buitenadres
weigert de buitenpoort alles. Een adres zetten of de WhatsApp-tab toelaten is openzetten: alleen op het woord van de
eigenaar. Test: slot_test.py. Zie CLAUDE.md in deze map.
"""
import argparse
import contextlib
import fcntl
import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import threading
import time
import unicodedata
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

MAP = os.environ.get("GODCHAT_SLOT_MAP") or os.path.expanduser("~/.config/godchat")
INSTELLING = os.path.join(MAP, "slot.json")     # hash van het wachtwoord, buitenadres, WhatsApp-tab aan/uit
SESSIES = os.path.join(MAP, "sessies.json")     # ingelogde toestellen (alleen een vingerafdruk van hun cookie)
LOGBOEK = os.path.join(MAP, "slot.log")

POORT_ERBIJ = 20            # buitenpoort = poort van de app + 20
SESSIE_DAGEN = 30           # zo lang blijft een toestel ingelogd na zijn laatste bezoek
MAX_SESSIES = 8
MIN_LENGTE = 8
VRIJE_POGINGEN = 5          # daarna een pauze die telkens verdubbelt: 30 s, 1 min, 2 min ... tot een kwartier
ALLEEN_LOKAAL = ("/slot", "/api/tool")          # nooit via de buitenpoort
WA_PADEN = ("/wa", "/api/wa")                   # de WhatsApp-tab: van buiten alleen als "wa" aan staat
VERDACHT = ("..", "//", "\\", ";", "#", "%2f", "%5c", "%2e", "%00", "%3b", "%23")   # in een pad van buiten: weigeren
# (";" en "#": server.py leest het pad met urlparse, en die knipt ";params" en "#fragment" eraf: /wa;x wordt daar /wa)
KOPPEN_BUITEN = (("x-frame-options", "SAMEORIGIN"), ("x-content-type-options", "nosniff"),
                 ("referrer-policy", "same-origin"), ("cross-origin-resource-policy", "same-origin"),
                 ("cross-origin-opener-policy", "same-origin"))
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"

_bestandsslot = threading.RLock()
_vast_staat = {"diepte": 0, "fh": None}
_cache = {}                 # pad -> (wijzigingstijd, inhoud)
_pogingen = {"mis": 0, "tot": 0.0}
_pogingslot = threading.Lock()
_TIK = secrets.token_urlsafe(18)   # staat in het formulier van /slot; een pagina van elders kent hem niet
_proxykop_gemeld = [0.0]              # laatste keer dat een proxy aan de lokale poort in het logboek kwam
_nu = time.time                    # los te zetten in de test


# --- bestanden ----------------------------------------------------------------------------------------------------

@contextlib.contextmanager
def _vast():
    """Eén schrijver tegelijk, ook tussen de app en de terminal (flock): wie leest, wijzigt en schrijft doet dat hierbinnen.
    Zonder dit kan een uitlog uit de terminal ongedaan raken door een 'gezien' dat de app net wegschreef."""
    with _bestandsslot:
        if _vast_staat["diepte"] == 0:
            os.makedirs(MAP, mode=0o700, exist_ok=True)
            fd = os.open(os.path.join(MAP, ".slot"), os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            _vast_staat["fh"] = fd
        _vast_staat["diepte"] += 1
        try:
            yield
        finally:
            _vast_staat["diepte"] -= 1
            if _vast_staat["diepte"] == 0:
                fd, _vast_staat["fh"] = _vast_staat["fh"], None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def _lees(pad):
    """JSON uit de slotmap, met een cache op wijzigingstijd: de terminal mag het bestand wijzigen terwijl de app draait.
    Een kapot of ontbrekend bestand is leeg, en leeg betekent dicht. Wijzig het antwoord nooit ter plaatse."""
    with _bestandsslot:
        try:
            mt = os.stat(pad).st_mtime_ns
        except OSError:
            _cache.pop(pad, None)
            return {}
        oud = _cache.get(pad)
        if oud and oud[0] == mt:
            return oud[1]
        try:
            with open(pad, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        _cache[pad] = (mt, data)
        return data


def _schrijf(pad, data):
    with _bestandsslot:
        os.makedirs(MAP, mode=0o700, exist_ok=True)
        try:
            os.chmod(MAP, 0o700)
        except OSError:
            pass
        tmp = f"{pad}.{os.getpid()}.{threading.get_ident()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, pad)
        _cache.pop(pad, None)


def _schoon(tekst, n=120):
    return "".join(c if c.isprintable() else "?" for c in str(tekst or ""))[:n]


def _log(regel):
    try:
        with _bestandsslot:
            os.makedirs(MAP, mode=0o700, exist_ok=True)
            fd = os.open(LOGBOEK, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {_schoon(regel, 300)}\n")
    except OSError:
        pass


def _logstaart(n):
    try:
        with open(LOGBOEK, encoding="utf-8") as fh:
            return [r.rstrip("\n") for r in fh.readlines()[-n:]]
    except OSError:
        return []


# --- wachtwoord ---------------------------------------------------------------------------------------------------

def _norm(wachtwoord):
    return unicodedata.normalize("NFKC", str(wachtwoord))


def _hash(wachtwoord, n=2 ** 15, r=8, p=1):
    zout = secrets.token_bytes(16)
    sleutel = hashlib.scrypt(wachtwoord.encode("utf-8"), salt=zout, n=n, r=r, p=p, dklen=32, maxmem=2 ** 27)
    return {"soort": "scrypt", "n": n, "r": r, "p": p, "zout": zout.hex(), "sleutel": sleutel.hex()}


def _klopt(wachtwoord, h):
    try:
        if h.get("soort") != "scrypt":
            return False
        proef = hashlib.scrypt(wachtwoord.encode("utf-8"), salt=bytes.fromhex(h["zout"]), n=int(h["n"]), r=int(h["r"]),
                               p=int(h["p"]), dklen=32, maxmem=2 ** 27)
        return hmac.compare_digest(proef, bytes.fromhex(h["sleutel"]))
    except (AttributeError, KeyError, ValueError, TypeError):
        return False


def zet_wachtwoord(wachtwoord, bron):
    """Nieuw wachtwoord; logt elk toestel uit. Gooit ValueError met een zin voor het scherm."""
    if not hasattr(hashlib, "scrypt"):   # de python3 van macOS zelf (LibreSSL) heeft geen scrypt
        raise ValueError("Deze Python kent geen scrypt (zo is de python3 van macOS zelf). Start de app en slot.py met "
                         "een python3 van Homebrew of python.org.")
    wachtwoord = _norm(wachtwoord)
    if len(wachtwoord) < MIN_LENGTE:
        raise ValueError(f"Het wachtwoord moet minstens {MIN_LENGTE} tekens hebben.")
    if len(wachtwoord) > 256:
        raise ValueError("Het wachtwoord is te lang.")
    with _vast():
        inst = dict(_lees(INSTELLING))
        inst.update(versie=1, hash=_hash(wachtwoord), gezet=time.strftime("%Y-%m-%d %H:%M"))
        _schrijf(INSTELLING, inst)
        _schrijf(SESSIES, {})
    with _pogingslot:
        _pogingen.update(mis=0, tot=0.0)
    _log(f"wachtwoord gezet ({bron}); alle toestellen uitgelogd")


def _probeer(wachtwoord):
    """(gelukt, seconden wachten). Eén poging tegelijk; na VRIJE_POGINGEN missers een pauze die telkens verdubbelt.
    De teller is er één voor iedereen, omdat geen kop van de proxy bewijst wie er klopt. Gevolg, bewust zo gelaten: een
    toestel in het netwerk dat blijft gokken houdt nieuwe logins tegen (hoogstens een kwartier per keer). Wie al
    ingelogd is merkt daar niets van; de uitweg is dat toestel uit Tailscale halen."""
    with _pogingslot:
        nu = _nu()
        if nu < _pogingen["tot"]:
            return False, int(_pogingen["tot"] - nu) + 1
        h = _lees(INSTELLING).get("hash")
        if h and len(wachtwoord) <= 256 and _klopt(_norm(wachtwoord), h):
            _pogingen.update(mis=0, tot=0.0)
            return True, 0
        _pogingen["mis"] += 1
        extra = _pogingen["mis"] - VRIJE_POGINGEN
        if extra >= 0:
            _pogingen["tot"] = nu + min(900, 30 * 2 ** min(extra, 10))
            return False, int(_pogingen["tot"] - nu)
        return False, 0


# --- sessies ------------------------------------------------------------------------------------------------------

def _vinger(token):
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def _geldig(v, nu):
    try:
        return nu - float(v["gezien"]) < SESSIE_DAGEN * 86400
    except (KeyError, TypeError, ValueError):
        return False


def _sessie_nieuw(toestel, van):
    token = secrets.token_urlsafe(32)
    with _vast():
        nu = time.time()
        s = {k: v for k, v in _lees(SESSIES).items() if _geldig(v, nu)}
        while len(s) >= MAX_SESSIES:
            del s[min(s, key=lambda k: s[k].get("gezien", 0))]
        s[_vinger(token)] = {"gemaakt": nu, "gezien": nu, "toestel": toestel, "van": van}
        _schrijf(SESSIES, s)
    return token


def _sessie_ok(token):
    """(geldig, cookie vernieuwen). Schrijft 'gezien' hoogstens om de vijf minuten bij."""
    if not token or len(token) > 200:
        return False, False
    sleutel = _vinger(token)
    v = _lees(SESSIES).get(sleutel)
    nu = time.time()
    if not isinstance(v, dict) or not _geldig(v, nu):
        return False, False
    oud = nu - float(v["gezien"])
    if oud > 300:
        with _vast():   # opnieuw lezen onder het slot: is het toestel intussen uitgelogd, dan blijft het uitgelogd
            s = _lees(SESSIES)
            if not isinstance(s.get(sleutel), dict):
                return False, False
            _schrijf(SESSIES, dict(s, **{sleutel: dict(s[sleutel], gezien=nu)}))
    return True, oud > 86400


def _sessie_weg(token=None, vinger=None):
    """Eén toestel uitloggen (op cookie of op het begin van zijn vingerafdruk), of zonder argument: allemaal."""
    with _vast():
        s = _lees(SESSIES)
        if token is None and vinger is None:
            nieuw = {}
        else:
            doel = _vinger(token) if token is not None else None
            nieuw = {k: v for k, v in s.items() if not (k == doel or (vinger and len(vinger) >= 12 and k.startswith(vinger)))}
        if nieuw != s:
            _schrijf(SESSIES, nieuw)
        return len(s) - len(nieuw)


# --- instellingen -------------------------------------------------------------------------------------------------

def _adres(tekst):
    """Een buitenadres in vaste vorm (schema + host, kleine letters, zonder pad), of ValueError."""
    u = urlsplit(str(tekst).strip())
    if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/") or u.query or u.fragment or "@" in u.netloc:
        raise ValueError("Geef een adres als https://<naam>.<domein>, zonder pad.")
    return f"{u.scheme}://{u.netloc}".lower()


def zet_buiten(adres):
    with _vast():
        inst = dict(_lees(INSTELLING))
        inst["buiten"] = [] if adres is None else [_adres(adres)]
        _schrijf(INSTELLING, inst)
    _log("buitenadres: " + (inst["buiten"][0] if inst["buiten"] else "uit"))


def zet_wa(aan):
    with _vast():
        inst = dict(_lees(INSTELLING))
        inst["wa"] = bool(aan)
        _schrijf(INSTELLING, inst)
    _log("WhatsApp-tab van buiten: " + ("aan" if aan else "uit"))


def _adressen(inst):
    buiten = inst.get("buiten")
    return [a.strip().lower().rstrip("/") for a in (buiten if isinstance(buiten, list) else []) if isinstance(a, str) and a.strip()]


def toestand():
    inst = _lees(INSTELLING)
    if not inst.get("hash"):
        return "dicht: nog geen wachtwoord"
    if not _adressen(inst):
        return "dicht: nog geen buitenadres"
    return "open voor " + ", ".join(_adressen(inst))


# --- antwoorden ---------------------------------------------------------------------------------------------------

def _stuur(h, code, body, soort="text/html; charset=utf-8", koppen=()):
    if isinstance(body, str):
        body = body.encode("utf-8")
    h.close_connection = True   # ook als de app ooit HTTP/1.1 spreekt: na een antwoord van het slot niets meer op deze lijn
    h.send_response(code)
    h.send_header("content-type", soort)
    h.send_header("cache-control", "no-store")
    if soort.startswith("text/html"):
        h.send_header("content-security-policy", CSP)
    h.send_header("content-length", str(len(body)))
    for k, v in koppen:
        h.send_header(k, v)
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)


def _tekst(h, code, tekst):
    _stuur(h, code, tekst, "text/plain; charset=utf-8")


def _json(h, data, code=200):
    _stuur(h, code, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")


def _naar(h, pad, koppen=()):
    _stuur(h, 303, "", "text/plain; charset=utf-8", (("location", pad),) + tuple(koppen))


STIJL = """
:root { --bg:#f6f5f1; --vlak:#fff; --lijn:#e4e1d8; --inkt:#22211e; --zacht:#6f6b62; --accent:#3f6b52; --fout:#b4443d; }
@media (prefers-color-scheme: dark) { :root { --bg:#171716; --vlak:#201f1d; --lijn:#34322e; --inkt:#ecebe6; --zacht:#a7a399; --accent:#8cc0a0; --fout:#e07a73; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--inkt); font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif; }
main { max-width:440px; margin:0 auto; padding:max(28px,env(safe-area-inset-top)) 20px 40px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 8px; }
p { margin:8px 0; } a { color:var(--accent); } .zacht { color:var(--zacht); font-size:14px; } .fout { color:var(--fout); } .goed { color:var(--accent); }
form { margin:14px 0; } label { display:block; font-size:14px; color:var(--zacht); margin:12px 0 4px; }
input[type=password], input[type=text] { width:100%; padding:12px; font:inherit; color:inherit; background:var(--vlak); border:1px solid var(--lijn); border-radius:10px; }
button { margin-top:14px; padding:12px 18px; font:inherit; font-weight:600; color:var(--bg); background:var(--accent); border:0; border-radius:10px; }
button.klein { margin:0; padding:6px 10px; font-size:13px; font-weight:400; color:var(--inkt); background:none; border:1px solid var(--lijn); }
ul { list-style:none; padding:0; margin:0; } li { display:flex; gap:10px; align-items:center; justify-content:space-between; padding:10px 0; border-top:1px solid var(--lijn); font-size:14px; }
li form { margin:0; } pre { font-size:12px; white-space:pre-wrap; word-break:break-word; color:var(--zacht); background:var(--vlak); border:1px solid var(--lijn); border-radius:10px; padding:10px; }
"""


def _pagina(titel, inhoud, kop=""):
    return ('<!doctype html><html lang="nl"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'<meta name="robots" content="noindex">{kop}<title>{html.escape(titel)}</title><style>{STIJL}</style></head>'
            f'<body><main>{inhoud}</main></body></html>')


def _loginpagina(melding=""):
    fout = f'<p class="fout">{html.escape(melding)}</p>' if melding else ""
    return _pagina("GodChat", '<h1>GodChat</h1><p class="zacht">Log in om verder te gaan.</p>' + fout +
                   '<form method="post" action="/login">'
                   '<input type="text" name="gebruiker" value="godchat" autocomplete="username" hidden>'
                   '<label for="w">Wachtwoord</label>'
                   '<input id="w" type="password" name="wachtwoord" autocomplete="current-password" autofocus required>'
                   '<button type="submit">Log in</button></form>')


def _toestel(ua):
    ua = ua or ""
    soort = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                                 ("Windows", "Windows"), ("Linux", "Linux")) if k in ua), "onbekend toestel")
    browser = next((n for k, n in (("Edg/", "Edge"), ("CriOS", "Chrome"), ("FxiOS", "Firefox"), ("Chrome/", "Chrome"),
                                   ("Firefox/", "Firefox"), ("Safari/", "Safari"), ("curl", "curl")) if k in ua), "")
    return soort + (" · " + browser if browser else "")


def _wie(h):
    """Voor het logboek en de lijst van toestellen. Deze koppen zet de proxy; ze bewijzen niets en openen niets."""
    van = _schoon((h.headers.get("X-Forwarded-For") or "").split(",")[-1].strip(), 60) or "?"   # de laatste zet de proxy
    login = _schoon(h.headers.get("Tailscale-User-Login"), 80)
    return van + (f" ({login})" if login else ""), _toestel(h.headers.get("User-Agent"))


def _cookie(h, naam):
    for kop in h.headers.get_all("Cookie") or []:
        for deel in kop.split(";"):
            k, _, v = deel.strip().partition("=")
            if k == naam:
                return v
    return ""


def _cookienaam(https):
    return "__Host-godchat" if https else "godchat"


def _cookiekop(https, token):
    """Set-Cookie voor de sessie; een leeg token wist hem."""
    duur = SESSIE_DAGEN * 86400 if token else 0
    return (f"{_cookienaam(https)}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={duur}" + ("; Secure" if https else ""))


def _formulier(h, grens):
    try:
        lengte = int(h.headers.get("content-length") or 0)
    except ValueError:
        lengte = 0
    if not 0 < lengte <= grens:
        return None
    velden = parse_qs(h.rfile.read(lengte).decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in velden.items()}


# --- de buitenpoort -----------------------------------------------------------------------------------------------

def _bewaak(h, methode):
    """True = de aanvraag mag door naar de app; Host en Origin zijn dan vertaald naar lokaal. False = hier beantwoord.
    Volgorde: opengezet? pad netjes? juiste Host? van de eigen pagina? ingelogd? toegelaten pad?"""
    inst = _lees(INSTELLING)
    adressen = _adressen(inst)
    if not inst.get("hash") or not adressen:
        _tekst(h, 503, "GodChat is van buiten niet opengezet.")
        return False

    padruw = (h.path or "").split("?", 1)[0]
    laag = padruw.lower()
    if (not padruw.startswith("/") or any(v in laag for v in VERDACHT) or any(not 32 < ord(c) < 127 for c in padruw)
            or urlparse(h.path).path != padruw):   # de app routeert op urlparse: leest die iets anders, dan weigeren
        _tekst(h, 400, "ongeldig adres")
        return False

    hosts = h.headers.get_all("Host") or []
    host = hosts[0].strip().lower() if len(hosts) == 1 else ""
    oorsprong = next((a for a in adressen if urlsplit(a).netloc == host), None)
    if not oorsprong:
        _tekst(h, 403, "onbekend adres")
        return False
    https = oorsprong.startswith("https://")
    h._slot_https = https

    # Alleen de eigen pagina, of een adres dat de eigenaar zelf intikt. Een toestel in hetzelfde Tailscale-netwerk is
    # "same-site": de cookie zou meegaan, dus dit hangt niet op SameSite alleen.
    sfs = (h.headers.get("Sec-Fetch-Site") or "").lower()
    if sfs and sfs not in ("same-origin", "none"):
        if (methode == "GET" and (h.headers.get("Sec-Fetch-Mode") or "").lower() == "navigate"
                and (h.headers.get("Sec-Fetch-Dest") or "document").lower() == "document"):
            # een link van elders: van hieruit opnieuw openen, dan gaat de cookie mee
            _stuur(h, 200, _pagina("GodChat", '<p><a href="/">Open GodChat</a></p>',
                                   '<meta http-equiv="refresh" content="0;url=/">'))
            return False
        _tekst(h, 403, "alleen vanaf de eigen pagina")
        return False
    herkomst = h.headers.get("Origin")
    if herkomst is not None and herkomst.strip().lower() != oorsprong:
        _tekst(h, 403, "alleen vanaf de eigen pagina")
        return False
    if methode not in ("GET", "HEAD") and herkomst is None and sfs != "same-origin":
        _tekst(h, 403, "alleen vanaf de eigen pagina (geen Origin)")
        return False

    token = _cookie(h, _cookienaam(https))
    ingelogd, vernieuw = _sessie_ok(token)

    if padruw == "/login":
        if methode == "GET":
            if ingelogd:
                _naar(h, "/")
            else:
                _stuur(h, 200, _loginpagina())
        elif methode == "POST":
            _login(h, https)
        else:
            _tekst(h, 405, "methode niet toegestaan")
        return False

    if not ingelogd:
        if methode == "GET" and "text/html" in (h.headers.get("Accept") or ""):
            _naar(h, "/login")
        else:
            _tekst(h, 401, "niet ingelogd")
        return False
    if vernieuw:
        h._slot_cookie = _cookiekop(https, token)

    if padruw == "/loguit":
        if methode != "POST":
            _tekst(h, 405, "methode niet toegestaan")
            return False
        _sessie_weg(token=token)
        van, toestel = _wie(h)
        _log(f"uitgelogd | {toestel} | van {van}")
        h._slot_cookie = None
        _naar(h, "/login", (("set-cookie", _cookiekop(https, "")),))
        return False
    if padruw == "/api/slot" and methode == "GET":
        _json(h, {"buiten": True, "ingelogd": True, "wa": bool(inst.get("wa"))})
        return False

    # Het pad zoals wij het lezen en zoals de app het leest (urlparse): geen van beide mag een dicht pad zijn.
    dicht = ALLEEN_LOKAAL + (() if inst.get("wa") else WA_PADEN)
    for pad in {unquote(padruw).lower(), unquote(urlparse(h.path).path).lower()}:
        if any(pad == p or pad.startswith(p + "/") for p in dicht):
            _tekst(h, 404, "niet gevonden")
            return False

    lokaal = f"127.0.0.1:{h._slot_lokale_poort}"
    del h.headers["Host"]
    h.headers["Host"] = lokaal
    if herkomst is not None:
        del h.headers["Origin"]
        h.headers["Origin"] = "http://" + lokaal
    return True


def _login(h, https):
    velden = _formulier(h, 4096)
    if velden is None:
        return _tekst(h, 400, "geen formulier")
    gelukt, wacht = _probeer(velden.get("wachtwoord", ""))
    van, toestel = _wie(h)
    if gelukt:
        _log(f"ingelogd | {toestel} | van {van}")
        return _naar(h, "/", (("set-cookie", _cookiekop(https, _sessie_nieuw(toestel, van))),))
    _log(f"mislukte login | {toestel} | van {van}" + (f" | pauze {wacht} s" if wacht else ""))
    if wacht:
        minuten = (wacht + 59) // 60
        return _stuur(h, 429, _loginpagina(f"Te veel pogingen. Probeer het over {wacht} seconden opnieuw." if wacht < 90
                                           else f"Te veel pogingen. Probeer het over {minuten} minuten opnieuw."))
    _stuur(h, 401, _loginpagina("Dat wachtwoord klopt niet."))


def _maak_buiten(Basis, lokale_poort):
    """De app-handler met het slot ervoor. Elke methode gaat eerst langs _bewaak; een fout in het slot laat niets door."""
    if getattr(Basis, "_slot_lokaal", False):
        raise ValueError("de buitenpoort krijgt de kale app-handler, nooit die van lokaal() (daar zit /slot in)")

    class Buiten(Basis):
        _slot_lokale_poort = lokale_poort   # wa.py herkent hieraan wat langs het slot kwam: naam niet wijzigen
        _slot_https = False
        _slot_cookie = None

        def end_headers(self):
            for k, v in KOPPEN_BUITEN:
                self.send_header(k, v)
            if self._slot_https:
                self.send_header("strict-transport-security", "max-age=15552000")
            if self._slot_cookie:
                self.send_header("set-cookie", self._slot_cookie)
                self._slot_cookie = None
            super().end_headers()

        def _slot_door(self, methode):
            try:
                door = _bewaak(self, methode)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as fout:   # dicht bij twijfel
                _log(f"FOUT in het slot: {type(fout).__name__}: {fout}")
                try:
                    _tekst(self, 500, "Fout in het slot.")
                except Exception:
                    pass
                return
            if not door:
                return
            doe = getattr(Basis, "do_" + methode, None)
            if doe is None:
                return _tekst(self, 405, "methode niet toegestaan")
            doe(self)

        def do_GET(self):
            self._slot_door("GET")

        def do_POST(self):
            self._slot_door("POST")

        def do_HEAD(self):
            self._slot_door("HEAD")

        def do_PUT(self):
            self._slot_door("PUT")

        def do_PATCH(self):
            self._slot_door("PATCH")

        def do_DELETE(self):
            self._slot_door("DELETE")

        def do_OPTIONS(self):
            self._slot_door("OPTIONS")

    for naam in dir(Basis):   # kent de app nog een methode, dan gaat ook die eerst langs het slot
        if naam.startswith("do_") and naam not in Buiten.__dict__:
            setattr(Buiten, naam, (lambda m: lambda self: self._slot_door(m))(naam[3:]))
    return Buiten


def start_buiten(Basis, poort, nep=False):
    """Start de buitenpoort in een eigen draad. Lukt dat niet, dan draait de app gewoon lokaal verder."""
    if nep and not os.environ.get("GODCHAT_SLOT_MAP"):
        return None   # het nepscherm krijgt geen buitenpoort, behalve in een test met een eigen slotmap
    buiten = poort + POORT_ERBIJ
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", buiten), _maak_buiten(Basis, poort))
    except OSError as fout:
        print(f"Slot: buitenpoort {buiten} niet gestart ({fout}); de app is alleen lokaal bereikbaar", flush=True)
        return None
    threading.Thread(target=srv.serve_forever, name="slot-buiten", daemon=True).start()
    print(f"Slot: buitenpoort op 127.0.0.1:{buiten} ({toestand()})", flush=True)
    return srv


# --- het lokale scherm /slot --------------------------------------------------------------------------------------

def _via_proxy(h):
    """Draagt de aanvraag een kop die een proxy zet? Dan kwam ze niet rechtstreeks van deze Mac. Een klant van buiten
    kan zo'n kop wel toevoegen maar niet weghalen, dus dit vangt een proxy die per vergissing aan de lokale poort hangt."""
    for kop in h.headers.keys():
        kop = kop.lower()
        if kop in ("forwarded", "via", "x-real-ip") or kop.startswith(("x-forwarded-", "tailscale-")):
            return True
    return False


def _weiger_proxy(h):
    nu = time.time()
    if nu - _proxykop_gemeld[0] > 600:
        _proxykop_gemeld[0] = nu
        _log("aanvraag met proxykoppen op de lokale poort geweigerd: hangt er een proxy aan de verkeerde poort?")
    _tekst(h, 403, "Deze poort is alleen voor de Mac zelf. Van buiten gaat het via het slot.")


def _lokaal_mag(h, poort, post):
    eigen = (f"127.0.0.1:{poort}", f"localhost:{poort}")
    if (h.headers.get("Host") or "").lower() not in eigen:
        return "onbekende host"
    sfs = (h.headers.get("Sec-Fetch-Site") or "").lower()
    if sfs and sfs != "same-origin" and (post or sfs != "none"):
        return "alleen vanaf de eigen pagina"
    if post and (h.headers.get("Origin") or "").lower() not in tuple("http://" + e for e in eigen):
        return "alleen vanaf de eigen pagina"
    return None


def _slotpagina(poort, melding="", fout=""):
    inst = _lees(INSTELLING)
    e = html.escape
    delen = ['<h1>Slot</h1><p class="zacht">De login voor GodChat van buiten deze Mac. Op de Mac zelf blijft de app '
             'zonder login werken.</p>']
    if melding:
        delen.append(f'<p class="goed">{e(melding)}</p>')
    if fout:
        delen.append(f'<p class="fout">{e(fout)}</p>')
    adressen = _adressen(inst)
    delen.append("<p>" + (f"Wachtwoord gezet op {e(str(inst.get('gezet') or '?'))}." if inst.get("hash")
                          else "Er is nog geen wachtwoord.") + "<br>"
                 + (f"Van buiten bereikbaar op {e(', '.join(adressen))}." if adressen
                    else "Van buiten nog niet opengezet.") + "<br>"
                 + f"WhatsApp-tab van buiten: {'aan' if inst.get('wa') else 'uit'}.</p>")
    tik = f'<input type="hidden" name="tik" value="{e(_TIK)}">'
    delen.append(f'<h2>{"Nieuw wachtwoord" if inst.get("hash") else "Wachtwoord kiezen"}</h2>'
                 f'<form method="post" action="/slot">{tik}<input type="hidden" name="actie" value="zet">'
                 '<input type="text" name="gebruiker" value="godchat" autocomplete="username" hidden>'
                 f'<label for="a">Wachtwoord (minstens {MIN_LENGTE} tekens)</label>'
                 '<input id="a" type="password" name="wachtwoord" autocomplete="new-password" required>'
                 '<label for="b">Nog een keer</label>'
                 '<input id="b" type="password" name="herhaal" autocomplete="new-password" required>'
                 '<button type="submit">Bewaar</button></form>'
                 '<p class="zacht">Een nieuw wachtwoord logt elk toestel uit.</p>')
    nu = time.time()
    sessies = sorted(((k, v) for k, v in _lees(SESSIES).items() if _geldig(v, nu)), key=lambda kv: -kv[1].get("gezien", 0))
    delen.append("<h2>Ingelogde toestellen</h2>")
    if sessies:
        rijen = []
        for k, v in sessies:
            gezien = time.strftime("%d-%m %H:%M", time.localtime(float(v.get("gezien", 0))))
            rijen.append(f'<li><span>{e(_schoon(v.get("toestel"), 60))}<br><span class="zacht">laatst {gezien} · van '
                         f'{e(_schoon(v.get("van"), 80))}</span></span><form method="post" action="/slot">{tik}'
                         f'<input type="hidden" name="actie" value="uitloggen"><input type="hidden" name="sessie" '
                         f'value="{e(k[:16])}"><button class="klein" type="submit">Log uit</button></form></li>')
        delen.append("<ul>" + "".join(rijen) + "</ul>" + f'<form method="post" action="/slot">{tik}'
                     '<input type="hidden" name="actie" value="uitloggen"><button class="klein" type="submit">'
                     'Alle toestellen uitloggen</button></form>')
    else:
        delen.append('<p class="zacht">Geen.</p>')
    staart = _logstaart(12)
    if staart:
        delen.append("<h2>Laatste gebeurtenissen</h2><pre>" + e("\n".join(staart)) + "</pre>")
    delen.append(f'<p class="zacht">Buitenpoort: 127.0.0.1:{poort + POORT_ERBIJ} ({e(toestand())}). '
                 '<a href="/">Terug naar GodChat</a></p>')
    return _pagina("Slot · GodChat", "".join(delen))


def _lokaal_post(h, poort):
    velden = _formulier(h, 8192)
    if velden is None or not hmac.compare_digest(velden.get("tik", "").encode(), _TIK.encode()):
        return _stuur(h, 403, _slotpagina(poort, fout="Deze pagina was verlopen. Probeer het opnieuw."))
    actie = velden.get("actie")
    if actie == "zet":
        if velden.get("wachtwoord", "") != velden.get("herhaal", ""):
            return _stuur(h, 400, _slotpagina(poort, fout="De twee wachtwoorden zijn niet gelijk."))
        try:
            zet_wachtwoord(velden.get("wachtwoord", ""), "scherm op de Mac")
        except ValueError as fout:
            return _stuur(h, 400, _slotpagina(poort, fout=str(fout)))
        return _stuur(h, 200, _slotpagina(poort, melding="Het wachtwoord is bewaard."))
    if actie == "uitloggen":
        aantal = _sessie_weg(vinger=velden.get("sessie")) if velden.get("sessie") else _sessie_weg()
        _log(f"uitgelogd vanaf het scherm op de Mac: {aantal} toestel(len)")
        return _stuur(h, 200, _slotpagina(poort, melding=f"{aantal} toestel(len) uitgelogd."))
    _stuur(h, 400, _slotpagina(poort, fout="Onbekende actie."))


def lokaal(Basis, poort):
    """De app-handler voor de lokale poort, met het scherm /slot erbij. Al het andere gaat ongewijzigd door, behalve
    een aanvraag met proxykoppen: die hoort op de buitenpoort en wordt hier geweigerd."""

    def _pad(h):
        try:
            return urlsplit(h.path).path
        except ValueError:
            return ""

    def _van_buiten(h):
        try:
            via = _via_proxy(h)
        except Exception:   # dit is een vangnet; het mag de app op de Mac zelf nooit breken
            via = False
        if via:
            _weiger_proxy(h)
        return via

    class Lokaal(Basis):
        _slot_lokaal = True

        def do_GET(self):
            if _van_buiten(self):
                return
            pad = _pad(self)
            if pad == "/slot":
                reden = _lokaal_mag(self, poort, False)
                return _tekst(self, 403, reden) if reden else _stuur(self, 200, _slotpagina(poort))
            if pad == "/api/slot":
                return _json(self, {"buiten": False, "ingelogd": True, "wa": True})
            return super().do_GET()

        def do_POST(self):
            if _van_buiten(self):
                return
            if _pad(self) == "/slot":
                reden = _lokaal_mag(self, poort, True)
                return _tekst(self, 403, reden) if reden else _lokaal_post(self, poort)
            return super().do_POST()

    return Lokaal


# --- terminal -----------------------------------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--zet", action="store_true")
    g.add_argument("--buiten", metavar="ADRES|uit")
    g.add_argument("--wa", choices=("aan", "uit"))
    g.add_argument("--uitloggen", action="store_true")
    a = p.parse_args()
    try:
        if a.zet:
            eerste = getpass.getpass("Nieuw wachtwoord: ")
            if eerste != getpass.getpass("Nog een keer: "):
                raise ValueError("De twee wachtwoorden zijn niet gelijk.")
            zet_wachtwoord(eerste, "terminal")
            print("Bewaard. Elk toestel is uitgelogd.")
        elif a.buiten is not None:
            zet_buiten(None if a.buiten == "uit" else a.buiten)
            print("Slot: " + toestand())
        elif a.wa:
            zet_wa(a.wa == "aan")
            print("WhatsApp-tab van buiten: " + a.wa)
        elif a.uitloggen:
            print(f"{_sessie_weg()} toestel(len) uitgelogd.")
        else:
            inst = _lees(INSTELLING)
            print(f"Slotmap: {MAP}")
            print("Wachtwoord: " + (f"gezet op {inst.get('gezet') or '?'}" if inst.get("hash") else "nog niet gekozen"))
            print("Slot: " + toestand())
            print("WhatsApp-tab van buiten: " + ("aan" if inst.get("wa") else "uit"))
            nu = time.time()
            sessies = [v for v in _lees(SESSIES).values() if _geldig(v, nu)]
            print(f"Ingelogde toestellen: {len(sessies)}")
            for v in sessies:
                print(f"  {_schoon(v.get('toestel'), 60)} · laatst {time.strftime('%d-%m %H:%M', time.localtime(float(v.get('gezien', 0))))} · van {_schoon(v.get('van'), 80)}")
            for regel in _logstaart(8):
                print("  | " + regel)
    except ValueError as fout:
        sys.exit(str(fout))


if __name__ == "__main__":
    main()
