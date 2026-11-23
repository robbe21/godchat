# GodChat-app

Een lokaal scherm boven de God-chat: het draait de God-chat en de klantchats (één per project onder `klanten` in
`routing.json`) zelf, elk als eigen `claude`-sessie op deze computer, en toont wat er op de eigenaar wacht. De app
luistert alleen op 127.0.0.1. Hoe de chats werken staat in `../PROTOCOL.md` en `../GOD.md`; dit bestand is voor wie
aan de app bouwt.

**Starten** (vanuit de werkmap, de map boven `tools/god-chat`): `python3 -I tools/god-chat/app/server.py` →
http://127.0.0.1:8742. Nodig: `../routing.json` (een kopie van `../routing.voorbeeld.json`, ingevuld). Optioneel:
`config.json` (zonder gelden de waarden van `config.voorbeeld.json`) en `../rooster.json` (zonder zijn er geen
geplande taken). Het scherm met nepgegevens, zonder motor en zonder sessies: `--nep --poort 8743` (leest `nep.json`).
Opnieuw starten pakt alles op: de sessie wordt hervat, de wachter start opnieuw, een beurt die liep komt opnieuw.

| Bestand | Wat |
|---|---|
| `server.py` | webserver en API: `/api/stand`, `gesprek`, `zeg`, `antwoord`, `neem-over`, `nieuwe-dag`, `door`, `taak`, `taak-status`, `model`, `tool`, `dicteer`, en `/s/<naam>` (vaste bestanden uit `statisch/`) |
| `motor.py` | per chat één `claude`-sessie en één `../wacht.py`, één beurt tegelijk; taakchats en geplande rondes; de startbeurten |
| `godchat_mcp.py` | de eigen tools van de sessies (stdio, zonder pakketten): `vraag_eigenaar`, `stuur_door`, `start_taak`, `taak_klaar`, `meld_stand`, `plan_taak`, `wijzig_taak`. Elke aanroep gaat als POST naar `/api/tool` |
| `index.html` | het scherm: dashboard (open vragen als kaartjes, cijfers uit het verloop), chatlijst, gesprek, Verloop |
| `klanten.py`, `klanten_lader.py`, `klanten.js` | de klanten (projecten) in één blik op het dashboard, en de korte opschriften van de vraagkaartjes: `KLANTEN.md` |
| `klok.py`, `klok_lader.py`, `klok.js` | geplande taken (zie hieronder) |
| `wa.py`, `wa.html`, `wa_lader.py`, `wa_spraak.py`, `wa_demo.py` | de WhatsApp-tab op `/wa`: `WA.md` |
| `push.py`, `push_lader.py`, `push_stuur.py`, `sw.js`, `push_vereisten.txt` | meldingen op je toestellen (Web Push): `PUSH.md` |
| `slot.py` | de login voor toegang van buiten (een tweede poort, app-poort + 20) |
| `herstart.py` | de app veilig herstarten |
| `statisch/` | het manifest en de iconen (installeerbaar vanuit Chrome) |
| `config.voorbeeld.json`, `nep.json` | de standaardinstellingen, en de verzonnen gegevens van het nepscherm |
| `data/` | stand, vragen, verloop, gesprekken, archief, logs. Niet met de hand bewerken; niet in git |
| tests | `test_zuinig.py`, `test_klok.py`, `test_wa.py`, `slot_test.py`; zelftests `klanten.py --zelftest-kort` en `push.py --zelftest`. Ze raken niets echts: tijdelijke mappen, een namaak-store en een namaak-sessie |

