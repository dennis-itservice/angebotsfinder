"""Prospekt-Check im Ablauf: Termine, Neustart, Aenderung der Merkliste, Fehler.

marktguru ist durch einen Ersatz ersetzt, der die echten Antworten vom
10.10.2026 liefert (tests/fixtures), LOGO durch einen mit einem Kasten
Coca-Cola in der Woche ab Mo 12.10. Die Uhr wird von Hand gestellt.
Uebernommen aus Sentinel (test_angebote_monitor.py).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import backend.monitor as monitor_modul
from backend.logo import LogoError, LogoOffer, LogoWeek
from backend.marktguru import MarktguruError
from backend.monitor import AngeboteMonitor, ServiceStatus

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMSTAG = datetime(2026, 10, 10, 12, 0)


def fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["results"]


MONSTER = fixture("marktguru_monster.json")
COLA = fixture("marktguru_cola.json")

# Mit Lidl, damit es heute einen Treffer gibt: Monster Energy ab Mo 12.10. fuer 0,75 €.
MERKLISTE = """\
zip_code: "97421"
shops: [EDEKA, REWE, Lidl]
products:
  - name: Monster Energy
  - name: Coca-Cola
    pack: 12x1l
"""


class FakeClient:
    def __init__(self, antworten=None, fehler=None, haengt=False):
        self.antworten = antworten if antworten is not None else {
            "Monster Energy": MONSTER, "Coca-Cola": COLA}
        self.fehler = fehler
        self.haengt = haengt
        self.aufrufe: list[tuple[tuple[str, ...], str]] = []

    async def search_many(self, queries, zip_code, limit=200):
        self.aufrufe.append((tuple(queries), zip_code))
        if self.haengt:
            await asyncio.sleep(10)
        if self.fehler:
            raise self.fehler
        return {q: self.antworten.get(q, []) for q in queries}


COLA_KASTEN = LogoOffer("Coca-Cola, Fanta, Sprite, Mezzo Mix", "diverse Sorten",
                        "12/1,0 Ltr. Kasten + € 3,30 Pfd. Ltr. = € 1,08", 12.99, 15.99, "1", False)
LOGO_MAERKTE = ("Dittelbrunn", "Schweinfurt", "Sennfeld")
MIT_LOGO = MERKLISTE.replace("shops: [EDEKA, REWE, Lidl]",
                             "shops:\n  - EDEKA\n  - REWE\n  - Lidl\n"
                             "  - LOGO: [Dittelbrunn, Schweinfurt, Sennfeld]")


class FakeLogo:
    def __init__(self, fehler=None):
        self.angebote = [COLA_KASTEN]
        self.fehler = fehler
        self.aufrufe: list[tuple[str, ...]] = []

    async def fetch(self, markets=()):
        self.aufrufe.append(tuple(markets))
        if self.fehler:
            raise self.fehler
        # Wie LogoClient: je gewaehltem Markt eine Liste, ohne Marktwahl eine fuer alle.
        return LogoWeek(date(2026, 10, 12), date(2026, 10, 17),
                        {markt: self.angebote for markt in markets} if markets
                        else {"": self.angebote})


class Uhr:
    def __init__(self, jetzt: datetime):
        self.jetzt = jetzt

    def __call__(self) -> datetime:
        return self.jetzt


def bauen(tmp_path, text=MERKLISTE, client=None, jetzt=SAMSTAG, logo=None):
    datei = tmp_path / "config" / "angebote.yaml"
    datei.parent.mkdir(exist_ok=True)
    if text is not None:
        datei.write_text(text, encoding="utf-8")
    monitor = AngeboteMonitor(datei, label="config/angebote.yaml",
                              client=client or FakeClient(), logo=logo or FakeLogo())
    monitor.clock = Uhr(jetzt)
    monitor.use_data_dir(tmp_path / "data")
    return monitor, datei


def pruefen(monitor, jetzt=None):
    if jetzt is not None:
        monitor.clock.jetzt = jetzt
    return asyncio.run(monitor.check())


# --- Erster Start und Termine ------------------------------------------------

def test_erster_start_prueft_sofort_und_meldet(tmp_path):
    monitor, _ = bauen(tmp_path)
    ergebnis = pruefen(monitor)

    assert monitor.client.aufrufe == [(("Monster Energy", "Coca-Cola"), "97421")]
    assert ergebnis.status is ServiceStatus.OK
    assert ergebnis.message == "Monster Energy: Lidl 0,75 € (Mo 12.10.–Sa 17.10.) mit App"
    assert ergebnis.notifications == [{
        "title": "Angebot: Monster Energy", "body": "Lidl 0,75 € (Mo 12.10.–Sa 17.10.) mit App",
        "tag": "angebote", "url": "/", "kind": "info"}]
    assert ergebnis.details == {
        "hits": 1, "last_run": "2026-10-10T12:00:00", "next_run": "2026-10-11T08:00:00",
        "subtitle": "EDEKA, REWE, Lidl · PLZ 97421 · Mo, Mi, Fr, So um 08:00"}
    assert (tmp_path / "data" / "angebote.json").exists()


def test_danach_nur_zu_den_terminen_und_ohne_doppelte_meldung(tmp_path):
    monitor, _ = bauen(tmp_path)
    pruefen(monitor)
    zwischendurch = pruefen(monitor, SAMSTAG + timedelta(minutes=1))
    assert len(monitor.client.aufrufe) == 1
    assert zwischendurch.notifications == []           # die Meldung kommt nur einmal
    assert zwischendurch.status is ServiceStatus.OK

    pruefen(monitor, datetime(2026, 10, 11, 7, 59))
    assert len(monitor.client.aufrufe) == 1
    sonntag = pruefen(monitor, datetime(2026, 10, 11, 8, 0))
    assert len(monitor.client.aufrufe) == 2
    assert sonntag.notifications == []                 # schon gemeldet


def test_neustart_meldet_nichts_doppelt(tmp_path):
    """Der Stand liegt im Volume und ueberlebt Neustart und Neubau."""
    erster, _ = bauen(tmp_path)
    pruefen(erster)

    zweiter, _ = bauen(tmp_path, jetzt=SAMSTAG + timedelta(minutes=5))
    ergebnis = pruefen(zweiter)
    assert zweiter.client.aufrufe == []                # kein Termin faellig
    assert ergebnis.status is ServiceStatus.OK and ergebnis.details["hits"] == 1
    assert pruefen(zweiter, datetime(2026, 10, 11, 8, 0)).notifications == []
    assert len(zweiter.client.aufrufe) == 1


def test_neustart_holt_verpassten_termin_nach(tmp_path):
    erster, _ = bauen(tmp_path, jetzt=datetime(2026, 10, 9, 8, 0))   # Freitag
    pruefen(erster)
    # Container war am Sonntag um 8 aus und startet um 10 Uhr
    zweiter, _ = bauen(tmp_path, jetzt=datetime(2026, 10, 11, 10, 0))
    pruefen(zweiter)
    assert len(zweiter.client.aufrufe) == 1


def test_neues_angebot_wird_gemeldet_bekannte_nicht(tmp_path):
    monitor, _ = bauen(tmp_path)
    pruefen(monitor)

    rewe = json.loads(json.dumps(MONSTER[0]))
    rewe.update(id=1, advertisers=[{"name": "REWE"}], price=0.99, requiresLoyalityMembership=False)
    monitor.client.antworten["Monster Energy"] = MONSTER + [rewe]
    ergebnis = pruefen(monitor, datetime(2026, 10, 11, 8, 0))
    assert [n["body"] for n in ergebnis.notifications] == ["REWE 0,99 € (Mo 12.10.–Sa 17.10.)"]
    assert ergebnis.details["hits"] == 2


# --- Merkliste aendern -------------------------------------------------------

def test_aenderung_der_merkliste_prueft_sofort(tmp_path):
    monitor, datei = bauen(tmp_path)
    pruefen(monitor)
    datei.write_text(MERKLISTE + "  - name: Red Bull\n", encoding="utf-8")
    pruefen(monitor, SAMSTAG + timedelta(minutes=1))
    assert monitor.client.aufrufe[-1] == (("Monster Energy", "Coca-Cola", "Red Bull"), "97421")


def test_nur_kommentare_geaendert_prueft_nicht(tmp_path):
    monitor, datei = bauen(tmp_path)
    pruefen(monitor)
    datei.write_text("# Einkauf\n" + MERKLISTE.replace("[EDEKA, REWE, Lidl]",
                                                         "[EDEKA, REWE, Lidl]   # Laeden"),
                     encoding="utf-8")
    pruefen(monitor, SAMSTAG + timedelta(minutes=1))
    assert len(monitor.client.aufrufe) == 1


def test_tippfehler_legt_nichts_lahm(tmp_path):
    monitor, datei = bauen(tmp_path)
    pruefen(monitor)
    datei.write_text(MERKLISTE.replace("pack:", "packung:"), encoding="utf-8")
    kaputt = pruefen(monitor, SAMSTAG + timedelta(minutes=1))
    assert kaputt.status is ServiceStatus.WARNING
    assert "packung" in kaputt.message and "letzte gültige Stand" in kaputt.message
    assert kaputt.details["hits"] == 1                 # alter Stand bleibt sichtbar
    assert len(monitor.client.aufrufe) == 1

    # Am naechsten Termin wird mit dem letzten gueltigen Stand weiter geprueft.
    pruefen(monitor, datetime(2026, 10, 11, 8, 0))
    assert len(monitor.client.aufrufe) == 2

    datei.write_text(MERKLISTE, encoding="utf-8")
    assert pruefen(monitor, datetime(2026, 10, 11, 8, 1)).status is ServiceStatus.OK


def test_fehlende_merkliste_beim_start(tmp_path):
    """Der haeufigste Fehler beim Einrichten: Vorlage nicht kopiert."""
    monitor, _ = bauen(tmp_path, text=None)
    ergebnis = pruefen(monitor)
    assert ergebnis.status is ServiceStatus.WARNING
    assert ergebnis.message == ("config/angebote.yaml fehlt. Bitte die Vorlage "
                                "config/angebote.example.yaml als angebote.yaml kopieren")
    assert monitor.request_run(SAMSTAG) == 0
    assert pruefen(monitor).status is ServiceStatus.WARNING
    assert monitor.client.aufrufe == []


def test_merkliste_nicht_lesbar(tmp_path):
    """Zum Beispiel, weil der Container (UID 10001) keine Leserechte hat."""
    monitor, datei = bauen(tmp_path, text=None)
    datei.mkdir()                                      # Verzeichnis statt Datei: OSError
    ergebnis = pruefen(monitor)
    assert ergebnis.status is ServiceStatus.WARNING
    assert ergebnis.message.startswith("config/angebote.yaml nicht lesbar:")


# --- Fehler beim Abruf -------------------------------------------------------

def test_fehlgeschlagener_abruf_wird_stuendlich_wiederholt(tmp_path):
    client = FakeClient(fehler=MarktguruError("marktguru: HTTP 503 bei der Suche nach 'Cola'"))
    monitor, _ = bauen(tmp_path, client=client)
    ergebnis = pruefen(monitor)
    assert ergebnis.status is ServiceStatus.WARNING
    assert ergebnis.message == "Abruf fehlgeschlagen: marktguru: HTTP 503 bei der Suche nach 'Cola'."

    pruefen(monitor, SAMSTAG + timedelta(minutes=59))
    assert len(client.aufrufe) == 1
    client.fehler = None
    erholt = pruefen(monitor, SAMSTAG + timedelta(minutes=60))
    assert len(client.aufrufe) == 2
    assert erholt.status is ServiceStatus.OK
    assert len(erholt.notifications) == 1


def test_neue_merkliste_wird_trotz_fehler_sofort_versucht(tmp_path):
    client = FakeClient(fehler=MarktguruError("weg"))
    monitor, datei = bauen(tmp_path, client=client)
    pruefen(monitor)
    datei.write_text(MERKLISTE.replace("97421", "97424"), encoding="utf-8")
    pruefen(monitor, SAMSTAG + timedelta(minutes=2))
    assert client.aufrufe[-1][1] == "97424"


def test_fehler_nach_erfolg_zeigt_den_alten_stand(tmp_path):
    monitor, _ = bauen(tmp_path)
    pruefen(monitor)
    monitor.client.fehler = MarktguruError("marktguru nicht erreichbar: DNS")
    ergebnis = pruefen(monitor, datetime(2026, 10, 11, 8, 0))
    assert ergebnis.status is ServiceStatus.WARNING
    assert ergebnis.message == ("Abruf fehlgeschlagen: marktguru nicht erreichbar: DNS. "
                                "Stand von Sa 10.10. 12:00.")
    assert ergebnis.details["hits"] == 1


def test_haengender_abruf_bricht_nach_zeitlimit_ab(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_modul, "FETCH_TIMEOUT", 0.05)
    monitor, _ = bauen(tmp_path, client=FakeClient(haengt=True))
    ergebnis = pruefen(monitor)
    assert ergebnis.status is ServiceStatus.WARNING
    assert "antwortet nicht" in ergebnis.message
    # Die Wartezeit gilt trotzdem - sonst wuerde jede Minute neu angefragt.
    pruefen(monitor, SAMSTAG + timedelta(minutes=1))
    assert len(monitor.client.aufrufe) == 1


def test_abbruch_durch_das_gesamte_zeitlimit_zaehlt_als_fehler(tmp_path):
    """Bricht das Zeitlimit des ganzen Durchlaufs (Runner, 30 s) den Abruf ab, gilt
    die Wartezeit von einer Stunde trotzdem. Vorher fragte die Schleife dann jede
    Minute neu an (Befund der Review vom 24.10.2026)."""
    monitor, _ = bauen(tmp_path, client=FakeClient(haengt=True))

    async def mit_zeitlimit():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(monitor.check(), timeout=0.05)

    asyncio.run(mit_zeitlimit())
    assert monitor.state.errors == {"marktguru": "marktguru: Abruf abgebrochen (Zeitlimit)"}
    gespeichert = json.loads((tmp_path / "data" / "angebote.json").read_text(encoding="utf-8"))
    assert gespeichert["errors"] == monitor.state.errors

    monitor.client.haengt = False
    ergebnis = pruefen(monitor, SAMSTAG + timedelta(minutes=1))
    assert len(monitor.client.aufrufe) == 1             # keine neue Anfrage nach einer Minute
    assert ergebnis.status is ServiceStatus.WARNING
    assert "Abruf abgebrochen (Zeitlimit)" in ergebnis.message
    pruefen(monitor, SAMSTAG + timedelta(minutes=60))
    assert len(monitor.client.aufrufe) == 2             # nach einer Stunde wieder


def test_zustand_ist_nur_fuer_den_container_lesbar(tmp_path):
    import os
    monitor, _ = bauen(tmp_path)
    pruefen(monitor)
    if os.name != "nt":
        assert oct(os.stat(tmp_path / "data" / "angebote.json").st_mode & 0o777) == "0o600"
    assert [p.name for p in (tmp_path / "data").iterdir()] == ["angebote.json"]


def test_programmfehler_wird_angezeigt_statt_abzustuerzen(tmp_path):
    monitor, _ = bauen(tmp_path, client=FakeClient(fehler=KeyError("results")))
    ergebnis = pruefen(monitor)
    assert ergebnis.status is ServiceStatus.WARNING
    assert "KeyError" in ergebnis.message


# --- Jetzt pruefen, Push aus, Ablauf ------------------------------------------

def test_jetzt_pruefen_mit_kurzer_sperre(tmp_path):
    monitor, _ = bauen(tmp_path)
    pruefen(monitor)
    assert monitor.request_run(SAMSTAG + timedelta(seconds=30)) == pytest.approx(30)
    assert monitor.request_run(SAMSTAG + timedelta(minutes=2)) == 0
    pruefen(monitor, SAMSTAG + timedelta(minutes=2))
    assert len(monitor.client.aufrufe) == 2
    # Der Knopf gilt nur fuer die naechste Pruefung.
    pruefen(monitor, SAMSTAG + timedelta(minutes=3))
    assert len(monitor.client.aufrufe) == 2


def test_push_aus_merkt_trotzdem_was_bekannt_ist(tmp_path):
    """Sonst kaeme beim Einschalten alles auf einmal."""
    monitor, datei = bauen(tmp_path, text=MERKLISTE + "push: false\n")
    ergebnis = pruefen(monitor)
    assert ergebnis.notifications == [] and ergebnis.details["hits"] == 1
    datei.write_text(MERKLISTE + "push: true\n", encoding="utf-8")
    wieder_an = pruefen(monitor, SAMSTAG + timedelta(minutes=1))
    assert len(monitor.client.aufrufe) == 2
    assert wieder_an.notifications == []


def test_abgelaufene_treffer_verschwinden_ohne_neuen_abruf(tmp_path):
    """Die REWE-Cola gilt bis Samstag 23:59 - am Sonntag frueh ist sie weg."""
    monitor, _ = bauen(tmp_path, text='zip_code: "97421"\nshops: [REWE]\nproducts: [Coca-Cola]\n')
    assert pruefen(monitor).details["hits"] == 2       # 6 x 0,33 l und 1,25 l
    sonntag = pruefen(monitor, datetime(2026, 10, 11, 0, 30))
    assert len(monitor.client.aufrufe) == 1
    assert sonntag.details["hits"] == 0
    assert sonntag.message == "Kein Angebot für Coca-Cola"


def test_daten_fuer_die_seite(tmp_path):
    monitor, _ = bauen(tmp_path)
    pruefen(monitor)
    daten = monitor.get_details()
    assert daten["summary"] == {"hits": 1, "current": 0, "upcoming": 1,
                                "last_run": "2026-10-10T12:00:00",
                                "next_run": "2026-10-11T08:00:00",
                                "last_error": None, "watchlist_error": None}
    treffer = daten["matches"][0]
    assert treffer["product"] == "Monster Energy" and treffer["shop"] == "Lidl"
    assert (treffer["price"], treffer["reference_price"], treffer["unit"]) == (0.75, 1.5, "l")
    assert (treffer["valid_from"], treffer["valid_to"]) == ("2026-10-12T00:00", "2026-10-17T23:59")
    assert treffer["is_current"] is False and treffer["app_only"] is True
    assert treffer["url"] == "https://www.marktguru.de/offers/25189233"
    assert daten["products"] == [{"label": "Monster Energy", "hits": 1},
                                 {"label": "Coca-Cola 12×1 l", "hits": 0}]
    assert daten["settings"]["shops"] == ["EDEKA", "REWE", "Lidl"]
    assert daten["settings"]["schedule"] == "Mo, Mi, Fr, So um 08:00"
    assert daten["watchlist_file"] == "config/angebote.yaml"
    # Hilft beim Eintragen: wie die anderen Geschaefte bei marktguru heissen
    assert "EDEKA Foodservice" in daten["other_shops"] and "Lidl" not in daten["other_shops"]


def test_daten_fuer_die_seite_ohne_merkliste(tmp_path):
    monitor, _ = bauen(tmp_path, text=None)
    pruefen(monitor)
    daten = monitor.get_details()
    assert (daten["settings"], daten["products"], daten["matches"]) == (None, [], [])
    assert "fehlt" in daten["summary"]["watchlist_error"]


def test_kaputte_zustandsdatei_fuehrt_nicht_zum_absturz(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "angebote.json").write_text("{kaputt", encoding="utf-8")
    monitor, _ = bauen(tmp_path)
    assert pruefen(monitor).status is ServiceStatus.OK
    assert len(monitor.client.aufrufe) == 1


def test_nicht_beschreibbares_volume_bremst_nicht(tmp_path):
    monitor, _ = bauen(tmp_path)
    blockiert = tmp_path / "keinverzeichnis"
    blockiert.write_text("x")
    monitor.state_file = blockiert / "angebote.json"
    assert pruefen(monitor).status is ServiceStatus.OK


# --- LOGO als zweite Quelle --------------------------------------------------

LOGO_ZEILE = "LOGO Dittelbrunn, Schweinfurt, Sennfeld 12,99 € (Mo 12.10.–Sa 17.10.)"
LIDL_ZEILE = "Lidl 0,75 € (Mo 12.10.–Sa 17.10.) mit App"


def test_logo_kommt_dazu(tmp_path):
    monitor, _ = bauen(tmp_path, text=MIT_LOGO)
    ergebnis = pruefen(monitor)
    assert monitor.logo.aufrufe == [LOGO_MAERKTE]
    assert len(monitor.client.aufrufe) == 1
    assert ergebnis.status is ServiceStatus.OK
    assert ergebnis.notifications[0]["title"] == "Angebote: Monster Energy, Coca-Cola 12×1 l"
    assert ergebnis.notifications[0]["body"] == (f"Monster Energy: {LIDL_ZEILE}\n"
                                                f"Coca-Cola 12×1 l: {LOGO_ZEILE}")
    assert ergebnis.details["subtitle"].startswith("EDEKA, REWE, Lidl, LOGO · PLZ 97421")

    daten = monitor.get_details()
    logo = next(t for t in daten["matches"] if t["source"] == "logo")
    assert logo["shop"] == "LOGO Dittelbrunn, Schweinfurt, Sennfeld"
    assert logo["url"] == "https://www.logo-getraenke.de/angebote/"
    assert (logo["reference_price"], logo["unit"], logo["regional"], logo["condition"]) == (
        1.08, "l", False, "")
    assert daten["settings"]["shops"] == ["EDEKA", "REWE", "Lidl",
                                          "LOGO (Dittelbrunn, Schweinfurt, Sennfeld)"]
    assert daten["source"] == ("marktguru.de (Prospekte der Region) und "
                               "logo-getraenke.de (LOGO direkt)")
    assert "LOGO" not in daten["other_shops"]


def test_nur_logo_fragt_marktguru_nicht(tmp_path):
    monitor, _ = bauen(tmp_path, text=MERKLISTE.replace("[EDEKA, REWE, Lidl]", "[LOGO: [Sennfeld]]"))
    ergebnis = pruefen(monitor)
    assert monitor.client.aufrufe == [] and monitor.logo.aufrufe == [("Sennfeld",)]
    assert ergebnis.message == "Coca-Cola 12×1 l: LOGO Sennfeld 12,99 € (Mo 12.10.–Sa 17.10.)"
    daten = monitor.get_details()
    assert daten["source"] == "logo-getraenke.de (LOGO direkt)"
    assert daten["other_shops"] == []


def test_logo_faellt_aus_marktguru_liefert_trotzdem(tmp_path):
    logo = FakeLogo(fehler=LogoError("LOGO: Zeitraum der Wochenangebote nicht gefunden. "
                                     "Seite geändert?"))
    monitor, _ = bauen(tmp_path, text=MIT_LOGO, logo=logo)
    ergebnis = pruefen(monitor)
    assert ergebnis.status is ServiceStatus.WARNING
    assert ergebnis.message == ("Abruf fehlgeschlagen: LOGO: Zeitraum der Wochenangebote nicht "
                                "gefunden. Seite geändert?")
    assert ergebnis.details["hits"] == 1                # Lidl von marktguru
    assert [n["body"] for n in ergebnis.notifications] == [LIDL_ZEILE]

    # Stuendlich nachholen, und zwar nur LOGO.
    pruefen(monitor, SAMSTAG + timedelta(minutes=59))
    assert len(logo.aufrufe) == 1
    logo.fehler = None
    erholt = pruefen(monitor, SAMSTAG + timedelta(minutes=60))
    assert len(logo.aufrufe) == 2 and len(monitor.client.aufrufe) == 1
    assert erholt.status is ServiceStatus.OK and erholt.details["hits"] == 2
    assert [n["body"] for n in erholt.notifications] == [LOGO_ZEILE]   # Lidl nicht noch einmal

    # Danach Ruhe bis zum naechsten Termin.
    pruefen(monitor, SAMSTAG + timedelta(hours=3))
    assert len(logo.aufrufe) == 2


def test_logo_fehler_nach_erfolg_behaelt_die_logo_treffer(tmp_path):
    monitor, _ = bauen(tmp_path, text=MIT_LOGO)
    pruefen(monitor)
    monitor.logo.fehler = LogoError("LOGO nicht erreichbar: DNS")
    ergebnis = pruefen(monitor, datetime(2026, 10, 11, 8, 0))
    assert ergebnis.status is ServiceStatus.WARNING
    assert ergebnis.message == "Abruf fehlgeschlagen: LOGO nicht erreichbar: DNS. Stand von Sa 10.10. 12:00."
    assert ergebnis.details["hits"] == 2                # der LOGO-Treffer vom Samstag bleibt
    assert ergebnis.notifications == []
    assert len(monitor.client.aufrufe) == 2             # marktguru ist frisch


def test_marktguru_faellt_aus_logo_liefert(tmp_path):
    client = FakeClient(fehler=MarktguruError("marktguru: HTTP 503 bei der Suche nach 'Cola'"))
    monitor, _ = bauen(tmp_path, text=MIT_LOGO, client=client)
    ergebnis = pruefen(monitor)
    assert ergebnis.status is ServiceStatus.WARNING
    assert ergebnis.message == "Abruf fehlgeschlagen: marktguru: HTTP 503 bei der Suche nach 'Cola'."
    assert [n["body"] for n in ergebnis.notifications] == [LOGO_ZEILE]
    client.fehler = None
    erholt = pruefen(monitor, SAMSTAG + timedelta(minutes=60))
    assert len(client.aufrufe) == 2 and len(monitor.logo.aufrufe) == 1
    assert erholt.status is ServiceStatus.OK
    assert [n["body"] for n in erholt.notifications] == [LIDL_ZEILE]


def test_beide_fallen_aus(tmp_path):
    monitor, _ = bauen(tmp_path, text=MIT_LOGO, client=FakeClient(fehler=MarktguruError("marktguru: weg")),
                       logo=FakeLogo(fehler=LogoError("LOGO: weg")))
    ergebnis = pruefen(monitor)
    assert ergebnis.status is ServiceStatus.WARNING
    assert ergebnis.message == "Abruf fehlgeschlagen: marktguru: weg. Abruf fehlgeschlagen: LOGO: weg."
    assert monitor.state.last_run_at is None
    pruefen(monitor, SAMSTAG + timedelta(minutes=30))
    assert len(monitor.logo.aufrufe) == 1               # auch dann nur stuendlich


def test_haengender_logo_abruf_haelt_marktguru_nicht_auf(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_modul, "FETCH_TIMEOUT", 0.05)

    class Haengt(FakeLogo):
        async def fetch(self, markets=()):
            await asyncio.sleep(10)

    monitor, _ = bauen(tmp_path, text=MIT_LOGO, logo=Haengt())
    ergebnis = pruefen(monitor)
    assert ergebnis.message == "Abruf fehlgeschlagen: LOGO antwortet nicht (über 0 s)."
    assert ergebnis.details["hits"] == 1


def test_logo_wieder_aus_der_merkliste(tmp_path):
    monitor, datei = bauen(tmp_path, text=MIT_LOGO)
    pruefen(monitor)
    datei.write_text(MERKLISTE, encoding="utf-8")
    ergebnis = pruefen(monitor, SAMSTAG + timedelta(minutes=1))
    assert ergebnis.details["hits"] == 1                # der LOGO-Treffer ist weg
    assert len(monitor.logo.aufrufe) == 1


def test_logo_programmfehler_wird_angezeigt(tmp_path):
    monitor, _ = bauen(tmp_path, text=MIT_LOGO, logo=FakeLogo(fehler=KeyError("products")))
    ergebnis = pruefen(monitor)
    assert ergebnis.message == "Abruf fehlgeschlagen: LOGO: KeyError: 'products'."
