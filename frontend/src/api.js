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

/**
 * Patch: apply a one-click fix to an already-remediated PDF blob.
 *
 * action = "metadata" | "headings"
 * params = { title, lang, author, subject }  (for metadata action)
 *
 * Returns { blob, result } — blob is the patched PDF, result is the X-Patch-Result JSON.
 */
export async function patch(pdfBlob, filename, action, params = {}) {
  const fd = new FormData();
  fd.append("file", pdfBlob, filename || "document.pdf");
  fd.append("action", action);
  if (params.title   !== undefined) fd.append("title",   params.title);
  if (params.lang    !== undefined) fd.append("lang",    params.lang);
  if (params.author  !== undefined) fd.append("author",  params.author);
  if (params.subject !== undefined) fd.append("subject", params.subject);
  const res = await fetch(url("/patch"), { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errText(res, "patch"));
  let result = null;
  try { result = JSON.parse(res.headers.get("X-Patch-Result") || "null"); } catch {}
  const blob = await res.blob();
  return { blob, result };
}

/**
 * Quick Fix All — applies every AI-driven auto-fix to an already-remediated PDF.
 * Returns { blob, result } — blob is the patched PDF, result is X-QuickFix-Result JSON.
 */
export async function quickfix(pdfBlob, filename) {
  const fd = new FormData();
  fd.append("file", pdfBlob, filename || "document.pdf");
  const res = await fetch(url("/quickfix"), { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errText(res, "quickfix"));
  let result = null;
  try { result = JSON.parse(res.headers.get("X-QuickFix-Result") || "null"); } catch {}
  const blob = await res.blob();
  return { blob, result };
}

/**
 * AI visual review — Claude looks at rendered pages + the structure tree and
 * flags the human-judgment items (alt text accuracy, reading order, headings,
 * decorative choices). Assistive triage only; never replaces human review.
 */
export async function visualCheck(pdfBlob, filename) {
  const fd = new FormData();
  fd.append("file", pdfBlob, filename || "document.pdf");
  const res = await fetch(url("/visual-check"), { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errText(res, "visual-check"));
  return res.json();
}

export async function getReadingOrder(pdfBlob, filename) {
  const fd = new FormData();
  fd.append("file", pdfBlob, filename || "document.pdf");
  const res = await fetch(url("/reading-order"), { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errText(res, "reading-order"));
  return res.json();
}

export async function reorder(pdfBlob, filename, orderedIds) {
  const fd = new FormData();
  fd.append("file", pdfBlob, filename || "document.pdf");
  fd.append("order", JSON.stringify(orderedIds));
  const res = await fetch(url("/reorder"), { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errText(res, "reorder"));
  let result = null;
  try { result = JSON.parse(res.headers.get("X-Reorder-Result") || "null"); } catch {}
  const blob = await res.blob();
  return { blob, result };
}

/**
 * Batch remediation — processes files one at a time client-side so the UI
 * can show live per-file progress.
 * onStatus(index, patch) is called after each phase: scanning, remediating, done, error.
 */
export async function batchRemediateFiles(files, onStatus) {
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    try {
      onStatus(i, { status: "scanning" });
      let m = await autotag(file);
      const { _questions, ...manifest } = m;
      manifest.document = {
        ...(manifest.document || {}),
        language: manifest.document?.language || "en-US",
      };
      onStatus(i, { status: "remediating" });
      const { blob, conformance } = await remediate(file, manifest);
      const baseName = file.name.replace(/\.pdf$/i, "");
      const downloadUrl = URL.createObjectURL(blob);
      onStatus(i, {
        status: "done",
        downloadUrl,
        downloadName: `${baseName}.accessible.pdf`,
        compliant: conformance?.compliant ?? false,
        failedRules: conformance?.failedRules ?? 0,
      });
    } catch (e) {
      onStatus(i, { status: "error", error: String(e.message || e) });
    }
  }
}

function errText(res, label) {
  return res.text().then(t => `${label} ${res.status}: ${t.slice(0, 200)}`).catch(() => `${label} ${res.status}`);
}
