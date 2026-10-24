// Service Worker fuer Push-Nachrichten.
// Wird als /sw.js von der Wurzel ausgeliefert (Geltungsbereich: ganze Seite) und
// ist ohne Anmeldung abrufbar: der Browser holt ihn regelmaessig neu, auch wenn
// die Session gerade abgelaufen ist. Kein fetch-Handler - Seitenaufrufe laufen
// unveraendert am Service Worker vorbei.

self.addEventListener("push", event => {
    let data = {};
    try {
        data = event.data ? event.data.json() : {};
    } catch (err) {
        data = { title: "Angebotsfinder", body: event.data ? event.data.text() : "" };
    }

    const title = data.title || "Angebotsfinder";
    event.waitUntil(self.registration.showNotification(title, {
        body: data.body || "",
        // Gleiche Art ersetzt die aeltere Nachricht, statt sie zu stapeln.
        tag: data.tag || "angebotsfinder",
        renotify: true,
        icon: "/static/icons/icon-192.png",
        data: { url: data.url || "/" },
    }));
});

self.addEventListener("notificationclick", event => {
    event.notification.close();
    const url = (event.notification.data && event.notification.data.url) || "/";
    event.waitUntil((async () => {
        const fenster = await clients.matchAll({ type: "window", includeUncontrolled: true });
        for (const client of fenster) {
            if (new URL(client.url).pathname === url && "focus" in client) {
                return client.focus();
            }
        }
        return clients.openWindow(url);
    })());
});