**Hoe het loopt.** De wachter levert (NIEUW, HARTSLAG) → beurt naar de sessie → pas na die beurt start de wachter
opnieuw. Elke startbeurt zegt hoe de eigenaar heet (`routing.json` → `eigenaar`); `routing.json` →
`klanten.<sleutel>.bijzonderheden` gaat als "Bijzonderheden voor jouw klant" mee in de startbeurt van die klantchat (`per_klant_regel`). In
een levering heet een eigen bericht `eigenaar` (`wacht.py` → `IK`). Een vraag komt via `vraag_eigenaar`
in "Wacht op jou"; het antwoord gaat letterlijk als beurt terug ("Antwoord van de eigenaar op je vraag «…»"). Het
vraagnummer is alleen intern (`index.html` koppelt de kaart via "Vraag <nr>" in het antwoord van de tool). Schrijf je in
het gesprek terwijl een vraag openstaat, dan sluit `zeg` die vraag en krijgt de sessie een naschrift dat alleen zij
ziet. Schrijf je terwijl de chat werkt, dan wacht je bericht in de rij en toont het scherm het als "wacht tot … klaar
is". "Neem over" start een sessie met de start uit het protocol; luistert er elders al een wachter voor die chat, dan
krijgt die OVERGENOMEN. Model of effort wijzigen schrijft `config.json` en geldt vanaf de volgende beurt (de sessie
herstart met `--resume`). "Nieuwe dag": verse sessies, gesprekken naar `data/archief/`, open vragen op vervallen.

**De CLI, niet de Agent SDK.** Per chat één proces `claude -p --input-format stream-json --output-format stream-json
--verbose` dat openblijft: elke regel op stdin is een beurt. Het is dezelfde `claude` als in de terminal (cwd = de
werkmap), dus met dezelfde login, instellingen en MCP-servers. De eigen tools komen erbij via `--mcp-config`.
Toestemming: `--permission-mode bypassPermissions`; `--disallowedTools` haalt de eigen wekkers weg (CronCreate,
CronDelete, CronList, ScheduleWakeup): herhalen gaat alleen via de klok. `schone_omgeving()` haalt `CLAUDE*` en
`ANTHROPIC*` uit de omgeving, want een proces dat de Claude-desktop-app startte erft anders een eigen adres en login.

**Zuinig met context.** Na elke beurt leest de motor hoe vol de context is. Boven `config.json` → `compact` (`tokens`,
of `pct` van het venster) stuurt hij tussen twee beurten `/compact` met `COMPACT_OPDRACHT`, niet als er iets in de rij
staat, bij een fout of binnen 15 minuten na een vraag van die chat. De eerste beurt erna krijgt een naschrift met de
werkwijze en de open vragen. Een API-fout 529 probeert de app na 60 en 180 s opnieuw. De motortaak `ochtend` (rooster,
06:00) laat de God-chat en elke klantchat boven 60k tokens samenvatten; sessie en open vragen blijven.

**Taakchats.** Een klantchat (`start_taak`), de God-chat (`start_taak` met `onder`) of de eigenaar ("+ Taak" op het
scherm) start een chat voor één taak: sleutel `<project>--<naam>`, eigen sessie, geen wachter, model en effort van de
chat erboven. De klantchat krijgt bij elke levering zijn open taken en stuurt door met `stuur_door`; `taak_klaar` sluit
de taak. Hoogstens zes open taken per project (`MAX_TAKEN`). Onder de God-chat: `god--<naam>`, meestal over de app zelf.

**Geplande taken (de klok).** `../rooster.json` (elke 30 s opnieuw gelezen) en per taak de opdracht in
`../rooster/<id>.md`. Op haar uur start de klok een ronde: taakchat `<onder>--r-<id>` met een verse sessie, die de
opdracht één keer doet en zichzelf sluit; de ouder hoort het bij zijn volgende levering. Een chat maakt een taak alleen
met `plan_taak` (ze komt uit onder het project; de tik van de eigenaar op de vraag zet ze aan) en past ze aan met
`wijzig_taak` (pas die tik voert het door). `koppelingen` noemt de MCP-servers die een ronde nodig heeft; ontbreken ze of moeten ze
inloggen, dan komt er een stille vraag in "Wacht op jou". Een taak van soort `motor` (`ochtend`, `nieuwe_dag`) roept
de motor aan in plaats van een ronde. Met `desktop_id` weigert de klok zolang dezelfde taak in de Claude-desktop-app aan
staat; zonder desktop-app of zonder roosters daar werkt de klok gewoon, een onleesbaar rooster daar doet weigeren. De
tijden staan in de tijdzone `GODCHAT_TZ` (standaard Europe/Brussels). Schakelaar en "Nu draaien" staan onder het project; geen chat roept
`/api/klok/*` aan. Een tik op een klokrij opent de pagina van de taak met al haar rondes.

