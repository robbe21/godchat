# WhatsApp-tab (`/wa`)

Je eigen WhatsApp in de GodChat-app, mobile-first: lezen uit de store van de bridge, versturen op je eigen tik, en
knoppen naar de chats (een klad versturen, een taak starten, een klantchat iets vragen).

| Bestand | Wat |
|---|---|
| `wa.py` | lezen uit de store, de adressen `/wa` en `/api/wa/…`, versturen, kladden en vraagkaarten |
| `wa.html` | de pagina (eigen CSS en JS, de kleuren van `index.html`), bij elke aanvraag opnieuw gelezen |
| `wa_lader.py` | laadt `wa.py` als vers moduleobject en opnieuw zodra het wijzigt; een kapotte versie laat de vorige dienen. Wat een herlading overleeft (tik-sleutel, droogloopberichten, caches) zit in `staat` |
| `wa_spraak.py` | één spraakbericht uitschrijven vanaf de commandline, met het geheugen van de tab; `../wacht.py` roept het aan voor elk spraakbericht in een levering |
| `wa_demo.py` | een namaak-store met verzonnen chats (ook vijandige inhoud, nummers met 999) en het startcommando van de demo |
| `test_wa.py` | tests op een namaak-store, namaak-app en namaak-bridge: `python3 -I tools/god-chat/app/test_wa.py` |

**Welke bridge.** De store: `GODCHAT_STORE`, anders `store` in `../routing.json`, anders
`~/whatsapp-mcp/whatsapp-bridge/store` (dezelfde keuze als `../wacht.py`). Het adres: `routing.json` → `bridge.api`,
anders `http://localhost:8080/api`; de sleutel van de bridge staat in de store (`.bridge-token`) en gaat als Bearer mee.
De outbox (alleen daarbinnen aanvaardt de bridge een bijlage): `bridge.outbox`, anders
`~/.local/share/whatsapp-mcp/outbox`. Gelezen bij het laden van `wa.py`. Een adres dat niet op deze computer is,
gebruikt de tab niet. `GODCHAT_WA_STORE`, `_BRIDGE`, `_ROUTING`, `_DATA` en `_OUTBOX` zijn er alleen voor tests en de
demo: een andere store dan de ingestelde praat nooit met de echte bridge.

**Lezen.** De store alleen-lezen (`messages.db`, `whatsapp.db`, media per chat). Tijdstempels hebben wisselende
tijdzones: sorteren op `julianday(timestamp), rowid`. Reacties hangen als chips aan hun bericht. Media die niet op
schijf staat, haalt de bridge op (`/download`). Spraak: afspelen (ogg, of m4a via ffmpeg) en "Schrijf uit" met
`../spraak.py`, bewaard in `data/wa/spraak.json`.

**Versturen** gebeurt alleen op een tik in de tab. Eén functie beslist (`echt_versturen()`): nooit in `--nep`, nooit
met een proef-store tegen de echte bridge, alleen als "Echt versturen" aan staat (`data/wa.json`, tandwiel in de tab;
standaard droogloop) en alleen als de store van je eigen nummer is (het toestel in `whatsapp.db` tegen `zelf` in
`routing.json`; anders weigert de tab met een rode balk). Alleen `_bridge_verstuur()` roept `/send` of `/react` aan.
Dezelfde tekst naar hetzelfde gesprek binnen 8 s wordt geweigerd; bij een onduidelijk antwoord van de bridge zegt de tab
dat het onzeker is en probeert hij niet opnieuw. In het Verloop komt alleen dát er iets verstuurd is, nooit de inhoud.
Geen enkele chat roept `/api/wa/…` aan.

**Bijlagen** (paperclip; op een breed scherm ook plakken en slepen). Elk bestand gaat eerst naar
`POST /api/wa/bijlage` en wordt meteen verwerkt: een foto wordt opnieuw gecodeerd (draaiing in de pixels, alle
metadata en GPS eruit, hoogstens 2560 px; heic via `sips`), een video wordt H.264/AAC zonder metadata (hoogstens 1280 px
en 50 MB), de rest gaat ongewijzigd. Wat niet te verwerken is, wordt geweigerd: nooit het origineel als terugval.
Versturen kopieert elke bijlage naar `<outbox>/godchat-wa/…`; gaat er één mis, dan stopt de tab en zegt hij wat weg is.

**Spraakbericht opnemen** (paperclip → "Spraakbericht", of lang drukken op de microfoon). Hoogstens 15 minuten. ffmpeg
maakt er Ogg Opus mono 48 kHz van zonder metadata, en de tab kijkt het resultaat na: de bridge rekent de duur die de
ontvanger ziet in 48 kHz, met een andere rate klopt die niet.

**Wie wacht op jou.** Geen leesstatus (die bewaart de bridge niet), maar: bij wie ben je een antwoord schuldig
(`wacht_voor()`). Een gesprek wacht als het laatste echte bericht van de ander komt, hoogstens 14 dagen oud is, je er
niet met een emoji op reageerde en je het niet afvinkte. Nooit: je zelfchat, een kanaal onder `bezet`, een
gearchiveerde chat. Een gewone groep (niet van een klant) wacht alleen als iemand je vermeldde of op je bericht
antwoordde. Het filter "Wacht op jou" toont: antwoord klaar (een klad), wacht op je antwoord, waarschijnlijk afgerond.
Afvinken: `POST /api/wa/klaar`, bewaard in `data/wa/klaar.json`.

**De koppeling met de chats.**
- *Klad*: een open vraag met het veld `whatsapp` (`vraag_eigenaar`; `motor.py` → `_wa_klad` laat alleen de eigen chats
  van een klant toe) staat onderaan dat gesprek met **Stuur** en **Pas aan**. Na een echte verzending beantwoordt de tab
  de vraag met "Zelf verstuurd vanuit de WhatsApp-tab …", zodat de chat niet nog eens stuurt. Is de vraag intussen
  elders beantwoord, dan weigert de tab het klad.
- *Vraagkaart*: een open vraag zonder `whatsapp` staat in het gesprek waar ze bij hoort.
- *Start taak* → `nieuwe_taak`; *Vraag de klantchat* en *Schrijf een antwoord* → `zeg`. Het bericht gaat mee als
  materiaal, met id en `chat_jid`.

**Veiligheid.** Berichten, namen en bestandsnamen komen van vreemden, en de pagina kan versturen. Daarom: alle tekst
als tekstknoop, CSP met nonce op `/wa`, media alleen inline voor een vaste lijst van soorten, elk geserveerd pad na
`realpath` binnen de store, een vaste lijst toegelaten `Host`-koppen en `Sec-Fetch-Site`, en voor elke POST `Origin`
plus de tik-sleutel uit de pagina. Van buiten houdt het slot (`slot.py`) de tab dicht tot iemand `--wa aan` zet; een
aanvraag met proxykoppen mag alleen binnen als ze langs het slot kwam.

**Demo.** `python3 -I tools/god-chat/app/wa_demo.py` bouwt `/tmp/godchat-wa-demo/` en drukt het commando af om de app
met die namaak-store als nepscherm te starten.

## Bekende grenzen

1. Niet getest op iPhone/Safari: spraak afspelen en opnemen, het toetsenbord onder het invoerveld, een echte
   iPhone-video.
2. Geen nieuw gesprek starten, geen ledenlijst van een groep, niet zoeken in berichten.
3. Wijzig `wa.py` en `wa.html` in een werkkopie: in deze map laden ze meteen in de draaiende app.
