# Installatie

Dit bestand is geschreven voor je eigen Claude Code, en leesbaar voor jezelf. Open Claude Code in je werkmap en zeg:
"installeer tools/god-chat volgens INSTALL.md".

**De werkmap** is de map waarin je projectmappen staan (of komen). Deze repo hoort op `<werkmap>/tools/god-chat/`: de
app draait de chats vanuit de werkmap en zoekt alles relatief daaraan. Alle commando's hieronder draai je in de
werkmap.

**Voor Claude.** Loop de stappen in volgorde af. Elke stap zegt wat je controleert, wat je doet en wat je de gebruiker
vraagt; een stap die al klaar is, sla je over. Vraag één ding tegelijk. Verstuur niets, zet niets open naar buiten,
typ nooit een wachtwoord of sleutel in dit gesprek, en start de app nooit twee keer. Wat alleen de gebruiker kan (een
QR scannen, een wachtwoord kiezen, een eigen Terminal-venster openen), vraag je, en je wacht tot het gedaan is.

## 1. Vereisten

**Controleer:**
- `uname -sm`: macOS (`Darwin`). Spraak uitschrijven vraagt Apple Silicon (`arm64`).
- `pwd` is de werkmap, en `tools/god-chat/app/server.py` bestaat. Zo niet: vraag de gebruiker waar de werkmap is, en
  zet de repo daar met `git clone https://github.com/robbe21/godchat.git tools/god-chat`.
- `python3 --version`: 3.9 of nieuwer. Wil de gebruiker later de app op de telefoon (het slot), dan moet
  `python3 -c "import hashlib; hashlib.scrypt"` zonder fout lopen: de python3 van macOS zelf kent geen scrypt, die van
  Homebrew (`brew install python`) wel. Start de app dan met die Python.
- `claude --version`, en ingelogd zoals de app het nodig heeft. De app start `claude` zonder de omgevingsvariabelen
  die met `CLAUDE` of `ANTHROPIC` beginnen; dit commando doet hetzelfde en moet `ok` geven:

      python3 -I -c "import sys, subprocess; sys.path.insert(0, 'tools/god-chat/app'); import motor; print(subprocess.run(['claude', '-p', 'Antwoord alleen: ok'], env=motor.schone_omgeving(), capture_output=True, text=True).stdout)"

  Zegt het "Not logged in", of komt er niets, zie Problemen.
- Voor de bridge: `go version` (1.25 of nieuwer), `uv --version` en `git --version`. Optioneel: `ffmpeg -version`.

