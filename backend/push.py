"""Web-Push: Geraete anmelden, neue Angebote aufs Handy schicken.

Der Browser meldet sich ueber die Push-API mit einem VAPID-Schluessel an, der
Angebotsfinder schickt verschluesselte Nachrichten an den Push-Dienst des
Browsers (bei Chrome: Google, bei Safari: Apple), der sie zustellt. Kein Konto,
keine zusaetzliche App. Die Verbindung geht nur nach draussen: Der Angebotsfinder
muss fuer die Zustellung nicht aus dem Internet erreichbar sein. Zum Anmelden
eines Geraets verlangt der Browser aber eine HTTPS-Adresse.

Schluessel und Geraeteliste liegen im Datenverzeichnis (Docker-Volume). Geht es
verloren, entsteht beim Start ein neuer Schluessel und die Geraete muessen sich
neu anmelden.

Uebernommen aus Sentinel 2.3.1, ohne die Alarm-Logik der Dienstueberwachung.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

logger = logging.getLogger("angebotsfinder.push")

# Push-Dienste der Browser. Geschickt wird nur dorthin - eine registrierte
# Adresse im Heimnetz wuerde sonst zu einem POST aus dem Container heraus.
ALLOWED_PUSH_HOSTS = (
    ".googleapis.com",        # Chrome, Edge auf Android, Samsung Internet
    ".mozilla.com",           # Firefox
    ".notify.windows.com",    # Edge auf Windows
    ".push.apple.com",        # Safari, iPhone als Home-Bildschirm-App
)
MAX_DEVICES = 20
TTL_HOURS = 24
"""So lange haelt der Push-Dienst eine Meldung fuer ein Geraet vor, das offline
ist. Angebote gelten tagelang, ein Tag reicht fuer Flugmodus und leeren Akku."""


# --- Speicher ----------------------------------------------------------------

def device_id(endpoint: str) -> str:
    """Kurze, stabile Kennung eines Geraets. Der Browser rechnet dieselbe aus."""
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]


def validate_subscription(sub: Any) -> dict:
    """Prueft, was der Browser schickt, bevor es gespeichert wird."""
    if not isinstance(sub, dict):
        raise ValueError("subscription fehlt")
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not isinstance(endpoint, str) or len(endpoint) > 1024:
        raise ValueError("endpoint fehlt oder ist zu lang")
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host.endswith(s) for s in ALLOWED_PUSH_HOSTS):
        raise ValueError(f"unbekannter Push-Dienst: {host or endpoint[:40]}")
    if not isinstance(keys, dict):
        raise ValueError("keys.p256dh und keys.auth fehlen")
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str) or not p256dh or not auth:
        raise ValueError("keys.p256dh und keys.auth fehlen")
    if len(p256dh) > 200 or len(auth) > 100:
        raise ValueError("Schlüssel zu lang")
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


class PushStore:
    """VAPID-Schluessel und registrierte Geraete als Dateien im Datenverzeichnis."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.devices_file = self.data_dir / "push_devices.json"
        self.key_file = self.data_dir / "vapid_private.pem"
        self.vapid = None
        self.public_key = ""
        self._devices: list[dict] = []

    def open(self) -> None:
        """Laedt oder erzeugt den Schluessel und laedt die Geraeteliste.

        Wirft OSError, wenn das Verzeichnis nicht beschreibbar ist - dann ist
        Push aus, der Rest laeuft weiter.
        """
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid02

        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.key_file.exists():
            self.vapid = Vapid02.from_pem(self.key_file.read_bytes())
        else:
            self.vapid = Vapid02()
            self.vapid.generate_keys()
            self._write(self.key_file, self.vapid.private_pem())
            logger.info("Neuer Push-Schlüssel erzeugt: %s", self.key_file)

        raw = self.vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        if self.devices_file.exists():
            try:
                daten = json.loads(self.devices_file.read_text(encoding="utf-8"))
                self._devices = [d for d in daten if isinstance(d, dict) and d.get("endpoint")]
            except ValueError:
                logger.error("%s ist beschädigt, die Geräteliste beginnt leer", self.devices_file)
                self._devices = []

    def _write(self, path: Path, content: bytes) -> None:
        """Atomar schreiben: erst in eine Nachbardatei, dann umbenennen."""
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _save(self) -> None:
        self._write(self.devices_file,
                    json.dumps(self._devices, indent=2, ensure_ascii=False).encode("utf-8"))

    def devices(self) -> list[dict]:
        return [dict(d) for d in self._devices]

    def add(self, subscription: dict, label: str, now: datetime) -> dict:
        sub = validate_subscription(subscription)
        label = (label or "").strip()[:100] or "Unbekanntes Gerät"
        for device in self._devices:
            if device["endpoint"] == sub["endpoint"]:
                device.update(keys=sub["keys"], label=label)
                self._save()
                return dict(device)
        if len(self._devices) >= MAX_DEVICES:
            raise ValueError(f"Höchstens {MAX_DEVICES} Geräte")
        device = {**sub, "id": device_id(sub["endpoint"]), "label": label,
                  "created_at": now.isoformat(timespec="seconds"),
                  "last_success_at": None, "last_error": None}
        self._devices.append(device)
        self._save()
        return dict(device)

    def remove(self, *, endpoint: str | None = None, id: str | None = None) -> bool:
        vorher = len(self._devices)
        self._devices = [d for d in self._devices
                         if d["endpoint"] != endpoint and d.get("id") != id]
        if len(self._devices) != vorher:
            self._save()
            return True
        return False

    def mark(self, endpoint: str, now: datetime, error: str | None) -> None:
        for device in self._devices:
            if device["endpoint"] == endpoint:
                if error is None:
                    device["last_success_at"] = now.isoformat(timespec="seconds")
                    device["last_error"] = None
                else:
                    device["last_error"] = f"{now.isoformat(timespec='seconds')}: {error[:200]}"
                self._save()
                return


