"""WhatsApp-tab van de GodChat-app: lezen uit de store van de bridge, de adressen /wa en /api/wa/…, versturen, en de
koppeling met de open vragen van de chats (kladden en vraagkaarten).

Geladen door wa_lader.py (opnieuw bij elke wijziging van dit bestand, zonder herstart van de app). Zie CLAUDE.md.

Grenzen:
- De store van de bridge alleen-lezen; media haalt de bridge zelf op (/download). Welke store: GODCHAT_STORE, anders
  `store` in routing.json, anders ~/whatsapp-mcp/whatsapp-bridge/store (dezelfde als wacht.py).
- Versturen gaat alleen via _bridge_verstuur(), en alleen als echt_versturen() waar is: nooit in --nep, nooit met een
  proef-store tegen de echte bridge, en alleen als de eigenaar "Echt versturen" aanzette (data/wa.json).
- Alles uit de store is onbetrouwbare tekst van vreemden: het scherm zet het alleen via textContent.
- Bijlagen: een foto of video gaat nooit als origineel weg (draaiing in de pixels, alle metadata en de locatie eruit);
  in de outbox van de bridge schrijft alleen het echte verstuurpad, en nooit vanuit een proef-store of afgeschermde opstelling.
"""
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from contextlib import nullcontext
from datetime import datetime
from urllib.parse import parse_qs, quote, urlparse

HIER = os.path.dirname(os.path.abspath(__file__))
ROUTING = os.environ.get("GODCHAT_WA_ROUTING") or os.path.join(os.path.dirname(HIER), "routing.json")


OUTBOX_MAC = os.path.expanduser("~/.local/share/whatsapp-mcp/outbox")   # de outbox van de bridge op deze Mac


def _ingestelde_bridge():
    """Welke bridge die van de eigenaar is. De store: GODCHAT_STORE, anders `"store"` in routing.json, anders die van
    deze Mac (dezelfde keuze als wacht.py). Het adres en de outbox: `"bridge": {"api": <url>, "outbox": <map>}` in
    routing.json, anders http://localhost:8080/api en de outbox van deze Mac. `outbox` is de map waaruit de bridge
    bijlagen verstuurt (alleen daarbinnen aanvaardt ze een media_path). Gelezen bij het laden van wa.py: wijzigen vraagt
    een herstart."""
    store, api, outbox = "~/whatsapp-mcp/whatsapp-bridge/store", "http://localhost:8080/api", OUTBOX_MAC
    try:
        with open(ROUTING, encoding="utf-8") as fh:
            r = json.load(fh)
        store = str(r.get("store") or store)
        b = r.get("bridge")
        if isinstance(b, dict):
            api, outbox = str(b.get("api") or api), str(b.get("outbox") or outbox)
    except (OSError, ValueError, AttributeError):
        pass
    store = os.environ.get("GODCHAT_STORE") or store
    return os.path.expanduser(store), api.rstrip("/"), os.path.expanduser(outbox)


def _tweeling(api):
    """Dezelfde bridge onder zijn andere lokale naam (localhost en 127.0.0.1)."""
    return {api, api.replace("//localhost:", "//127.0.0.1:"), api.replace("//127.0.0.1:", "//localhost:")}


STANDAARD_STORE, STANDAARD_BRIDGE, STANDAARD_OUTBOX = _ingestelde_bridge()
STANDAARD_BRIDGES = tuple(sorted(_tweeling(STANDAARD_BRIDGE)))
STORE = os.environ.get("GODCHAT_WA_STORE") or STANDAARD_STORE   # de omgeving is er alleen voor tests en de demo
BRIDGE = (os.environ.get("GODCHAT_WA_BRIDGE") or STANDAARD_BRIDGE).rstrip("/")
# De bridge draait altijd op dezelfde Mac: een adres elders zou het token en de berichten naar een andere machine sturen.
BRIDGE_LOKAAL = urlparse(BRIDGE).scheme == "http" and urlparse(BRIDGE).hostname in ("localhost", "127.0.0.1", "::1")
DATA = os.environ.get("GODCHAT_WA_DATA") or os.path.join(HIER, "data")
SPRAAK = os.path.join(os.path.dirname(HIER), "spraak.py")
WHISPER_PY = os.path.expanduser(os.environ.get("GODCHAT_WHISPER_PY") or "~/.venvs/mlx-whisper/bin/python")
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
SIPS = "/usr/bin/sips"

# Een proef-store (GODCHAT_WA_STORE) praat nooit met de echte bridge: niet versturen, niet ophalen, geen health.
# Hetzelfde geldt voor een bridge-adres dat niet lokaal is.
PROEF_STORE = os.path.realpath(STORE) != os.path.realpath(STANDAARD_STORE)
ECHTE_BRIDGE = BRIDGE in STANDAARD_BRIDGES
AFGESCHERMD = (PROEF_STORE and ECHTE_BRIDGE) or not BRIDGE_LOKAAL
# De outbox van de bridge: GODCHAT_WA_OUTBOX alleen voor tests en de demo. Een proef-store komt nooit aan de echte outbox
# (niet schrijven en niet lezen); een afgeschermde opstelling schrijft er ook nooit in.
OUTBOX = os.path.expanduser(os.environ.get("GODCHAT_WA_OUTBOX") or STANDAARD_OUTBOX)
OUTBOX_ECHT = os.path.realpath(OUTBOX) in {os.path.realpath(OUTBOX_MAC), os.path.realpath(STANDAARD_OUTBOX)}
OUTBOX_DICHT = AFGESCHERMD or (PROEF_STORE and OUTBOX_ECHT)       # schrijven
OUTBOX_LEZEN_DICHT = PROEF_STORE and OUTBOX_ECHT                  # lezen (eigen verstuurde media tonen)

# Eén lijst van toegelaten Host-koppen. EXTRA_HOSTS is voor een naam van buiten (de naam van een tunnel), alleen samen
# met het slot (slot.py): dat vertaalt Host en Origin vandaag zelf naar lokaal, dus de lijst is leeg.
EXTRA_HOSTS = frozenset()
TOEGELATEN_HOSTS = frozenset()   # 127.0.0.1:<poort>, localhost:<poort> en EXTRA_HOSTS; gezet in start()
# Koppen die een proxy toevoegt (tailscale serve, een tunnel). Via zo'n proxy komt alles van 127.0.0.1: dat adres bewijst
# dus niet dat de eigenaar zelf aan deze Mac zit. Wat zulke koppen draagt, mag alleen binnen als het langs het slot kwam.
VAN_BUITEN = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded", "Via",
              "Tailscale-User-Login", "Tailscale-User-Name")   # zelfde lijst als slot.lokaal op de lokale poort
NUMMER_FOUT = ("Niet verstuurd: de WhatsApp die de tab leest is niet die van jouw nummer (`zelf` in routing.json), of dat "
               "is niet na te gaan. Versturen blijft uit tot `store` in routing.json naar de store van je eigen bridge wijst.")

MAX_TEKST = 4000
REM = 8              # seconden: dezelfde tekst naar dezelfde chat binnen deze tijd = dubbele tik
MAX_DROOG = 50       # droogloopberichten per chat in het geheugen
INLINE = {"image/jpeg", "image/png", "image/webp", "image/gif", "video/mp4", "audio/ogg", "audio/mp4", "audio/mpeg",
          "application/pdf"}
SOORTEN = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
           ".mp4": "video/mp4", ".mov": "video/quicktime", ".3gp": "video/3gpp", ".ogg": "audio/ogg", ".oga": "audio/ogg",
           ".opus": "audio/ogg", ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".aac": "audio/aac", ".pdf": "application/pdf",
           ".html": "text/html", ".htm": "text/html", ".svg": "image/svg+xml", ".txt": "text/plain"}
MEDIA_WOORD = {"image": "Foto", "video": "Video", "audio": "Spraakbericht", "document": "Document", "sticker": "Sticker"}
MEDIA_OPDRACHT = {"image": "[foto]", "video": "[video]", "audio": "[spraakbericht]", "sticker": "[sticker]"}
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
JID = re.compile(r"^[0-9A-Za-z._\-]{1,64}@[a-z.]{1,32}$")
BERICHT_ID = re.compile(r"^[0-9A-Za-z._\-]{1,128}$")
BIJLAGE_ID = re.compile(r"^[0-9a-f]{16}$")

# Bijlagen. De bridge kiest de soort op de extensie: jpg jpeg png gif webp = foto, mp4 avi mov = video, ogg = spraak,
# de rest = document (met de bestandsnaam op schijf als naam voor de ontvanger).
MAX_BIJLAGE = 100 * 1024 * 1024        # per bestand, zoals opgeladen
MAX_WACHTEND = 500 * 1024 * 1024       # alle wachtende bijlagen samen
MAX_BIJLAGEN = 10                      # per verzending
MAX_VIDEO_UIT = 50 * 1024 * 1024       # een video na het omzetten
BIJLAGE_LEVEN = 2 * 3600               # seconden: een wachtende bijlage die niet verstuurd werd, wordt opgeruimd
MAX_ZIJDE_FOTO = 2560
MAX_ZIJDE_VIDEO = 1280
PNG_BLIJFT = 3 * 1024 * 1024           # een png tot zo groot (en hoogstens MAX_ZIJDE_FOTO) blijft png: tekst blijft scherp
VIDEO_TIJD = 300                       # seconden voor het omzetten van één video
MAX_SPRAAK = 900                       # seconden: een opgenomen spraakbericht hoogstens zo lang (het scherm stopt op 15 min)
MIN_SPRAAK = 0.7                       # seconden: korter is een misser, geen bericht
SPRAAK_TIJD = 120                      # seconden voor het omzetten van één opname
MEDIA_TIMEOUT = 75                     # seconden wachten op de bridge bij een bijlage (de bridge zelf geeft het na 60 s op)
MAX_VERSTUURD = 2000                   # regels in data/wa/verstuurd.json
WACHT_DAGEN = 14                       # ouder dan dit wacht een gesprek niet meer op jou
# korte afsluiters: als geheel bericht (met of zonder uitroepteken of emoji erachter) vragen ze waarschijnlijk geen antwoord
AFSLUITERS = frozenset({
    "ok", "okay", "oke", "oké", "okee", "okeé", "okey", "oki", "okido", "okidoki", "k", "kk",
    "top", "super", "perfect", "perfecto", "prima", "great", "nice", "cool", "fijn", "mooi", "goed", "is goed", "oké dan",
    "ok dan", "allright", "alright", "all right", "yes", "ja", "jep", "yep",
    "thanks", "thank you", "thank u", "thanks a lot", "many thanks", "thx", "tnx", "ty", "cheers",
    "merci", "merci beaucoup", "mercikes", "merciekes", "dank je", "dank je wel", "dankjewel", "dankje", "dank u",
    "dank u wel", "dankuwel", "bedankt", "heel erg bedankt", "super bedankt", "alvast bedankt",
    "tot morgen", "tot straks", "tot later", "tot dan", "see you", "see you tomorrow", "see you later", "see ya", "cya",
})
FOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi"}   # .avi ook: de bridge zou hem anders onbewerkt als video sturen

APP = None
NEP = False
POORT = 8742
STAAT = {}


class Fout(Exception):
    """Een fout voor het scherm: code + tekst in gewone woorden."""

    def __init__(self, code, tekst, koppen=None):
        super().__init__(tekst)
        self.code, self.tekst, self.koppen = code, tekst, koppen


def start(app, nep, poort, staat):
    global APP, NEP, POORT, STAAT, TOEGELATEN_HOSTS
    APP, NEP, POORT, STAAT = app, bool(nep), int(poort), staat
    TOEGELATEN_HOSTS = frozenset({f"127.0.0.1:{POORT}", f"localhost:{POORT}"}) | EXTRA_HOSTS
    staat.setdefault("tik", secrets.token_urlsafe(24))
    staat.setdefault("slot", threading.Lock())
    staat.setdefault("slot_lijst", threading.Lock())
    staat.setdefault("slot_spraak", threading.Lock())
    staat.setdefault("slot_m4a", threading.Lock())
    staat.setdefault("droog", {})
    staat.setdefault("rem", {})
    staat.setdefault("nep_versturen", "droog")
    staat.setdefault("bijlagen", {})
    staat.setdefault("slot_verwerk", threading.BoundedSemaphore(2))   # hoogstens twee ffmpeg's tegelijk
    staat.setdefault("slot_verstuurd", threading.Lock())
    os.makedirs(os.path.join(DATA, "wa", "m4a"), exist_ok=True)
    try:
        _ruim_bijlagen_op()
    except OSError:
        pass
    if "lijst" not in staat and not staat.get("warm_bezig"):   # de eerste lijst niet laten wachten op het aggregaat
        staat["warm_bezig"] = True

        def warm():
            try:
                con = db()
                try:
                    laatste(con)
                finally:
                    con.close()
                namenboek()
                bridge_stand()
            except Exception:
                pass
            finally:
                staat["warm_bezig"] = False
        threading.Thread(target=warm, daemon=True).start()


# --- instelling en de ene beslissing over versturen ----------------------------------------------------------------
def _instelling_pad():
    return os.path.join(DATA, "wa.json")


def instelling():
    if NEP:
        return STAAT.get("nep_versturen", "droog")
    try:
        with open(_instelling_pad(), encoding="utf-8") as fh:
            return "echt" if json.load(fh).get("versturen") == "echt" else "droog"
    except (OSError, ValueError, AttributeError):
        return "droog"


def _schrijf_json(pad, data):
    os.makedirs(os.path.dirname(pad), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(pad), prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, pad)


def wil_echt():
    """Staat "Echt versturen" aan en mag deze opstelling met de bridge praten?"""
    return (not NEP) and instelling() == "echt" and not AFGESCHERMD


def echt_versturen():
    """De enige beslissing: wordt er echt verstuurd, of is het een droogloop? Alleen vanaf het eigen nummer."""
    return wil_echt() and eigen_nummer() is True


