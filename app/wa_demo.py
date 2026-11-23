#!/usr/bin/env python3
"""Namaak-WhatsApp voor de WhatsApp-tab: een store met verzonnen mensen, een eigen routing.json en nep.json, en een
app-map met symlinks naar de echte bestanden. Raakt de echte store en de echte app niet.

  /opt/homebrew/bin/python3 -I tools/god-chat/app/wa_demo.py          bouwt /tmp/godchat-wa-demo/ en drukt het startcommando af

Alle nummers beginnen met 999 en kunnen niet bestaan. test_wa.py gebruikt bouw() voor zijn eigen namaak-store.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HIER = os.path.dirname(os.path.abspath(__file__))
DOEL = "/tmp/godchat-wa-demo"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

ZELF = "999000000009@s.whatsapp.net"
MARLEEN = "999000000001@s.whatsapp.net"   # adresboek: volledige naam
JOOST = "999000000002@s.whatsapp.net"     # chats.name is een nummer → naam uit routing ("Joost (zeilschool)")
FATIMA = "999000000003@s.whatsapp.net"    # chats.name "Fatima K." wint van routing
LIES = "999000000004@s.whatsapp.net"      # vijandige adresboeknaam
NUMMER = "999000000005@s.whatsapp.net"    # niets bekend: alleen het nummer
LUS = "999000000006@s.whatsapp.net"       # bezet kanaal
PIET = "999000000007@s.whatsapp.net"      # alleen een pushnaam
ZAAK = "999000000008@s.whatsapp.net"      # alleen een zakelijke naam
TEAM = "120999000000000001@g.us"          # groep van klant duinhuis
KORREL = "120999000000000002@g.us"        # groep van klant bakkerij, vijandige naam
LEEG = "999000000010@s.whatsapp.net"      # chat zonder berichten: verborgen
LID = "99900000000011@lid"                # lege lid-chat: verborgen
BROADCAST = "status@broadcast"            # verborgen
BEA = "999000000012@s.whatsapp.net"       # wacht, maar afgerond ("Top, bedankt!")
DIRK = "999000000013@s.whatsapp.net"      # de eigenaar antwoordde het laatst: wacht niet
EVA = "999000000014@s.whatsapp.net"       # de eigenaar reageerde met een emoji op het laatste: wacht niet
CLUB = "120999000000000003@g.us"          # gewone groep met een vermelding van de eigenaar: wacht
BUURT = "120999000000000004@g.us"         # gewone groep waar alleen gepraat wordt: wacht niet
EIGEN_LID = "999000000000099"             # het lid van het eigen toestel (15 cijfers, zoals in het echt)

MESSAGES_SQL = """
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP,
  ephemeral_expiration INTEGER NOT NULL DEFAULT 0, ephemeral_setting_timestamp INTEGER NOT NULL DEFAULT 0);
CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN,
  media_type TEXT, filename TEXT, url TEXT, media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
  deleted_at TIMESTAMP, quoted_message_id TEXT, PRIMARY KEY (id, chat_jid), FOREIGN KEY (chat_jid) REFERENCES chats(jid));
CREATE INDEX idx_messages_chat_jid ON messages(chat_jid);
CREATE TABLE calls (call_id TEXT, chat_jid TEXT, from_jid TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN, call_type TEXT,
  is_group BOOLEAN, result TEXT, duration_sec INTEGER, ended_at TIMESTAMP, reason TEXT, PRIMARY KEY (call_id, chat_jid));
"""
WHATSAPP_SQL = """
CREATE TABLE whatsmeow_contacts (our_jid TEXT, their_jid TEXT, first_name TEXT, full_name TEXT, push_name TEXT,
  business_name TEXT, redacted_phone TEXT, PRIMARY KEY (our_jid, their_jid));
CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL);
CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY, lid TEXT);
CREATE TABLE whatsmeow_chat_settings (our_jid TEXT, chat_jid TEXT, muted_until BIGINT NOT NULL DEFAULT 0,
  pinned BOOLEAN NOT NULL DEFAULT false, archived BOOLEAN NOT NULL DEFAULT false, wasa_root_secret_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (our_jid, chat_jid));
