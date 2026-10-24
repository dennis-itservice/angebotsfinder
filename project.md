# Angebotsfinder

> **Konvention:** Diese Datei ist die alleinstehende Wissensbasis des Projekts.
> Vor jedem Push und nach jeder Erkenntnis aktualisieren. Keine Passwörter oder
> persönlichen Daten eintragen, das Repo wird mit Freunden geteilt.
> Die Anleitung für Nutzer steht in `README.md`.

## Übersicht

Der Prospekt-Check aus Sentinel als eigenständiger Docker-Container für Freunde.
Eine Merkliste (`config/angebote.yaml`) nennt Postleitzahl, Geschäfte, Produkte und
Prüftermine. Der Angebotsfinder fragt zu den Terminen bei marktguru.de ab (dazu LOGO
Getränkemarkt direkt bei logo-getraenke.de), zeigt die Treffer auf einer Seite und
schickt neue Angebote einmal als Web-Push aufs Handy. Anmeldung mit einem Zugang,
der beim ersten Aufruf festgelegt wird.

- **Entwicklung:** `H:\Documents\Softwareprojekte\angebotsfinder\`
- **GitHub:** `dennis-itservice/angebotsfinder` (privat, zum Teilen mit Freunden)
- **Herkunft:** Sentinel 2.3.1 (`H:\Documents\Softwareprojekte\sentinel\`), Stand
  24.10.2026. Abgleich, marktguru- und LOGO-Abfrage und Push-Versand sind fast
  unverändert übernommen, siehe „Unterschiede zu Sentinel“.
- **Betrieb:** bei den Freunden, nicht bei Dennis. Es gibt keine Produktion, die
  Dennis betreut.
- **Stand:** Version 1.0.0 vom 24.10.2026. Lokal mit Uvicorn gegen die echten
  Quellen geprüft. Der Docker-Build selbst ist ungetestet, auf dem Entwicklungsrechner
  gibt es kein Docker (siehe „Geprüft“ und „Offene Punkte“).

Auftrag von Dennis (24.10.2026): Freunde wollen das Angebote-Modul aus Sentinel mit
eigenen Produkten und Geschäften. Neues Repo zum Teilen, nur das Nötige. Login mit
Benutzer und Passwort, beim ersten Aufruf festgelegt, weil die Seite für Push aus
dem Internet erreichbar sein soll. Passwort sicher im Container ablegen, es muss
einen Neubau überleben. README für Nutzer, die technisch nicht fit sind. Läuft als
Docker-Container. Am Ende eine eigene Review.

---

## Tech-Stack

- **Backend:** FastAPI + Uvicorn (async), Python 3.12 im Container (lokal 3.14 getestet)
- **Abfrage:** aiohttp (marktguru-API, LOGO-Seite), LOGO-HTML mit `html.parser`
- **Push:** Web-Push mit pywebpush (VAPID), wie in Sentinel und im SiWa-Portal
- **Passwort-Hash:** Argon2id aus `cryptography` (ohnehin für Web-Push im Image)
- **Frontend:** Vanilla HTML/CSS/JS ohne Build-Schritt, SVG-Symbole statt Icon-Schrift
- **Container:** `python:3.12-slim`, Benutzer UID 10001, Port 8098
- **Tests:** pytest, ohne Netzzugriff

Versionen in `requirements.txt` gepinnt, Stand 24.10.2026, `pip-audit` ohne Befund.
Wie Sentinel 2.3.1, nur **aiohttp 3.14.5 statt 3.14.3**: 3.14.3 hat 8 bekannte
Lücken (behoben in 3.14.4, u. a. CVE-2026-111064 bis -111066, -111748 bis -111750).
Die meisten betreffen den aiohttp-Server, den weder Sentinel noch der
Angebotsfinder nutzen. **Sentinel hat noch 3.14.3** (Hinweis an Dennis, dort nichts
geändert).

Alle Laufzeit-Pakete gibt es als Wheel für Python 3.12 auf Linux amd64 und arm64
(geprüft am 24.10.2026 mit `pip download --platform …`). Einzige Ausnahme ist
`http_ece` (nur Quellpaket, reines Python, baut ohne Compiler wie in Sentinel).
Für 32-Bit-ARM ist nichts geprüft. Die Linux-Wheels von cryptography 50.0.2 enthalten
Argon2id (OpenSSL-Implementierung, im `.so` nachgesehen).

---

## Architektur

| Datei | Inhalt |
|---|---|
| `backend/main.py` | App-Fabrik `create_app()`, Middleware (Anmeldung, Sicherheitsheader), Endpunkte, `Access` (Zugang, Sessions, Einrichtungscode), `Runner` (Prüfschleife) |
| `backend/auth.py` | Argon2id-Hash, `CredentialStore` (`zugang.json`), Einrichtungscode, `Sessions`, Kommandozeile `zuruecksetzen` |
| `backend/monitor.py` | `AngeboteMonitor`: Termine, Zustand im Volume, Text für die Seite (aus Sentinel `angebote_monitor.py`) |
| `backend/angebote.py` | Merkliste, Abgleich, Zeitplan, Texte, reine Funktionen (aus Sentinel, fast unverändert) |
| `backend/marktguru.py`, `backend/logo.py` | Abfrage der Quellen (aus Sentinel, nur User-Agent geändert) |
| `backend/push.py` | `PushStore`, `PushSender` mit Versand im Hintergrund (aus Sentinel, ohne Alarm-Logik) |
| `backend/settings.py` | Pfade und Umgebungsvariablen |
| `frontend/` | `index.html`/`app.js` (Angebote), `push.html`/`push.js`, `login.html`, `setup.html`, `sw.js`, `manifest.webmanifest`, `icons/` |

Ablauf: Der `Runner` ruft jede Minute `AngeboteMonitor.check()` mit Zeitlimit 30 s.
Das kostet fast nichts. Abgefragt wird nur zum Termin, nach einer Änderung der
Merkliste, nach einem Fehler stündlich oder auf Knopfdruck (Details in Sentinels
`sentinel_project.md`, Abschnitt „Monitor-Typ angebote“, gilt hier genauso). Neue
Treffer liefert `check()` als `notifications`, der `Runner` gibt sie an
`PushSender.send_in_background()`. Statuswechsel stehen im Protokoll, nicht jeder Lauf.

---

## Entscheidungen

### Zugang und Ersteinrichtung

- **Ein Zugang** (Benutzer + Passwort). Mehrere Handys gehen über die Push-Geräte,
  Benutzerverwaltung wäre mehr als nötig.
- **Ersteinrichtung mit Einrichtungscode.** Dennis wollte: beim ersten Login Benutzer
  und Passwort festlegen. Die Seite ist aus dem Internet erreichbar, ohne Schutz
  könnte ein Fremder zwischen Start und Einrichtung zuvorkommen. Deshalb schreibt der
  Container einen Code (`XXXX-XXXX-XXXX`, 12 Zeichen aus 32, 60 Bit) ins Protokoll,
  die Einrichtung verlangt ihn. Verworfen: Einrichtung nur aus dem Heimnetz
  (hinter einem Reverse Proxy im Bridge-Netz kommt jede Anfrage von einer privaten
  IP, die Prüfung wäre wirkungslos) und ein Zeitfenster nach dem Start (schwach).
  Der Code gilt nur im Speicher und bis zur Einrichtung, nach einem Neustart gibt es
  einen neuen. Groß/klein, Leerzeichen und Bindestriche beim Abtippen sind egal.
- **Passwort-Hash Argon2id** (OWASP- und RFC-9106-Empfehlung) mit den OWASP-Werten
  m = 19 MiB, t = 2, p = 1, Salz 16 Byte, PHC-Format. Aus `cryptography`
  (`Argon2id.derive_phc_encoded`/`verify_phc_encoded`), kein neues Paket. Rund 25 ms
  auf dem PC. Passwort und Benutzername werden NFKC-normalisiert (NIST SP 800-63B),
  damit ein „ö“ auf jedem Gerät gleich ist. Beim Start ein Selbsttest: fehlt
  Argon2id, bricht der Start mit klarer Meldung ab statt erst bei der Einrichtung.
- **Ablage** in `data/zugang.json` (Benutzer, Hash, Zeitpunkt), Rechte 0600, atomar
  geschrieben. `/app/data` ist das **benannte Volume `angebotsfinder-daten` mit festem
  Namen** (`name:` in der Compose-Datei), damit es auch bei anderem Projektordner
  dasselbe bleibt. Das Volume überlebt Neustart, *Erstellen*, Neubau und Updates.
  Nicht auf der Freigabe, weil dort oft „Jeder“ lesen darf (Erfahrung aus Sentinel).
- **Regeln:** Benutzername 1 bis 64 Zeichen ohne Steuerzeichen, beim Anmelden
  Groß/klein egal (Handys schreiben den ersten Buchstaben groß). Passwort 10 bis 256
  Zeichen, nicht gleich dem Benutzernamen, sonst keine Kompositionsregeln.
- **Anmeldung:** Hash-Prüfung im Thread, höchstens eine gleichzeitig (Semaphore), damit
  eine Flut von Anmeldeversuchen weder Speicher noch Prozessor sprengt. Bei falschem
  Namen wird trotzdem gerechnet (keine Auskunft über Namen per Antwortzeit). 1 s
  Verzögerung und Protokolleintrag bei jedem Fehlversuch, Formular höchstens 4 KB.
- **Sessions** im Speicher wie in Sentinel, aber **7 Tage** statt 24 h: Am Handy wäre
  die tägliche Anmeldung lästig, nach einem Neustart sind sie ohnehin weg.
- **Secure-Cookie automatisch:** `secure` hängt an `request.url.scheme`. Uvicorn
  übernimmt `X-Forwarded-Proto` und `-For` von jedem Absender
  (`--forwarded-allow-ips "*"`). Gefälscht ändert das nur die IP im Protokoll oder
  macht das eigene Cookie unbrauchbar, deshalb vertretbar. Über `http://` im Heimnetz
  klappt die Anmeldung weiter, über den HTTPS-Proxy bekommt das Cookie `Secure`.
