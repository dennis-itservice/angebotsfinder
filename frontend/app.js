// Angebotsseite: Treffer, Merkliste, Einstellungen, Knopf "Jetzt pruefen".

const POLL_INTERVAL = 60_000;
const container = document.getElementById("content");
const subtitle = document.getElementById("subtitle");
const lastUpdate = document.getElementById("last-update");

// Ergebnis des letzten Klicks auf "Jetzt pruefen" - bleibt ueber das
// Neuzeichnen der Seite hinweg stehen.
let runNote = "";

async function load() {
    try {
        render(await fetchJson("/api/angebote"));
        lastUpdate.textContent = `Stand: ${new Date().toLocaleTimeString("de-DE")}`;
    } catch (err) {
        console.error("Fehler beim Laden:", err);
        lastUpdate.textContent = `Aktualisierung fehlgeschlagen: ${err.message}`;
    }
}

function summaryItem(value, label, cls = "") {
    return `
        <div class="summary-item">
            <div class="summary-value ${cls}">${esc(value)}</div>
            <div class="summary-label">${esc(label)}</div>
        </div>
    `;
}

// Blau = Treffer, grau = nichts gefunden oder noch nicht geprueft,
// gelb = Merkliste oder Abruf hat ein Problem.
function statusClass(data) {
    if (data.status === "warning") return "warning";
    if (data.status === "error") return "error";
    if (data.status === "ok" && data.summary.hits > 0) return "info";
    return "unknown";
}

function offerRow(m) {
    const extras = [];
    if (m.reference_price !== null && m.unit) extras.push(`${formatPrice(m.reference_price)}/${m.unit}`);
    if (m.old_price !== null) extras.push(`statt ${formatPrice(m.old_price)}`);
    if (m.app_only) extras.push("nur mit App");
    if (m.regional) extras.push("nicht in allen Märkten");
    const when = m.is_current
        ? `bis ${esc(formatDay(m.valid_to))} <span class="offer-badge current">läuft</span>`
        : `${esc(formatDay(m.valid_from))}–${esc(formatDay(m.valid_to))} <span class="offer-badge">demnächst</span>`;
    const description = m.description.length > 160 ? `${m.description.slice(0, 157)}…` : m.description;
    const link = m.url
        ? ` <a href="${esc(m.url)}" target="_blank" rel="noopener noreferrer">${m.source === "logo" ? "Angebote bei LOGO" : "Prospekt"}</a>`
        : "";
    return `<div class="offer-row">
        <div class="offer-main">
            <div class="offer-product">${esc(m.product)}</div>
            <div><strong>${esc(m.shop)}</strong> <span class="nowrap">· ${when}</span></div>
            <div class="muted small" title="${esc(m.description)}">${esc(description)}${link}</div>
        </div>
        <div class="offer-price">
            <strong>${esc(formatPrice(m.price))}</strong>
            ${m.condition ? `<div class="small">${esc(m.condition)}</div>` : ""}
            ${extras.length ? `<div class="muted small">${esc(extras.join(" · "))}</div>` : ""}
        </div>
    </div>`;
}

function render(data) {
    const s = data.summary;
    const settings = data.settings;
    subtitle.textContent = data.subtitle || "Angebote aus den Prospekten";

    // Als Liste statt Tabelle: am Handy waeren Zeitraum und Hinweis sonst
    // seitlich aus dem Bild gerutscht.
    const offerRows = data.matches.length
        ? data.matches.map(offerRow).join("")
        : '<div class="offer-row muted">Zurzeit kein Treffer.</div>';

    const productRows = data.products.length
        ? data.products.map(p => `<tr>
            <td>${esc(p.label)}</td>
            <td>${p.hits ? `<span class="offer-badge current">${esc(p.hits)} Treffer</span>` : '<span class="muted">kein Angebot</span>'}</td>
        </tr>`).join("")
        : '<tr><td colspan="2" class="muted">Merkliste fehlt oder ist leer.</td></tr>';

    const settingRows = settings ? [
        ["Postleitzahl", settings.zip_code],
        ["Geschäfte", settings.shops.length ? settings.shops.join(", ") : "alle"],
        ["Prüfung", settings.schedule],
        ["Vorschau", `Angebote, die in den nächsten ${settings.days_ahead} Tagen beginnen`],
        ["Push", settings.push ? "bei neuen Treffern, je Angebot einmal" : "aus"],
    ] : [];
    settingRows.push(["Merkliste", `${data.watchlist_file}. Änderungen wirken bei der nächsten Prüfung, spätestens nach einer Minute. Ein Neustart ist nicht nötig.`]);
    if (data.other_shops.length) {
        settingRows.push(["Weitere Geschäfte", `${data.other_shops.join(", ")} (aus den Suchergebnissen, so unter shops: eintragen)`]);
    }
    settingRows.push(["Quelle", data.source]);

    const pushHint = data.push_devices === 0
        ? '<div class="hint">Tipp: Über die Glocke oben rechts kommen neue Angebote als Push-Nachricht aufs Handy.</div>'
        : "";

    container.innerHTML = `
        <div class="status-line ${statusClass(data)}">${esc(data.message)}</div>
        ${pushHint}
        <div class="summary-card">
            ${summaryItem(s.hits, "Treffer", s.hits > 0 ? "info" : "")}
            ${summaryItem(s.current, "Laufen jetzt")}
            ${summaryItem(s.upcoming, "Demnächst")}
            ${summaryItem(formatStamp(s.last_run), "Geprüft", "small")}
            ${summaryItem(formatStamp(s.next_run), "Nächste Prüfung", "small")}
        </div>
        <div class="actions">
            <button type="button" class="action-btn" id="btn-run">${icon("refresh")} Jetzt prüfen</button>
            <span class="muted small" id="run-note">${esc(runNote)}</span>
        </div>
        <h2 class="section-title">Treffer</h2>
        <div class="offer-list">${offerRows}</div>
        <h2 class="section-title">Merkliste</h2>
        <div class="table-wrap">
            <table class="list-table">
                <thead><tr><th>Produkt</th><th>Stand</th></tr></thead>
                <tbody>${productRows}</tbody>
            </table>
        </div>
        <h2 class="section-title">Einstellungen</h2>
        <div class="offer-list">
            ${settingRows.map(([k, v]) => `<div class="info-row"><span class="muted">${esc(k)}</span><span>${esc(v)}</span></div>`).join("")}
        </div>
    `;
}

// Ein Handler fuer den Knopf, auch wenn die Seite neu gezeichnet wird.
container.addEventListener("click", async event => {
    const button = event.target.closest("#btn-run");
    if (!button) return;
    button.disabled = true;
    const note = document.getElementById("run-note");
    if (note) note.textContent = "Prüfe …";
    try {
        const result = await postJson("/api/angebote/pruefen");
        runNote = `Geprüft um ${new Date().toLocaleTimeString("de-DE")}: ${result.message}`;
    } catch (err) {
        runNote = err.message;
    }
    await load();
});

load();
setInterval(load, POLL_INTERVAL);
