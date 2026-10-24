// Push-Benachrichtigungen fuer dieses Geraet ein- und ausschalten.

const statusEl = document.getElementById("push-status");
const rulesEl = document.getElementById("push-rules");
const rowsEl = document.getElementById("device-rows");
const lastUpdate = document.getElementById("last-update");
const btnEnable = document.getElementById("btn-enable");
const btnDisable = document.getElementById("btn-disable");
const btnTest = document.getElementById("btn-test");

const RULES = "Neue Angebote kommen als Push-Nachricht, je Angebot einmal, zu den Prüfterminen aus der Merkliste. " +
    "Die Nachricht nennt Geschäft, Preis und Zeitraum. Antippen öffnet die Angebotsseite.";

let config = null;
let registration = null;
let subscription = null;

function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = `status-line ${cls}`;
}

function urlBase64ToUint8Array(base64) {
    const padded = (base64 + "=".repeat((4 - base64.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(padded), c => c.charCodeAt(0));
}

function sameBytes(a, b) {
    return a.length === b.length && a.every((v, i) => v === b[i]);
}

// Dieselbe Kennung wie auf dem Server: die ersten 12 Hex-Zeichen von SHA-256(endpoint).
async function deviceId(endpoint) {
    const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
    return [...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 12);
}

function isIos() {
    return /iPhone|iPad|iPod/.test(navigator.userAgent)
        || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function deviceLabel() {
    const ua = navigator.userAgent;
    let browser = "Browser";
    if (/Edg\//.test(ua)) browser = "Edge";
    else if (/SamsungBrowser/.test(ua)) browser = "Samsung Internet";
    else if (/Firefox\//.test(ua)) browser = "Firefox";
    else if (/Chrome\//.test(ua)) browser = "Chrome";
    else if (/Safari\//.test(ua)) browser = "Safari";

    let system = "";
    if (/Android/.test(ua)) system = "Android";
    else if (isIos()) system = "iOS";
    else if (/Windows/.test(ua)) system = "Windows";
    else if (/Mac OS X/.test(ua)) system = "macOS";
    else if (/Linux/.test(ua)) system = "Linux";
    return system ? `${browser} auf ${system}` : browser;
}

function renderDevices(devices, myId) {
    if (!devices.length) {
        rowsEl.innerHTML = '<tr><td colspan="5" class="muted">Noch kein Gerät angemeldet.</td></tr>';
        return;
    }
    rowsEl.innerHTML = devices.map(d => `
        <tr class="${d.last_error ? "problem" : ""}">
            <td>${esc(d.label)}${d.id === myId ? ' <span class="muted">(dieses Gerät)</span>' : ""}</td>
            <td class="nowrap">${esc(formatTime(d.created_at))}</td>
            <td class="nowrap">${esc(d.last_success_at ? formatTime(d.last_success_at) : "–")}</td>
            <td class="muted">${esc(d.last_error || "–")}</td>
            <td><button type="button" class="link-btn" data-remove="${esc(d.id)}" title="Gerät entfernen" aria-label="Gerät entfernen">
                ${icon("delete")}</button></td>
        </tr>`).join("");
}

async function refresh() {
    let devices = [];
    let myId = null;
    if (config && config.available) {
        devices = await fetchJson("/api/push/devices");
    }
    if (subscription) myId = await deviceId(subscription.endpoint);
    const registered = Boolean(myId && devices.some(d => d.id === myId));

    if (registration) {
        if (Notification.permission === "denied") {
            setStatus("Benachrichtigungen sind für diese Seite blockiert. Bitte in den Website-Einstellungen des Browsers erlauben.", "error");
        } else if (registered) {
            setStatus("Dieses Gerät bekommt Push-Nachrichten.", "ok");
        } else {
            setStatus("Dieses Gerät ist noch nicht angemeldet.", "unknown");
        }
        btnEnable.hidden = registered;
        btnTest.hidden = !registered;
        btnDisable.hidden = !subscription;
    }
    renderDevices(devices, myId);
    lastUpdate.textContent = `Stand: ${new Date().toLocaleTimeString("de-DE")}`;
}

async function busy(action) {
    const buttons = [btnEnable, btnDisable, btnTest];
    buttons.forEach(b => { b.disabled = true; });
    try {
        await action();
    } catch (err) {
        console.error(err);
        setStatus(`Fehler: ${err.message}`, "error");
    } finally {
        buttons.forEach(b => { b.disabled = false; });
    }
}

btnEnable.addEventListener("click", () => busy(async () => {
    const erlaubnis = await Notification.requestPermission();
    if (erlaubnis !== "granted") {
        setStatus("Ohne Erlaubnis für Benachrichtigungen geht es nicht.", "warning");
        return;
    }
    if (!subscription) {
        subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(config.public_key),
        });
    }
    await postJson("/api/push/subscribe", { subscription: subscription.toJSON(), label: deviceLabel() });
    await refresh();
}));

btnDisable.addEventListener("click", () => busy(async () => {
    const endpoint = subscription.endpoint;
    await subscription.unsubscribe();
    subscription = null;
    await postJson("/api/push/unsubscribe", { endpoint });
    await refresh();
}));

btnTest.addEventListener("click", () => busy(async () => {
    const result = await postJson("/api/push/test", { endpoint: subscription.endpoint });
    if (result.sent) {
        setStatus("Test verschickt. Die Nachricht sollte gleich erscheinen.", "ok");
    } else if (result.removed) {
        setStatus("Der Push-Dienst kennt dieses Gerät nicht mehr. Bitte neu aktivieren.", "warning");
        await subscription.unsubscribe();
        subscription = null;
        await refresh();
    } else {
        setStatus(`Test fehlgeschlagen: ${result.errors.join("; ")}`, "error");
    }
}));

rowsEl.addEventListener("click", event => {
    const button = event.target.closest("[data-remove]");
    if (!button) return;
    busy(async () => {
        const id = button.dataset.remove;
        await postJson(`/api/push/devices/${encodeURIComponent(id)}/remove`);
        if (subscription && id === await deviceId(subscription.endpoint)) {
            await subscription.unsubscribe();
            subscription = null;
        }
        await refresh();
    });
});

async function init() {
    config = await fetchJson("/api/push/config");
    if (!config.available) {
        setStatus(`Push ist auf dem Server nicht verfügbar: ${config.reason}`, "error");
        await refresh();
        return;
    }
    rulesEl.textContent = RULES;

    if (!window.isSecureContext) {
        setStatus("Push geht nur über eine HTTPS-Adresse (https://…). Über http:// lässt sich nichts aktivieren. "
            + "Wie du eine HTTPS-Adresse einrichtest, steht in der Anleitung.", "warning");
        await refresh();
        return;
    }
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
        setStatus(isIos()
            ? "Auf dem iPhone geht Push nur, wenn der Angebotsfinder als App auf dem Home-Bildschirm liegt: "
              + "In Safari auf Teilen tippen, dann „Zum Home-Bildschirm“. Danach die App öffnen, anmelden "
              + "und hier aktivieren. Das geht ab iOS 16.4."
            : "Dieser Browser unterstützt keine Push-Nachrichten.", "warning");
        await refresh();
        return;
    }

    registration = await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
    subscription = await registration.pushManager.getSubscription();

    // Hat der Server einen neuen Schluessel (Volume neu angelegt), taugt die
    // alte Anmeldung nicht mehr. Dann verwerfen - Aktivieren legt sie neu an.
    const used = subscription && subscription.options && subscription.options.applicationServerKey;
    if (used && !sameBytes(new Uint8Array(used), urlBase64ToUint8Array(config.public_key))) {
        await subscription.unsubscribe();
        subscription = null;
    }
    await refresh();
}

init().catch(err => {
    console.error(err);
    setStatus(`Fehler: ${err.message}`, "error");
});