- **Zurücksetzen:** `python -m backend.auth zuruecksetzen` im Container löscht
  `zugang.json`. Die App merkt das beim nächsten Aufruf von `/login` oder `/setup`,
  beendet alle Sessions und schreibt einen neuen Code ins Protokoll. Die README sagt
  trotzdem „neu starten“, das ist für Nutzer eindeutiger.
- **Beschädigte `zugang.json`** gilt wie keine: Einrichtung mit Code. Fremde kommen
  so nicht hinein.

### Sonstiges

- **Code im Image** statt eingebunden (anders als Sentinel): Ein Update ist immer
  *Erstellen*, ohne Sonderfälle. `pull_policy: build` sorgt dafür, dass der
  Container Manager wirklich neu baut (Sentinel-Erfahrung vom 03.10.2026).
- **Code gehört root**, nur `/app/data` gehört UID 10001 (Rechte 700). Die App kann
  ihren eigenen Code nicht ändern.
- **Merkliste als Verzeichnis eingebunden** (`./config:/app/config:ro`), wie in
  Sentinel wegen Editoren, die beim Speichern eine neue Datei anlegen.
- **Bridge-Netz mit Port 8098** statt `network_mode: host`: läuft auch auf Docker
  Desktop und kollidiert nicht mit Sentinel (8099).
