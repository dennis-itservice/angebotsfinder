"""Angebotsfinder: FastAPI-App, Anmeldung, Pruefschleife, Endpunkte."""

from __future__ import annotations

import asyncio
import html
import logging
import platform
import posixpath
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import fastapi
import starlette
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from backend import auth, settings
from backend.models import PushEndpointRequest, PushSubscribeRequest
from backend.monitor import AngeboteMonitor, CheckResult, ServiceStatus
from backend.push import PushSender, PushStore, Sender

APP_VERSION = "1.0.0"

# Welche Bibliotheken das Image mitbringt. Daran ist nach einem Update
# erkennbar, ob wirklich neu gebaut wurde.
RUNTIME = (f"python {platform.python_version()}, fastapi {fastapi.__version__}, "
           f"starlette {starlette.__version__}")

FRONTEND = settings.FRONTEND
CHECK_INTERVAL = 60.0
CHECK_TIMEOUT = 30.0
"""Ohne Zeitlimit konnte bei Sentinel eine haengende Abfrage die Schleife fuer
immer anhalten. Die Seite zeigte dann weiter den letzten Stand."""
FORM_BODY_LIMIT = 4096
FAILED_LOGIN_DELAY = 1.0
MAX_WAITING_LOGINS = 8
"""So viele Anmeldungen duerfen auf die Passwort-Pruefung warten. Mehr gleichzeitig
gibt es nur bei einem Angriff, dann lieber gleich ablehnen als den Besitzer
minutenlang in die Schlange zu stellen."""

# Ohne Anmeldung erreichbar. Der Browser holt Service Worker, Manifest und
# Symbole auch ohne Session. Sie enthalten nichts Schuetzenswertes.
PUBLIC_PATHS = {"/login", "/setup", "/static/style.css", "/sw.js", "/manifest.webmanifest"}
PUBLIC_PREFIXES = ("/static/icons/",)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    # Alles kommt vom eigenen Server, kein Inline-Skript, keine fremden Schriften.
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; object-src 'none'; "
                                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
}

