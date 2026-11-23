# Meldingen op je toestellen (Web Push)

De geïnstalleerde app (Chrome op Android of op de computer) krijgt een melding als er iets op je wacht, ook als de app
dicht is. Optioneel: zonder de eigen omgeving (zie Opzetten) zijn er geen meldingen en werkt de rest gewoon.

**Wanneer.** Een nieuwe open vraag (`vraag_eigenaar`) en een chat die in fout valt. Nooit voor een bericht of een
beurt. Tussen 22:00 en 07:30 geen melding; wat dan binnenkomt en om 07:30 nog openstaat, volgt in één melding. Staat de
app op dat toestel open en in beeld, dan komt er geen melding bovenop.

**Wat erin staat.** De klant of chat als titel, en de kop van het kaartje als tekst (het opschrift uit `klanten.py`,
anders de vraag zelf; hoogstens 140 tekens, gemaskeerd). Nooit het concept of de context: het staat op een
vergrendelscherm. Een tik opent de app op die vraag (`/#vraag=<nr>`) of die chat (`/#chat=<id>`).

| Bestand | Wat |
|---|---|
| `push.py` | de logica (alleen standaardbibliotheek, laadt opnieuw zonder herstart): `ronde()` kijkt wat nieuw is en of het gemeld mag worden, `aanvraag()` verdeelt `/api/push/…`, `--zelftest` |
| `push_lader.py` | laadt `push.py` en herlaadt het zodra het wijzigt; één draad roept elke 2 s `ronde()` aan; dient `/sw.js` |
| `push_stuur.py` | verstuurt één melding met `pywebpush` (versleutelen RFC 8291, ondertekenen VAPID). Draait alleen als los proces met de Python van de eigen omgeving |
| `sw.js` | de service worker: toont de melding en opent de app bij een tik; de iconen zitten erin als data-URL. Geen `fetch`-handler, dus geen cache |
| `push_vereisten.txt` | de vaste versies van de eigen omgeving |
| `index.html` | de balk op het dashboard (één keer vragen), de regel onder de chatlijst (aan, testmelding, uit), de sprong naar een vraag of chat |

**Waar wat staat** (buiten de werkmap): `~/.config/godchat/push/`, alleen leesbaar voor je eigen gebruiker:
`vapid-prive.pem` (de sleutel van de app; komt nooit in een antwoord, logregel of chat), `toestellen.json`,
`stand.json` (welke vraag al gemeld is) en eventueel `instellingen.json` (`{"contact": "mailto:…"}`).

**Wat de computer verlaat.** Per melding één aanvraag naar de pushdienst van de browser. De inhoud is versleuteld voor
dat ene toestel; de dienst ziet alleen dát er een melding is en het contactadres uit de ondertekening (standaard geen
persoonlijk adres, `CONTACT` in `push.py`). Een toestel met een adres dat niet van een pushdienst van een browser is,
wordt geweigerd (`DIENSTEN`).

**Opzetten.** `python3 -m venv ~/.venvs/godchat-push && ~/.venvs/godchat-push/bin/pip install -r push_vereisten.txt`.
Een andere plek: zet `GODCHAT_PUSH_PY` op de Python van die omgeving. Aanzetten doe je per toestel in de app ("Een
melding op dit toestel…" op het dashboard, of "Meldingen aanzetten" onder de chatlijst), daarna de toestemming van de
browser. "Stuur een testmelding" stuurt er één naar dat toestel. Alles tegelijk uit: `AAN = False` in `push.py`.

**Het slot.** Alle adressen vallen vanzelf achter het slot. De melding zelf heeft geen login nodig; is de sessie
verlopen, dan vraagt de app na de tik om in te loggen.

**Testen.** `python3 -I tools/god-chat/app/push.py --zelftest` (verstuurt niets). Het nepscherm verstuurt nooit iets
en raakt `~/.config/godchat/push/` niet aan; met `GODCHAT_PUSH_MAP` en `GODCHAT_PUSH_PROEFDIENST` wijst een test de
sleutelmap en de pushdienst naar een eigen map en een nagemaakte dienst.

## Bekende grenzen

1. Of Android het merkje in de statusbalk toont, hangt af van hoe de app geïnstalleerd is (echte app of snelkoppeling).
2. Wat een melding waard is, staat vast: elke nieuwe vraag en elke chat in fout (`push.py` → `verwerk`).
