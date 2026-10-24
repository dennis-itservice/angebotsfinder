"""Gemeinsame Testhilfen. Kein Netzzugriff."""

from __future__ import annotations

import pytest

ENV_KEYS = ("ANGEBOTSFINDER_DATA_DIR", "ANGEBOTSFINDER_WATCHLIST",
            "ANGEBOTSFINDER_PUSH_CONTACT", "ANGEBOTSFINDER_LOG_LEVEL")


@pytest.fixture(autouse=True)
def saubere_umgebung(monkeypatch):
    """Echte Werte aus der Shell sollen kein Testergebnis beeinflussen."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
