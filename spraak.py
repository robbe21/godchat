"""Zet WhatsApp-spraakberichten om in tekst. Draai met de Python die mlx-whisper heeft: standaard
~/.venvs/mlx-whisper/bin/python, of het pad in GODCHAT_WHISPER_PY.

Gebruik: spraak.py <audiobestand> [<audiobestand> ...]
De taal wordt per bestand herkend (talen door elkaar gaat dus goed).
"""
import sys

import mlx_whisper

MODEL = "mlx-community/whisper-large-v3-turbo"


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for pad in sys.argv[1:]:
        uit = mlx_whisper.transcribe(pad, path_or_hf_repo=MODEL, condition_on_previous_text=False)
        print(f"{pad}\n{uit['text'].strip()}\n", flush=True)


if __name__ == "__main__":
    main()
