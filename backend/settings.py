"""Pfade und Einstellungen aus der Umgebung.

Im Container ist alles fest: Code unter /app, die Merkliste unter /app/config
(eingebunden aus dem Projektordner), Zugang, Push-Schluessel und Zustand unter
/app/data (benanntes Docker-Volume). Die Umgebungsvariablen braucht man nur
beim lokalen Testen oder fuer die Push-Kontaktadresse.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

DEFAULT_CONTACT = "mailto:angebotsfinder@example.com"
WATCHLIST_LABEL = "config/angebote.yaml"


def data_dir() -> Path:
    """Zugang, Push-Schluessel, Geraeteliste und Zustand der Pruefung."""
    return Path(os.environ.get("ANGEBOTSFINDER_DATA_DIR") or ROOT / "data")


def watchlist_path() -> Path:
    return Path(os.environ.get("ANGEBOTSFINDER_WATCHLIST") or ROOT / WATCHLIST_LABEL)


def watchlist_label() -> str:
    """So, wie der Nutzer die Datei auf seinem Geraet sieht."""
    return os.environ.get("ANGEBOTSFINDER_WATCHLIST") or WATCHLIST_LABEL


def push_contact() -> str:
    """Absenderangabe fuer die Push-Dienste (VAPID 'sub'). mailto: oder https://."""
    contact = (os.environ.get("ANGEBOTSFINDER_PUSH_CONTACT") or "").strip() or DEFAULT_CONTACT
    if not contact.startswith(("mailto:", "https://")):
        raise ValueError("ANGEBOTSFINDER_PUSH_CONTACT muss mit mailto: oder https:// beginnen, "
                         f"war {contact!r}")
    return contact


def log_level() -> str:
    return (os.environ.get("ANGEBOTSFINDER_LOG_LEVEL") or "INFO").upper()
