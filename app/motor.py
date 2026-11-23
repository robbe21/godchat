"""De motor van de GodChat-app: per chat één Claude Code-sessie (claude -p met streaming JSON) en één wachter (wacht.py).

Eén beurt tegelijk per chat. De wachter draait tussen de beurten; wat hij levert (NIEUW, HARTSLAG) gaat als beurt naar
de sessie, en pas als die beurt klaar is start hij opnieuw (zo blijft 'afgehandeld' in state/ kloppen).
Vragen aan de eigenaar en doorsturen tussen chats lopen via de tools in godchat_mcp.py, niet via tekst.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

HIER = os.path.dirname(os.path.abspath(__file__))
GC = os.path.dirname(HIER)                      # tools/god-chat
WORK = os.path.dirname(os.path.dirname(GC))     # de werkmap: projectmappen staan relatief hieraan
DATA = os.path.join(HIER, "data")
ROUTING = os.path.join(GC, "routing.json")      # (tests wijzen dit naar een eigen bestand)
PATH_EXTRA = ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")]
KNIP_TOOL = 4000
MODELLEN = ("opus", "sonnet", "fable", "haiku")   # aliassen, nooit een versienummer
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_TAKEN = 6   # open taakchats per klant: een rem, geen doel
HERVAT_RONDE = ("HERVAT NA EEN HERSTART VAN DE APP: je was al met deze ronde bezig. Kijk eerst in je eigen gesprek "
                "hierboven en in de logboeken van de opdracht wat je al deed, en doe niets dubbel. Verstuurde je al "
                "iets, verstuur het niet opnieuw.")
RONDE_VERLOREN = "de app viel weg voor de ronde begon"
# Zuinig met context: een API-fout 529 nog twee keer opnieuw, en samenvatten tussen twee beurten.
OVERBELAST = re.compile(r"\b529\b|overloaded", re.I)
OPNIEUW_NA_S = (60, 180)
OPNIEUW_TEKST = ("[GodChat-app] Je vorige beurt brak af op een tijdelijke API-fout (529 Overloaded), niet door jou. "
                 "Doe die beurt nu alsnog: het bericht staat hierboven. Wat je al deed, doe je niet dubbel.")
VENSTER_STANDAARD = 1_000_000
OCHTEND_MIN = 60_000           # de ochtendronde vat een chat samen vanaf zoveel tokens (sessie en open vragen blijven)
COMPACT_NA_VRAAG_S = 15 * 60   # niet samenvatten zolang een vraag van deze chat jonger is dan dit
COMPACT_OPDRACHT = (
    "/compact Dit is een chat in de GodChat-app. Bewaar in de samenvatting letterlijk en volledig: wie je bent "
    "(je sleutel, je klant of je onderwerp, je opdracht), hoe de eigenaar heet, dat tools/god-chat/PROTOCOL.md (bij de "
    "God-chat ook GOD.md) en je CHAT.md gelden, elke vraag die bij de eigenaar openstaat en waarover, alles wat op het "
    "ja van de eigenaar wacht, elke toezegging die de eigenaar zelf deed en elke lopende afspraak (met wie, wat, "
    "wanneer), wat je al verstuurde of live zette, en werk dat half af is (branch, bestand, volgende stap). Laat weg: "
    "toolresultaten, bestandsinhoud en tussenstappen die af zijn.")


def nu_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def uur(ts=None):
    return datetime.fromtimestamp(ts or time.time()).strftime("%H:%M")


def beurt_gebruik(e):
    """Tokens en API-prijs uit het 'result' van `claude -p`; gaat ruw mee in de verloopregel van de beurt."""
    u = e.get("usage") or {}
    g = {"in": u.get("input_tokens"), "uit": u.get("output_tokens"), "cache_lezen": u.get("cache_read_input_tokens"),
         "cache_schrijven": u.get("cache_creation_input_tokens"), "kosten_usd": e.get("total_cost_usd"),
         "stappen": e.get("num_turns")}
    return {k: v for k, v in g.items() if isinstance(v, (int, float)) and not isinstance(v, bool)} or None


def klant_bord(chat, velden):
    """De velden voor de klantrij op het dashboard (klanten.py, via klanten_lader). Een fout daar raakt de chat nooit."""
    try:
        import klanten_lader
        return klanten_lader.meld(chat.id, chat.soort, chat.ouder, velden) or ""
    except (Exception, SystemExit):
        return ""


def klok(naam, *args, standaard=None):
    """Een haak van de klok voor geplande taken (klok.py, via klok_lader). Een fout daar raakt de chat nooit."""
    try:
        import klok_lader
        return getattr(klok_lader, naam)(*args)
    except (Exception, SystemExit):
        return standaard


def archief_stempel(st):
    """Een rondegesprek gaat naar het archief onder de starttijd van die ronde (de pagina van de taak koppelt zo
    gesprek en ronde; klok.py → taak_detail). Zonder starttijd: nu."""
    try:
        return datetime.fromisoformat((st or {}).get("sinds")).astimezone(ZoneInfo(os.environ.get("GODCHAT_TZ") or "Europe/Brussels")).strftime("%Y-%m-%d-%H%M")
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d-%H%M")


def archief_pad(cid, stempel):
    """data/archief/<cid>-<stempel>.json; bestaat dat al, dan -2, -3 …"""
    pad = os.path.join(DATA, "archief", f"{cid}-{stempel}.json")
    n = 2
    while os.path.exists(pad):
        pad = os.path.join(DATA, "archief", f"{cid}-{stempel}-{n}.json")
        n += 1
    return pad


def lees_json(pad, standaard):
    try:
        with open(pad, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return standaard


def lees_routing():
    """routing.json: welke WhatsApp-chats bij welk project horen. Zonder dat bestand start de app niet."""
    try:
        with open(ROUTING, encoding="utf-8") as fh:
            r = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"Geen routing.json in {os.path.dirname(ROUTING)}: kopieer routing.voorbeeld.json naar "
                         "routing.json en vul je eigen gegevens in.") from None
    except ValueError as fout:
        raise SystemExit(f"{ROUTING} is geen geldige JSON: {fout}") from None
    if not isinstance(r, dict) or not isinstance(r.get("klanten"), dict):
        raise SystemExit(f"{ROUTING} heeft geen object 'klanten' (zie routing.voorbeeld.json).")
    return r


def schrijf_json(pad, data):
    os.makedirs(os.path.dirname(pad), exist_ok=True)
    tmp = pad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, pad)


def schone_omgeving():
    """De omgeving van een terminal: zonder wat de Claude-desktop-app zijn kinderen meegeeft (eigen API-adres, eigen
    login, CLAUDE_CODE_*). Met die variabelen negeert `claude` het env-blok uit ~/.claude/settings.json."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "ANTHROPIC")) and k not in ("AI_AGENT", "BAGGAGE", "__CFBundleIdentifier")}
    paden = env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":")
    env["PATH"] = ":".join([p for p in PATH_EXTRA if p not in paden] + paden)
    return env


def kort(tekst, n):
    tekst = " ".join(str(tekst or "").split())
    return tekst if len(tekst) <= n else tekst[: n - 1] + "…"


def tool_invoer(naam, invoer):
    if not isinstance(invoer, dict):
        return kort(invoer, KNIP_TOOL)
    for sleutel in ("command", "file_path", "pattern", "url", "query"):
        if sleutel in invoer and len(invoer) <= 3:
            return str(invoer[sleutel])
    return json.dumps(invoer, ensure_ascii=False, indent=1)[:KNIP_TOOL]


def tool_uitvoer(inhoud):
    if isinstance(inhoud, list):
        inhoud = "\n".join(x.get("text", f"[{x.get('type')}]") for x in inhoud if isinstance(x, dict))
    inhoud = str(inhoud or "")
    return inhoud if len(inhoud) <= KNIP_TOOL else inhoud[:KNIP_TOOL] + f"\n… (+{len(inhoud) - KNIP_TOOL} tekens)"


def per_klant_regel(klant):
    """De bijzonderheden van een project: `klanten.<sleutel>.bijzonderheden` in routing.json (tekst, of een lijst
    regels). Staat er niets, dan niets. Bewust niet in CLAUDE.md van de kit: dat bestand komt uit de repo."""
    try:
        with open(ROUTING, encoding="utf-8") as fh:
            info = (json.load(fh).get("klanten") or {}).get(klant) or {}
    except (OSError, ValueError, AttributeError):
        return ""
    b = info.get("bijzonderheden") if isinstance(info, dict) else None
    if isinstance(b, list):
        b = " ".join(str(x).strip() for x in b if str(x).strip())
    return b.strip() if isinstance(b, str) else ""


# Een automatisering staat altijd als geplande taak onder haar project. Chats in de app krijgen dus geen eigen wekker
# of cron (die zie je nergens en stopt met de sessie); ze plannen met plan_taak (klok.py).
WEKKERS = "CronCreate,CronDelete,CronList,ScheduleWakeup"


def eigenaar_zin(naam):
    """De zin die elke sessie in haar startbeurt krijgt: hoe de eigenaar heet (routing.json → "eigenaar")."""
    naam = " ".join(str(naam or "").split())
    return f"De eigenaar heet {naam}." if naam else ""


