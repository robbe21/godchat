"""Tests voor 'zuinig met context' in motor.py: samenvatten tussen twee beurten, opnieuw proberen na
een 529, en de ochtendstart van de klok (nieuwe_dag met vragen_open en alleen_als_stil).

Raakt niets echts: DATA en ROUTING wijzen naar een tijdelijke map (een verzonnen routing.json met de projecten
bakkerij en studio), de sessie is een namaak die de stroom van `claude -p` nabootst, en Chat.start_wachter is
uitgeschakeld (anders neemt een test de echte chats over).
Draaien: python3 -I tools/god-chat/app/test_zuinig.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import motor  # noqa: E402

GESTUURD = []        # (chat, tekst) zoals de namaak-sessie ze kreeg
SCRIPT = {}          # chat -> lijst van antwoorden (functie tekst -> lijst events), één per stuur


class NepSessie:
    def __init__(self, chat, resume):
        self.chat, self.sig, self.weg = chat, tuple(chat.motor.cfg(chat.id).get(k) for k in ("model", "effort")), False
        chat.verwerk({"type": "system", "subtype": "init", "session_id": resume or f"s-{chat.id}"})

    def leeft(self):
        return not self.weg

    def stuur(self, tekst):
        GESTUURD.append((self.chat.id, tekst))
        rij = SCRIPT.get(self.chat.id) or []
        events = rij.pop(0)(tekst) if rij else antwoord(tekst, 50_000)

        def speel():
            for e in events:
                time.sleep(0.01)
                self.chat.verwerk(e)
        threading.Thread(target=speel, daemon=True).start()

    def stop(self):
        self.weg = True


def antwoord(tekst, ctx, fout=None):
    if tekst.startswith("/compact"):
        return [{"type": "system", "subtype": "status", "status": "compacting"},
                {"type": "system", "subtype": "compact_boundary",
                 "compact_metadata": {"trigger": "manual", "pre_tokens": ctx, "post_tokens": 31_000}},
                {"type": "result", "subtype": "success", "is_error": False, "result": "", "usage": {},
                 "total_cost_usd": 1.0, "num_turns": 0}]
    if fout:
        return [{"type": "result", "subtype": "success", "is_error": True, "result": fout, "usage": {},
                 "total_cost_usd": 1.0, "num_turns": 1}]
    return [{"type": "assistant", "message": {"content": [{"type": "text", "text": "Gedaan."}],
                                              "usage": {"input_tokens": 5, "cache_read_input_tokens": ctx - 5,
                                                        "cache_creation_input_tokens": 0}}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Gedaan.",
             "usage": {"input_tokens": 5, "cache_read_input_tokens": ctx}, "total_cost_usd": 2.0, "num_turns": 1,
             "modelUsage": {"claude-opus-5-5": {"contextWindow": 1_000_000}}}]


class Basis(unittest.TestCase):
    def setUp(self):
        self.map = tempfile.mkdtemp(prefix="godchat-zuinig-")
        self.oud = (motor.DATA, motor.Sessie, motor.Chat.start_wachter, motor.OPNIEUW_NA_S, motor.COMPACT_NA_VRAAG_S,
                    motor.ROUTING)
        motor.DATA = self.map
        motor.ROUTING = os.path.join(self.map, "routing.json")
        import klok_lader   # de klok (ouder_regel bij elke levering) schrijft haar stand ook in de tijdelijke map
        self.klok_oud = dict(klok_lader.staat)
        klok_lader.staat.update(stand=os.path.join(self.map, "klok.json"), rooster=os.path.join(self.map, "rooster.json"))
        with open(motor.ROUTING, "w", encoding="utf-8") as fh:
            json.dump({"eigenaar": "Anna", "zelf": "999000000001@s.whatsapp.net", "klanten": {
                "bakkerij": {"naam": "Bakkerij Korrel", "map": "projecten/bakkerij", "whatsapp": {}},
                "studio": {"naam": "Studio Noord", "map": "projecten/studio", "whatsapp": {}}}, "bezet": {}}, fh)
        motor.Sessie = NepSessie
        motor.Chat.start_wachter = lambda self, hervat=False: None
        motor.OPNIEUW_NA_S = (0.2, 0.4)
        GESTUURD.clear()
        SCRIPT.clear()
        self.m = motor.Motor({"poort": 1, "chats": {}, "compact": {"aan": True, "pct": 50, "tokens": 200_000}})
        for cid in self.m.chats:
            self.m.zet_chat(cid, in_app=True, sessie=f"s-{cid}")

    def tearDown(self):
        self.m.stopt = True
        time.sleep(1.2)
        (motor.DATA, motor.Sessie, motor.Chat.start_wachter, motor.OPNIEUW_NA_S, motor.COMPACT_NA_VRAAG_S,
         motor.ROUTING) = self.oud
        import klok_lader
        klok_lader.staat.clear()
        klok_lader.staat.update(self.klok_oud)
        shutil.rmtree(self.map, ignore_errors=True)

    def wacht_stil(self, chat, t=10):
        einde = time.time() + t
        time.sleep(0.3)
        while time.time() < einde:
            if chat.toestand() != "werkt" and not chat.wachtrij:
                time.sleep(0.3)
                if chat.toestand() != "werkt" and not chat.wachtrij:
                    return
            time.sleep(0.05)
        self.fail(f"{chat.id} werd niet stil: {chat.toestand()}")

    def naar(self, cid):
        return [t for c, t in GESTUURD if c == cid]


class Compacten(Basis):
    def test_boven_drempel_samenvatten_en_herinnering(self):
        c = self.m.chats["god"]
        SCRIPT["god"] = [lambda t: antwoord(t, 600_000)]
        c.beurt("eigenaar", "eerste")
        self.wacht_stil(c)
        stuurde = self.naar("god")
        self.assertEqual(stuurde[0], "eerste")
        self.assertTrue(stuurde[1].startswith("/compact "), stuurde)
        self.assertEqual(c.st()["ctx"], 31_000)
        self.assertEqual(c.st()["venster"], 1_000_000)
        self.assertTrue(c.st().get("compact_op"))
        self.assertNotIn("lopend", {k for k, v in c.st().items() if v})
        self.assertTrue(any("gesprek samengevat: 600k → 31k" in e["kop"] for e in self.m.verloop))
        self.m.vragen.append({"nr": 1, "chat": "god", "status": "open", "vraag": "Mag het mailtje weg?", "uur": "09:00",
                              "ts": (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds"),
                              "context": "Ja = mail aan Noor", "concept": "Dag Noor"})
        c.beurt("eigenaar", "tweede")
        self.wacht_stil(c)
        tweede = self.naar("god")[2]
        self.assertTrue(tweede.startswith("tweede\n\n[GodChat-app] Je gesprek is net samengevat"), tweede)
        self.assertIn("GOD.md", tweede)
        self.assertIn("«Mag het mailtje weg?»", tweede)
        c.beurt("eigenaar", "derde")
        self.wacht_stil(c)
        self.assertEqual(self.naar("god")[3], "derde")   # de herinnering komt maar één keer
        # een antwoord op een vraag van vóór de samenvatting draagt het concept mee
        self.m.antwoord(1, "Ja")
        self.wacht_stil(c)
        self.assertIn("Concept:\nDag Noor", self.naar("god")[4])

    def test_onder_drempel_niets(self):
        c = self.m.chats["bakkerij"]
        SCRIPT["bakkerij"] = [lambda t: antwoord(t, 150_000)]
        c.beurt("eigenaar", "hoi")
        self.wacht_stil(c)
        self.assertEqual(self.naar("bakkerij"), ["hoi"])

    def test_verse_vraag_houdt_tegen(self):
        c = self.m.chats["bakkerij"]
        self.m.vragen.append({"nr": 2, "chat": "bakkerij", "status": "open", "vraag": "Live?", "ts": motor.nu_iso()})
        SCRIPT["bakkerij"] = [lambda t: antwoord(t, 600_000)]
        c.beurt("eigenaar", "hoi")
        self.wacht_stil(c)
        self.assertEqual(self.naar("bakkerij"), ["hoi"])

    def test_uit_in_config(self):
        self.m.config["compact"]["aan"] = False
        c = self.m.chats["bakkerij"]
        SCRIPT["bakkerij"] = [lambda t: antwoord(t, 900_000)]
        c.beurt("eigenaar", "hoi")
        self.wacht_stil(c)
        self.assertEqual(self.naar("bakkerij"), ["hoi"])

    def test_drempel_pct_of_tokens(self):
        c = self.m.chats["bakkerij"]
        self.assertEqual(c.compact_drempel(), 200_000)
        self.m.config["compact"] = {"aan": True, "pct": 50}
        self.assertEqual(c.compact_drempel(), 500_000)

    def test_taakchat_ook(self):
        t = self.m.nieuwe_taak("bakkerij", "proef", "Doe iets kleins.")
        tid = t["sleutel"] if isinstance(t, dict) and "sleutel" in t else next(
            c.id for c in self.m.chats.values() if c.soort == "taak")
        c = self.m.chats[tid]
        self.wacht_stil(c)
        SCRIPT[tid] = [lambda x: antwoord(x, 450_000)]
        c.beurt("eigenaar", "verder")
        self.wacht_stil(c)
        stuurde = self.naar(tid)
        self.assertTrue(stuurde[-1].startswith("/compact "), stuurde)
        c.beurt("eigenaar", "nog")
        self.wacht_stil(c)
        self.assertIn("Je opdracht (uit je startbeurt):\nDoe iets kleins.", self.naar(tid)[-1])


class Opnieuw529(Basis):
    def test_twee_keer_opnieuw_dan_goed(self):
        c = self.m.chats["god"]
        fout = "API Error: Repeated 529 Overloaded errors. The API is at capacity"
        SCRIPT["god"] = [lambda t: antwoord(t, 0, fout), lambda t: antwoord(t, 0, fout), lambda t: antwoord(t, 60_000)]
        c.beurt("eigenaar", "hallo")
        self.wacht_stil(c)
        stuurde = self.naar("god")
        self.assertEqual(stuurde[0], "hallo")
        self.assertEqual(stuurde[1:], [motor.OPNIEUW_TEKST] * 2)
        self.assertIsNone(c.fout)
        self.assertFalse(any(e["soort"] == "fout" for e in self.m.verloop))

    def test_derde_keer_fout(self):
        c = self.m.chats["god"]
        fout = "API Error: Repeated 529 Overloaded errors."
        SCRIPT["god"] = [lambda t: antwoord(t, 0, fout)] * 3
        c.beurt("eigenaar", "hallo")
        self.wacht_stil(c)
        self.assertEqual(len(self.naar("god")), 3)
        self.assertIn("529", c.fout or "")

    def test_andere_fout_niet_opnieuw(self):
        c = self.m.chats["god"]
        SCRIPT["god"] = [lambda t: antwoord(t, 0, "API Error: 400 invalid request")]
        c.beurt("eigenaar", "hallo")
        self.wacht_stil(c)
        self.assertEqual(len(self.naar("god")), 1)
        self.assertTrue(c.fout)


class Ochtend(Basis):
    def test_vragen_open_en_in_startbeurt(self):
        self.m.vragen.append({"nr": 3, "chat": "bakkerij", "status": "open", "vraag": "Reviews-tab live?",
                              "ts": "2026-11-22T18:00:00+01:00", "concept": "Bericht aan Tom"})
        uit = self.m.nieuwe_dag(vragen_open=True, alleen_als_stil=True)
        self.assertTrue(uit["ok"])
        self.assertGreaterEqual(uit["chats"], 2)
        self.assertEqual(self.m.vragen[-1]["status"], "open")
        c = self.m.chats["bakkerij"]
        self.wacht_stil(c)
        self.assertIn("«Reviews-tab live?»", self.naar("bakkerij")[0])
        self.m.antwoord(3, "Ja, zet live")
        self.wacht_stil(c)
        self.assertIn("Concept:\nBericht aan Tom", self.naar("bakkerij")[-1])

    def test_bezig_dan_niets(self):
        c = self.m.chats["god"]
        SCRIPT["god"] = [lambda t: (time.sleep(1.5), antwoord(t, 10_000))[1]]
        c.beurt("eigenaar", "lang werk")
        time.sleep(0.3)
        uit = self.m.nieuwe_dag(vragen_open=True, alleen_als_stil=True)
        self.assertEqual(uit, {"ok": False, "bezig": ["God-chat"]})
        self.wacht_stil(c)

    def test_ochtend_vat_samen_in_plaats_van_vers(self):
        c, k = self.m.chats["bakkerij"], self.m.chats["studio"]
        SCRIPT["bakkerij"] = [lambda t: antwoord(t, 120_000)]
        SCRIPT["studio"] = [lambda t: antwoord(t, 40_000)]
        c.beurt("eigenaar", "gisteren")
        k.beurt("eigenaar", "gisteren")
        self.wacht_stil(c)
        self.wacht_stil(k)
        self.m.vragen.append({"nr": 5, "chat": "bakkerij", "status": "open", "vraag": "Live?",
                              "ts": "2026-11-22T18:00:00+01:00"})
        uit = self.m.ochtend(vragen_open=True, alleen_als_stil=True)
        self.assertEqual(uit, {"ok": True, "chats": 1})
        self.wacht_stil(c)
        self.assertTrue(self.naar("bakkerij")[-1].startswith("/compact "))
        self.assertEqual(self.naar("studio"), ["gisteren"])          # onder 60k: niets
        self.assertEqual(c.st()["sessie"], "s-bakkerij")               # dezelfde sessie, geen verse start
        self.assertEqual(self.m.vragen[-1]["status"], "open")
        self.assertTrue(os.listdir(os.path.join(self.map, "archief")))
        self.assertFalse(any(b.get("tekst") == "gisteren" for b in c.beurten))
        c.beurt("wachter", "NIEUW")
        self.wacht_stil(c)
        self.assertIn("«Live?»", self.naar("bakkerij")[-1])

    def test_ochtend_wacht_als_bezig(self):
        c = self.m.chats["god"]
        SCRIPT["god"] = [lambda t: (time.sleep(1.5), antwoord(t, 10_000))[1]]
        c.beurt("eigenaar", "lang werk")
        time.sleep(0.3)
        self.assertEqual(self.m.ochtend(vragen_open=True, alleen_als_stil=True), {"ok": False, "bezig": ["God-chat"]})
        self.wacht_stil(c)

    def test_knop_blijft_zoals_hij_was(self):
        self.m.vragen.append({"nr": 4, "chat": "bakkerij", "status": "open", "vraag": "x", "ts": motor.nu_iso()})
        self.m.nieuwe_dag()
        self.assertEqual(self.m.vragen[-1]["status"], "vervallen")


if __name__ == "__main__":
    unittest.main(verbosity=2)
