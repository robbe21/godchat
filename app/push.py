#!/usr/bin/env python3
"""Meldingen op je toestellen (Web Push): een nieuwe vraag in "Wacht op jou", of een chat die in fout valt.

Dit bestand is de logica en laadt opnieuw zonder herstart (push_lader.py roept elke 2 s `ronde()` aan en geeft de
aanvragen op /api/push/… door). Alleen standaardbibliotheek: versleutelen en ondertekenen doet push_stuur.py met
pywebpush, als los proces in een eigen omgeving (~/.venvs/godchat-push, of de Python in GODCHAT_PUSH_PY). Ontbreekt die
omgeving, dan zijn er geen meldingen en werkt de rest gewoon.

- Een melding is er alleen voor een nieuwe open vraag en voor een chat die in fout valt; nooit voor een bericht of
  een beurt. Tussen 22:00 en 07:30 geen melding (PROTOCOL.md): wat dan binnenkomt en om 07:30 nog openstaat, volgt dan.
- Wat erin staat: de klant of chat en de kop van het kaartje (het opschrift uit klanten.py, anders de vraag zelf).
  Nooit het concept of de context: het staat op een vergrendelscherm.
- Antwoorden kan niet vanuit de melding: een tik opent de app op die vraag (sw.js, index.html).
- De sleutel van de app, de toestellen en de stand staan buiten de werkmap, in ~/.config/godchat/push/ (alleen leesbaar
  voor je eigen gebruiker). Niets daarvan komt in een antwoord, een logregel of een chat. Het nepscherm raakt die map
  nooit aan.
- Niets hier raakt vraag_eigenaar of de motor: dit deel leest alleen app.stand().

Zelftest: python3 -I push.py --zelftest
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import urlsplit

HIER = os.path.dirname(os.path.abspath(__file__))
# de eigen omgeving met pywebpush (push_vereisten.txt)
PYTHON = os.path.expanduser(os.environ.get("GODCHAT_PUSH_PY") or "~/.venvs/godchat-push/bin/python")
STUUR = os.path.join(HIER, "push_stuur.py")
SLEUTEL = "vapid-prive.pem"

AAN = True                  # False: geen enkele melding meer (opslaan volstaat, geen herstart)
STIL = ((22, 0), (7, 30))   # tussen deze uren geen melding; wat dan binnenkomt volgt om 07:30 als het nog openstaat
WACHT_OP_KOP_S = 12         # zo lang wacht een melding op het opschrift van het kaartje (klanten.py), daarna de vraag zelf
STAND_ELKE_S = 10           # zo vaak kijken we sowieso naar de chats (een chat in fout), ook zonder nieuwe vraag
TTL_S = 4 * 3600            # zo lang bewaart de pushdienst een melding voor een toestel dat niet bereikbaar is
PER_UUR = 30                # noodrem: meer meldingen per uur verstuurt dit deel nooit
FOUT_RUST_S = 1800          # dezelfde chat in fout: hoogstens één melding per half uur
MAX_TOESTELLEN = 8
MAX_TEKST = 140
# VAPID vraagt een contactadres in elke aanvraag aan de pushdienst. Bewust geen persoonlijk adres: wie het wil wijzigen
# zet {"contact": "mailto:…"} in instellingen.json in de map van de meldingen.
CONTACT = "mailto:push@godchat.invalid"
# Alleen naar de pushdiensten van de browsers: een toestel met een ander adres wordt geweigerd.
DIENSTEN = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")

_MASKER = re.compile(r"\b(?:re|sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_\-]{12,}|\beyJ[A-Za-z0-9_\-]{10,}"
                     r"|[A-Za-z0-9]{32,}|\+\d[\d \-]{7,}\d|\b\d{9,}\b")
_B64 = re.compile(r"^[A-Za-z0-9_-]+$")
_reserve_slot = threading.Lock()


# --- klein gereedschap -------------------------------------------------------------------------------------------
def _slot(staat):
    return staat.get("slot") or _reserve_slot


def map_(staat):
    """De map met de sleutel, de toestellen en de stand. None = hier worden geen meldingen verstuurd (nepscherm)."""
    pad = os.environ.get("GODCHAT_PUSH_MAP")
    if pad:
        return pad
    if staat.get("nep"):
        return None   # het nepscherm raakt de echte sleutel en de echte toestellen nooit aan
    return os.path.expanduser("~/.config/godchat/push")


def _lees(pad, standaard):
    try:
        with open(pad, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, type(standaard)) else standaard
    except (OSError, ValueError, UnicodeDecodeError):
        return standaard


def _schrijf(pad, data):
    """Alleen leesbaar voor je eigen gebruiker, en nooit half geschreven."""
    os.makedirs(os.path.dirname(pad), mode=0o700, exist_ok=True)
    tmp = pad + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, pad)


def _kort(tekst, n):
    tekst = " ".join(str(tekst or "").split())
    return tekst if len(tekst) <= n else tekst[: n - 1] + "…"


def _masker(tekst):
    """Niets wat op een sleutel of telefoonnummer lijkt gaat de deur uit (zelfde regel als klanten.py)."""
    mod = getattr(sys.modules.get("klanten_lader"), "_mod", None)
    if mod is not None and hasattr(mod, "masker"):
        try:
            return mod.masker(tekst)
        except Exception:
            pass
    return _MASKER.sub("[…]", str(tekst or ""))


def _id(endpoint):
    return hashlib.sha256(str(endpoint).encode("utf-8", "replace")).hexdigest()[:16]


def _iso(t=None):
    return datetime.fromtimestamp(t if t is not None else time.time()).astimezone().isoformat(timespec="seconds")


def stil(nu):
    """Vallen we in de stille uren? (22:00 tot 07:30, de klok van deze Mac)"""
    uur = (nu.hour, nu.minute)
    return uur >= STIL[0] or uur < STIL[1]


def _log(tekst):
    print("Meldingen: " + tekst, flush=True)   # nooit een adres of sleutel van een toestel


# --- de sleutel van de app en de toestellen ----------------------------------------------------------------------
def beschikbaar(staat):
    """-> (kan het?, waarom niet)"""
    if not AAN:
        return False, "meldingen staan uit (AAN = False in push.py)"
    if not map_(staat):
        return False, "het nepscherm verstuurt geen meldingen"
    if not os.path.isfile(PYTHON) or not os.path.isfile(STUUR):
        return False, "de omgeving voor de meldingen ontbreekt (zie PUSH.md → Opzetten)"
    return True, ""


def _hulp(args, invoer=None, wacht=25):
    """Eén aanroep van push_stuur.py in de eigen omgeving. -> dict (altijd; ok=False bij een fout)"""
    try:
        r = subprocess.run([PYTHON, "-I", STUUR] + list(args), input=invoer, capture_output=True, text=True, timeout=wacht,
                           stdin=None if invoer is not None else subprocess.DEVNULL)
        uit = json.loads(r.stdout.strip().splitlines()[-1])
        return uit if isinstance(uit, dict) else {"ok": False, "status": None, "fout": "onleesbaar antwoord"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": None, "fout": "te traag"}
    except (OSError, ValueError, IndexError) as fout:
        return {"ok": False, "status": None, "fout": type(fout).__name__}


def publiek(staat):
    """De publieke sleutel van de app (de browser vraagt hem bij het aanzetten). Maakt het sleutelpaar de eerste keer."""
    m = map_(staat)
    if not m:
        return None
    pem = os.path.join(m, SLEUTEL)
    with _slot(staat):
        try:
            st = os.stat(pem)
            merk = (st.st_mtime_ns, st.st_size)
        except OSError:
            merk = None
        geheugen = staat.get("publiek")
        if merk and geheugen and geheugen[0] == merk:
            return geheugen[1]
        uit = (staat.get("hulp") or _hulp)(["--sleutel", m])
        if not uit.get("ok") or not isinstance(uit.get("publiek"), str):
            _log("de sleutel van de app is niet te maken of te lezen: " + str(uit.get("fout")))
            return None
        if uit.get("nieuw"):
            _log("sleutel van de app gemaakt (blijft op deze Mac)")
        try:
            st = os.stat(pem)
            staat["publiek"] = ((st.st_mtime_ns, st.st_size), uit["publiek"])
        except OSError:
            pass
        return uit["publiek"]


def _toestellen(m):
    data = _lees(os.path.join(m, "toestellen.json"), {})
    return [t for t in data.get("toestellen") or [] if isinstance(t, dict) and t.get("endpoint") and t.get("id")]


def _bewaar_toestellen(m, lijst):
    _schrijf(os.path.join(m, "toestellen.json"), {"versie": 1, "toestellen": lijst})


def _toestelnaam(ua):
    ua = str(ua or "")
    wat = ("Android" if "Android" in ua else "iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua
           else "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua else "toestel")
    browser = ("Edge" if "Edg/" in ua else "Firefox" if "Firefox/" in ua else "Chrome" if "Chrome/" in ua
               else "Safari" if "Safari/" in ua else "")
    return f"{wat} · {browser}" if browser else wat


def _keur_abonnement(ab):
    """Een abonnement zoals de browser het geeft (PushSubscription.toJSON). -> (schoon dict | None, reden)"""
    if not isinstance(ab, dict):
        return None, "geen abonnement"
    endpoint, keys = ab.get("endpoint"), ab.get("keys")
    if not isinstance(endpoint, str) or not 20 <= len(endpoint) <= 1200 or not isinstance(keys, dict):
        return None, "onvolledig abonnement"
    try:
        u = urlsplit(endpoint)
        host = (u.hostname or "").lower()
    except ValueError:
        return None, "ongeldig adres"
    proef = os.environ.get("GODCHAT_PUSH_PROEFDIENST")   # alleen voor tests: een nagemaakte dienst op deze Mac
    if proef and u.netloc == proef and u.scheme == "http":
        pass
    elif u.scheme != "https" or not any(host == d or host.endswith("." + d) for d in DIENSTEN):
        return None, "dit is geen pushdienst van een browser"
    p, a = keys.get("p256dh"), keys.get("auth")
    if (not isinstance(p, str) or not isinstance(a, str) or not 80 <= len(p) <= 100 or not 16 <= len(a) <= 32
            or not _B64.match(p) or not _B64.match(a)):
        return None, "ongeldige sleutels van het toestel"
    return {"endpoint": endpoint, "p256dh": p, "auth": a}, ""


def abonneer(staat, body, ua=""):
    """POST /api/push/abonneer: dit toestel wil meldingen (of meldt zich opnieuw). Verstuurt niets."""
    ok, reden = beschikbaar(staat)
    if not ok:
        return {"ok": False, "fout": reden}
    schoon, reden = _keur_abonnement((body or {}).get("abonnement"))
    if not schoon:
        return {"ok": False, "fout": reden}
    m = map_(staat)
    oud = (body or {}).get("oud")
    with _slot(staat):
        lijst = [t for t in _toestellen(m) if not (isinstance(oud, str) and t["endpoint"] == oud)]
        bestaand = next((t for t in lijst if t["endpoint"] == schoon["endpoint"]), None)
        if bestaand:
            bestaand.update(p256dh=schoon["p256dh"], auth=schoon["auth"], gezien=_iso())
        else:
            lijst.append({"id": _id(schoon["endpoint"]), **schoon, "toestel": _toestelnaam(ua), "sinds": _iso(),
                          "gezien": _iso(), "laatst_ok": None, "mislukt": 0})
            lijst = lijst[-MAX_TOESTELLEN:]
            _log(f"toestel erbij ({_toestelnaam(ua)}); nu {len(lijst)}")
        _bewaar_toestellen(m, lijst)
    return {"ok": True, "id": _id(schoon["endpoint"]), "nieuw": not bestaand, "toestellen": len(lijst)}


def afmelden(staat, body):
    """POST /api/push/afmelden: dit toestel wil geen meldingen meer."""
    m = map_(staat)
    endpoint = (body or {}).get("endpoint")
    if not m or not isinstance(endpoint, str):
        return {"ok": False, "fout": "geen toestel"}
    with _slot(staat):
        lijst = _toestellen(m)
        rest = [t for t in lijst if t["endpoint"] != endpoint]
        if len(rest) != len(lijst):
            _bewaar_toestellen(m, rest)
            _log(f"toestel afgemeld; nu {len(rest)}")
    return {"ok": True, "toestellen": len(rest)}


# --- versturen ---------------------------------------------------------------------------------------------------
def _contact(m):
    c = _lees(os.path.join(m, "instellingen.json"), {}).get("contact")
    return c if isinstance(c, str) and c.startswith(("mailto:", "https://")) and len(c) <= 200 else CONTACT


def _mag(staat, nu_t):
    uren = staat.setdefault("uren", deque())
    while uren and nu_t - uren[0] > 3600:
        uren.popleft()
    if len(uren) >= PER_UUR:
        return False
    uren.append(nu_t)
    return True


def stuur_allen(staat, bericht, topic, alleen=None, urgentie="high"):
    """Eén melding naar alle toestellen (of alleen naar het toestel met dit adres). -> [{id, toestel, ok, status, fout}]

    Gooit nooit. Een toestel dat opgezegd heeft (404/410) verdwijnt uit de lijst."""
    m = map_(staat)
    if not m:
        return []
    with _slot(staat):
        lijst = _toestellen(m)
        if not _mag(staat, time.time()):
            _log("noodrem: te veel meldingen dit uur, deze is niet verstuurd")
            return []
    doel = [t for t in lijst if alleen is None or t["endpoint"] == alleen]
    if not doel:
        return []
    tekst = json.dumps(bericht, ensure_ascii=False)
    pem, contact = os.path.join(m, SLEUTEL), _contact(m)
    stuur = staat.get("stuur") or (lambda opdracht: _hulp([], invoer=json.dumps(opdracht)))
    uitslag = []
    for t in doel:
        opdracht = {"abonnement": {"endpoint": t["endpoint"], "keys": {"p256dh": t["p256dh"], "auth": t["auth"]}},
                    "bericht": tekst, "sleutel": pem, "sub": contact, "ttl": TTL_S, "urgentie": urgentie, "topic": topic,
                    "wacht": 12}
        try:
            r = stuur(opdracht)
        except Exception as fout:
            r = {"ok": False, "status": None, "fout": type(fout).__name__}
        uitslag.append({"id": t["id"], "toestel": t.get("toestel") or "toestel", "ok": bool(r.get("ok")),
                        "status": r.get("status"), "weg": bool(r.get("weg")), "fout": _kort(r.get("fout") or "", 160)})
    with _slot(staat):   # de lijst kan intussen gewijzigd zijn: vers lezen en alleen onze toestellen bijwerken
        lijst = _toestellen(m)
        per_id = {u["id"]: u for u in uitslag}
        nieuw = []
        for t in lijst:
            u = per_id.get(t["id"])
            if u and u["weg"]:
                _log(f"toestel heeft opgezegd ({t.get('toestel')}): weggehaald")
                continue
            if u and u["ok"]:
                t.update(laatst_ok=_iso(), mislukt=0)
            elif u:
                t["mislukt"] = int(t.get("mislukt") or 0) + 1
            nieuw.append(t)
        _bewaar_toestellen(m, nieuw)
    _log(f"{bericht.get('tag')} → " + ", ".join(f"{u['toestel']}: {u['status'] or u['fout'] or '?'}" for u in uitslag))
    return uitslag


def _uitslagtekst(uitslag):
    if not uitslag:
        return "Er is geen toestel om naar te sturen."
    goed = sum(1 for u in uitslag if u["ok"])
    if goed == len(uitslag):
        return "Verstuurd naar " + ("dit toestel." if goed == 1 else f"{goed} toestellen.") + " Komt er niets, kijk dan of meldingen voor GodChat aan staan op het toestel."
    mis = next(u for u in uitslag if not u["ok"])
    waarom = ("het toestel heeft de meldingen opgezegd: zet ze opnieuw aan" if mis["weg"]
              else f"de pushdienst antwoordde {mis['status']}" if mis["status"] else f"geen verbinding met de pushdienst ({mis['fout'] or 'onbekend'})")
    return (f"Verstuurd naar {goed} van {len(uitslag)} toestellen; " if goed else "Niet verstuurd: ") + waarom + "."


def test(staat, body):
    """POST /api/push/test: een tik op "Stuur een testmelding". Alleen naar het toestel dat erom vraagt; ook 's nachts."""
    ok, reden = beschikbaar(staat)
    if not ok:
        return {"ok": False, "tekst": reden}
    endpoint = (body or {}).get("endpoint")
    if not isinstance(endpoint, str):
        return {"ok": False, "tekst": "Dit toestel heeft de meldingen nog niet aangezet."}
    bericht = {"t": "GodChat", "b": "Testmelding. Zo komt een vraag bij je binnen; een tik opent de app.", "tag": "test",
               "url": "/", "ts": int(time.time() * 1000)}
    uitslag = stuur_allen(staat, bericht, "test", alleen=endpoint)
    if not uitslag:
        return {"ok": False, "tekst": "Dit toestel staat niet (meer) in de lijst: zet de meldingen opnieuw aan."}
    return {"ok": all(u["ok"] for u in uitslag), "tekst": _uitslagtekst(uitslag)}


