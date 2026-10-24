"""LOGO Getraenkemarkt: Seite lesen und Abruf gegen einen lokalen Server.

Die Testdaten sind echte Antworten von logo-getraenke.de vom 11.10.2026,
gekuerzt (tests/fixtures/logo_*). Darin: das Sternchen-Angebot Braustueb'l Bier
("nicht in allen Filialen"), Gentleman Jack mit "Coca-Cola" im Text, Krombacher
doppelt (oben bei den Wochenangeboten und bei den Bieren) und die Marktliste mit
Dittelbrunn, Schweinfurt und Sennfeld. Aus der Woche ab 12.10.2026 kommt die
Mengenstaffel fuer den Coca-Cola Kombikasten (logo_markt_staffel.json).
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from backend.logo import (LogoClient, LogoError, LogoOffer, euro, parse_market_offers,
                          parse_offers_page, resolve_markets)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEITE = (FIXTURES / "logo_angebote.html").read_text(encoding="utf-8")
MARKT = json.loads((FIXTURES / "logo_markt.json").read_text(encoding="utf-8"))
STAFFEL = json.loads((FIXTURES / "logo_markt_staffel.json").read_text(encoding="utf-8"))


# --- Seite lesen -------------------------------------------------------------

def test_seite_mit_maerkten_zeitraum_und_angeboten():
    seite = parse_offers_page(SEITE)
    assert seite.period == (date(2026, 10, 5), date(2026, 10, 10))
    assert [(m.id, m.name) for m in seite.markets] == [
        ("17373", "Dittelbrunn"), ("17388", "Fulda - Dr.-Raabe-Str."),
        ("17389", "Fulda - Heidelsteinstr."), ("17478", "Schweinfurt"), ("17480", "Sennfeld")]
    assert seite.markets[0].address == "Hauptstr. 1, 97456 Dittelbrunn"
    assert [o.name for o in seite.offers] == [
        "Krombacher Bier", "Guinness Extra Stout", "Krombacher Bier", "Braustüb´l Bier",
        "TWENTYFOUR 7 Energy", "Gentleman Jack", "Jim Beam Whiskey"]
    assert seite.offers[0] == LogoOffer(
        name="Krombacher Bier", description="diverse Sorten, + Sixpack GRATIS!",
        content="20/0,5 Ltr. Kasten + € 3,58 Pfd. Ltr. = € 1,40", price=13.99, old_price=18.99,
        image="96721_z", regional=False)
    assert (seite.offers[0].pack, seite.offers[0].reference_price) == ((20, 0.5), 1.4)


def test_sternchen_heisst_nicht_in_allen_filialen():
    seite = parse_offers_page(SEITE)
    assert [o.name for o in seite.offers if o.regional] == ["Braustüb´l Bier"]


def test_mein_markt_wird_nicht_als_angebot_der_woche_gelesen():
    """Die Seite fuellt "Mein Markt" erst im Browser. Steht dort doch etwas, zaehlt es nicht."""
    seite = SEITE.replace('style="display: none;">\n</div>',
                          'style="display: none;">' + MARKT["products"] + "</div>")
    assert len(parse_offers_page(seite).offers) == 7


def test_angebote_von_mein_markt():
    angebote = parse_market_offers(MARKT, "Dittelbrunn")
    assert [o.name for o in angebote] == [
        "Guinness Extra Stout", "Krombacher Bier", "TWENTYFOUR 7 Energy", "Gentleman Jack"]
    assert not any(o.regional for o in angebote)
    assert angebote[0].image == "98533"            # Bildadresse dort ohne Anfuehrungszeichen
    assert angebote[0].old_price == 16.49


@pytest.mark.parametrize("leer", ["", "  \n  "])
def test_markt_ohne_eigene_liste(leer):
    assert parse_market_offers({"products": leer}, "Dittelbrunn") is None


@pytest.mark.parametrize("antwort", [None, [], {"produkte": ""}, {"products": None}])
def test_kaputte_marktantwort(antwort):
    with pytest.raises(LogoError, match="keine gültige Antwort für den Markt Dittelbrunn"):
        parse_market_offers(antwort, "Dittelbrunn")


def test_marktantwort_in_neuem_format_ist_ein_fehler():
    with pytest.raises(LogoError, match="Markt Dittelbrunn nicht lesbar. Seite geändert"):
        parse_market_offers({"products": "<div class='angebot'>Krombacher 13,99 €</div>"},
                            "Dittelbrunn")


def test_angebot_ohne_preis_faellt_weg_alle_ohne_preis_ist_ein_fehler():
    ohne_guinness = MARKT["products"].replace("<span>12.99 €</span>", "<span></span>", 1)
    assert [o.name for o in parse_market_offers({"products": ohne_guinness}, "D")][0] == "Krombacher Bier"
    ohne_preise = re.sub(r"\d+\.\d\d €", "", MARKT["products"])
    with pytest.raises(LogoError, match="keines von 4 Angeboten lesbar"):
        parse_market_offers({"products": ohne_preise}, "D")


def test_vergessenes_endtag_verschiebt_nichts():
    kaputt = MARKT["products"].replace("<div class=\"product-desc\">", "<p><div class=\"product-desc\">")
    assert len(parse_market_offers({"products": kaputt}, "D")) == 4


@pytest.mark.parametrize("kopf,erwartet", [
    ("05.10.-10.10.2026", (date(2026, 10, 5), date(2026, 10, 10))),
    ("28.12.-02.01.2027", (date(2026, 12, 28), date(2027, 1, 2))),
    ("12.10.2026 – 17.10.2026", (date(2026, 10, 12), date(2026, 10, 17))),
])
def test_zeitraum(kopf, erwartet):
    assert parse_offers_page(SEITE.replace("05.10.-10.10.2026", kopf)).period == erwartet


@pytest.mark.parametrize("kopf", ["", "10.10.-05.10.2026", "31.02.-05.03.2026"])
def test_zeitraum_aus_dem_handzettel_wenn_der_kopf_nicht_passt(kopf):
    """Link zum Handzettel /2026KW41/: Kalenderwoche 41, Montag bis Samstag."""
    seite = SEITE.replace("05.10.-10.10.2026", kopf)
    assert parse_offers_page(seite).period == (date(2026, 10, 5), date(2026, 10, 10))


def test_ohne_zeitraum():
    seite = SEITE.replace("05.10.-10.10.2026", "diese Woche").replace("2026KW41", "aktuell")
    assert parse_offers_page(seite).period is None


@pytest.mark.parametrize("inhalt,packung,grundpreis", [
    ("20/0,5 Ltr. Kasten + € 3,58 Pfd. Ltr. = € 1,40", (20, 0.5), 1.4),
    ("12/1,0 Ltr. Kasten + € 3,30 Pfd. Ltr. = € 1,08", (12, 1.0), 1.08),
    ("1 Ltr. Flasche + € 0,25 Pfd. Ltr. = € 1,39", (1, 1.0), 1.39),
    ("0,33 Ltr. Flasche + € 0,25 Pfd. Ltr. = € 9,06", (1, 0.33), 9.06),
    ("20/0,5 Ltr. Kasten + € 3,10/4,50 Pfd. Ltr. = € 1,50", (20, 0.5), 1.5),
    ("24/330 ml Dose + € 6,00 Pfd.", (24, 0.33), None),
    ("Geschenkset", None, None),
])
def test_packung_und_grundpreis(inhalt, packung, grundpreis):
    angebot = LogoOffer("X", "", inhalt, 1.0, None, "", False)
    assert (angebot.pack, angebot.reference_price) == (packung, grundpreis)


def test_mengenstaffel_aus_der_echten_woche():
    """Drei Preise fuer denselben Kasten. Die Stufe steht nur in der Beschreibung."""
    angebote = parse_market_offers(STAFFEL, "Dittelbrunn")
    assert [(o.name, o.price, o.condition) for o in angebote] == [
        ("Paulaner Spezi, Limo, Cola", 12.99, None),
        ("Coca-Cola Kombikasten", 11.49, "ab 3 Kästen"),
        ("Coca-Cola Kombikasten", 12.49, "bei 2 Kästen"),
        ("Coca-Cola Kombikasten", 13.49, "bei 1 Kasten")]
    assert len({o.key for o in angebote}) == 4      # jede Stufe ein eigenes Angebot
    assert [(o.pack, o.reference_price) for o in angebote[1:]] == [
        ((12, 1.0), 0.96), ((12, 1.0), 1.04), ((12, 1.0), 1.12)]


@pytest.mark.parametrize("beschreibung,stufe", [
    ("mehr kaufen - zusätzlich sparen! Ab 3 Kasten - Pro Kasten", "ab 3 Kästen"),
    ("mehr kaufen - zusätzlich sparen! 2 Kasten - Pro Kasten", "bei 2 Kästen"),
    ("1 Kasten – pro Kasten", "bei 1 Kasten"),
    ("ab 2 Kisten - pro Kiste", "ab 2 Kisten"),
    ("diverse Sorten, + Sixpack GRATIS!", None),
    ("diverse Sorten, teilweise koffeinhaltig, NEU im Sortiment - Cola!", None),
    ("0 Kasten - Pro Kasten", None),
    ("Art.-Nr. 123 Kasten - Pro Kasten", None),
])
def test_stufe_einer_mengenstaffel(beschreibung, stufe):
    assert LogoOffer("X", beschreibung, "", 1.0, None, "", False).condition == stufe


@pytest.mark.parametrize("text,betrag", [
    ("13.99 €", 13.99), ("13,99 €", 13.99), ("1.039,00 €", 1039.0), ("9 €", 9.0),
    ("€", None), ("", None), ("1.2.3", None),
])
def test_preise(text, betrag):
    assert euro(text) == betrag


def test_maerkte_gross_klein_egal_und_mit_vorschlaegen():
    maerkte = parse_offers_page(SEITE).markets
    gewaehlt = resolve_markets(maerkte, ("dittelbrunn", "SCHWEINFURT", " Sennfeld", "Dittelbrunn"))
    assert [m.id for m in gewaehlt] == ["17373", "17478", "17480"]
    with pytest.raises(LogoError, match=r"Markt 'Fulda' nicht gefunden\. Ähnlich: "
                                        r"Fulda - Dr\.-Raabe-Str\., Fulda - Heidelsteinstr\."):
        resolve_markets(maerkte, ("Fulda",))
    with pytest.raises(LogoError, match="stehen in der Marktliste auf logo-getraenke.de"):
        resolve_markets(maerkte, ("Hamburg",))
    with pytest.raises(LogoError, match="Marktliste nicht gefunden"):
        resolve_markets([], ("Dittelbrunn",))


# --- Abruf -------------------------------------------------------------------

class Logo:
    """Zustand des Testservers: Seite, Antworten je Markt, Mitschrift."""

    def __init__(self):
        self.seite = SEITE
        self.status = 200
        self.markt: dict[str, tuple[int, str]] = {}     # Nummer -> (Status, Text)
        self.abrufe: list[dict] = []


@pytest.fixture
def server():
    zustand = Logo()

    class Handler(BaseHTTPRequestHandler):
        def antworten(self, status, body):
            daten = body.encode("utf-8")
            self.send_response(status)
            # Wie WordPress: auch die JSON-Antwort kommt als text/html.
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("Content-Length", str(len(daten)))
            self.end_headers()
            self.wfile.write(daten)

        def do_GET(self):
            zustand.abrufe.append({"art": "GET", "pfad": self.path, "headers": dict(self.headers)})
            if self.path == "/angebote/":
                return self.antworten(zustand.status, zustand.seite)
            return self.antworten(404, "")

        def do_POST(self):
            laenge = int(self.headers.get("Content-Length", 0))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(laenge).decode("utf-8")).items()}
            zustand.abrufe.append({"art": "POST", "pfad": self.path, "form": form,
                                   "headers": dict(self.headers)})
            if self.path != "/wp-admin/admin-ajax.php" or form.get("action") != "my_product_view":
                return self.antworten(400, "0")
            return self.antworten(*zustand.markt.get(form.get("marketid"), (200, json.dumps(MARKT))))

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    basis = f"http://127.0.0.1:{httpd.server_port}"
    zustand.client = LogoClient(offers_url=f"{basis}/angebote/",
                                ajax_url=f"{basis}/wp-admin/admin-ajax.php", timeout=5)
    try:
        yield zustand
    finally:
        httpd.shutdown()
        httpd.server_close()


def holen(client, *maerkte):
    return asyncio.run(client.fetch(tuple(maerkte)))


def test_drei_maerkte_wie_mein_markt_auf_der_seite(server):
    woche = holen(server.client, "Dittelbrunn", "Schweinfurt", "Sennfeld")
    assert (woche.valid_from, woche.valid_to) == (date(2026, 10, 5), date(2026, 10, 10))
    assert list(woche.by_market) == ["Dittelbrunn", "Schweinfurt", "Sennfeld"]
    assert all(len(angebote) == 4 for angebote in woche.by_market.values())
    posts = [a for a in server.abrufe if a["art"] == "POST"]
    assert sorted(a["form"]["marketid"] for a in posts) == ["17373", "17478", "17480"]
    assert [a["art"] for a in server.abrufe].count("GET") == 1
    assert all(a["headers"]["User-Agent"].startswith("Angebotsfinder") for a in server.abrufe)


def test_ohne_maerkte_alle_angebote_der_woche(server):
    woche = holen(server.client)
    assert list(woche.by_market) == [""]
    assert len(woche.by_market[""]) == 7
    assert not [a for a in server.abrufe if a["art"] == "POST"]


def test_markt_ohne_eigene_liste_bekommt_alle_angebote_der_woche(server):
    server.markt["17478"] = (200, json.dumps({"products": ""}))
    woche = holen(server.client, "Dittelbrunn", "Schweinfurt")
    assert len(woche.by_market["Dittelbrunn"]) == 4
    assert len(woche.by_market["Schweinfurt"]) == 7


def test_unbekannter_markt(server):
    with pytest.raises(LogoError, match="Markt 'Schweinfurth' nicht gefunden. Ähnlich: Schweinfurt"):
        holen(server.client, "Schweinfurth")


def test_angebotsseite_nicht_da_oder_geaendert(server):
    server.status = 503
    with pytest.raises(LogoError, match="Angebotsseite antwortet mit HTTP 503"):
        holen(server.client, "Dittelbrunn")
    server.status = 200
    server.seite = "<html><body>Wartungsarbeiten</body></html>"
    with pytest.raises(LogoError, match="Zeitraum der Wochenangebote nicht gefunden"):
        holen(server.client)
    server.seite = SEITE.replace("prodcut-details", "angebot-karte")
    with pytest.raises(LogoError, match="keine Angebote auf der Seite gefunden"):
        holen(server.client)
    server.seite = SEITE.replace("button_cookie", "knopf")
    with pytest.raises(LogoError, match="Marktliste nicht gefunden"):
        holen(server.client, "Dittelbrunn")


def test_marktabfrage_mit_fehler(server):
    server.markt["17480"] = (500, "")
    with pytest.raises(LogoError, match="HTTP 500 bei den Angeboten für Sennfeld"):
        holen(server.client, "Dittelbrunn", "Sennfeld")
    server.markt["17480"] = (200, "<html>kein JSON</html>")
    with pytest.raises(LogoError, match="keine gültige Antwort für den Markt Sennfeld"):
        holen(server.client, "Sennfeld")


def test_nicht_erreichbar():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        frei = s.getsockname()[1]
    client = LogoClient(offers_url=f"http://127.0.0.1:{frei}/angebote/", timeout=2)
    # Linux lehnt die Verbindung sofort ab, Windows laesst sie ins Zeitlimit laufen.
    with pytest.raises(LogoError, match="nicht erreichbar|antwortet nicht"):
        holen(client, "Dittelbrunn")
