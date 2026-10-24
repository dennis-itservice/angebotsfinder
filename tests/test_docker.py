"""Dockerfile und docker-compose.yml: Waechter fuer Daten, Rechte und Neubau.

Docker selbst laeuft in den Tests nicht. Diese Tests halten fest, was beim
Einrichten und bei Updates schiefgehen wuerde, wenn jemand die Dateien aendert.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
DIENST = COMPOSE["services"]["angebotsfinder"]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def zeilen(name: str) -> list[str]:
    return (ROOT / name).read_text(encoding="utf-8").splitlines()


def test_daten_liegen_in_einem_benannten_volume_mit_festem_namen():
    """Zugang und Push-Schluessel muessen Neustart, Neubau und Updates ueberleben.
    Mit festem Namen auch dann, wenn der Projektordner anders heisst."""
    assert "angebotsfinder-daten:/app/data" in DIENST["volumes"]
    assert COMPOSE["volumes"]["angebotsfinder-daten"]["name"] == "angebotsfinder-daten"


def test_merkliste_ist_als_verzeichnis_und_nur_lesbar_eingebunden():
    """Als Verzeichnis, nicht als einzelne Datei: Editoren, die beim Speichern eine
    neue Datei anlegen, waeren sonst fuer den Container unsichtbar."""
    assert "./config:/app/config:ro" in DIENST["volumes"]


def test_erstellen_baut_immer_neu():
    """Sonst nimmt der Container Manager der Synology das alte Image wieder her."""
    assert DIENST["pull_policy"] == "build"


def test_port_zeitzone_und_neustart():
    assert DIENST["ports"] == ["8098:8098"]
    assert "TZ=Europe/Berlin" in DIENST["environment"]
    assert DIENST["restart"] == "unless-stopped"
    assert "--port\", \"8098\"" in DOCKERFILE


def test_kein_passwort_in_der_compose_datei():
    """Der Zugang entsteht bei der Einrichtung im Browser, nie per Umgebungsvariable."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert not re.search(r"(?i)pass(word|wort)?\w*\s*[=:]", text)


def test_container_laeuft_nicht_als_root_und_darf_seinen_code_nicht_aendern():
    assert re.search(r"^USER angebote$", DOCKERFILE, re.M)
    assert "chown -R" not in DOCKERFILE
    assert "chown angebote:angebote /app/data" in DOCKERFILE


def test_proxy_angaben_nur_aus_dem_privaten_netz():
    """Sonst bekommt das Cookie hinter dem HTTPS-Proxy kein Secure. Aber nicht "*":
    Dann nimmt uvicorn die IP, die ein Angreifer selbst in X-Forwarded-For schreibt."""
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    werte = re.search(r'"--forwarded-allow-ips", "([^"]+)"', DOCKERFILE).group(1)
    assert werte != "*"
    vertraut = _TrustedHosts(werte)
    for proxy in ("127.0.0.1", "172.17.0.1", "172.20.0.1", "192.168.178.20", "10.0.0.5"):
        assert proxy in vertraut, proxy                 # Docker-Bridge, Heimnetz
    assert "203.0.113.9" not in vertraut
    # Hinter dem Proxy: die echte IP, die der Proxy anhaengt, nicht die gefaelschte
    assert vertraut.get_trusted_client_address("6.6.6.6, 203.0.113.9")[0] == "203.0.113.9"


def test_merkliste_und_daten_bleiben_aus_repo_und_image():
    assert "config/angebote.yaml" in zeilen(".gitignore")
    assert "data/" in zeilen(".gitignore")
    assert "config/" in zeilen(".dockerignore")
    assert "data/" in zeilen(".dockerignore")


def test_versionen_sind_gepinnt():
    pakete = [z for z in zeilen("requirements.txt") if z.strip() and not z.startswith("#")]
    assert pakete and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9_.]*", p) for p in pakete)