class Sessie:
    """Eén `claude -p`-proces met streaming JSON in en uit. Blijft open tussen de beurten."""

    def __init__(self, chat, resume):
        self.chat = chat
        m = chat.motor
        mcp = {"mcpServers": {"godchat": {
            "command": "python3", "args": ["-I", os.path.join(HIER, "godchat_mcp.py")],
            "env": {"GODCHAT_URL": f"http://127.0.0.1:{m.poort}", "GODCHAT_CHAT": chat.id, "GODCHAT_SLEUTEL": m.sleutel}}}}
        cfg = m.cfg(chat.id)
        args = [m.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "bypassPermissions", "--mcp-config", json.dumps(mcp),
                "-n", f"GodChat · {chat.naam}", "--disallowedTools", WEKKERS]
        self.sig = (cfg.get("model"), cfg.get("effort"))
        if cfg.get("model"):
            args += ["--model", cfg["model"]]
        if cfg.get("effort"):
            args += ["--effort", cfg["effort"]]
        if resume:
            args += ["--resume", resume]
        os.makedirs(os.path.join(DATA, "logs"), exist_ok=True)
        self.log = open(os.path.join(DATA, "logs", f"{chat.id}.stderr.log"), "a", encoding="utf-8")
        self.log.write(f"\n--- {nu_iso()} start{' (resume)' if resume else ''}\n")
        self.log.flush()
        self.proc = subprocess.Popen(args, cwd=WORK, env=m.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.log, text=True, bufsize=1)
        threading.Thread(target=self._lees, daemon=True, name=f"lees-{chat.id}").start()

    def leeft(self):
        return self.proc.poll() is None

    def stuur(self, tekst):
        self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": tekst}}) + "\n")
        self.proc.stdin.flush()

    def _lees(self):
        for regel in self.proc.stdout:
            try:
                e = json.loads(regel)
            except ValueError:
                continue
            self.chat.verwerk(e)
        self.chat.proces_weg(self)

    def stop(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


class Chat:
    def __init__(self, motor, cid, naam, soort, info):
        self.motor, self.id, self.naam, self.soort, self.info = motor, cid, naam, soort, info
        self.ouder = None            # taakchat: de sleutel van de klantchat erboven
        self.slot = threading.RLock()
        self.wachtrij = deque()
        self.wek = threading.Condition(self.slot)
        self.beurt_klaar = threading.Event()
        self.sessie = None
        self.bezig = False
        self.fout = None
        self.versie = 0
        self.wachter = None          # Popen van wacht.py
        self.wachter_draad = None
        self.wachter_mag = threading.Event()
        self.huidig = None           # de assistent-beurt die nu loopt
        self.beurt_regel = None      # meld_stand tijdens deze beurt
        self.beurt_start = 0
        self.hervat_bij_start = False
        p = self.pad_gesprek()
        self.beurten = lees_json(p, [])
        st = motor.stand_chat(cid)
        self.regel, self.uur = st.get("regel", ""), st.get("uur", "")
        threading.Thread(target=self._werk, daemon=True, name=f"werk-{cid}").start()

    # --- bestanden -----------------------------------------------------------------------------------------------
    def pad_gesprek(self):
        return os.path.join(DATA, "gesprek", f"{self.id}.json")

    def bewaar(self):
        schrijf_json(self.pad_gesprek(), self.beurten)

    def st(self):
        return self.motor.stand_chat(self.id)

    # --- toestand ------------------------------------------------------------------------------------------------
    def toestand(self):
        st = self.st()
        if self.soort == "taak" and st.get("status") == "klaar":
            return "klaar"
        if st.get("noodrem"):
            return "uit"
        if not st.get("in_app"):
            return "desktop"   # draait niet in de app (nog niet gestart, of overgenomen door een chat elders)
        if self.fout:
            return "fout"
        if self.bezig or self.wachtrij or self.huidig is not None:
            return "werkt"
        if any(v["chat"] == self.id and v["status"] == "open" for v in self.motor.vragen):
            return "wacht"
        return "slaapt"

    def zet_regel(self, regel):
        with self.slot:
            self.regel, self.uur = kort(regel, 160), uur()
            self.motor.zet_chat(self.id, regel=self.regel, uur=self.uur)
            self.versie += 1

    # --- beurten -------------------------------------------------------------------------------------------------
    def beurt(self, bron, tekst, na=None, naschrift=""):
        """Zet een beurt in de rij. na: 'start' (wachter starten als de beurt klaar is) of 'wachter' (opnieuw starten)."""
        with self.slot:
            self.wachtrij.append({"bron": bron, "tekst": tekst, "na": na, "naschrift": naschrift, "uur": uur()})   # naschrift: wel naar de sessie, niet in het gesprek
            self.versie += 1
            self.wek.notify()

    def _werk(self):
        while not self.motor.stopt:
            with self.slot:
                while not self.wachtrij and not self.motor.stopt:
                    self.wek.wait(1)
                if self.motor.stopt:
                    return
                item = self.wachtrij.popleft()
                self.bezig = True
                self.versie += 1
            if item.get("compact"):   # de ochtendronde: samenvatten, verder niets (geen wachter, geen klok)
                self._misschien_compacten(ochtend=True)
                with self.slot:
                    self.bezig = False
                    self.versie += 1
                continue
            self._draai(item)
            with self.slot:
                self.bezig = False
                self.versie += 1
            if self.soort == "taak":
                if self.st().get("status") == "klaar":
                    self.stop_sessie()   # taak_klaar kwam midden in de beurt
                elif self.st().get("ronde"):
                    klok("beurt_einde", self)   # een geplande ronde: fout, of zelf sluiten als ze nergens op wacht
                else:
                    self._misschien_compacten()
                continue                 # een taakchat heeft geen wachter
            if item.get("na") in ("start", "wachter") and self.st().get("in_app"):
                self.wachter_mag.set()
                if item["na"] == "start":
                    self.start_wachter(hervat=self.hervat_bij_start)
                    self.hervat_bij_start = False
            self._misschien_compacten()

    # --- zuinig met context ----------------------------------------------------------------------------------------
    def compact_drempel(self):
        """Tokens waarboven deze chat tussen twee beurten samengevat wordt; None = uit. config.json → "compact"."""
        c = self.motor.config.get("compact") or {}
        if not c.get("aan"):
            return None
        venster = self.st().get("venster") or VENSTER_STANDAARD
        kandidaten = [venster * c["pct"] / 100] if c.get("pct") else []
        if c.get("tokens"):
            kandidaten.append(c["tokens"])
        return int(min(kandidaten)) if kandidaten else None

    def _misschien_compacten(self, ochtend=False):
        """Alleen tussen twee beurten, in de werkdraad: niets in de rij, geen fout, en geen vraag die net gesteld is.
        ochtend: de ronde van de klok; drempel OCHTEND_MIN, en het gesprek op het scherm gaat eerst naar het archief."""
        drempel = self.compact_drempel()
        if ochtend and drempel:
            drempel = min(drempel, OCHTEND_MIN)
        ctx = self.st().get("ctx") or 0
        if not drempel or ctx < drempel or self.motor.stopt:
            return
        st = self.st()
        if self.fout or self.wachtrij or not st.get("in_app") or st.get("noodrem") or st.get("status") == "klaar":
            return
        if not st.get("sessie"):
            return   # (een sessie die na een herstart nog niet draait, start _draai zelf met --resume)
        grens = datetime.now().astimezone().timestamp() - COMPACT_NA_VRAAG_S
        for v in self.motor.vragen:
            if v["chat"] == self.id and v["status"] == "open":
                try:
                    if datetime.fromisoformat(v["ts"]).timestamp() > grens:
                        return   # het antwoord kan onderweg zijn: na een volgende beurt opnieuw kijken
                except (KeyError, ValueError):
                    return
        self.compact_info = None
        regel = self.regel
        if ochtend:
            with self.slot:
                if self.beurten:
                    os.makedirs(os.path.join(DATA, "archief"), exist_ok=True)
                    schrijf_json(os.path.join(DATA, "archief", f"{self.id}-{datetime.now():%Y-%m-%d-%H%M}.json"),
                                 self.beurten)
                self.beurten = []
                self.bewaar()
        self._draai({"bron": "app", "tekst": f"Gesprek samenvatten: {ctx // 1000}k tokens, drempel {drempel // 1000}k",
                     "stuur": COMPACT_OPDRACHT, "na": None, "uur": uur()})
        info = getattr(self, "compact_info", None)
        if info:
            na = info.get("post_tokens") or 0
            self.motor.zet_chat(self.id, ctx=na, compact_op=nu_iso())
            self.na_compact = True
            self.zet_regel(regel)
            self.motor.gebeurd("start", self.id, self.naam, f"gesprek samengevat: {ctx // 1000}k → {na // 1000}k tokens",
                               json.dumps(info, ensure_ascii=False)[:600])

    def herinnering_na_compact(self):
        """Het naschrift bij de eerste beurt na het samenvatten: de grenzen en de open vragen, letterlijk uit de app."""
        open_ = [v for v in self.motor.vragen if v["chat"] == self.id and v["status"] == "open"]
        werk = "`tools/god-chat/GOD.md` en `tools/god-chat/PROTOCOL.md`" if self.id == "god" else (
            "`tools/god-chat/PROTOCOL.md` en de `CHAT.md` van je klantmap" if self.soort == "klant" else
            "`tools/god-chat/PROTOCOL.md` (\"Wat altijd wacht op het ja van de eigenaar\")")
        tekst = (f"[GodChat-app] Je gesprek is net samengevat om tokens te sparen. Er geldt niets nieuws: {werk} blijven je "
                 "werkwijze. Twijfel je over een grens, lees die sectie dan opnieuw vóór je iets verstuurt, live zet of "
                 "geld raakt.")
        if self.motor.eigenaar:
            tekst += " " + eigenaar_zin(self.motor.eigenaar)
        if self.soort == "taak" and self.st().get("opdracht"):
            tekst += f"\n\nJe opdracht (uit je startbeurt):\n{kort(self.st()['opdracht'], 2500)}"
        if open_:
            tekst += "\n\nJe vragen die nu bij de eigenaar openstaan (stel ze niet opnieuw):\n" + "\n".join(
                f"- «{v['vraag']}» ({v.get('uur', '')})" + (f" — {kort(v['context'], 200)}" if v.get("context") else "")
                for v in open_)
        else:
            tekst += "\n\nEr staat geen vraag van jou open bij de eigenaar."
        return tekst

    def _draai(self, item):
        cfg = self.motor.cfg(self.id)
        if self.sessie and self.sessie.leeft() and self.sessie.sig != (cfg.get("model"), cfg.get("effort")):
            self.stop_sessie()   # model of effort gewijzigd: deze beurt start hem opnieuw met --resume
        if not item.get("stuur"):   # samenvatten wordt na een herstart niet herhaald: dat beslist de volgende beurt
            self.motor.zet_chat(self.id, lopend=item)
        if getattr(self, "na_compact", False) and not item.get("stuur"):
            self.na_compact = False
            item = {**item, "naschrift": "\n\n".join(x for x in (item.get("naschrift"), self.herinnering_na_compact()) if x)}
        self.pogingen_529 = 0
        with self.slot:
            self.beurten.append({"rol": "user", "bron": item["bron"], "uur": uur(), "tekst": item["tekst"]})
            self.huidig = {"rol": "assistant", "uur": uur(), "delen": [], "tools": {}}
            self.beurten.append(self.huidig)
            self.beurt_regel = None
            self.beurt_start = time.time()
            self.beurt_klaar.clear()
            self.versie += 1
            self.bewaar_zonder_huidig()
        for poging in (1, 2):
            try:
                if not self.sessie or not self.sessie.leeft():
                    self.sessie = Sessie(self, self.st().get("sessie"))
                self.sessie.stuur(item.get("stuur") or
                                  (item["tekst"] + (f"\n\n{item['naschrift']}" if item.get("naschrift") else "")))
                break
            except OSError as fout:
                self.sessie = None
                if poging == 2:
                    self._einde(f"kon de sessie niet starten: {fout}", fout=True)
                    return
        while True:
            while not self.beurt_klaar.wait(5):
                if self.motor.stopt:
                    return
                if self.sessie is None or not self.sessie.leeft():
                    if not self.beurt_klaar.is_set():
                        self._einde("de sessie stopte midden in de beurt (zie data/logs)", fout=True)
                    return
            if not getattr(self, "opnieuw", False):
                break
            # 529 Overloaded: de beurt loopt nog (geen _einde); na een pauze vraagt de app de sessie het opnieuw.
            self.opnieuw = False
            wacht = OPNIEUW_NA_S[min(self.pogingen_529, len(OPNIEUW_NA_S)) - 1]
            self.zet_regel(f"API overbelast (529): nieuwe poging {self.pogingen_529} van {len(OPNIEUW_NA_S)} om "
                           f"{uur(time.time() + wacht)}")
            einde = time.time() + wacht
            while time.time() < einde:
                if self.motor.stopt:
                    return
                time.sleep(2)
            with self.slot:
                self.beurt_klaar.clear()
            try:
                if not self.sessie or not self.sessie.leeft():
                    self.sessie = Sessie(self, self.st().get("sessie"))
                self.sessie.stuur(item.get("stuur") or OPNIEUW_TEKST)
            except OSError as fout:
                self.sessie = None
                self._einde(f"kon de sessie niet opnieuw starten na een 529: {fout}", fout=True)
                return
        self.motor.zet_chat(self.id, lopend=None)

    def bewaar_zonder_huidig(self):
        schrijf_json(self.pad_gesprek(), [self._schoon(b) for b in self.beurten])

    @staticmethod
    def _schoon(b):
        return {k: v for k, v in b.items() if k != "tools"}

    def verwerk(self, e):
        """Eén regel uit de stroom van `claude -p`."""
        soort = e.get("type")
        with self.slot:
            if soort == "system" and e.get("subtype") == "init":
                if e.get("session_id") and e["session_id"] != self.st().get("sessie"):
                    self.motor.zet_chat(self.id, sessie=e["session_id"])
                if self.soort == "taak" and self.st().get("ronde"):   # staan de koppelingen van de ronde erbij?
                    threading.Thread(target=klok, args=("koppelingen_bij_start", self.id, list(e.get("mcp_servers") or [])),
                                     daemon=True).start()   # eigen draad: de klok neemt het slot van de motor
                return
            if soort == "system" and e.get("subtype") == "compact_boundary":
                self.compact_info = e.get("compact_metadata") or {}   # ook als claude zelf samenvat (rond 97%)
                if self.compact_info.get("post_tokens"):
                    self.motor.zet_chat(self.id, ctx=self.compact_info["post_tokens"])
                return
            if soort == "system" and e.get("subtype") == "status" and e.get("status") == "compacting":
                self.zet_regel("Vat het gesprek samen om tokens te sparen…")
                return
            if e.get("parent_tool_use_id"):
                return   # binnenkant van een subagent
            h = self.huidig
            if h is None:
                if soort != "assistant":
                    return
                # Een beurt die de sessie zelf begon: een achtergrondtaak die klaar is, of het antwoord dat pas komt
                # nadat een eerder 'result' de beurt al afsloot (na --resume). Tonen, niet weggooien.
                h = self.huidig = {"rol": "assistant", "uur": uur(), "delen": [], "tools": {}, "vanzelf": True}
                self.beurten.append(h)
                self.beurt_regel = None
                self.beurt_start = time.time()
            if soort == "assistant":
                u = e.get("message", {}).get("usage") or {}
                ctx = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if ctx:
                    self.ctx = ctx   # hoe vol de context nu is: de laatste stap van de hoofdsessie
                for blok in e.get("message", {}).get("content", []):
                    if blok.get("type") == "text" and blok.get("text", "").strip():
                        h["delen"].append({"soort": "tekst", "tekst": blok["text"]})
                    elif blok.get("type") == "tool_use":
                        t = {"naam": blok.get("name", "?").replace("mcp__godchat__", ""),
                             "invoer": tool_invoer(blok.get("name"), blok.get("input")), "uitvoer": ""}
                        h["tools"][blok.get("id")] = t
                        if not h["delen"] or h["delen"][-1]["soort"] != "tools":
                            h["delen"].append({"soort": "tools", "tools": []})
                        h["delen"][-1]["tools"].append(t)
                self.versie += 1
            elif soort == "user":
                inhoud = e.get("message", {}).get("content", [])
                for blok in inhoud if isinstance(inhoud, list) else []:
                    if blok.get("type") == "tool_result" and blok.get("tool_use_id") in h["tools"]:
                        h["tools"][blok["tool_use_id"]]["uitvoer"] = tool_uitvoer(blok.get("content"))
                self.versie += 1
            elif soort == "result":
                self.gebruik = beurt_gebruik(e)
                vensters = [m.get("contextWindow") for m in (e.get("modelUsage") or {}).values() if isinstance(m, dict)]
                vensters = [v for v in vensters if isinstance(v, int) and v > 0]
                if getattr(self, "ctx", None) or vensters:
                    self.motor.zet_chat(self.id, **({"ctx": self.ctx} if getattr(self, "ctx", None) else {}),
                                        **({"venster": max(vensters)} if vensters else {}))
                    if self.gebruik is not None and getattr(self, "ctx", None):
                        self.gebruik["context"] = self.ctx
                    self.ctx = None
                if (e.get("is_error") and OVERBELAST.search(str(e.get("result") or ""))
                        and getattr(self, "pogingen_529", 0) < len(OPNIEUW_NA_S) and h is not None
                        and not h.get("vanzelf")):
                    self.pogingen_529 = getattr(self, "pogingen_529", 0) + 1
                    self.opnieuw = True     # _draai wacht en probeert opnieuw; de beurt loopt door
                    self.beurt_klaar.set()
                elif e.get("is_error"):
                    self._einde(f"fout: {kort(e.get('result') or e.get('subtype'), 200)}", fout=True)
                else:
                    self._einde(None)

    def _einde(self, fouttekst, fout=False):
        with self.slot:
            h = self.huidig
            duur = int(time.time() - self.beurt_start) if self.beurt_start else 0
            duur_t = f"{duur} s" if duur < 90 else f"{duur // 60} min {duur % 60} s"
            if h is not None:
                h["einde"] = (fouttekst + " · " if fouttekst else "beurt klaar · ") + duur_t
                h.pop("tools", None)
            self.huidig = None
            self.motor.zet_chat(self.id, lopend=None)
            self.fout = fouttekst if fout else None
            laatste = next((d["tekst"] for d in reversed(h["delen"]) if d["soort"] == "tekst"), "") if h else ""
            extra = {"duur": duur, **({"gebruik": self.gebruik} if getattr(self, "gebruik", None) else {})}
            self.gebruik = None
            if fout:
                self.zet_regel(fouttekst)
                self.motor.gebeurd("fout", self.id, self.naam, fouttekst, fouttekst, **extra)
            else:
                if not self.beurt_regel and laatste:
                    self.zet_regel(laatste.strip().splitlines()[0])
                tools = [t["naam"] for d in (h["delen"] if h else []) if d["soort"] == "tools" for t in d["tools"]]
                if laatste or tools:
                    tekst = laatste + (f"\n\nTools: {', '.join(tools)}" if tools else "")
                    self.motor.gebeurd("actie", self.id, self.naam, self.beurt_regel or kort(laatste, 110), tekst.strip(), **extra)
            self.bewaar()
            self.versie += 1
            self.beurt_klaar.set()

    def proces_weg(self, sessie):
        with self.slot:
            if self.sessie is not sessie:
                return   # een gestopte oude sessie; _draai bewaakt de huidige
            self.sessie = None
            if self.huidig is not None:
                self._einde("de sessie stopte midden in de beurt (zie data/logs)", fout=True)

    # --- wachter -------------------------------------------------------------------------------------------------
    def start_wachter(self, hervat=False):
        if self.soort == "taak" or (self.wachter_draad and self.wachter_draad.is_alive()):
            return
        self.wachter_mag.set()
        self.wachter_draad = threading.Thread(target=self._wachter, args=(hervat,), daemon=True, name=f"wacht-{self.id}")
        self.wachter_draad.start()

    def stop_wachter(self):
        self.wachter_mag.clear()
        p = self.wachter
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def _wachter(self, hervat):
        fouten = 0
        cfg = self.motor.config.get("chats", {}).get(self.id, {})
        while not self.motor.stopt and self.st().get("in_app"):
            if not self.wachter_mag.wait(2):
                continue
            args = ["python3", "-I", os.path.join(GC, "wacht.py"), self.id]
            if self.id == "god":
                args += ["--max-wacht", "21600"]
            args += cfg.get("wacht_args", [])
            if hervat:
                args.append("--hervat")
                hervat = False
            self.motor.zet_chat(self.id, luistert_sinds=nu_iso())
            self.versie += 1
            self.wachter = subprocess.Popen(args, cwd=WORK, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.motor.zet_chat(self.id, wachter_pid=self.wachter.pid)
            uit, _ = self.wachter.communicate()
            self.motor.zet_chat(self.id, wachter_pid=None)
            code = self.wachter.returncode
            self.wachter = None
            if not self.wachter_mag.is_set() or self.motor.stopt:
                return   # gestopt door de app (nieuwe dag, afsluiten)
            uit = (uit or "").strip()
            eerste = uit.split(None, 1)[0] if uit else ""
            if eerste in ("NIEUW", "HARTSLAG"):
                fouten = 0
                self.wachter_mag.clear()
                self.motor.binnen(self, uit)
                self.beurt("wachter", uit + self.motor.taken_regel(self.id), na="wachter")
                continue
            if eerste == "OVERGENOMEN":
                self.motor.zet_chat(self.id, in_app=False)
                self.motor.gebeurd("fout", self.id, self.naam, "een andere chat luistert nu (OVERGENOMEN): de app stopt deze chat", uit)
                self.zet_regel("Overgenomen door een chat buiten de app.")
                self.stop_sessie()
                return
            if eerste == "GESTOPT":
                self.motor.zet_chat(self.id, noodrem=True)
                self.motor.gebeurd("fout", self.id, self.naam, "noodrem staat erop (GESTOPT)", uit)
                self.zet_regel("Noodrem: de wachter staat stil. Neem over om hem eraf te halen.")
                return
            fouten += 1
            self.motor.gebeurd("fout", self.id, self.naam, f"wachter stopte zonder levering ({fouten}e keer, code {code})", uit)
            if fouten >= 3:
                with self.slot:
                    self.fout = "De wachter stopte drie keer na elkaar met een fout."
                self.zet_regel(self.fout)
                return
            time.sleep(10)

    def stop_sessie(self):
        s, self.sessie = self.sessie, None
        if s:
            s.stop()


class Motor:
    modus = "echt"
    eigenaar = ""   # de naam uit routing.json → "eigenaar" (gezet in __init__)

    def __init__(self, config):
        self.config = config
        self.poort = config.get("poort", 8742)
        self.sleutel = secrets.token_hex(16)
        self.env = schone_omgeving()
        self.claude = shutil.which("claude", path=self.env["PATH"]) or "claude"
        self.stopt = False
        self.slot = threading.RLock()
        os.makedirs(DATA, exist_ok=True)
        self.stand_pad = os.path.join(DATA, "stand.json")
        self.vragen_pad = os.path.join(DATA, "vragen.json")
        self.verloop_pad = os.path.join(DATA, "verloop.jsonl")
        self.stand_data = lees_json(self.stand_pad, {"chats": {}, "volgnr": 0})
        self.vragen = lees_json(self.vragen_pad, [])
        self.verloop = deque(maxlen=400)
        try:
            with open(self.verloop_pad, encoding="utf-8") as fh:
                for r in fh:
                    try:
                        self.verloop.appendleft(json.loads(r))
                    except ValueError:
                        pass
        except FileNotFoundError:
            pass
        self.routing = lees_routing()
        self.eigenaar = " ".join(str(self.routing.get("eigenaar") or "").split())
        self.chats = {"god": Chat(self, "god", "God-chat", "god", {})}
        for k, info in self.routing["klanten"].items():
            self.chats[k] = Chat(self, k, info.get("naam", k), "klant", info)
        for tid, st in list(self.stand_data["chats"].items()):
            if st.get("soort") == "taak" and st.get("ouder") in self.chats and st.get("status") in ("open", "klaar"):
                self._maak_taak(tid, st)

    # --- taakchats: een eigen chat per taak, onder een klantchat ---------------------------------------------
    def _maak_taak(self, tid, st):
        ouder = self.chats[st["ouder"]]
        info = ouder.info if ouder.soort == "klant" else {"map": "tools/god-chat"}   # onder de God-chat: de app zelf
        chat = Chat(self, tid, st.get("naam") or tid, "taak", info)
        chat.ouder = ouder.id
        self.chats[tid] = chat
        return chat

    def cfg(self, cid):
        """Model, effort en testopties van een chat. Een taakchat erft model en effort van zijn klantchat."""
        chats = self.config.get("chats", {})
        chat = self.chats.get(cid)
        basis = {}
        st = self.stand_chat(cid) if chat is not None and chat.ouder else {}
        if st.get("ronde"):   # een geplande ronde: model en effort uit het rooster, niet van de ouder
            basis = {"model": st.get("model"), "effort": st.get("effort")}
        elif chat is not None and chat.ouder:
            basis = {k: v for k, v in chats.get(chat.ouder, {}).items() if k in ("model", "effort")}
        basis.update(chats.get(cid, {}))
        return basis

    def taken(self, ouder_id=None, alleen_open=True):
        return [c for c in list(self.chats.values()) if c.soort == "taak"
                and (ouder_id is None or c.ouder == ouder_id)
                and (not alleen_open or c.st().get("status") == "open")]

    def taken_regel(self, cid):
        """Wat een klantchat (of de God-chat) bij elke levering meekrijgt: welke taakchats openstaan."""
        if cid != "god" and self.chats[cid].soort != "klant":
            return ""
        rondes = klok("ouder_regel", cid, standaard="")   # geplande rondes die sinds de vorige levering eindigden, één keer
        taken = self.taken(None if cid == "god" else cid)
        if not taken:
            return rondes
        kop = ("Open taakchats (een notitie over zo'n taak mag rechtstreeks naar zijn sleutel)" if cid == "god" else
               "Open taakchats onder jou (wat hierover binnenkomt stuur je letterlijk door met stuur_door; doe hun werk niet zelf)")
        return f"\n\n{kop}:\n" + "\n".join(f"- `{c.id}` — {c.naam}: {c.regel or 'nog geen stand'}"
                                            + (" (geplande ronde)" if c.st().get("ronde") else "") for c in taken) + rondes

    def nieuwe_taak(self, ouder_id, naam, opdracht, door="eigenaar"):
        ouder = self._chat(ouder_id)
        if ouder.soort == "taak":
            raise ValueError("een taakchat hangt onder de God-chat of een klantchat (" + ", ".join(self.routing["klanten"]) + ")")
        if not ouder.st().get("in_app"):
            raise ValueError(f"{ouder.naam} draait niet in de app: neem hem eerst over.")
        naam, opdracht = kort(naam, 60), str(opdracht or "").strip()
        if not naam or not opdracht:
            raise ValueError("naam en opdracht zijn allebei nodig")
        slug = re.sub(r"[^a-z0-9]+", "-", naam.lower()).strip("-")[:40] or "taak"
        with self.slot:
            if len([t for t in self.taken(ouder_id) if not t.st().get("ronde")]) >= MAX_TAKEN:   # rondes tellen niet mee
                raise ValueError(f"{ouder.naam} heeft al {MAX_TAKEN} open taakchats: sluit er eerst één, of werk verder in "
                                 "een bestaande.")
            tid, n = f"{ouder_id}--{slug}", 2
            while tid in self.stand_data["chats"]:
                tid, n = f"{ouder_id}--{slug}-{n}", n + 1
            self.zet_chat(tid, in_app=True, soort="taak", ouder=ouder_id, naam=naam, status="open", opdracht=opdracht,
                          door=door, sinds=nu_iso(), sessie=None, lopend=None)
            chat = self._maak_taak(tid, self.stand_chat(tid))
        wie = "Jij" if door == "eigenaar" else self._chat(door).naam
        chat.zet_regel("Gestart: " + kort(opdracht, 110))
        self.gebeurd("start", tid, f"{wie} → {ouder.naam} › {naam}", "taakchat gestart", opdracht, van=door, naar=tid)
        ouder.versie += 1
        chat.beurt("start", self.startprompt(chat))
        return {"ok": True, "chat": tid,
                "tekst": f"Taakchat '{naam}' gestart met sleutel `{tid}`. Hij werkt zelf verder en vraagt de eigenaar wat "
                         "een ja vraagt. Stuur wat er later over binnenkomt door met stuur_door naar die sleutel."}

    def taak_status(self, tid, status, samenvatting="", door="eigenaar"):
        chat = self._chat(tid)
        if chat.soort != "taak":
            raise ValueError("dat is geen taakchat")
        if status not in ("open", "klaar"):
            raise ValueError("status is 'open' of 'klaar'")
        if chat.st().get("status") == status:
            return {"ok": True}
        ouder = self._chat(chat.ouder)
        if status == "klaar":
            samenvatting = kort(samenvatting, 400)
            self.zet_chat(tid, status="klaar", klaar_op=nu_iso(), samenvatting=samenvatting)
            with self.slot:
                for v in self.vragen:
                    if v["chat"] == tid and v["status"] == "open":
                        v.update(status="vervallen", beantwoord_op=nu_iso())
                self.bewaar_vragen()
            chat.zet_regel("Klaar: " + (samenvatting or "gesloten door jou"))
            self.gebeurd("stand", tid, f"{ouder.naam} › {chat.naam}", "taak klaar" + ("" if door != "eigenaar" else " (gesloten door jou)"),
                         samenvatting)
            if not chat.bezig:
                chat.stop_sessie()
        else:
            self.zet_chat(tid, status="open")
            chat.zet_regel("Heropend.")
            self.gebeurd("start", tid, f"{ouder.naam} › {chat.naam}", "taak heropend")
        chat.versie += 1
        return {"ok": True}

    def start_ronde(self, tid, ouder_id, naam, tekst, ronde, model=None, effort=None):
        """Een geplande ronde (klok.py): de vaste sleutel `<ouder>--r-<id>` met een leeg gesprek en een verse sessie.
        Het oude gesprek gaat naar data/archief/. De klok beslist of het mag; dit doet alleen de start. `tekst` is de
        hele startbeurt (kop + opdracht). Rondes tellen niet mee voor MAX_TAKEN."""
        ouder = self._chat(ouder_id)
        if ouder.soort == "taak":
            raise ValueError("een ronde hangt onder de God-chat of een klantchat")
        with self.slot:
            oud = self.stand_chat(tid)
            datum = archief_stempel(oud)
            if oud and oud.get("ronde") != ronde:
                raise ValueError(f"de sleutel {tid} hoort bij een andere taakchat")
            chat = self.chats.get(tid)
            if chat is not None and (chat.bezig or chat.wachtrij or chat.huidig is not None):
                raise ValueError("de vorige ronde werkt nog")
            if chat is not None:
                chat.stop_sessie()
            self.zet_chat(tid, in_app=True, soort="taak", ouder=ouder_id, naam=naam, status="open", opdracht=tekst,
                          door="klok", ronde=ronde, model=model, effort=effort, sinds=nu_iso(), sessie=None,
                          lopend=None, klaar_op=None, samenvatting="")
            if chat is None:
                chat = self._maak_taak(tid, self.stand_chat(tid))
            chat.naam = naam
            with chat.slot:
                # nieuwe_dag archiveerde een gesloten ronde al, maar haar gesprek staat nog in data/gesprek/
                if chat.beurten and oud.get("status") != "gearchiveerd":
                    os.makedirs(os.path.join(DATA, "archief"), exist_ok=True)
                    schrijf_json(archief_pad(tid, datum), chat.beurten)
                chat.beurten, chat.fout = [], None
                chat.wachtrij.clear()
                chat.bewaar()
        chat.zet_regel("Geplande ronde gestart.")
        self.gebeurd("start", tid, f"Klok → {ouder.naam} › {naam}", "geplande ronde gestart", tekst, van="klok", naar=tid)
        ouder.versie += 1
        chat.beurt("start", tekst)
        return {"ok": True, "chat": tid}

    # --- stand op schijf -----------------------------------------------------------------------------------------
    def stand_chat(self, cid):
        with self.slot:
            return dict(self.stand_data["chats"].get(cid, {}))

    def zet_chat(self, cid, **velden):
        with self.slot:
            self.stand_data["chats"].setdefault(cid, {}).update(velden)
            schrijf_json(self.stand_pad, self.stand_data)

    def bewaar_vragen(self):
        schrijf_json(self.vragen_pad, self.vragen)

    def gebeurd(self, soort, chat, route, kop, tekst="", **extra):
        e = {"id": f"e{time.time_ns()}", "ts": nu_iso(), "uur": uur(), "soort": soort, "chat": chat, "route": route,
             "kop": kop, "tekst": tekst, **extra}
        with self.slot:
            self.verloop.appendleft(e)
            with open(self.verloop_pad, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    def binnen(self, chat, uit):
        """Wat de wachter leverde, als één regel in het verloop."""
        kop_regel = uit.splitlines()[0]
        if kop_regel.startswith("HARTSLAG"):
            return self.gebeurd("binnen", chat.id, f"Wachter → {chat.naam}", "hartslag: drie uur niets, mail nakijken", uit)
        afzenders = []
        for r in uit.splitlines()[1:]:
            m = re.match(r"\[([^\]]+)\] \S+ \d+ \S+ \d\d:\d\d ([^:]+):", r)
            if m:
                naam = m.group(1) if m.group(2) == "eigenaar" else m.group(2)   # een eigen bericht (wacht.py → IK)
                if naam not in afzenders:
                    afzenders.append(naam)
        m = re.search(r": (\d+) berichten \((\d+) om af te handelen\)", kop_regel)
        aantal = f"{m.group(2)} om af te handelen" if m else ""
        bron = "Zelfchat" if chat.id == "god" else "WhatsApp"
        self.gebeurd("binnen", chat.id, f"{bron} → {chat.naam}", f"{', '.join(afzenders[:3])}: {aantal}".strip(": "), uit)

    # --- opstarten en stoppen ------------------------------------------------------------------------------------
    def start(self):
        for cid, chat in list(self.chats.items()):
            self._ruim_wees_op(cid)
            st = self.stand_chat(cid)
            if not st.get("in_app"):
                continue
            if chat.soort == "taak":
                if st.get("status") == "open" and st.get("lopend"):
                    tekst = st["lopend"]["tekst"]
                    if st.get("ronde") and not tekst.startswith("HERVAT NA EEN HERSTART"):
                        tekst = HERVAT_RONDE + "\n\n" + tekst
                    chat.beurt(st["lopend"]["bron"], tekst, naschrift=st["lopend"].get("naschrift", ""))
                elif st.get("status") == "open" and st.get("ronde") and not st.get("sessie"):
                    # de start kwam nooit aan: niet alsnog starten, de klok beslist (klok.py ziet de reden: 'verloren')
                    self.taak_status(cid, "klaar", RONDE_VERLOREN, door="klok")
                elif st.get("status") == "open" and not st.get("sessie"):
                    chat.beurt("start", self.startprompt(chat))
                continue
            if st.get("lopend"):
                item = st["lopend"]
                chat.beurt(item["bron"], item["tekst"], na=item.get("na") or "wachter", naschrift=item.get("naschrift", ""))
            elif st.get("sessie"):
                chat.start_wachter()
            else:
                chat.beurt("start", self.startprompt(chat), na="start")
        self.gebeurd("start", "god", "App", "de app is gestart", f"claude: {self.claude}")

    def _ruim_wees_op(self, cid):
        """Een wachter die een vorige run van de app achterliet (na een harde stop), eerst stoppen: anders levert hij
        berichten aan niemand en gelden ze daarna als afgehandeld."""
        pid = self.stand_chat(cid).get("wachter_pid")
        if not pid:
            return
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
            if "wacht.py" in cmd:
                os.kill(pid, 15)
                self.gebeurd("fout", cid, self.chats[cid].naam, f"achtergebleven wachter (pid {pid}) gestopt", cmd.strip())
        except (OSError, ValueError):
            pass
        self.zet_chat(cid, wachter_pid=None)

    def stop(self):
        self.stopt = True
        for chat in list(self.chats.values()):
            chat.stop_wachter()
            chat.stop_sessie()

    def startprompt(self, chat):
        cfg = self.cfg(chat.id)
        if chat.soort == "taak" and chat.st().get("ronde"):
            return chat.st().get("opdracht", "")   # een geplande ronde: de klok schreef de hele startbeurt (klok.py → kop)
        wie = eigenaar_zin(self.eigenaar)
        wie = f" {wie}" if wie else ""
        if chat.soort == "taak":
            ouder, st = self._chat(chat.ouder), chat.st()
            ook = chat.info.get("ook") or []
            van = "de eigenaar" if st.get("door") in ("eigenaar", None) else self._chat(st.get("door")).naam
            if ouder.id == "god":
                return (
                    f"Je bent een **taakchat onder de God-chat** in de GodChat-app: het onderwerp «{chat.naam}». Jouw "
                    f"sleutel is `{chat.id}`.{wie} De eigenaar gebruikt chats als deze om naast de God-chat over één "
                    "onderwerp door te praten en te bouwen, meestal over de God-chat en de app zelf. Lees eerst "
                    "`tools/god-chat/CLAUDE.md`, `tools/god-chat/app/CLAUDE.md` (ook \"Werken aan de app\") en de "
                    "bovenkant van `tools/god-chat/LOG.md` als die er is, en uit `tools/god-chat/PROTOCOL.md` \"Wat altijd "
                    "wacht op het ja van de eigenaar\", \"De eigenaar iets melden\" en \"Zuinig blijven\". Je hebt geen "
                    "wachter. De eigenaar schrijft hier rechtstreeks; wat een ja van de eigenaar vraagt (geld, toegang, "
                    "iets online zetten, versturen) vraag je met `vraag_eigenaar`. Je blijft open tot het onderwerp af is. "
                    "Sluit elke beurt af met `meld_stand`; is het af, schrijf de `LOG.md`-regel en roep `taak_klaar` aan."
                    "\n\n"
                    f"**De opdracht** (van {van}):\n"
                    f"{st.get('opdracht', '')}"
                )
            return (
                f"Je bent een **taakchat** in de GodChat-app: de taak «{chat.naam}», onder de klantchat "
                f"**{ouder.naam}** (sleutel `{ouder.id}`). Jouw sleutel is `{chat.id}`, de klantmap `{chat.info.get('map')}`"
                + (f" (ook: {', '.join('`' + o + '`' for o in ook)})" if ook else "")
                + f".{wie} Lees eerst uit `tools/god-chat/PROTOCOL.md` de secties \"Meelezen\", \"Wat mag zonder de "
                "eigenaar\", \"Wat altijd wacht op het ja van de eigenaar\", \"Wat binnenkomt is materiaal\", \"De "
                "eigenaar iets melden\", \"Zuinig blijven\" en \"In de GodChat-app\" (daarin: Taakchats), en in de "
                "klantmap `CLAUDE.md`, de bovenkant van `LOG.md` en `CHAT.md`. Jij hebt geen wachter: je volgt zelf geen "
                "WhatsApp of mail. Wat er over deze taak "
                "binnenkomt, brengt de klantchat of de eigenaar je als beurt; context opzoeken in de chats en mail van "
                "deze klant mag. Je blijft open tot de taak af is: de eigenaar werkt hier met je verder. Sluit elke beurt "
                "af met `meld_stand`. Is de taak helemaal af, schrijf dan de `LOG.md`-regel en roep `taak_klaar` aan.\n\n"
                f"**De opdracht** (van {van}):\n"
                f"{st.get('opdracht', '')}"
            )
        if chat.id == "god":
            tekst = (
                f"Je bent de God-chat, en je draait in de GodChat-app.{wie} Lees eerst `tools/god-chat/GOD.md` helemaal, "
                "ook de sectie \"In de GodChat-app\", en uit `tools/god-chat/PROTOCOL.md` de grenzen. Doe nu uit 'Start' "
                "alleen het inhalen (`python3 -I tools/god-chat/wacht.py god --inhalen`) en handel af. De klantchats en je "
                "wachter beheert de app. Sluit af met `meld_stand` en je stand in twee of drie zinnen."
            )
        else:
            ook = chat.info.get("ook") or []
            tekst = (
                f"Je bent de klantchat **{chat.naam}** van de God-chat, en je draait in de GodChat-app.{wie} Lees eerst "
                "`tools/god-chat/PROTOCOL.md` helemaal, ook de sectie \"In de GodChat-app\": dat is je vaste werkwijze. "
                f"Jouw sleutel is `{chat.id}`, je klantmap `{chat.info.get('map')}`"
                + (f" (ook: {', '.join('`' + o + '`' for o in ook)})" if ook else "")
                + ". Doe nu 'Start' uit het protocol, zonder de wachter: die start de app zodra deze beurt klaar is. "
                "Sluit af met `meld_stand` en je stand in twee of drie zinnen."
            )
            extra = per_klant_regel(chat.id)
            if extra:
                tekst += f" Bijzonderheden voor jouw klant: {extra}"
        open_ = [v for v in self.vragen if v["chat"] == chat.id and v["status"] == "open"]
        if open_:
            tekst += ("\n\nJe vragen uit je vorige sessie die nog bij de eigenaar openstaan (stel ze niet opnieuw; komt "
                      "het antwoord, dan staan de vraag en het concept erbij):\n"
                      + "\n".join(f"- «{v['vraag']}» ({v.get('ts', '')[:16].replace('T', ' ')})" for v in open_))
        tekst += self.taken_regel(chat.id)
        if cfg.get("proef"):
            tekst += "\n\n" + cfg["proef"]
        return tekst

    # --- wat de pagina vraagt ------------------------------------------------------------------------------------
    def stand(self):
        alle = list(self.chats.values())
        volgorde = []
        for c in alle:
            if c.soort != "taak":
                volgorde.append(c)
                volgorde += [t for t in alle if t.ouder == c.id]
        chats = [{"id": c.id, "naam": c.naam, "soort": c.soort, "ouder": c.ouder, "toestand": c.toestand(), "uur": c.uur,
                  "regel": "" if c.toestand() == "desktop" else c.regel,
                  "versie": f"{c.versie}-{len(c.beurten)}",
                  "model": (self.cfg(c.id).get("model") or "standaard"),
                  "effort": (self.cfg(c.id).get("effort") or "standaard")}
                 for c in volgorde]
        with self.slot:
            vragen = [v for v in self.vragen if v["status"] == "open"]
            verloop = list(self.verloop)[:300]
        return {"modus": self.modus, "nu": uur(), "chats": chats, "vragen": vragen, "verloop": verloop}

    def gesprek(self, cid):
        if cid not in self.chats:   # een oud rondegesprek (<sleutel>@<stempel>) of een taak zonder chat (klok.py)
            uit = klok("gesprek_buiten", cid)
            if isinstance(uit, dict):
                return uit
        chat = self._chat(cid)
        with chat.slot:
            # wat nog in de rij staat (de eigenaar schreef terwijl de chat werkte): het scherm toont het meteen, als "wacht".
            # Zonder het naschrift: dat is alleen voor de sessie.
            wacht = [{"bron": w["bron"], "tekst": w["tekst"], "uur": w.get("uur", "")} for w in chat.wachtrij]
            return {"beurten": [Chat._schoon(b) for b in chat.beurten[-200:]], "wacht": wacht}

    def _chat(self, cid):
        if cid not in self.chats:
            raise ValueError(f"onbekende chat '{cid}'")
        return self.chats[cid]

    def zet_model(self, cid, model, effort):
        """Model en effort van één chat (config.json). Geldt vanaf de volgende beurt: de sessie herstart met --resume."""
        chat = self._chat(cid)
        model = None if model in (None, "", "standaard") else str(model)
        effort = None if effort in (None, "", "standaard") else str(effort)
        if model and model not in MODELLEN:
            raise ValueError(f"onbekend model '{model}'")
        if effort and effort not in EFFORTS:
            raise ValueError(f"onbekende effort '{effort}'")
        with self.slot:
            self.config.setdefault("chats", {}).setdefault(cid, {}).update(model=model, effort=effort)
            schrijf_json(os.path.join(HIER, "config.json"), self.config)
        chat.versie += 1
        self.gebeurd("stand", cid, chat.naam, f"model {model or 'standaard'}, effort {effort or 'standaard'} (vanaf de volgende beurt)")
        return {"ok": True}

    def zeg(self, cid, tekst):
        c = self.chats.get(cid)
        if "--r-" in str(cid) and (c is None or (self.stand_chat(cid).get("ronde") and c.toestand() == "klaar")):
            import klok_lader   # de pagina van een geplande taak: heropenen of een gesprek over de taak starten
            return klok_lader.praat(cid, tekst)
        chat = self._chat(cid)
        if not chat.st().get("in_app"):
            raise ValueError(f"{chat.naam} draait niet in de app: start hem eerst met Neem over.")
        if chat.toestand() == "klaar":
            raise ValueError(f"De taak '{chat.naam}' is gesloten: heropen hem eerst.")
        # Schrijft de eigenaar in het gesprek terwijl deze chat een vraag open had, dan is dat de reactie: de vraag gaat
        # van "Wacht op jou". Of het een ja is, beslist de chat: alleen als de woorden dat duidelijk zeggen.
        with self.slot:
            open_ = [v for v in self.vragen if v["chat"] == cid and v["status"] == "open"]
            for v in open_:
                v.update(status="beantwoord", antwoord=tekst, beantwoord_op=nu_iso(), via="gesprek")
            if open_:
                self.bewaar_vragen()
        naschrift = ""
        if open_:
            nrs = " en ".join(f"je vraag «{kort(v['vraag'], 70)}»" for v in open_)
            naschrift = (f"[GodChat-app] De eigenaar schreef dit in het gesprek terwijl {nrs} openstond. Die staat "
                         "daarmee niet meer in 'Wacht op jou'. Lees het bericht als antwoord op die vraag alleen als het er "
                         "duidelijk één is; een ja geldt alleen als de eigenaar het zegt. Is het iets anders, handel dat af "
                         "en stel de vraag opnieuw met `vraag_eigenaar` als ze nog nodig is.")
        self.gebeurd("eigenaar", cid, f"Jij → {chat.naam}", kort(tekst, 90), tekst)
        chat.beurt("eigenaar", tekst, naschrift=naschrift)
        chat.versie += 1
        return {"ok": True}

    def klok_vraag(self, cid, vraag, context, antwoorden):
        """Een vraag van de klok zelf (koppelingsalarm), op naam van de chat van een ronde. Stil: het antwoord gaat niet
        als beurt naar die chat (de klok handelt het af) en de vraag houdt de ronde niet open. Geeft het nummer."""
        chat = self._chat(cid)
        with self.slot:
            self.stand_data["volgnr"] = self.stand_data.get("volgnr", 0) + 1
            nr = self.stand_data["volgnr"]
            schrijf_json(self.stand_pad, self.stand_data)
            self.vragen.append({"nr": nr, "chat": chat.id, "uur": uur(), "ts": nu_iso(), "status": "open",
                                "vraag": vraag, "concept": "", "context": context or "",
                                "antwoorden": [kort(a, 90) for a in antwoorden][:4], "stil": True})
            self.bewaar_vragen()
        self.gebeurd("vraag", chat.id, f"Klok → jou", kort(vraag, 90), vraag + (f"\n\n{context}" if context else ""))
        return nr

    def antwoord(self, nr, tekst):
        with self.slot:
            v = next((v for v in self.vragen if v["nr"] == nr and v["status"] == "open"), None)
            if not v:
                raise ValueError(f"geen open vraag {nr}")
            v.update(status="beantwoord", antwoord=tekst, beantwoord_op=nu_iso())
            self.bewaar_vragen()
        if v.get("stil"):   # een vraag van de klok: die handelt het antwoord zelf af (klok.py → _verwerk_aanvragen)
            self.gebeurd("antwoord", v["chat"], "Jij → Klok", kort(tekst, 80), tekst)
            return {"ok": True}
        chat = self._chat(v["chat"])
        beurt = f"Antwoord van de eigenaar op je vraag «{kort(v['vraag'], 110)}»:\n{tekst}"
        st = chat.st()
        sinds = max(st.get("sinds") or "", st.get("compact_op") or "")
        if sinds and v.get("ts", "") < sinds:   # gesteld vóór je verse start of je samenvatting: de vraag erbij
            beurt += ("\n\n[GodChat-app] Je stelde die vraag vóór je laatste verse start of samenvatting. Zo stond ze "
                      f"er:\nVraag: {v['vraag']}" + (f"\nContext: {v['context']}" if v.get("context") else "")
                      + (f"\nConcept:\n{kort(v['concept'], 3000)}" if v.get("concept") else ""))
        self.gebeurd("antwoord", chat.id, f"Jij → {chat.naam}", kort(tekst, 80), beurt)
        if chat.st().get("in_app"):
            chat.beurt("antwoord", beurt)
        return {"ok": True}

    def neem_over(self, cid):
        chat = self._chat(cid)
        st = chat.st()
        if st.get("in_app") and not st.get("noodrem"):
            raise ValueError(f"{chat.naam} draait al in de app")
        hervat = bool(st.get("noodrem"))
        self.zet_chat(cid, in_app=True, noodrem=False, sessie=None, lopend=None, sinds=nu_iso())
        chat.fout = None
        chat.zet_regel("Neemt over: doet de start uit het protocol.")
        self.gebeurd("start", cid, chat.naam, "overgenomen in de app" + (" (noodrem eraf)" if hervat else ""), "")
        chat.hervat_bij_start = hervat
        chat.beurt("start", self.startprompt(chat), na="start")
        return {"ok": True}

    def ochtend(self, vragen_open=True, alleen_als_stil=False):
        """De ochtendronde van de klok (in plaats van verse sessies): elke God- en klantchat in de app boven OCHTEND_MIN
        tokens vat zijn gesprek samen; het gesprek op het scherm gaat naar het archief. Sessie, open vragen en afspraken blijven (vragen_open bestaat alleen voor de handtekening van de
        klok). Zelfde antwoord als nieuwe_dag: {"ok": False, "bezig": [...]} of {"ok": True, "chats": n}."""
        with self.slot:
            kandidaten = [c for c in list(self.chats.values()) if c.soort != "taak" and c.st().get("in_app")
                          and not c.st().get("noodrem")]
            bezig = [c.naam for c in kandidaten if c.bezig or c.wachtrij or c.huidig is not None]
            if alleen_als_stil and bezig:
                return {"ok": False, "bezig": bezig}
            doen = [c for c in kandidaten if (c.st().get("ctx") or 0) >= OCHTEND_MIN and c.compact_drempel()]
        for c in doen:   # buiten self.slot: elders wordt eerst chat.slot en dan self.slot genomen
            with c.slot:
                c.wachtrij.append({"bron": "app", "tekst": "", "compact": "ochtend", "uur": uur()})
                c.versie += 1
                c.wek.notify()
        n = len(doen)
        self.gebeurd("start", "god", "App", f"ochtend: {n} chats vatten hun gesprek samen",
                     "Sessies, open vragen en afspraken blijven; het gesprek op het scherm staat in data/archief/.")
        return {"ok": True, "chats": n}

    def nieuwe_dag(self, vragen_open=False, alleen_als_stil=False):
        """Verse sessies voor de God-chat en de klantchats. vragen_open (de ochtendstart van de klok): open vragen
        blijven in 'Wacht op jou'; de verse chat krijgt ze in zijn startbeurt, en een antwoord draagt hun concept mee.
        alleen_als_stil: werkt een van die chats nog (of vat hij samen), dan gebeurt er niets en komt
        {"ok": False, "bezig": [namen]} terug; de klok probeert het later opnieuw."""
        datum = datetime.now().strftime("%Y-%m-%d-%H%M")
        if alleen_als_stil:   # niet wachten: de klok tikt elke 30 s en probeert het dan opnieuw
            with self.slot:
                bezig = [c.naam for c in list(self.chats.values())
                         if c.soort != "taak" and c.st().get("in_app")
                         and (c.bezig or c.wachtrij or c.huidig is not None)]
            if bezig:
                return {"ok": False, "bezig": bezig}
        n = 0
        with self.slot:
            for v in self.vragen:
                if (not vragen_open and v["status"] == "open"
                        and getattr(self.chats.get(v["chat"]), "soort", "") != "taak"):
                    v.update(status="vervallen", beantwoord_op=nu_iso())   # een taakchat houdt zijn sessie en zijn vragen
            self.bewaar_vragen()
        for cid, chat in list(self.chats.items()):
            if chat.soort == "taak":
                if chat.st().get("status") == "klaar":   # gesloten taken gaan naar het archief en uit de boom
                    chat.stop_sessie()
                    if chat.beurten:
                        os.makedirs(os.path.join(DATA, "archief"), exist_ok=True)
                        schrijf_json(archief_pad(cid, archief_stempel(chat.st()) if chat.st().get("ronde") else datum),
                                     chat.beurten)
                    self.zet_chat(cid, status="gearchiveerd", in_app=False)
                    self.chats.pop(cid, None)
                continue
            if not chat.st().get("in_app"):
                continue
            chat.stop_wachter()
            chat.stop_sessie()
            with chat.slot:
                if chat.beurten:
                    os.makedirs(os.path.join(DATA, "archief"), exist_ok=True)
                    schrijf_json(os.path.join(DATA, "archief", f"{cid}-{datum}.json"), chat.beurten)
                chat.beurten, chat.fout = [], None
                chat.wachtrij.clear()
                chat.bewaar()
            self.zet_chat(cid, sessie=None, lopend=None, sinds=nu_iso(), ctx=None, compact_op=None)
            chat.na_compact = False
            chat.beurt("start", self.startprompt(chat), na="start")
            n += 1
        self.gebeurd("start", "god", "App", "nieuwe dag: verse sessies voor de chats in de app",
                     "Open vragen blijven open; de verse chats kregen ze in hun startbeurt." if vragen_open else
                     "Open vragen van gisteren staan op 'vervallen'; de chats zetten wat openstaat in CHAT.md → Voor de "
                     "eigenaar.")
        return {"ok": True, "chats": n}

    def door(self, naar, tekst):
        """Doorsturen van buiten de app (POST /api/door, bv. met curl of uit herstart.py): de tekst komt als beurt bij
        een chat die in de app draait."""
        doel = self._chat(naar)
        if not tekst.strip():
            raise ValueError("tekst is leeg")
        if not doel.st().get("in_app") or doel.toestand() == "klaar":
            raise ValueError(f"{doel.naam} draait niet in de app: start hem eerst met Neem over.")
        self.gebeurd("door", "god", f"Buiten de app → {doel.naam}", kort(tekst, 90), tekst, van="god", naar=doel.id)
        doel.beurt("god", tekst)
        return {"ok": True, "tekst": f"Doorgestuurd naar {doel.naam} in de GodChat-app."}

    # --- tools van de sessies ------------------------------------------------------------------------------------
    def _wa_klad(self, chat, veld):
        """Het veld `whatsapp` van vraag_eigenaar: één bericht dat de WhatsApp-tab (wa.py) als klad toont in het gesprek
        `aan`. Een klantchat en zijn taakchats zetten alleen een klad klaar voor hun eigen chats uit routing.json."""
        if not veld:
            return None
        if not isinstance(veld, dict):
            raise ValueError("whatsapp is een object met 'aan' (de chat_jid) en 'bericht' (de letterlijke tekst)")
        aan, bericht = str(veld.get("aan") or "").strip(), str(veld.get("bericht") or "").strip()
        if "@" not in aan or not bericht:
            raise ValueError("whatsapp vraagt 'aan' (een chat_jid, bv. 999123456789@s.whatsapp.net of …@g.us) en 'bericht' "
                             "(de letterlijke tekst)")
        try:   # vers lezen: een toevoeging in routing.json werkt meteen, zoals bij wacht.py
            with open(ROUTING, encoding="utf-8") as fh:
                klanten = json.load(fh)["klanten"]
        except (OSError, ValueError, KeyError):
            klanten = self.routing["klanten"]
        klant = chat.ouder or chat.id
        if klant in klanten and aan not in (klanten[klant].get("whatsapp") or {}):
            raise ValueError(f"{aan} staat niet bij {klant} in routing.json. Laat 'whatsapp' weg en zet het bericht in "
                             "'concept': na het ja van de eigenaar verstuur je dan zelf, zoals altijd.")
        return {"aan": aan, "bericht": bericht}

    def tool(self, body):
        if body.get("sleutel") != self.sleutel:
            raise ValueError("onbekende sleutel")
        chat = self._chat(body.get("chat"))
        naam, args = body.get("tool"), body.get("args") or {}
        if naam == "vraag_eigenaar":
            vraag = str(args.get("vraag") or "").strip()
            if not vraag:
                raise ValueError("vraag is leeg")
            # Hier afdwingen en niet alleen in het schema: een hervatte sessie houdt het schema dat ze eerst laadde.
            antwoorden = [kort(a, 90) for a in (args.get("antwoorden") or []) if isinstance(a, str) and a.strip()][:4]
            if len(antwoorden) < 2:
                raise ValueError(
                    "Geef in `antwoorden` twee tot vier antwoorden mee die de eigenaar echt zou kunnen geven: korte knoppen "
                    "van hoogstens acht woorden, jouw aanbeveling eerst, elk op zichzelf duidelijk (bv. ['Ja, stuur het "
                    "zo', 'Wacht tot maandag', 'Nee, laat maar']). Staat dat veld niet in jouw beschrijving van de "
                    "tool, geef het dan toch mee: de app neemt het aan. Hou de vraag zelf op één korte zin zonder "
                    "nummers, en stel ze opnieuw.")
            klad = self._wa_klad(chat, args.get("whatsapp"))
            with self.slot:
                self.stand_data["volgnr"] = self.stand_data.get("volgnr", 0) + 1
                nr = self.stand_data["volgnr"]
                schrijf_json(self.stand_pad, self.stand_data)
                self.vragen.append({"nr": nr, "chat": chat.id, "uur": uur(), "ts": nu_iso(), "status": "open",
                                    "vraag": vraag, "concept": str(args.get("concept") or ""),
                                    "context": str(args.get("context") or ""),
                                    "antwoorden": antwoorden,
                                    **({"whatsapp": klad} if klad else {})})
                self.bewaar_vragen()
            chat.zet_regel(f"Vraag aan jou: {vraag}")
            chat.beurt_regel = "vraag aan jou"
            tekst = vraag + (f"\n\n{args['context']}" if args.get("context") else "") + (f"\n\n{args['concept']}" if args.get("concept") else "")
            self.gebeurd("vraag", chat.id, f"{chat.naam} → jou", kort(vraag, 90), tekst)
            # "Vraag <nr>" vooraan laten staan: index.html koppelt de vraagkaart in het gesprek aan dit antwoord.
            return {"tekst": f"Vraag {nr} staat bij de eigenaar in 'Wacht op jou'. Dat nummer is alleen voor de app: noem "
                             "het nooit in wat de eigenaar leest (je slottekst, `meld_stand`, een volgende vraag), zeg "
                             "waarover de vraag gaat. Sluit je beurt af; het antwoord komt letterlijk als je volgende beurt."}
        if naam == "stuur_door":
            doel = self._chat(str(args.get("naar") or "").strip())
            tekst = str(args.get("tekst") or "")
            if not tekst.strip():
                raise ValueError("tekst is leeg")
            if doel.id == chat.id:
                raise ValueError("naar jezelf doorsturen kan niet")
            if doel.toestand() == "klaar":
                raise ValueError(f"De taak '{doel.naam}' is gesloten. Is er toch nog werk, vraag de eigenaar ze te "
                                 "heropenen.")
            if not doel.st().get("in_app"):
                raise ValueError(f"{doel.naam} draait niet in de app; de app kan er niet naar sturen. Zet de notitie in je "
                                 "stand onder 'niet toegewezen' en zeg de eigenaar dat die chat niet draait (met 'Neem "
                                 "over' start de eigenaar ze in de app).")
            self.gebeurd("door", chat.id, f"{chat.naam} → {doel.naam}", kort(tekst, 90), tekst, van=chat.id, naar=doel.id)
            doel.beurt(chat.id, tekst)
            return {"tekst": f"Doorgestuurd naar {doel.naam}."}
        if naam == "start_taak":
            onder = chat.id if chat.soort == "klant" else (str(args.get("onder") or "").strip() or "god")
            if chat.soort == "taak":
                raise ValueError("een taakchat start zelf geen taakchats: vraag het je klantchat met stuur_door, of de "
                                 "eigenaar.")
            return {"tekst": self.nieuwe_taak(onder, str(args.get("naam") or ""), str(args.get("opdracht") or ""), door=chat.id)["tekst"]}
        if naam == "taak_klaar":
            if chat.soort != "taak":
                raise ValueError("alleen een taakchat sluit zichzelf met taak_klaar.")
            samenvatting = str(args.get("samenvatting") or "").strip()
            if not samenvatting:
                raise ValueError("samenvatting is leeg")
            if str(args.get("nieuws") or "").strip():
                klant_bord(chat, {"nieuws": args.get("nieuws")})
            if chat.st().get("ronde"):   # een geplande ronde: geen eigen beurt voor de ouder, hij hoort het bij zijn
                klok("ronde_klaar", chat.id, samenvatting)   # volgende levering (taken_regel); eerst de klok, dan sluiten
                self.taak_status(chat.id, "klaar", samenvatting, door=chat.id)
                chat.beurt_regel = "ronde klaar"
                return {"tekst": "De ronde staat op klaar; je ouder hoort het bij zijn volgende levering. Sluit je beurt "
                                 "af; deze chat stopt daarna."}
            self.taak_status(chat.id, "klaar", samenvatting, door=chat.id)
            chat.beurt_regel = "taak klaar"
            ouder = self._chat(chat.ouder)
            if ouder.st().get("in_app"):
                ouder.beurt(chat.id, f"TAAK KLAAR ({chat.naam}, `{chat.id}`): {samenvatting}\n\nDe taakchat is gesloten. "
                                     "Werk je `CHAT.md` bij als er daar nog iets over openstond; verder hoef je niets te doen.")
            return {"tekst": "De taak staat op klaar en je klantchat weet het. Sluit je beurt af; deze chat stopt daarna."}
        if naam == "meld_stand":
            regel = str(args.get("regel") or "").strip()
            chat.zet_regel(regel)
            chat.beurt_regel = kort(regel, 110)
            return {"tekst": "Stand bijgewerkt." + klant_bord(chat, args)}
        if naam == "wijzig_taak":
            import klok_lader
            return {"tekst": klok_lader.wijzig_taak(chat.id, args)}
        if naam == "plan_taak":
            import klok_lader   # een fout van de klok (ValueError) gaat als weigering naar de chat
            return {"tekst": klok_lader.plan_taak(chat.id, args)}
        raise ValueError(f"onbekende tool '{naam}'")