"""

ROUTING = {
    "_": "Namaak-routering voor de demo van de WhatsApp-tab. Verzonnen.",
    "zelf": ZELF,
    "klanten": {
        "duinhuis": {"naam": "Pension Duinhuis", "whatsapp": {MARLEEN: "Marleen", JOOST: "Joost (zeilschool)", TEAM: "groep Duinhuis team"}},
        "bakkerij": {"naam": "Bakkerij Korrel", "whatsapp": {FATIMA: "Fatima", KORREL: "groep Korrel"}},
    },
    "bezet": {LUS: "Testlus: de demo-bot (verzonnen)"},
}

KLAD = ("Hoi Marleen, de nieuwe boekingspagina staat klaar op de proefsite. Kijk je er vandaag even naar? "
        "Dan zet ik hem morgen live.\nhttps://example.org/duinhuis-proef")
NEP = {
    "_": "Nepgegevens voor de demo van de WhatsApp-tab. Verzonnen.",
    "chats": [
        {"id": "god", "naam": "God-chat", "soort": "god", "toestand": "slaapt", "uur": "09:00", "regel": "Luistert op de zelfchat."},
        {"id": "duinhuis", "naam": "Pension Duinhuis", "soort": "klant", "toestand": "wacht", "uur": "09:10", "regel": "Antwoord aan Marleen staat klaar."},
        {"id": "bakkerij", "naam": "Bakkerij Korrel", "soort": "klant", "toestand": "wacht", "uur": "09:20", "regel": "Vraag over de bestellijst."},
    ],
    "vragen": [
        {"nr": 1, "chat": "duinhuis", "uur": "09:10", "status": "open",
         "vraag": "Marleen vroeg of de boekingspagina af is. Dit antwoord sturen?",
         "context": "Ja = ik stuur het zoals het hier staat.", "concept": KLAD, "antwoorden": ["Ja, stuur", "Nee, wacht"],
         "whatsapp": {"aan": MARLEEN, "bericht": KLAD}},
        {"nr": 2, "chat": "bakkerij--bestellijst", "uur": "09:20", "status": "open",
         "vraag": "Fatima wil de bestellijst per mail in plaats van per WhatsApp. Zal ik dat zo instellen?",
         "context": "Het kost tien minuten; de mail gaat elke ochtend om 6 uur.",
         "concept": "Hoi Fatima, vanaf morgen krijg je de bestellijst om 6 uur in je mailbox.", "antwoorden": []},
    ],
    "verloop": [],
    "gesprekken": {},
}


def _ts(nu, minuten_terug, uren_tz=1, minuten_tz=0):
    tz = timezone(timedelta(hours=uren_tz, minutes=minuten_tz))
    return (nu - timedelta(minutes=minuten_terug)).astimezone(tz).isoformat(sep=" ", timespec="seconds")


def _maak_jpg(pad, kleur="teal", codec="mjpeg"):
    """Een effen beeld. Een webp-encoder heeft deze ffmpeg niet: de sticker is een png onder de naam .webp."""
    subprocess.run([FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={kleur}:s=320x240",
                    "-frames:v", "1", "-c:v", codec, "-f", "image2", pad], check=True)


def _maak_ogg(pad):
    subprocess.run([FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", pad], check=True)


PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
       b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def bouw(doel=DOEL, nu=None):
    """Bouwt de namaak-store in <doel>/store, plus routing.json en nep.json. Geeft een dict met de paden en id's."""
    nu = nu or datetime.now(timezone.utc)
    store = os.path.join(doel, "store")
    if os.path.isdir(store):
        shutil.rmtree(store)
    os.makedirs(store)
    with open(os.path.join(store, ".bridge-token"), "w") as fh:
        fh.write("demo-token\n")
    m = sqlite3.connect(os.path.join(store, "messages.db"))
    m.executescript(MESSAGES_SQL)
    w = sqlite3.connect(os.path.join(store, "whatsapp.db"))
    w.executescript(WHATSAPP_SQL)

    chats = {ZELF: "999000000009", MARLEEN: "Marleen", JOOST: "999000000002", FATIMA: "Fatima K.", LIES: "999000000004",
             NUMMER: "999000000005", LUS: "999000000006", PIET: "999000000007", ZAAK: "999000000008",
             TEAM: "Duinhuis team", KORREL: 'Korrel <b>"bakkers"</b> & \'co\'', LEEG: "Lege chat", LID: "", BROADCAST: "",
             BEA: "999000000012", DIRK: "999000000013", EVA: "999000000014", CLUB: "Zeilclub Noord", BUURT: "Buurtgroep"}
    m.executemany("insert into chats (jid, name) values (?, ?)", chats.items())
    me = "999000000009@s.whatsapp.net"
    w.executemany("insert into whatsmeow_contacts values (?, ?, ?, ?, ?, ?, '')", [
        (me, MARLEEN, "Marleen", "Marleen Verzonnen", "marleen", ""),
        (me, JOOST, "", "", "", ""),
        (me, FATIMA, "", "", "Fatima push", ""),
        (me, LIES, "Lies", '<b>Tante "Lies"</b> <img src=x onerror="window.__xss=1">', "", ""),
        (me, PIET, "", "", "Pushnaam Piet", ""),
        (me, ZAAK, "", "", "", "Zaak BV"),
        (me, "999000000021@s.whatsapp.net", "", "Groepslid Ans", "", ""),
        (me, BEA, "Bea", "Bea Verzonnen", "", ""),
        (me, DIRK, "Dirk", "Dirk Verzonnen", "", ""),
        (me, EVA, "Eva", "Eva Verzonnen", "", ""),
    ])
    w.execute("insert into whatsmeow_lid_map values ('99900000000011', '999000000010')")
    w.execute("insert into whatsmeow_device (jid, lid) values (?, ?)",   # het toestel van het eigen nummer
              (ZELF.replace("@", ":7@"), EIGEN_LID + ":7@lid"))
    w.execute("insert into whatsmeow_chat_settings (our_jid, chat_jid, archived) values (?, ?, 1)", (me, ZAAK))

    rijen = []

    def b(chat, mid, minuten, tekst="", van=None, mij=False, soort="", bestand="", tz=(1, 0), weg=None, citaat=None,
          lengte=None, ts=None):
        sender = (van or chat).split("@")[0] if not mij else ZELF.split("@")[0]
        rijen.append((mid, chat, sender, tekst, ts or _ts(nu, minuten, *tz), 1 if mij else 0, soort, bestand, lengte, weg, citaat))

    # Marleen (klant duinhuis): tekst, opmaak, links, reacties, antwoord-op, foto, spraak
    b(MARLEEN, "M001", 60 * 26, "Hoi! Hoe staat het met de *nieuwe* boekingspagina? _Geen haast_ hoor.")
    b(MARLEEN, "M002", 60 * 25, "Bijna klaar. Morgen meer, zie https://example.org/proef?a=1&b=2.", mij=True)
    b(MARLEEN, "M003", 60 * 3, "Super 🙌 Kun je ook ~oude prijzen~ `weghalen`?", citaat="M002")
    b(MARLEEN, "M004", 60 * 2, "", soort="image", bestand="image_x_M004.jpg", lengte=4000)
    b(MARLEEN, "M005", 90, "", soort="audio", bestand="audio_x_M005.ogg", lengte=9000)
    b(MARLEEN, "M006", 80, "Dit antwoordt op iets dat niet meer bestaat.", citaat="WEG999")
    b(MARLEEN, "M007", 70, "Deze foto staat nog niet op schijf", soort="image", bestand="image_x_M007.jpg", lengte=5000)
    b(MARLEEN, "R001", 65, "👍", van=MARLEEN, soort="reaction", bestand="M002")
    b(MARLEEN, "R002", 64, "❤️", mij=True, soort="reaction", bestand="M003")
    b(MARLEEN, "R003", 63, "😂", van=MARLEEN, soort="reaction", bestand="M003")
    b(MARLEEN, "R004", 62, "", van=MARLEEN, soort="reaction", bestand="M003")   # ingetrokken
    # Joost: wisselende tijdzones; de tekst van J002 sorteert later, maar het bericht is vroeger
    vandaag = nu.astimezone(timezone(timedelta(hours=1))).date()
    gisteren = vandaag - timedelta(days=1)
    b(JOOST, "J001", 0, "Eerste (12:00 Belgische tijd, gisteren)", ts=f"{gisteren} 12:00:00+01:00")
    b(JOOST, "J002", 0, "Nog eerder (18:00 in Singapore = 11:00 Belgisch)", ts=f"{gisteren} 18:00:00+08:00")
    b(JOOST, "J003", 0, "Daarna (17:30 in Mumbai = 13:00 Belgisch)", ts=f"{gisteren} 17:30:00+05:30")
    b(JOOST, "J004", 0, "Hoe laat is de zeilles morgen? Laatste bericht.", ts=f"{gisteren} 16:00:00+03:00")
    # groep Duinhuis team: afzender bekend, onbekend (= groeps-id), verwijderd, lange tekst, pdf, html-document
    b(TEAM, "T001", 60 * 50, "Welkom in de groep!", van="999000000021@s.whatsapp.net")
    b(TEAM, "T002", 60 * 49, "Bericht van iemand die de bridge niet kent.", van=TEAM)
    b(TEAM, "T003", 60 * 48, "oeps", van="999000000021@s.whatsapp.net", weg=_ts(nu, 60 * 47))
    b(TEAM, "T004", 60 * 30, ("Lange tekst. " * 60).strip() + "\n\nMet een tweede alinea en een link: https://example.org/lang.",
      van=MARLEEN)
    b(TEAM, "T005", 60 * 20, "De offerte", van=MARLEEN, soort="document", bestand="offerte duinhuis.pdf", lengte=len(PDF))
    b(TEAM, "T006", 60 * 19, "", van=MARLEEN, soort="document", bestand='../../kwaad"bestand".html', lengte=60)
    b(TEAM, "T007", 60 * 18, '<img src=x onerror="window.__xss=1">', van="999000000021@s.whatsapp.net")
    b(TEAM, "T008", 60 * 17, "<script>window.__xss=1</script> en [link](javascript:alert(1)) javascript:alert(1)", van=MARLEEN)
    # Fatima (klant bakkerij) en groep Korrel
    b(FATIMA, "F001", 60 * 5, "Kan de bestellijst voortaan per mail? 🍞")
    b(KORREL, "K001", 60 * 6, "Morgen 200 pistolets extra!", van=FATIMA)
    b(KORREL, "K002", 60 * 6 - 1, "", van=FATIMA, soort="sticker", bestand="sticker_x_K002.webp", lengte=100)
    # privé en de rest
    b(LIES, "L001", 60 * 30, "Kom je zondag eten? 😊")
    # zelf verstuurd met media: de bridge bewaart alleen de naam; het bestand staat in de wortel van de (namaak-)outbox
    b(LIES, "L002", 60 * 31, "De tafel van zondag", mij=True, soort="image", bestand="foto-demo-tafel.jpg", lengte=4000)
    b(LIES, "L003", 60 * 32, "", mij=True, soort="document", bestand="menu zondag.pdf", lengte=len(PDF))
    b(NUMMER, "N001", 60 * 24 * 9, "Hallo, wie is dit?")
    b(LUS, "U001", 60 * 4, "Bericht in een kanaal met een eigen lus.")
    b(PIET, "P001", 60 * 24 * 3, "Groetjes van Piet")
    b(ZAAK, "Z001", 60 * 24 * 20, "Uw factuur staat klaar.")
    b(ZELF, "S001", 30, "notitie: Marleen antwoorden over de boekingspagina", mij=True)
    b(BROADCAST, "B001", 10, "status-update die niet in de lijst hoort")
    # wie wacht op jou: een afsluiter, een beantwoord gesprek, een emoji als antwoord, twee gewone groepen
    b(BEA, "E001", 60 * 6, "Ik stuur je straks de foto's van het weekend.", mij=True)
    b(BEA, "E002", 60 * 5, "Top, bedankt! 🙏")
    b(DIRK, "D001", 60 * 8, "Lukt het om donderdag te bellen?")
    b(DIRK, "D002", 60 * 7, "Ja, om 10 uur is goed.", mij=True)
    b(EVA, "A001", 60 * 9, "Ik heb de sleutel bij de buren gelegd.")
    b(EVA, "RA01", 60 * 8, "👍", mij=True, soort="reaction", bestand="A001")
    b(CLUB, "C001", 60 * 30, "Wie gaat er mee zeilen zaterdag?", van="999000000021@s.whatsapp.net")
    b(CLUB, "C002", 60 * 29, "Ik misschien.", mij=True)
    b(CLUB, "C003", 60 * 4, f"@{EIGEN_LID} kan jij de boot ophalen om 9 uur", van="999000000021@s.whatsapp.net")
    b(CLUB, "C004", 60 * 3, "Ik neem broodjes mee", van=MARLEEN)
    b(BUURT, "Q001", 60 * 12, "Wie heeft er nog een ladder?", van="999000000021@s.whatsapp.net")
    b(BUURT, "Q002", 60 * 11, "Ik, kom maar halen", van=MARLEEN)
    m.executemany("insert into messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, "
                  "file_length, deleted_at, quoted_message_id) values (?,?,?,?,?,?,?,?,?,?,?)", rijen)
    m.commit()
    m.close()
    w.commit()
    w.close()

    # media op schijf, zoals de bridge ze bewaart: <soort>_<datum>_<tijd>_<id><ext>, documenten zonder extensie
    def map_(jid):
        p = os.path.join(store, jid.replace(":", "_"))
        os.makedirs(p, exist_ok=True)
        return p
    _maak_jpg(os.path.join(map_(MARLEEN), "image_20261121_101500_M004.jpg"))
    _maak_ogg(os.path.join(map_(MARLEEN), "audio_20261121_103000_M005.ogg"))
    with open(os.path.join(map_(TEAM), "document_20261120_120000_T005"), "wb") as fh:
        fh.write(PDF)
    with open(os.path.join(map_(TEAM), "document_20261120_120100_T006"), "w") as fh:
        fh.write("<script>window.__xss=1</script><h1>niet openen</h1>")
    _maak_jpg(os.path.join(map_(KORREL), "sticker_20261121_080000_K002.webp"), "orange", "png")

    outbox = os.path.join(doel, "outbox")   # namaak-outbox van de bridge (GODCHAT_WA_OUTBOX), nooit de echte
    if os.path.isdir(outbox):
        shutil.rmtree(outbox)
    os.makedirs(outbox, mode=0o700)
    _maak_jpg(os.path.join(outbox, "foto-demo-tafel.jpg"), "sienna")
    with open(os.path.join(outbox, "menu zondag.pdf"), "wb") as fh:
        fh.write(PDF)

    with open(os.path.join(doel, "routing.json"), "w", encoding="utf-8") as fh:
        json.dump(ROUTING, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(doel, "nep.json"), "w", encoding="utf-8") as fh:
        json.dump(NEP, fh, ensure_ascii=False, indent=1)
    return {"doel": doel, "store": store, "routing": os.path.join(doel, "routing.json"),
            "nep": os.path.join(doel, "nep.json"), "outbox": outbox}


