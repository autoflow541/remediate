// Backend client. The base URL is configured at build time via VITE_API_BASE
// so the studio (which can live on static hosting) can point at an engine
// running anywhere. If unset, the autotag/validate/remediate features are
// disabled and the studio still works for manual judgment + manifest export.

export const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");
export const HAS_BACKEND = API_BASE.length > 0;

function url(path) {
  if (!HAS_BACKEND) throw new Error("No backend configured (set VITE_API_BASE).");
  return `${API_BASE}${path}`;
}

export async function health() {
  const res = await fetch(url("/health"));
  if (!res.ok) throw new Error(`health ${res.status}`);
  return res.json();
}

export async function autotag(file, detectHeaders = true) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("detect_headers", String(detectHeaders));
  const res = await fetch(url("/autotag"), { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errText(res, "autotag"));
  return res.json();
}

export async function validate(file, flavour = "ua1") {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("flavour", flavour);
  const res = await fetch(url("/validate"), { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errText(res, "validate"));
  return res.json();
}

/**
 * Remediate: send the original PDF + manifest, get back the tagged PDF (as a
 * Blob) plus the conformance summary parsed from the X-Conformance header.
 */
export async function remediate(file, manifest, flavour = "ua1") {
  const fd = new FormData();
  fd.append("file", file);
  fd.append(
    "manifest",
    new Blob([JSON.stringify(manifest)], { type: "application/json" }),
    "manifest.tags.json"
  );
  fd.append("flavour", flavour);
  const res = await fetch(url("/remediate"), { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errText(res, "remediate"));
  let conformance = null;
  try {
    conformance = JSON.parse(res.headers.get("X-Conformance") || "null");
  } catch {
    /* header optional */
  }
  const blob = await res.blob();
  return { blob, conformance };
}

async function errText(res, label) {
  try {
    const body = await res.json();
    return `${label} ${res.status}: ${body.detail || JSON.stringify(body)}`;
  } catch {
    return `${label} ${res.status}`;
  }
}