- **Content-Security-Policy** `default-src 'self'` ohne `unsafe-inline`. Deshalb keine
  Inline-Skripte und -Styles, Fehlermeldungen auf Anmelde- und Einrichtungsseite setzt
  der Server ein (Platzhalter `__FEHLER__`, `__BENUTZER__`, maskiert). Keine Google
  Fonts mehr (in Sentinel ein offener Punkt), Symbole als SVG aus den Material Icons
  (Apache 2.0).
- **iPhone:** `manifest.webmanifest` mit `display: standalone` und Symbole
  (192/512 px, `apple-touch-icon` 180 px, aus `icons/icon.svg` per Headless-Chrome
  gerendert). Damit geht Push als Home-Bildschirm-App ab iOS 16.4. In Sentinel fehlte
  das noch. Am echten iPhone **nicht** getestet.
- **Push:** nur Angebote, keine Störungsmeldungen, keine Ruhezeit. TTL **24 h** statt
  6 h (Sentinel-Offenpunkt „Push für Angebote hält 6 h“). Kontaktadresse (VAPID `sub`)
  aus `ANGEBOTSFINDER_PUSH_CONTACT`, sonst `mailto:angebotsfinder@example.com`. Eine
  ungültige Adresse schaltet nur Push ab, nicht die App.
