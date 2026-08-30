/*
 * Outbound + contact click tracking for GA4 (gtag). Imported for its side effect
 * from the app entry — attaches one delegated document listener that fires:
 *   - outbound_click → any external http(s) link (Auto-Flow CTAs / cross-links),
 *                      labeled with destination domain, link text, and source host.
 *   - contact_click  → mailto:/tel: links.
 * Safe if gtag hasn't loaded (events are dropped).
 */
function send(name, params) {
  if (typeof window !== "undefined" && typeof window.gtag === "function") {
    window.gtag("event", name, params);
  }
}

if (typeof document !== "undefined") {
  document.addEventListener(
    "click",
    function (e) {
      var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
      if (!a) return;
      var raw = a.getAttribute("href") || "";
      if (raw.indexOf("mailto:") === 0) {
        send("contact_click", { method: "email", link_url: raw, source: location.hostname });
        return;
      }
      if (raw.indexOf("tel:") === 0) {
        send("contact_click", { method: "phone", link_url: raw, source: location.hostname });
        return;
      }
      var u;
      try {
        u = new URL(a.href, location.href);
      } catch (_) {
        return;
      }
      if (!/^https?:$/.test(u.protocol)) return;
      if (u.hostname && u.hostname !== location.hostname) {
        send("outbound_click", {
          link_domain: u.hostname,
          link_url: u.href,
          link_text: (a.textContent || "").replace(/\s+/g, " ").trim().slice(0, 100),
          source: location.hostname,
        });
      }
    },
    true
  );
}
