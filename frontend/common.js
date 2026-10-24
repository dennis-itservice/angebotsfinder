// Gemeinsame Helfer fuer alle Seiten.

// Alles, was aus der API kommt, geht durch esc(), bevor es per innerHTML auf
// die Seite gelangt. Produktnamen, Beschreibungen und Fehlermeldungen stammen
// aus fremden Quellen (marktguru, LOGO).
function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// Symbole als SVG statt Icon-Schrift von Google: keine Anfrage an fremde Server.
// Pfade aus den Material Icons (Google, Apache License 2.0).
const ICONS = {
    refresh: "M17.65 6.35A7.958 7.958 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z",
    delete: "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z",
};

function icon(name) {
    return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${ICONS[name]}"/></svg>`;
}

function formatTime(iso) {
    if (!iso) return "–";
    return new Date(iso).toLocaleString("de-DE");
}

const WEEKDAYS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

// "Sa 17.10." - Zeitangaben ohne Zeitzone kommen vom Server in lokaler Zeit.
function formatDay(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    const dd = String(d.getDate()).padStart(2, "0");
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    return `${WEEKDAYS[d.getDay()]} ${dd}.${mm}.`;
}

// "So 11.10. 08:00"
function formatStamp(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    return `${formatDay(iso)} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function formatPrice(value) {
    if (value === null || value === undefined) return "–";
    return `${Number(value).toFixed(2).replace(".", ",")} €`;
}

// Abgelaufene Session (eine Woche oder Neustart des Containers): zur Anmeldung,
// statt endlos "HTTP 401" anzuzeigen.
async function fetchJson(url) {
    const res = await fetch(url);
    if (res.status === 401) {
        window.location.href = "/login";
        throw new Error("Nicht angemeldet");
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
}

async function postJson(url, body) {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
    });
    if (res.status === 401) {
        window.location.href = "/login";
        throw new Error("Nicht angemeldet");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
}