def stand(staat):
    """GET /api/push/stand: wat het scherm nodig heeft. Geen adressen of sleutels van toestellen."""
    ok, reden = beschikbaar(staat)
    if not ok:
        return {"ok": True, "beschikbaar": False, "reden": reden}
    sleutel = publiek(staat)
    if not sleutel:
        return {"ok": True, "beschikbaar": False, "reden": "de sleutel van de app is niet te lezen"}
    m = map_(staat)
    nu = datetime.now()
    return {"ok": True, "beschikbaar": True, "sleutel": sleutel, "nu_stil": stil(nu),
            "stil": "%02d:%02d tot %02d:%02d" % (STIL[0] + STIL[1]),
            "toestellen": [{"id": t["id"], "toestel": t.get("toestel"), "sinds": t.get("sinds"), "laatst_ok": t.get("laatst_ok")}
                           for t in _toestellen(m)]}


def aanvraag(staat, methode, pad, body, ua=""):
    """Alles onder /api/push/ (push_lader geeft het door). -> (statuscode, antwoord). Gooit niet."""
    try:
        if methode == "GET" and pad == "/api/push/stand":
            return 200, stand(staat)
        if methode == "POST" and pad == "/api/push/abonneer":
            return 200, abonneer(staat, body, ua)
        if methode == "POST" and pad == "/api/push/afmelden":
            return 200, afmelden(staat, body)
        if methode == "POST" and pad == "/api/push/test":
            return 200, test(staat, body)
        return 404, {"ok": False, "fout": "niet gevonden"}
    except Exception as fout:
        _log(f"fout in {pad}: {type(fout).__name__}")
        return 500, {"ok": False, "fout": "interne fout"}


