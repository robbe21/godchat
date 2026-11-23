"""Tests voor de klok voor geplande taken (klok.py). Draaien: python3 -I tools/god-chat/app/test_klok.py

Pure logica met een valse klok, een tijdelijke map (rooster, stand, opdrachten, desktop-roosters) en een namaak-motor.
Nooit de echte Motor met echte chats, nooit een `claude`-proces, nooit wacht.py; niets in data/ wordt geraakt.
Alleen de laatste twee tests gebruiken motor.py zelf, op een Motor zonder __init__ (geen chats, geen draden) met
paden in de tijdelijke map.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HIER)
import klok  # noqa: E402

TZ = klok.TZ


def bxl(*a):
    return datetime(*a, tzinfo=TZ)


class NepChat:
    def __init__(self, motor, cid, naam, soort, ouder=None):
        self.motor, self.id, self.naam, self.soort, self.ouder = motor, cid, naam, soort, ouder
        self.fout = None
        self.wachtrij = []
        self.beurten = []
        self.staat = "slaapt"

    def toestand(self):
        if self.soort == "taak" and self.motor.stand_chat(self.id).get("status") == "klaar":
            return "klaar"
        return self.staat


class NepMotor:
    """Dezelfde methoden die klok.py van de motor gebruikt, zonder iets te starten."""

    def __init__(self):
        self.chats = {}
        self.stand = {}
        self.vragen = []
        self.verloop = []
        self.starts = []
        self.statussen = []
        self.gooi = None
        self._slot = threading.Lock()
        for cid, naam, soort in (("god", "God-chat", "god"), ("bakkerij", "Bakkerij", "klant"), ("studio", "Studio", "klant")):
            self.chats[cid] = NepChat(self, cid, naam, soort)
            self.stand[cid] = {"in_app": True}

    def stand_chat(self, cid):
        return dict(self.stand.get(cid, {}))

    def gebeurd(self, soort, chat, route, kop, tekst="", **extra):
        self.verloop.append((soort, chat, kop, tekst))

    def taak_status(self, tid, status, samenvatting="", door="eigenaar"):
        self.statussen.append((tid, status, samenvatting, door))
        self.stand.setdefault(tid, {}).update(status=status, samenvatting=samenvatting)

    def start_ronde(self, tid, ouder, naam, tekst, ronde, model=None, effort=None):
        with self._slot:
            time.sleep(0.01)   # een trage start: twee tikken tegelijk moeten elkaar hier toch niet inhalen
            self.starts.append({"tid": tid, "ouder": ouder, "naam": naam, "tekst": tekst, "ronde": ronde,
                                "model": model, "effort": effort})
        if self.gooi:
            raise self.gooi
        self.stand[tid] = {"in_app": True, "soort": "taak", "ouder": ouder, "status": "open", "ronde": ronde,
                           "model": model, "effort": effort}
        chat = self.chats.get(tid) or NepChat(self, tid, naam, "taak", ouder)
        chat.staat = "werkt"
        self.chats[tid] = chat


class Basis(unittest.TestCase):
    def setUp(self):
        self.map = tempfile.mkdtemp(prefix="klok-test-")
        os.makedirs(os.path.join(self.map, "rooster"))
        self.desk = os.path.join(self.map, "desktop")
        self.motor = NepMotor()
        self.nu = [bxl(2026, 11, 23, 8, 0, 10)]   # maandag
        self.staat = {"slot": threading.RLock(), "app": self.motor, "rooster": os.path.join(self.map, "rooster.json"),
                      "stand": os.path.join(self.map, "data", "klok.json"), "basis": self.map, "desktop": self.desk,
                      "klok": lambda: self.nu[0]}

    def tearDown(self):
        shutil.rmtree(self.map, ignore_errors=True)

    def taak(self, **kw):
        t = {"id": "weekronde", "naam": "Weekronde advertenties", "onder": "bakkerij", "dagen": [1], "uren": ["08:00"],
             "inhalen_min": 120, "opdracht": "rooster/weekronde.md", "model": "opus", "effort": "medium",
             "max_duur_min": 120, "aan": True}
        t.update(kw)
        pad = os.path.join(self.map, t["opdracht"]) if isinstance(t.get("opdracht"), str) else None
        if pad and not os.path.exists(pad) and not kw.get("_geen_bestand"):
            with open(pad, "w", encoding="utf-8") as fh:
                fh.write(f"Opdracht van {t['id']}: doe de ronde.\n")
        t.pop("_geen_bestand", None)
        return t

    def rooster(self, *taken):
        with open(self.staat["rooster"], "w", encoding="utf-8") as fh:
            json.dump({"_": "test", "taken": list(taken)}, fh, indent=2)

    def stand(self):
        return klok.lees_stand(self.staat)

    def zet_stand(self, tid, **velden):
        d = klok.lees_stand(self.staat)
        d["taken"].setdefault(tid, {}).update(velden)
        klok.schrijf_stand(self.staat, d)

    def desktop(self, taken, oud_dagen=0, kapot=False, naam="a"):
        pad = os.path.join(self.desk, naam, "b", "scheduled-tasks.json")
        os.makedirs(os.path.dirname(pad), exist_ok=True)
        with open(pad, "w", encoding="utf-8") as fh:
            fh.write("{kapot" if kapot else json.dumps({"scheduledTasks": taken}))
        if oud_dagen:
            t = time.time() - oud_dagen * 86400
            os.utime(pad, (t, t))
        return pad

    def tik(self):
        klok.tik(self.staat)

    def laatste(self, tid="weekronde"):
        return self.stand()["taken"].get(tid, {}).get("laatste") or {}


class Tijd(Basis):
    def test_jongste_slot_dag_en_week(self):
        t, _ = klok.ontleed(self.taak(), self.motor, self.map)
        self.assertEqual(klok.jongste_slot(t, bxl(2026, 11, 22, 20, 0)), bxl(2026, 11, 16, 8, 0))   # zondag → vorige maandag
        self.assertEqual(klok.jongste_slot(t, bxl(2026, 11, 23, 7, 59)), bxl(2026, 11, 16, 8, 0))
        self.assertEqual(klok.jongste_slot(t, bxl(2026, 11, 23, 8, 0)), bxl(2026, 11, 23, 8, 0))
        t2, _ = klok.ontleed(self.taak(dagen=[1, 2, 3, 4, 5], uren=["23:30"]), self.motor, self.map)
        self.assertEqual(klok.jongste_slot(t2, bxl(2026, 11, 24, 0, 10)), bxl(2026, 11, 23, 23, 30))   # over middernacht
        self.assertEqual(klok.jongste_slot(t2, bxl(2026, 11, 30, 9, 0)), bxl(2026, 11, 27, 23, 30))    # over het weekend
        self.assertEqual(klok.volgend_slot(t2, bxl(2026, 11, 27, 23, 31)), bxl(2026, 11, 30, 23, 30))

    def test_zomer_en_wintertijd(self):
        t, _ = klok.ontleed(self.taak(dagen=[1, 2, 3, 4, 5, 6, 7], uren=["08:00"], inhalen_min=2160), self.motor, self.map)
        # 25 okt 2026: 03:00 zomertijd → 02:00 wintertijd
        voor = klok.jongste_slot(t, datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc))
        na = klok.jongste_slot(t, datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(voor.astimezone(timezone.utc).hour, 6)   # 08:00 CEST
        self.assertEqual(na.astimezone(timezone.utc).hour, 7)     # 08:00 CET
        zondag = klok.jongste_slot(t, datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc))
        self.assertEqual((zondag - voor), timedelta(hours=25))
        self.assertEqual(klok.venster_einde(t, voor), zondag)   # 36 u inhalen, maar hoogstens tot het volgende uur
        # 29 mrt 2026: 02:00 → 03:00; 02:30 bestaat die dag niet: geen crash, en het moment ligt vóór "nu"
        t2, _ = klok.ontleed(self.taak(dagen=[7], uren=["02:30"]), self.motor, self.map)
        s = klok.jongste_slot(t2, bxl(2026, 3, 29, 4, 0))
        self.assertIsNotNone(s)
        self.assertEqual(s.date().isoformat(), "2026-03-29")
        self.assertLessEqual(s, bxl(2026, 3, 29, 4, 0))
        self.assertEqual(klok.jongste_slot(t2, bxl(2026, 3, 29, 1, 59)).date().isoformat(), "2026-03-22")

    def test_meerdere_uren_venster_tot_volgend_uur(self):
        t, _ = klok.ontleed(self.taak(uren=["12:00", "08:00"], inhalen_min=600), self.motor, self.map)
        self.assertEqual(t["uren"], ["08:00", "12:00"])
        self.assertEqual(klok.venster_einde(t, bxl(2026, 11, 23, 8, 0)), bxl(2026, 11, 23, 12, 0))
        self.rooster(self.taak(uren=["08:00", "12:00"], inhalen_min=600))
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        self.nu[0] = bxl(2026, 11, 23, 11, 59)
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)
        self.assertEqual(self.laatste()["slot"], "2026-11-23T08:00:00+01:00")
        self.motor.chats["bakkerij--r-weekronde"].staat = "slaapt"
        self.motor.stand["bakkerij--r-weekronde"]["status"] = "klaar"
        self.nu[0] = bxl(2026, 11, 23, 12, 0, 20)
        self.tik()
        self.assertEqual(len(self.motor.starts), 2)
        self.assertEqual(self.laatste()["slot"], "2026-11-23T12:00:00+01:00")


class Tik(Basis):
    def test_niets_voor_aan_sinds(self):
        self.rooster(self.taak(aan=False))
        self.nu[0] = bxl(2026, 11, 23, 8, 30)
        klok.zet(self.staat, "weekronde", True)
        self.tik()
        self.assertEqual(self.motor.starts, [])
        self.nu[0] = bxl(2026, 11, 30, 8, 0, 5)
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)

    def test_met_de_hand_aangezet_geldt_als_nu(self):
        self.rooster(self.taak(aan=True))   # geen aan_sinds in de stand: de eerste tik zet hem
        self.nu[0] = bxl(2026, 11, 23, 8, 30)
        self.tik()
        self.assertEqual(self.motor.starts, [])
        self.assertEqual(self.stand()["taken"]["weekronde"]["aan_sinds"], "2026-11-23T08:30:00+01:00")

    def test_op_tijd_en_kop(self):
        self.rooster(self.taak())
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)
        s = self.motor.starts[0]
        self.assertEqual((s["tid"], s["ronde"], s["model"], s["effort"]), ("bakkerij--r-weekronde", "weekronde", "opus", "medium"))
        self.assertIn("«Weekronde advertenties», onder Bakkerij", s["tekst"])
        self.assertIn("`bakkerij--r-weekronde`", s["tekst"])
        self.assertIn("voor de ronde van 08:00.", s["tekst"])
        self.assertNotIn("Ingehaald", s["tekst"])
        self.assertTrue(s["tekst"].endswith("--- DE OPDRACHT ---\nOpdracht van weekronde: doe de ronde.\n"))
        rec = self.laatste()
        self.assertEqual((rec["status"], rec["ingehaald"]), ("loopt", False))
        self.assertEqual(self.stand()["taken"]["weekronde"]["laatst_voor"], "2026-11-23T08:00:00+01:00")
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)

    def test_inhalen_binnen_en_buiten_venster(self):
        self.rooster(self.taak(inhalen_min=120))
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        self.nu[0] = bxl(2026, 11, 23, 9, 59)
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)
        self.assertIn("Ingehaald: de app of de Mac stond uit op het uur zelf.", self.motor.starts[0]["tekst"])
        self.assertTrue(self.laatste()["ingehaald"])
        # buiten het venster: een week later, om 10:01
        self.motor.stand["bakkerij--r-weekronde"]["status"] = "klaar"
        self.motor.chats["bakkerij--r-weekronde"].staat = "slaapt"
        self.nu[0] = bxl(2026, 11, 30, 10, 1)
        n = len(self.motor.verloop)
        self.tik()
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)
        self.assertEqual(self.laatste()["status"], "overgeslagen")
        self.assertEqual(self.laatste()["regel"], "de app of de Mac stond uit op het uur")
        self.assertEqual(self.stand()["taken"]["weekronde"]["laatst_voor"], "2026-11-30T08:00:00+01:00")
        self.assertEqual(len(self.motor.verloop) - n, 1)   # één regel, niet per tik

    def test_een_ingehaalde_ronde_na_dagen_slaap(self):
        self.rooster(self.taak(dagen=[1, 2, 3, 4, 5, 6, 7], uren=["08:00"], inhalen_min=2160))
        self.zet_stand("weekronde", aan_sinds="2026-11-01T00:00:00+01:00", aan_gezien=True,
                       laatst_voor="2026-11-18T08:00:00+01:00")
        self.nu[0] = bxl(2026, 11, 23, 9, 0)   # de Mac sliep vijf dagen
        for _ in range(5):
            self.tik()
        self.assertEqual(len(self.motor.starts), 1)
        self.assertEqual(self.laatste()["slot"], "2026-11-23T08:00:00+01:00")
        self.assertEqual(self.stand()["taken"]["weekronde"].get("historie"), [])

    def test_schrijven_voor_starten(self):
        self.rooster(self.taak())
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        self.motor.gooi = RuntimeError("de sessie startte niet")
        self.tik()
        self.motor.gooi = None
        self.tik()
        self.nu[0] += timedelta(minutes=5)
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)
        rec = self.laatste()
        self.assertEqual(rec["status"], "fout")
        self.assertIn("de sessie startte niet", rec["regel"])
        self.assertEqual(self.stand()["taken"]["weekronde"]["laatst_voor"], "2026-11-23T08:00:00+01:00")

    def test_twee_tikken_tegelijk(self):
        self.rooster(self.taak())
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        draden = [threading.Thread(target=self.tik) for _ in range(8)]
        draden.append(threading.Thread(target=lambda: self._probeer_nu()))
        for d in draden:
            d.start()
        for d in draden:
            d.join()
        self.assertEqual(len(self.motor.starts), 1)

    def _probeer_nu(self):
        try:
            klok.nu_draaien(self.staat, "weekronde")
        except ValueError:
            pass

    def test_kapotte_regel_stopt_de_andere_niet(self):
        self.rooster(self.taak(id="kapot", uren=["25:00"]), self.taak(id="weg", opdracht="rooster/bestaat-niet.md", _geen_bestand=True),
                     self.taak(id="vreemd", onder="niemand"), "geen object", self.taak())
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        self.tik()
        self.assertEqual([s["ronde"] for s in self.motor.starts], ["weekronde"])
        o = {t["id"]: t for t in klok.overzicht(self.staat)["taken"] if t["id"]}
        self.assertIn("kapot uur", o["kapot"]["fout"])
        self.assertIn("bestaat niet", o["weg"]["fout"])
        self.assertIn("onbekend project", o["vreemd"]["fout"])
        self.assertIsNone(o["weekronde"]["fout"])

    def test_onleesbaar_rooster(self):
        with open(self.staat["rooster"], "w") as fh:
            fh.write('{"taken": [')
        self.tik()
        self.assertEqual(self.motor.starts, [])
        o = klok.overzicht(self.staat)
        self.assertFalse(o["ok"])
        self.assertIn("niet te lezen", o["fout"])

    def test_uitzondering_in_een_taak(self):
        self.rooster(self.taak(id="a"), self.taak(id="b", opdracht="rooster/b.md"))
        for tid in ("a", "b"):
            self.zet_stand(tid, aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        echt = klok._probeer

        def probeer(staat, d, taak, *a, **kw):
            if taak["id"] == "a":
                raise KeyError("stuk")
            return echt(staat, d, taak, *a, **kw)
        klok._probeer = probeer
        try:
            self.tik()
        finally:
            klok._probeer = echt
        self.assertEqual([s["ronde"] for s in self.motor.starts], ["b"])
        o = {t["id"]: t for t in klok.overzicht(self.staat)["taken"]}
        self.assertIn("stuk", o["a"]["fout"])


class Sloten(Basis):
    def setUp(self):
        super().setUp()
        self.rooster(self.taak(desktop_id="bakkerij-koppen"))
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)

    def test_desktop_aan(self):
        self.desktop([{"id": "bakkerij-koppen", "enabled": True, "lastRunAt": "2026-11-19T23:24:22.301Z"}])
        n = len(self.motor.verloop)
        self.tik()
        self.tik()
        self.assertEqual(self.motor.starts, [])
        self.assertEqual(self.laatste()["status"], "geweigerd")
        self.assertNotIn("laatst_voor", self.stand()["taken"]["weekronde"])
        self.assertEqual(len(self.motor.verloop) - n, 1)
        o = klok.overzicht(self.staat)["taken"][0]
        self.assertTrue(o["desktop"]["aan"])
        # binnen het venster uitgezet in de desktop-app: de volgende tik start alsnog
        self.desktop([{"id": "bakkerij-koppen", "enabled": False}])
        self.nu[0] += timedelta(minutes=30)
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)
        self.assertNotIn("Ingehaald", self.motor.starts[0]["tekst"])
        # en na het venster wordt het "overgeslagen" met de reden
        self.motor.stand["bakkerij--r-weekronde"]["status"] = "klaar"

    def test_desktop_aan_tot_na_venster(self):
        self.desktop([{"id": "bakkerij-koppen", "enabled": True}])
        self.tik()
        self.nu[0] = bxl(2026, 11, 23, 10, 5)
        self.tik()
        self.assertEqual(self.laatste()["status"], "overgeslagen")
        self.assertIn("niet gestart binnen het venster: staat nog aan", self.laatste()["regel"])

    def test_desktop_oud_bestand(self):
        self.desktop([{"id": "bakkerij-koppen", "enabled": True}], oud_dagen=11)
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)
        self.assertIn("1 oud rooster genegeerd", klok.overzicht(self.staat)["taken"][0]["desktop"]["uitleg"])

    def test_desktop_onleesbaar(self):
        self.desktop([], kapot=True)
        self.tik()
        self.assertEqual(self.motor.starts, [])
        self.assertEqual(self.laatste()["status"], "geweigerd")
        self.assertIn("niet te lezen", self.laatste()["regel"])

    def test_desktop_map_bestaat_niet(self):
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)

    def test_desktop_zonder_rooster(self):
        os.makedirs(os.path.join(self.desk, "a", "b"))   # de desktop-app staat er, maar zonder scheduled-tasks.json
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)      # ontbreekt is niet onleesbaar

    def test_desktop_draaide_al(self):
        self.desktop([{"id": "bakkerij-koppen", "enabled": False, "lastRunAt": "2026-11-23T07:00:05.000Z"}])
        self.tik()
        self.assertEqual(self.motor.starts, [])
        self.assertEqual(self.laatste()["status"], "overgeslagen")
        self.assertIn("draaide ze al", self.laatste()["regel"])
        self.assertEqual(self.stand()["taken"]["weekronde"]["laatst_voor"], "2026-11-23T08:00:00+01:00")

    def test_vorige_loopt_nog(self):
        self.motor.stand["bakkerij--r-weekronde"] = {"status": "open", "ronde": "weekronde", "soort": "taak"}
        c = self.motor.chats["bakkerij--r-weekronde"] = NepChat(self.motor, "bakkerij--r-weekronde", "K", "taak", "bakkerij")
        c.staat = "werkt"
        n = len(self.motor.verloop)
        self.tik()
        self.tik()
        self.assertEqual(self.motor.starts, [])
        self.assertEqual(self.laatste()["status"], "wacht op de vorige ronde")
        self.assertEqual(len(self.motor.verloop) - n, 1)
        c.staat = "slaapt"
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)

    def test_vorige_open_en_stil(self):
        self.motor.stand["bakkerij--r-weekronde"] = {"status": "open", "ronde": "weekronde", "soort": "taak"}
        c = self.motor.chats["bakkerij--r-weekronde"] = NepChat(self.motor, "bakkerij--r-weekronde", "K", "taak", "bakkerij")
        c.staat = "wacht"
        self.zet_stand("weekronde", historie=[], laatste={"slot": "2026-11-16T08:00:00+01:00", "gestart": "x",
                                                            "status": "loopt", "onder": "bakkerij", "naam": "K"})
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)
        self.assertEqual(self.motor.statussen[0][:2], ("bakkerij--r-weekronde", "klaar"))
        self.assertIn("vervangen door de ronde van ma 23 nov 08:00", self.motor.statussen[0][2])
        self.assertEqual(self.stand()["taken"]["weekronde"]["historie"][0]["status"], "klaar (vervangen)")

    def test_andere_taakchat_met_die_sleutel(self):
        self.motor.stand["bakkerij--r-weekronde"] = {"status": "open", "soort": "taak"}
        self.tik()
        self.assertEqual(self.motor.starts, [])
        self.assertIn("andere taakchat", self.laatste()["regel"])

    def test_ouder_niet_in_app_of_noodrem(self):
        self.motor.stand["bakkerij"] = {"in_app": False}
        self.tik()
        self.assertEqual(self.motor.starts, [])
        self.assertIn("draait niet in de app", self.laatste()["regel"])
        self.motor.stand["bakkerij"] = {"in_app": True, "noodrem": True}
        self.tik()
        self.assertEqual(self.motor.starts, [])
        self.assertIn("noodrem", self.laatste()["regel"])
        self.motor.stand["bakkerij"] = {"in_app": True}
        self.tik()
        self.assertEqual(len(self.motor.starts), 1)


class Knoppen(Basis):
    def test_nu_draaien_neemt_het_volgende_moment_van_vandaag(self):
        self.rooster(self.taak(dagen=[1, 2, 3, 4, 5, 6, 7], uren=["08:00", "18:00"]))
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        self.tik()   # 08:00 loopt
        self.motor.stand["bakkerij--r-weekronde"]["status"] = "klaar"
        self.motor.chats["bakkerij--r-weekronde"].staat = "slaapt"
        self.nu[0] = bxl(2026, 11, 23, 10, 0)
        r = klok.nu_draaien(self.staat, "weekronde")
        self.assertEqual(r["chat"], "bakkerij--r-weekronde")
        self.assertEqual(len(self.motor.starts), 2)
        self.assertIn("de eigenaar tikte op Nu draaien", self.motor.starts[1]["tekst"])
        self.assertEqual(self.stand()["taken"]["weekronde"]["laatst_voor"], "2026-11-23T18:00:00+01:00")
        self.assertEqual(klok.overzicht(self.staat)["taken"][0]["volgende"], "2026-11-24T08:00:00+01:00")
        self.motor.stand["bakkerij--r-weekronde"]["status"] = "klaar"
        self.motor.chats["bakkerij--r-weekronde"].staat = "slaapt"
        self.nu[0] = bxl(2026, 11, 23, 18, 5)
        self.tik()
        self.assertEqual(len(self.motor.starts), 2)
        self.nu[0] = bxl(2026, 11, 24, 8, 0, 30)
        self.tik()
        self.assertEqual(len(self.motor.starts), 3)

    def test_nu_draaien_alleen_als_aan(self):
        self.rooster(self.taak(aan=False))
        with self.assertRaisesRegex(ValueError, "staat uit"):
            klok.nu_draaien(self.staat, "weekronde")

    def test_nu_draaien_desktop_aan(self):
        self.rooster(self.taak(desktop_id="x"))
        self.desktop([{"id": "x", "enabled": True}])
        with self.assertRaisesRegex(ValueError, "geweigerd"):
            klok.nu_draaien(self.staat, "weekronde")
        self.assertEqual(self.motor.starts, [])

    def test_aanzetten_geweigerd_bij_desktop_aan(self):
        self.rooster(self.taak(aan=False, desktop_id="bakkerij-koppen"))
        self.desktop([{"id": "bakkerij-koppen", "enabled": True}])
        with self.assertRaisesRegex(ValueError, r"Zet «Weekronde advertenties» eerst uit in de Claude-desktop-app"):
            klok.zet(self.staat, "weekronde", True)
        with open(self.staat["rooster"]) as fh:
            self.assertFalse(json.load(fh)["taken"][0]["aan"])
        klok.zet(self.staat, "weekronde", False)   # uitzetten mag altijd

    def test_rooster_blijft_buiten_aan_gelijk(self):
        tekst = ('{\n    "_": "uitleg met \\"aan\\": true erin",\n    "taken": [\n'
                 '        {"id": "weekronde", "naam": "K", "onder": "bakkerij", "dagen": [1],   "uren": ["08:00"],\n'
                 '         "opdracht": "rooster/weekronde.md", "extra": {"aan": true}, "aan": false},\n'
                 '        {\n            "id": "tweede",\n            "naam": "Twee é",\n            "onder": "god",\n'
                 '            "dagen": [1, 2],\n            "uren": ["09:42"],\n            "opdracht": "rooster/weekronde.md"\n        }\n'
                 '    ]\n}\n')
        self.taak()   # maakt het opdrachtbestand
        with open(self.staat["rooster"], "w", encoding="utf-8") as fh:
            fh.write(tekst)
        klok.zet(self.staat, "weekronde", True)
        with open(self.staat["rooster"], encoding="utf-8") as fh:
            na = fh.read()
        self.assertEqual(na, tekst.replace('{"aan": true}, "aan": false}', '{"aan": true}, "aan": true}'))
        klok.zet(self.staat, "tweede", True)   # het veld ontbreekt: komt erbij, in de inspringing van de taak
        with open(self.staat["rooster"], encoding="utf-8") as fh:
            na2 = fh.read()
        self.assertIn('            "opdracht": "rooster/weekronde.md",\n            "aan": true\n        }', na2)
        self.assertEqual(na2.replace(',\n            "aan": true', ""), na)
        d = json.loads(na2)
        self.assertEqual([t["aan"] for t in d["taken"]], [True, True])
        self.assertEqual(d["taken"][0]["extra"], {"aan": True})

    def test_overzicht_volgende(self):
        self.rooster(self.taak(dagen=[1, 2, 3, 4, 5], uren=["09:42"]), self.taak(id="uit", aan=False, opdracht="rooster/uit.md"))
        self.nu[0] = bxl(2026, 11, 27, 10, 0)   # vrijdag
        o = {t["id"]: t for t in klok.overzicht(self.staat)["taken"]}
        self.assertEqual(o["weekronde"]["volgende"], "2026-11-30T09:42:00+01:00")
        self.assertIsNone(o["uit"]["volgende"])
        self.assertEqual(o["weekronde"]["chat"], "bakkerij--r-weekronde")


class Einde(Basis):
    def setUp(self):
        super().setUp()
        self.rooster(self.taak())
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        self.tik()
        self.sl = "bakkerij--r-weekronde"
        self.chat = self.motor.chats[self.sl]

    def test_taak_klaar_en_ouder_regel_een_keer(self):
        self.assertEqual(klok.ouder_regel(self.staat, "bakkerij"), "")   # nog niets geëindigd
        self.nu[0] += timedelta(minutes=20)
        self.assertTrue(klok.ronde_klaar(self.staat, self.sl, "Drie advertenties vervangen, rapport in LOG.md."))
        rec = self.laatste()
        self.assertEqual((rec["status"], rec["regel"]), ("klaar", "Drie advertenties vervangen, rapport in LOG.md."))
        r = klok.ouder_regel(self.staat, "bakkerij")
        self.assertIn("Geplande rondes sinds je vorige beurt:", r)
        self.assertIn("- «Weekronde advertenties» (ma 23 nov 08:20, klaar): Drie advertenties vervangen", r)
        self.assertEqual(klok.ouder_regel(self.staat, "bakkerij"), "")
        self.assertEqual(klok.ouder_regel(self.staat, "studio"), "")

    def test_zonder_taak_klaar_sluit_de_klok(self):
        self.chat.beurten = [{"rol": "user", "tekst": "x"},
                             {"rol": "assistant", "delen": [{"soort": "tekst", "tekst": "Ronde gedaan.\nDetails…"}]}]
        klok.beurt_einde(self.staat, self.chat)
        self.assertEqual(self.motor.statussen[-1], (self.sl, "klaar", "Ronde gedaan.", "klok"))
        self.assertEqual(self.laatste()["status"], "klaar (niet zelf afgesloten)")

    def test_open_vraag_blijft_open(self):
        self.motor.vragen.append({"chat": self.sl, "status": "open"})
        klok.beurt_einde(self.staat, self.chat)
        self.assertEqual(self.motor.statussen, [])
        self.assertEqual(self.laatste()["status"], "loopt")

    def test_fout_blijft_open(self):
        self.chat.fout = "fout: overloaded"
        klok.beurt_einde(self.staat, self.chat)
        self.assertEqual(self.motor.statussen, [])
        self.assertEqual((self.laatste()["status"], self.laatste()["regel"]), ("fout", "fout: overloaded"))

    def test_eigenaar_schreef_nog(self):
        self.chat.wachtrij = [{"tekst": "nog iets"}]
        klok.beurt_einde(self.staat, self.chat)
        self.assertEqual(self.motor.statussen, [])

    def test_loopt_lang_en_verloren(self):
        self.nu[0] += timedelta(minutes=121)
        self.tik()
        self.assertEqual(self.laatste()["status"], "loopt lang")
        self.motor.stand[self.sl].update(status="klaar", samenvatting=klok.VERLOREN)
        self.tik()
        self.assertEqual(self.laatste()["status"], "verloren")


class NepMotorDag(NepMotor):
    def __init__(self):
        super().__init__()
        self.dagen = []
        self.bezig = []
        self.dag_gooi = None

    def nieuwe_dag(self, vragen_open=False, alleen_als_stil=False):
        self.dagen.append((vragen_open, alleen_als_stil))
        if self.dag_gooi:
            raise self.dag_gooi
        if alleen_als_stil and self.bezig:
            return {"ok": False, "bezig": list(self.bezig)}
        return {"ok": True, "chats": 6}


class MotorSoort(Basis):
    def setUp(self):
        super().setUp()
        self.motor = NepMotorDag()
        self.staat["app"] = self.motor
        self.nu[0] = bxl(2026, 11, 24, 6, 0, 10)   # dinsdag

    def dag(self, **kw):
        t = {"id": "nieuwe-dag", "naam": "Nieuwe dag", "onder": "god", "soort": "motor", "actie": "nieuwe_dag",
             "dagen": [1, 2, 3, 4, 5, 6, 7], "uren": ["06:00"], "inhalen_min": 180, "aan": True}
        t.update(kw)
        return t

    def aan(self):
        self.zet_stand("nieuwe-dag", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)

    def test_ontleden(self):
        self.rooster(self.dag(), self.dag(id="zonder-actie", actie=None), self.dag(id="rare-soort", soort="iets"))
        taken, fout = klok.lees_rooster(self.staat)
        self.assertIsNone(fout)
        self.assertEqual(taken[0][1]["soort"], "motor")
        self.assertIsNone(taken[0][1]["sleutel"])
        self.assertIsNone(taken[1][1])
        self.assertIsNone(taken[2][1])

    def test_op_tijd_een_keer(self):
        self.rooster(self.dag())
        self.aan()
        self.tik()
        self.assertEqual(self.motor.dagen, [(True, True)])
        self.assertEqual(self.motor.starts, [])
        rec = self.laatste("nieuwe-dag")
        self.assertEqual((rec["status"], rec["regel"]), ("klaar", "verse sessies voor 6 chats"))
        self.nu[0] = bxl(2026, 11, 24, 7, 0)
        self.tik()
        self.assertEqual(len(self.motor.dagen), 1)
        self.nu[0] = bxl(2026, 11, 25, 6, 0, 5)
        self.tik()
        self.assertEqual(len(self.motor.dagen), 2)

    def test_wacht_tot_stil_en_meldt_een_keer(self):
        self.rooster(self.dag())
        self.aan()
        self.motor.bezig = ["Bakkerij"]
        for minuut in (0, 1, 2):
            self.nu[0] = bxl(2026, 11, 24, 6, minuut, 10)
            self.tik()
        self.assertEqual(len(self.motor.dagen), 3)
        rec = self.laatste("nieuwe-dag")
        self.assertEqual(rec["status"], klok.WACHT_STIL)
        self.assertNotIn("gestart", rec)
        self.assertNotIn("laatst_voor", self.stand()["taken"]["nieuwe-dag"])
        self.assertEqual(len([v for v in self.motor.verloop if klok.WACHT_STIL in v[2]]), 1)
        self.motor.bezig = []
        self.nu[0] = bxl(2026, 11, 24, 6, 20)
        self.tik()
        self.assertEqual(self.laatste("nieuwe-dag")["status"], "klaar")
        self.tik()
        self.assertEqual(len(self.motor.dagen), 4)

    def test_na_het_venster_overgeslagen(self):
        self.rooster(self.dag())
        self.aan()
        self.motor.bezig = ["God-chat"]
        self.tik()
        self.nu[0] = bxl(2026, 11, 24, 9, 0, 1)
        self.tik()
        rec = self.laatste("nieuwe-dag")
        self.assertEqual(rec["status"], "overgeslagen")
        self.assertIn("bezig: God-chat", rec["regel"])
        self.motor.bezig = []
        self.tik()
        self.assertEqual(len(self.motor.dagen), 1)

    def test_fout_niet_opnieuw(self):
        self.rooster(self.dag())
        self.aan()
        self.motor.dag_gooi = RuntimeError("kapot")
        self.tik()
        self.motor.dag_gooi = None
        self.nu[0] = bxl(2026, 11, 24, 6, 5)
        self.tik()
        self.assertEqual(len(self.motor.dagen), 1)
        self.assertEqual(self.laatste("nieuwe-dag")["status"], "fout")

    def test_oude_motor_zonder_argumenten(self):
        self.motor.nieuwe_dag = lambda: {"ok": True}
        self.rooster(self.dag())
        self.aan()
        self.tik()
        rec = self.laatste("nieuwe-dag")
        self.assertEqual(rec["status"], "geweigerd")
        self.assertNotIn("laatst_voor", self.stand()["taken"]["nieuwe-dag"])

    def test_verloren_na_vijf_minuten_loopt(self):
        self.rooster(self.dag())
        self.zet_stand("nieuwe-dag", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True,
                       laatst_voor="2026-11-24T06:00:00+01:00",
                       laatste={"slot": "2026-11-24T06:00:00+01:00", "gestart": "2026-11-24T06:00:00+01:00",
                                "status": "loopt", "onder": "god", "soort": "motor"})
        self.nu[0] = bxl(2026, 11, 24, 6, 10)
        self.tik()
        self.assertEqual(self.laatste("nieuwe-dag")["status"], "verloren")
        self.assertEqual(self.motor.dagen, [])

    def test_ochtend(self):
        self.motor.ochtend = self.motor.nieuwe_dag
        self.rooster(self.dag(id="ochtend", actie="ochtend"))
        self.zet_stand("ochtend", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        self.tik()
        self.assertEqual(self.motor.dagen, [(True, True)])
        self.assertEqual(self.laatste("ochtend")["regel"], "samenvatting voor 6 chats")

    def test_nu_draaien_geweigerd_en_ouder_hoort_niets(self):
        self.rooster(self.dag())
        self.aan()
        with self.assertRaises(ValueError):
            klok.nu_draaien(self.staat, "nieuwe-dag")
        self.tik()
        self.assertEqual(klok.ouder_regel(self.staat, "god"), "")
        o = klok.overzicht(self.staat)["taken"][0]
        self.assertEqual((o["soort"], o["chat_bestaat"]), ("motor", False))


class MotorStukken(unittest.TestCase):
    """motor.py zelf, op een Motor zonder __init__: geen chats met draden, geen sessies, paden in een tijdelijke map."""

    def setUp(self):
        import motor
        self.m = motor
        self.map = tempfile.mkdtemp(prefix="klok-motor-")
        M = motor.Motor.__new__(motor.Motor)
        M.config = {"chats": {"bakkerij": {"model": "fable", "effort": "max"}, "bakkerij--r-b": {"effort": "low"}}}
        M.slot = threading.RLock()
        M.stand_pad = os.path.join(self.map, "stand.json")
        M.vragen_pad = os.path.join(self.map, "vragen.json")
        M.verloop_pad = os.path.join(self.map, "verloop.jsonl")
        M.vragen, M.stopt, M.claude = [], False, "claude-bestaat-niet"
        from collections import deque
        M.verloop = deque(maxlen=10)
        M.stand_data = {"chats": {"bakkerij": {"in_app": True}}, "volgnr": 0}
        M.chats = {}
        self.M = M
        self.beurten = []
        test = self

        class Stub:
            def __init__(s, cid, soort, ouder=None):
                s.id, s.soort, s.ouder, s.naam, s.versie, s.bezig = cid, soort, ouder, cid, 0, False

            def st(s):
                return M.stand_chat(s.id)

            def beurt(s, bron, tekst, na=None, naschrift=""):
                test.beurten.append((s.id, bron, tekst))

            def zet_regel(s, r):
                pass

            def stop_sessie(s):
                pass

            def start_wachter(s, hervat=False):
                test.beurten.append((s.id, "wachter", ""))
        self.Stub = Stub
        M.chats["bakkerij"] = Stub("bakkerij", "klant")

    def tearDown(self):
        shutil.rmtree(self.map, ignore_errors=True)

    def test_cfg_ronde_uit_het_rooster(self):
        M = self.M
        M.stand_data["chats"]["bakkerij--r-a"] = {"soort": "taak", "ouder": "bakkerij", "ronde": "a", "model": "opus", "effort": "medium"}
        M.stand_data["chats"]["bakkerij--r-b"] = {"soort": "taak", "ouder": "bakkerij", "ronde": "b", "model": "opus", "effort": "medium"}
        M.stand_data["chats"]["bakkerij--gewoon"] = {"soort": "taak", "ouder": "bakkerij"}
        for k in ("bakkerij--r-a", "bakkerij--r-b", "bakkerij--gewoon"):
            M.chats[k] = self.Stub(k, "taak", "bakkerij")
        self.assertEqual(M.cfg("bakkerij--r-a"), {"model": "opus", "effort": "medium"})
        self.assertEqual(M.cfg("bakkerij--r-b"), {"model": "opus", "effort": "low"})   # config.json gaat voor
        self.assertEqual(M.cfg("bakkerij--gewoon"), {"model": "fable", "effort": "max"})
        self.assertEqual(M.startprompt(M.chats["bakkerij--r-a"]), "")   # de ronde: haar eigen opdracht (hier leeg)

    def test_start_hervat_en_verloren(self):
        M = self.M
        M.stand_data["chats"]["bakkerij--r-a"] = {"soort": "taak", "ouder": "bakkerij", "ronde": "a", "in_app": True,
                                                 "status": "open", "sessie": "s1",
                                                 "lopend": {"bron": "start", "tekst": "KOP\n--- DE OPDRACHT ---\nx"}}
        M.stand_data["chats"]["bakkerij--r-b"] = {"soort": "taak", "ouder": "bakkerij", "ronde": "b", "in_app": True,
                                                 "status": "open", "sessie": None, "lopend": None}
        for k in ("bakkerij--r-a", "bakkerij--r-b"):
            M.chats[k] = self.Stub(k, "taak", "bakkerij")
        M.stand_data["chats"]["bakkerij"]["sessie"] = "s0"
        M.start()
        a = [b for b in self.beurten if b[0] == "bakkerij--r-a"]
        self.assertEqual(len(a), 1)
        self.assertTrue(a[0][2].startswith(self.m.HERVAT_RONDE + "\n\nKOP\n"))
        self.assertFalse([b for b in self.beurten if b[0] == "bakkerij--r-b"])   # niet alsnog gestart
        st = M.stand_chat("bakkerij--r-b")
        self.assertEqual((st["status"], st["samenvatting"]), ("klaar", klok.VERLOREN))
        self.assertEqual(self.m.RONDE_VERLOREN, klok.VERLOREN)


class MotorRouting(MotorStukken):
    def test_zeg_en_gesprek_gaan_naar_de_klok_voor_een_taak_zonder_open_chat(self):
        import klok_lader
        geroepen = []
        oud = (klok_lader.praat, klok_lader.gesprek_buiten)
        klok_lader.praat = lambda cid, tekst: (geroepen.append(("praat", cid, tekst)), {"ok": True})[1]
        klok_lader.gesprek_buiten = lambda cid: {"beurten": [], "wacht": [], "via": cid}
        try:
            self.assertEqual(self.M.zeg("bakkerij--r-a", "hoi"), {"ok": True})            # nog geen chat
            self.assertEqual(self.M.zeg("bakkerij--r-a@2026-11-09-0800", "x"), {"ok": True})
            self.assertEqual(self.M.gesprek("bakkerij--r-a@2026-11-09-0800")["via"], "bakkerij--r-a@2026-11-09-0800")
            with self.assertRaises(ValueError):   # gewone onbekende chat: zoals vroeger
                self.M.zeg("onbekend", "x")
        finally:
            klok_lader.praat, klok_lader.gesprek_buiten = oud
        self.assertEqual([g[1] for g in geroepen], ["bakkerij--r-a", "bakkerij--r-a@2026-11-09-0800"])


class NepMotorPlan(NepMotor):
    sleutel = "s"

    def __init__(self):
        super().__init__()
        self.chats["bakkerij--bestelpagina"] = NepChat(self, "bakkerij--bestelpagina", "Bestelpagina", "taak", "bakkerij")

    def tool(self, body):
        assert body["sleutel"] == self.sleutel and body["tool"] == "vraag_eigenaar"
        nr = len(self.vragen) + 50
        self.vragen.append({"nr": nr, "chat": body["chat"], "status": "open", **body["args"]})
        return {"tekst": f"Vraag {nr} staat bij de eigenaar in 'Wacht op jou'."}


class PlanTaak(Basis):
    OPDRACHT = "Doe elke werkdag de Facebook-ronde volgens het schema in de klantmap. " * 3

    def setUp(self):
        super().setUp()
        self.motor = NepMotorPlan()
        self.staat["app"] = self.motor
        self.nu[0] = bxl(2026, 11, 23, 14, 0)
        self.rooster(self.taak())   # één bestaande taak, met de hand opgemaakt
        with open(self.staat["rooster"], "w", encoding="utf-8") as fh:
            fh.write('{\n  "_": "test",\n  "taken": [\n    ' + json.dumps(self.taak(), ensure_ascii=False) + '\n  ]\n}\n')

    def plan(self, chat="bakkerij", **kw):
        args = {"naam": "Facebook-ronde", "dagen": [1, 2, 3, 4, 5], "uren": ["09:30"], "opdracht": self.OPDRACHT}
        args.update(kw)
        return klok.plan_taak(self.staat, chat, args)

    def ruw(self, tid):
        with open(self.staat["rooster"], encoding="utf-8") as fh:
            return next(t for t in json.load(fh)["taken"] if t["id"] == tid)

    def test_klantchat_plant_onder_zichzelf_uit_met_vraag(self):
        tekst = self.plan()
        t = self.ruw("facebook-ronde")
        self.assertEqual((t["onder"], t["aan"], t["model"], t["effort"], t["door"]), ("bakkerij", False, "opus", "medium", "bakkerij"))
        with open(os.path.join(self.map, "rooster", "facebook-ronde.md"), encoding="utf-8") as fh:
            self.assertIn(self.OPDRACHT.strip(), fh.read())
        v = self.motor.vragen[-1]
        self.assertEqual((v["chat"], v["antwoorden"]), ("bakkerij", [klok.PLAN_JA, klok.PLAN_NEE]))
        self.assertIn("elke werkdag om 09:30", v["vraag"])
        self.assertEqual(self.stand()["aanvragen"]["facebook-ronde"]["nr"], v["nr"])
        self.assertIn("nog uit", tekst)
        with open(self.staat["rooster"], encoding="utf-8") as fh:
            regels = fh.read().splitlines()
        self.assertIn("    " + json.dumps(self.taak(), ensure_ascii=False) + ",", regels)   # bestaande regel onaangeroerd
        self.assertTrue(any(r.startswith('    {"id": "facebook-ronde"') for r in regels))   # nieuwe taak op één regel

    def test_tik_op_ja_zet_aan_zonder_inhalen(self):
        self.plan()
        self.tik()
        self.assertFalse(self.ruw("facebook-ronde")["aan"])   # vraag nog open
        self.motor.vragen[-1].update(status="beantwoord", antwoord=klok.PLAN_JA)
        self.tik()
        self.assertTrue(self.ruw("facebook-ronde")["aan"])
        self.assertNotIn("facebook-ronde", self.stand().get("aanvragen", {}))
        self.assertFalse([s for s in self.motor.starts if s["tid"] == "bakkerij--r-facebook-ronde"])   # 09:30 was voorbij
        self.nu[0] = bxl(2026, 11, 24, 9, 30, 10)
        self.tik()
        self.assertTrue([s for s in self.motor.starts if s["tid"] == "bakkerij--r-facebook-ronde"])
        kop = [s for s in self.motor.starts if s["tid"] == "bakkerij--r-facebook-ronde"][0]["tekst"]
        self.assertNotIn("desktop-app; hier is dit anders", kop)

    def test_nee_of_ander_antwoord_laat_uit(self):
        self.plan()
        self.motor.vragen[-1].update(status="beantwoord", antwoord="misschien later")
        self.tik()
        self.assertFalse(self.ruw("facebook-ronde")["aan"])
        self.assertNotIn("facebook-ronde", self.stand().get("aanvragen", {}))

    def test_taakchat_plant_onder_zijn_project(self):
        self.plan(chat="bakkerij--bestelpagina")
        self.assertEqual(self.ruw("facebook-ronde")["onder"], "bakkerij")

    def test_god_kiest_onder(self):
        self.plan(chat="god", onder="studio")
        self.assertEqual(self.ruw("facebook-ronde")["onder"], "studio")

    def test_ronde_weigert(self):
        self.motor.chats["bakkerij--r-x"] = NepChat(self.motor, "bakkerij--r-x", "X", "taak", "bakkerij")
        for c in ("bakkerij--r-x",):
            with self.assertRaises(ValueError):
                self.plan(chat=c)
        with open(self.staat["rooster"], encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["taken"]), 1)

    def test_korte_opdracht_en_dubbel_weigeren_zonder_sporen(self):
        with self.assertRaises(ValueError):
            self.plan(opdracht="post iets")
        self.assertFalse(os.path.exists(os.path.join(self.map, "rooster", "facebook-ronde.md")))
        with self.assertRaises(ValueError):
            self.plan(uren=["25:00"])
        self.assertFalse(os.path.exists(os.path.join(self.map, "rooster", "facebook-ronde.md")))
        self.plan()
        with self.assertRaises(ValueError):
            self.plan()
        self.plan(naam="Facebook ronde!")  # andere naam, zelfde stam: eigen id
        self.assertEqual(self.ruw("facebook-ronde-2")["naam"], "Facebook ronde!")


class Pagina(PlanTaak):
    """De pagina van een automatisering: rondes met hun gesprek, praten, wijzig_taak."""

    def setUp(self):
        super().setUp()
        self.archief = os.path.join(self.map, "archief")
        os.makedirs(self.archief)
        self.staat["archief"] = self.archief
        m = self.motor
        m.gezegd = []
        m.zeg = lambda cid, tekst: (m.gezegd.append((cid, tekst)), {"ok": True})[1]

        def beurt(chat, bron, tekst, na=None, naschrift=""):
            chat.beurten.append((bron, tekst, naschrift))
        NepChat.beurt = beurt
        NepChat.zet_regel = lambda chat, regel: None
        self.sleutel = "bakkerij--r-weekronde"

    def tearDown(self):
        for k in ("beurt", "zet_regel"):
            if hasattr(NepChat, k):
                delattr(NepChat, k)
        super().tearDown()

    def archiveer(self, stempel, tekst="oud"):
        with open(os.path.join(self.archief, f"{self.sleutel}-{stempel}.json"), "w", encoding="utf-8") as fh:
            json.dump([{"rol": "user", "bron": "start", "uur": "08:00", "tekst": tekst, "tools": {"x": 1}}], fh)

    def test_detail_koppelt_rondes_aan_hun_gesprek(self):
        self.zet_stand("weekronde", laatste={"slot": "2026-11-23T08:00:00+01:00", "gestart": "2026-11-23T08:00:30+01:00",
                                               "status": "klaar", "regel": "nieuw"},
                       historie=[{"slot": "2026-11-16T08:00:00+01:00", "gestart": "2026-11-16T08:00:59+01:00",
                                  "status": "overgeslagen", "regel": "x"},
                                 {"slot": "2026-11-09T08:00:00+01:00", "gestart": "2026-11-09T08:00:10+01:00",
                                  "status": "klaar", "regel": "oud"}])
        self.archiveer("2026-11-09-0800")
        self.archiveer("2026-11-17-1015")   # de eigenaar sprak de taak aan buiten een ronde
        self.motor.chats[self.sleutel] = NepChat(self.motor, self.sleutel, "Weekronde", "taak", "bakkerij")
        self.motor.chats[self.sleutel].beurten = [{"rol": "user"}]
        self.motor.stand[self.sleutel] = {"ronde": "weekronde", "status": "klaar", "sinds": "2026-11-23T08:00:31+01:00"}
        d = klok.taak_detail(self.staat, "weekronde")
        self.assertEqual([(r["status"], r["gesprek"]) for r in d["rondes"]],
                         [("klaar", "huidig"), ("gesprek met jou", "2026-11-17-1015"), ("overgeslagen", None),
                          ("klaar", "2026-11-09-0800")])
        self.assertIn("doe de ronde", d["opdracht"])
        g = klok.gesprek_buiten(self.staat, self.sleutel + "@2026-11-09-0800")
        self.assertEqual((g["alleen_lezen"], g["beurten"][0]["tekst"]), (True, "oud"))
        self.assertNotIn("tools", g["beurten"][0])
        self.assertIsNone(klok.gesprek_buiten(self.staat, self.sleutel + "@../../x"))
        self.assertIsNone(klok.gesprek_buiten(self.staat, "bakkerij"))
        self.assertEqual(klok.gesprek_buiten(self.staat, self.sleutel), {"beurten": [], "wacht": []})

    def test_praat_zonder_chat_start_gesprek_zonder_ronde(self):
        klok.praat(self.staat, self.sleutel, "zet ze op 10:00")
        st = self.motor.starts[-1]
        self.assertEqual(st["tid"], self.sleutel)
        self.assertIn("Je doet de ronde **niet**", st["tekst"])
        self.assertIn("doe de ronde", st["tekst"])   # de opdracht staat erin
        self.assertEqual(self.motor.chats[self.sleutel].beurten[-1][:2], ("eigenaar", "zet ze op 10:00"))
        self.assertFalse(self.stand()["taken"].get("weekronde", {}).get("laatste"))   # geen ronde in de stand

    def test_praat_heropent_gesloten_ronde_en_open_ronde_krijgt_gewoon_tekst(self):
        c = NepChat(self.motor, self.sleutel, "Weekronde", "taak", "bakkerij")
        self.motor.chats[self.sleutel] = c
        self.motor.stand[self.sleutel] = {"ronde": "weekronde", "status": "klaar"}
        klok.praat(self.staat, self.sleutel + "@2026-11-09-0800", "waarom overgeslagen?")
        self.assertEqual(self.motor.statussen[-1][:2], (self.sleutel, "open"))
        self.assertEqual(c.beurten[-1][2], klok.PRAAT_NASCHRIFT)
        self.assertFalse(self.motor.starts)
        klok.praat(self.staat, self.sleutel, "nog iets")
        self.assertEqual(self.motor.gezegd, [(self.sleutel, "nog iets")])

    def test_praat_weigert_andere_taakchat_en_onbekend(self):
        self.motor.stand[self.sleutel] = {"status": "open"}   # een gewone taakchat met die sleutel
        with self.assertRaises(ValueError):
            klok.praat(self.staat, self.sleutel, "hoi")
        with self.assertRaises(ValueError):
            klok.praat(self.staat, "bakkerij--r-bestaat-niet", "hoi")

    def test_wijzig_vraagt_en_tik_voert_door(self):
        self.motor.chats[self.sleutel] = NepChat(self.motor, self.sleutel, "Weekronde", "taak", "bakkerij")
        self.motor.stand[self.sleutel] = {"ronde": "weekronde", "status": "open"}
        nieuw = "Nieuwe opdracht: doe de weekronde en kijk ook naar de reacties van vorige week, per advertentie.\n"
        tekst = klok.wijzig_taak(self.staat, self.sleutel, {"uren": ["10:00"], "model": "sonnet", "opdracht": nieuw})
        self.assertIn(klok.WIJZIG_JA, tekst)
        v = self.motor.vragen[-1]
        self.assertEqual((v["chat"], v["antwoorden"]), (self.sleutel, [klok.WIJZIG_JA, klok.WIJZIG_NEE]))
        self.assertIn("elke maandag om 08:00 → elke maandag om 10:00", v["concept"])
        self.assertIn("+Nieuwe opdracht", v["concept"])
        self.assertEqual(self.ruw("weekronde")["uren"], ["08:00"])   # nog niets veranderd
        with self.assertRaises(ValueError):   # één voorstel tegelijk
            klok.wijzig_taak(self.staat, self.sleutel, {"model": "haiku"})
        self.tik()
        self.assertEqual(self.ruw("weekronde")["uren"], ["08:00"])
        v.update(status="beantwoord", antwoord=klok.WIJZIG_JA)
        self.nu[0] = bxl(2026, 11, 23, 14, 5)
        self.tik()
        r = self.ruw("weekronde")
        self.assertEqual((r["uren"], r["model"], r["aan"]), (["10:00"], "sonnet", True))
        with open(os.path.join(self.map, "rooster", "weekronde.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), nieuw)
        with open(self.staat["rooster"], encoding="utf-8") as fh:
            self.assertIn('    {"id": "weekronde"', fh.read())   # nog altijd één regel
        self.assertEqual(self.stand()["taken"]["weekronde"]["aan_sinds"], "2026-11-23T14:05:00+01:00")
        self.assertFalse(self.stand().get("wijzigingen"))

    def test_wijzig_nee_en_rechten(self):
        klok.wijzig_taak(self.staat, "bakkerij", {"id": "weekronde", "dagen": [1, 3]})
        self.motor.vragen[-1].update(status="beantwoord", antwoord=klok.WIJZIG_NEE)
        self.tik()
        self.assertEqual(self.ruw("weekronde")["dagen"], [1])
        for chat in ("studio",):
            with self.assertRaises(ValueError):
                klok.wijzig_taak(self.staat, chat, {"id": "weekronde", "dagen": [2]})
        with self.assertRaises(ValueError):   # niets anders dan nu
            klok.wijzig_taak(self.staat, "god", {"id": "weekronde", "dagen": [1]})
        with self.assertRaises(ValueError):   # kapot uur
            klok.wijzig_taak(self.staat, "god", {"id": "weekronde", "uren": ["8u"]})

    def test_wijzig_niet_door_als_de_taak_intussen_veranderde(self):
        klok.wijzig_taak(self.staat, "god", {"id": "weekronde", "model": "sonnet"})
        with open(self.staat["rooster"], encoding="utf-8") as fh:
            t = fh.read()
        with open(self.staat["rooster"], "w", encoding="utf-8") as fh:
            fh.write(t.replace('"model": "opus"', '"model": "haiku"'))
        self.motor.vragen[-1].update(status="beantwoord", antwoord=klok.WIJZIG_JA)
        self.tik()
        self.assertEqual(self.ruw("weekronde")["model"], "haiku")
        self.assertTrue([x for x in self.motor.verloop if "niet aangepast" in x[2]])


class NepMotorKoppel(NepMotor):
    def klok_vraag(self, cid, vraag, context, antwoorden):
        nr = len(self.vragen) + 80
        self.vragen.append({"nr": nr, "chat": cid, "status": "open", "vraag": vraag, "context": context,
                            "antwoorden": list(antwoorden), "stil": True})
        return nr


class Koppeling(Basis):
    """Een ronde die door een ontbrekende of uitgelogde koppeling niets kan, verwittigt de eigenaar zelf."""

    def setUp(self):
        super().setUp()
        self.motor = NepMotorKoppel()
        self.staat["app"] = self.motor
        self.rooster(self.taak(koppelingen=["meta-ads"]))
        self.zet_stand("weekronde", aan_sinds="2026-11-20T00:00:00+01:00", aan_gezien=True)
        self.tik()
        self.sl = "bakkerij--r-weekronde"
        self.chat = self.motor.chats[self.sl]

    def init(self, *servers):
        return klok.koppelingen_bij_start(self.staat, self.sl, [{"name": n, "status": st} for n, st in servers])

    def test_moet_inloggen_een_stille_vraag_een_keer(self):
        self.assertEqual(self.init(("meta-ads", "needs-auth"), ("whatsapp", "connected")), [("meta-ads", "moet inloggen")])
        v = self.motor.vragen[-1]
        self.assertEqual((v["chat"], v["stil"], v["antwoorden"]), (self.sl, True, [klok.KOPPEL_OPNIEUW, klok.KOPPEL_LATER]))
        self.assertIn("meta-ads moet inloggen", v["vraag"])
        self.assertIn("/mcp → meta-ads → Authenticate", v["context"])
        self.assertEqual(self.laatste()["koppeling"], "meta-ads moet inloggen")
        self.assertEqual(self.stand()["koppelvragen"]["weekronde"]["nr"], v["nr"])
        self.assertTrue([x for x in self.motor.verloop if x[0] == "fout" and x[2] == "koppeling ontbreekt"])
        # de ronde merkt het zelf ook en sluit af: geen tweede vraag, status fout
        klok.ronde_klaar(self.staat, self.sl, "KOPPELING: meta-ads requires authentication")
        self.assertEqual(len(self.motor.vragen), 1)
        self.assertEqual(self.laatste()["status"], "fout")

    def test_verbonden_of_nog_bezig_niets_en_ontbrekend_wel(self):
        self.assertIsNone(self.init(("meta-ads", "connected")))
        self.assertIsNone(self.init(("meta-ads", "pending")))
        self.assertEqual(self.motor.vragen, [])
        self.assertEqual(self.init(("whatsapp", "connected")), [("meta-ads", "niet geladen")])
        self.assertIn("claude.ai", self.motor.vragen[-1]["context"])

    def test_zonder_koppelingen_in_het_rooster_vangt_de_ronde_het(self):
        self.rooster(self.taak())
        self.assertIsNone(self.init(("whatsapp", "connected")))
        klok.ronde_klaar(self.staat, self.sl, "**KOPPELING:** de connector fb ads bestaat niet in deze sessie.")
        v = self.motor.vragen[-1]
        self.assertIn("de connector fb ads bestaat niet", v["vraag"])
        self.assertEqual(self.laatste()["status"], "fout")

    def test_stille_vraag_houdt_de_ronde_niet_open_en_slottekst_telt(self):
        self.init(("meta-ads", "needs-auth"))
        self.chat.beurten = [{"rol": "assistant", "delen": [{"soort": "tekst", "tekst": "KOPPELING: meta-ads\nmeer"}]}]
        klok.beurt_einde(self.staat, self.chat)
        self.assertEqual(self.motor.statussen[-1][:2], (self.sl, "klaar"))
        self.assertEqual(self.laatste()["status"], "fout")
        self.assertEqual(len(self.motor.vragen), 1)

    def test_opnieuw_draait_meteen_en_later_niet(self):
        self.init(("meta-ads", "needs-auth"))
        klok.ronde_klaar(self.staat, self.sl, "KOPPELING: meta-ads")
        self.motor.stand[self.sl]["status"] = "klaar"
        self.nu[0] += timedelta(minutes=30)
        starts = len(self.motor.starts)
        self.motor.vragen[-1].update(status="beantwoord", antwoord=klok.KOPPEL_OPNIEUW)
        self.tik()
        self.assertEqual(len(self.motor.starts), starts + 1)
        self.assertNotIn("weekronde", self.stand().get("koppelvragen", {}))
        self.init(("meta-ads", "needs-auth"))   # nog steeds niet: een nieuwe vraag
        klok.ronde_klaar(self.staat, self.sl, "KOPPELING: meta-ads")
        self.motor.stand[self.sl]["status"] = "klaar"
        self.motor.vragen[-1].update(status="beantwoord", antwoord=klok.KOPPEL_LATER)
        self.tik()
        self.assertEqual(len(self.motor.starts), starts + 1)
        self.assertNotIn("weekronde", self.stand().get("koppelvragen", {}))

    def test_plan_taak_neemt_koppelingen_mee(self):
        m = NepMotorPlan()
        self.staat["app"] = m
        klok.plan_taak(self.staat, "bakkerij", {"naam": "Mailronde", "dagen": [1], "uren": ["09:00"],
                                               "opdracht": PlanTaak.OPDRACHT, "koppelingen": ["google-workspace"]})
        with open(self.staat["rooster"], encoding="utf-8") as fh:
            t = next(x for x in json.load(fh)["taken"] if x["id"] == "mailronde")
        self.assertEqual(t["koppelingen"], ["google-workspace"])
        with self.assertRaises(ValueError):
            klok.plan_taak(self.staat, "bakkerij", {"naam": "Andere", "dagen": [1], "uren": ["09:00"],
                                                   "opdracht": PlanTaak.OPDRACHT, "koppelingen": "meta-ads"})

    def test_rooster_en_kop(self):
        self.assertIsNotNone(klok.ontleed(self.taak(koppelingen="meta-ads"), self.motor, self.map)[1])
        self.assertIsNotNone(klok.ontleed(self.taak(koppelingen=["a b"]), self.motor, self.map)[1])
        self.assertIn(klok.KOPPEL_MERK, self.motor.starts[0]["tekst"])
        self.assertIsNone(klok._koppel_merk("Ronde gedaan. KOPPELING: nee"))


class MotorKlokVraag(MotorStukken):
    def test_stille_vraag_geen_beurt_bij_antwoord(self):
        M = self.M
        M.stand_data["chats"]["bakkerij--r-a"] = {"soort": "taak", "ouder": "bakkerij", "ronde": "a", "in_app": True,
                                                 "status": "klaar"}
        M.chats["bakkerij--r-a"] = self.Stub("bakkerij--r-a", "taak", "bakkerij")
        nr = M.klok_vraag("bakkerij--r-a", "«a» kon niets doen", "uitleg", ["Ja", "Nee"])
        self.assertTrue(M.vragen[-1]["stil"])
        M.antwoord(nr, "Ja")
        self.assertEqual(M.vragen[-1]["status"], "beantwoord")
        self.assertEqual(self.beurten, [])   # de gesloten ronde wordt niet gewekt


if __name__ == "__main__":
    unittest.main(verbosity=1)
