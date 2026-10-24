"""Anmeldung: ein Zugang (Benutzer und Passwort), beim ersten Aufruf festgelegt.

Wie das Passwort abgelegt wird:
- Gespeichert wird nur ein Argon2id-Hash im PHC-Format, nie das Passwort selbst.
  Salz und Parameter stecken im Hash. Argon2id ist die erste Empfehlung von
  OWASP und RFC 9106, hier mit den OWASP-Werten m=19 MiB, t=2, p=1. Die
  Bibliothek cryptography ist fuer Web-Push ohnehin im Image, es kommt kein
  Paket dazu.
- Die Datei zugang.json liegt im Datenverzeichnis, mit Rechten 0600 und atomar
  geschrieben. Im Container ist das Datenverzeichnis ein benanntes
  Docker-Volume: Es ueberlebt Neustart, Neubau des Images und Updates.

Ersteinrichtung: Solange kein Zugang da ist, fuehrt jede Seite zur Einrichtung.
Die Seite ist aus dem Internet erreichbar. Damit niemand anderes zuvorkommt,
braucht die Einrichtung einen Einrichtungscode. Er steht beim Start im
Protokoll des Containers und gilt bis zur Einrichtung.

Sessions liegen im Speicher. Nach einem Neustart meldet man sich neu an.

Passwort vergessen:
    python -m backend.auth zuruecksetzen
loescht den Zugang. Danach den Container neu starten, im Protokoll steht dann
ein neuer Einrichtungscode.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from backend.files import write_atomic

logger = logging.getLogger("angebotsfinder.zugang")

CREDENTIALS_FILE = "zugang.json"
COOKIE_NAME = "angebotsfinder_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60
"""Eine Woche. Am Handy waere eine taegliche Anmeldung laestig, und nach einem
Neustart des Containers ist die Session ohnehin weg."""

MIN_PASSWORD = 10
MAX_PASSWORD = 256
MAX_USERNAME = 64

# OWASP Password Storage Cheat Sheet, Argon2id: 19 MiB Speicher, 2 Durchgaenge,
# 1 Strang. Eine Pruefung dauert damit auf einem PC rund 25 ms, auf einer
# kleinen NAS einige hundert.
ARGON2_MEMORY_KIB = 19 * 1024
ARGON2_ITERATIONS = 2
ARGON2_LANES = 1
SALT_BYTES = 16
HASH_BYTES = 32

# Ohne 0/O und 1/I, damit sich der Code aus dem Protokoll sicher abtippen laesst.
# 12 Zeichen aus 32 sind 60 Bit: Durchprobieren ist aussichtslos.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 12


# --- Passwort-Hash -----------------------------------------------------------

def normalize_password(password: str) -> str:
    """NFKC wie in NIST SP 800-63B: Ein 'ö' ist auf jedem Geraet dasselbe 'ö'."""
    return unicodedata.normalize("NFKC", password)


def hash_password(password: str) -> str:
    kdf = Argon2id(salt=os.urandom(SALT_BYTES), length=HASH_BYTES,
                   iterations=ARGON2_ITERATIONS, lanes=ARGON2_LANES,
                   memory_cost=ARGON2_MEMORY_KIB)
    return kdf.derive_phc_encoded(normalize_password(password).encode("utf-8"))


def verify_password(password: str, encoded: str) -> bool:
    """Vergleich in konstanter Zeit (cryptography). Parameter kommen aus dem Hash."""
    try:
        Argon2id.verify_phc_encoded(normalize_password(password).encode("utf-8"), encoded)
    except (InvalidKey, ValueError):
        return False
    return True


def selftest() -> None:
    """Beim Start: Ohne Argon2id kann niemand einen Zugang anlegen. Dann lieber
    sofort mit klarer Meldung abbrechen als erst bei der Einrichtung."""
    encoded = hash_password("selbsttest")
    if not verify_password("selbsttest", encoded) or verify_password("anders", encoded):
        raise RuntimeError("Argon2id-Selbsttest fehlgeschlagen")


# --- Eingaben bei der Einrichtung --------------------------------------------

def normalize_username(username: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", username).split())


def _username_key(username: str) -> bytes:
    """Gross/klein egal: Handys schreiben das erste Zeichen gern gross."""
    return normalize_username(username).casefold().encode("utf-8")


def validate_new_credentials(username: str, password: str, repeat: str) -> str:
    """Prueft Benutzer und Passwort fuer die Einrichtung. Gibt den Benutzernamen
    zurueck, wie er gespeichert wird, oder wirft ValueError mit einem Satz fuer
    die Seite."""
    name = normalize_username(username)
    if not name:
        raise ValueError("Bitte einen Benutzernamen eingeben.")
    if len(name) > MAX_USERNAME:
        raise ValueError(f"Der Benutzername darf höchstens {MAX_USERNAME} Zeichen haben.")
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise ValueError("Der Benutzername enthält unzulässige Zeichen.")
    if password != repeat:
        raise ValueError("Die beiden Passwörter stimmen nicht überein.")
    length = len(normalize_password(password))
    if length < MIN_PASSWORD:
        raise ValueError(f"Das Passwort braucht mindestens {MIN_PASSWORD} Zeichen.")
    if length > MAX_PASSWORD:
        raise ValueError(f"Das Passwort darf höchstens {MAX_PASSWORD} Zeichen haben.")
    if normalize_password(password).casefold() == name.casefold():
        raise ValueError("Das Passwort darf nicht der Benutzername sein.")
    return name


# --- Einrichtungscode ----------------------------------------------------------

def new_setup_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LENGTH, 4))


def _code_key(code: str) -> bytes:
    """Bindestriche, Leerzeichen und Kleinschreibung beim Abtippen sind egal."""
    return "".join(ch for ch in code.upper() if ch.isalnum()).encode("utf-8")


def setup_code_matches(given: str, expected: str | None) -> bool:
    if not expected:
        return False
    return secrets.compare_digest(_code_key(given), _code_key(expected))


# --- Ablage ------------------------------------------------------------------

@dataclass(frozen=True)
class Credentials:
    username: str
    password_hash: str
    created_at: str


class CredentialStore:
    """Der eine Zugang als Datei im Datenverzeichnis (Docker-Volume)."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / CREDENTIALS_FILE
        self.credentials: Credentials | None = None
        self.problem: str | None = None
        """Warum eine vorhandene Datei nicht taugt - nur fuers Protokoll."""

    @property
    def configured(self) -> bool:
        return self.credentials is not None

    def load(self) -> None:
        self.problem = None
        if not self.path.exists():
            self.credentials = None
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            username = str(data["username"])
            password_hash = str(data["password_hash"])
            if not username or not password_hash.startswith("$argon2id$"):
                raise ValueError("unbekanntes Format")
            self.credentials = Credentials(username, password_hash, str(data.get("created_at", "")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Beschaedigt: wie ohne Zugang. Die Einrichtung braucht den Code aus
            # dem Protokoll, es kommt also niemand Fremdes hinein.
            self.credentials = None
            self.problem = f"{self.path} ist beschädigt ({exc})"

    def save(self, username: str, password: str, now: datetime) -> None:
        credentials = Credentials(username, hash_password(password), now.isoformat(timespec="seconds"))
        content = json.dumps({"version": 1, "username": credentials.username,
                              "password_hash": credentials.password_hash,
                              "created_at": credentials.created_at},
                             indent=2, ensure_ascii=False).encode("utf-8")
        write_atomic(self.path, content)
        self.credentials = credentials

    def verify(self, username: str, password: str) -> bool:
        credentials = self.credentials
        if credentials is None:
            return False
        name_ok = secrets.compare_digest(_username_key(username), _username_key(credentials.username))
        # Immer rechnen, auch bei falschem Namen. Sonst verraet die Antwortzeit,
        # ob es den Namen gibt.
        password_ok = verify_password(password, credentials.password_hash)
        return name_ok and password_ok

    def reset(self) -> bool:
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        self.credentials = None
        return existed


# --- Sessions ----------------------------------------------------------------

class Sessions:
    """Session-Token -> Zeitpunkt der Anmeldung. Nur im Speicher."""

    def __init__(self, max_age: float = SESSION_MAX_AGE, clock=time.time):
        self.max_age = max_age
        self.clock = clock
        self._tokens: dict[str, float] = {}

    def create(self) -> str:
        now = self.clock()
        # Abgelaufene beim Anmelden aufraeumen, sonst blieben sie ewig im Speicher.
        for token, created in list(self._tokens.items()):
            if now - created > self.max_age:
                del self._tokens[token]
        token = secrets.token_urlsafe(32)
        self._tokens[token] = now
        return token

    def valid(self, token: str | None) -> bool:
        created = self._tokens.get(token or "")
        if created is None:
            return False
        if self.clock() - created > self.max_age:
            self._tokens.pop(token, None)
            return False
        return True

    def delete(self, token: str | None) -> None:
        self._tokens.pop(token or "", None)

    def clear(self) -> None:
        self._tokens.clear()


# --- Bremse gegen Durchprobieren -----------------------------------------------

MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 15 * 60
MAX_TRACKED = 10_000


class LoginThrottle:
    """Je IP hoechstens MAX_ATTEMPTS Fehlversuche in ATTEMPT_WINDOW Sekunden.

    Ein Versuch zaehlt schon beim Start und wird erst bei Erfolg vergessen. Sonst
    kaemen viele gleichzeitige Anfragen alle durch, bevor der erste Fehlversuch
    feststeht. Die IP ist verlaesslich, weil uvicorn X-Forwarded-For nur von
    Proxys im privaten Netz uebernimmt (Dockerfile).
    """

    def __init__(self, max_attempts: int = MAX_ATTEMPTS, window: float = ATTEMPT_WINDOW,
                 clock=time.monotonic, max_tracked: int = MAX_TRACKED):
        self.max_attempts = max_attempts
        self.window = window
        self.clock = clock
        self.max_tracked = max_tracked
        self._attempts: dict[str, list[float]] = {}

    def _recent(self, ip: str, now: float) -> list[float]:
        recent = [t for t in self._attempts.get(ip, []) if now - t < self.window]
        if recent:
            self._attempts[ip] = recent
        else:
            self._attempts.pop(ip, None)
        return recent

    def retry_after(self, ip: str) -> float:
        """0: Versuch erlaubt. Sonst die Sekunden, bis der aelteste Versuch verfaellt."""
        now = self.clock()
        recent = self._recent(ip, now)
        if len(recent) < self.max_attempts:
            return 0.0
        return max(1.0, self.window - (now - recent[0]))

    def attempt(self, ip: str) -> None:
        now = self.clock()
        if ip not in self._attempts and len(self._attempts) >= self.max_tracked:
            # Viele verschiedene IPs: erst Abgelaufene aufraeumen, sonst die aelteste raus.
            for other in list(self._attempts):
                self._recent(other, now)
            if len(self._attempts) >= self.max_tracked:
                oldest = min(self._attempts, key=lambda k: self._attempts[k][-1])
                del self._attempts[oldest]
        self._attempts.setdefault(ip, []).append(now)

    def success(self, ip: str) -> None:
        self._attempts.pop(ip, None)


# --- Zuruecksetzen per Kommandozeile -----------------------------------------

USAGE = """Aufruf: python -m backend.auth zuruecksetzen

Loescht den gespeicherten Zugang (Benutzer und Passwort). Danach den Container
neu starten. Im Protokoll steht dann ein neuer Einrichtungscode, mit dem du im
Browser Benutzer und Passwort neu festlegst."""


def main(argv: list[str]) -> int:
    from backend import settings

    if argv[1:] != ["zuruecksetzen"]:
        print(USAGE)
        return 2
    store = CredentialStore(settings.data_dir())
    if store.reset():
        print("Zugang gelöscht. Bitte den Container jetzt neu starten. Danach steht im "
              "Protokoll ein neuer Einrichtungscode.")
    else:
        print(f"Es war kein Zugang eingerichtet ({store.path} fehlt).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