**Installeerbaar.** `index.html` linkt het manifest met `crossorigin="use-credentials"`, zodat het ook achter het slot
geladen wordt. `sw.js` is er voor de meldingen, bewust zonder `fetch`-handler: niets wordt gecachet.

**Werken aan de app.**
- `index.html`, `wa.html`, `klanten.js`, `klok.js` en `sw.js` worden bij elke aanvraag gelezen: herladen volstaat.
  `wa.py`, `klanten.py`, `klok.py` en `push.py` laden opnieuw via hun lader. De rest (o.a. `motor.py`, `server.py`,
  `godchat_mcp.py`) vraagt een herstart, en die breekt lopende beurten af.
- Herstarten alleen met `python3 -I tools/god-chat/app/herstart.py [--bericht "<tekst>" --naar <sleutel>]`: hij wacht
  tot alle chats drie minuten stil zijn, laadt de nieuwe code eerst op proef en logt in `data/logs/herstart.log`.
  Nooit met `kill`.
- Elk adres in `server.py` of `wa.py` valt vanzelf achter het slot. Wat alleen op de computer zelf mag, zet je in
  `ALLEEN_LOKAAL` in `slot.py`. Op de lokale poort weigert `server.py` (`_vreemd`) elke vreemde `Host`
  (DNS-rebinding) en elke POST van een andere oorsprong of poort; de WhatsApp-tab eist daarbovenop zijn tik-sleutel.
- Een hervatte sessie houdt de beschrijving van een tool zoals ze die de eerste keer laadde. Een wijziging in
  `godchat_mcp.py` bereikt alleen nieuwe sessies; de app neemt een nieuw veld wel aan.
- Testen: een `Motor` met een nep-`Sessie` start na een startbeurt nog altijd de echte `wacht.py`. Zet
  `Chat.start_wachter` uit en wijs `motor.DATA` en `motor.ROUTING` naar een tijdelijke map (zie `test_zuinig.py`).
  Laat nooit iets tegen je echte store of bridge lopen: gebruik `wa_demo.py`.

**Omgeving.** `GODCHAT_STORE` (de store van de bridge; gaat voor `store` in `routing.json`), `GODCHAT_WHISPER_PY` (de
Python met mlx-whisper, voor dicteren en spraak), `GODCHAT_PUSH_PY` (de Python met pywebpush), `GODCHAT_TZ` (de tijdzone van het rooster, standaard
Europe/Brussels). De app zet zelf
`GODCHAT_URL`, `GODCHAT_CHAT` en `GODCHAT_SLEUTEL` voor `godchat_mcp.py`. Alleen voor tests en de demo:
`GODCHAT_WA_STORE`, `GODCHAT_WA_BRIDGE`, `GODCHAT_WA_ROUTING`, `GODCHAT_WA_DATA`, `GODCHAT_WA_OUTBOX`,
`GODCHAT_KLANT_BRON`, `GODCHAT_PUSH_MAP`, `GODCHAT_PUSH_PROEFDIENST`, `GODCHAT_SLOT_MAP`.

## Bekende grenzen

1. **Geen hard slot op versturen.** De grens is het protocol: met `bypassPermissions` kan een sessie technisch
   WhatsApp of mail sturen. Wie dat dicht wil, bouwt een hook die verstuur-tools alleen toelaat na een ja in de app.
2. **macOS**: dicteren en spraak vragen mlx-whisper (Apple Silicon); de WhatsApp-tab zoekt ffmpeg en ffprobe in
   `/opt/homebrew/bin` en gebruikt `sips` voor heic.
3. Het Verloop toont elk beurt-einde, ook een lege hartslag.

Laatst bijgewerkt: 2026-11-23
