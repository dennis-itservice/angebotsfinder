"""Angebote aus den Prospekten der Region: Abfrage bei marktguru.de.

marktguru sammelt die Prospekte fast aller Ketten (EDEKA, REWE, Lidl, ALDI,
Kaufland, Netto, ...) und bietet eine Suche nach Produkt und Postleitzahl. Das
ist die Schnittstelle, die die eigene Webseite benutzt - nicht offiziell
dokumentiert. Die beiden Schluessel dafuer stehen oeffentlich im Quelltext der
Startseite und werden von dort gelesen, nicht fest eingetragen. Aendert
marktguru die Seite, schlaegt der Abruf mit einer klaren Meldung fehl, statt
still "keine Angebote" zu liefern.

EDEKA direkt geht nicht: edeka.de blockt Abrufe ausserhalb eines Browsers
(Akamai, HTTP 403). Getestet am 10.10.2026.

Uebernommen aus Sentinel 2.3.1 (Prospekt-Check).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import aiohttp

HOME_URL = "https://www.marktguru.de/"
DEFAULT_API_HOST = "api.marktguru.de"
OFFER_URL = "https://www.marktguru.de/offers/{id}"
USER_AGENT = "Angebotsfinder (private Angebotsabfrage)"

# <script type="application/json">{"config":{"apiHostAddress":..,"apiKey":..,"clientKey":..}}
CONFIG_SCRIPT = re.compile(r'<script type="application/json">\s*(\{"config".*?)</script>', re.S)

# Mehr Suchen gleichzeitig waeren unhoeflich - es sind ohnehin nur wenige.
PARALLEL_SEARCHES = 3


class MarktguruError(Exception):
    """Abruf fehlgeschlagen. Die Meldung erscheint auf der Seite."""


def parse_keys(html: str) -> tuple[str, str, str]:
    """(api_host, api_key, client_key) aus dem Quelltext der Startseite."""
    match = CONFIG_SCRIPT.search(html)
    if not match:
        raise MarktguruError("marktguru: Schlüssel auf der Startseite nicht gefunden. "
                             "Seite geändert?")
    try:
        config = json.loads(match.group(1))["config"]
        return (str(config.get("apiHostAddress") or DEFAULT_API_HOST),
                str(config["apiKey"]), str(config["clientKey"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise MarktguruError(f"marktguru: Konfiguration auf der Startseite unlesbar ({exc})") from exc


class MarktguruClient:
    def __init__(self, home_url: str = HOME_URL, api_base: str | None = None,
                 timeout: float = 10.0):
        self.home_url = home_url
        # Tests setzen einen lokalen Server. Sonst kommt der Host aus der Startseite.
        self.api_base = api_base
        self.timeout = timeout
        self.home_loads = 0
        self._keys: tuple[str, str, str] | None = None
        self._lock = asyncio.Lock()

    async def search_many(self, queries: list[str], zip_code: str,
                          limit: int = 200) -> dict[str, list[dict[str, Any]]]:
        """Rohdaten der Treffer je Suchbegriff, alle in einer Sitzung."""
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        limiter = asyncio.Semaphore(PARALLEL_SEARCHES)
        try:
            async with aiohttp.ClientSession(timeout=timeout,
                                             headers={"User-Agent": USER_AGENT}) as session:
                async def one(query: str) -> list[dict[str, Any]]:
                    async with limiter:
                        return await self._search(session, query, zip_code, limit)

                results = await asyncio.gather(*(one(q) for q in queries))
        except MarktguruError:
            raise
        except TimeoutError as exc:
            raise MarktguruError("marktguru antwortet nicht (Zeitlimit)") from exc
        except aiohttp.ClientError as exc:
            raise MarktguruError(f"marktguru nicht erreichbar: {exc}") from exc
        return dict(zip(queries, results))

    async def _keys_for(self, session: aiohttp.ClientSession,
                        rejected: tuple[str, str, str] | None) -> tuple[str, str, str]:
        """Schluessel aus dem Zwischenspeicher, sonst von der Startseite.

        rejected: diese Schluessel hat die API eben abgelehnt - dann neu holen,
        ausser eine parallele Suche hat das schon getan.
        """
        async with self._lock:
            if self._keys is None or self._keys == rejected:
                async with session.get(self.home_url) as resp:
                    if resp.status != 200:
                        raise MarktguruError(f"marktguru: Startseite antwortet mit HTTP {resp.status}")
                    html = await resp.text()
                self.home_loads += 1
                self._keys = parse_keys(html)
            return self._keys

    async def _search(self, session: aiohttp.ClientSession, query: str, zip_code: str,
                      limit: int) -> list[dict[str, Any]]:
        rejected = None
        for _ in range(2):
            keys = await self._keys_for(session, rejected)
            host, api_key, client_key = keys
            base = self.api_base or f"https://{host}/api/v1"
            params = {"as": "web", "q": query, "zipCode": zip_code,
                      "limit": str(limit), "offset": "0"}
            headers = {"x-apikey": api_key, "x-clientkey": client_key,
                       "Accept": "application/json"}
            async with session.get(f"{base}/offers/search", params=params,
                                   headers=headers) as resp:
                if resp.status in (401, 403) and rejected is None:
                    # Schluessel gewechselt: einmal neu von der Startseite holen.
                    rejected = keys
                    continue
                if resp.status != 200:
                    raise MarktguruError(f"marktguru: HTTP {resp.status} bei der Suche "
                                         f"nach {query!r}")
                try:
                    data = await resp.json(content_type=None)
                except ValueError as exc:
                    raise MarktguruError(f"marktguru: keine gültige Antwort bei der Suche "
                                         f"nach {query!r}") from exc
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise MarktguruError("marktguru: Antwort ohne Trefferliste. Format geändert?")
            return results
        raise MarktguruError("marktguru: Schlüssel werden abgelehnt (HTTP 401)")
