# GodChat-kit

Eén God-chat boven een klantchat per project. Elke chat is een `claude`-sessie die de GodChat-app op deze computer
draait. Een klantchat volgt de WhatsApp en de mail van één project, bereidt voor wat kan en vraagt de eigenaar alleen
om een ja. Hoe de chats werken, staat in `PROTOCOL.md` en `GOD.md`; hoe de app gebouwd is, in `app/CLAUDE.md`. Deze
map hoort op `<werkmap>/tools/god-chat/`: de chats draaien in de werkmap en vinden hun projectmappen relatief daaraan.

| Bestand | Wat |
|---|---|
| `PROTOCOL.md` | de werkwijze van elke klantchat en taakchat: meelezen, wat op het ja van de eigenaar wacht, melden |
| `GOD.md` | de werkwijze van de God-chat: de zelfchat, bewaken, een nieuw project |
| `wacht.py` | de wachter: leest de store van de WhatsApp-bridge (alleen-lezen, kost geen tokens) en stopt als er iets is. Ook `--inhalen`, `--status`, `--proef <klant>`, `--stop <klant\|alles>` en `--hervat` |
| `spraak.py` | spraakberichten naar tekst met mlx-whisper (de Python uit `GODCHAT_WHISPER_PY`) |
| `routing.voorbeeld.json` | het voorbeeld voor `routing.json`: eigenaar, zelfchat, projecten met hun WhatsApp-chats, bezet |
| `rooster.voorbeeld.json` | het voorbeeld voor `rooster.json`: de geplande taken |
| `app/` | de GodChat-app: scherm, motor, eigen tools, WhatsApp-tab, klok, meldingen en slot (`app/CLAUDE.md`) |
| `README.md`, `INSTALL.md`, `LICENSE` | wat het is, de installatie stap voor stap, de licentie (MIT) |

## Hoe het loopt

- **Wachter.** De app start per chat `wacht.py <sleutel>`. Die wekt de chat 2 minuten na een bericht zonder antwoord,
  5 minuten nadat een gesprek met de eigenaar stilviel, en 90 seconden na een notitie in de zelfchat. Overdag stopt hij
  ook na drie uur stilte (HARTSLAG; bij de God-chat na zes): dan kijkt de chat de mail na. De levering gaat als beurt
  naar de sessie; pas na die beurt luistert de wachter weer.
- **Sessies.** Per chat één `claude -p`-proces in stream-json dat openblijft, met de eigen tools uit
  `app/godchat_mcp.py` erbij. De startbeurt zegt wie de chat is, hoe de eigenaar heet (`routing.json` → `eigenaar`) en
  wat er in `bijzonderheden` van het project staat. "Neem over" start een chat; "Nieuwe dag" geeft alle chats een
  verse sessie.
- **Vragen.** Wat een ja vraagt, zet een chat met `vraag_eigenaar` in "Wacht op jou", met knoppen; het antwoord komt
  letterlijk als volgende beurt terug. `meld_stand` vult de statusregel en de rij van het project op het dashboard.
- **Taakchats.** Een chat voor één taak (`start_taak`, of "+ Taak" op het scherm): eigen sessie, geen wachter, open
  tot `taak_klaar`. Onder de God-chat gaan ze meestal over de app zelf.
- **Klok.** `rooster.json` en per taak `rooster/<id>.md`: op haar uur start een geplande taak een verse chat die de
  opdracht één keer doet. Een chat maakt of wijzigt een taak alleen met `plan_taak` of `wijzig_taak`; aanzetten doet de
  eigenaar. De uren gelden in `GODCHAT_TZ` (standaard Europe/Brussels).

## Werken aan de app

Lees eerst `app/CLAUDE.md`, en daar vooral "Werken aan de app": welke bestanden vanzelf opnieuw laden en welke een
herstart vragen. Herstarten gaat alleen met `python3 -I tools/god-chat/app/herstart.py`: nooit met `kill`, en start
`server.py` nooit een tweede keer naast een app die al draait. Test nooit tegen je echte store of bridge.

## Je eigen gegevens: nooit in de repo

Dit staat in `.gitignore` en hoort bij jou, niet bij de kit:

- `routing.json`: je naam, je nummer, je projecten en hun WhatsApp-chats;
- `rooster.json` en `rooster/`: je geplande taken en hun opdrachten;
- `app/config.json`: poort, model en effort per chat;
- `state/`: de stand van de wachters;
- `app/data/`: vragen, verloop, gesprekken, archief en logs.

Ook van jou: je projectmappen in de werkmap (met `CLAUDE.md`, `LOG.md` en `CHAT.md`), een `LOG.md` die een taakchat
van de God-chat in deze map schrijft, en `~/.config/godchat/` (het wachtwoord van het slot, de sleutels van de
meldingen). Zet daar nooit iets van in een commit, een issue of een bericht, en toon nooit het token van de bridge
(`.bridge-token` in de store).