- **Keine API-Doku nach außen** (`/docs`, `/openapi.json` abgeschaltet).
- **User-Agent** `Angebotsfinder (private Angebotsabfrage)`, ohne Dennis' Adresse.
- **Logs auf Deutsch**, weil die Nutzer den Einrichtungscode dort suchen.

### Unterschiede zu Sentinel

| Sentinel | Angebotsfinder |
|---|---|
| Dashboard mit Kacheln, Detailseite je Dienst | Eine Seite mit den Angeboten (ehemals Detailseite) |
| `services.yaml`, mehrere Monitore, Scheduler | Ein `AngeboteMonitor`, `Runner` in `main.py` |
| Zugangsdaten aus `sentinel.env` | Ersteinrichtung im Browser, Argon2id-Hash im Volume |
| Push mit Alarm-Logik, Ruhezeit, TTL 6 h | Push nur für Angebote, TTL 24 h |
| Push-Tag `sentinel-angebote`, URL `/detail/angebote` | Tag `angebote`, URL `/` |
| Zustand `angebote-<name>.json`, `version: 2` | `angebote.json`, `version: 1`, ohne Übernahme alter Stände |
| Code eingebunden, `network_mode: host`, Port 8099 | Code im Image, Bridge, Port 8098 |
| Google Fonts, keine CSP | SVG-Symbole, CSP |
| `get_details()` async | `get_details()` synchron |

Ein Fehler, der im Abgleich, bei marktguru oder LOGO in Sentinel behoben wird, gilt
meist auch hier. Die Dateien `angebote.py`, `marktguru.py` und `logo.py` sind bis
auf Kommentare, User-Agent und Push-Ziel gleich, ein `diff` zeigt das schnell.

---

## Konfiguration

**Merkliste** `config/angebote.yaml`: Format und Regeln wie in Sentinel, beschrieben
in der README (Abschnitt 4) und in der Vorlage `config/angebote.example.yaml`. Die
Vorlage hat EDEKA, REWE, Lidl, Monster Energy und Coca-Cola 12×1 l. LOGO steht
auskommentiert drin, ein Test prüft, dass das Einschalten funktioniert.

**Umgebungsvariablen** (alle optional):

| Variable | Zweck |
|---|---|
| `TZ` | Zeitzone, in der Compose-Datei `Europe/Berlin` |
| `ANGEBOTSFINDER_PUSH_CONTACT` | Kontakt für die Push-Dienste, `mailto:` oder `https://` |
| `ANGEBOTSFINDER_LOG_LEVEL` | Standard `INFO` |
| `ANGEBOTSFINDER_DATA_DIR` | Datenverzeichnis, Standard `data/` (im Container `/app/data`) |
| `ANGEBOTSFINDER_WATCHLIST` | Pfad der Merkliste, Standard `config/angebote.yaml` |

