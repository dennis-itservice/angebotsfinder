# Angebotsfinder

Der Angebotsfinder schaut für dich in die Prospekte der Geschäfte in deiner Nähe.
Du legst eine Merkliste an, zum Beispiel „Coca-Cola im 12er-Kasten bei EDEKA, REWE
und Lidl“. Ist ein Produkt im Angebot, zeigt es die Seite an, und du bekommst eine
Push-Nachricht aufs Handy. Jedes Angebot meldet er nur einmal.

Die Angebote kommen von [marktguru.de](https://www.marktguru.de). Dort stehen die
Prospekte fast aller Ketten, regional passend zu deiner Postleitzahl. Die Angebote
von LOGO Getränkemarkt holt der Angebotsfinder direkt bei logo-getraenke.de.

Der Angebotsfinder läuft als Docker-Container auf einem Gerät bei dir zu Hause.
Er braucht kein Konto bei einem fremden Dienst und keine zusätzliche App.

**Inhalt**

1. [Was du brauchst](#1-was-du-brauchst)
2. [Installation](#2-installation)
3. [Erster Aufruf: Zugang anlegen](#3-erster-aufruf-zugang-anlegen)
4. [Merkliste einrichten](#4-merkliste-einrichten)
5. [Push-Nachrichten aufs Handy](#5-push-nachrichten-aufs-handy)
6. [Von unterwegs erreichbar machen (HTTPS)](#6-von-unterwegs-erreichbar-machen-https)
7. [Betrieb: Update, Neustart, Protokoll, Sicherung](#7-betrieb-update-neustart-protokoll-sicherung)
8. [Passwort vergessen](#8-passwort-vergessen)
9. [Wenn etwas nicht klappt](#9-wenn-etwas-nicht-klappt)
10. [Gut zu wissen](#10-gut-zu-wissen)

---

## 1. Was du brauchst

- **Ein Gerät, das rund um die Uhr läuft und Docker kann.** Zum Beispiel eine
  Synology-NAS mit dem Paket „Container Manager“, ein Raspberry Pi mit 64-Bit-System
  oder ein kleiner Heimserver mit Linux. Das Gerät braucht Internet.
- **Für Push-Nachrichten eine HTTPS-Adresse**, zum Beispiel
  `https://meinname.synology.me:8443`. Browser erlauben Push nur über HTTPS. Ohne
  HTTPS funktioniert die Seite im Heimnetz trotzdem, nur ohne Push. Wie du das
  einrichtest, steht in [Abschnitt 6](#6-von-unterwegs-erreichbar-machen-https).

Auf 32-Bit-Systemen (ältere Raspberry Pi OS) kann der Bau des Containers scheitern.

## 2. Installation

### 2.1 Dateien holen

Die Dateien liegen auf GitHub unter
`https://github.com/dennis-itservice/angebotsfinder`.

- **Ohne Vorkenntnisse:** Auf der GitHub-Seite oben auf den grünen Knopf **Code**
  klicken, dann **Download ZIP**. Die ZIP-Datei entpacken. Der Ordner heißt dann
  `angebotsfinder-main`. Benenne ihn in `angebotsfinder` um.
- **Mit git:** `git clone https://github.com/dennis-itservice/angebotsfinder.git`

Im Ordner liegen unter anderem `docker-compose.yml`, `Dockerfile`, diese Anleitung
und der Ordner `config`.

### 2.2 Merkliste anlegen

Im Ordner `config` liegt die Vorlage `angebote.example.yaml`. Kopiere sie in
denselben Ordner und nenne die Kopie **`angebote.yaml`**.

Für den Anfang reicht die Vorlage. Deine eigenen Produkte und Geschäfte trägst du
später ein ([Abschnitt 4](#4-merkliste-einrichten)). Die Vorlage selbst bleibt
unverändert. Bei einem Update wird sie ersetzt, deine `angebote.yaml` nicht.

### 2.3 Starten auf einer Synology

1. **Ordner hochladen:** Öffne die File Station und lade den Ordner `angebotsfinder`
   in die Freigabe `docker`. Danach gibt es `/docker/angebotsfinder` mit der Datei
   `docker-compose.yml` darin. (Die Freigabe `docker` legt der Container Manager
   bei seiner Installation an.)
2. **Projekt anlegen:** Container Manager → **Projekt** → **Erstellen**.
   - Projektname: `angebotsfinder`
   - Pfad: `/docker/angebotsfinder`
   - Quelle: **Vorhandene docker-compose.yml verwenden**
3. Mit **Weiter** durchklicken und **Fertig**. Der Container Manager baut jetzt den
   Container. Beim ersten Mal dauert das einige Minuten. Danach steht das Projekt
   auf „Wird ausgeführt“.

### 2.4 Starten auf der Kommandozeile (Linux, Raspberry Pi)

```bash
cd angebotsfinder
docker compose up -d --build
```

Docker baut den Container und startet ihn. Er startet nach einem Neustart des
Geräts von selbst wieder.

## 3. Erster Aufruf: Zugang anlegen

1. Öffne im Browser `http://<Adresse-des-Geräts>:8098`, zum Beispiel
   `http://192.168.178.20:8098`. Die Adresse deiner NAS oder deines Pi siehst du in
   deinem Router.
2. Es erscheint die Seite **Einrichtung**. Lege einen Benutzernamen und ein
   Passwort fest. Das Passwort braucht mindestens 10 Zeichen.
3. Trage den **Einrichtungscode** ein. Er steht im Protokoll des Containers in
   einer Zeile wie `Einrichtungscode: K7RM-4QXD-9WTB`.
   - Synology: Container Manager → **Container** → `angebotsfinder` → **Details**
     → **Protokoll**
   - Kommandozeile: `docker compose logs angebotsfinder | grep Einrichtungscode`
4. Klicke auf **Zugang anlegen**. Du bist angemeldet und siehst die Angebotsseite.

Ab jetzt meldest du dich mit Benutzername und Passwort an. Groß- und
Kleinschreibung beim Benutzernamen ist egal, beim Passwort nicht. Eine Anmeldung
gilt eine Woche lang oder bis zum nächsten Neustart des Containers.

**Warum ein Einrichtungscode?** Die Seite ist später aus dem Internet erreichbar.
Ohne Code könnte ein Fremder schneller sein und den Zugang für sich anlegen. Den
Code sieht nur, wer das Protokoll des Containers lesen kann, also du. Er gilt nur
bis zur Einrichtung.

**Wie das Passwort gespeichert wird:** Der Angebotsfinder speichert das Passwort
nicht selbst, sondern nur einen Argon2id-Hash davon. Das ist das Verfahren, das
Sicherheitsfachleute heute für Passwörter empfehlen. Aus dem Hash lässt sich das
Passwort nicht zurückrechnen. Der Hash liegt im Docker-Volume `angebotsfinder-daten`
und bleibt bei Neustart, Neubau und Updates erhalten.

## 4. Merkliste einrichten

Die Merkliste ist die Datei `config/angebote.yaml`. Du bearbeitest sie mit einem
Texteditor.

- Synology: In der File Station die Datei mit dem Paket **Texteditor** öffnen oder
  herunterladen, am PC bearbeiten und wieder hochladen.
- Linux: zum Beispiel `nano config/angebote.yaml`.
- Windows: Editor oder Notepad++.

**Nach dem Speichern musst du nichts neu starten.** Der Angebotsfinder liest die
Datei jede Minute. Hat sich etwas geändert, prüft er sofort.

### So sieht eine Merkliste aus

```yaml
zip_code: "97421"

shops:
  - EDEKA
  - REWE
  - Lidl
  - LOGO: [Schweinfurt, Sennfeld]

products:
  - name: Monster Energy
  - name: Coca-Cola
    pack: 12x1l
  - Kaffee

schedule:
  days: [mo, mi, fr, so]
  time: "08:00"

days_ahead: 14
push: true
```

| Eintrag | Bedeutung | Ohne Angabe |
|---|---|---|
| `zip_code` | Deine Postleitzahl, in Anführungszeichen. Sie bestimmt die Prospekte. | muss angegeben werden |
| `shops` | Geschäfte, so geschrieben wie bei marktguru.de. Groß- und Kleinschreibung ist egal. `shops: []` heißt: alle Geschäfte. | alle Geschäfte |
| `products` | Was du suchst. Je Zeile ein Produkt. Mit `pack` grenzt du die Packung ein. | muss angegeben werden |
| `schedule` | An welchen Wochentagen und um wie viel Uhr geprüft wird. Tage: `mo di mi do fr sa so`. Die Uhrzeit in Anführungszeichen. | Mo, Mi, Fr, So um 08:00 |
| `days_ahead` | So viele Tage im Voraus zeigt der Angebotsfinder Angebote, die erst später beginnen (0 bis 60). | 14 |
| `push` | `true`: Push-Nachricht bei neuen Treffern. `false`: nur auf der Seite anzeigen. | `true` |

### Produkte

- Der Name muss **als ganzes Wort** im Angebot stehen. „Monster Energy“ findet keine
  „Monster Jam“-Spielzeugtrucks.
- Ein kurzer Name findet mehr. „Cola“ findet auch Mischgetränke und Angebote, bei
  denen eine Cola gratis dabei ist. Ein genauerer Name wie „Coca-Cola“ oder eine
  Packung hilft.
- `pack` grenzt die Packung ein: `12x1l` (Kasten mit 12 Flaschen zu 1 Liter),
  `6x1,5l`, `24x0,33l`, `24x330ml` oder `1,5l` für eine einzelne Flasche.
- Kurzform ohne Packung: `- Kaffee` statt `- name: Kaffee`.
- Höchstens 30 Produkte.

### Geschäfte

- Schreibe die Geschäfte so wie marktguru.de, zum Beispiel `ALDI SÜD`,
  `Netto Marken-Discount` oder `PENNY`. Welche Namen es in deiner Gegend gibt,
  zeigt die Angebotsseite unten unter **Weitere Geschäfte**.
- Der Name muss genau passen. `EDEKA` ist nicht `EDEKA Foodservice` (das ist der
  Großhandel).
- **LOGO Getränkemarkt** gibt es nicht bei marktguru. Schreibe die Märkte in eckige
  Klammern: `- LOGO: [Schweinfurt, Sennfeld]`. Die Namen stehen in der Marktliste
  auf logo-getraenke.de/angebote. Nur `- LOGO` ohne Märkte nimmt alle Angebote der
  Woche, auch die, die es nicht in jedem Markt gibt.

### Typische Fehler

- **Einrückung:** Die Zeilen unter `shops:` und `products:` beginnen mit zwei
  Leerzeichen. Keine Tabulatoren verwenden.
- **Anführungszeichen:** Postleitzahl und Uhrzeit gehören in Anführungszeichen,
  also `"08:00"` und `"97421"`.
- **Doppelpunkt:** Nach `zip_code`, `shops`, `products` und `schedule` steht ein
  Doppelpunkt.

Ein Fehler in der Datei legt nichts lahm. Die Seite zeigt ihn gelb an, mit Zeile
oder Eintrag, und prüft mit dem letzten gültigen Stand weiter.

## 5. Push-Nachrichten aufs Handy

Push geht nur über die HTTPS-Adresse ([Abschnitt 6](#6-von-unterwegs-erreichbar-machen-https)).
Das Handy muss für die Zustellung nicht im Heimnetz sein.

**Android (Chrome, Samsung Internet, Firefox):**

1. Die HTTPS-Adresse im Browser öffnen und anmelden.
2. Oben rechts auf die **Glocke** tippen.
3. **Auf diesem Gerät aktivieren** und Benachrichtigungen erlauben.
4. **Test senden.** Nach wenigen Sekunden erscheint die Testnachricht.

**iPhone und iPad (ab iOS 16.4):** Apple erlaubt Push nur für Seiten auf dem
Home-Bildschirm.

1. Die HTTPS-Adresse in **Safari** öffnen.
2. Auf **Teilen** tippen, dann **Zum Home-Bildschirm**.
3. Den Angebotsfinder vom Home-Bildschirm aus öffnen und dort anmelden.
4. Glocke → **Auf diesem Gerät aktivieren** → Mitteilungen erlauben → **Test senden**.

**PC (Chrome, Edge, Firefox):** wie bei Android. Nachrichten kommen, solange der
Browser läuft.

Danach kommt zu jedem neuen Angebot aus deiner Merkliste eine Nachricht mit
Geschäft, Preis und Zeitraum. Jedes Angebot kommt nur einmal. Antippen öffnet die
Angebotsseite. Auf der Seite mit der Glocke siehst du alle angemeldeten Geräte und
kannst einzelne entfernen. Mehrere Handys gehen, zum Beispiel für die ganze Familie.

## 6. Von unterwegs erreichbar machen (HTTPS)

Für Push-Nachrichten und für den Aufruf von unterwegs braucht der Angebotsfinder
eine HTTPS-Adresse. Dafür sind drei Dinge nötig:

1. **Ein Name für deinen Internetanschluss** (DynDNS), zum Beispiel
   `meinname.synology.me`. Deine IP-Adresse zu Hause ändert sich regelmäßig, der
   Name zeigt immer auf die aktuelle.
2. **Eine Portfreigabe im Router**, damit Anfragen aus dem Internet beim Gerät
   ankommen.
3. **Ein Reverse Proxy mit Zertifikat.** Er nimmt die HTTPS-Anfragen an und reicht
   sie an den Angebotsfinder auf Port 8098 weiter.

### Mit einer Synology

1. **DDNS:** Systemsteuerung → **Externer Zugriff** → **DDNS** → **Hinzufügen**.
   Dienstanbieter „Synology“, einen freien Hostnamen wählen, zum Beispiel
   `meinname.synology.me`. Die Option für ein Zertifikat von Let's Encrypt
   einschalten.
2. **Reverse Proxy:** Systemsteuerung → **Anmeldeportal** → **Erweitert** →
   **Reverse Proxy** → **Erstellen**.
   - Quelle: Protokoll **HTTPS**, Hostname `meinname.synology.me`, Port **8443**.
     Trage den Hostnamen vor dem Port ein.
   - Ziel: Protokoll **HTTP**, Hostname `localhost`, Port **8098**.
3. **Zertifikat:** Systemsteuerung → **Sicherheit** → **Zertifikat** →
   **Einstellungen**. Beim Eintrag `meinname.synology.me:8443` das Zertifikat für
   `meinname.synology.me` auswählen.
4. **Router:** Den Port **8443** (TCP) an die NAS weiterleiten. Bei einer Fritz!Box:
   Internet → **Freigaben** → **Portfreigaben** → Gerät für Freigaben hinzufügen →
   NAS auswählen → **Neue Freigabe**. Für die Ausstellung und Erneuerung des
   Zertifikats braucht Let's Encrypt außerdem Port **80** zur NAS.
5. Ist die Firewall der NAS eingeschaltet, Port 8443 dort erlauben.

Deine Adresse lautet dann `https://meinname.synology.me:8443`. Teste sie einmal
über das Mobilfunknetz, also mit ausgeschaltetem WLAN.

Hast du eine eigene Domain, geht auch eine Adresse wie
`https://angebote.meinname.de`. Dann zeigt ein CNAME-Eintrag bei deinem
Domain-Anbieter auf deine DDNS-Adresse, und der Reverse Proxy lauscht mit diesem
Hostnamen auf Port 443.

### Ohne Synology

Jeder Reverse Proxy mit Let's-Encrypt-Zertifikat geht, zum Beispiel
[Nginx Proxy Manager](https://nginxproxymanager.com) oder [Caddy](https://caddyserver.com).
Ziel ist `http://<Adresse-des-Geräts>:8098`. Wichtig sind die üblichen Kopfzeilen
`X-Forwarded-For` und `X-Forwarded-Proto`, beide Programme setzen sie von selbst.

**Gib Port 8098 nie direkt im Router frei.** Dort gibt es kein HTTPS, Passwörter
gingen unverschlüsselt durchs Internet. Nach außen gehört nur der Port des Reverse
Proxys.

## 7. Betrieb: Update, Neustart, Protokoll, Sicherung

| Aufgabe | Synology (Container Manager) | Kommandozeile |
|---|---|---|
| Protokoll ansehen | Container → `angebotsfinder` → Details → Protokoll | `docker compose logs angebotsfinder` |
| Neu starten | Container → `angebotsfinder` → Aktion → Neu starten | `docker compose restart` |
| Stoppen | Projekt → `angebotsfinder` → Aktion → Stoppen | `docker compose down` |
| Update | siehe unten | siehe unten |

### Update auf eine neue Version

1. Neue Dateien holen: ZIP herunterladen und entpacken, oder `git pull`.
2. Die neuen Dateien über die alten kopieren. Deine `config/angebote.yaml` ist im
   ZIP nicht enthalten und bleibt erhalten.
3. Neu bauen:
   - Synology: Container Manager → **Projekt** → `angebotsfinder` → **Aktion** →
     **Erstellen**. Das baut den Container mit den neuen Dateien neu.
   - Kommandozeile: `docker compose up -d --build`
4. Unten links auf der Seite steht die Version.

Zugang, Push-Geräte und gemeldete Angebote bleiben bei einem Update erhalten. Sie
liegen im Docker-Volume `angebotsfinder-daten`, nicht im Container.

### Sicherung

- `config/angebote.yaml` ist eine normale Datei. Nimm sie in deine Datensicherung auf.
- Das Volume `angebotsfinder-daten` enthält den Passwort-Hash, den Schlüssel für
  Push-Nachrichten und die Liste der gemeldeten Angebote. Geht es verloren, ist das
  kein Drama: Du richtest den Zugang neu ein und meldest die Handys neu für Push an.

### Entfernen

Projekt stoppen und löschen (Synology: Projekt → Aktion → Löschen). Das Volume
entfernt `docker volume rm angebotsfinder-daten`.

## 8. Passwort vergessen

Setze den Zugang zurück und lege ihn neu an.

**Synology:**

1. Container Manager → **Container** → `angebotsfinder` → **Details** → **Terminal**
   → **Erstellen**. Es öffnet sich eine Eingabezeile im Container.
2. Eingeben und mit Enter bestätigen:
   ```
   python -m backend.auth zuruecksetzen
   ```
3. Container → `angebotsfinder` → **Aktion** → **Neu starten**.
4. Seite öffnen und wie beim ersten Mal einrichten. Der neue Einrichtungscode steht
   im Protokoll ([Abschnitt 3](#3-erster-aufruf-zugang-anlegen)).

**Kommandozeile:**

```bash
docker exec angebotsfinder python -m backend.auth zuruecksetzen
docker compose restart
docker compose logs angebotsfinder | grep Einrichtungscode
```

Push-Geräte und Merkliste bleiben dabei erhalten.

## 9. Wenn etwas nicht klappt

**Die Seite zeigt gelb: „config/angebote.yaml fehlt“**
Die Vorlage wurde noch nicht kopiert, siehe [2.2](#22-merkliste-anlegen).

**„config/angebote.yaml nicht lesbar: Permission denied“**
Der Container läuft aus Sicherheitsgründen nicht als Administrator und darf die
Datei nicht lesen. Synology: In der File Station Rechtsklick auf den Ordner
`config` → Eigenschaften → Berechtigung → Erstellen → Benutzer oder Gruppe
**Jeder**, Recht **Lesen**, auf Unterordner und Dateien anwenden. Linux:
`chmod -R a+rX config`.

**„kein gültiges YAML in Zeile …“**
In der genannten Zeile oder kurz davor stimmt die Schreibweise nicht. Meist ist es
die Einrückung oder ein fehlender Doppelpunkt, siehe [Typische Fehler](#typische-fehler).

**Ein Produkt ist im Prospekt, der Angebotsfinder findet es nicht**
- Steht das Geschäft genau so in `shops` wie bei marktguru? Die Seite nennt unter
  „Weitere Geschäfte“ die Namen aus deiner Gegend.
- Ist der Produktname zu genau? Versuch es ohne `pack` oder mit einem kürzeren Namen.
- Stimmt die Postleitzahl? Viele Ketten haben regionale Prospekte.
- Einzelne Märkte mit eigenem Prospekt (manche EDEKA-Kaufleute) können abweichen.

**Die Seite zeigt gelb: „Abruf fehlgeschlagen …“**
marktguru oder LOGO war nicht erreichbar. Der Angebotsfinder versucht es jede
Stunde erneut und zeigt bis dahin den letzten Stand. Bleibt die Meldung tagelang,
hat sich vermutlich die Seite des Anbieters geändert.

**Die Einrichtung meldet „Der Einrichtungscode stimmt nicht“**
Nach jedem Neustart gibt es einen neuen Code. Nimm den jüngsten aus dem Protokoll.
Leerzeichen sowie Groß- und Kleinschreibung sind egal.

**Auf der Push-Seite steht „Push geht nur über eine HTTPS-Adresse“**
Du hast die Seite über `http://` geöffnet. Öffne sie über deine HTTPS-Adresse
([Abschnitt 6](#6-von-unterwegs-erreichbar-machen-https)).

**Auf dem iPhone steht „Auf dem iPhone geht Push nur, wenn …“**
Füge die Seite in Safari zum Home-Bildschirm hinzu und öffne sie von dort
([Abschnitt 5](#5-push-nachrichten-aufs-handy)).

**Die Testnachricht kommt nicht an**
Prüfe in den Einstellungen des Handys, ob der Browser (bzw. die App auf dem
Home-Bildschirm) Mitteilungen anzeigen darf. Energiesparmodi können Nachrichten
verzögern.

**Nach einem Update hat sich nichts geändert**
Steht unten links noch die alte Version, wurde nicht neu gebaut. Synology: Projekt →
Aktion → **Erstellen** (nicht nur „Neu starten“). Kommandozeile:
`docker compose up -d --build`.

**Port 8098 ist auf dem Gerät schon belegt**
In `docker-compose.yml` bei `ports` die linke Zahl ändern, zum Beispiel
`"8099:8098"`. Die Seite ist dann unter Port 8099 erreichbar, auch im Reverse Proxy.

## 10. Gut zu wissen

- **Wann geprüft wird:** zu den Terminen aus der Merkliste, sofort nach einer
  Änderung der Merkliste und mit dem Knopf **Jetzt prüfen** (höchstens einmal pro
  Minute). War der Container zu einem Termin aus, holt er die Prüfung nach.
- **Was abgefragt wird:** je Prüfung eine Suche pro Produkt bei marktguru.de. Mit
  LOGO kommen die Angebotsseite und eine Abfrage je Markt dazu. Bei vier Terminen
  pro Woche sind das wenige Abfragen.
- **Grenzen:** marktguru und LOGO bieten keine offizielle Schnittstelle an. Ändern
  sie ihre Seiten, wird die Anzeige gelb und nennt den Fehler. Der Angebotsfinder
  meldet dann nicht fälschlich „kein Angebot“.
- **Datenschutz:** Der Angebotsfinder verbindet sich nur mit marktguru.de, mit
  logo-getraenke.de (wenn LOGO in der Merkliste steht) und mit den Push-Diensten
  der Browser (Google, Apple, Mozilla). Die Push-Nachrichten sind
  Ende-zu-Ende-verschlüsselt. Die Seite lädt nichts von fremden Servern.
- **Für Entwickler:** Aufbau, Entscheidungen und Tests stehen in
  [project.md](project.md).
