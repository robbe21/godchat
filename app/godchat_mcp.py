#!/usr/bin/env python3
"""MCP-server (stdio) met de eigen tools van een chat in de GodChat-app.

De app start dit per chat via --mcp-config, met GODCHAT_URL, GODCHAT_CHAT en GODCHAT_SLEUTEL in de omgeving.
Elke aanroep gaat als POST naar de app (/api/tool); de app beslist en antwoordt met één tekst.
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("GODCHAT_URL", "http://127.0.0.1:8742")
CHAT = os.environ.get("GODCHAT_CHAT", "")
SLEUTEL = os.environ.get("GODCHAT_SLEUTEL", "")

TOOLS = [
    {
        "name": "vraag_eigenaar",
        "description": (
            "Zet één vraag bij de eigenaar in 'Wacht op jou' van de GodChat-app. Gebruik dit voor alles wat het ja of de "
            "keuze van de eigenaar vraagt (versturen, live zetten, geld, wissen, toegang, iets beloven). Eén vraag per "
            "beslissing. De eigenaar leest dit op de telefoon tussen ander werk door: schrijf kort en in gewone woorden, "
            "zoals je het aan tafel zou vragen. De vraag is één korte zin. Wat ja betekent en waarom staat in 'context', "
            "in hoogstens twee korte zinnen. Alles wat langer is (het bericht, de mail, de stappen) staat in 'concept'; "
            "dat klapt de eigenaar alleen open om het na te lezen, er gaat dus niets verloren. Geen sleutels, "
            "bestandsnamen of PR-nummers in de vraag zelf. De app nummert vragen alleen voor zichzelf: noem nergens in "
            "wat de eigenaar leest een vraagnummer, zeg waarover de vraag gaat ('de vraag over de bestelpagina'). Denk "
            "mee: geef in 'antwoorden' altijd de twee tot vier antwoorden die de eigenaar het waarschijnlijkst wil "
            "geven; ze worden knoppen. Na de aanroep sluit je je beurt af: het antwoord (een knop, of wat de eigenaar "
            "zelf typt) komt letterlijk als je volgende beurt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vraag": {"type": "string", "description": (
                    "De ene vraag, hoogstens vijftien woorden, die de eigenaar met ja of nee kan beantwoorden. Geen "
                    "haakjes, geen opsomming, geen bedragen of data die in het concept horen. Bv. 'De bestelpagina live "
                    "zetten en Tom het bericht sturen?'")},
                "context": {"type": "string", "description": (
                    "Hoogstens twee korte zinnen. Eerst wat er bij ja gebeurt, beginnend met 'Ja = …'. Dan, alleen als "
                    "het nodig is, waarom je dit vraagt (wie vroeg wat). Geen verslag van wat je deed.")},
                "concept": {"type": "string", "description": (
                    "Alles wat de eigenaar moet kunnen nalezen, letterlijk zoals het zou gaan: het bericht of de mail, "
                    "de stappen, de bedragen, de links. Begin met wat verstuurd of gedaan wordt; achtergrond onderaan.")},
                "antwoorden": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "string"}, "description": (
                    "Twee tot vier antwoorden die de eigenaar echt zou kunnen geven, elk een knop: hoogstens acht "
                    "woorden, in de stem van de eigenaar, en zo dat het op zichzelf duidelijk is wat het doet. Jouw "
                    "aanbeveling eerst. Niet alleen 'Ja' en 'Nee': denk aan de keuzes die er werkelijk zijn, bv. ['Ja, "
                    "stuur het zo', 'Stuur, maar zonder de korting', 'Wacht tot maandag', 'Nee, laat maar']. Een klik is "
                    "het antwoord van de eigenaar, letterlijk: een knop die iets verstuurt, live zet of geld raakt zegt "
                    "dat ook.")},
                "whatsapp": {
                    "type": "object", "additionalProperties": False, "required": ["aan", "bericht"],
                    "properties": {
                        "aan": {"type": "string", "description": "De chat_jid van het gesprek: één van jouw chats uit routing.json."},
                        "bericht": {"type": "string", "description": "De letterlijke tekst die verstuurd wordt, zonder aanhef of uitleg eromheen."},
                    },
                    "description": (
                        "Alleen als je vraagt om één WhatsApp-bericht te versturen: het gesprek en de letterlijke tekst. "
                        "De WhatsApp-tab van de app toont het dan als klad in dat gesprek, met Stuur en Pas aan. Zet "
                        "hetzelfde bericht ook in 'concept'. Tikt de eigenaar in de tab op Stuur, dan is het al verstuurd "
                        "en begint het antwoord met 'Zelf verstuurd vanuit de WhatsApp-tab': stuur het dan niet nog eens. "
                        "Komt het antwoord op de gewone manier (bv. 'Ja, stuur het zo'), dan verstuur jij zoals altijd.")},
            },
            "required": ["vraag", "antwoorden"],
            "additionalProperties": False,
        },
    },
    {
        "name": "stuur_door",
        "description": (
            "Stuur een bericht als beurt naar een andere chat in de GodChat-app (de God-chat gebruikt dit voor notities "
            "uit de zelfchat, een klantchat om wat binnenkomt bij zijn taakchat te brengen). 'naar' is de sleutel: god, "
            "de sleutel van een project uit routing.json (bv. `bakkerij`), of de sleutel van een open taakchat (bv. "
            "`bakkerij--webshop`). De tekst komt letterlijk bij die chat aan; de eigenaar ziet hem in het Verloop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "naar": {"type": "string", "description": "Sleutel van de chat."},
                "tekst": {"type": "string", "description": "Wat die chat letterlijk als beurt krijgt."},
            },
            "required": ["naar", "tekst"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_taak",
        "description": (
            "Start een taakchat: een eigen chat voor één taak, onder een klantchat. Voor werk dat meer is dan een "
            "antwoord of een korte ingreep (een feature, een pagina, een onderzoek), na een akkoord of op vraag van de "
            "eigenaar. De taakchat krijgt de opdracht, werkt zelfstandig binnen dezelfde grenzen, blijft open tot de "
            "taak af is en sluit zichzelf met taak_klaar. Je krijgt zijn sleutel terug; stuur wat er later over "
            "binnenkomt daarheen met stuur_door. Kijk eerst of er al een open taakchat voor bestaat (de lijst staat bij "
            "elke levering)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "naam": {"type": "string", "description": "Korte naam van de taak, twee tot vier woorden (bv. 'Bestelpagina teksten')."},
                "opdracht": {"type": "string", "description": (
                    "De volledige brief, op zichzelf staand: wat er gevraagd is en door wie (berichten letterlijk, met "
                    "bericht-id's en chat_jid), wat er al ligt (bestanden, branch, PR), wat klaar betekent, en wat op het "
                    "ja van de eigenaar wacht.")},
                "onder": {"type": "string", "description": "Alleen voor de God-chat: de sleutel van de klantchat waar de taak bij hoort. Weggelaten = onder de God-chat zelf (een onderwerp over de God-chat of de app)."},
            },
            "required": ["naam", "opdracht"],
            "additionalProperties": False,
        },
    },
    {
        "name": "plan_taak",
        "description": (
            "Maak een automatisering aan: werk dat op een vast uur terugkomt (elke dag, elke werkdag, elke maandag). "
            "Dit is de enige manier om in de app iets te laten terugkomen: een automatisering staat als chat onder haar "
            "project. Geen cron, launchd, /loop of wekker, en ook geen 'elke hartslag om X uur' in je CHAT.md. De app "
            "zet de taak onder jouw project (de God-chat kiest met `onder`), nog uit, met je opdracht in "
            "tools/god-chat/rooster/<id>.md, en stelt de eigenaar zelf de vraag met vaste knoppen; die tik zet ze aan. "
            "Op haar uur start de klok telkens een verse chat onder het project die de opdracht één keer doet en "
            "zichzelf sluit; jij hoort het resultaat bij je volgende levering. Vraag eerst het akkoord van de eigenaar "
            "op wat de taak doet als ze iets verstuurt, post of wijzigt: de opdracht zegt dan letterlijk wat ze zelf mag."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "naam": {"type": "string", "description": "Korte naam, twee tot zes woorden (bv. 'Wekelijks cijferoverzicht')."},
                "dagen": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7},
                          "description": "Dagen van de week: 1 = maandag … 7 = zondag. Werkdagen = [1,2,3,4,5]."},
                "uren": {"type": "array", "items": {"type": "string"},
                         "description": "Tijden in de tijdzone van het rooster als [\"09:30\"], hoogstens vier."},
                "opdracht": {"type": "string", "description": (
                    "De volledige brief die een verse chat elke keer krijgt, op zichzelf staand: het akkoord van de "
                    "eigenaar letterlijk, de werkmap en de bestanden, de stappen, wat de ronde zelf mag versturen, posten "
                    "of wijzigen (en wat niet), wanneer ze stopt, en wat er in het rapport komt.")},
                "onder": {"type": "string", "description": "Alleen voor de God-chat en zijn taakchats: de klantchat waar de taak bij hoort. Weggelaten = onder de God-chat."},
                "model": {"type": "string", "enum": ["opus", "sonnet", "haiku", "fable"], "description": "Standaard opus."},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"], "description": "Standaard medium."},
                "max_duur_min": {"type": "integer", "description": "Hoe lang één ronde hoogstens loopt, 5 tot 240. Standaard 60."},
                "koppelingen": {"type": "array", "items": {"type": "string"}, "description": "De lokale MCP-servers die de ronde nodig heeft (bv. [\"google-workspace\"]; `claude mcp list`). Ontbreken ze bij de start of moeten ze inloggen, dan verwittigt de app de eigenaar. Reken in een ronde alleen op lokale servers, niet op connectoren van claude.ai."},
            },
            "required": ["naam", "dagen", "uren", "opdracht"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wijzig_taak",
        "description": (
            "Stel voor een geplande taak (automatisering) aan te passen: de opdracht, de dagen, het uur, het model, de "
            "effort, de duur of de naam. Er verandert niets tot de eigenaar op de knop tikt; de app vraagt het zelf, met "
            "wat er nu staat en wat er komt, en voert het dan door. Een ronde past alleen haar eigen taak aan (id mag "
            "dan weg); een klantchat de taken onder zijn project; de God-chat alles. Aanzetten of uitzetten kan hier "
            "niet: dat doet de eigenaar met de schakelaar. Pas rooster.json en rooster/*.md nooit met de hand aan."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "De id van de taak (bv. 'weekoverzicht'). Weglaten in de chat van de taak zelf."},
                "opdracht": {"type": "string", "description": "De hele nieuwe opdracht, niet alleen de wijziging. Weglaten als ze blijft."},
                "dagen": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}, "description": "1 = maandag … 7 = zondag."},
                "uren": {"type": "array", "items": {"type": "string"}, "description": "Tijden in de tijdzone van het rooster als [\"10:00\"]."},
                "model": {"type": "string", "enum": ["opus", "sonnet", "haiku", "fable"]},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
                "max_duur_min": {"type": "integer", "description": "5 tot 240."},
                "naam": {"type": "string", "description": "Een nieuwe korte naam."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "taak_klaar",
        "description": (
            "Alleen voor een taakchat: sluit deze taak. Roep dit pas aan als alles gedaan is, ook de laatste stap na het "
            "ja van de eigenaar, en de LOG.md-regel geschreven is. De klantchat krijgt je samenvatting; deze chat stopt "
            "na de beurt. De eigenaar kan hem in de app heropenen."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "samenvatting": {"type": "string", "description": "Wat er nu ligt en waar, in twee of drie zinnen."},
                "nieuws": {"type": "string", "description": (
                    "Wat er door deze taak voor de klant veranderd is, in één zin van hoogstens 90 tekens, met dezelfde "
                    "regels als `nieuws` in `meld_stand`. Bv. 'Tom kan nu zelf de openingsuren aanpassen in "
                    "/admin.' Weglaten als er voor de klant niets veranderde (een onderzoek, of werk dat nog op het ja "
                    "van de eigenaar wacht) en bij een taak onder de God-chat.")},
            },
            "required": ["samenvatting"],
            "additionalProperties": False,
        },
    },
    {
        "name": "meld_stand",
        "description": (
            "Werk bij wat de eigenaar van jou en van je klant ziet. `regel` is je eigen statusregel, naast je naam. De "
            "andere velden gaan niet over jou maar over de klant: ze vullen zijn rij op het dashboard, waar de eigenaar "
            "op de telefoon in één blik wil zien wat er speelt. Schrijf ze zoals je het aan tafel zou zeggen: kort, in "
            "het Nederlands, rechtstreeks tegen de eigenaar ('je besliste', nooit 'de eigenaar besliste'). Roep dit één "
            "keer aan, aan het eind van een beurt waarin iets gebeurde, en geef een veld alleen mee als het veranderd "
            "is: wat je weglaat, blijft staan. Een hartslag of een ronde zonder werk is alleen `regel`. Nergens "
            "vraagnummers, PR-nummers, branches, links, sleutels of telefoonnummers, en geen 'vandaag', 'morgen' of "
            "'gisteren': de app zet zelf de dag erbij en haalt weg wat ouder is dan een week. Het antwoord zegt wat er "
            "nu voor jouw klant op het bord staat; klopt daar iets niet meer, zet het dan in dezelfde beurt recht. Een "
            "taakchat meldt alleen `regel` en `nieuws`; de God-chat en zijn taakchats alleen `regel`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "regel": {"type": "string", "description": (
                    "Eén zin, hoogstens 140 tekens: waar jij staat of wat je net deed. Dit is de plek voor je eigen werk "
                    "(wat je nakeek, wat klaarstaat, waarop je wacht). Geen vraagnummers: wacht er iets op de eigenaar, "
                    "zeg dan waarover ('wacht op je ja voor de bestelpagina').")},
                "nieuws": {"type": "string", "description": (
                    "Eén zin, hoogstens 90 tekens: wat er bij de klant gebeurde. De toets: zou de eigenaar dit aan een "
                    "vriend vertellen over deze klant? Het is een gebeurtenis die morgen nog waar is: iets ging live of de "
                    "deur uit, de klant vroeg, zei of besliste iets dat ertoe doet, je vond iets in hun cijfers. Liever "
                    "wat een ander deed of wat er landde dan wat de eigenaar zelf net besliste: dat is al bekend. Geen "
                    "toestand ('wacht op', 'is aan zet': dat is `volgende`), niet jouw werk, en niets dat nog op het ja "
                    "van de eigenaar wacht. Goed: 'Tom kan nu zelf de openingsuren aanpassen in /admin.' Goed: "
                    "'Tom wil de workshop voortaan ook op zondag geven.' Fout: 'PR #12 gemerged, taakchat "
                    "gesloten, wacht op Noor.' Fout: 'Niets nieuws op WhatsApp of in de mail.' Is er niets gebeurd, laat "
                    "het veld dan weg.")},
                "nieuws_datum": {"type": "string", "description": (
                    "Alleen als het niet vandaag gebeurde: de dag zelf als JJJJ-MM-DD, hoogstens zeven dagen terug.")},
                "volgende": {"type": "string", "description": (
                    "Hoogstens 60 tekens, zonder hoofdletter of punt: wat er nu eerst komt, en van wie. Schrijf geen "
                    "datum in de tekst: geef je `volgende_datum` mee, dan zet de app de dag ervoor. Goed, met datum "
                    "2026-12-05: 'de nieuwsbrief over de kerstbroden vertrekt'. Goed, zonder datum: 'Noor kiest de "
                    "beelden voor de nieuwe site'. Fout: 'maandag hartslag en mail nakijken' (jouw werk).")},
                "volgende_datum": {"type": "string", "description": (
                    "JJJJ-MM-DD in de tijdzone van het rooster, vandaag of hoogstens een maand verder. Alleen voor wat die dag vanzelf "
                    "gebeurt of vaststaat: een ingeplande verzending, een afspraak, een les. Niet voor iets wat de "
                    "eigenaar zelf nog moet doen: na die dag verdwijnt de regel en lijkt het gedaan. Weet je de dag niet "
                    "zeker, laat hem weg.")},
                "bij_jou": {"type": "array", "items": {"type": "string"}, "description": (
                    "Hoogstens twee dingen van elk hoogstens 50 tekens die alleen de eigenaar zelf kan doen, die de "
                    "eigenaar zelf toezegde of waar iemand op wacht, en waar geen knop voor kan bestaan: iemand bellen, "
                    "ergens heen gaan, iets tekenen. Geen beslissing of akkoord (dat vraag je met `vraag_eigenaar`), geen "
                    "bericht dat nog niet beantwoord is (PROTOCOL.md, Meelezen: niet melden), geen open punten uit "
                    "CLAUDE.md. Het is geen takenlijst: de eigenaar moet het kunnen lezen zonder zich iets te verwijten. "
                    "Goed: ['Bram bellen: hij zit niet op WhatsApp']. Fout: ['Lies nog antwoorden over de foto', "
                    "'De offerte nalezen (open sinds twee weken)']. Geef altijd de hele lijst. Is alles gedaan: "
                    "`wis: ['bij_jou']`.")},
                "cijfer": {"type": "object", "additionalProperties": False, "required": ["waarde"],
                           "properties": {"waarde": {"type": "number"}, "van": {"type": "number"},
                                          "ook": {"type": "number"}, "bij": {"type": "string"}},
                           "description": (
                    "Het ene vaste getal van jouw klant. Wat het telt en waar het vandaan komt staat in het antwoord van "
                    "deze tool (achter 'meter'); dat verander je niet zelf. Alleen als je het in deze beurt met je "
                    "gewone gereedschap uit die bron las, en hoogstens één keer per dag: nooit een getal uit LOG.md, "
                    "CHAT.md of je geheugen, nooit een schatting, en zoek er geen sleutels voor. `waarde` is het getal, "
                    "`van` het geheel als dat er is (12 plaatsen, 40 broden), `ook` het tweede getal dat het antwoord "
                    "noemt, `bij` in hoogstens 30 tekens waar het bij hoort als dat wisselt. Goed: {'waarde': 9, 'van': "
                    "12, 'bij': 'Workshop za 5 dec'}. Fout: {'waarde': 20} omdat de LOG van eergisteren 20 inschrijvingen "
                    "noemde.")},
                "wis": {"type": "array", "items": {"type": "string"}, "description": (
                    "Velden die van het bord af moeten omdat ze niet meer kloppen: 'nieuws', 'volgende', 'bij_jou' of "
                    "'cijfer'. Een leeg veld meegeven wist niets.")},
            },
            "required": ["regel"],
            "additionalProperties": False,
        },
    },
]



def roep_app(tool, args):
    data = json.dumps({"chat": CHAT, "sleutel": SLEUTEL, "tool": tool, "args": args}).encode()
    req = urllib.request.Request(URL + "/api/tool", data=data, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("tekst", "ok"), False
    except urllib.error.HTTPError as fout:
        return fout.read().decode(errors="replace") or str(fout), True
    except OSError as fout:
        return f"De GodChat-app is niet bereikbaar: {fout}", True


def antwoord(mid, result=None, error=None):
    bericht = {"jsonrpc": "2.0", "id": mid}
    if error:
        bericht["error"] = error
    else:
        bericht["result"] = result
    sys.stdout.write(json.dumps(bericht) + "\n")
    sys.stdout.flush()


def main():
    for regel in sys.stdin:
        regel = regel.strip()
        if not regel:
            continue
        try:
            msg = json.loads(regel)
        except ValueError:
            continue
        methode, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue   # notificaties
        if methode == "initialize":
            versie = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
            antwoord(mid, {"protocolVersion": versie, "capabilities": {"tools": {}},
                           "serverInfo": {"name": "godchat", "version": "1"}})
        elif methode == "tools/list":
            antwoord(mid, {"tools": TOOLS})
        elif methode == "tools/call":
            p = msg.get("params") or {}
            tekst, fout = roep_app(p.get("name"), p.get("arguments") or {})
            antwoord(mid, {"content": [{"type": "text", "text": tekst}], "isError": fout})
        elif methode == "ping":
            antwoord(mid, {})
        else:
            antwoord(mid, error={"code": -32601, "message": f"onbekend: {methode}"})


if __name__ == "__main__":
    main()