# --- de ronde: wat is er nieuw, en mag het nu gemeld worden? -----------------------------------------------------
def _hash(v):
    return hashlib.sha1(" ".join(str(v.get("vraag") or "").split()).encode("utf-8", "replace")).hexdigest()[:16]


def _chatnaam(chats, cid):
    c = chats.get(cid) or {}
    naam = str(c.get("naam") or cid or "GodChat")
    ouder = chats.get(c.get("ouder")) if c.get("soort") == "taak" else None
    return _kort(f"{ouder.get('naam')} · {naam}" if ouder and ouder.get("naam") else naam, 60)


def _opschriften(staat, vragen):
    """De koppen van de kleine kaartjes (klanten.py): {nr: tekst | None}; een nr dat ontbreekt is nog niet beslist.
    None = er is geen klantenblok: dan wachten we niet."""
    if "koppen" in staat:   # tests
        return staat["koppen"](vragen)
    lader = sys.modules.get("klanten_lader")
    mod = getattr(lader, "_mod", None)
    if mod is None or not hasattr(mod, "_kort_blik"):
        return None
    try:
        mod._kort_wek(lader._bron(mod), lader.staat)   # de wachter van de opschriften loopt dan zeker
        return mod._kort_blik(lader.staat, vragen)
    except Exception:
        return None


