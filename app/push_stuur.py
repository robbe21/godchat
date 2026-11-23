#!/usr/bin/env python3
"""Verstuurt één melding via Web Push. Het enige deel van de meldingen dat pakketten gebruikt (pywebpush).

Draait nooit in de app zelf, wel als los proces met de Python van de eigen omgeving:
    ~/.venvs/godchat-push/bin/python push_stuur.py --sleutel <map>   maakt de sleutel van de app als hij er niet is
    ~/.venvs/godchat-push/bin/python push_stuur.py                   leest één opdracht (JSON) van stdin

Het versleutelen (RFC 8291) en ondertekenen (VAPID, RFC 8292) doet pywebpush; hier staat geen eigen cryptografie.
Antwoordt altijd met één regel JSON op stdout. De private sleutel, de sleutels van een toestel en de ondertekening
komen nooit in dat antwoord en nooit op stderr.
"""
import json
import os
import sys

SLEUTEL = "vapid-prive.pem"


def _uit(**velden):
    print(json.dumps(velden, ensure_ascii=False), flush=True)
    return 0 if velden.get("ok") else 1


def _publiek(vapid):
    """De publieke sleutel zoals de browser hem vraagt (applicationServerKey), afgeleid door py_vapid/cryptography."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import b64urlencode
    ruw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64urlencode(ruw)


def sleutel(map_):
    """Maak de sleutel van de app als hij er nog niet is (alleen leesbaar voor je eigen gebruiker) en zeg de publieke
    helft."""
    from py_vapid import Vapid02
    os.makedirs(map_, mode=0o700, exist_ok=True)
    pad = os.path.join(map_, SLEUTEL)
    nieuw = False
    if not os.path.isfile(pad):
        vapid = Vapid02()
        vapid.generate_keys()
        fd = os.open(pad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # bestaat hij intussen toch: fout, niet overschrijven
        with os.fdopen(fd, "wb") as fh:
            fh.write(vapid.private_pem())
        nieuw = True
    vapid = Vapid02.from_file(pad)
    return _uit(ok=True, publiek=_publiek(vapid), nieuw=nieuw)


def stuur(opdracht):
    from pywebpush import WebPushException, webpush
    ab = opdracht["abonnement"]
    koppen = {"Urgency": str(opdracht.get("urgentie") or "normal")}
    if opdracht.get("topic"):
        koppen["Topic"] = str(opdracht["topic"])
    try:
        antwoord = webpush(
            subscription_info={"endpoint": ab["endpoint"], "keys": {"p256dh": ab["keys"]["p256dh"], "auth": ab["keys"]["auth"]}},
            data=str(opdracht["bericht"]),
            vapid_private_key=str(opdracht["sleutel"]),
            vapid_claims={"sub": str(opdracht["sub"])},
            ttl=int(opdracht.get("ttl") or 0),
            timeout=float(opdracht.get("wacht") or 10),
            headers=koppen,
        )
        return _uit(ok=True, status=antwoord.status_code)
    except WebPushException as fout:
        status = fout.status_code
        tekst = ""
        try:
            tekst = " ".join(str(fout.response.text or "").split())[:200] if fout.response is not None else ""
        except Exception:
            tekst = ""
        # 404 en 410: dit toestel heeft het abonnement opgezegd; de app haalt het dan weg
        return _uit(ok=False, status=status, weg=status in (404, 410), fout=tekst or " ".join(str(fout.message).split())[:200])


def main():
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--sleutel":
            return sleutel(sys.argv[2])
        if len(sys.argv) == 1:
            return stuur(json.loads(sys.stdin.read()))
        return _uit(ok=False, status=None, fout="gebruik: push_stuur.py [--sleutel <map>]")
    except Exception as fout:   # alleen de soort fout: in de tekst van een netwerkfout staat het adres van het toestel
        return _uit(ok=False, status=None, fout=type(fout).__name__)


if __name__ == "__main__":
    sys.exit(main())
