"""Atomares Schreiben und Einstellungen aus der Umgebung."""

from __future__ import annotations

import os

import pytest

from backend import settings
from backend.files import write_atomic


def test_schreibt_atomar_mit_rechten_0600(tmp_path):
    ziel = tmp_path / "unter" / "datei.json"
    write_atomic(ziel, b"eins")
    write_atomic(ziel, b"zwei")
    assert ziel.read_bytes() == b"zwei"
    assert [p.name for p in ziel.parent.iterdir()] == ["datei.json"]   # keine Reste
    if os.name != "nt":
        assert oct(os.stat(ziel).st_mode & 0o777) == "0o600"


def test_fehler_laesst_keine_reste_und_die_alte_datei_ganz(tmp_path, monkeypatch):
    ziel = tmp_path / "datei.json"
    write_atomic(ziel, b"alt")

    def kaputt(*args):
        raise OSError("Platte voll")

    monkeypatch.setattr(os, "replace", kaputt)
    with pytest.raises(OSError, match="Platte voll"):
        write_atomic(ziel, b"neu")
    assert ziel.read_bytes() == b"alt"
    assert [p.name for p in tmp_path.iterdir()] == ["datei.json"]


@pytest.mark.parametrize("wert,erwartet", [
    (None, "INFO"), ("", "INFO"), ("debug", "DEBUG"), (" ERROR ", "ERROR"), ("laut", "INFO"),
])
def test_protokollstufe(monkeypatch, wert, erwartet):
    """Ein Tippfehler darf den Start nicht verhindern."""
    if wert is not None:
        monkeypatch.setenv("ANGEBOTSFINDER_LOG_LEVEL", wert)
    assert settings.log_level() == erwartet
