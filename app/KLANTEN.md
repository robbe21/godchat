# Klanten in één blik (het blok op het dashboard)

Per klant (een project onder `klanten` in `../routing.json`) één rij op het dashboard, onder de vraagkaarten, in de
volgorde van `routing.json`. Betekenis komt van de chat, feiten van de app: de klantchat leest alles van zijn klant en
meldt zelf het nieuws; de app toont daarnaast alleen wat ze zeker weet. Elk cijfer klopt of ontbreekt.

| Bestand | Wat |
|---|---|
| `klanten.py` | de logica (stdlib, leest alleen): `bouw()` voor `/api/klanten`, `meld()` voor wat de chats melden, de opschriften, `--zelftest-kort` |
| `klanten_lader.py` | laadt `klanten.py` en herlaadt het zodra het wijzigt; dient `/api/klanten` en `/klanten.js`; vangt elke fout af |
| `klanten.js` | het scherm: één IIFE met eigen CSS (`kl-`), haalt elke 30 s op, tekent `#klanten` en de kaart "Werktijd per klant" |
| `data/klantblik.json` | wat de chats meldden, per klant |
| `data/vraagkort.json` | per vraag het opschrift van het kleine kaartje, of waarom er geen is |

**Wat een rij toont.** Naam met een stip (oranje: er wacht een vraag of iets ligt bij jou; blauw: een chat van die klant
werkt) en rechts wanneer de chat het laatst iets meldde. Klanten waar iets op jou wacht staan bovenaan. Een tik klapt
uit: alles bij jou, het nieuws, wat volgt, de open taakchats, het laatste bericht per WhatsApp-gesprek (wie, richting,
wanneer), de meter met bron en dag, en wat het systeem voor die klant deed (werktijd zonder hartslagen, afgeronde
taken).

**Wat de chats melden** (`meld_stand`, alle velden optioneel; de regels staan in de beschrijving van de tool en in
`klanten.py` → `_meld`): `nieuws` (één zin, hoogstens 90 tekens, weg na 7 dagen), `volgende` (+ `volgende_datum`),
`bij_jou` (hoogstens twee dingen die alleen de eigenaar kan), `cijfer` (`waarde`, `van`, `ook`, `bij`) en `wis`. De
teksten spreken de eigenaar aan ("je besliste"): een zin óver de eigenaar ("de eigenaar …" of de naam uit
`routing.json`) wordt geweigerd. De app weigert per veld met één zin en antwoordt een klantchat altijd met wat er op het
bord staat. Een taakchat meldt alleen `nieuws` (ook via `taak_klaar`); de God-chat niets.

**De meter** is optioneel, per klant in `routing.json`:

    "meter": {"label": "plaatsen workshop", "eenheid": "", "van": 12, "ook_label": "op de wachtlijst",
              "bron": "inschrijvingen voor de eerstvolgende workshop, uit het inschrijvingsformulier"}

`label` zegt wat het getal telt, `eenheid` staat ervoor (bv. "€"), `van` is het geheel (null als er geen is: dan is het
een los getal in plaats van een deel), `ook_label` noemt een tweede getal, `bron` zegt de chat waar het getal staat. De
chat meldt alleen het getal (`cijfer`), en alleen als hij het in die beurt uit de bron las. Zonder `meter` heeft de
klant geen meter en weigert de app een cijfer.

**Wat de app zelf weet.** Open vragen en toestand uit `app.stand()`; afgeronde taken uit `data/stand.json`; werktijd
uit `data/verloop.jsonl` (een beurt na een hartslag telt niet); het laatste contact uit de WhatsApp-store
(alleen-lezen, hoogstens elke 3 min); `laatste_werk` uit de nieuwste kop van de `LOG.md`'s van de klant (`map` en `ook`
in `routing.json`). Elke bron in een eigen `try`: een kapotte bron geeft een leeg vak, nooit een leeg scherm.

## Opschriften van de vraagkaartjes

Op het dashboard staat elke open vraag als klein kaartje met een paar woorden. Die kiest een kleine aanroep:
`claude -p --model haiku` zonder tools, met dezelfde login als de chats, vanuit een lege map. Een wachter in
`klanten.py` kijkt of `data/vragen.json` wijzigde en vraagt per nieuwe vraag een opschrift (hoogstens twee aanroepen
per vraag, hoogstens `KORT_PER_UUR` per uur; een vraag korter dan `KORT_VANAF` tekens krijgt er geen). Het model mag
niet vrij herschrijven: het wijst alleen een stuk aan, en `kort_keur()` knipt dat stuk letterlijk uit de vraag en
keurt het (aaneengesloten, echt korter, geen ontkenning of voorwaarde die erbuiten valt, en wat niet terug kan,
zoals sturen, live zetten of betalen, blijft erin). Mislukt het, dan staat de vraag zelf op het kaartje. Uitzetten:
`KORT_AAN = False`. Let op: ook het nepscherm vraagt opschriften aan voor zijn (verzonnen) vragen als die lang genoeg
zijn.

**Testen.** `python3 -I tools/god-chat/app/klanten.py --zelftest-kort` (roept het model nooit aan). Het hele blok op
een eigen map: `GODCHAT_KLANT_BRON=<map>` met `data/`, `routing.json`, `messages.db` en `Work/` zoals
`klanten.bron_uit_map` ze verwacht.