def app_map(doel=DOEL):
    """Een app-map met symlinks naar de echte bestanden, zodat server.py de namaak-nep.json leest."""
    app = os.path.join(doel, "app")
    os.makedirs(app, exist_ok=True)
    for naam in ("server.py", "wa_lader.py", "wa.py", "wa.html", "index.html"):
        p = os.path.join(app, naam)
        if os.path.lexists(p):
            os.remove(p)
        os.symlink(os.path.join(HIER, naam), p)
    if os.path.isfile(os.path.join(HIER, "config.json")):   # zonder config.json gelden de standaardwaarden van server.py
        shutil.copy(os.path.join(HIER, "config.json"), os.path.join(app, "config.json"))
    shutil.copy(os.path.join(doel, "nep.json"), os.path.join(app, "nep.json"))
    spraak = os.path.join(doel, "spraak.py")
    if os.path.lexists(spraak):
        os.remove(spraak)
    os.symlink(os.path.join(os.path.dirname(HIER), "spraak.py"), spraak)
    os.makedirs(os.path.join(doel, "data"), exist_ok=True)
    return app


def main():
    info = bouw(DOEL)
    app_map(DOEL)
    print(f"Namaak-store klaar in {info['store']}")
    print("Start de demo met:")
    print(f"  GODCHAT_WA_STORE={info['store']} GODCHAT_WA_ROUTING={info['routing']} GODCHAT_WA_DATA={DOEL}/data "
          f"GODCHAT_WA_OUTBOX={info['outbox']} "
          f"{sys.executable} -I {DOEL}/app/server.py --nep --poort 8747")
    print("en open http://127.0.0.1:8747/wa")


if __name__ == "__main__":
    main()
