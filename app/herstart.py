#!/usr/bin/env python3
"""Herstart de GodChat-app veilig: wacht tot geen enkele chat werkt, ook geen achtergrond-subagent, en start dan opnieuw.

  python3 -I tools/god-chat/app/herstart.py [--bericht "<tekst na de herstart>"] [--naar <chat>[,<chat>…]] [--max-min 90]

Het bericht gaat na de herstart als beurt naar de God-chat, of met --naar naar andere chats (bv. de taakchat die de
herstart vroeg en het resultaat wil nakijken; meerdere met komma's). Vlak voor het stoppen laadt hij de nieuwe code één keer op proef
(motor.py en server.py importeren, niets starten): laadt die niet, dan blijft de oude app draaien.

Maakt zichzelf los van de chat die hem start (die chat stopt mee bij de herstart). Rustig = geen chat op "werkt" én
drie minuten lang geen regel bijgeschreven in de transcripties van de sessies in de app (subagents schrijven daar ook).
Lukt dat niet binnen --max-min, dan herstart hij niet en zegt hij de God-chat wie hem tegenhield (elk halfuur staat
dat ook in het logboek). Logboek: data/logs/herstart.log. Stop de app nooit met kill.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HIER = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(os.path.dirname(os.path.dirname(HIER)))
LOG = os.path.join(HIER, "data", "logs", "herstart.log")
TRANSCRIPTS = os.path.expanduser("~/.claude/projects/" + re.sub(r"[^A-Za-z0-9]", "-", WORK))
STIL = 180


def log(tekst):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {tekst}\n")


def api(poort, pad, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{poort}{pad}", data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def laatste_per_chat():
    """Per chat in de app: het tijdstip van de laatste regel in zijn transcriptie of in die van zijn subagents."""
    try:
        with open(os.path.join(HIER, "data", "stand.json"), encoding="utf-8") as fh:
            chats = json.load(fh)["chats"]
    except (OSError, ValueError):
        return {}
    uit = {}
    for cid, c in chats.items():
        s = c.get("sessie")
        if not s or not c.get("in_app"):
            continue
        for f in glob.glob(os.path.join(TRANSCRIPTS, s + ".jsonl")) + glob.glob(os.path.join(TRANSCRIPTS, s, "**", "*"), recursive=True):
            if os.path.isfile(f):
                uit[cid] = max(uit.get(cid, 0), os.path.getmtime(f))
    return uit


def stil_sinds():
    """Seconden sinds de laatste regel in een transcriptie van een sessie in de app."""
    laatste = laatste_per_chat()
    return time.time() - max(laatste.values()) if laatste else STIL


def tegenhouders(werkt):
    """Wie houdt de herstart nu tegen: chats in een beurt, en chats (of hun subagents) die net nog schreven."""
    nu = time.time()
    schrijft = sorted(cid for cid, t in laatste_per_chat().items() if nu - t < STIL)
    return f"in een beurt: {', '.join(werkt) or 'niemand'}; schreef in de laatste {STIL // 60} min: {', '.join(schrijft) or 'niemand'}"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bericht", default="")
    p.add_argument("--naar", default="god", help="de chat(s) die het bericht krijgen, met komma's (standaard de God-chat)")
    p.add_argument("--max-min", type=int, default=90)
    p.add_argument("--los", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args()
    if not a.los:
        subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--los", "--bericht", a.bericht, "--naar", a.naar, "--max-min", str(a.max_min)],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        print(f"De herstart is ingepland: zodra alle chats drie minuten stil zijn (hoogstens {a.max_min} min wachten). Log: {LOG}")
        return
    try:
        with open(os.path.join(HIER, "config.json"), encoding="utf-8") as fh:
            poort = json.load(fh).get("poort", 8742)
    except FileNotFoundError:   # zonder config.json draait de app op de standaardpoort (server.py)
        poort = 8742
    log(f"ingepland (bericht: {'ja' if a.bericht else 'nee'})")
    einde = time.time() + a.max_min * 60
    volgende_log = time.time() + 1800
    while True:
        try:
            werkt = [c["id"] for c in api(poort, "/api/stand")["chats"] if c["toestand"] == "werkt"]
        except OSError:
            werkt = []
        if not werkt and stil_sinds() >= STIL:
            break
        if time.time() > einde:
            wie = tegenhouders(werkt)
            log(f"niet herstart: na {a.max_min} min nog niet stil ({wie})")
            try:   # niet stil opgeven: de God-chat bewaakt de app en beslist wat er nu gebeurt
                api(poort, "/api/door", {"naar": "god", "tekst": (
                    "[herstart-script, niet van de eigenaar] De ingeplande herstart van de app is NIET gebeurd: na "
                    f"{a.max_min} min was het nog geen drie minuten stil ({wie}). Er wachten mogelijk chats en de "
                    "eigenaar op. Kijk wie hem tegenhoudt, plan hem opnieuw in of leg de eigenaar de keuze voor.")})
            except OSError:
                pass
            return
        if time.time() > volgende_log:
            log(f"wacht nog ({tegenhouders(werkt)})")
            volgende_log = time.time() + 1800
        time.sleep(10)
    # Proef vóór het stoppen: laadt de code die nu op schijf staat? Zo niet, dan blijft de oude app draaien.
    try:
        proef = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import motor, server", HIER],
                               cwd=WORK, capture_output=True, text=True, timeout=60, env=dict(os.environ, NO_COLOR="1"))
        fout = "" if proef.returncode == 0 else (proef.stderr.strip().splitlines() or ["onbekende fout"])[-1]
    except (OSError, subprocess.TimeoutExpired) as uitz:
        fout = f"{type(uitz).__name__}: {uitz}"
    if fout:
        log(f"niet herstart: de nieuwe code laadt niet ({fout[:300]})")
        return
    pids = subprocess.run(["lsof", "-nP", f"-tiTCP:{poort}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
    for pid in pids:
        os.kill(int(pid), 15)
    for _ in range(45):
        if not subprocess.run(["lsof", "-nP", f"-tiTCP:{poort}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.strip():
            break
        time.sleep(1)
    else:
        for pid in pids:
            try:
                os.kill(int(pid), 9)
            except OSError:
                pass
        time.sleep(1)
    log(f"gestopt: {pids}")
    uit = open(os.path.join(HIER, "data", "logs", "server.log"), "a")
    subprocess.Popen([sys.executable, "-I", os.path.join(HIER, "server.py")], cwd=WORK, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=uit, stderr=uit, close_fds=True)
    for _ in range(30):
        time.sleep(1)
        try:
            api(poort, "/api/stand")
            break
        except OSError:
            continue
    else:
        log("FOUT: de app antwoordt niet na de herstart (zie server.log)")
        return
    log("gestart")
    if a.bericht:
        time.sleep(20)
        for naar in [n.strip() for n in a.naar.split(",") if n.strip()]:
            try:
                api(poort, "/api/door", {"naar": naar, "tekst": a.bericht})
                log(f"bericht bij {naar}")
            except OSError as fout:   # ook een 400 (onbekende of gesloten chat): de rest krijgt het wel
                log(f"bericht niet afgeleverd bij {naar}: {fout}")


if __name__ == "__main__":
    main()
