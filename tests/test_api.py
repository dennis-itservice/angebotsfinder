"""HTTP-Ebene: Ersteinrichtung, Anmeldung, Schutz der Seiten, Angebote, Push."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend import auth
from backend.monitor import CheckResult, ServiceStatus
from backend.push import device_id

from test_monitor import MERKLISTE, SAMSTAG, FakeClient, FakeLogo, Uhr

PASSWORT = "Sommer-2026!"
FCM = "https://fcm.googleapis.com/fcm/send/geraet1"
SUB = {"endpoint": FCM, "keys": {"p256dh": "B" + "x" * 86, "auth": "a" * 22}}


class FakeWebPush:
    def __init__(self):
        self.aufrufe = []

    async def __call__(self, **kwargs):
        self.aufrufe.append(kwargs)


def app_mit(tmp_path, eingerichtet=True, data_dir=None, merkliste=MERKLISTE):
    datei = tmp_path / "config" / "angebote.yaml"
    datei.parent.mkdir(parents=True, exist_ok=True)
    if merkliste is not None:
        datei.write_text(merkliste, encoding="utf-8")
    data_dir = data_dir or tmp_path / "data"
    if eingerichtet:
        auth.CredentialStore(data_dir).save("anna", PASSWORT, datetime(2026, 10, 24, 20, 0))
    fake = FakeWebPush()
    app = main.create_app(data_dir=data_dir, watchlist=datei, watchlist_label="config/angebote.yaml",
                          start_loop=False, client=FakeClient(), logo=FakeLogo(), webpush=fake)
    app.fake = fake
    return app


@pytest.fixture(autouse=True)
def ohne_wartezeit(monkeypatch):
    monkeypatch.setattr(main, "FAILED_LOGIN_DELAY", 0)


@pytest.fixture
def client(tmp_path):
    app = app_mit(tmp_path)
    with TestClient(app, follow_redirects=False) as c:
        c.app_ = app
        yield c


def anmelden(c, user="anna", pw=PASSWORT):
    return c.post("/login", data={"username": user, "password": pw})


def einrichten(c, code, user="anna", pw=PASSWORT, pw2=None):
    return c.post("/setup", data={"username": user, "password": pw,
                                  "password2": pw if pw2 is None else pw2, "code": code})


# --- Ersteinrichtung -------------------------------------------------------

@pytest.fixture
def neu(tmp_path):
    """Frisch installiert: noch kein Zugang."""
    app = app_mit(tmp_path / "neu", eingerichtet=False)
    with TestClient(app, follow_redirects=False) as c:
        c.app_ = app
        c.code = app.state.access.setup_code
        yield c


def test_ohne_zugang_fuehrt_alles_zur_einrichtung(neu):
    assert neu.get("/").headers["location"] == "/login"
    assert neu.get("/login").headers["location"] == "/setup"
    assert neu.post("/login", data={"username": "x", "password": "y"}).headers["location"] == "/setup"
    seite = neu.get("/setup")
    assert seite.status_code == 200 and "Einrichtungscode" in seite.text
    assert "__FEHLER__" not in seite.text and "__BENUTZER__" not in seite.text
    assert neu.get("/api/angebote").status_code == 401


def test_einrichtungscode_steht_im_protokoll(tmp_path, caplog):
    app = app_mit(tmp_path, eingerichtet=False)
    with caplog.at_level(logging.WARNING, logger="angebotsfinder"):
        with TestClient(app):
            code = app.state.access.setup_code
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    assert f"Einrichtungscode: {code}" in caplog.text


def test_einrichtungscode_auch_bei_leisem_protokoll(caplog, monkeypatch):
    """ANGEBOTSFINDER_LOG_LEVEL=ERROR darf den Code nicht verschlucken (Befund der Review)."""
    monkeypatch.setattr(logging.getLogger("angebotsfinder"), "level", logging.ERROR)
    caplog.set_level(logging.DEBUG)
    main.log_setup_code("ABCD-EFGH-JK23")
    assert "Einrichtungscode: ABCD-EFGH-JK23" in caplog.text


def test_einrichtung_mit_falschem_code(neu, caplog):
    antwort = einrichten(neu, "AAAA-BBBB-CCCC")
    assert antwort.status_code == 400
    assert "Einrichtungscode stimmt nicht" in antwort.text
    assert 'value="anna"' in antwort.text                    # Name muss man nicht neu tippen
    assert "Einrichtung mit falschem Code" in caplog.text
    assert not neu.app_.state.access.store.configured


@pytest.mark.parametrize("pw,pw2,meldung", [
    ("kurz", "kurz", "mindestens 10 Zeichen"),
    (PASSWORT, PASSWORT + "!", "stimmen nicht überein"),
])
def test_einrichtung_mit_unbrauchbarem_passwort(neu, pw, pw2, meldung):
    antwort = einrichten(neu, neu.code, pw=pw, pw2=pw2)
    assert antwort.status_code == 400 and meldung in antwort.text
    assert not neu.app_.state.access.store.configured


def test_einrichtung_klappt_und_meldet_gleich_an(neu, tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="angebotsfinder"):
        antwort = einrichten(neu, neu.code.lower().replace("-", " "))
    assert antwort.status_code == 303 and antwort.headers["location"] == "/"
    assert auth.COOKIE_NAME in antwort.headers["set-cookie"]
    assert neu.get("/api/angebote").status_code == 200
    assert "Zugang eingerichtet für Benutzer 'anna'" in caplog.text

    gespeichert = json.loads((tmp_path / "neu" / "data" / "zugang.json").read_text(encoding="utf-8"))
    assert gespeichert["password_hash"].startswith("$argon2id$")
    assert PASSWORT not in json.dumps(gespeichert)

    # Danach ist die Einrichtung zu, auch mit dem alten Code.
    assert neu.app_.state.access.setup_code is None
    assert neu.get("/setup").headers["location"] == "/login"
    zweiter = einrichten(neu, neu.code, user="mallory", pw="Uebernahme-123")
    assert zweiter.headers["location"] == "/login"
    assert neu.app_.state.access.store.verify("anna", PASSWORT)
    assert not neu.app_.state.access.store.verify("mallory", "Uebernahme-123")


def test_benutzername_wird_maskiert(neu):
    antwort = einrichten(neu, "falsch", user='"><script>alert(1)</script>')
    assert "<script>alert(1)" not in antwort.text
    assert "&quot;&gt;&lt;script&gt;" in antwort.text


def test_zugang_ueberlebt_den_neustart(tmp_path):
    app = app_mit(tmp_path, eingerichtet=False)
    with TestClient(app, follow_redirects=False) as c:
        einrichten(c, app.state.access.setup_code)
    # Neuer Container, gleiches Volume
    neu_gestartet = app_mit(tmp_path, eingerichtet=False)
    with TestClient(neu_gestartet, follow_redirects=False) as c:
        assert neu_gestartet.state.access.setup_code is None
        assert c.get("/api/angebote").status_code == 401        # Sessions sind weg
        assert anmelden(c).headers["location"] == "/"
        assert c.get("/api/angebote").status_code == 200


def test_zuruecksetzen_im_laufenden_betrieb(client, tmp_path, caplog):
    """python -m backend.auth zuruecksetzen: Alte Sessions enden sofort, nicht erst
    beim naechsten Aufruf der Anmeldeseite (Befund der Review). Danach neu einrichten."""
    anmelden(client)
    assert client.get("/api/angebote").status_code == 200
    auth.CredentialStore(tmp_path / "data").reset()
    with caplog.at_level(logging.WARNING, logger="angebotsfinder"):
        assert client.get("/api/angebote").status_code == 401
    code = client.app_.state.access.setup_code
    assert code and f"Einrichtungscode: {code}" in caplog.text
    assert client.get("/login").headers["location"] == "/setup"
    assert einrichten(client, code, user="berta").headers["location"] == "/"
    assert client.get("/api/angebote").status_code == 200


def test_datenverzeichnis_unbrauchbar(tmp_path):
    """Volume fehlt oder ist nicht beschreibbar: klare Meldung statt Absturz."""
    datei = tmp_path / "keinverzeichnis"
    datei.write_text("x")
    app = app_mit(tmp_path, eingerichtet=False, data_dir=datei)
    with TestClient(app, follow_redirects=False) as c:
        antwort = einrichten(c, app.state.access.setup_code)
        assert antwort.status_code == 500
        assert "nicht beschreibbar" in antwort.text
        assert app.state.push.unavailable


# --- Anmeldung -----------------------------------------------------------------

def test_falsches_passwort(client, caplog):
    antwort = anmelden(client, pw="falsch-falsch")
    assert antwort.status_code == 303
    assert antwort.headers["location"] == "/login?fehler=1"
    assert "Anmeldung fehlgeschlagen für 'anna'" in caplog.text
    assert "Benutzername oder Passwort falsch" in client.get("/login?fehler=1").text
    assert "Benutzername oder Passwort falsch" not in client.get("/login").text
    assert client.get("/api/angebote").status_code == 401


def test_nach_fuenf_fehlversuchen_ist_die_ip_gesperrt(tmp_path):
    """Auch das richtige Passwort hilft dann nicht mehr. Andere IPs bleiben frei."""
    app = app_mit(tmp_path)
    with TestClient(app, follow_redirects=False, client=("203.0.113.9", 50000)) as angreifer:
        for _ in range(5):
            assert anmelden(angreifer, pw="falsch-falsch").headers["location"] == "/login?fehler=1"
        assert anmelden(angreifer).headers["location"] == "/login?gesperrt=1"
        assert "Zu viele Fehlversuche. Bitte in 15 Minuten" in angreifer.get("/login?gesperrt=1").text
    with TestClient(app, follow_redirects=False, client=("192.168.178.20", 50000)) as besitzer:
        assert anmelden(besitzer).headers["location"] == "/"


def test_erfolgreiche_anmeldung_setzt_die_zaehlung_zurueck(client):
    for _ in range(4):
        anmelden(client, pw="falsch-falsch")
    assert anmelden(client).headers["location"] == "/"
    client.post("/logout")
    for _ in range(4):
        anmelden(client, pw="falsch-falsch")
    assert anmelden(client).headers["location"] == "/"


def test_volle_warteschlange_lehnt_ab_statt_zu_warten(client):
    """Eine Flut von Anmeldungen soll den Besitzer nicht minutenlang warten lassen."""
    client.app_.state.access._waiting = main.MAX_WAITING_LOGINS
    assert anmelden(client).headers["location"] == "/login?belegt=1"
    assert "sehr viele Anmeldungen" in client.get("/login?belegt=1").text
    client.app_.state.access._waiting = 0
    assert anmelden(client).headers["location"] == "/"


def test_einrichtung_ist_ebenfalls_gebremst(neu):
    for _ in range(5):
        assert einrichten(neu, "AAAA-BBBB-CCCC").status_code == 400
    gesperrt = einrichten(neu, neu.code)
    assert gesperrt.status_code == 429 and "Zu viele Fehlversuche" in gesperrt.text
    assert not neu.app_.state.access.store.configured


def test_gelungene_einrichtung_vergisst_die_fehlversuche(neu):
    for _ in range(4):
        einrichten(neu, "AAAA-BBBB-CCCC")
    assert einrichten(neu, neu.code).headers["location"] == "/"
    assert neu.app_.state.access.throttle._attempts == {}


@pytest.mark.parametrize("herkunft,erwartet", [
    ("same-site", 403), ("cross-site", 403), ("same-origin", 303), ("none", 303), (None, 303),
])
def test_schreibende_anfragen_nur_von_der_eigenen_seite(client, herkunft, erwartet):
    """SameSite=Lax laesst Nachbar-Subdomains derselben Domain durch. Sec-Fetch-Site
    nicht (Befund der Review)."""
    anmelden(client)
    kopf = {"Sec-Fetch-Site": herkunft} if herkunft else {}
    assert client.post("/logout", headers=kopf).status_code == erwartet
    if erwartet == 403:
        assert client.post("/api/angebote/pruefen", headers=kopf).status_code == 403
        assert client.post("/login", data={"username": "anna", "password": PASSWORT},
                           headers=kopf).status_code == 403
        assert client.get("/api/angebote", headers=kopf).status_code == 200   # Lesen geht


def test_anmelden_und_abmelden(client):
    antwort = anmelden(client, user="Anna")
    assert antwort.status_code == 303 and antwort.headers["location"] == "/"
    cookie = antwort.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "Secure" not in cookie                       # http:// im Heimnetz
    assert f"Max-Age={7 * 24 * 3600}" in cookie

    assert client.get("/api/angebote").status_code == 200
    client.post("/logout")
    assert client.get("/api/angebote").status_code == 401


def test_secure_cookie_ueber_https(tmp_path):
    """Hinter dem Reverse Proxy (X-Forwarded-Proto: https) bekommt das Cookie Secure."""
    with TestClient(app_mit(tmp_path), base_url="https://testserver", follow_redirects=False) as c:
        assert "Secure" in anmelden(c).headers["set-cookie"]


def test_zu_grosser_body_wird_abgelehnt(client, neu):
    """Anmeldung und Einrichtung sind ohne Session aus dem Internet erreichbar."""
    for c, pfad in ((client, "/login"), (neu, "/setup")):
        antwort = c.post(pfad, content=b"username=" + b"x" * 5000,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert antwort.status_code == 413, pfad


def test_sicherheitsheader(client):
    antwort = client.get("/login")
    assert antwort.headers["X-Content-Type-Options"] == "nosniff"
    assert antwort.headers["X-Frame-Options"] == "DENY"
    csp = antwort.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp


def test_seiten_ohne_inline_skript_und_fremde_quellen():
    """Sonst blockiert die Content-Security-Policy etwas."""
    for datei in main.FRONTEND.glob("*.html"):
        text = datei.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), datei.name
        assert " style=" not in text and " onclick=" not in text, datei.name
        assert "googleapis" not in text and "http://" not in text, datei.name


def test_was_ohne_anmeldung_erreichbar_ist(client):
    for pfad in ("/static/style.css", "/sw.js", "/manifest.webmanifest",
                 "/static/icons/icon-192.png", "/static/icons/apple-touch-icon.png"):
        assert client.get(pfad).status_code == 200, pfad
    assert "javascript" in client.get("/sw.js").headers["content-type"]
    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["display"] == "standalone"          # Bedingung fuer Push am iPhone
    for pfad in ("/", "/push", "/static/app.js", "/static/index.html", "/docs", "/openapi.json"):
        assert client.get(pfad).status_code == 303, pfad
    assert client.get("/api/push/config").status_code == 401


@pytest.mark.parametrize("pfad", [
    "/static/icons/%2e%2e/app.js", "/static/icons/%2e%2e/index.html", "/static/icons/..%2fcommon.js",
    "/static/icons/./../push.js", "/static/icons//icon.svg", "/static/icons/..%5capp.js",
])
def test_kein_umweg_ueber_die_offenen_symbole(client, pfad):
    """/static/icons/../app.js passte frueher zum offenen Anfang, StaticFiles lieferte
    dann app.js ohne Anmeldung aus (Befund der Review)."""
    assert client.get(pfad).status_code == 303


def test_seiten_nach_anmeldung(client):
    anmelden(client)
    for pfad in ("/", "/push"):
        antwort = client.get(pfad)
        assert antwort.status_code == 200
        assert antwort.headers["cache-control"] == "no-cache", pfad
        for platzhalter in ("__VERSION__", "__FEHLER__", "__BENUTZER__"):
            assert platzhalter not in antwort.text, pfad
        assert f"/static/style.css?v={main.APP_VERSION}" in antwort.text, pfad
        assert f"Angebotsfinder {main.APP_VERSION}" in antwort.text, pfad
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/docs").status_code == 404                # keine API-Doku nach aussen


# --- Angebote ----------------------------------------------------------------

def test_angebote_vor_der_ersten_pruefung(client):
    anmelden(client)
    daten = client.get("/api/angebote").json()
    assert daten["version"] == main.APP_VERSION
    assert (daten["status"], daten["message"]) == ("unknown", "Erste Prüfung läuft …")
    assert daten["push_devices"] == 0
    assert daten["watchlist_file"] == "config/angebote.yaml"


def test_jetzt_pruefen(client):
    anmelden(client)
    client.app_.state.runner.monitor.clock = Uhr(SAMSTAG)
    antwort = client.post("/api/angebote/pruefen")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["status"] == "ok"
    assert daten["message"] == "Monster Energy: Lidl 0,75 € (Mo 12.10.–Sa 17.10.) mit App"
    assert daten["summary"]["hits"] == 1 and daten["matches"][0]["shop"] == "Lidl"
    assert daten["subtitle"] == "EDEKA, REWE, Lidl · PLZ 97421 · Mo, Mi, Fr, So um 08:00"

    zweimal = client.post("/api/angebote/pruefen")
    assert zweimal.status_code == 429
    assert "Bitte noch 60 s warten" in zweimal.json()["detail"]


def test_jetzt_pruefen_braucht_anmeldung(client):
    assert client.post("/api/angebote/pruefen").status_code == 401


def test_neue_angebote_kommen_per_push(tmp_path):
    app = app_mit(tmp_path)
    with TestClient(app, follow_redirects=False) as c:
        anmelden(c)
        assert c.post("/api/push/subscribe", json={"subscription": SUB}).status_code == 200
        app.state.runner.monitor.clock = Uhr(SAMSTAG)
        c.post("/api/angebote/pruefen")
        assert c.get("/api/angebote").json()["push_devices"] == 1
    # Beim Herunterfahren wartet die App auf laufende Sendungen.
    gesendet = [json.loads(a["data"]) for a in app.fake.aufrufe]
    assert gesendet == [{"title": "Angebot: Monster Energy",
                         "body": "Lidl 0,75 € (Mo 12.10.–Sa 17.10.) mit App",
                         "tag": "angebote", "url": "/", "kind": "info"}]
    assert app.fake.aufrufe[0]["headers"] == {"Urgency": "normal"}


# --- Pruefschleife -----------------------------------------------------------

class Pruefung:
    def __init__(self, verhalten):
        self.verhalten = verhalten

    async def check(self):
        return await self.verhalten()


def test_haengende_pruefung_wird_nach_zeitlimit_zum_fehler():
    async def haengt():
        await asyncio.sleep(10)

    runner = main.Runner(Pruefung(haengt), timeout=0.05)
    ergebnis = asyncio.run(runner.run_once())
    assert ergebnis.status is ServiceStatus.ERROR
    assert ergebnis.message == "Prüfung nach 0 s abgebrochen"
    assert runner.result is ergebnis


def test_jetzt_pruefen_waehrend_die_schleife_prueft():
    """Das Warten auf die laufende Pruefung zaehlt nicht zum Zeitlimit. Vorher brach
    der Knopf ab und ueberschrieb das gute Ergebnis mit einem Fehler (Befund der Review)."""
    laeufe = []
    sperre = asyncio.Lock()                         # wie im AngeboteMonitor

    async def dauert():
        async with sperre:
            laeufe.append("start")
            await asyncio.sleep(0.3)
            return CheckResult(status=ServiceStatus.OK, message=f"Lauf {len(laeufe)}")

    runner = main.Runner(Pruefung(dauert), timeout=0.5)

    async def ablauf():
        schleife = asyncio.create_task(runner.run_once())
        await asyncio.sleep(0.05)
        knopf = await runner.run_once()             # wartet 0,25 s, prueft dann 0,3 s
        return await schleife, knopf

    schleife, knopf = asyncio.run(ablauf())
    assert (schleife.status, knopf.status) == (ServiceStatus.OK, ServiceStatus.OK)
    assert runner.result is knopf and knopf.message == "Lauf 2"


def test_programmfehler_wird_angezeigt():
    async def kaputt():
        raise ValueError("kaputt")

    ergebnis = asyncio.run(main.Runner(Pruefung(kaputt)).run_once())
    assert (ergebnis.status, ergebnis.message) == (ServiceStatus.ERROR,
                                                   "Programmfehler: ValueError: kaputt")


def test_nur_zustandswechsel_im_protokoll(caplog):
    stand = {"status": ServiceStatus.OK}

    async def pruefen():
        return CheckResult(status=stand["status"], message=stand["status"].value)

    runner = main.Runner(Pruefung(pruefen))
    with caplog.at_level(logging.INFO, logger="angebotsfinder"):
        for _ in range(3):
            asyncio.run(runner.run_once())
        stand["status"] = ServiceStatus.WARNING
        asyncio.run(runner.run_once())
    assert [r.getMessage() for r in caplog.records] == ["Stand: ok - ok", "Stand: warning - warning"]


def test_neue_angebote_ohne_push_landen_im_protokoll(caplog):
    async def mit_angebot():
        return CheckResult(status=ServiceStatus.OK, message="x",
                           notifications=[{"title": "Angebot: Cola"}])

    with caplog.at_level(logging.WARNING, logger="angebotsfinder"):
        asyncio.run(main.Runner(Pruefung(mit_angebot)).run_once())
    assert "Push ist nicht verfügbar: Angebot: Cola" in caplog.text


# --- Push ----------------------------------------------------------------------

def test_push_konfiguration(client):
    anmelden(client)
    cfg = client.get("/api/push/config").json()
    assert cfg["available"] is True and cfg["reason"] is None
    assert len(cfg["public_key"]) == 87


def test_push_anmelden_testen_abmelden(client):
    anmelden(client)
    antwort = client.post("/api/push/subscribe", json={"subscription": SUB, "label": "Chrome auf Android"})
    assert antwort.status_code == 200
    assert antwort.json() == {"id": device_id(FCM), "label": "Chrome auf Android"}

    geraete = client.get("/api/push/devices").json()
    assert [g["label"] for g in geraete] == ["Chrome auf Android"]
    # Endpoint und Schluessel gehen nicht zurueck an den Browser.
    assert "endpoint" not in geraete[0] and "keys" not in geraete[0]

    test = client.post("/api/push/test", json={"endpoint": FCM}).json()
    assert test == {"sent": 1, "failed": 0, "removed": 0, "errors": []}
    aufruf = client.app_.fake.aufrufe[0]
    assert aufruf["subscription_info"]["endpoint"] == FCM
    assert aufruf["headers"] == {"Urgency": "high"}
    assert json.loads(aufruf["data"])["title"] == "Angebotsfinder: Test"

    assert client.post("/api/push/unsubscribe", json={"endpoint": FCM}).json() == {"removed": True}
    assert client.get("/api/push/devices").json() == []


def test_geraet_ueber_die_liste_entfernen(client):
    anmelden(client)
    client.post("/api/push/subscribe", json={"subscription": SUB})
    pfad = f"/api/push/devices/{device_id(FCM)}/remove"
    assert client.post(pfad).json() == {"removed": True}
    assert client.post(pfad).json() == {"removed": False}


def test_fremdes_ziel_wird_abgelehnt(client):
    anmelden(client)
    boese = {**SUB, "endpoint": "https://192.168.178.1/admin"}
    antwort = client.post("/api/push/subscribe", json={"subscription": boese})
    assert antwort.status_code == 400
    assert "Push-Dienst" in antwort.json()["detail"]


def test_test_nur_an_angemeldete_geraete(client):
    anmelden(client)
    assert client.post("/api/push/test", json={"endpoint": FCM}).status_code == 404
    assert client.post("/api/push/test", json={}).status_code == 404
    assert client.app_.fake.aufrufe == []


def test_push_ohne_anmeldung(client):
    assert client.post("/api/push/subscribe", json={"subscription": SUB}).status_code == 401
    assert client.get("/api/push/devices").status_code == 401


def test_falsche_kontaktadresse_schaltet_nur_push_ab(tmp_path, monkeypatch):
    monkeypatch.setenv("ANGEBOTSFINDER_PUSH_CONTACT", "anna@example.de")
    app = app_mit(tmp_path)
    with TestClient(app, follow_redirects=False) as c:
        anmelden(c)
        cfg = c.get("/api/push/config").json()
        assert cfg["available"] is False and "mailto:" in cfg["reason"]
        assert c.get("/api/push/devices").status_code == 503
        assert c.get("/api/angebote").status_code == 200