logging.basicConfig(
    level=settings.log_level(),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("angebotsfinder")
# Der Einrichtungscode muss im Protokoll stehen, auch wenn jemand die
# Protokollstufe auf ERROR stellt. Sonst liesse sich nichts einrichten.
setup_logger = logging.getLogger("angebotsfinder.einrichtung")
setup_logger.setLevel(logging.WARNING)


def is_public(path: str) -> bool:
    """Ohne Anmeldung erlaubt? Nur normalisierte Pfade: Bei /static/icons/../app.js
    passt der Anfang, StaticFiles liefert aber app.js aus (Befund der Review)."""
    if path in PUBLIC_PATHS:
        return True
    return (path.startswith(PUBLIC_PREFIXES) and "\\" not in path
            and posixpath.normpath(path) == path)


def cross_site(request: Request) -> bool:
    """Schreibende Anfrage von einer anderen Seite? SameSite=Lax schuetzt nicht vor
    Nachbar-Subdomains derselben Domain (grafana.example.de -> angebote.example.de).
    Browser melden die Herkunft in Sec-Fetch-Site. Fehlt der Kopf (alter Browser,
    curl), wird nicht blockiert."""
    if request.method in SAFE_METHODS:
        return False
    site = request.headers.get("sec-fetch-site")
    return site is not None and site not in ("same-origin", "none")


# --- Seiten ------------------------------------------------------------------

def page(name: str, status_code: int = 200, error: str = "", username: str = "") -> HTMLResponse:
    """HTML-Seite mit eingesetzter Version, Fehlermeldung und Benutzername.

    __VERSION__ steht in den Adressen von CSS und JS (?v=...). Nach einem Update
    ist die Adresse neu, kein Browser nimmt dann noch eine alte Datei aus dem
    Cache. Der Benutzername kommt als Letztes, damit ein Name wie "__FEHLER__"
    nichts ersetzt.
    """
    text = (FRONTEND / name).read_text(encoding="utf-8").replace("__VERSION__", APP_VERSION)
    box = f'<div class="form-error" role="alert">{html.escape(error)}</div>' if error else ""
    text = text.replace("__FEHLER__", box).replace("__BENUTZER__", html.escape(username, quote=True))
    return HTMLResponse(text, status_code=status_code, headers={"Cache-Control": "no-cache"})


async def read_form(request: Request, limit: int = FORM_BODY_LIMIT) -> dict[str, str]:
    """Formular mit hoechstens `limit` Bytes. Anmeldung und Einrichtung sind ohne
    Session aus dem Internet erreichbar."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="Anfrage zu groß")
        chunks.append(chunk)
    form = parse_qs(b"".join(chunks).decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] for key, values in form.items()}


def client_host(request: Request) -> str:
    return request.client.host if request.client else "unbekannt"


def set_session_cookie(response: RedirectResponse, request: Request, token: str) -> None:
    # Secure, sobald die Seite ueber HTTPS kommt. Hinter einem Reverse Proxy
    # meldet der das per X-Forwarded-Proto (uvicorn --forwarded-allow-ips).
    # Ueber http:// im Heimnetz klappt die Anmeldung so weiterhin.
    response.set_cookie(key=auth.COOKIE_NAME, value=token, max_age=auth.SESSION_MAX_AGE,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")


# --- Zugang ------------------------------------------------------------------

class Access:
    """Zugang, Sessions und Einrichtungscode."""

    def __init__(self, data_dir: Path):
        self.store = auth.CredentialStore(data_dir)
        self.sessions = auth.Sessions()
        self.throttle = auth.LoginThrottle()
        self.setup_code: str | None = None
        # Argon2id kostet Speicher und Zeit. Immer nur eine Rechnung auf einmal,
        # auch wenn jemand die Anmeldung mit vielen Anfragen gleichzeitig flutet.
        self._hashing = asyncio.Semaphore(1)
        self._waiting = 0
        self._was_configured: bool | None = None
        self._logged_problem: str | None = None

    def session_valid(self, token: str | None) -> bool:
        if not self.sessions.valid(token):
            return False
        if not self.store.path.exists():
            # Zugang wurde im laufenden Betrieb zurueckgesetzt: Alle Sessions
            # enden sofort, nicht erst beim naechsten Aufruf der Anmeldung.
            self.refresh()
            return False
        return True

    def refresh(self) -> None:
        """Zugang von der Platte lesen. Fehlt er, gibt es einen Einrichtungscode."""
        self.store.load()
        if self.store.problem and self.store.problem != self._logged_problem:
            logger.error("%s. Der Zugang muss neu eingerichtet werden.", self.store.problem)
        self._logged_problem = self.store.problem
        configured = self.store.configured
        if configured:
            self.setup_code = None
        else:
            if self._was_configured:
                # Zugang wurde geloescht (zuruecksetzen): alte Sessions gelten nicht mehr.
                self.sessions.clear()
            if self.setup_code is None:
                self.setup_code = auth.new_setup_code()
                log_setup_code(self.setup_code)
        self._was_configured = configured

    async def check_login(self, username: str, password: str) -> bool | None:
        """None: Gerade warten zu viele Anmeldungen, die Anfrage wird abgelehnt."""
        if self._waiting >= MAX_WAITING_LOGINS:
            return None
        self._waiting += 1
        try:
            async with self._hashing:
                return await asyncio.to_thread(self.store.verify, username, password)
        finally:
            self._waiting -= 1

    async def create(self, username: str, password: str) -> bool:
        """False, wenn inzwischen schon ein Zugang angelegt wurde."""
        async with self._hashing:
            if self.store.configured:
                return False
            await asyncio.to_thread(self.store.save, username, password, datetime.now())
        self.setup_code = None
        self._was_configured = True
        return True


def log_setup_code(code: str) -> None:
    for line in ("=" * 64,
                 "Der Angebotsfinder ist noch nicht eingerichtet.",
                 f"Einrichtungscode: {code}",
                 "Die Seite im Browser öffnen, Benutzer und Passwort festlegen",
                 "und diesen Code eingeben. Er gilt bis zur Einrichtung.",
                 "=" * 64):
        setup_logger.warning(line)


# --- Pruefschleife -----------------------------------------------------------

class Runner:
    """Jede Minute pruefen, Ergebnis ablegen, neue Angebote per Push melden."""

    def __init__(self, monitor: AngeboteMonitor, interval: float = CHECK_INTERVAL,
                 timeout: float = CHECK_TIMEOUT):
        self.monitor = monitor
        self.interval = interval
        self.timeout = timeout
        self.sender: PushSender | None = None
        self.result = CheckResult(status=ServiceStatus.UNKNOWN, message="Erste Prüfung läuft …")
        # Schleife und Knopf "Jetzt pruefen" nacheinander. Das Warten hier zaehlt
        # nicht zum Zeitlimit, sonst brach der Knopf ab, waehrend die Schleife
        # pruefte, und ueberschrieb ihr Ergebnis mit einem Fehler (Befund der Review).
        self._lock = asyncio.Lock()

    async def run_once(self) -> CheckResult:
        async with self._lock:
            try:
                result = await asyncio.wait_for(self.monitor.check(), timeout=self.timeout)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                result = CheckResult(status=ServiceStatus.ERROR,
                                     message=f"Prüfung nach {self.timeout:.0f} s abgebrochen")
            except Exception as exc:
                logger.exception("Prüfung fehlgeschlagen")
                result = CheckResult(status=ServiceStatus.ERROR,
                                     message=f"Programmfehler: {type(exc).__name__}: {exc}")
            if result.status != self.result.status:
                # Nur Zustandswechsel, sonst stuende jede Minute eine Zeile im Protokoll.
                logger.info("Stand: %s - %s", result.status.value, result.message)
            self.result = result
        for payload in result.notifications:
            if self.sender is None:
                logger.warning("Neue Angebote, aber Push ist nicht verfügbar: %s", payload["title"])
            else:
                self.sender.send_in_background(payload)
        return result

    async def loop(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval)


class PushRuntime:
    """Was beim Start fuer Push eingerichtet wurde - oder warum nicht."""

    def __init__(self):
        self.store: PushStore | None = None
        self.sender: PushSender | None = None
        self.unavailable: str | None = "noch nicht gestartet"

    def require(self) -> tuple[PushStore, PushSender]:
        if self.unavailable or self.store is None or self.sender is None:
            raise HTTPException(status_code=503,
                                detail=f"Push ist nicht verfügbar: {self.unavailable}")
        return self.store, self.sender


# --- App ---------------------------------------------------------------------

def create_app(
    data_dir: Path | None = None,
    watchlist: Path | None = None,
    watchlist_label: str | None = None,
    start_loop: bool = True,
    client: Any = None,
    logo: Any = None,
    webpush: Sender | None = None,
) -> FastAPI:
    data_dir = Path(data_dir or settings.data_dir())
    access = Access(data_dir)
    monitor = AngeboteMonitor(watchlist or settings.watchlist_path(),
                              label=watchlist_label or settings.watchlist_label(),
                              client=client, logo=logo)
    runner = Runner(monitor)
    push = PushRuntime()

    def setup_push() -> None:
        store = PushStore(data_dir)
        try:
            contact = settings.push_contact()
            store.open()
        except Exception as exc:
            # Volume fehlt, ist nicht beschreibbar, oder die Kontaktadresse taugt
            # nicht: Push aus, der Rest laeuft.
            push.unavailable = str(exc)
            logger.error("Push ist nicht verfügbar: %s", exc)
            return
        push.store = store
        push.sender = PushSender(store, contact, webpush)
        push.unavailable = None
        runner.sender = push.sender
        logger.info("Push bereit: %d Gerät(e) angemeldet", len(store.devices()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        auth.selftest()
        logger.info("Angebotsfinder %s (%s), Merkliste %s", APP_VERSION, RUNTIME,
                    monitor.watchlist_label)
        monitor.use_data_dir(data_dir)
        setup_push()
        access.refresh()
        task = asyncio.create_task(runner.loop()) if start_loop else None
        yield
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if push.sender is not None:
            await push.sender.drain()

    app = FastAPI(title="Angebotsfinder", version=APP_VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.access = access
    app.state.runner = runner
    app.state.push = push

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            token = request.cookies.get(auth.COOKIE_NAME)
            if cross_site(request):
                logger.warning("%s %s von fremder Seite abgelehnt (%s)", request.method, path,
                               client_host(request))
                response = JSONResponse(status_code=403, content={"detail": "Fremde Herkunft"})
            elif is_public(path) or access.session_valid(token):
                response = await call_next(request)
            elif path.startswith("/api/"):
                response = JSONResponse(status_code=401, content={"detail": "Nicht angemeldet"})
            else:
                response = RedirectResponse(url="/login", status_code=303)
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            if path.startswith("/static/"):
                # Ohne Cache-Control schaetzt der Browser selbst, wie lange er eine
                # Datei ungefragt weiterverwendet. no-cache heisst: immer nachfragen.
                response.headers.setdefault("Cache-Control", "no-cache")
            return response

    app.add_middleware(AuthMiddleware)
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    # --- Anmeldung und Einrichtung -------------------------------------------

    def blocked_text(wait: float) -> str:
        minutes = max(1, round(wait / 60))
        return (f"Zu viele Fehlversuche. Bitte in {minutes} Minute{'n' if minutes > 1 else ''} "
                f"erneut versuchen.")

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        access.refresh()
        if not access.store.configured:
            return RedirectResponse(url="/setup", status_code=303)
        error = ""
        if "gesperrt" in request.query_params:
            error = blocked_text(access.throttle.retry_after(client_host(request)))
        elif "belegt" in request.query_params:
            error = "Gerade laufen sehr viele Anmeldungen. Bitte kurz warten und erneut versuchen."
        elif "fehler" in request.query_params:
            error = "Benutzername oder Passwort falsch."
        return page("login.html", error=error)

    @app.post("/login", include_in_schema=False)
    async def login(request: Request):
        access.refresh()
        if not access.store.configured:
            return RedirectResponse(url="/setup", status_code=303)
        form = await read_form(request)
        username = form.get("username", "")
        ip = client_host(request)
        # Pruefen und Zaehlen ohne await dazwischen: Gleichzeitige Anfragen sehen
        # so jeden Versuch der anderen.
        if access.throttle.retry_after(ip):
            logger.warning("Anmeldung von %s gesperrt: zu viele Fehlversuche", ip)
            return RedirectResponse(url="/login?gesperrt=1", status_code=303)
        access.throttle.attempt(ip)
        ok = await access.check_login(username, form.get("password", ""))
        if ok is None:
            logger.warning("Anmeldung von %s abgelehnt: zu viele gleichzeitig", ip)
            return RedirectResponse(url="/login?belegt=1", status_code=303)
        if not ok:
            logger.warning("Anmeldung fehlgeschlagen für %r von %s", username[:64], ip)
            # Bremst automatisiertes Durchprobieren zusaetzlich zur Sperre je IP.
            await asyncio.sleep(FAILED_LOGIN_DELAY)
            return RedirectResponse(url="/login?fehler=1", status_code=303)
        access.throttle.success(ip)
        logger.info("Angemeldet von %s", ip)
        response = RedirectResponse(url="/", status_code=303)
        set_session_cookie(response, request, access.sessions.create())
        return response

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request):
        access.sessions.delete(request.cookies.get(auth.COOKIE_NAME))
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(key=auth.COOKIE_NAME)
        return response

    @app.get("/setup", include_in_schema=False)
    async def setup_page():
        access.refresh()
        if access.store.configured:
            return RedirectResponse(url="/login", status_code=303)
        return page("setup.html")

    @app.post("/setup", include_in_schema=False)
    async def setup(request: Request):
        access.refresh()
        if access.store.configured:
            return RedirectResponse(url="/login", status_code=303)
        form = await read_form(request)
        username = form.get("username", "")
        ip = client_host(request)
        wait = access.throttle.retry_after(ip)
        if wait:
            logger.warning("Einrichtung von %s gesperrt: zu viele Fehlversuche", ip)
            return page("setup.html", 429, username=username, error=blocked_text(wait))
        if not auth.setup_code_matches(form.get("code", ""), access.setup_code):
            access.throttle.attempt(ip)
            logger.warning("Einrichtung mit falschem Code von %s", ip)
            await asyncio.sleep(FAILED_LOGIN_DELAY)
            return page("setup.html", 400, username=username,
                        error="Der Einrichtungscode stimmt nicht. Er steht im Protokoll des "
                              "Containers, in der Zeile „Einrichtungscode“.")
        try:
            name = auth.validate_new_credentials(username, form.get("password", ""),
                                                 form.get("password2", ""))
        except ValueError as exc:
            return page("setup.html", 400, username=username, error=str(exc))
        try:
            created = await access.create(name, form.get("password", ""))
        except OSError as exc:
            logger.error("Zugang lässt sich nicht speichern: %s", exc)
            return page("setup.html", 500, username=username,
                        error=f"Der Zugang ließ sich nicht speichern ({exc}). Das "
                              f"Datenverzeichnis des Containers ist nicht beschreibbar.")
        if not created:
            return RedirectResponse(url="/login", status_code=303)
        access.throttle.success(ip)
        logger.info("Zugang eingerichtet für Benutzer %r", name)
        response = RedirectResponse(url="/", status_code=303)
        set_session_cookie(response, request, access.sessions.create())
        return response

    # --- Seiten --------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        return page("index.html")

    @app.get("/push", include_in_schema=False)
    async def push_page():
        return page("push.html")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        # Von der Wurzel ausgeliefert, damit sein Geltungsbereich die ganze Seite
        # umfasst. Unter /static/ gaelte er nur dort.
        return FileResponse(FRONTEND / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        # Fuer das iPhone: Push geht dort nur als Home-Bildschirm-App.
        return FileResponse(FRONTEND / "manifest.webmanifest",
                            media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})

    # --- Angebote ------------------------------------------------------------

    def offers_response() -> dict[str, Any]:
        result = runner.result
        return {
            "version": APP_VERSION,
            "status": result.status.value,
            "message": result.message,
            "checked_at": result.checked_at.isoformat(timespec="seconds"),
            "subtitle": result.details.get("subtitle", ""),
            "push_devices": len(push.store.devices()) if push.store else 0,
            **monitor.get_details(),
        }

    @app.get("/api/angebote")
    async def get_offers():
        return offers_response()

    @app.post("/api/angebote/pruefen")
    async def run_now():
        """Knopf "Jetzt pruefen", hoechstens einmal pro Minute."""
        wait = monitor.request_run(monitor.clock())
        if wait > 0:
            raise HTTPException(status_code=429,
                                detail=f"Gerade erst geprüft. Bitte noch {wait:.0f} s warten.")
        await runner.run_once()
        return offers_response()

    # --- Push ----------------------------------------------------------------

    @app.get("/api/push/config")
    async def push_config():
        return {
            "available": push.unavailable is None,
            "reason": push.unavailable,
            "public_key": push.store.public_key if push.store and not push.unavailable else None,
        }

    @app.get("/api/push/devices")
    async def push_devices():
        store, _ = push.require()
        return [{key: d.get(key) for key in
                 ("id", "label", "created_at", "last_success_at", "last_error")}
                for d in store.devices()]

    @app.post("/api/push/subscribe")
    async def push_subscribe(body: PushSubscribeRequest):
        store, _ = push.require()
        try:
            device = store.add(body.subscription, body.label, datetime.now())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        logger.info("Push-Gerät angemeldet: %s (%s)", device["id"], device["label"])
        return {"id": device["id"], "label": device["label"]}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(body: PushEndpointRequest):
        store, _ = push.require()
        return {"removed": store.remove(endpoint=body.endpoint)}

    @app.post("/api/push/devices/{device_id}/remove")
    async def push_remove_device(device_id: str):
        store, _ = push.require()
        return {"removed": store.remove(id=device_id)}

    @app.post("/api/push/test")
    async def push_test(body: PushEndpointRequest):
        store, sender = push.require()
        if not body.endpoint or not any(d["endpoint"] == body.endpoint for d in store.devices()):
            raise HTTPException(status_code=404, detail="Gerät nicht angemeldet")
        summary = await sender.send(
            {"title": "Angebotsfinder: Test",
             "body": "Push-Benachrichtigungen kommen auf diesem Gerät an.",
             "tag": "angebotsfinder-test", "url": "/push", "kind": "test"},
            urgency="high", only_endpoint=body.endpoint,
        )
        return {"sent": summary.sent, "failed": summary.failed,
                "removed": summary.removed, "errors": summary.errors}

    return app


app = create_app()