---

## API-Endpunkte

| Methode | Pfad | Anmeldung | Zweck |
|---|---|---|---|
| GET/POST | `/setup` | nein | Ersteinrichtung, nur solange kein Zugang da ist |
| GET/POST | `/login` | nein | Anmeldung (ohne Zugang: Weiterleitung zu `/setup`) |
| POST | `/logout` | ja | Abmelden |
| GET | `/`, `/push` | ja | Angebotsseite, Push-Seite |
| GET | `/sw.js`, `/manifest.webmanifest`, `/static/style.css`, `/static/icons/*` | nein | Service Worker, Manifest, Stil, Symbole |
| GET | `/api/angebote` | ja | Stand, Treffer, Merkliste, Einstellungen, `push_devices` |
| POST | `/api/angebote/pruefen` | ja | „Jetzt prüfen“, 429 innerhalb von 60 s nach dem letzten Abruf |
| GET | `/api/push/config` | ja | verfügbar?, öffentlicher Schlüssel |
| GET | `/api/push/devices` | ja | angemeldete Geräte ohne Endpoint und Schlüssel |
| POST | `/api/push/subscribe`, `/unsubscribe`, `/test`, `/devices/{id}/remove` | ja | Geräte verwalten, Testnachricht |

Ohne Session: Seiten → 303 auf `/login`, API → 401.

---

## Dateistruktur

```
angebotsfinder/
├── Dockerfile, docker-compose.yml, .dockerignore
├── requirements.txt, requirements-dev.txt, pytest.ini
├── README.md                  # Anleitung für Nutzer
├── project.md                 # diese Datei
├── config/
│   ├── angebote.example.yaml  # Vorlage (im Repo)
│   └── angebote.yaml          # echte Merkliste, nicht im Repo
├── backend/                   # siehe Architektur
├── frontend/
│   └── icons/                 # icon.svg, icon-192.png, icon-512.png, apple-touch-icon.png
└── tests/
    └── fixtures/              # echte Antworten von marktguru (10.10.2026) und LOGO (11.10.2026), aus Sentinel
```

---

## Lokales Testen

Docker ist auf dem Entwicklungsrechner nicht installiert. Die venv liegt bewusst nicht
auf H: (Server-Spiegelung): `C:\Dev\venvs\angebotsfinder`.

```bash
cd /h/Documents/Softwareprojekte/angebotsfinder
python -m venv /c/Dev/venvs/angebotsfinder                 # einmalig
/c/Dev/venvs/angebotsfinder/Scripts/python -m pip install -r requirements-dev.txt
/c/Dev/venvs/angebotsfinder/Scripts/python -m pytest -p no:cacheprovider
```

Starten gegen die echten Quellen, Daten und Merkliste außerhalb des Projekts:

```bash
FORWARDED_ALLOW_IPS='*' PYTHONUTF8=1 \
ANGEBOTSFINDER_DATA_DIR=<ordner>/data ANGEBOTSFINDER_WATCHLIST=<ordner>/angebote.yaml \
/c/Dev/venvs/angebotsfinder/Scripts/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8098
# http://127.0.0.1:8098, Einrichtungscode steht in der Ausgabe
```

- **`--forwarded-allow-ips "*"` geht unter Windows nicht auf der Kommandozeile:**
  click (die Kommandozeile von Uvicorn) ersetzt `*` unter Windows durch die Dateien im
  Ordner. Deshalb die Umgebungsvariable `FORWARDED_ALLOW_IPS`. Im Container (Linux)
  gilt das nicht.
- `PYTHONUTF8=1`, sonst schreibt Python unter Windows Umlaute im Protokoll als cp1252.
- Push lässt sich über `http://127.0.0.1` ausprobieren, Browser behandeln localhost
  als sicher.