# --- bridge --------------------------------------------------------------------------------------------------------
def _token():
    try:
        with open(os.path.join(STORE, ".bridge-token"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _bridge(pad, body=None, timeout=10):
    """Eén aanvraag aan de bridge, zonder systeemproxy. Het token blijft hier. Versturen gaat via _bridge_verstuur."""
    if pad not in ("/health", "/download"):
        raise Fout(500, "dit adres van de bridge mag alleen via _bridge_verstuur")
    if AFGESCHERMD:
        raise Fout(409, "proef-store: de WhatsApp-tab praat niet met de echte bridge")
    return _bridge_open(pad, body, timeout)


def _bridge_open(pad, body, timeout):
    kop = {"content-type": "application/json"}
    if _token():
        kop["Authorization"] = "Bearer " + _token()
    req = urllib.request.Request(BRIDGE + pad, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST", headers=kop)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read()


def _bridge_verstuur(pad, body, timeout=20):
    """De enige plek die /send of /react van de bridge aanroept. Controleert zelf nog eens of dat mag."""
    if pad not in ("/send", "/react"):
        raise Fout(500, "onbekend verstuuradres")
    if not echt_versturen():
        raise Fout(409, "versturen staat uit (droogloop): niets verstuurd")
    return _bridge_open(pad, body, timeout)


def bridge_stand():
    """Toestand van de bridge, hoogstens elke 15 s gevraagd, op de achtergrond: een peiling wacht er nooit op."""
    if not BRIDGE_LOKAAL:
        return {"ok": False, "verbonden": False}
    if AFGESCHERMD:
        return {"ok": True, "verbonden": None, "proef": True}
    b = STAAT.get("bridge") or {"ok": None, "verbonden": None}
    if time.time() - STAAT.get("bridge_t", 0) > 15 and not STAAT.get("bridge_bezig"):
        STAAT["bridge_bezig"] = True

        def kijk():
            try:
                code, raw = _bridge("/health", timeout=3)
                d = json.loads(raw or b"{}")
                STAAT["bridge"] = {"ok": True, "verbonden": bool(d.get("connected"))}
            except urllib.error.HTTPError as fout:
                STAAT["bridge"] = {"ok": True, "verbonden": False} if fout.code == 503 else {"ok": False, "verbonden": False}
            except Exception:
                STAAT["bridge"] = {"ok": False, "verbonden": False}
            STAAT["bridge_t"] = time.time()
            STAAT["bridge_bezig"] = False
        if "bridge" not in STAAT:
            kijk()   # de eerste keer even wachten (hoogstens 3 s)
            b = STAAT["bridge"]
        else:
            threading.Thread(target=kijk, daemon=True).start()
    return b


# --- store -----------------------------------------------------------------------------------------------------------
def db(naam="messages.db"):
    return sqlite3.connect(f"file:{quote(os.path.join(STORE, naam))}?mode=ro", uri=True, timeout=10)


def routing():
    try:
        with open(ROUTING, encoding="utf-8") as fh:
            r = json.load(fh)
    except (OSError, ValueError):
        r = {}
    r.setdefault("zelf", "")
    r.setdefault("klanten", {})
    r.setdefault("bezet", {})
    return r


def eigen_nummer():
    """Is de store die de tab leest die van het eigen nummer (`zelf` in routing.json)? True, False, of None als het
    niet na te gaan is. Op een Mac met meer dan één bridge mag de tab nooit vanaf een ander nummer versturen. De bridge
    zelf hoort vanzelf bij de store: het token komt uit de store en een andere bridge weigert het."""
    c = STAAT.get("eigen")
    if c and time.time() - c[0] < 60:
        return c[1]
    uit = None
    zelf = str(routing().get("zelf") or "").split("@")[0]
    try:
        con = db("whatsapp.db")
        try:
            toestellen = [str(j) for (j,) in con.execute("select jid from whatsmeow_device") if j]
        finally:
            con.close()
        if zelf and toestellen:   # een toestel-jid is <nummer>:<toestel>@s.whatsapp.net
            uit = all(j.split("@")[0].split(":")[0] == zelf for j in toestellen)
    except sqlite3.Error:
        uit = None
    STAAT["eigen"] = (time.time(), uit)
    return uit


def _schone_naam(naam):
    """'Anna (boekhouding)' → 'Anna'."""
    return re.sub(r"\s*\(.*?\)\s*", " ", str(naam or "")).strip()


def is_nummer(s):
    return bool(re.fullmatch(r"\+?[\d\s\-()]{6,}", (s or "").strip()))


def mooi_nummer(nummer):
    n = re.sub(r"\D", "", nummer or "")
    if not n:
        return "onbekend"
    if len(n) <= 6:
        return "+" + n
    rest = n[2:]
    return "+" + n[:2] + " " + " ".join(rest[i:i + 3] for i in range(0, len(rest), 3))


def namenboek():
    """Adresboek uit whatsapp.db: nummer → namen, en lid → nummer. Hoogstens elke 60 s opnieuw gelezen."""
    nb = STAAT.get("namenboek")
    if nb and time.time() - nb["t"] < 60:
        return nb
    boek, lids = {}, {}
    try:
        con = db("whatsapp.db")
        try:
            for jid, first, full, push, bus in con.execute(
                    "select their_jid, first_name, full_name, push_name, business_name from whatsmeow_contacts"):
                if not jid:
                    continue
                boek[jid.split("@")[0]] = {"full": (full or "").strip(), "first": (first or "").strip(),
                                           "push": (push or "").strip(), "business": (bus or "").strip()}
            for lid, pn in con.execute("select lid, pn from whatsmeow_lid_map"):
                lids[str(lid).split("@")[0]] = str(pn).split("@")[0]
            archief = {j for (j,) in con.execute("select chat_jid from whatsmeow_chat_settings where archived")}
        finally:
            con.close()
    except sqlite3.Error:
        archief = set()
    nb = {"t": time.time(), "boek": boek, "lids": lids, "archief": archief}
    STAAT["namenboek"] = nb
    return nb


def chatnamen(con):
    return {j: (n or "") for j, n in con.execute("select jid, name from chats")}


def routing_namen(r):
    uit = {}
    for k in r["klanten"].values():
        for jid, naam in (k.get("whatsapp") or {}).items():
            uit[jid] = str(naam)
    return uit


def klant_van(r):
    uit = {}
    for sleutel, k in r["klanten"].items():
        for jid in (k.get("whatsapp") or {}):
            uit[jid] = sleutel
    return uit


def naam_voor(jid, nb, cn, rn):
    """Naam van een chat of afzender: adresboek → chats.name (geen kaal nummer) → routing → pushnaam → zakelijk → nummer."""
    if jid.endswith("@g.us"):
        return (cn.get(jid) or "").strip() or "Groep zonder naam"
    nummer = jid.split("@")[0]
    if jid.endswith("@lid"):
        nummer = nb["lids"].get(nummer, nummer)
    c = nb["boek"].get(nummer) or {}
    pn_jid = nummer + "@s.whatsapp.net"
    chatnaam = (cn.get(jid) or cn.get(pn_jid) or "").strip()
    for kandidaat in (c.get("full"), "" if is_nummer(chatnaam) else chatnaam, _schone_naam(rn.get(jid) or rn.get(pn_jid)),
                      c.get("push"), c.get("business")):
        if kandidaat and kandidaat.strip():
            return kandidaat.strip()
    return mooi_nummer(nummer)


def afzender_bekend(sender, chat_jid):
    """Een telefoonnummer. In een groep is de afzender soms het groeps-id zelf: dan is hij onbekend."""
    s = sender or ""
    if not (s.isdigit() and 7 <= len(s) <= 15):
        return False
    return not (chat_jid.endswith("@g.us") and s == chat_jid.split("@")[0])


def ts_s(ts):
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        return 0.0


def lokaal(ts):
    try:
        d = datetime.fromisoformat(str(ts)).astimezone()
        return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]} {d:%H:%M}"
    except ValueError:
        return str(ts)[:16]


def uur():
    return datetime.now().strftime("%H:%M")


# --- laatste bericht per chat (cache: het aggregaat over de hele store kost ±130 ms) ------------------------------
def _laatste_volledig():
    con = db()
    try:
        con.execute("begin")
        mx = con.execute("select coalesce(max(rowid), 0) from messages").fetchone()[0]
        per = {j: (jd, rid) for j, rid, jd in con.execute(
            "select chat_jid, rowid, max(julianday(timestamp)) from messages "
            "where coalesce(media_type, '') != 'reaction' group by chat_jid") if jd is not None}
        con.rollback()
    finally:
        con.close()
    return {"max": mx, "per": per, "t": time.time()}


def _laatste_bij(L, con, tot):
    """Werkt de cache bij met de rijen na L['max'] (nieuwe berichten, ook oude uit een history-sync)."""
    if tot <= L["max"]:
        return []
    rijen = con.execute("select chat_jid, rowid, julianday(timestamp), media_type, filename from messages "
                        "where rowid > ? and rowid <= ? order by rowid", (L["max"], tot)).fetchall()
    per = L["per"]
    for jid, rid, jd, soort, _ in rijen:
        if soort == "reaction" or jd is None:
            continue
        oud = per.get(jid)
        if not oud or (jd, rid) > oud:
            per[jid] = (jd, rid)
    L["max"] = tot
    return rijen


def laatste(con):
    """(cache, nieuwe rijen sinds de vorige blik). Elke 5 min op de achtergrond helemaal opnieuw (verwijderingen)."""
    with STAAT["slot_lijst"]:
        L = STAAT.get("lijst")
        if L is None:
            L = STAAT["lijst"] = _laatste_volledig()
        tot = con.execute("select coalesce(max(rowid), 0) from messages").fetchone()[0]
        _laatste_bij(L, con, tot)
        if time.time() - L["t"] > 300 and not STAAT.get("lijst_bezig"):
            STAAT["lijst_bezig"] = True
            L["t"] = time.time()

            def opnieuw():
                try:
                    nieuw = _laatste_volledig()
                    with STAAT["slot_lijst"]:
                        c2 = db()
                        try:
                            _laatste_bij(nieuw, c2, max(nieuw["max"], STAAT["lijst"]["max"]))
                        finally:
                            c2.close()
                        STAAT["lijst"] = nieuw
                except sqlite3.Error:
                    pass
                finally:
                    STAAT["lijst_bezig"] = False
            threading.Thread(target=opnieuw, daemon=True).start()
        return L


# --- kladden en vraagkaarten ------------------------------------------------------------------------------------------
def koppeling(r=None):
    """Uit de open vragen van de chats: kladden (vraag met whatsapp.aan) en vraagkaarten, per WhatsApp-chat."""
    r = r or routing()
    try:
        st = APP.stand()
    except Exception:
        st = {"chats": [], "vragen": []}
    chatnaam = {c.get("id"): c.get("naam") for c in st.get("chats") or []}
    uit = {}
    for v in st.get("vragen") or []:
        if v.get("status", "open") != "open":
            continue
        klant = str(v.get("chat") or "").split("--")[0]
        k = r["klanten"].get(klant) or {}
        item = {"nr": v.get("nr"), "van": klant, "van_naam": k.get("naam") or chatnaam.get(klant) or klant,
                "vraag": str(v.get("vraag") or ""), "context": str(v.get("context") or ""),
                "concept": str(v.get("concept") or ""),
                "antwoorden": [str(a) for a in (v.get("antwoorden") or []) if isinstance(a, str)], "uur": v.get("uur") or ""}
        wa = v.get("whatsapp")
        if isinstance(wa, dict) and wa.get("aan") and wa.get("bericht"):
            uit.setdefault(str(wa["aan"]), []).append(dict(item, soort="klad", bericht=str(wa["bericht"])))
            continue
        chats = k.get("whatsapp") or {}
        if len(chats) == 1:
            doel = list(chats)
        else:
            hooi = " ".join((item["vraag"], item["context"], item["concept"][:300]))
            doel = []
            for jid, naam in chats.items():
                naam = _schone_naam(naam)
                if jid.endswith("@g.us"):
                    woord = re.sub(r"^groep\s+", "", naam, flags=re.I)
                else:
                    woord = naam.split(" ")[0] if naam else ""
                if len(re.sub(r"\W", "", woord)) >= 3 and re.search(r"(?<!\w)" + re.escape(woord) + r"(?!\w)", hooi, re.I):
                    doel.append(jid)
        for jid in doel:
            uit.setdefault(jid, []).append(dict(item, soort="vraag"))
    return uit


def _kv(kop):
    return hashlib.sha1(json.dumps(kop, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


# --- chats en berichten -----------------------------------------------------------------------------------------------
def _ctx(con, r=None):
    r = r or routing()
    return {"r": r, "nb": namenboek(), "cn": chatnamen(con), "rn": routing_namen(r), "kv": klant_van(r),
            "klanten": r["klanten"], "zelf": r.get("zelf") or ""}


def _kort(tekst, n=140):
    t = " ".join((tekst or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


# --- wie wacht op jou -------------------------------------------------------------------------------------------------
def _jd(unix):
    return unix / 86400.0 + 2440587.5


def _unix(jd):
    return (jd - 2440587.5) * 86400.0


def eigen_ids():
    """Het eigen nummer en lid (zonder toestel en domein), uit whatsmeow_device. In een groep staat een vermelding van
    de eigenaar als @<lid>; @<nummer> telt ook. Hoogstens elke 60 s opnieuw gelezen."""
    c = STAAT.get("eigen_ids")
    if c and time.time() - c[0] < 60:
        return c[1]
    ids = set()
    try:
        con = db("whatsapp.db")
        try:
            try:
                rijen = con.execute("select jid, lid from whatsmeow_device").fetchall()
            except sqlite3.OperationalError:   # oudere store zonder lid-kolom
                rijen = [(j, None) for (j,) in con.execute("select jid from whatsmeow_device")]
        finally:
            con.close()
        for rij in rijen:
            for v in rij:
                d = str(v or "").split("@")[0].split(":")[0]
                if d.isdigit() and len(d) >= 6:
                    ids.add(d)
    except sqlite3.Error:
        pass
    ids = frozenset(ids)
    STAAT["eigen_ids"] = (time.time(), ids)
    return ids


def is_afsluiter(tekst, soort=""):
    """Vraagt dit bericht waarschijnlijk geen antwoord? Alleen emoji of leestekens, een sticker zonder tekst, of een
    bericht dat helemaal uit korte afsluiters bestaat ("Top, bedankt!", "ok 👍", "Merci, tot morgen")."""
    t = (tekst or "").strip()
    if soort == "sticker" and not t:
        return True
    if soort and soort not in ("tekst", "text") or not t:
        return False
    woorden = re.sub(r"[^\w\s]|_", " ", t.lower()).split()
    if not woorden:
        return True   # alleen emoji of leestekens
    if len(woorden) > 6:
        return False
    i = 0
    while i < len(woorden):
        for lengte in (3, 2, 1):
            if " ".join(woorden[i:i + lengte]) in AFSLUITERS and i + lengte <= len(woorden):
                i += lengte
                break
        else:
            return False
    return True


def klaar_lijst():
    """Afgevinkte gesprekken: jid → hoogste rowid die de eigenaar afvinkte. In --nep alleen in het geheugen."""
    if NEP:
        return STAAT.setdefault("nep_klaar", {})
    k = STAAT.get("klaar")
    if k is None:
        try:
            with open(os.path.join(DATA, "wa", "klaar.json"), encoding="utf-8") as fh:
                ruw = json.load(fh)
            k = {str(j): int(v) for j, v in ruw.items() if isinstance(v, int) and not isinstance(v, bool)}
        except (OSError, ValueError, AttributeError):
            k = {}
        STAAT["klaar"] = k
    return k


ECHT_BERICHT = "coalesce(media_type, '') != 'reaction' and (deleted_at is null or deleted_at = '')"


def wacht_voor(con, jid, rij, ctx, klaar, eigen, nu=None):
    """Wacht dit gesprek op een antwoord van de eigenaar? Geeft {"sinds", "n", "soort", "tot"} of None.

    In gewone woorden: kijk naar het laatste echte bericht (geen reactie, niet verwijderd). Het gesprek wacht als dat
    bericht van de ander komt, hoogstens 14 dagen oud is, de eigenaar er niet met een emoji op reageerde en het niet
    afvinkte (of er kwam daarna weer iets van de ander). Nooit: de eigen chat (de zelfchat), een kanaal waar een lus
    antwoordt (`bezet`), een gearchiveerde chat of een broadcast. In een groep van een klant geldt hetzelfde; een gewone
    groep wacht alleen als iemand er de eigenaar sinds het laatste eigen bericht (en binnen de 14 dagen) vermeldde
    (@<lid> of @<nummer>) of op een eigen bericht antwoordde.

    `sinds` is het eerste bericht van de ander sinds het laatste eigen bericht (bij een gewone groep: het eerste dat de
    eigenaar aanspreekt), `n` het aantal berichten van de ander vanaf daar (hoogstens 99), `tot` de rowid van het laatste.
    `soort`: "vraag" als een van de laatste vijf een ? bevat; "afgerond" als het laatste een afsluiter is en er
    nergens een ? in staat (zie is_afsluiter); anders "gewoon".

    Snel voor wat niet wacht: zonder vraag aan de database als het laatste bericht een eigen bericht is of te oud."""
    _, _, _, _, ts, van_mij, _, _, verwijderd = rij
    if jid == ctx["zelf"] or jid in ctx["r"]["bezet"] or jid in ctx["nb"]["archief"] or jid.endswith("@broadcast"):
        return None
    nu = nu or time.time()
    grens = nu - WACHT_DAGEN * 86400
    if ts_s(ts) < grens or (van_mij and not verwijderd):
        return None
    laatst = con.execute(f"select rowid, id, content, julianday(timestamp), is_from_me, media_type from messages "
                         f"where chat_jid = ? and {ECHT_BERICHT} order by julianday(timestamp) desc, rowid desc limit 1",
                         (jid,)).fetchone()
    if not laatst or laatst[4] or laatst[3] is None or _unix(laatst[3]) < grens:
        return None
    l_rid, l_id, l_tekst, l_jd, _, l_soort = laatst
    if klaar.get(jid, 0) >= l_rid:
        return None
    reactie = con.execute("select content from messages where chat_jid = ? and media_type = 'reaction' and is_from_me = 1 "
                          "and filename = ? order by julianday(timestamp) desc, rowid desc limit 1", (jid, l_id)).fetchone()
    if reactie and (reactie[0] or "").strip():
        return None   # een emoji op het laatste bericht is ook een antwoord
    mijn = con.execute("select julianday(timestamp), rowid from messages where chat_jid = ? and is_from_me = 1 and "
                       "coalesce(media_type, '') != 'reaction' order by julianday(timestamp) desc, rowid desc limit 1",
                       (jid,)).fetchone() or (-1.0, -1)
    na = "(julianday(timestamp) > ? or (julianday(timestamp) = ? and rowid > ?))"
    na_args = [mijn[0], mijn[0], mijn[1]]
    if jid.endswith("@g.us") and jid not in ctx["kv"]:
        # gewone groep: alleen als iemand de eigenaar aansprak, binnen de 14 dagen en na het laatste eigen bericht
        rijen = con.execute(
            f"select rowid, julianday(timestamp), content, quoted_message_id from messages where chat_jid = ? and "
            f"is_from_me = 0 and {ECHT_BERICHT} and julianday(timestamp) >= ? and {na} order by julianday(timestamp), rowid",
            [jid, _jd(grens), *na_args]).fetchall()
        mij_ids = None
        start = None
        for i, (_, _, tekst, citaat) in enumerate(rijen):
            if any(re.search("@" + e + r"(?!\d)", tekst or "") for e in eigen):
                start = i
                break
            if citaat:
                if mij_ids is None:
                    mij_ids = {x for (x,) in con.execute(
                        "select id from messages where chat_jid = ? and is_from_me = 1", (jid,))}
                if citaat in mij_ids:
                    start = i
                    break
        if start is None:
            return None
        reeks = rijen[start:]
        n, sinds = len(reeks), reeks[0][1]
        teksten = [r[2] or "" for r in reeks]
        vijf, ergens = teksten[-5:], any("?" in t for t in teksten)
    else:
        n, sinds, ergens = con.execute(
            f"select count(*), min(julianday(timestamp)), max(instr(coalesce(content, ''), '?') > 0) from messages "
            f"where chat_jid = ? and is_from_me = 0 and {ECHT_BERICHT} and {na}", [jid, *na_args]).fetchone()
        if not n:
            return None
        vijf = [t or "" for (t,) in con.execute(
            f"select content from messages where chat_jid = ? and is_from_me = 0 and {ECHT_BERICHT} and {na} "
            "order by julianday(timestamp) desc, rowid desc limit 5", [jid, *na_args])]
    if any("?" in t for t in vijf):
        soort = "vraag"
    elif not ergens and is_afsluiter(l_tekst, l_soort or ""):
        soort = "afgerond"
    else:
        soort = "gewoon"
    return {"sinds": round(_unix(sinds)), "n": min(int(n), 99), "soort": soort, "tot": l_rid}


def wacht_n(con, L, ctx=None, lijst=None):
    """Aantal wachtende gesprekken zonder de soort "afgerond". Onthouden tot er een nieuw bericht is, er iets afgevinkt
    wordt of er een minuut voorbij is; met `lijst` (alle lijst-items) geteld uit die lijst."""
    sleutel = (L["max"], STAAT.get("klaar_v", 0))
    c = STAAT.get("wacht_tel")
    if lijst is None and c and c[0] == sleutel and time.time() - c[1] < 60:
        return c[2]
    if lijst is None:
        ctx = ctx or _ctx(con)
        grens = _jd(time.time() - WACHT_DAGEN * 86400)
        lijst = chat_items(con, [j for j, (jd, _) in L["per"].items() if jd >= grens], ctx, {})
    n = sum(1 for x in lijst if x.get("wacht") and x["wacht"]["soort"] != "afgerond")
    STAAT["wacht_tel"] = (sleutel, time.time(), n)
    return n


def klaar(body):
    """Afvinken ("hoeft geen antwoord") tot en met rowid `tot`, of met `terug: true` het vinkje weghalen."""
    jid = body.get("chat")
    con = db()
    try:
        _chat_bestaat(con, jid)
        L = laatste(con)
        with STAAT["slot"]:
            k = dict(klaar_lijst())
            if body.get("terug") is True:
                k.pop(jid, None)
            else:
                tot = body.get("tot")
                if not isinstance(tot, int) or isinstance(tot, bool) or tot <= 0:
                    raise Fout(400, "tot is het nummer van het laatste bericht")
                k[jid] = max(min(tot, L["max"]), k.get(jid, 0))
            if NEP:
                STAAT["nep_klaar"] = k
            else:
                _schrijf_json(os.path.join(DATA, "wa", "klaar.json"), k)
                STAAT["klaar"] = k
            STAAT["klaar_v"] = STAAT.get("klaar_v", 0) + 1
        ctx = _ctx(con)
        item = (chat_items(con, [jid], ctx) or [None])[0]
        return {"ok": True, "chat": item, "wacht_n": wacht_n(con, L, ctx)}
    finally:
        con.close()


def chat_items(con, jids, ctx, kop=None):
    L = laatste(con)
    klaar_k, eigen, nu = klaar_lijst(), eigen_ids(), time.time()
    kop = koppeling(ctx["r"]) if kop is None else kop
    jids = [j for j in jids if j in L["per"] and j != "status@broadcast" and not j.endswith("@broadcast")]
    if not jids:
        return []
    rowids = [L["per"][j][1] for j in jids]
    rijen = {}
    for i in range(0, len(rowids), 900):
        stuk = rowids[i:i + 900]
        for rij in con.execute("select rowid, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, "
                               f"deleted_at from messages where rowid in ({','.join('?' * len(stuk))})", stuk):
            rijen[rij[1]] = rij
    uit = []
    for jid in jids:
        rij = rijen.get(jid)
        if not rij:
            continue
        _, _, sender, content, ts, van_mij, soort, bestand, verwijderd = rij
        wacht = wacht_voor(con, jid, rij, ctx, klaar_k, eigen, nu)
        groep = jid.endswith("@g.us")
        naam = "Jij (notities)" if jid == ctx["zelf"] else naam_voor(jid, ctx["nb"], ctx["cn"], ctx["rn"])
        if verwijderd:
            tekst = "Dit bericht is verwijderd"
        elif soort == "document":
            tekst = "Document: " + (bestand or "") + (" · " + content if content else "")
        elif soort in MEDIA_WOORD:
            tekst = MEDIA_WOORD[soort] + (" · " + content if content else "")
        else:
            tekst = content or ""
        wie = ""
        if groep and not van_mij and afzender_bekend(sender, jid):
            wie = naam_voor(sender + "@s.whatsapp.net", ctx["nb"], ctx["cn"], ctx["rn"])
        klant = ctx["kv"].get(jid)
        rn = _schone_naam(ctx["rn"].get(jid)) if jid in ctx["rn"] else None
        rol = re.search(r"\(([^()]{2,80})\)", str(ctx["rn"].get(jid) or ""))   # "Anna (boekhouding)" → de rol
        k = kop.get(jid) or []
        uit.append({"jid": jid, "naam": naam, "groep": groep, "klant": klant,
                    "klant_naam": (ctx["klanten"].get(klant) or {}).get("naam") if klant else None,
                    "routing_naam": rn if rn and rn != naam else None, "rol": rol.group(1) if rol else None,
                    "bezet": ctx["r"]["bezet"].get(jid), "zelf": jid == ctx["zelf"],
                    "archief": jid in ctx["nb"]["archief"],
                    "laatste": {"ts": ts_s(ts), "tekst": _kort(tekst), "soort": soort or "tekst", "van_mij": bool(van_mij),
                                "wie": wie},
                    "wacht": wacht,
                    "klad": sum(1 for x in k if x["soort"] == "klad"), "vraag": sum(1 for x in k if x["soort"] == "vraag")})
    uit.sort(key=lambda c: -c["laatste"]["ts"])
    return uit


def _chat_bestaat(con, jid):
    if not isinstance(jid, str) or not JID.match(jid) or jid.endswith("@broadcast"):
        raise Fout(400, "onbekend gesprek")
    if not con.execute("select 1 from chats where jid = ?", (jid,)).fetchone():
        raise Fout(404, "dat gesprek staat niet in WhatsApp")
    return jid


def _chatmap(jid):
    return os.path.join(STORE, jid.replace(":", "_"))


def lokale_media(jid):
    """id → bestandsnaam van wat al op schijf staat in de map van de chat (namen als image_20260626_140313_<id>.jpg)."""
    map_ = _chatmap(jid)
    try:
        mt = os.stat(map_).st_mtime_ns
    except OSError:
        return {}
    cache = STAAT.setdefault("media_lijst", {})
    c = cache.get(jid)
    if c and c[0] == mt:
        return c[1]
    uit = {}
    for naam in os.listdir(map_):
        delen = naam.split("_", 3)
        if len(delen) == 4:
            uit[delen[3].split(".")[0]] = naam
    if len(cache) > 200:
        cache.clear()
    cache[jid] = (mt, uit)
    return uit


def _spraak():
    pad = os.path.join(DATA, "wa", "spraak.json")
    c = STAAT.get("spraak")
    try:
        mt = os.stat(pad).st_mtime_ns
    except OSError:
        return {}
    if not c or c[0] != mt:
        try:
            with open(pad, encoding="utf-8") as fh:
                c = (mt, json.load(fh))
        except (OSError, ValueError):
            c = (mt, {})
        STAAT["spraak"] = c
    return c[1]


KOL = ("rowid, id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, file_length, deleted_at, "
       "quoted_message_id, julianday(timestamp)")


def _berichten_uit(con, jid, rijen, ctx):
    """Rijen uit messages → berichten voor het scherm, met reacties en antwoord-op."""
    if not rijen:
        return []
    ids = [r[1] for r in rijen]
    groep = jid.endswith("@g.us")
    reacties = {}
    for i in range(0, len(ids), 900):
        stuk = ids[i:i + 900]
        for sender, van_mij, emoji, doel in con.execute(
                "select sender, is_from_me, content, filename from messages where chat_jid = ? and media_type = 'reaction' "
                f"and filename in ({','.join('?' * len(stuk))}) order by julianday(timestamp), rowid", [jid, *stuk]):
            reacties.setdefault(doel, {})["mij" if van_mij else (sender or "?")] = emoji or ""
    citaten = {}
    q_ids = list({r[11] for r in rijen if r[11]})
    for i in range(0, len(q_ids), 900):
        stuk = q_ids[i:i + 900]
        for qid, sender, van_mij, content, soort, bestand in con.execute(
                "select id, sender, is_from_me, content, media_type, filename from messages where chat_jid = ? "
                f"and id in ({','.join('?' * len(stuk))})", [jid, *stuk]):
            citaten[qid] = (sender, van_mij, content, soort, bestand)
    lokaal_ = lokale_media(jid)
    spraak = _spraak()
    naam_chat = naam_voor(jid, ctx["nb"], ctx["cn"], ctx["rn"])

    def wie_van(sender, van_mij):
        if van_mij:
            return "Jij", ctx["zelf"] or None
        if groep:
            if afzender_bekend(sender, jid):
                return naam_voor(sender + "@s.whatsapp.net", ctx["nb"], ctx["cn"], ctx["rn"]), sender + "@s.whatsapp.net"
            return "", None
        return naam_chat, (sender + "@s.whatsapp.net") if afzender_bekend(sender, jid) else None

    uit = []
    for rid, mid, _, sender, content, ts, van_mij, soort, bestand, grootte, verwijderd, qid, jd in rijen:
        wie, wie_jid = wie_van(sender, van_mij)
        r = reacties.get(mid) or {}
        tel = {}
        for wie_r, emoji in r.items():
            if emoji:
                t = tel.setdefault(emoji, {"emoji": emoji, "n": 0, "van_mij": False})
                t["n"] += 1
                t["van_mij"] = t["van_mij"] or wie_r == "mij"
        antwoord_op = None
        if qid:
            q = citaten.get(qid)
            if q:
                qw, _ = wie_van(q[0], q[1])
                qt = q[2] or ""
                if not qt and q[3]:
                    qt = (MEDIA_WOORD.get(q[3], q[3]) + (": " + q[4] if q[3] == "document" and q[4] else ""))
                antwoord_op = {"id": qid, "wie": qw, "tekst": _kort(qt, 160), "soort": q[3] or "tekst"}
            else:
                antwoord_op = {"id": qid, "ontbreekt": True}
        soort = soort or "tekst"
        uit.append({"id": mid, "rowid": rid, "ts": ts_s(ts), "jd": jd, "van_mij": bool(van_mij), "wie": wie,
                    "wie_jid": wie_jid, "tekst": "" if verwijderd else (content or ""), "soort": soort,
                    "bestand": bestand if soort == "document" else None, "grootte": grootte or None,
                    "lokaal": (mid in lokaal_ or (bool(van_mij) and _eigen_media(jid, bestand, ts_s(ts)) is not None))
                    if soort in MEDIA_WOORD else False,
                    "antwoord_op": antwoord_op, "reacties": list(tel.values()), "verwijderd": bool(verwijderd),
                    "uitgeschreven": spraak.get(f"{jid}|{mid}") if soort == "audio" else None,
                    "beantwoordbaar": (not verwijderd) and wie_jid is not None})
    return uit


def kladden_voor(jid, kop):
    return kop.get(jid) or []


def berichten(jid, voor=None, n=60):
    n = max(1, min(int(n or 60), 300))
    con = db()
    try:
        _chat_bestaat(con, jid)
        ctx = _ctx(con)
        waar, args = "chat_jid = ? and coalesce(media_type, '') != 'reaction'", [jid]
        if voor:
            try:
                jd_s, rid_s = str(voor).split(":", 1)
                jd, rid = float(jd_s), int(rid_s)
            except ValueError:
                raise Fout(400, "onbekende cursor")
            waar += " and (julianday(timestamp) < ? or (julianday(timestamp) = ? and rowid < ?))"
            args += [jd, jd, rid]
        rijen = con.execute(f"select {KOL} from messages where {waar} order by julianday(timestamp) desc, rowid desc "
                            "limit ?", [*args, n + 1]).fetchall()
        meer = len(rijen) > n
        rijen = list(reversed(rijen[:n]))
        lijst = _berichten_uit(con, jid, rijen, ctx)
        kop = koppeling(ctx["r"])
        chat = (chat_items(con, [jid], ctx, kop) or [None])[0]
        cursor = f"{rijen[0][12]!r}:{rijen[0][0]}" if rijen else None
        with STAAT["slot"]:
            droog = list(STAAT["droog"].get(jid, []))
        return {"chat": chat, "berichten": lijst, "meer": meer, "cursor": cursor, "kladden": kladden_voor(jid, kop),
                "droog": droog}
    finally:
        con.close()


def chats():
    con = db()
    try:
        ctx = _ctx(con)
        L = laatste(con)
        kop = koppeling(ctx["r"])
        lijst = chat_items(con, list(L["per"]), ctx, kop)
        klanten = []
        try:
            for c in APP.stand().get("chats") or []:
                if c.get("soort") in ("god", "klant"):
                    klanten.append({"id": c["id"], "naam": c.get("naam") or c["id"]})
        except Exception:
            pass
        return {"nu": time.time(), "max": L["max"], "nep": NEP, "versturen": instelling(), "echt": echt_versturen(),
                "proef": AFGESCHERMD, "zelf": ctx["zelf"], "bridge": dict(bridge_stand(), nummer=eigen_nummer()),
                "klanten": klanten, "wacht_n": wacht_n(con, L, ctx, lijst),
                "chats": lijst, "kladden": kop, "kv": _kv(kop)}
    finally:
        con.close()


def pols(na, chat, kv):
    con = db()
    try:
        L = laatste(con)
        uit = {"max": L["max"], "versturen": instelling(), "echt": echt_versturen(),
               "bridge": dict(bridge_stand(), nummer=eigen_nummer()), "chats": [], "berichten": []}
        r = routing()
        uit["wacht_n"] = wacht_n(con, L)   # onthouden: een lege peiling vraagt niets aan de database
        if na < 0 or L["max"] - na > 3000:
            uit["herlaad"] = True
            return uit
        kop = koppeling(r)
        nkv = _kv(kop)
        if L["max"] > na:
            rijen = con.execute("select chat_jid, rowid, media_type, filename from messages where rowid > ? order by rowid",
                                (na,)).fetchall()
            ctx = _ctx(con, r)
            veranderd = sorted({j for j, _, _, _ in rijen})   # ook een reactie: een eigen emoji is een antwoord
            uit["chats"] = chat_items(con, veranderd, ctx, kop)
            if chat and isinstance(chat, str) and JID.match(chat):
                nieuw = [rid for j, rid, soort, _ in rijen if j == chat and soort != "reaction"]
                doelen = [doel for j, _, soort, doel in rijen if j == chat and soort == "reaction" and doel]
                lijst = []
                if nieuw:
                    lijst += con.execute(f"select {KOL} from messages where rowid in ({','.join('?' * len(nieuw))})",
                                         nieuw).fetchall()
                if doelen:
                    lijst += con.execute(f"select {KOL} from messages where chat_jid = ? and id in "
                                         f"({','.join('?' * len(doelen))})", [chat, *doelen]).fetchall()
                gezien, rijen2 = set(), []
                for rij in sorted(lijst, key=lambda x: (x[12] or 0, x[0])):
                    if rij[0] not in gezien and rij[7] != "reaction":
                        gezien.add(rij[0])
                        rijen2.append(rij)
                uit["berichten"] = _berichten_uit(con, chat, rijen2, ctx)
        if nkv != kv:
            uit["kv"] = nkv
            uit["kladden"] = kop
            tel = {}
            for jid, items in kop.items():
                tel[jid] = [sum(1 for x in items if x["soort"] == "klad"), sum(1 for x in items if x["soort"] == "vraag")]
            uit["tellers"] = tel
        return uit
    finally:
        con.close()


# --- media -------------------------------------------------------------------------------------------------------------
def _binnen_store(pad):
    echt = os.path.realpath(pad)
    basis = os.path.realpath(STORE)
    if not (echt == basis or echt.startswith(basis + os.sep)):
        raise Fout(403, "dat bestand ligt buiten de store")
    return echt


def media_pad(jid, mid, ophalen=True):
    """(pad op schijf, soort, oorspronkelijke naam). Staat het niet op schijf, dan haalt de bridge het op."""
    if not isinstance(mid, str) or not BERICHT_ID.match(mid):
        raise Fout(400, "onbekend bericht")
    con = db()
    try:
        _chat_bestaat(con, jid)
        rij = con.execute("select media_type, filename, is_from_me, timestamp from messages where chat_jid = ? and id = ?",
                          (jid, mid)).fetchone()
    finally:
        con.close()
    if not rij or rij[0] not in MEDIA_WOORD:
        raise Fout(404, "geen media bij dat bericht")
    soort, bestand, van_mij, ts = rij
    naam = lokale_media(jid).get(mid)
    if naam:
        return _binnen_store(os.path.join(_chatmap(jid), naam)), soort, bestand
    if van_mij:   # zelf verstuurd: de bridge bewaarde alleen de naam, het bestand zelf staat bij ons of in de outbox
        eigen = _eigen_media(jid, bestand, ts_s(ts))
        if eigen:
            return eigen, soort, bestand
    if not ophalen:
        raise Fout(404, "nog niet op schijf")
    if AFGESCHERMD:
        raise Fout(404, "niet op schijf (proef-store: niet ophalen)")
    try:
        code, raw = _bridge("/download", {"message_id": mid, "chat_jid": jid}, timeout=90)
        d = json.loads(raw or b"{}")
    except urllib.error.HTTPError as fout:
        try:
            d = json.loads(fout.read() or b"{}")
        except ValueError:
            d = {}
        raise Fout(502, "WhatsApp gaf het bestand niet (meestal te oud): " + str(d.get("message") or fout.code)[:200])
    except (OSError, ValueError) as fout:
        raise Fout(502, f"de bridge is niet bereikbaar ({type(fout).__name__})")
    if not d.get("success") or not d.get("path"):
        raise Fout(502, "WhatsApp gaf het bestand niet: " + str(d.get("message") or "?")[:200])
    pad = _binnen_store(str(d["path"]))
    if not os.path.isfile(pad):
        raise Fout(502, "de bridge meldt een bestand dat er niet is")
    return pad, soort, bestand


def _inhoud_soort(pad, soort, bestand):
    ext = os.path.splitext(bestand if soort == "document" and bestand else pad)[1].lower()
    if soort == "sticker":
        return "image/webp"
    return SOORTEN.get(ext, "application/octet-stream")


def _veilige_naam(naam):
    naam = os.path.basename(str(naam or "bestand").replace("\\", "/")) or "bestand"
    ascii_ = re.sub(r"[^A-Za-z0-9._ -]", "_", naam).strip(" .") or "bestand"
    return ascii_[:120], quote(naam[:200], safe="")


def naar_m4a(bron, jid, mid):
    doel = os.path.join(DATA, "wa", "m4a", hashlib.sha1(f"{jid}|{mid}".encode()).hexdigest() + ".m4a")
    if os.path.isfile(doel):
        return doel
    with STAAT["slot_m4a"]:
        if os.path.isfile(doel):
            return doel
        tmp = doel + ".tmp.m4a"
        try:
            r = subprocess.run([FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-i", bron, "-vn", "-c:a", "aac",
                                "-b:a", "64k", "-movflags", "+faststart", tmp], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise Fout(500, "omzetten van het spraakbericht lukte niet")
        if r.returncode or not os.path.isfile(tmp):
            raise Fout(500, "omzetten van het spraakbericht lukte niet")
        os.replace(tmp, doel)
    return doel


def stuur_media(h, q):
    jid, mid = (q.get("chat") or [""])[0], (q.get("id") or [""])[0]
    pad, soort, bestand = media_pad(jid, mid)
    ctype = _inhoud_soort(pad, soort, bestand)
    if (q.get("vorm") or [""])[0] == "m4a" and soort == "audio":
        pad, ctype = naar_m4a(pad, jid, mid), "audio/mp4"
    grootte = os.path.getsize(pad)
    inline = ctype in INLINE
    if not inline:
        ctype = "application/octet-stream"
    weergave = bestand if soort == "document" and bestand else os.path.basename(pad)
    ascii_, utf = _veilige_naam(weergave)
    start_, eind = 0, grootte - 1
    code = 200
    bereik = h.headers.get("Range")
    if bereik:
        m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", bereik)
        if not m or (not m.group(1) and not m.group(2)):
            return _tekst(h, 416, "ongeldig bereik", {"Content-Range": f"bytes */{grootte}"})
        if m.group(1):
            start_ = int(m.group(1))
            eind = min(int(m.group(2)), grootte - 1) if m.group(2) else grootte - 1
        else:
            start_ = max(0, grootte - int(m.group(2)))
        if start_ > eind or start_ >= grootte:
            return _tekst(h, 416, "ongeldig bereik", {"Content-Range": f"bytes */{grootte}"})
        code = 206
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(eind - start_ + 1))
    h.send_header("Accept-Ranges", "bytes")
    if code == 206:
        h.send_header("Content-Range", f"bytes {start_}-{eind}/{grootte}")
    h.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename=\"{ascii_}\"; "
                                         f"filename*=UTF-8''{utf}")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    h.send_header("Cache-Control", "private, max-age=3600")
    if not inline:
        h.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
    h.end_headers()
    if h.command == "HEAD":
        return
    with open(pad, "rb") as fh:
        fh.seek(start_)
        over = eind - start_ + 1
        while over > 0:
            stuk = fh.read(min(65536, over))
            if not stuk:
                break
            h.wfile.write(stuk)
            over -= len(stuk)


def uitschrijven(jid, mid):
    sleutel = f"{jid}|{mid}"
    al = _spraak().get(sleutel)
    if al:
        return {"tekst": al}
    pad, soort, _ = media_pad(jid, mid)
    if soort != "audio":
        raise Fout(400, "dat is geen spraakbericht")
    if not STAAT["slot_spraak"].acquire(timeout=200):
        raise Fout(503, "er wordt al iets uitgeschreven; probeer zo opnieuw")
    try:
        al = _spraak().get(sleutel)
        if al:
            return {"tekst": al}
        try:
            r = subprocess.run([WHISPER_PY, "-I", SPRAAK, pad], capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise Fout(504, "uitschrijven duurde te lang")
        except OSError as fout:
            raise Fout(500, f"uitschrijven kan niet starten ({type(fout).__name__})")
        if r.returncode:
            raise Fout(500, "uitschrijven mislukt: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200])
        regels = r.stdout.strip().splitlines()
        tekst = " ".join(x.strip() for x in regels[1:] if x.strip()) or "(geen woorden gehoord)"
        alles = dict(_spraak())
        alles[sleutel] = tekst
        _schrijf_json(os.path.join(DATA, "wa", "spraak.json"), alles)
        return {"tekst": tekst}
    finally:
        STAAT["slot_spraak"].release()


# --- bijlagen: naam, opladen, verwerken ----------------------------------------------------------------------------
def _bijlagen_map():
    return os.path.join(DATA, "wa", "bijlagen")


def _mb(n):
    return f"{n // 1048576} MB" if n >= 1048576 else f"{n} bytes"


def _bijlage_naam(naam):
    """De naam die het scherm opgeeft is onbetrouwbaar: alleen de basisnaam, zonder / \\ stuurtekens of punt vooraan,
    hoogstens 120 tekens (en 240 bytes op schijf). De extensie blijft, in kleine letters: de bridge kiest daarop."""
    naam = unicodedata.normalize("NFC", str(naam or "")).replace("\\", "/").split("/")[-1]
    naam = "".join(c for c in naam if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Zl", "Zp"))
    naam = naam.strip().lstrip(". ").rstrip(" ")
    stam, ext = os.path.splitext(naam)
    ext = ext.lower() if 1 < len(ext) <= 10 else ""
    if not ext:
        stam = naam
    stam = stam.rstrip(" .")
    while stam and (len(stam) + len(ext) > 120 or len((stam + ext).encode()) > 240):
        stam = stam[:-1].rstrip(" .")
    return (stam + ext) if stam else "bestand" + ext


def _weg_map(pad):
    try:
        if os.path.islink(pad) or not os.path.isdir(pad):
            os.remove(pad)
        else:
            shutil.rmtree(pad, ignore_errors=True)
    except OSError:
        pass


def _ruim_bijlagen_op():
    """Wachtende bijlagen ouder dan BIJLAGE_LEVEN weg (op de wijzigingstijd van de map: `staat` overleeft geen herstart)."""
    wortel = _bijlagen_map()
    nu = time.time()
    try:
        namen = os.listdir(wortel)
    except OSError:
        namen = []
    for n in namen:
        p = os.path.join(wortel, n)
        try:
            if nu - os.lstat(p).st_mtime > BIJLAGE_LEVEN:
                _weg_map(p)
        except OSError:
            pass
    with STAAT["slot"]:
        for bid in [k for k, b in STAAT["bijlagen"].items() if not os.path.isfile(b["pad"])]:
            del STAAT["bijlagen"][bid]


def _wachtend_totaal():
    totaal = 0
    for map_, _, bestanden in os.walk(_bijlagen_map()):
        for b in bestanden:
            try:
                totaal += os.lstat(os.path.join(map_, b)).st_size
            except OSError:
                pass
    return totaal


def _bijlagen_weg(ids):
    """Wachtende bijlagen opruimen (na een verzending, of als het scherm er een weghaalt)."""
    wortel = os.path.realpath(_bijlagen_map())
    for bid in ids:
        with STAAT["slot"]:
            b = STAAT["bijlagen"].pop(bid, None)
        if b:
            map_ = os.path.realpath(os.path.dirname(b["pad"]))
            if map_.startswith(wortel + os.sep):
                _weg_map(map_)


def bijlage_weg(body):
    bid = body.get("id")
    if not isinstance(bid, str) or not BIJLAGE_ID.match(bid):
        raise Fout(400, "onbekende bijlage")
    _bijlagen_weg([bid])
    return {"ok": True}


def _sha1(pad):
    h = hashlib.sha1()
    with open(pad, "rb") as fh:
        for stuk in iter(lambda: fh.read(1 << 20), b""):
            h.update(stuk)
    return h.hexdigest()


def bijlage_op(h, q):
    """POST /api/wa/bijlage?naam=…: ruwe inhoud in de body, in stukken naar schijf, en meteen verwerkt, zodat een fout
    zich toont vóór het versturen. De poortwachter (_poort) ging al voorbij. Met `spraak=1` is de body een opname uit de
    browser die een spraakbericht wordt (Ogg Opus); zonder blijft een geluidsbestand een document."""
    h.close_connection = True   # een body die we niet (helemaal) lezen mag de volgende aanvraag niet vergiftigen
    naam = _bijlage_naam((q.get("naam") or [""])[0])
    spraak = (q.get("spraak") or [""])[0] == "1"
    if h.headers.get("Transfer-Encoding"):
        raise Fout(411, "Opladen vraagt een vaste lengte (Content-Length).")
    try:
        lengte = int(h.headers.get("Content-Length"))
    except (TypeError, ValueError):
        raise Fout(411, "Opladen vraagt een lengte (Content-Length).")
    if lengte < 0:
        raise Fout(400, "ongeldige lengte")
    if lengte == 0:
        raise Fout(400, f"«{naam}» is leeg: er is niets om mee te sturen.")
    if lengte > MAX_BIJLAGE:
        raise Fout(413, f"«{naam}» is te groot: hoogstens {_mb(MAX_BIJLAGE)} per bestand.")
    wortel = _bijlagen_map()
    os.makedirs(wortel, mode=0o700, exist_ok=True)
    _ruim_bijlagen_op()
    if _wachtend_totaal() + lengte > MAX_WACHTEND:
        raise Fout(413, f"Er wachten al te veel bijlagen (samen hoogstens {_mb(MAX_WACHTEND)}). Verstuur ze of haal er "
                        "eerst een paar weg.")
    bid = secrets.token_hex(8)
    map_ = os.path.join(wortel, bid)
    os.mkdir(map_, 0o700)
    bron = os.path.join(map_, ".bron")
    try:
        fd = os.open(bron, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            over = lengte
            while over > 0:
                stuk = h.rfile.read(min(1 << 16, over))
                if not stuk:
                    raise Fout(400, f"Het opladen van «{naam}» brak af: probeer opnieuw.")
                fh.write(stuk)
                over -= len(stuk)
        with STAAT["slot_verwerk"]:
            uit = _verwerk(bron, naam, map_, spraak)
        for n in os.listdir(map_):   # tussenbestanden (bron, sips) weg; alleen het resultaat blijft
            if os.path.join(map_, n) != uit["pad"]:
                os.remove(os.path.join(map_, n))
        uit.update(grootte=os.path.getsize(uit["pad"]), sha1=_sha1(uit["pad"]), tijd=time.time())
    except BaseException:
        _weg_map(map_)
        raise
    with STAAT["slot"]:
        STAAT["bijlagen"][bid] = uit
    antw = {"id": bid, "naam": uit["naam"], "soort": uit["soort"], "grootte": uit["grootte"]}
    for k in ("breedte", "hoogte", "duur"):
        if uit.get(k):
            antw[k] = uit[k]
    return antw


def _soort_bridge(naam):
    """Zoals de bridge kiest (classifyMediaPath): op de extensie."""
    ext = os.path.splitext(naam)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        return "image"
    if ext in (".mp4", ".avi", ".mov"):
        return "video"
    if ext == ".ogg":
        return "audio"
    return "document"


def _verwerk(bron, naam, map_, spraak=False):
    ext = os.path.splitext(naam)[1]
    stam = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    if spraak:   # de naam en het type van de browser tellen niet: ffprobe zegt wat het is
        return _verwerk_spraak(bron, naam, map_, "spraak-" + stam)
    if ext in FOTO_EXT:
        return _verwerk_foto(bron, naam, ext, map_, "foto-" + stam)
    if ext in VIDEO_EXT:
        return _verwerk_video(bron, naam, map_, "video-" + stam)
    doel = os.path.join(map_, naam)   # document (of gif, of ogg): ongewijzigd, onder zijn eigen naam
    os.replace(bron, doel)
    return {"pad": doel, "naam": naam, "soort": _soort_bridge(naam), "origineel": naam}


# EXIF-oriëntatie zelf lezen (tag 0x0112), zodat het resultaat niet afhangt van wat een ffmpeg-versie zelf doet.
def _tiff_orientatie(t):
    try:
        if t[:4] == b"II*\0":
            e = "<"
        elif t[:4] == b"MM\0*":
            e = ">"
        else:
            return 1
        off = struct.unpack(e + "I", t[4:8])[0]
        n = struct.unpack(e + "H", t[off:off + 2])[0]
        for i in range(min(n, 500)):
            p = off + 2 + 12 * i
            tag, typ, cnt = struct.unpack(e + "HHI", t[p:p + 8])
            if tag == 0x0112:
                v = struct.unpack(e + "H", t[p + 8:p + 10])[0] if typ == 3 else struct.unpack(e + "I", t[p + 8:p + 12])[0]
                return v if 1 <= v <= 8 else 1
    except (struct.error, IndexError):
        pass
    return 1


def _orientatie(pad):
    """1–8 uit de EXIF van een jpg, tiff, png (eXIf) of webp (EXIF); 1 als er niets (leesbaars) is."""
    try:
        with open(pad, "rb") as fh:
            kop = fh.read(12)
            if kop[:2] == b"\xff\xd8":   # jpg: segmenten tot SOS
                fh.seek(2)
                while True:
                    m = fh.read(2)
                    if len(m) < 2 or m[0] != 0xFF:
                        return 1
                    while m[1] == 0xFF:
                        m = m[1:] + fh.read(1)
                    if m[1] in (0xD8, 0x01) or 0xD0 <= m[1] <= 0xD7:
                        continue
                    if m[1] in (0xDA, 0xD9):
                        return 1
                    lengte = struct.unpack(">H", fh.read(2))[0]
                    data = fh.read(lengte - 2)
                    if m[1] == 0xE1 and data[:6] == b"Exif\0\0":
                        return _tiff_orientatie(data[6:])
            if kop[:4] in (b"II*\0", b"MM\0*"):
                fh.seek(0)
                return _tiff_orientatie(fh.read(4 << 20))
            if kop[:8] == b"\x89PNG\r\n\x1a\n":
                fh.seek(8)
                while True:
                    k = fh.read(8)
                    if len(k) < 8:
                        return 1
                    lengte, soort = struct.unpack(">I4s", k)
                    if soort == b"eXIf":
                        return _tiff_orientatie(fh.read(min(lengte, 1 << 20)))
                    if soort in (b"IDAT", b"IEND"):
                        return 1
                    fh.seek(lengte + 4, 1)
            if kop[:4] == b"RIFF" and kop[8:12] == b"WEBP":
                while True:
                    k = fh.read(8)
                    if len(k) < 8:
                        return 1
                    soort, lengte = struct.unpack("<4sI", k)
                    if soort == b"EXIF":
                        d = fh.read(min(lengte, 1 << 20))
                        return _tiff_orientatie(d[6:] if d[:6] == b"Exif\0\0" else d)
                    fh.seek(lengte + (lengte & 1), 1)
    except (OSError, struct.error, IndexError):
        pass
    return 1


# per oriëntatie: wat er moet gebeuren om rechtop te staan (EXIF: 2 spiegelen, 3 halve slag, 6 kwartslag rechtsom, …)
DRAAI = {1: [], 2: ["hflip"], 3: ["hflip", "vflip"], 4: ["vflip"], 5: ["transpose=0"], 6: ["transpose=1"],
         7: ["transpose=3"], 8: ["transpose=2"]}


def _probe(pad, velden="stream=index,codec_type,codec_name,width,height,pix_fmt:stream_side_data=rotation"):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", velden, "-of", "json", pad],
                           capture_output=True, timeout=60)
        if r.returncode:
            return []
        return json.loads(r.stdout or b"{}").get("streams") or []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def _draaiing(stroom):
    for sd in stroom.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    return 0


def _ffmpeg(args, timeout):
    try:
        r = subprocess.run([FFMPEG, "-nostdin", "-y", "-loglevel", "error", *args], capture_output=True,
                           timeout=max(1, timeout))
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        raise
    except OSError:
        return False


def _sips_jpg(bron, doel):
    try:
        r = subprocess.run([SIPS, "-s", "format", "jpeg", "-s", "formatOptions", "best", bron, "--out", doel],
                           capture_output=True, timeout=120)
        return r.returncode == 0 and os.path.isfile(doel) and os.path.getsize(doel) > 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _kaal_jpg(pad):
    """Alleen JFIF (APP0) en een kleurprofiel (APP2 ICC_PROFILE) blijven; EXIF, XMP, commentaar en de rest gaan eruit,
    wat de encoder er ook in zette. Lukt het lezen niet, dan een fout: nooit een bestand met metadata versturen."""
    with open(pad, "rb") as fh:
        d = fh.read()
    if d[:2] != b"\xff\xd8":
        raise ValueError("geen jpg")
    uit, i = [b"\xff\xd8"], 2
    while True:
        if i + 4 > len(d) or d[i] != 0xFF:
            raise ValueError("onleesbaar segment")
        m = d[i + 1]
        if m == 0xDA:
            uit.append(d[i:])
            break
        lengte = struct.unpack(">H", d[i + 2:i + 4])[0]
        seg = d[i:i + 2 + lengte]
        houd = not (0xE0 <= m <= 0xEF or m == 0xFE) or (m == 0xE0 and seg[4:9] == b"JFIF\0") or \
            (m == 0xE2 and seg[4:16] == b"ICC_PROFILE\0")
        if houd:
            uit.append(seg)
        i += 2 + lengte
    with open(pad, "wb") as fh:
        fh.write(b"".join(uit))


def _kale_png(pad):
    """Tekst-, tijd- en EXIF-blokken uit een png."""
    with open(pad, "rb") as fh:
        d = fh.read()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("geen png")
    uit, i = [d[:8]], 8
    while i < len(d):
        lengte, soort = struct.unpack(">I4s", d[i:i + 8])
        blok = d[i:i + 12 + lengte]
        if len(blok) < 12 + lengte:
            raise ValueError("afgebroken png")
        if soort not in (b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"):
            uit.append(blok)
        i += 12 + lengte
        if soort == b"IEND":
            break
    with open(pad, "wb") as fh:
        fh.write(b"".join(uit))


def _verwerk_foto(bron, naam, ext, map_, stam):
    kapot = Fout(422, f"«{naam}» is geen leesbare foto: niet meegestuurd. Stuur hem eventueel als bestand in een zip.")
    werk = bron
    if ext in (".heic", ".heif"):
        werk = os.path.join(map_, ".sips.jpg")
        if not _sips_jpg(bron, werk):
            raise kapot
        orient = _orientatie(werk)
        if orient == 1:   # staat de draaiing alleen in de heic zelf (irot), dan via ffprobe
            st = (_probe(bron) or [{}])[0]
            orient = {270: 6, 90: 8, 180: 3}.get(_draaiing(st), 1)
    else:
        orient = _orientatie(bron)
    st = next((x for x in _probe(werk) if x.get("codec_type") == "video"), None)
    if not st and ext in (".webp", ".tif", ".tiff", ".bmp"):   # wat ffmpeg niet leest, leest sips soms wel
        werk = os.path.join(map_, ".sips.jpg")
        if _sips_jpg(bron, werk):
            orient = _orientatie(werk)
            st = next((x for x in _probe(werk) if x.get("codec_type") == "video"), None)
    if not st or not st.get("width") or not st.get("height"):
        raise kapot
    w, h = int(st["width"]), int(st["height"])
    if orient >= 5:
        w, h = h, w
    f = min(1.0, MAX_ZIJDE_FOTO / max(w, h))
    W, H = max(1, round(w * f)), max(1, round(h * f))
    png = ext == ".png" and werk == bron and os.path.getsize(bron) <= PNG_BLIJFT and max(w, h) <= MAX_ZIJDE_FOTO
    filters = list(DRAAI[orient]) + ([f"scale={W}:{H}:flags=lanczos"] if f < 1 else [])
    uit = os.path.join(map_, stam + (".png" if png else ".jpg"))
    alfa = bool(re.match(r"(rgba|bgra|argb|abgr|ya\d|yuva|gbrap|pal8)", str(st.get("pix_fmt") or "")))
    if png:
        args = ["-vf", ",".join(filters) or "null", "-c:v", "png"]
    elif alfa:   # doorzichtig wordt wit, niet zwart
        args = ["-filter_complex", f"[0:v]{','.join(filters + ['format=rgba'])}[v];color=c=white:s={W}x{H}[w];"
                                   "[w][v]overlay=shortest=1,format=yuvj420p[u]", "-map", "[u]",
                "-c:v", "mjpeg", "-q:v", "3"]
    else:
        args = ["-vf", ",".join(filters + ["format=yuvj420p"]), "-c:v", "mjpeg", "-q:v", "3"]
    try:
        gelukt = _ffmpeg(["-noautorotate", "-i", werk, *args, "-frames:v", "1", "-map_metadata", "-1", "-f", "image2",
                          uit], 120)
    except subprocess.TimeoutExpired:
        raise kapot
    if not gelukt or not os.path.isfile(uit) or not os.path.getsize(uit):
        raise kapot
    try:
        (_kale_png if png else _kaal_jpg)(uit)
    except (ValueError, struct.error, OSError):
        raise kapot
    nu = next((x for x in _probe(uit) if x.get("codec_type") == "video"), None)
    if not nu or _orientatie(uit) != 1:
        raise kapot
    return {"pad": uit, "naam": os.path.basename(uit), "soort": "image", "origineel": naam,
            "breedte": int(nu["width"]), "hoogte": int(nu["height"])}


def _verwerk_video(bron, naam, map_, stam):
    stromen = _probe(bron)
    v = next((x for x in stromen if x.get("codec_type") == "video"), None)
    a = next((x for x in stromen if x.get("codec_type") == "audio"), None)
    if not v:
        raise Fout(422, f"«{naam}» is geen leesbare video: niet meegestuurd.")
    uit = os.path.join(map_, stam + ".mp4")
    eind = time.monotonic() + VIDEO_TIJD
    kaal = ["-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", "-f", "mp4", uit]
    klaar = False
    try:
        if v.get("codec_name") == "h264" and (a is None or a.get("codec_name") == "aac"):
            # al H.264: alleen opnieuw inpakken (de draaiing reist mee als matrix, de metadata niet)
            klaar = _ffmpeg(["-i", bron, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", *kaal], eind - time.monotonic()) \
                and os.path.getsize(uit) <= MAX_VIDEO_UIT
        if not klaar:
            # omzetten: ffmpeg draait de beelden zelf rechtop (autorotate) en schrijft dan geen matrix meer
            schaal = (f"scale=w='trunc(min(1\\,{MAX_ZIJDE_VIDEO}/max(iw\\,ih))*iw/2)*2'"
                      f":h='trunc(min(1\\,{MAX_ZIJDE_VIDEO}/max(iw\\,ih))*ih/2)*2',format=yuv420p")
            geluid = ["-c:a", "aac", "-b:a", "128k"] if a else ["-an"]
            for enc in (["-c:v", "h264_videotoolbox", "-b:v", "4M"], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]):
                if _ffmpeg(["-i", bron, "-map", "0:v:0", "-map", "0:a:0?", "-vf", schaal, *enc, *geluid, *kaal],
                           eind - time.monotonic()):
                    klaar = True
                    break
    except subprocess.TimeoutExpired:
        raise Fout(422, f"Het omzetten van «{naam}» duurde langer dan {VIDEO_TIJD // 60} minuten: niet meegestuurd.")
    if not klaar or not os.path.isfile(uit):
        raise Fout(422, f"«{naam}» kon niet omgezet worden naar mp4: niet meegestuurd.")
    if os.path.getsize(uit) > MAX_VIDEO_UIT:
        raise Fout(413, f"«{naam}» is na het omzetten nog groter dan {_mb(MAX_VIDEO_UIT)}: te groot om zo te sturen. "
                        "Kort hem in, of deel hem als link.")
    nv = next((x for x in _probe(uit) if x.get("codec_type") == "video"), None)
    if not nv or nv.get("codec_name") != "h264":
        raise Fout(422, f"«{naam}» kon niet omgezet worden naar mp4: niet meegestuurd.")
    w, h = int(nv.get("width") or 0), int(nv.get("height") or 0)
    if _draaiing(nv) in (90, 270):
        w, h = h, w
    return {"pad": uit, "naam": os.path.basename(uit), "soort": "video", "origineel": naam, "breedte": w, "hoogte": h}


# Spraakbericht: de bridge leest de duur zelf uit het bestand, als (laatste granule − pre-skip) / de "input sample rate"
# uit OpusHead. Opus telt granules altijd in 48 kHz, dus alleen met -ar 48000 ziet de ontvanger de juiste duur.
def _ogg_opus(pad):
    """(kanalen, input sample rate, pre-skip, laatste granule) van een Ogg Opus-bestand, zoals de bridge het leest.
    OpusHead moet in de eerste pagina staan. Geeft None als het geen leesbaar Ogg Opus is."""
    try:
        with open(pad, "rb") as fh:
            d = fh.read()
    except OSError:
        return None
    i, kop, granule = 0, None, -1
    while i + 27 <= len(d):
        if d[i:i + 4] != b"OggS":
            return None
        g = struct.unpack("<q", d[i + 6:i + 14])[0]
        n = d[i + 26]
        if i + 27 + n > len(d):
            return None
        lengte = sum(d[i + 27:i + 27 + n])
        body = d[i + 27 + n:i + 27 + n + lengte]
        if kop is None:   # de eerste pagina
            if body[:8] != b"OpusHead" or len(body) < 19:
                return None
            kop = (body[9], struct.unpack("<I", body[12:16])[0], struct.unpack("<H", body[10:12])[0])
        if g != -1:
            granule = g
        i += 27 + n + lengte
    if kop is None or i != len(d) or granule < 0:
        return None
    return kop + (granule,)


def _verwerk_spraak(bron, naam, map_, stam):
    """Een opname uit de browser (webm/opus, mp4/aac van Safari, of wat ook) → Ogg Opus mono 48 kHz zonder metadata.
    Wat geen geluidsopname is, te kort of te lang is, of niet om te zetten is: geweigerd, nooit het origineel."""
    stromen = _probe(bron, "stream=index,codec_type,codec_name")
    if not any(x.get("codec_type") == "audio" for x in stromen) or any(x.get("codec_type") == "video" for x in stromen):
        raise Fout(422, "Dat is geen geluidsopname: niet meegestuurd.")
    uit = os.path.join(map_, stam + ".ogg")
    try:
        # -t begrenst het werk: wat langer is dan MAX_SPRAAK wordt hieronder toch geweigerd
        gelukt = _ffmpeg(["-i", bron, "-map", "0:a:0", "-vn", "-t", str(MAX_SPRAAK + 1), "-c:a", "libopus", "-b:a", "32k",
                          "-ar", "48000", "-ac", "1", "-application", "voip", "-vbr", "on", "-map_metadata", "-1",
                          "-f", "ogg", uit], SPRAAK_TIJD)
    except subprocess.TimeoutExpired:
        raise Fout(422, "Het omzetten van de opname duurde te lang: niet meegestuurd.")
    if not gelukt or not os.path.isfile(uit):
        raise Fout(422, "De opname kon niet omgezet worden naar een spraakbericht: niet meegestuurd.")
    # zelf nakijken wat de bridge en WhatsApp zullen lezen
    o = _ogg_opus(uit)
    stroom = _probe(uit, "stream=codec_type,codec_name")
    if not o or o[0] != 1 or o[1] != 48000 or [x.get("codec_name") for x in stroom] != ["opus"]:
        raise Fout(422, "De opname kon niet omgezet worden naar een spraakbericht: niet meegestuurd.")
    duur = (o[3] - o[2]) / o[1]
    if duur < MIN_SPRAAK:
        raise Fout(422, "De opname is te kort: niet meegestuurd.")
    if duur > MAX_SPRAAK:
        raise Fout(413, f"De opname is te lang: hoogstens {MAX_SPRAAK // 60} minuten.")
    return {"pad": uit, "naam": os.path.basename(uit), "soort": "audio", "origineel": naam, "duur": round(duur, 1)}


# --- zelf verstuurde media terugvinden ----------------------------------------------------------------------------
def _verstuurd_pad():
    return os.path.join(DATA, "wa", "verstuurd.json")


def _verstuurd():
    pad = _verstuurd_pad()
    c = STAAT.get("verstuurd")
    try:
        mt = os.stat(pad).st_mtime_ns
    except OSError:
        return []
    if not c or c[0] != mt:
        try:
            with open(pad, encoding="utf-8") as fh:
                lijst = json.load(fh)
            c = (mt, lijst if isinstance(lijst, list) else [])
        except (OSError, ValueError):
            c = (mt, [])
        STAAT["verstuurd"] = c
    return c[1]


def _verstuurd_bij(regel):
    with STAAT["slot_verstuurd"]:
        lijst = [r for r in _verstuurd() if isinstance(r, dict)] + [regel]
        _schrijf_json(_verstuurd_pad(), lijst[-MAX_VERSTUURD:])


def _in_outbox(pad):
    """Het echte pad als het binnen de outbox ligt, anders None."""
    try:
        basis = os.path.realpath(OUTBOX)
        echt = os.path.realpath(pad)
    except (TypeError, ValueError, OSError):
        return None
    return echt if echt.startswith(basis + os.sep) else None


def _eigen_media(jid, bestand, ts):
    """Het bestand van een eigen bericht met media dat niet in de map van de chat staat (de bridge bewaarde alleen de
    naam): eerst wat de tab zelf verstuurde (verstuurd.json, zelfde chat en naam, binnen 10 minuten), dan de wortel van
    de outbox (wat de chats via de MCP stuurden). Een proef-store kijkt nooit in de echte outbox."""
    if OUTBOX_LEZEN_DICHT or not bestand or not isinstance(bestand, str):
        return None
    best = None
    for r in _verstuurd():
        if isinstance(r, dict) and r.get("chat") == jid and r.get("naam") == bestand:
            try:
                d = abs(float(r.get("tijd") or 0) - ts)
            except (TypeError, ValueError):
                continue
            if d <= 600 and (best is None or d < best[0]):
                best = (d, r.get("pad"))
    if best and isinstance(best[1], str):
        p = _in_outbox(best[1])
        if p and os.path.isfile(p):
            return p
    if "/" in bestand or "\\" in bestand or "\0" in bestand or bestand.startswith(".") or len(bestand) > 255:
        return None
    p = _in_outbox(os.path.join(OUTBOX, bestand))
    return p if p and os.path.isfile(p) and os.path.dirname(p) == os.path.realpath(OUTBOX) else None


# --- versturen ---------------------------------------------------------------------------------------------------------
def _verloop(chat, route, kop):
    """Een regel in het Verloop van de app. Nooit de inhoud van een bericht."""
    try:
        if hasattr(APP, "gebeurd"):
            APP.gebeurd("stand", chat, route, kop)
        elif hasattr(APP, "_gebeurd"):
            with getattr(APP, "slot", None) or nullcontext():
                APP._gebeurd("stand", chat, route, kop, "")
    except Exception as fout:
        print(f"WhatsApp-tab: verloop mislukt ({type(fout).__name__})", flush=True)


def _verloop_naam(jid, naam, ctx):
    """In het Verloop staat alleen de naam van een gesprek dat bij het werk hoort; een privégesprek blijft naamloos."""
    if jid == ctx["zelf"] or jid in ctx["kv"] or jid in ctx["r"]["bezet"]:
        return naam
    return "privégesprek"


def _open_vraag(nr):
    """De open vraag met dit nummer uit de app, of None als ze al beantwoord of vervallen is."""
    try:
        return next((v for v in APP.stand().get("vragen") or []
                     if v.get("nr") == nr and v.get("status", "open") == "open"), None)
    except Exception:
        return None


def _bericht_rij(con, jid, mid):
    if not isinstance(mid, str) or not BERICHT_ID.match(mid):
        raise Fout(400, "onbekend bericht")
    rij = con.execute(f"select {KOL} from messages where chat_jid = ? and id = ?", (jid, mid)).fetchone()
    if not rij:
        raise Fout(404, "dat bericht staat niet (meer) in de store")
    return rij


def _rem(jid, tekst):
    sleutel = hashlib.sha1(f"{jid}\n{tekst}".encode()).hexdigest()
    with STAAT["slot"]:
        nu = time.time()
        for k in [k for k, t in STAAT["rem"].items() if nu - t > REM]:
            del STAAT["rem"][k]
        if sleutel in STAAT["rem"]:
            raise Fout(409, "Dit bericht ging net al weg naar dit gesprek. Wacht even en kijk of het er staat.")
        STAAT["rem"][sleutel] = nu
    return sleutel


def stuur(body):
    c = _stuur_controles(body)
    if not echt_versturen():
        return _stuur_droog(c)
    if c["bijlagen"]:
        return _stuur_bijlagen(c)
    return _stuur_tekst(c)


def _stuur_controles(body):
    """Alles wat moet kloppen vóór er iets gebeurt, in de volgorde van altijd: chat bestaat, klad nog open en van dit
    gesprek, nummer, dubbel-rem. Geeft wat de verzending nodig heeft."""
    jid = body.get("chat")
    tekst = str(body.get("tekst") or "").strip()
    ids = body.get("bijlagen") or []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise Fout(400, "bijlagen is een lijst van id's")
    if len(ids) > MAX_BIJLAGEN:
        raise Fout(400, f"hoogstens {MAX_BIJLAGEN} bijlagen per bericht")
    if len(set(ids)) != len(ids):
        raise Fout(400, "dezelfde bijlage staat er twee keer in")
    if not tekst and not ids:
        raise Fout(400, "het bericht is leeg")
    if len(tekst) > MAX_TEKST:
        raise Fout(400, f"het bericht is te lang (hoogstens {MAX_TEKST} tekens)")
    if ids and body.get("antwoord_op"):
        raise Fout(400, "antwoord-op kan niet samen met een bijlage")
    bijlagen = []
    with STAAT["slot"]:
        for i in ids:
            b = STAAT["bijlagen"].get(i)
            if not b or not os.path.isfile(b["pad"]):
                raise Fout(400, "bijlage niet gevonden: kies ze opnieuw")
            bijlagen.append(dict(b, id=i))
    r = routing()
    con = db()
    try:
        _chat_bestaat(con, jid)
        ctx = _ctx(con, r)
        naam = "Jij (notities)" if jid == ctx["zelf"] else naam_voor(jid, ctx["nb"], ctx["cn"], ctx["rn"])
        verzoek = {"recipient": jid, "message": tekst}
        citaat = None
        if body.get("antwoord_op"):
            rij = _bericht_rij(con, jid, body["antwoord_op"])
            b = _berichten_uit(con, jid, [rij], ctx)[0]
            if b["verwijderd"] or not b["wie_jid"]:
                raise Fout(400, "Van wie dat bericht komt weet ik niet, dus antwoorden met citaat kan niet. Stuur het zonder citaat.")
            q = b["tekst"] or (MEDIA_WOORD.get(b["soort"], "") + (": " + b["bestand"] if b["bestand"] else ""))
            verzoek.update(quoted_message_id=b["id"], quoted_sender_jid=b["wie_jid"], quoted_content=q)
            citaat = {"id": b["id"], "wie": b["wie"], "tekst": _kort(q, 160), "soort": b["soort"]}
    finally:
        con.close()
    # Hoort dit bericht bij een klad, dan moet de vraag nog openstaan: werd ze intussen op het dashboard beantwoord,
    # dan verstuurt de chat zelf en zou een tik hier de klant hetzelfde bericht twee keer geven.
    vraag_nr, origineel = None, ""
    if body.get("vraag") not in (None, ""):
        try:
            vraag_nr = int(body["vraag"])
        except (ValueError, TypeError):
            raise Fout(400, "onbekende vraag")
        v = _open_vraag(vraag_nr)
        if v is None:
            raise Fout(409, "Die vraag is intussen al beantwoord: de chat handelt het zelf af. Niets verstuurd; kijk eerst "
                            "in het gesprek.")
        wa = v.get("whatsapp") if isinstance(v.get("whatsapp"), dict) else {}
        if wa.get("aan") and wa["aan"] != jid:
            raise Fout(400, "dat klad hoort bij een ander gesprek")
        origineel = str(wa.get("bericht") or "").strip()
    klant = ctx["kv"].get(jid) or "god"
    route = f"Jij → WhatsApp › {_verloop_naam(jid, naam, ctx)}"
    if wil_echt() and eigen_nummer() is not True:
        raise Fout(409, NUMMER_FOUT)
    # de dubbel-rem telt de inhoud van de bijlagen mee: twee verschillende foto's zonder tekst mogen, dezelfde twee keer niet
    rem = _rem(jid, tekst if not bijlagen else tekst + "\n\0bijlagen:" + ",".join(b["sha1"] for b in bijlagen))
    return {"jid": jid, "tekst": tekst, "verzoek": verzoek, "citaat": citaat, "vraag_nr": vraag_nr,
            "origineel": origineel, "klant": klant, "route": route, "rem": rem, "bijlagen": bijlagen}


def _bijlagen_woord(m):
    return f"{m} bijlage" + ("" if m == 1 else "n")


def _alleen_spraak(c):
    """Bestaat de verzending alleen uit spraakberichten (geen tekst)? Dan "(<n> s)" voor het Verloop, anders None."""
    if c["tekst"] or not c["bijlagen"] or any(b["soort"] != "audio" for b in c["bijlagen"]):
        return None
    m = len(c["bijlagen"])
    s = round(sum(float(b.get("duur") or 0) for b in c["bijlagen"]))
    return f"{'spraakbericht' if m == 1 else f'{m} spraakberichten'}", f"({s} s)"


def _met_duur(regel, b):
    if b.get("duur"):
        regel["duur"] = b["duur"]
    return regel


def _stuur_droog(c):
    """Droogloop: geen bridge en niets in de outbox; het bericht blijft met stippelrand in het gesprek."""
    tekst, bijlagen = c["tekst"], c["bijlagen"]
    def nieuw(t):
        return {"id": f"droog-{time.time_ns()}-{len(berichten)}", "rowid": None, "ts": time.time(), "van_mij": True, "wie": "Jij",
                "wie_jid": None, "tekst": t, "soort": "tekst", "droog": True, "antwoord_op": c["citaat"],
                "reacties": [], "verwijderd": False, "beantwoordbaar": False}
    berichten = []
    if tekst and bijlagen and all(b["soort"] == "audio" for b in bijlagen):
        berichten.append(nieuw(tekst))   # zoals in het echt: een spraakbericht draagt geen onderschrift, de tekst gaat eerst
        bericht = nieuw("")
    else:
        bericht = nieuw(tekst)
    if bijlagen:
        bericht["bijlagen"] = [_met_duur({"naam": b["naam"], "soort": b["soort"], "grootte": b["grootte"]}, b) for b in bijlagen]
    berichten.append(bericht)
    with STAAT["slot"]:
        lijst = STAAT["droog"].setdefault(c["jid"], [])
        lijst.extend(berichten)
        del lijst[:-MAX_DROOG]
    _bijlagen_weg([b["id"] for b in bijlagen])
    sp = _alleen_spraak(c)
    if sp:
        _verloop(c["klant"], c["route"], f"droogloop: {sp[0]} niet verstuurd {sp[1]}")
    else:
        _verloop(c["klant"], c["route"], f"droogloop: niets verstuurd ({len(tekst)} tekens"
                                         + (f", {_bijlagen_woord(len(bijlagen))})" if bijlagen else ")"))
    return {"ok": True, "droog": True, "bericht": bericht, "berichten": berichten}


def _bridge_stuur_een(verzoek, timeout):
    """Eén /send aan de bridge. Geeft (uitkomst, reden, code): ok · geweigerd (vóór WhatsApp: niet verstuurd) ·
    onbereikbaar (niet verstuurd) · waarschijnlijk (de bridge meldt een fout bij het versturen zelf) · tijd · weggevallen
    · raar · onleesbaar · onzeker. Alles na "onbereikbaar" is onzeker: nooit opnieuw proberen."""
    try:
        code, raw = _bridge_verstuur("/send", verzoek, timeout=timeout)
        d = json.loads(raw or b"{}")
    except urllib.error.HTTPError as fout:
        try:
            d = json.loads(fout.read() or b"{}")
        except ValueError:
            d = None
        reden = str(d.get("message") or "") if isinstance(d, dict) else ""
        if fout.code in (400, 401, 403, 404, 405, 413) or reden.startswith(("Not connected", "Error parsing JID")):
            return "geweigerd", reden, fout.code
        if isinstance(d, dict) and d.get("success") is False:
            return "waarschijnlijk", reden, fout.code
        return "raar", reden, fout.code
    except (socket.timeout, TimeoutError):
        return "tijd", "", None
    except urllib.error.URLError as fout:
        if isinstance(getattr(fout, "reason", None), (socket.timeout, TimeoutError)):
            return "tijd", "", None
        return "onbereikbaar", "", None
    except ValueError:
        return "onleesbaar", "", None
    except (OSError, http.client.HTTPException):   # de bridge sloot de verbinding (bv. haar eigen WriteTimeout van 60 s)
        return "weggevallen", "", None
    if not isinstance(d, dict) or d.get("success") is not True:
        return "onzeker", "", None
    return "ok", "", 200


def _rem_los(rem):
    with STAAT["slot"]:   # geweigerd vóór er iets naar WhatsApp ging: opnieuw proberen mag meteen
        STAAT["rem"].pop(rem, None)


def _beantwoord_vraag(c, antw):
    if c["vraag_nr"] is None:
        return
    try:
        APP.antwoord(c["vraag_nr"], antw)
    except (ValueError, TypeError):
        pass   # de vraag ging net dicht: het bericht is wel weg


def _stuur_tekst(c):
    tekst = c["tekst"]
    uitkomst, reden, code = _bridge_stuur_een(c["verzoek"], 20)
    if uitkomst == "geweigerd":
        _rem_los(c["rem"])
        raise Fout(502, "Niet verstuurd: " + (f"de bridge meldt «{reden[:200]}»." if reden
                                              else f"de bridge weigerde de aanvraag ({code})."))
    if uitkomst == "waarschijnlijk":   # een fout bij het versturen zelf: zelden is het toch weg
        raise Fout(502, f"Waarschijnlijk niet verstuurd: de bridge meldt «{reden[:200] or code}». Kijk of het "
                        "bericht in het gesprek staat voor je opnieuw stuurt.")
    if uitkomst == "raar":
        raise Fout(504, "Onzeker of het verstuurd is (de bridge antwoordde vreemd). Kijk of het bericht in het gesprek "
                        "verschijnt voor je opnieuw stuurt.")
    if uitkomst == "tijd":
        raise Fout(504, "Onzeker of het verstuurd is (geen antwoord binnen 20 s). Kijk of het bericht in het gesprek "
                        "verschijnt voor je opnieuw stuurt.")
    if uitkomst == "onbereikbaar":
        _rem_los(c["rem"])
        raise Fout(502, "Niet verstuurd: de bridge is niet bereikbaar.")
    if uitkomst == "onleesbaar":
        raise Fout(504, "Onzeker of het verstuurd is (onleesbaar antwoord van de bridge). Kijk of het bericht in het "
                        "gesprek verschijnt voor je opnieuw stuurt.")
    if uitkomst == "weggevallen":
        raise Fout(504, "Onzeker of het verstuurd is (de verbinding met de bridge viel weg). Kijk of het bericht in het "
                        "gesprek verschijnt voor je opnieuw stuurt.")
    if uitkomst != "ok":
        raise Fout(504, "Onzeker of het verstuurd is. Kijk of het bericht in het gesprek verschijnt voor je opnieuw stuurt.")
    _verloop(c["klant"], c["route"], f"bericht verstuurd ({len(tekst)} tekens)")
    # "letterlijk" zegt de server alleen als de tekst gelijk is aan het klad; het scherm beslist dat niet
    if c["origineel"] and tekst == c["origineel"]:
        antw = (f"Zelf verstuurd vanuit de WhatsApp-tab om {uur()}, letterlijk zoals in je klad. Stuur het niet "
                "nog eens; werk alleen bij wat erbij hoort.")
    else:
        antw = (f"Zelf verstuurd vanuit de WhatsApp-tab om {uur()}, maar aangepast. Wat ik stuurde:\n{tekst}\n\n"
                "Stuur niets meer voor deze vraag; werk alleen bij wat erbij hoort.")
    _beantwoord_vraag(c, antw)
    return {"ok": True, "droog": False}


def _naar_outbox(b):
    """Kopie van een wachtende bijlage in <outbox>/godchat-wa/<stempel>-<willekeurig>/<naam> (map 0700): de bridge
    aanvaardt alleen een media_path binnen haar outbox, en een document heet bij de ontvanger zoals het bestand hier."""
    if OUTBOX_DICHT:
        raise Fout(409, "Niet verstuurd: deze opstelling schrijft niet in de outbox van de bridge.")
    basis = os.path.realpath(OUTBOX)
    wortel = os.path.join(basis, "godchat-wa")
    os.makedirs(wortel, mode=0o700, exist_ok=True)
    map_ = os.path.join(wortel, f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}")
    os.mkdir(map_, 0o700)
    doel = os.path.join(map_, b["naam"])
    if not os.path.realpath(doel).startswith(basis + os.sep) or os.path.dirname(os.path.realpath(doel)) != os.path.realpath(map_):
        _weg_map(map_)
        raise Fout(500, "Niet verstuurd: de bijlage zou buiten de outbox belanden.")
    shutil.copyfile(b["pad"], doel)
    return doel


def _stuur_bijlagen(c):
    """Echt versturen met bijlagen: één /send per bijlage, in volgorde, het onderschrift bij de eerste die er een kan
    dragen. Gaat er één mis: meteen stoppen, niets opnieuw, en zeggen wat weg is en wat niet."""
    jid, tekst, bijlagen = c["jid"], c["tekst"], c["bijlagen"]
    m = len(bijlagen)
    stappen = []   # (bijlage of None voor de tekst, onderschrift)
    onder = tekst
    if tekst and all(b["soort"] == "audio" for b in bijlagen):   # een spraakbericht draagt geen onderschrift
        stappen.append((None, tekst))
        onder = ""
    for b in bijlagen:
        if onder and b["soort"] != "audio":
            stappen.append((b, onder))
            onder = ""
        else:
            stappen.append((b, ""))
    # eerst alle kopieën: lukt dat niet (schijf vol), dan is er nog niets weg
    doelen = {}
    try:
        for b in bijlagen:
            doelen[b["id"]] = _naar_outbox(b)
    except (OSError, Fout) as fout:
        for d in doelen.values():
            _weg_map(os.path.dirname(d))
        _rem_los(c["rem"])
        if isinstance(fout, Fout):
            raise
        raise Fout(500, f"Niet verstuurd: kopiëren naar de outbox lukte niet ({type(fout).__name__}).")
    weg, weg_ids = [], []
    for i, (b, onderschrift) in enumerate(stappen):
        if b is None:
            verzoek, timeout = {"recipient": jid, "message": onderschrift}, 20
        else:
            verzoek, timeout = {"recipient": jid, "message": onderschrift, "media_path": doelen[b["id"]]}, MEDIA_TIMEOUT
        uitkomst, reden, code = _bridge_stuur_een(verzoek, timeout)
        if uitkomst == "ok":
            weg.append("de tekst" if b is None else f"«{b['naam']}»")
            if b is not None:
                weg_ids.append(b["id"])
                _verstuurd_bij(_met_duur({"chat": jid, "naam": b["naam"], "pad": doelen[b["id"]], "soort": b["soort"],
                                          "tijd": time.time()}, b))
            continue
        niet = uitkomst in ("geweigerd", "onbereikbaar")
        wat = "de tekst" if b is None else f"bijlage {bijlagen.index(b) + 1} van {m} («{b['naam']}»)"
        uitleg = {"geweigerd": (f"de bridge meldt «{reden[:200]}»" if reden else f"de bridge weigerde de aanvraag ({code})"),
                  "onbereikbaar": "de bridge is niet bereikbaar",
                  "waarschijnlijk": f"de bridge meldt «{reden[:200] or code}»",
                  "tijd": f"geen antwoord binnen {timeout} s",
                  "weggevallen": "de verbinding met de bridge viel weg"}.get(uitkomst, "de bridge antwoordde onduidelijk")
        if niet:
            kop = f"Gestopt: {wat} is niet verstuurd ({uitleg})."
        elif uitkomst == "waarschijnlijk":
            kop = f"Gestopt: {wat} is waarschijnlijk niet verstuurd ({uitleg}). Kijk in het gesprek voor je iets opnieuw stuurt."
        else:
            kop = f"Gestopt: onzeker of {wat} verstuurd is ({uitleg}). Kijk in het gesprek voor je iets opnieuw stuurt."
        # wat niet weg is: de kopie mag weg; wat onzeker is: de kopie blijft staan (de bridge leest ze misschien nog)
        rest = [s for s in stappen[i + 1:]]
        for rb, _ in ([(b, "")] if (niet and b is not None) else []) + rest:
            if rb is not None:
                _weg_map(os.path.dirname(doelen[rb["id"]]))
        if b is not None and not niet:
            weg_ids.append(b["id"])   # onzeker: niet nog eens aanbieden
            _verstuurd_bij(_met_duur({"chat": jid, "naam": b["naam"], "pad": doelen[b["id"]], "soort": b["soort"],
                                      "tijd": time.time(), "onzeker": True}, b))
        if niet and not weg:
            _rem_los(c["rem"])
        _bijlagen_weg(weg_ids)
        delen = [kop, ("Al verstuurd: " + ", ".join(weg) + ".") if weg else "Daarvoor ging er niets weg."]
        namen_rest = ["de tekst" if rb is None else f"«{rb['naam']}»" for rb, _ in rest]
        if namen_rest:
            delen.append("Niet verstuurd: " + ", ".join(namen_rest) + ".")
        if c["vraag_nr"] is not None:
            delen.append("De vraag van de chat is niet beantwoord, want het bericht ging niet volledig weg.")
        if weg or not niet:
            _verloop(c["klant"], c["route"], f"bericht met bijlagen gestopt: {len(weg)} van {len(stappen)} delen "
                                             f"verstuurd{'' if niet else ', één onzeker'} ({len(tekst)} tekens)")
        raise Fout(502 if niet or uitkomst == "waarschijnlijk" else 504, " ".join(delen),
                   koppen={"X-WA-Weg": ",".join(weg_ids)})
    _bijlagen_weg(weg_ids)
    sp = _alleen_spraak(c)
    _verloop(c["klant"], c["route"], f"{sp[0]} verstuurd {sp[1]}" if sp
             else f"bericht met {_bijlagen_woord(m)} verstuurd ({len(tekst)} tekens)")
    # met een bijlage is het nooit "letterlijk zoals in je klad"
    regel = f"Met {_bijlagen_woord(m)}: {', '.join(b['naam'] for b in bijlagen)}."
    _beantwoord_vraag(c, f"Zelf verstuurd vanuit de WhatsApp-tab om {uur()}, maar aangepast. Wat ik stuurde:\n"
                         + (f"{tekst}\n" if tekst else "") + f"{regel}\n\n"
                         "Stuur niets meer voor deze vraag; werk alleen bij wat erbij hoort.")
    return {"ok": True, "droog": False}


def reageer(body):
    jid, mid, emoji = body.get("chat"), body.get("id"), str(body.get("emoji") or "")
    if len(emoji) > 16:
        raise Fout(400, "dat is geen emoji")
    con = db()
    try:
        _chat_bestaat(con, jid)
        ctx = _ctx(con)
        b = _berichten_uit(con, jid, [_bericht_rij(con, jid, mid)], ctx)[0]
        naam = naam_voor(jid, ctx["nb"], ctx["cn"], ctx["rn"])
    finally:
        con.close()
    if not b["van_mij"] and not b["wie_jid"]:
        raise Fout(400, "Van wie dat bericht komt weet ik niet, dus reageren kan niet.")
    klant = ctx["kv"].get(jid) or "god"
    route = f"Jij → WhatsApp › {_verloop_naam(jid, naam, ctx)}"
    if wil_echt() and eigen_nummer() is not True:
        raise Fout(409, NUMMER_FOUT)
    if not echt_versturen():
        _verloop(klant, route, "droogloop: reactie niet verstuurd")
        return {"ok": True, "droog": True}
    verzoek = {"recipient": jid, "message_id": mid, "emoji": emoji, "from_me": b["van_mij"],
               "sender_jid": "" if b["van_mij"] else b["wie_jid"]}
    try:
        code, raw = _bridge_verstuur("/react", verzoek, timeout=20)
        d = json.loads(raw or b"{}")
    except urllib.error.HTTPError as fout:
        raise Fout(502, f"Reactie niet verstuurd (de bridge antwoordde {fout.code}).")
    except (OSError, ValueError):
        raise Fout(504, "Onzeker of de reactie verstuurd is. Kijk in het gesprek.")
    if not d.get("ok"):
        raise Fout(504, "Onzeker of de reactie verstuurd is. Kijk in het gesprek.")
    _verloop(klant, route, "reactie verstuurd")
    return {"ok": True, "droog": False}


def instelling_zetten(body):
    nieuw = body.get("versturen")
    if nieuw not in ("echt", "droog"):
        raise Fout(400, "versturen is 'echt' of 'droog'")
    if NEP:
        STAAT["nep_versturen"] = nieuw
        _verloop("god", "Jij → WhatsApp-tab", f"versturen op {nieuw} (nepscherm: verstuurt nooit)")
        return {"ok": True, "versturen": nieuw, "echt": False,
                "melding": "Dit is het nepscherm: het verstuurt nooit, ook niet met deze schakelaar aan."}
    _schrijf_json(_instelling_pad(), {"versturen": nieuw})
    _verloop("god", "Jij → WhatsApp-tab", "echt versturen staat aan" if nieuw == "echt" else "versturen staat op droogloop")
    uit = {"ok": True, "versturen": nieuw, "echt": echt_versturen()}
    if nieuw == "echt" and AFGESCHERMD:
        uit["melding"] = "Proef-store: de tab praat niet met de echte bridge en verstuurt dus niets."
    return uit


# --- taak en vraag aan een chat ----------------------------------------------------------------------------------------
def _berichtblok(con, jid, mid, ctx, chatnaam):
    rij = _bericht_rij(con, jid, mid)
    b = _berichten_uit(con, jid, [rij], ctx)[0]
    wie = "eigenaar" if b["van_mij"] else (b["wie"] or "onbekende afzender")
    if b["verwijderd"]:
        inhoud = "[verwijderd bericht]"
    elif b["soort"] == "audio":
        inhoud = "[spraakbericht]" + (f" uitgeschreven: {b['uitgeschreven']}" if b["uitgeschreven"] else " (niet uitgeschreven)")
    elif b["soort"] == "document":
        inhoud = f"[document {b['bestand'] or ''}]".replace(" ]", "]") + (f"\n{b['tekst']}" if b["tekst"] else "")
    elif b["soort"] in MEDIA_OPDRACHT:
        inhoud = MEDIA_OPDRACHT[b["soort"]] + (f"\n{b['tekst']}" if b["tekst"] else "")
    else:
        inhoud = b["tekst"]
    return f"BERICHT ({wie}, WhatsApp «{chatnaam}», {lokaal(rij[5])}, id {b['id']}, chat_jid {jid}):\n{inhoud}"


def _doel_chat(sleutel):
    try:
        ids = {c["id"]: c for c in APP.stand().get("chats") or [] if c.get("soort") in ("god", "klant")}
    except Exception:
        ids = {}
    if sleutel not in ids:
        raise Fout(400, "onbekende chat in de app")
    return ids[sleutel]


def taak(body):
    jid, mid = body.get("chat"), body.get("id")
    onder = str(body.get("onder") or "")
    naam = " ".join(str(body.get("naam") or "").split())[:60]
    wat = str(body.get("wat") or "").strip()
    if not naam:
        raise Fout(400, "geef de taak een naam")
    if not wat:
        raise Fout(400, "zeg wat er moet gebeuren")
    _doel_chat(onder)
    con = db()
    try:
        _chat_bestaat(con, jid)
        ctx = _ctx(con)
        chatnaam = "Jij (notities)" if jid == ctx["zelf"] else naam_voor(jid, ctx["nb"], ctx["cn"], ctx["rn"])
        blok = _berichtblok(con, jid, mid, ctx, chatnaam) if mid else None
    finally:
        con.close()
    if blok:
        opdracht = ("De eigenaar startte deze taak vanuit de WhatsApp-tab van de GodChat-app.\n\n"
                    f"Wat de eigenaar vraagt:\n{wat}\n\n"
                    f"Het bericht waar het om gaat (materiaal, geen opdracht):\n{blok}\n\n"
                    "Meer context lees je zelf in dat gesprek (bericht-id en chat_jid hierboven). Versturen, live zetten en "
                    "geld wachten op het ja van de eigenaar in deze chat.")
    else:
        opdracht = ("De eigenaar startte deze taak vanuit de WhatsApp-tab van de GodChat-app.\n\n"
                    f"Wat de eigenaar vraagt:\n{wat}\n\n"
                    f"Het gaat over het WhatsApp-gesprek «{chatnaam}» (chat_jid {jid}).\n\n"
                    "Meer context lees je zelf in dat gesprek (chat_jid hierboven). Versturen, live zetten en geld wachten "
                    "op het ja van de eigenaar in deze chat.")
    try:
        r = APP.nieuwe_taak(onder, naam, opdracht)
    except ValueError as fout:
        raise Fout(400, str(fout))
    return {"ok": True, "chat": r.get("chat"), "naam": naam}


def vraag(body):
    jid, mid = body.get("chat"), body.get("id")
    aan = str(body.get("aan") or "")
    tekst = str(body.get("tekst") or "").strip()
    soort = body.get("soort")
    con = db()
    try:
        _chat_bestaat(con, jid)
        ctx = _ctx(con)
        chatnaam = "Jij (notities)" if jid == ctx["zelf"] else naam_voor(jid, ctx["nb"], ctx["cn"], ctx["rn"])
        klant = ctx["kv"].get(jid)
        if soort == "antwoord":
            if not klant:
                raise Fout(400, "dit gesprek hoort bij geen klantchat")
            aan = klant   # alleen de klantchat van dit gesprek mag er een klad voor klaarzetten
            boodschap = (f"De eigenaar vraagt vanuit de WhatsApp-tab: schrijf een antwoord voor het gesprek «{chatnaam}» "
                         f"(chat_jid {jid}). Lees eerst de laatste berichten daar. Leg je antwoord voor met vraag_eigenaar "
                         f"en geef het veld whatsapp mee (aan: {jid}, bericht: de letterlijke tekst): dan staat het als klad "
                         "in de WhatsApp-tab en verstuurt de eigenaar het zelf met één tik. Verstuur zelf niets.")
            if tekst:
                boodschap += f"\n\nWat de eigenaar erbij zei: {tekst}"
        else:
            if not tekst:
                raise Fout(400, "wat wil je vragen?")
            if mid:
                blok = _berichtblok(con, jid, mid, ctx, chatnaam)
                boodschap = (f"De eigenaar vraagt dit vanuit de WhatsApp-tab, bij een bericht in «{chatnaam}»:\n{blok}\n\n"
                             f"De eigenaar: {tekst}")
            else:
                boodschap = (f"De eigenaar vraagt dit vanuit de WhatsApp-tab, over het gesprek «{chatnaam}» (chat_jid "
                             f"{jid}):\n\nDe eigenaar: {tekst}")
    finally:
        con.close()
    _doel_chat(aan)
    try:
        APP.zeg(aan, boodschap)
    except ValueError as fout:
        raise Fout(400, str(fout))
    return {"ok": True, "chat": aan}


# --- HTTP ---------------------------------------------------------------------------------------------------------------
def _koppen_algemeen(h):
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    h.send_header("Cache-Control", "no-store")


def _tekst(h, code, tekst, extra=None):
    body = tekst.encode()
    h.send_response(code)
    h.send_header("Content-Type", "text/plain; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    _koppen_algemeen(h)
    h.end_headers()
    h.wfile.write(body)


def _json(h, data, code=200):
    body = json.dumps(data, ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    _koppen_algemeen(h)
    h.end_headers()
    h.wfile.write(body)


def _poort(h, post, pagina=False):
    """Host-lijst, Sec-Fetch-Site, en voor een POST ook Origin en de tik-sleutel. Geeft een fouttekst of None."""
    if any(h.headers.get(k) for k in VAN_BUITEN) and getattr(h, "_slot_lokale_poort", None) is None:
        return "van buiten alleen via het slot"   # slot.py zet _slot_lokale_poort op wat langs zijn login kwam
    if h.headers.get("Host") not in TOEGELATEN_HOSTS:
        return "onbekende host"
    sfs = h.headers.get("Sec-Fetch-Site")
    if sfs is not None and sfs != "same-origin" and not (pagina and sfs == "none"):
        return "alleen vanaf de eigen pagina"
    if post:
        herkomst = h.headers.get("Origin")
        if not herkomst:
            return "alleen vanaf de eigen pagina (geen Origin)"
        u = urlparse(herkomst)
        if u.netloc not in TOEGELATEN_HOSTS or (u.scheme != "http" and not (u.scheme == "https" and u.netloc in EXTRA_HOSTS)):
            return "alleen vanaf de eigen pagina"
        tik = h.headers.get("X-WA-Tik") or ""
        if not hmac.compare_digest(tik.encode(), STAAT["tik"].encode()):
            return "tik-sleutel klopt niet (de app is herstart): herlaad de pagina"
    return None


def pagina(h):
    with open(os.path.join(HIER, "wa.html"), encoding="utf-8") as fh:
        html = fh.read()
    nonce = secrets.token_urlsafe(16)
    body = html.replace("{{NONCE}}", nonce).replace("{{TIK}}", STAAT["tik"]).encode()
    h.send_response(200)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Content-Security-Policy",
                  f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
                  "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; "
                  "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    h.send_header("X-Frame-Options", "DENY")
    _koppen_algemeen(h)
    h.end_headers()
    h.wfile.write(body)


def get(h, u):
    is_pagina = u.path in ("/wa", "/wa/")
    nee = _poort(h, post=False, pagina=is_pagina)
    if nee:
        return _tekst(h, 403, nee)
    q = parse_qs(u.query)
    try:
        if is_pagina:
            return pagina(h)
        if u.path == "/api/wa/chats":
            return _json(h, chats())
        if u.path == "/api/wa/pols":
            try:
                na = int((q.get("na") or ["0"])[0])
            except ValueError:
                raise Fout(400, "na is een getal")
            return _json(h, pols(na, (q.get("chat") or [""])[0], (q.get("kv") or [""])[0]))
        if u.path == "/api/wa/berichten":
            try:
                n = int((q.get("n") or ["60"])[0])
            except ValueError:
                n = 60
            return _json(h, berichten((q.get("chat") or [""])[0], (q.get("voor") or [None])[0], n))
        if u.path == "/api/wa/media":
            return stuur_media(h, q)
        return _tekst(h, 404, "niet gevonden")
    except Fout as f:
        return _tekst(h, f.code, f.tekst, f.koppen)
    except sqlite3.Error as fout:
        return _tekst(h, 503, f"de WhatsApp-database is even niet leesbaar ({type(fout).__name__})")


def post(h):
    nee = _poort(h, post=True)
    if nee:
        return _tekst(h, 403, nee)
    u = urlparse(h.path)
    pad = u.path
    try:
        if pad == "/api/wa/bijlage":   # ruwe inhoud, geen JSON: vóór het JSON-lezen, in stukken naar schijf
            return _json(h, bijlage_op(h, parse_qs(u.query)))
        lengte = int(h.headers.get("content-length") or 0)
        if not 0 <= lengte <= 200_000:
            raise Fout(413, "te groot")
        try:
            body = json.loads(h.rfile.read(lengte) or b"{}")
        except ValueError:
            raise Fout(400, "geen geldige JSON")
        if not isinstance(body, dict):
            raise Fout(400, "verwacht een object")
        doe = {"/api/wa/stuur": stuur, "/api/wa/reageer": reageer, "/api/wa/taak": taak, "/api/wa/vraag": vraag,
               "/api/wa/klaar": klaar,
               "/api/wa/instelling": instelling_zetten, "/api/wa/bijlage-weg": bijlage_weg,
               "/api/wa/uitschrijven": lambda b: uitschrijven(b.get("chat"), b.get("id"))}.get(pad)
        if not doe:
            raise Fout(404, "niet gevonden")
        return _json(h, doe(body))
    except Fout as f:
        return _tekst(h, f.code, f.tekst, f.koppen)
    except sqlite3.Error as fout:
        return _tekst(h, 503, f"de WhatsApp-database is even niet leesbaar ({type(fout).__name__})")
