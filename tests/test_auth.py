"""Zugang: Passwort-Hash, Ablage, Einrichtungscode, Sessions, Zuruecksetzen."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime

import pytest

from backend import auth

PASSWORT = "Sommer-2026!"
JETZT = datetime(2026, 10, 24, 20, 0)


# --- Passwort-Hash -----------------------------------------------------------

def test_argon2id_mit_den_owasp_werten():
    encoded = auth.hash_password(PASSWORT)
    assert encoded.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert PASSWORT not in encoded
    assert auth.verify_password(PASSWORT, encoded)
    assert not auth.verify_password("sommer-2026!", encoded)
    assert not auth.verify_password("", encoded)


def test_jeder_hash_hat_sein_eigenes_salz():
    assert auth.hash_password(PASSWORT) != auth.hash_password(PASSWORT)


@pytest.mark.parametrize("kaputt", ["", "klartext", "$argon2id$v=19$m=19456,t=2,p=1$abc",
                                    "$2b$12$abcdefghijklmnopqrstuv"])
def test_unbrauchbarer_hash_heisst_falsch_statt_absturz(kaputt):
    assert auth.verify_password(PASSWORT, kaputt) is False


def test_umlaute_auf_jedem_geraet_gleich():
    """Ein 'ö' kommt je nach Tastatur zusammengesetzt oder als o + Punkte."""
    zusammen = unicodedata.normalize("NFC", "Größe-und-Gewicht")
    zerlegt = unicodedata.normalize("NFD", zusammen)
    assert zusammen != zerlegt
    assert auth.verify_password(zerlegt, auth.hash_password(zusammen))


def test_selbsttest():
    auth.selftest()


# --- Eingaben bei der Einrichtung --------------------------------------------

def test_gueltige_eingaben():
    assert auth.validate_new_credentials("  Anna   Muster ", PASSWORT, PASSWORT) == "Anna Muster"


@pytest.mark.parametrize("name,passwort,wiederholung,meldung", [
    ("", PASSWORT, PASSWORT, "Benutzernamen eingeben"),
    ("   ", PASSWORT, PASSWORT, "Benutzernamen eingeben"),
    ("a" * 65, PASSWORT, PASSWORT, "höchstens 64 Zeichen"),
    ("anna\x00", PASSWORT, PASSWORT, "unzulässige Zeichen"),
    ("anna​", PASSWORT, PASSWORT, "unzulässige Zeichen"),
    ("anna", PASSWORT, PASSWORT + "x", "stimmen nicht überein"),
    ("anna", "kurz12345", "kurz12345", "mindestens 10 Zeichen"),
    ("anna", "x" * 257, "x" * 257, "höchstens 256 Zeichen"),
    ("Anna-Lena1", "anna-lena1", "anna-lena1", "nicht der Benutzername"),
])
def test_ungueltige_eingaben(name, passwort, wiederholung, meldung):
    with pytest.raises(ValueError, match=meldung):
        auth.validate_new_credentials(name, passwort, wiederholung)


def test_zehn_zeichen_reichen():
    assert auth.validate_new_credentials("anna", "a" * 10, "a" * 10) == "anna"


# --- Einrichtungscode ----------------------------------------------------------

def test_einrichtungscode_ist_gut_abzutippen():
    code = auth.new_setup_code()
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    assert not set(code) & set("01OI")
    assert len({auth.new_setup_code() for _ in range(50)}) == 50


def test_einrichtungscode_vergleich():
    code = "ABCD-EFGH-JK23"
    assert auth.setup_code_matches(code, code)
    assert auth.setup_code_matches(" abcd efgh jk23 ", code)
    assert auth.setup_code_matches("ABCDEFGHJK23", code)
    assert not auth.setup_code_matches("ABCD-EFGH-JK24", code)
    assert not auth.setup_code_matches("", code)
    assert not auth.setup_code_matches(code, None)   # eingerichtet: kein Code mehr gueltig


# --- Ablage ------------------------------------------------------------------

def test_ablage_enthaelt_nur_den_hash(tmp_path):
    store = auth.CredentialStore(tmp_path)
    store.load()
    assert not store.configured
    store.save("Anna", PASSWORT, JETZT)
    assert store.configured

    roh = (tmp_path / "zugang.json").read_text(encoding="utf-8")
    assert PASSWORT not in roh
    daten = json.loads(roh)
    assert daten["username"] == "Anna" and daten["created_at"] == "2026-10-24T20:00:00"
    assert daten["password_hash"].startswith("$argon2id$")
    assert [p.name for p in tmp_path.iterdir()] == ["zugang.json"]   # keine Reste
    if os.name != "nt":
        assert oct(os.stat(tmp_path / "zugang.json").st_mode & 0o777) == "0o600"


def test_zugang_ueberlebt_den_neustart(tmp_path):
    auth.CredentialStore(tmp_path).save("Anna", PASSWORT, JETZT)
    neu = auth.CredentialStore(tmp_path)
    neu.load()
    assert neu.configured
    assert neu.verify("Anna", PASSWORT)
    assert neu.verify("anna", PASSWORT)                # Handy schreibt gross: egal
    assert neu.verify(" Anna ", PASSWORT)
    assert not neu.verify("Anna", "Sommer-2026")
    assert not neu.verify("Berta", PASSWORT)


def test_ohne_zugang_kommt_niemand_rein(tmp_path):
    store = auth.CredentialStore(tmp_path)
    store.load()
    assert not store.verify("", "")
    assert not store.verify("Anna", PASSWORT)


@pytest.mark.parametrize("inhalt", ["{kaputt", "[]", '{"username": "Anna"}',
                                    '{"username": "Anna", "password_hash": "geheim"}',
                                    '{"username": "", "password_hash": "$argon2id$x"}'])
def test_beschaedigte_ablage_heisst_neu_einrichten(tmp_path, inhalt):
    (tmp_path / "zugang.json").write_text(inhalt, encoding="utf-8")
    store = auth.CredentialStore(tmp_path)
    store.load()
    assert not store.configured
    assert "beschädigt" in store.problem


def test_zuruecksetzen(tmp_path):
    store = auth.CredentialStore(tmp_path)
    store.save("Anna", PASSWORT, JETZT)
    assert store.reset() is True
    assert not store.configured and not (tmp_path / "zugang.json").exists()
    assert store.reset() is False


def test_zuruecksetzen_per_kommandozeile(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANGEBOTSFINDER_DATA_DIR", str(tmp_path))
    auth.CredentialStore(tmp_path).save("Anna", PASSWORT, JETZT)
    assert auth.main(["auth", "zuruecksetzen"]) == 0
    assert "Zugang gelöscht" in capsys.readouterr().out
    assert not (tmp_path / "zugang.json").exists()
    assert auth.main(["auth", "zuruecksetzen"]) == 0
    assert "kein Zugang eingerichtet" in capsys.readouterr().out
    assert auth.main(["auth"]) == 2
    assert "Aufruf" in capsys.readouterr().out


# --- Sessions ----------------------------------------------------------------

class Uhr:
    def __init__(self):
        self.jetzt = 1000.0

    def __call__(self):
        return self.jetzt


def test_session_lebenszyklus():
    sessions = auth.Sessions()
    token = sessions.create()
    assert len(token) >= 40
    assert sessions.valid(token)
    sessions.delete(token)
    assert not sessions.valid(token)
    assert not sessions.valid(None) and not sessions.valid("")


def test_session_laeuft_nach_einer_woche_ab():
    uhr = Uhr()
    sessions = auth.Sessions(clock=uhr)
    token = sessions.create()
    uhr.jetzt += auth.SESSION_MAX_AGE
    assert sessions.valid(token)
    uhr.jetzt += 1
    assert not sessions.valid(token)


def test_abgelaufene_sessions_werden_beim_anmelden_entfernt():
    uhr = Uhr()
    sessions = auth.Sessions(clock=uhr)
    alt = sessions.create()
    uhr.jetzt += auth.SESSION_MAX_AGE + 1
    sessions.create()
    assert alt not in sessions._tokens and len(sessions._tokens) == 1


def test_alle_sessions_beenden():
    sessions = auth.Sessions()
    tokens = [sessions.create() for _ in range(3)]
    sessions.clear()
    assert not any(sessions.valid(t) for t in tokens)