def _bericht_vraag(v, chats, kop):
    nr = v.get("nr")
    return {"t": _chatnaam(chats, v.get("chat")), "b": _kort(_masker(kop or v.get("vraag") or "Er wacht een vraag op je."), MAX_TEKST),
            "tag": f"vraag-{nr}", "url": f"/#vraag={nr}", "ts": int(time.time() * 1000)}


def _lees_stand(m):
    st = _lees(os.path.join(m, "stand.json"), {})
    st["vragen"] = st["vragen"] if isinstance(st.get("vragen"), dict) else {}
    st["fout"] = st["fout"] if isinstance(st.get("fout"), dict) else {}
    return st


def verwerk(staat, st, s, nu):
    """De kern, zonder klok en zonder schijf: `st` is de stand (wordt bijgewerkt), `s` is app.stand(), `nu` een datetime.
    -> True als `st` gewijzigd is."""
    m = map_(staat)
    toestellen = bool(_toestellen(m)) if m else False
    nu_t = nu.timestamp()
    nacht = stil(nu)
    chats = {c["id"]: c for c in s.get("chats") or [] if isinstance(c, dict) and c.get("id")}
    open_vragen = {str(v["nr"]): v for v in s.get("vragen") or []
                   if isinstance(v, dict) and v.get("nr") is not None and v.get("status", "open") == "open"}
    gewijzigd = False
    vr = st["vragen"]

    if not st.get("begonnen"):   # de allereerste keer: over wat er al stond komt geen melding meer
        vr.clear()
        vr.update({nr: {"h": _hash(v), "stand": "gemeld", "t": nu_t} for nr, v in open_vragen.items()})
        st["begonnen"] = True
        gewijzigd = True

    for nr in [nr for nr in vr if nr not in open_vragen]:   # beantwoord of vervallen: weg, ook als ze nog wachtte
        del vr[nr]
        gewijzigd = True
    for nr, v in open_vragen.items():
        e = vr.get(nr)
        if not isinstance(e, dict) or e.get("h") != _hash(v):
            vr[nr] = {"h": _hash(v), "stand": "nieuw", "t": nu_t}
            gewijzigd = True

    nieuw = [nr for nr, e in vr.items() if e.get("stand") == "nieuw"]
    koppen = _opschriften(staat, list(open_vragen.values())) if nieuw else {}
    for nr in nieuw:
        e = vr[nr]
        if koppen is not None and nr not in koppen and nu_t - (e.get("t") or 0) < WACHT_OP_KOP_S:
            continue   # het opschrift van het kaartje komt nog (een paar seconden)
        if not toestellen:
            e["stand"] = "geen"   # geen toestel met meldingen aan: de vraag staat gewoon in de app
        elif nacht:
            e["stand"] = "ochtend"
        else:
            stuur_allen(staat, _bericht_vraag(open_vragen[nr], chats, (koppen or {}).get(nr)), f"vraag-{nr}")
            e["stand"] = "gemeld"
        gewijzigd = True

    if not nacht:   # de stille uren zijn voorbij: wat toen binnenkwam en nog openstaat
        ochtend = [nr for nr, e in vr.items() if e.get("stand") == "ochtend"]
        if ochtend and toestellen:
            if len(ochtend) == 1:
                koppen = _opschriften(staat, list(open_vragen.values())) or {}
                stuur_allen(staat, _bericht_vraag(open_vragen[ochtend[0]], chats, koppen.get(ochtend[0])), f"vraag-{ochtend[0]}")
            else:
                tel = {}
                for nr in ochtend:
                    naam = _chatnaam(chats, open_vragen[nr].get("chat"))
                    tel[naam] = tel.get(naam, 0) + 1
                wie = ", ".join(f"{naam} ({n})" if n > 1 else naam for naam, n in tel.items())
                stuur_allen(staat, {"t": "GodChat", "b": _kort(f"{len(ochtend)} vragen wachten op je: {wie}", MAX_TEKST),
                                    "tag": "ochtend", "url": "/", "ts": int(time.time() * 1000)}, "ochtend", urgentie="normal")
        for nr in ochtend:
            vr[nr]["stand"] = "gemeld" if toestellen else "geen"
            gewijzigd = True

    # een chat die in fout valt (alleen de overgang; wat bij de start al in fout stond telt niet)
    in_fout = {cid for cid, c in chats.items() if c.get("toestand") == "fout"}
    geheugen = staat.setdefault("ronde", {})
    was = geheugen.get("fout_was")
    geheugen["fout_was"] = in_fout
    fouten = st["fout"]
    for cid in (in_fout - was if was is not None else ()):
        f = fouten.setdefault(cid, {})
        if nu_t - (f.get("t") or 0) >= FOUT_RUST_S and not f.get("wacht"):
            f["wacht"] = True
            gewijzigd = True
    for cid, f in list(fouten.items()):
        if not isinstance(f, dict) or not f.get("wacht"):
            continue
        if cid not in in_fout or not toestellen:   # alweer hersteld, of niemand om het aan te melden
            f["wacht"] = False
            gewijzigd = True
        elif not nacht:
            stuur_allen(staat, {"t": _chatnaam(chats, cid), "b": "Deze chat is in fout gevallen. Tik om te kijken.",
                                "tag": "fout-" + _id(cid)[:8], "url": f"/#chat={cid}", "ts": int(time.time() * 1000)},
                        "fout-" + _id(cid)[:8])
            f.update(t=nu_t, wacht=False)
            gewijzigd = True
    for cid in [c for c, f in fouten.items() if not isinstance(f, dict) or (not f.get("wacht") and nu_t - (f.get("t") or 0) > 86400)]:
        del fouten[cid]
        gewijzigd = True
    return gewijzigd