- Sichtprüfung wie in Sentinel: Headless-Chrome per DevTools-Protokoll, Chrome per
  PowerShell `Start-Process` mit `--remote-debugging-port`, Handybreite über
  `Emulation.setDeviceMetricsOverride` mit `mobile: true`.

---

## Geprüft (24.10.2026)

- **Tests:** 286, alle grün (Python 3.14, Windows). Übernommen aus Sentinel:
  Abgleich, marktguru, LOGO, Ablauf, Push-Versand (inklusive echtem verschlüsseltem
  Versand an einen lokalen Empfänger). Neu: Argon2id-Ablage, Einrichtungscode,
  Einrichtung, Anmeldung, Zurücksetzen, Neustart mit gleichem Volume, Secure-Cookie
  über HTTPS, CSP ohne Inline-Skripte, Wächter für Dockerfile und Compose-Datei.
- **Live lokal:** Uvicorn mit echter Abfrage. Vorlage: marktguru ohne Treffer (EDEKA,
  REWE, Lidl, Woche bis 24.10.). Mit LOGO (Schweinfurt, Sennfeld) und „Cola“ und
  „Bier“: 13 Treffer, LOGO-Woche 19.10.–24.10. mit 36 Angeboten. Die geänderte
  Merkliste wurde nach 19 s ohne Neustart geprüft.
- **Browser** (Headless-Chrome 155): Einrichtung mit falschem Code (Meldung, Name
  bleibt stehen), mit richtigem Code (gleich angemeldet), Angebotsseite, „Jetzt
  prüfen“, Push-Seite (Aktivieren-Knopf da), Abmelden, falsches Passwort, Anmeldung
  mit kleinem Anfangsbuchstaben. 360, 412 und 1280 px ohne waagerechtes Scrollen.
  Keine Konsolenfehler außer dem erwarteten 400 beim falschen Code, keine
  CSP-Verstöße.
- **Pakete:** `pip-audit` ohne Befund, Wheels für Linux amd64/arm64 vorhanden.
- **Nicht geprüft:** Docker-Build und Container (kein Docker auf dem
  Entwicklungsrechner), Push-Zustellung auf einem echten Handy (in Sentinel mit
  demselben Code belegt), iPhone als Home-Bildschirm-App, die Synology-Schritte der
  README auf einer fremden NAS.

---

## Offene Punkte

| Prio | Thema | Beschreibung |
|---|---|---|
| Hoch | **Erster Docker-Build** | Einmal bauen und starten, bevor die Freunde loslegen. `docker compose up -d --build`, dann Einrichtungscode im Protokoll, Einrichtung, Seite, Push-Test. |
| Mittel | **iPhone** | Push als Home-Bildschirm-App ist eingebaut, aber nicht auf einem echten iPhone getestet. |
| Mittel | **Leserechte der Merkliste auf einer Synology** | Der Container liest `config/` als UID 10001. Ohne Leserecht „Jeder“ auf dem Ordner meldet die Seite „nicht lesbar“. Die README beschreibt den Fix. |
| Niedrig | **marktguru und LOGO ohne offizielle Schnittstelle** | Wie in Sentinel. Ändert sich eine Seite, wird die Anzeige gelb. Korrekturen aus Sentinel übernehmen. |
| Niedrig | **aiohttp in Sentinel** | Sentinel nutzt noch aiohttp 3.14.3 mit 8 bekannten Lücken. Betrifft dort nur den Client, trotzdem beim nächsten Sentinel-Update anheben. |

---

## Änderungshistorie

| Datum | Änderung |
|---|---|
| 2026-10-24 | **1.0.0**: Erste Version aus Sentinel 2.3.1. Ersteinrichtung mit Einrichtungscode, Argon2id-Hash im Volume, Angebotsseite, Push mit iPhone-Manifest, CSP, README für Nutzer. 286 Tests |
