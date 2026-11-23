#!/usr/bin/env python3
"""God-chat: wacht tot er WhatsApp-berichten zijn voor één chat van de God-chat, druk ze af en stop.

Kost geen tokens: leest alleen de lokale database van de WhatsApp-bridge (alleen-lezen) en houdt zijn eigen
stand bij in state/. Draai met `python3 -I`.

  wacht.py <klant>              blokkeert tot er iets nieuws is (of tot --max-wacht), drukt het af en stopt
  wacht.py <klant> --inhalen    drukt meteen af wat nog niet afgehandeld is en stopt (begin van een verse chat)
  wacht.py --status             wie luistert, hoe lang geleden, hoeveel er nog ligt
  wacht.py --proef <klant>      wat de laatste --uren binnenkwam en hoe vaak dat gewekt had; verandert niets
  wacht.py --stop <klant|alles> noodrem: de wachter van die chat stopt en mag niet opnieuw starten
  wacht.py <klant> --hervat     haalt de noodrem eraf en luistert weer

<klant> is een sleutel onder `klanten` in routing.json, of `god` (de zelfchat van de eigenaar).
De eerste regel van de uitvoer is NIEUW, NIETS, HARTSLAG, OVERGENOMEN, GESTOPT of FOUT (zie PROTOCOL.md).

De store van de bridge: de omgeving GODCHAT_STORE, anders `store` in routing.json, anders
~/whatsapp-mcp/whatsapp-bridge/store.

Wanneer wekt een bericht? Een bericht waar de eigenaar nog niet op antwoordde wekt na 2 minuten stilte, zodat een
reeks berichten in één keer aankomt. Schreef de eigenaar zelf mee in het laatste half uur, dan wekt het gesprek
5 minuten nadat het stilviel. De zelfchat wekt na 90 seconden. In een levering heet een eigen bericht "eigenaar".
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from urllib.parse import quote

HIER = os.path.dirname(os.path.abspath(__file__))
STANDAARD_STORE = "~/whatsapp-mcp/whatsapp-bridge/store"
STORE = os.path.expanduser(STANDAARD_STORE)   # main() zet de echte: GODCHAT_STORE, `store` in routing.json, of dit
IK = "eigenaar"   # zo heet een eigen bericht (van de eigenaar) in een levering
POLL = 5          # seconden tussen twee blikken op de database
STIL = 90         # de zelfchat: zo lang moet het stil zijn voor een reeks notities als af geldt
MAX_REEKS = 480   # en langer wachten we daar nooit op het einde van een reeks
STIL_KLANT = 120  # een bericht waar de eigenaar nog niet op antwoordde wekt na 2 minuten stilte
MAX_KLANT = 600   # en nooit later dan 10 minuten na het eerste bericht dat nog ligt
LIVE = 1800       # schreef de eigenaar zelf mee in de laatste 30 minuten, dan loopt het gesprek nog
STIL_LIVE = 300   # een gesprek waarin de eigenaar meeschrijft wekt 5 minuten nadat het stilviel, in één keer
MAX_LIVE = 1800   # en nooit later dan een half uur na het eerste bericht dat nog ligt
VERS = 2          # dagen: een ouder bericht wekt nooit (history-sync van oude berichten)
INHAAL = 7        # dagen: zo ver kijkt --inhalen terug
KNIP = 700        # tekens per bericht in de uitvoer
MAX_REGELS = 120  # regels per levering; de rest haalt de chat zelf op
SPRAAK_UIT = os.path.join(HIER, "app", "wa_spraak.py")   # schrijft één spraakbericht uit (lokaal, deelt het geheugen van de tab)
MAX_SPRAAK = 8    # spraakberichten per levering die we zelf uitschrijven (de recentste); de rest doet de chat
KNIP_SPRAAK = 4000  # tekens per uitgeschreven spraakbericht
DAG_VAN, DAG_TOT = (7, 30), (22, 0)   # alleen binnen deze uren stopt de wachter met een HARTSLAG
BOT_BEGIN = ("[God]", "▶")    # regels in de zelfchat die van een bot komen, geen notitie van de eigenaar
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
KOLOMMEN = "rowid, id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename"
MEDIA = {"audio": "[spraakbericht]", "image": "[foto]", "video": "[video]", "document": "[document]",
         "sticker": "[sticker]"}
KLOK = 0.0        # alleen voor tests: verschuiving van de klok in seconden


def nu_s():
    return time.time() + KLOK


def routing():
    with open(os.path.join(HIER, "routing.json"), encoding="utf-8") as fh:
        return json.load(fh)


def store_pad(r):
    """De store van de bridge: GODCHAT_STORE, anders `store` in routing.json, anders de standaard."""
    return os.path.expanduser(str(os.environ.get("GODCHAT_STORE") or (r or {}).get("store") or STANDAARD_STORE))


def chats_van(r, klant):
    """jid -> naam van elke WhatsApp-chat die bij deze chat hoort."""
    if klant == "god":
        return {r["zelf"]: "zelfchat"}
    k = r["klanten"].get(klant)
    if k is None:
        print(f"FOUT onbekende klant '{klant}'. Bekend: god, " + ", ".join(r["klanten"]))
        sys.exit(1)
    return dict(k.get("whatsapp", {}))


def db(naam="messages.db"):
    return sqlite3.connect(f"file:{quote(os.path.join(STORE, naam))}?mode=ro", uri=True, timeout=10)   # het pad is instelbaar


def namen():
    """nummer -> naam uit het adresboek van de bridge."""
    out = {}
    try:
        con = db("whatsapp.db")
        for jid, full, push in con.execute("select their_jid, full_name, push_name from whatsmeow_contacts"):
            naam = (full or push or "").strip()
            if naam:
                out[jid.split("@")[0]] = naam
        con.close()
    except sqlite3.Error:
        pass
    return out


def rijen(con, jids, na_rowid, dagen):
    if not jids:
        return []
    vraag = (f"select {KOLOMMEN} from messages where rowid > ? and chat_jid in ({','.join('?' * len(jids))}) "
             "and julianday(timestamp) > julianday('now', ?) order by rowid")
    return list(con.execute(vraag, [na_rowid, *jids, f"-{dagen} days"]))


def tijd(ts):
    """Tijdstempel van een bericht als seconden sinds 1970."""
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        return nu_s()


def van_bot(rij):
    return (rij[4] or "").lstrip().startswith(BOT_BEGIN)


def wekt(rij, klant):
    """Is dit een bericht waarvoor de chat wakker moet worden?"""
    _, _, _, _, content, _, van_mij, soort, _ = rij
    if soort == "reaction" or not ((content or "").strip() or soort):
        return False
    if klant == "god":
        return not van_bot(rij)
    return not van_mij


def toonbaar(rij, klant):
    if rij[7] == "reaction":
        return False
    return not (klant == "god" and van_bot(rij))


def lokaal(ts):
    try:
        d = datetime.fromisoformat(str(ts)).astimezone()
        return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]} {d:%H:%M}"
    except ValueError:
        return str(ts)[:16]


def schrijf_spraak_uit(toon):
    """De spraakberichten in deze levering uitschrijven, zodat de chat tekst leest in plaats van "[spraakbericht]".
    {bericht-id: tekst}. Wat mislukt of te lang duurt, ontbreekt: dan blijft het een spraakbericht met de gewone hint."""
    uit, einde = {}, time.time() + 300
    for x in [x for x in toon if x[7] == "audio"][-MAX_SPRAAK:]:
        if time.time() > einde:
            break
        try:
            r = subprocess.run([sys.executable, "-I", SPRAAK_UIT, x[2], x[1]], capture_output=True, text=True, timeout=240)
            if r.returncode == 0 and r.stdout.strip():
                uit[x[1]] = r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return uit


def regels(toon, jids, nm, spraak=None):
    """Eén regel per bericht; een rij foto's of documenten van dezelfde afzender wordt één regel. Een eigen bericht
    heet IK ("eigenaar")."""
    spraak = spraak or {}
    uit, i = [], 0
    while i < len(toon):
        rowid, mid, jid, sender, content, ts, van_mij, soort, bestand = toon[i]
        if van_mij:
            wie = IK
        else:
            wie = nm.get(sender or "", "+" + (sender or "?"))
        kop = f"[{jids.get(jid, jid)}] {lokaal(ts)} {wie}:"
        tekst = " ".join((content or "").split())
        j = i + 1
        if soort and not tekst:
            while (j < len(toon) and toon[j][2] == jid and toon[j][3] == sender and toon[j][7] == soort
                   and not (toon[j][4] or "").strip() and soort not in ("document", "audio")):
                j += 1
        if j - i >= 3:
            ids = ", ".join(x[1] for x in toon[i:j])
            uit.append(f"{kop} {j - i}x {MEDIA.get(soort, f'[{soort}]')}  (ids {ids})")
            i = j
            continue
        if len(tekst) > KNIP:
            tekst = f"{tekst[:KNIP]}… (+{len(tekst) - KNIP} tekens)"
        if soort == "audio" and mid in spraak:
            gezegd = spraak[mid]
            if len(gezegd) > KNIP_SPRAAK:
                gezegd = f"{gezegd[:KNIP_SPRAAK]}… (+{len(gezegd) - KNIP_SPRAAK} tekens)"
            tekst = f"[spraakbericht, uitgeschreven] {gezegd} {tekst}".strip()
        elif soort:
            label = f"[document {bestand}]" if soort == "document" and bestand else MEDIA.get(soort, f"[{soort}]")
            tekst = f"{label} {tekst}".strip()
        uit.append(f"{kop} {tekst}  (id {mid})")
        i += 1
    return uit


def druk_af(kop, klant, rs, jids):
    toon = [x for x in rs if toonbaar(x, klant)]
    te_doen = sum(1 for x in toon if wekt(x, klant))
    betrokken = sorted({x[2] for x in toon})
    print(f"{kop} {klant}: {len(toon)} berichten ({te_doen} om af te handelen) in {len(betrokken)} chats, "
          f"laatste {lokaal(toon[-1][5])}")
    spraak = schrijf_spraak_uit(toon)
    uit = regels(toon, jids, namen(), spraak)
    if len(uit) > MAX_REGELS:
        print(f"… {len(uit) - MAX_REGELS} eerdere regels weggelaten; haal de draad op met list_messages.")
        uit = uit[-MAX_REGELS:]
    print("\n".join(uit))
    print("chat_jid: " + "; ".join(f"{jids.get(j, j)} = {j}" for j in betrokken))
    if any(x[7] and not (x[7] == "audio" and x[1] in spraak) for x in toon):
        print("Media: mcp__whatsapp__download_media met het id en de chat_jid; een spraakbericht dat hierboven niet "
              "uitgeschreven staat daarna met tools/god-chat/spraak.py.")
    if spraak:
        print("Spraakberichten zijn automatisch uitgeschreven (namen en cijfers kunnen fout gehoord zijn: bij twijfel "
              "het origineel beluisteren of navragen).")


def stand_pad(sd, klant):
    return os.path.join(sd, f"{klant}.json")


def lees_stand(sd, klant):
    try:
        with open(stand_pad(sd, klant), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def schrijf_stand(sd, klant, st):
    tmp = stand_pad(sd, klant) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, stand_pad(sd, klant))


def nieuwe_stand(con, klant):
    """Eerste start: een klantchat haalt de laatste 24 uur in, de God-chat begint bij nu."""
    if klant == "god":
        basis = con.execute("select coalesce(max(rowid), 0) from messages").fetchone()[0]
    else:
        basis = con.execute("select coalesce(max(rowid), 0) from messages "
                            "where julianday(timestamp) <= julianday('now', '-1 days')").fetchone()[0]
    return {"basis": basis, "geleverd": {}, "afgehandeld": {}}


def open_rijen(con, jids, st, soort, dagen):
    """Per chat de rijen na de cursor `geleverd` of `afgehandeld`."""
    if not jids:
        return {}
    cursor = {j: st[soort].get(j, st["basis"]) for j in jids}
    per = {}
    for x in rijen(con, list(jids), min(cursor.values()), dagen):
        if x[0] > cursor[x[2]]:
            per.setdefault(x[2], []).append(x)
    return per


def loopt_nog(con, jid, t_laatste):
    """Schreef de eigenaar zelf in deze chat in het half uur voor het laatste bericht? (een eigen reactie telt ook)"""
    jd = con.execute("select max(julianday(timestamp)) from messages where chat_jid = ? and is_from_me = 1",
                     [jid]).fetchone()[0]
    return jd is not None and t_laatste - (jd - 2440587.5) * 86400 <= LIVE


def drempel(klant, live, a):
    """(stilte, plafond) in seconden: hoe lang een chat stil moet zijn voor hij wekt, en hoe lang we hoogstens wachten."""
    if live:
        return STIL_LIVE, MAX_LIVE
    if klant == "god":
        return a.stil or STIL, MAX_REEKS
    return a.stil or STIL_KLANT, MAX_KLANT


def nu():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def overdag():
    t = datetime.now()
    return DAG_VAN <= (t.hour, t.minute) < DAG_TOT


def inhalen(klant, a):
    jids = chats_van(routing(), klant)
    con = db()
    st = lees_stand(a.state, klant) or nieuwe_stand(con, klant)
    per = open_rijen(con, jids, st, "afgehandeld", INHAAL)
    toon = [x for rs in per.values() if any(wekt(y, klant) for y in rs) for x in rs]
    if toon:
        druk_af("NIEUW (inhalen)", klant, sorted(toon), jids)
    else:
        print(f"NIETS {klant}: niets onafgehandeld.")
    for jid, rs in per.items():
        st["geleverd"][jid] = max(rs[-1][0], st["geleverd"].get(jid, 0))
    schrijf_stand(a.state, klant, st)
    return 0


def wacht(klant, a):
    con = db()
    st = lees_stand(a.state, klant) or nieuwe_stand(con, klant)
    con.close()
    if st.get("eigenaar") == "gestopt" and not a.hervat:
        print(f"GESTOPT {klant}: de noodrem staat erop. Start de wachter niet opnieuw en sluit je beurt af. "
              "Alleen de eigenaar haalt hem eraf (wacht.py <klant> --hervat).")
        return 0
    token = uuid.uuid4().hex[:12]
    st["afgehandeld"] = dict(st["geleverd"])   # opnieuw starten betekent: de vorige levering is afgehandeld
    st["eigenaar"], st["luistert_sinds"] = token, nu()
    schrijf_stand(a.state, klant, st)
    begin = time.time()
    fouten = 0
    while True:
        with open(os.path.join(a.state, f"{klant}.hartslag"), "w", encoding="utf-8") as fh:
            fh.write(nu())
        eigenaar = (lees_stand(a.state, klant) or {}).get("eigenaar")
        if eigenaar == "gestopt":
            print(f"GESTOPT {klant}: de noodrem staat erop. Start de wachter niet opnieuw en sluit je beurt af.")
            return 0
        if eigenaar != token:
            print(f"OVERGENOMEN {klant}: een nieuwere chat luistert nu. Start de wachter niet opnieuw en sluit je beurt af.")
            return 0
        ligt_er_iets = False
        try:
            jids = chats_van(routing(), klant)   # elke blik opnieuw: een toevoeging werkt meteen
            con = db()
            klaar = {}
            for jid, rs in open_rijen(con, jids, st, "geleverd", a.vers or VERS).items():
                wekkers = [x for x in rs if wekt(x, klant)]
                if not wekkers:
                    continue
                ligt_er_iets = True
                t_laatste = tijd(rs[-1][5])
                live = klant != "god" and loopt_nog(con, jid, t_laatste)
                stil, plafond = drempel(klant, live, a)
                if nu_s() - t_laatste >= stil or nu_s() - tijd(wekkers[0][5]) >= plafond:
                    klaar[jid] = rs
            if klaar:
                druk_af("NIEUW", klant, sorted(x for rs in klaar.values() for x in rs), jids)
                for jid, rs in klaar.items():
                    st["geleverd"][jid] = rs[-1][0]
                st["laatste_levering"] = nu()
                schrijf_stand(a.state, klant, st)
                return 0
            con.close()
            fouten = 0
        except (sqlite3.Error, OSError, ValueError) as fout:
            fouten += 1
            if fouten >= 12:
                print(f"FOUT {klant}: {type(fout).__name__}: {fout}")
                return 1
        if not ligt_er_iets and time.time() - begin >= a.max_wacht and overdag():
            print(f"HARTSLAG {klant}: niets nieuws op WhatsApp sinds {st['luistert_sinds'][11:16]}.")
            return 0
        time.sleep(POLL)


def status(a):
    r = routing()
    con = db()
    print("chat            luistert   laatste teken   ligt er nog")
    for klant in ["god", *r["klanten"]]:
        st = lees_stand(a.state, klant)
        try:
            leeftijd = time.time() - os.path.getmtime(os.path.join(a.state, f"{klant}.hartslag"))
        except OSError:
            leeftijd = None
        if st is None:
            print(f"{klant:<15} nooit gestart")
            continue
        if st.get("eigenaar") == "gestopt":
            luistert = "NOODREM"
        else:
            luistert = "ja" if leeftijd is not None and leeftijd < 4 * POLL else "nee"
        teken = "-" if leeftijd is None else (f"{int(leeftijd)} s geleden" if leeftijd < 120 else f"{int(leeftijd // 60)} min geleden")
        try:
            per = open_rijen(con, chats_van(r, klant), st, "afgehandeld", INHAAL)
        except sqlite3.Error:
            per = {}
        ligt = sum(1 for rs in per.values() for x in rs if wekt(x, klant))
        print(f"{klant:<15} {luistert:<10} {teken:<15} {ligt}")
    return 0


def proef(klant, a):
    jids = chats_van(routing(), klant)
    con = db()
    if not jids:
        print(f"{klant}: geen WhatsApp-chats in routing.json.")
        return 0
    vraag = (f"select {KOLOMMEN} from messages where chat_jid in ({','.join('?' * len(jids))}) "
             "and julianday(timestamp) > julianday('now', ?) order by julianday(timestamp)")
    per = {}
    for x in con.execute(vraag, [*jids, f"-{a.uren} hours"]):
        per.setdefault(x[2], []).append(x)
    totaal = gewekt = 0
    telling = {}
    for jid, rs in per.items():
        ligt, eigen = False, None
        for i, x in enumerate(rs):
            t = tijd(x[5])
            if x[6] and klant != "god":
                eigen = t
            if wekt(x, klant):
                ligt = True
                totaal += 1
                telling[jid] = telling.get(jid, 0) + 1
            volgende = tijd(rs[i + 1][5]) if i + 1 < len(rs) else float("inf")
            live = eigen is not None and t - eigen <= LIVE
            if ligt and volgende - t >= drempel(klant, live, a)[0]:
                gewekt += 1
                ligt = False
    print(f"{klant}: {totaal} berichten in de laatste {a.uren} uur, dat had de chat {gewekt} keer gewekt.")
    for jid, n in sorted(telling.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {jids.get(jid, jid)}")
    return 0


def stop(wie, a):
    r = routing()
    con = db()
    for klant in (["god", *r["klanten"]] if wie == "alles" else [wie]):
        if klant != "god" and klant not in r["klanten"]:
            chats_van(r, klant)   # drukt de fout af en stopt
        st = lees_stand(a.state, klant) or nieuwe_stand(con, klant)
        st["eigenaar"] = "gestopt"
        schrijf_stand(a.state, klant, st)
        print(f"{klant}: noodrem erop")
    return 0


def main():
    global KLOK, STORE
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("klant", nargs="?")
    p.add_argument("--inhalen", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--proef", metavar="KLANT")
    p.add_argument("--stop", metavar="KLANT|alles")
    p.add_argument("--hervat", action="store_true", help="haalt de noodrem eraf en luistert weer")
    p.add_argument("--uren", type=int, default=24)
    p.add_argument("--max-wacht", type=int, default=10800, help="seconden tot een HARTSLAG (alleen overdag)")
    p.add_argument("--stil", type=int, default=None, help=argparse.SUPPRESS)   # alleen voor tests
    p.add_argument("--state", default=os.path.join(HIER, "state"))
    p.add_argument("--klok", help=argparse.SUPPRESS)   # alleen voor tests: doe alsof het dit tijdstip is
    p.add_argument("--vers", type=int, default=None, help=argparse.SUPPRESS)   # alleen voor tests: dagen terug dat een bericht nog wekt
    a = p.parse_args()
    if a.klok:
        KLOK = datetime.fromisoformat(a.klok).timestamp() - time.time()
    pad = os.path.join(HIER, "routing.json")
    try:
        r = routing()
    except FileNotFoundError:
        print(f"FOUT geen routing.json in {HIER}: kopieer routing.voorbeeld.json naar routing.json en vul het in.")
        return 1
    except ValueError as fout:
        print(f"FOUT {pad} is geen geldige JSON: {fout}")
        return 1
    STORE = store_pad(r)
    if not os.path.isfile(os.path.join(STORE, "messages.db")):
        print(f"FOUT geen WhatsApp-store: {os.path.join(STORE, 'messages.db')} bestaat niet. Wijs `store` in routing.json "
              "(of GODCHAT_STORE) naar de map store/ van je WhatsApp-bridge.")
        return 1
    os.makedirs(a.state, exist_ok=True)
    if a.status:
        return status(a)
    if a.proef:
        return proef(a.proef, a)
    if a.stop:
        return stop(a.stop, a)
    if not a.klant:
        p.error("geef een klant, of --status, --proef of --stop")
    if a.inhalen:
        return inhalen(a.klant, a)
    return wacht(a.klant, a)


if __name__ == "__main__":
    sys.exit(main())