def ronde(staat):
    """Eén keer kijken (push_lader roept dit elke 2 s aan). Kost niets tot er iets verandert. Gooit niet."""
    if not AAN:
        return
    app, m = staat.get("app"), map_(staat)
    if app is None or not m or not os.path.isfile(PYTHON):
        return
    geheugen = staat.setdefault("ronde", {})
    try:
        mt = os.stat(os.path.join(HIER, "data", "vragen.json")).st_mtime_ns
    except OSError:
        mt = None
    nu_t = time.time()
    st = _lees_stand(m)
    bezig = (any(e.get("stand") in ("nieuw", "ochtend") for e in st["vragen"].values() if isinstance(e, dict))
             or any(f.get("wacht") for f in st["fout"].values() if isinstance(f, dict)))
    if mt == geheugen.get("mtime") and nu_t < geheugen.get("volgende", 0.0) and not (bezig and nu_t >= geheugen.get("snel", 0.0)):
        return
    geheugen.update(mtime=mt, volgende=nu_t + STAND_ELKE_S, snel=nu_t + 2)
    s = app.stand() or {}
    if verwerk(staat, st, s, datetime.now()):
        _schrijf(os.path.join(m, "stand.json"), st)


# --- zelftest ----------------------------------------------------------------------------------------------------
def zelftest():
    """De regels van de meldingen, met een nagemaakte verzender en een eigen klok. Verstuurt niets."""
    fouten, teller = [], [0]

    def eis(voorwaarde, naam):
        teller[0] += 1
        if not voorwaarde:
            fouten.append(naam)
            print(f"  NIET OK  {naam}")
        return voorwaarde

    werk = tempfile.mkdtemp(prefix="gc-push-test-")
    vorige = os.environ.get("GODCHAT_PUSH_MAP")
    os.environ["GODCHAT_PUSH_MAP"] = werk
    echte_python = PYTHON
    globals()["PYTHON"] = sys.executable   # de toetsen hebben de eigen omgeving niet nodig: er wordt niets verstuurd
    try:
        dag = datetime(2026, 11, 23, 14, 0, 0)
        eis(not stil(dag) and stil(dag.replace(hour=22, minute=0)) and stil(dag.replace(hour=7, minute=29))
            and not stil(dag.replace(hour=7, minute=30)) and stil(dag.replace(hour=2)) and not stil(dag.replace(hour=21, minute=59)),
            "stil: van 22:00 tot 07:30")

        goed = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123def456", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}
        eis(_keur_abonnement(goed)[0] is not None, "abonnement: een adres van Chrome mag")
        eis(_keur_abonnement(dict(goed, endpoint="https://updates.push.services.mozilla.com/wpush/v2/xyz9991234567"))[0] is not None,
            "abonnement: een adres van Firefox mag")
        eis(_keur_abonnement(dict(goed, endpoint="https://evil.example/fcm.googleapis.com/abcdefghij"))[0] is None,
            "abonnement: een ander adres wordt geweigerd")
        eis(_keur_abonnement(dict(goed, endpoint="http://fcm.googleapis.com/fcm/send/abc123def456"))[0] is None,
            "abonnement: zonder https wordt geweigerd")
        eis(_keur_abonnement(dict(goed, endpoint="https://fcm.googleapis.com.evil.example/send/abc123"))[0] is None,
            "abonnement: een adres dat erop lijkt wordt geweigerd")
        eis(_keur_abonnement(dict(goed, keys={"p256dh": "kort", "auth": "a" * 22}))[0] is None, "abonnement: foute sleutels geweigerd")
        eis(_keur_abonnement("x")[0] is None and _keur_abonnement({})[0] is None, "abonnement: rommel geweigerd")

        verstuurd = []

        def stuur(opdracht):
            verstuurd.append(opdracht)
            if "weg" in opdracht["abonnement"]["endpoint"]:
                return {"ok": False, "status": 410, "weg": True, "fout": "Gone"}
            return {"ok": True, "status": 201}

        def berichten():
            return [json.loads(o["bericht"]) for o in verstuurd]

        koppen = {}
        staat = {"slot": threading.Lock(), "stuur": stuur, "koppen": lambda vragen: dict(koppen),
                 "hulp": lambda args: {"ok": True, "publiek": "P" * 87, "nieuw": True}}
        eis(stand(staat).get("beschikbaar") in (True, False), "stand: antwoordt altijd")
        eis(aanvraag(staat, "GET", "/api/push/stand", None)[0] == 200 and aanvraag(staat, "GET", "/api/push/onbekend", None)[0] == 404
            and aanvraag(staat, "POST", "/api/push/abonneer", {"abonnement": 5})[1].get("ok") is False
            and aanvraag(staat, "POST", "/api/push/test", None)[0] == 200, "aanvraag: verdeelt de adressen en gooit nooit")
        chats = [{"id": "bakkerij", "naam": "Bakkerij", "soort": "klant", "toestand": "slaapt"},
                 {"id": "bakkerij--openingsuren", "naam": "Openingsuren op de site", "soort": "taak", "ouder": "bakkerij", "toestand": "werkt"},
                 {"id": "god", "naam": "God-chat", "soort": "god", "toestand": "slaapt"}]

        def vraag(nr, chat, tekst):
            return {"nr": nr, "chat": chat, "status": "open", "vraag": tekst, "concept": "GEHEIM CONCEPT", "context": "GEHEIME CONTEXT"}

        v1 = vraag(1, "bakkerij", "Stond er al toen de meldingen begonnen?")
        st = {"vragen": {}, "fout": {}}
        # zonder toestel: niets versturen, wel onthouden
        verwerk(staat, st, {"chats": chats, "vragen": [v1]}, dag)
        eis(st["vragen"]["1"]["stand"] == "gemeld" and not verstuurd, "start: over wat er al stond komt geen melding")
        v2 = vraag(2, "bakkerij", "De nieuwe home live zetten, met de webshop onder /shop/?")
        koppen["2"] = None
        verwerk(staat, st, {"chats": chats, "vragen": [v1, v2]}, dag)
        eis(st["vragen"]["2"]["stand"] == "geen" and not verstuurd, "zonder toestel: geen melding, de vraag staat in de app")

        # een toestel erbij
        r = abonneer(staat, {"abonnement": goed}, "Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile Safari/537.36")
        eis(r.get("ok") and r.get("nieuw") and r.get("toestellen") == 1, "abonneer: een toestel erbij")
        eis(abonneer(staat, {"abonnement": goed}, "x").get("nieuw") is False and len(_toestellen(werk)) == 1, "abonneer: twee keer is één toestel")
        eis(_toestellen(werk)[0]["toestel"] == "Android · Chrome", "abonneer: het toestel krijgt een naam")
        eis(oct(os.stat(os.path.join(werk, "toestellen.json")).st_mode & 0o777) == "0o600", "toestellen.json is alleen leesbaar voor de eigen gebruiker")
        eis(not abonneer(staat, {"abonnement": dict(goed, endpoint="https://evil.example/x/abcdefghijklmnop")}, "x").get("ok"),
            "abonneer: een vreemd adres wordt geweigerd")
        eis("endpoint" not in json.dumps(stand(staat)) and "p256dh" not in json.dumps(stand(staat)) and len(stand(staat)["toestellen"]) == 1,
            "stand: toont het toestel, zonder adres of sleutels")

        # een nieuwe vraag: wacht op het opschrift, dan één melding met chat + opschrift
        v3 = vraag(3, "bakkerij--openingsuren", "De nieuwe openingsuren live zetten, met de feestdagen er nog niet in?")
        verwerk(staat, st, {"chats": chats, "vragen": [v1, v2, v3]}, dag)
        eis(st["vragen"]["3"]["stand"] == "nieuw" and not verstuurd, "nieuwe vraag: wacht eerst op het opschrift")
        koppen["3"] = "De nieuwe openingsuren live zetten?"
        verwerk(staat, st, {"chats": chats, "vragen": [v1, v2, v3]}, dag.replace(second=3))
        b = berichten()
        eis(len(b) == 1 and b[0]["t"] == "Bakkerij · Openingsuren op de site" and b[0]["b"] == "De nieuwe openingsuren live zetten?"
            and b[0]["tag"] == "vraag-3" and b[0]["url"] == "/#vraag=3", "nieuwe vraag: klant · taak, het opschrift, en het adres van de vraag")
        eis("GEHEIM" not in json.dumps(verstuurd), "melding: nooit het concept of de context")
        eis(verstuurd[0]["topic"] == "vraag-3" and verstuurd[0]["ttl"] == TTL_S and verstuurd[0]["urgentie"] == "high"
            and verstuurd[0]["sub"] == CONTACT, "melding: topic, bewaartijd, voorrang en een contact zonder persoonlijke naam")
        verwerk(staat, st, {"chats": chats, "vragen": [v1, v2, v3]}, dag.replace(second=9))
        eis(len(verstuurd) == 1, "nieuwe vraag: maar één keer")

        # geen opschrift binnen de wachttijd: dan de vraag zelf
        v4 = vraag(4, "god", "Noor de verbeterde cijfers sturen voor ze morgen met Bram praat?")
        verwerk(staat, st, {"chats": chats, "vragen": [v1, v2, v3, v4]}, dag.replace(minute=1, second=0))
        verwerk(staat, st, {"chats": chats, "vragen": [v1, v2, v3, v4]}, dag.replace(minute=1, second=5))
        eis(len(verstuurd) == 1, "zonder opschrift: nog even wachten")
        verwerk(staat, st, {"chats": chats, "vragen": [v1, v2, v3, v4]}, dag.replace(minute=1, second=WACHT_OP_KOP_S + 1))
        eis(len(verstuurd) == 2 and berichten()[1]["b"] == v4["vraag"] and berichten()[1]["t"] == "God-chat",
            "zonder opschrift: na de wachttijd de vraag zelf")
        koppen["5"] = None
        v5 = vraag(5, "bakkerij", "Mag de sleutel sk_live_abcdefghijklmnop1234 in het bestand, ja of nee?")
        verwerk(staat, st, {"chats": chats, "vragen": [v1, v2, v3, v4, v5]}, dag.replace(minute=2))
        eis("sk_live" not in berichten()[2]["b"] and "[…]" in berichten()[2]["b"], "melding: iets wat op een sleutel lijkt wordt gemaskeerd")

        # beantwoord voor de melding vertrok: geen melding
        v6 = vraag(6, "bakkerij", "Is dit een vraag die meteen beantwoord wordt, nog voor het opschrift er is?")
        verwerk(staat, st, {"chats": chats, "vragen": [v6]}, dag.replace(minute=3))
        verwerk(staat, st, {"chats": chats, "vragen": []}, dag.replace(minute=3, second=2))
        eis(len(verstuurd) == 3 and st["vragen"] == {}, "beantwoord voor de melding: geen melding, en de stand ruimt op")

        # de stille uren: niets, en om 07:30 wat nog openstaat
        nacht = datetime(2026, 11, 23, 23, 15, 0)
        koppen.update({"7": "Kaartje zeven?", "8": None, "9": None})
        v7, v8, v9 = vraag(7, "bakkerij", "Vraag zeven van de nacht, lang genoeg om een vraag te zijn?"), vraag(8, "god", "Vraag acht?"), vraag(9, "bakkerij", "Vraag negen?")
        verwerk(staat, st, {"chats": chats, "vragen": [v7, v8, v9]}, nacht)
        eis(len(verstuurd) == 3 and all(st["vragen"][n]["stand"] == "ochtend" for n in "789"), "stille uren: geen melding, wel onthouden")
        verwerk(staat, st, {"chats": chats, "vragen": [v7, v8]}, datetime(2026, 11, 24, 7, 29, 0))
        eis(len(verstuurd) == 3 and "9" not in st["vragen"], "stille uren: 07:29 nog stil; wat intussen beantwoord is valt weg")
        verwerk(staat, st, {"chats": chats, "vragen": [v7, v8]}, datetime(2026, 11, 24, 7, 30, 0))
        eis(len(verstuurd) == 4 and berichten()[3]["b"] == "2 vragen wachten op je: Bakkerij, God-chat" and berichten()[3]["url"] == "/"
            and all(st["vragen"][n]["stand"] == "gemeld" for n in "78"), "07:30: één melding voor wat 's nachts binnenkwam en nog openstaat")
        verwerk(staat, st, {"chats": chats, "vragen": [v7, v8]}, datetime(2026, 11, 24, 7, 31, 0))
        eis(len(verstuurd) == 4, "07:30: maar één keer")
        v10 = vraag(10, "bakkerij", "Eén vraag in de nacht?")
        koppen["10"] = "Eén in de nacht?"
        verwerk(staat, st, {"chats": chats, "vragen": [v7, v8, v10]}, datetime(2026, 11, 25, 1, 0, 0))
        verwerk(staat, st, {"chats": chats, "vragen": [v7, v8, v10]}, datetime(2026, 11, 25, 8, 0, 0))
        eis(len(verstuurd) == 5 and berichten()[4]["b"] == "Eén in de nacht?" and berichten()[4]["url"] == "/#vraag=10",
            "07:30 met één vraag: gewoon die vraag")

        # een chat die in fout valt
        staat.pop("ronde", None)
        fout = [dict(c, toestand="fout") if c["id"] == "god" else c for c in chats]
        verwerk(staat, st, {"chats": fout, "vragen": [v7, v8, v10]}, dag.replace(day=25))
        eis(len(verstuurd) == 5, "fout: wat bij de start al in fout stond, wordt niet gemeld")
        verwerk(staat, st, {"chats": chats, "vragen": [v7, v8, v10]}, dag.replace(day=25, minute=1))
        verwerk(staat, st, {"chats": fout, "vragen": [v7, v8, v10]}, dag.replace(day=25, minute=2))
        eis(len(verstuurd) == 6 and berichten()[5]["t"] == "God-chat" and berichten()[5]["url"] == "/#chat=god"
            and berichten()[5]["tag"].startswith("fout-"), "fout: een chat die in fout valt geeft één melding, zonder de fouttekst")
        verwerk(staat, st, {"chats": chats, "vragen": [v7, v8, v10]}, dag.replace(day=25, minute=3))
        verwerk(staat, st, {"chats": fout, "vragen": [v7, v8, v10]}, dag.replace(day=25, minute=4))
        eis(len(verstuurd) == 6, "fout: dezelfde chat binnen het half uur niet opnieuw")
        verwerk(staat, st, {"chats": chats, "vragen": [v7, v8, v10]}, dag.replace(day=25, minute=40))
        verwerk(staat, st, {"chats": fout, "vragen": [v7, v8, v10]}, dag.replace(day=25, minute=41))
        eis(len(verstuurd) == 7, "fout: na het half uur wel")

        # testmelding, opzeggen, noodrem
        t = test(staat, {"endpoint": goed["endpoint"]})
        eis(t.get("ok") and "Verstuurd naar dit toestel" in t.get("tekst", "") and berichten()[-1]["tag"] == "test", "test: naar het toestel dat erom vraagt")
        eis(not test(staat, {"endpoint": "https://fcm.googleapis.com/fcm/send/onbekend"}).get("ok"), "test: een onbekend toestel krijgt niets")
        weg = dict(goed, endpoint="https://fcm.googleapis.com/fcm/send/weg-abcdefghij")
        abonneer(staat, {"abonnement": weg}, "Mozilla/5.0 (Macintosh) Chrome/130")
        n = len(verstuurd)
        stuur_allen(staat, {"t": "x", "b": "y", "tag": "proef"}, "proef")
        eis(len(verstuurd) == n + 2 and [t["endpoint"] for t in _toestellen(werk)] == [goed["endpoint"]],
            "opgezegd (410): dat toestel verdwijnt uit de lijst, het andere blijft")
        eis(_toestellen(werk)[0]["laatst_ok"] is not None, "geslaagd: het toestel onthoudt wanneer")
        eis(afmelden(staat, {"endpoint": goed["endpoint"]}).get("toestellen") == 0, "afmelden: het toestel is weg")
        abonneer(staat, {"abonnement": goed}, "x")
        staat["uren"] = deque([time.time()] * PER_UUR)
        n = len(verstuurd)
        eis(stuur_allen(staat, {"t": "x", "b": "y", "tag": "proef"}, "proef") == [] and len(verstuurd) == n, "noodrem: niet meer dan PER_UUR per uur")

        # het nepscherm raakt de echte map nooit aan
        del os.environ["GODCHAT_PUSH_MAP"]
        eis(map_({"nep": True}) is None and beschikbaar({"nep": True})[0] is False and stuur_allen({"nep": True}, {"t": "x"}, "x") == [],
            "nepscherm: geen map, geen meldingen")
        eis(map_({}) == os.path.expanduser("~/.config/godchat/push"), "echt: de map staat buiten de werkmap")
    finally:
        globals()["PYTHON"] = echte_python
        if vorige is None:
            os.environ.pop("GODCHAT_PUSH_MAP", None)
        else:
            os.environ["GODCHAT_PUSH_MAP"] = vorige
        import shutil
        shutil.rmtree(werk, ignore_errors=True)
    print(f"{teller[0] - len(fouten)} van {teller[0]} toetsen geslaagd (meldingen)")
    return 1 if fouten else 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--zelftest":
        sys.exit(zelftest())
    print("gebruik: python3 -I push.py --zelftest", file=sys.stderr)
    sys.exit(2)
