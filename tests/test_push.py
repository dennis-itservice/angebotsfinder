"""Push: Speicher, Versand - und ein echter Versand an einen lokalen Empfaenger."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend import settings
from backend.push import (MAX_DEVICES, TTL_HOURS, PushSender, PushStore, device_id,
                          validate_subscription)

FCM = "https://fcm.googleapis.com/fcm/send/abc123"
T0 = datetime(2026, 10, 3, 12, 0, 0)
KONTAKT = "mailto:test@example.org"


def sub(endpoint=FCM):
    return {"endpoint": endpoint, "keys": {"p256dh": "BPq" + "x" * 84, "auth": "a" * 22}}


def run(coro):
    return asyncio.run(coro)


# --- Kontaktadresse ----------------------------------------------------------

def test_kontakt_standard_und_eigener(monkeypatch):
    assert settings.push_contact() == "mailto:angebotsfinder@example.com"
    monkeypatch.setenv("ANGEBOTSFINDER_PUSH_CONTACT", " mailto:anna@example.de ")
    assert settings.push_contact() == "mailto:anna@example.de"
    monkeypatch.setenv("ANGEBOTSFINDER_PUSH_CONTACT", "anna@example.de")
    with pytest.raises(ValueError, match="mailto: oder https://"):
        settings.push_contact()


# --- Pruefung der Anmeldung --------------------------------------------------

@pytest.mark.parametrize("endpoint", [
    FCM,
    "https://updates.push.services.mozilla.com/wpush/v2/x",
    "https://db5p.notify.windows.com/w/?token=x",
    "https://web.push.apple.com/x",
])
def test_bekannte_push_dienste(endpoint):
    assert validate_subscription(sub(endpoint))["endpoint"] == endpoint


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/x",            # kein https
    "https://192.168.178.1/x",                 # Heimnetz - waere ein POST aus dem Container heraus
    "https://evil.example.com/x",
    "https://fcm.googleapis.com.evil.de/x",    # nur Praefix passt
])
def test_fremde_ziele_werden_abgelehnt(endpoint):
    with pytest.raises(ValueError, match="Push-Dienst"):
        validate_subscription(sub(endpoint))


@pytest.mark.parametrize("keys", [{"p256dh": "x"}, [], "abc", {"p256dh": "x" * 201, "auth": "a"}])
def test_schluessel_muessen_dabei_sein(keys):
    with pytest.raises(ValueError, match="keys|Schlüssel"):
        validate_subscription({"endpoint": FCM, "keys": keys})


# --- Speicher ----------------------------------------------------------------

def test_schluessel_wird_einmal_erzeugt_und_bleibt(tmp_path):
    a = PushStore(tmp_path)
    a.open()
    assert len(a.public_key) == 87  # 65 Bytes base64url ohne Padding
    b = PushStore(tmp_path)
    b.open()
    assert b.public_key == a.public_key
    if os.name != "nt":
        assert oct(os.stat(tmp_path / "vapid_private.pem").st_mode & 0o777) == "0o600"


def test_geraete_hinzufuegen_ersetzen_entfernen(tmp_path):
    store = PushStore(tmp_path)
    store.open()
    d = store.add(sub(), "Chrome auf Android", T0)
    assert d["id"] == device_id(FCM)
    store.add(sub(), "Neuer Name", T0)  # gleicher Endpoint: ersetzen, nicht doppeln
    assert [x["label"] for x in store.devices()] == ["Neuer Name"]

    zweiter = PushStore(tmp_path)
    zweiter.open()
    assert len(zweiter.devices()) == 1  # ueberlebt einen Neustart

    assert store.remove(id=d["id"]) is True
    assert store.remove(endpoint=FCM) is False
    assert store.devices() == []


def test_hoechstzahl_an_geraeten(tmp_path):
    store = PushStore(tmp_path)
    store.open()
    for i in range(MAX_DEVICES):
        store.add(sub(f"{FCM}{i}"), "x", T0)
    with pytest.raises(ValueError, match="Höchstens"):
        store.add(sub(f"{FCM}neu"), "x", T0)


def test_kaputte_geraeteliste_fuehrt_nicht_zum_absturz(tmp_path):
    (tmp_path / "push_devices.json").write_text("{kaputt", encoding="utf-8")
    store = PushStore(tmp_path)
    store.open()
    assert store.devices() == []


def test_nicht_beschreibbares_verzeichnis_wirft(tmp_path):
    datei = tmp_path / "keinverzeichnis"
    datei.write_text("x")
    with pytest.raises(OSError):
        PushStore(datei).open()


# --- Versand mit Ersatz-Sender -----------------------------------------------

class FakeWebPush:
    """Ersetzt pywebpush.webpush_async. antworten: je Aufruf None oder eine Exception."""

    def __init__(self, *antworten):
        self.antworten = list(antworten)
        self.aufrufe = []

    async def __call__(self, **kwargs):
        self.aufrufe.append(kwargs)
        antwort = self.antworten.pop(0) if self.antworten else None
        if isinstance(antwort, BaseException):
            raise antwort


def webpush_fehler(status):
    from pywebpush import WebPushException

    class Antwort:
        status_code = status
        text = ""
        headers = {}

    return WebPushException("Push failed", response=Antwort())


def geraete(tmp_path, anzahl):
    store = PushStore(tmp_path)
    store.open()
    for i in range(anzahl):
        store.add(sub(f"{FCM}{i}"), f"Geraet {i}", T0)
    return store


def test_versand_erfolg_fehler_und_abgemeldet(tmp_path):
    store = geraete(tmp_path, 4)
    fake = FakeWebPush(None, webpush_fehler(410), webpush_fehler(500), ConnectionError("weg"))
    summary = run(PushSender(store, KONTAKT, fake).send({"title": "t"}, urgency="high"))

    assert (summary.sent, summary.removed, summary.failed) == (1, 1, 2)
    uebrig = {d["label"]: d for d in store.devices()}
    assert set(uebrig) == {"Geraet 0", "Geraet 2", "Geraet 3"}       # 410 -> entfernt
    assert uebrig["Geraet 0"]["last_success_at"] is not None
    assert "HTTP 500" in uebrig["Geraet 2"]["last_error"]
    assert "ConnectionError" in uebrig["Geraet 3"]["last_error"]

    aufruf = fake.aufrufe[0]
    assert aufruf["headers"] == {"Urgency": "high"}
    assert aufruf["ttl"] == TTL_HOURS * 3600 == 24 * 3600
    assert aufruf["vapid_claims"] == {"sub": KONTAKT}
    assert json.loads(aufruf["data"]) == {"title": "t"}
    # Jeder Aufruf bekommt einen eigenen Claims-Block (pywebpush ergaenzt ihn)
    assert fake.aufrufe[1]["vapid_claims"] is not aufruf["vapid_claims"]


def test_test_push_nur_an_ein_geraet(tmp_path):
    store = geraete(tmp_path, 3)
    fake = FakeWebPush()
    summary = run(PushSender(store, KONTAKT, fake).send({}, only_endpoint=f"{FCM}1"))
    assert summary.sent == 1
    assert [a["subscription_info"]["endpoint"] for a in fake.aufrufe] == [f"{FCM}1"]


def test_versand_im_hintergrund_haelt_nichts_auf(tmp_path, caplog):
    """Neue Angebote gehen im Hintergrund raus. Ein Fehler beim Versand landet im
    Protokoll, nicht in der Pruefung."""
    store = geraete(tmp_path, 2)
    fake = FakeWebPush(None, ConnectionError("weg"))

    async def ablauf():
        sender = PushSender(store, KONTAKT, fake)
        sender.send_in_background({"title": "Angebot: Cola"})
        assert fake.aufrufe == []                  # noch nicht losgelaufen
        await sender.drain()

    with caplog.at_level("INFO", logger="angebotsfinder.push"):
        run(ablauf())
    assert len(fake.aufrufe) == 2
    assert fake.aufrufe[0]["headers"] == {"Urgency": "normal"}
    assert "Push 'Angebot: Cola': 1 zugestellt, 1 fehlgeschlagen, 0 entfernt" in caplog.text


# --- Echter Versand an einen lokalen Empfaenger -----------------------------

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_echter_versand_ist_verschluesselt_und_signiert(tmp_path):
    """pywebpush schickt wirklich - an einen lokalen Empfaenger statt an Google.

    Der Empfaenger entschluesselt mit dem Geraeteschluessel und prueft die
    VAPID-Signatur gegen den oeffentlichen Schluessel. So ist belegt, dass
    Schluesselablage, Verschluesselung und Signatur zusammenpassen.
    """
    import http_ece

    empfangen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            laenge = int(self.headers["Content-Length"])
            empfangen["body"] = self.rfile.read(laenge)
            empfangen["headers"] = dict(self.headers)
            self.send_response(201)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}/push/geraet"

    geraet_key = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    p256dh = geraet_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    store = PushStore(tmp_path)
    store.open()
    # Am Pruefcode vorbei: der lokale Empfaenger ist kein bekannter Push-Dienst.
    store._devices = [{"endpoint": endpoint, "keys": {"p256dh": b64url(p256dh), "auth": b64url(auth)},
                       "id": device_id(endpoint), "label": "Test"}]
    try:
        summary = run(PushSender(store, KONTAKT).send({"title": "Angebotsfinder: Test",
                                                       "body": "Größe ✓"}, urgency="high"))
    finally:
        server.server_close()

    assert summary.sent == 1, summary.errors
    headers = {k.lower(): v for k, v in empfangen["headers"].items()}
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["urgency"] == "high"
    assert headers["ttl"] == str(24 * 3600)

    klartext = http_ece.decrypt(empfangen["body"], private_key=geraet_key,
                                auth_secret=auth, version="aes128gcm")
    assert json.loads(klartext) == {"title": "Angebotsfinder: Test", "body": "Größe ✓"}

    # Authorization: vapid t=<JWT>, k=<oeffentlicher Schluessel>
    schema, rest = headers["authorization"].split(" ", 1)
    teile = dict(p.strip().split("=", 1) for p in rest.split(","))
    assert schema == "vapid" and teile["k"] == store.public_key
    kopf, nutzlast, signatur = teile["t"].split(".")
    claims = json.loads(b64url_decode(nutzlast))
    assert claims["sub"] == KONTAKT
    assert claims["aud"] == f"http://127.0.0.1:{server.server_port}"

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    sig = b64url_decode(signatur)
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    oeffentlich = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),
                                                               b64url_decode(store.public_key))
    oeffentlich.verify(der, f"{kopf}.{nutzlast}".encode(), ec.ECDSA(hashes.SHA256()))
