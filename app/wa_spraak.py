#!/usr/bin/env python3
"""Eén WhatsApp-spraakbericht uitschrijven vanaf de commandline, met het geheugen van de WhatsApp-tab.

  python3 -I tools/god-chat/app/wa_spraak.py <chat_jid> <bericht-id>

Drukt alleen de tekst af, op één regel. Gebruikt `wa.uitschrijven`: staat de tekst al in `data/wa/spraak.json` (door de
tab of door een eerdere levering), dan komt hij daaruit; anders haalt de bridge het geluid op en schrijft mlx-whisper
het uit, lokaal en in ±5 seconden. `tools/god-chat/wacht.py` roept dit aan voor elk spraakbericht in een levering, zodat
een chat de tekst meteen leest. Verstuurt niets en wijzigt niets buiten dat ene geheugenbestand.
"""
import os
import sys
import threading

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HIER)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    import wa
    wa.STAAT.setdefault("slot_spraak", threading.Lock())   # in de app zet de lader dit slot; hier draaien we alleen
    try:
        print(" ".join(str(wa.uitschrijven(sys.argv[1], sys.argv[2])["tekst"]).split()))
    except wa.Fout as fout:
        sys.exit(f"niet uitgeschreven: {fout}")


if __name__ == "__main__":
    main()