**Doe:** ontbreekt iets, zeg dan wat en hoe het erbij komt (bv. `brew install go uv ffmpeg python`; zonder Homebrew
eerst https://brew.sh), en vraag of je het mag installeren of dat de gebruiker het zelf doet.

## 2. De WhatsApp-bridge installeren en koppelen

De chats lezen WhatsApp via de bridge [verygoodplugins/whatsapp-mcp](https://github.com/verygoodplugins/whatsapp-mcp):
een Go-programma dat zich als gekoppeld toestel aan je nummer hangt en alles in een lokale database bewaart (de store),
plus een MCP-server voor Claude.

**Controleer:** bestaat `~/whatsapp-mcp/whatsapp-bridge/store/messages.db`? Dan is er al een gekoppelde bridge: ga naar
stap 3. Staat een bestaande bridge ergens anders, onthoud dan de map `store/` voor stap 4.

**Vraag eerst:** "De bridge is onofficieel; WhatsApp kan een nummer dat ze gebruikt beperken of blokkeren. Wil je
verder?" Ga pas verder na een ja.

**Doe:**
1. `git clone https://github.com/verygoodplugins/whatsapp-mcp.git ~/whatsapp-mcp` (die plek is de standaard van de
   kit).
2. Vraag de gebruiker een eigen Terminal-venster te openen en daar te typen:
   `cd ~/whatsapp-mcp/whatsapp-bridge && go run .`
   Er verschijnt een QR-code. Scan die met WhatsApp op de telefoon: Instellingen → Gekoppelde apparaten → Apparaat
   koppelen. De eerste synchronisatie kan enkele minuten duren. Start de bridge niet zelf vanuit dit gesprek: de QR
   moet zichtbaar zijn, en de bridge moet blijven draaien.
3. Blijvend laten draaien (aanbevolen): is de koppeling gelukt, laat de gebruiker de bridge in dat venster stoppen met
   Ctrl+C en dan `~/whatsapp-mcp/scripts/install-launchd-macos.sh` draaien. Dat start de bridge bij elke login en houdt
   haar in het oog.

**Controleer:** `ls -a ~/whatsapp-mcp/whatsapp-bridge/store/` toont `messages.db` en `.bridge-token`. Toon de inhoud van
het token nooit.

## 3. De MCP `whatsapp` in Claude Code

De chats lezen en sturen WhatsApp met deze MCP-server. Hij moet precies `whatsapp` heten.

**Controleer:** `claude mcp list`. Staat `whatsapp` er al en is hij verbonden, ga dan naar stap 4.

**Doe:** `claude mcp add whatsapp -- uv --directory "$HOME/whatsapp-mcp/whatsapp-mcp-server" run main.py`, en
controleer opnieuw met `claude mcp list`.

Een server die net is toegevoegd, kent deze sessie nog niet. Vraag de gebruiker Claude Code te sluiten en in de
werkmap opnieuw te openen met `claude --continue`; ga dan verder met stap 4.

## 4. routing.json

`routing.json` zegt hoe de eigenaar heet, wat de zelfchat is en welke WhatsApp-chats bij welk project horen.

**Controleer:** bestaat `tools/god-chat/routing.json` al? Lees het dan en vraag wat er moet veranderen.

**Doe:** `cp tools/god-chat/routing.voorbeeld.json tools/god-chat/routing.json`. De uitleg onder `_` mag blijven staan
(de app leest ze niet); de voorbeeldprojecten vervang je.

**Vraag de gebruiker:**
1. **De naam** → `eigenaar`. Elke chat krijgt die in zijn startbeurt.
2. **Het eigen nummer** → `zelf`. Laat de gebruiker in WhatsApp een bericht aan zichzelf sturen, en zoek met
   `list_chats` de chat met dat nummer. Het `chat_jid` heeft de vorm `<landcode en nummer>@s.whatsapp.net`.
3. **Welke projecten.** Begin met één of twee. Per project een sleutel (kleine letters, cijfers en `-`), een `naam`
   en een `map` relatief aan de werkmap, bv. `projecten/bakkerij`.
4. **Welke WhatsApp-chats bij welk project.** Laat de gebruiker namen noemen, zoek ze met `list_chats` (op naam) en
   laat elke treffer bevestigen voor hij in `whatsapp` komt (`chat_jid` → naam). Eén-op-één eindigt op
   `@s.whatsapp.net`, een groep op `@g.us`. Alleen deze chats wekken een klantchat.
5. **Bijzonderheden** per project (optioneel): wat die chat altijd moet weten, bv. wie over prijzen beslist, of een
   groep waarin de chat nooit zelf schrijft.
6. **Bezet** (optioneel): chats waarop al een eigen bot of lus draait. Anders blijft het `{}`.

Een `meter` per project is optioneel: laat hem weg tenzij de gebruiker er een wil. Staat de store niet op
`~/whatsapp-mcp/whatsapp-bridge/store`, zet dan `"store": "<pad naar store/>"` naast `zelf`. Luistert de bridge niet op
poort 8080, zet dan `"bridge": {"api": "http://localhost:<poort>/api"}`.

**Controleer:** `python3 -m json.tool tools/god-chat/routing.json > /dev/null` (geldige JSON), en
`python3 -I tools/god-chat/wacht.py --proef <sleutel>` voor elk project en voor `god`: hoeveel berichten er de laatste
24 uur binnenkwamen en hoe vaak dat een chat had gewekt. Het verandert niets.

## 5. Projectmappen

Elke klantchat heeft een map als geheugen: `CLAUDE.md` (naslag: wie, wat, waar), `LOG.md` (wat er gebeurde, nieuwste
bovenaan) en `CHAT.md` (de aanvulling op de werkwijze, die de chat bij zijn eerste start zelf schrijft).

**Controleer** per project: bestaat `<map>/CLAUDE.md`?

**Doe:** ontbreekt hij, maak dan de map en een korte `CLAUDE.md` uit wat de gebruiker vertelt: wie de klant of het
project is, de contactpersonen en wie beslist, wat er loopt, waar alles staat (site, repo, documenten) en de open
punten. Tien tot dertig regels; verzin niets. Maak ook `LOG.md` met één kop: `## <datum> — GodChat gestart`.

## 6. Starten

**Controleer** eerst of de app al draait:
`curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8742/api/stand`. Geeft dat `200`, dan draait ze al:
start ze niet opnieuw (een tweede start stopt meteen met "Poort … is niet vrij") en ga naar stap 7.

**Optioneel**, vóór de start:
- `cp tools/god-chat/app/config.voorbeeld.json tools/god-chat/app/config.json` om de poort of het model per chat te
  kiezen. Zet onder `chats` de sleutels van de eigen projecten; de voorbeeldsleutels mogen weg. Zonder dit bestand
  gelden dezelfde standaardwaarden.
- `cp tools/god-chat/rooster.voorbeeld.json tools/god-chat/rooster.json`: elke ochtend om 06:00 vatten grote chats hun
  gesprek samen. Zonder dit bestand zijn er geen geplande taken.

**Doe:** vraag de gebruiker een eigen Terminal-venster te openen en daar te typen:

    cd <werkmap> && python3 -I tools/god-chat/app/server.py

Start de app niet zelf vanuit dit gesprek: ze stopt dan zodra dit gesprek stopt. Het venster blijft open; wordt het
gesloten, dan stopt de app. Na een herstart met `python3 -I tools/god-chat/app/herstart.py` draait ze los van elk
venster, met haar log in `tools/god-chat/app/data/logs/server.log`.

**Controleer:** in het venster staat `GodChat (…) op http://127.0.0.1:8742`, en het `curl`-commando hierboven geeft
`200`. Laat de gebruiker http://127.0.0.1:8742 openen.

## 7. Neem over

Bij een eerste start draait er nog geen chat: elke chat staat op "niet in de app". Vraag de gebruiker op het scherm
eerst de God-chat te openen en op **Neem over** te klikken, daarna elke klantchat. Elke chat leest dan zijn werkwijze,
haalt in wat er de laatste dagen binnenkwam en schrijft zijn `CHAT.md`. Die eerste beurt kost meer dan een gewone.

**Controleer:** `python3 -I tools/god-chat/wacht.py --status` toont bij elke chat `ja` onder "luistert". Een chat
luistert pas na zijn eerste beurt; dat kan een paar minuten duren.

## 8. Eerste proef

**Vraag de gebruiker** een notitie te zetten in de eigen WhatsApp-chat (de zelfchat), voor één project, bv. "Bakkerij
Korrel: zoek uit hoeveel plaatsen er nog vrij zijn op de workshop."

**Controleer:** na ongeveer anderhalve minuut krijgt de God-chat de notitie en stuurt hij ze door; in het Verloop staat
"God-chat → <naam van het project>". De klantchat gaat ermee aan de slag, en wat een ja vraagt, staat in "Wacht op
jou". Dan werkt het geheel.

Zeg de gebruiker nog dat de WhatsApp-tab (`/wa`) pas echt verstuurt na "Echt versturen" (het tandwiel in de tab); tot
dan is elke tik daar een droogloop.

## Daarna (optioneel)

- **Mail.** Voeg in de werkmap een mail-MCP naar keuze toe aan Claude Code (bv. een Google Workspace- of
  Microsoft 365-MCP) en test hem in een gewone sessie. Herstart daarna de app met `herstart.py`, zodat de chats hem
  laden, en zeg elke klantchat welke mailbox en welke zoekopdrachten bij zijn project horen: hij zet het in
  `CHAT.md` → Mail. Zonder mail-MCP slaan de chats de mail over.
- **Spraak** (Apple Silicon), met `brew install ffmpeg` erbij (de app zoekt ffmpeg in `/opt/homebrew/bin`):

      python3 -m venv ~/.venvs/mlx-whisper && ~/.venvs/mlx-whisper/bin/pip install mlx-whisper

  Een andere plek: zet `GODCHAT_WHISPER_PY` op die Python. Daarna staan spraakberichten uitgeschreven in elke levering
  en werkt de dicteerknop. De eerste keer haalt mlx-whisper zijn model op.
- **Meldingen** (Web Push):

      python3 -m venv ~/.venvs/godchat-push && ~/.venvs/godchat-push/bin/pip install -r tools/god-chat/app/push_vereisten.txt

  Een andere plek: zet `GODCHAT_PUSH_PY` op die Python. Daarna per toestel aanzetten op het scherm ("Meldingen
  aanzetten" onder de chatlijst). Meer in `app/PUSH.md`.
- **De app op je telefoon** (het slot). Alleen als de gebruiker dat uitdrukkelijk wil, want dit zet de app open naar
  buiten:
  1. Het wachtwoord kiest de gebruiker zelf, in een Terminal: `python3 -I tools/god-chat/app/slot.py --zet` (of op
     http://127.0.0.1:8742/slot). Nooit in dit gesprek. Het slot vraagt een Python met scrypt (zie stap 1).
  2. Tailscale op de computer en de telefoon, en `tailscale serve --bg 8762`: de buitenpoort is de poort van de app
     plus 20. Zet nooit de poort van de app zelf open.
  3. `python3 -I tools/god-chat/app/slot.py --buiten https://<het adres dat Tailscale toont>`.
  4. De WhatsApp-tab van buiten is een aparte keuze: `slot.py --wa aan`.
  Controleer met `python3 -I tools/god-chat/app/slot.py --status`.
- **Geplande taken.** Met `rooster.json` (stap 6) loopt de klok. Nieuwe taken maken de chats zelf met `plan_taak`: ze
  komen uitgeschakeld onder hun project te staan, en de gebruiker zet ze aan met een tik. De uren gelden in
  Europe/Brussels; een andere tijdzone zet je met `GODCHAT_TZ`, bv. `GODCHAT_TZ=Europe/Amsterdam`.

Een omgevingsvariabele (`GODCHAT_TZ`, `GODCHAT_WHISPER_PY`, `GODCHAT_PUSH_PY`, `GODCHAT_STORE`) zet je het best in het
shellprofiel (bv. `~/.zshrc`), zodat ze ook na een herstart geldt.

## Problemen

- **De app start niet: "Geen routing.json in …".** Zie stap 4. Zegt ze dat `routing.json` geen object `klanten` heeft,
  dan ontbreekt dat blok.
- **De store wordt niet gevonden.** In het Verloop of bij `wacht.py --status` staat
  `FOUT geen WhatsApp-store: …/messages.db bestaat niet`. Draait de bridge, en is ze gekoppeld (stap 2)? Staat de store
  ergens anders, zet dan `store` in `routing.json` of `GODCHAT_STORE`.
- **"Not logged in"** in een chat of bij de controle van stap 1. De app start `claude` zonder de omgevingsvariabelen
  die met `CLAUDE` of `ANTHROPIC` beginnen, dus alleen een gewone login werkt: open een terminal, typ `claude`, log in
  met `/login`, en herstart dan de app met `herstart.py`. Een API-sleutel in `ANTHROPIC_API_KEY` of een proxy werkt
  hier niet.
- **De poort is bezet** ("Poort … is niet vrij"). Kijk eerst of de app al draait: `curl` uit stap 6, of
  `lsof -nP -iTCP:8742 -sTCP:LISTEN`. Is het de app, start dan geen tweede: herstarten gaat met `herstart.py`. Is het
  een ander programma, kies dan een andere `poort` in `app/config.json`. De buitenpoort van het slot schuift mee
  (poort plus 20).
- **`claude` niet gevonden.** De app zoekt `claude` in het `PATH` van het venster waarin ze startte, en in
  `/opt/homebrew/bin`, `/usr/local/bin` en `~/.local/bin`.