# --- Versand -----------------------------------------------------------------

@dataclass
class SendSummary:
    sent: int = 0
    failed: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)


Sender = Callable[..., Awaitable[Any]]


class PushSender:
    def __init__(self, store: PushStore, contact: str, webpush: Sender | None = None,
                 ttl_hours: float = TTL_HOURS):
        self.store = store
        self.contact = contact
        self.ttl_hours = ttl_hours
        if webpush is None:
            from pywebpush import webpush_async as webpush
        self._webpush = webpush
        self._tasks: set[asyncio.Task] = set()

    async def send(self, payload: dict, *, urgency: str = "normal",
                   only_endpoint: str | None = None) -> SendSummary:
        from pywebpush import WebPushException

        summary = SendSummary()
        data = json.dumps(payload, ensure_ascii=False)
        for device in self.store.devices():
            if only_endpoint and device["endpoint"] != only_endpoint:
                continue
            now = datetime.now()
            try:
                await self._webpush(
                    subscription_info={"endpoint": device["endpoint"], "keys": device["keys"]},
                    data=data,
                    vapid_private_key=self.store.vapid,
                    # Bei jedem Aufruf neu: pywebpush ergaenzt den Block um aud und exp.
                    vapid_claims={"sub": self.contact},
                    ttl=int(self.ttl_hours * 3600),
                    timeout=10,
                    headers={"Urgency": urgency},
                )
            except WebPushException as exc:
                if exc.status_code in (404, 410):
                    # Geraet hat sich abgemeldet oder die Anmeldung ist abgelaufen.
                    self.store.remove(endpoint=device["endpoint"])
                    summary.removed += 1
                    logger.info("Push-Gerät %s (%s) ist abgemeldet und wurde entfernt",
                                device.get("id"), device.get("label"))
                    continue
                summary.failed += 1
                fehler = f"HTTP {exc.status_code}" if exc.status_code else exc.message
                summary.errors.append(f"{device.get('label')}: {fehler}")
                self.store.mark(device["endpoint"], now, fehler)
                logger.warning("Push an %s fehlgeschlagen: %s", device.get("id"), fehler)
            except Exception as exc:  # Netz weg, Zeitlimit, ...
                summary.failed += 1
                fehler = f"{type(exc).__name__}: {exc}"
                summary.errors.append(f"{device.get('label')}: {fehler}")
                self.store.mark(device["endpoint"], now, fehler)
                logger.warning("Push an %s fehlgeschlagen: %s", device.get("id"), fehler)
            else:
                summary.sent += 1
                self.store.mark(device["endpoint"], now, None)
        return summary

    def send_in_background(self, payload: dict, urgency: str = "normal") -> None:
        """Ein langsamer Push-Dienst darf keine Pruefung aufhalten."""
        task = asyncio.create_task(self._send_logged(payload, urgency))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_logged(self, payload: dict, urgency: str) -> None:
        try:
            summary = await self.send(payload, urgency=urgency)
            logger.info("Push '%s': %d zugestellt, %d fehlgeschlagen, %d entfernt",
                        payload.get("title"), summary.sent, summary.failed, summary.removed)
        except Exception:
            logger.exception("Push-Versand fehlgeschlagen")

    async def drain(self) -> None:
        """Wartet auf laufende Sendungen - fuer Tests und beim Herunterfahren."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
