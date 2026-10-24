"""marktguru-Abfrage gegen einen lokalen Server statt gegen marktguru.de.

Der Server spielt Startseite (mit den Schluesseln im Quelltext, Aufbau wie am
10.10.2026) und Suche (prueft die Schluessel wie das Original: ohne sie HTTP 401).
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from backend.marktguru import MarktguruClient, MarktguruError, parse_keys

STARTSEITE = ('<!doctype html><html><body><footer>2026 marktguru</footer>'
              '<script type="application/json">{"config":{"apiHostAddress":"api.marktguru.de",'
              '"apiKey":"%s","clientKey":"%s","tagManagerKey":"GTM-X"}}</script>'
              '<script src="/app.js"></script></body></html>')


class Marktguru:
    """Zustand des Testservers: aktuelle Schluessel, Antworten, Mitschrift."""

    def __init__(self):
        self.keys = ("K1", "C1")
        self.startseite = None          # None = normale Startseite mit self.keys
        self.antwort = None             # None = Treffer je Suchbegriff
        self.home_loads = 0
        self.suchen: list[dict] = []


@pytest.fixture
def server():
    zustand = Marktguru()

    class Handler(BaseHTTPRequestHandler):
        def antworten(self, status, body, art="application/json"):
            daten = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", art)
            self.send_header("Content-Length", str(len(daten)))
            self.end_headers()
            self.wfile.write(daten)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                zustand.home_loads += 1
                html = zustand.startseite or STARTSEITE % zustand.keys
                return self.antworten(200, html, "text/html; charset=utf-8")
            if url.path == "/api/v1/offers/search":
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                zustand.suchen.append({"params": params, "headers": dict(self.headers)})
                if (self.headers.get("x-apikey"), self.headers.get("x-clientkey")) != zustand.keys:
                    return self.antworten(401, '"Invalid or missing api key"')
                if zustand.antwort is not None:
                    return self.antworten(*zustand.antwort)
                treffer = [{"id": 1, "description": f"Treffer fuer {params['q']}"}]
                return self.antworten(200, json.dumps({"totalResults": 1, "results": treffer}))
            return self.antworten(404, "{}")

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    basis = f"http://127.0.0.1:{httpd.server_port}"
    zustand.client = MarktguruClient(home_url=f"{basis}/", api_base=f"{basis}/api/v1", timeout=5)
    try:
        yield zustand
    finally:
        httpd.shutdown()
        httpd.server_close()


def suchen(client, *begriffe, plz="97421"):
    return asyncio.run(client.search_many(list(begriffe), plz))


def test_suche_mit_den_schluesseln_von_der_startseite(server):
    ergebnis = suchen(server.client, "Monster Energy", "Coca-Cola")
    assert ergebnis == {"Monster Energy": [{"id": 1, "description": "Treffer fuer Monster Energy"}],
                        "Coca-Cola": [{"id": 1, "description": "Treffer fuer Coca-Cola"}]}
    assert server.home_loads == 1                       # einmal fuer beide Suchen
    erste = server.suchen[0]
    assert {k: erste["params"][k] for k in ("as", "zipCode", "offset")} == {
        "as": "web", "zipCode": "97421", "offset": "0"}
    assert erste["headers"]["User-Agent"].startswith("Angebotsfinder")
    assert sorted(s["params"]["q"] for s in server.suchen) == ["Coca-Cola", "Monster Energy"]


def test_schluessel_bleiben_im_zwischenspeicher(server):
    suchen(server.client, "Cola")
    suchen(server.client, "Bier")
    assert server.home_loads == 1


def test_gewechselte_schluessel_werden_einmal_neu_geholt(server):
    suchen(server.client, "Cola")
    server.keys = ("K2", "C2")
    assert suchen(server.client, "Cola")["Cola"][0]["id"] == 1
    assert server.home_loads == 2


def test_abgelehnte_schluessel_sind_ein_klarer_fehler(server):
    server.startseite = STARTSEITE % ("alt", "alt")
    with pytest.raises(MarktguruError, match="HTTP 401"):
        suchen(server.client, "Cola")
    assert server.home_loads == 2                       # einmal neu versucht, nicht endlos


def test_startseite_ohne_schluessel(server):
    server.startseite = "<html><body>Wartungsarbeiten</body></html>"
    with pytest.raises(MarktguruError, match="Schlüssel auf der Startseite nicht gefunden"):
        suchen(server.client, "Cola")


def test_serverfehler(server):
    server.antwort = (503, '{"error": "busy"}')
    with pytest.raises(MarktguruError, match="HTTP 503 bei der Suche nach 'Cola'"):
        suchen(server.client, "Cola")


def test_keine_json_antwort(server):
    server.antwort = (200, "<html>Fehler</html>")
    with pytest.raises(MarktguruError, match="keine gültige Antwort"):
        suchen(server.client, "Cola")


def test_antwort_ohne_trefferliste(server):
    server.antwort = (200, '{"offers": []}')
    with pytest.raises(MarktguruError, match="ohne Trefferliste"):
        suchen(server.client, "Cola")


def test_nicht_erreichbar():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        frei = s.getsockname()[1]
    client = MarktguruClient(home_url=f"http://127.0.0.1:{frei}/", timeout=2)
    # Linux lehnt die Verbindung sofort ab, Windows laesst sie ins Zeitlimit laufen.
    with pytest.raises(MarktguruError, match="nicht erreichbar|antwortet nicht"):
        suchen(client, "Cola")


def test_schluessel_aus_dem_quelltext():
    assert parse_keys(STARTSEITE % ("A+b/c=", "D/e+f=")) == ("api.marktguru.de", "A+b/c=", "D/e+f=")
    with pytest.raises(MarktguruError, match="unlesbar"):
        parse_keys('<script type="application/json">{"config":{"apiKey":"x"}}</script>')
