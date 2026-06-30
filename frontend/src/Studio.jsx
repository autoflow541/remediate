import { useState, useRef, useCallback, useEffect, useMemo } from "react";
import * as api from "./api";
import { API_BASE, patch as apiPatch, quickfix as apiQuickfix, getReadingOrder, reorder as apiReorder } from "./api";
import { fixHeadingOrder, scoreManifest } from "./manifest";

// ── Helpers ────────────────────────────────────────────────────────────────

/** Derive a title automatically from manifest content or filename. */
function autoTitle(manifest, filename) {
  // 1. PDF metadata
  const doc = manifest?.document || {};
  if ((doc.title || "").trim()) return doc.title.trim();
  if ((doc.suggestedTitle || "").trim()) return doc.suggestedTitle.trim();

  // 2. First heading (H1 preferred, then any heading)
  function findHeading(nodes, preferred) {
    for (const n of nodes || []) {
      if (n.tag === preferred && (n.text || "").trim()) return n.text.trim();
      const found = findHeading(n.children, preferred);
      if (found) return found;
    }
    return "";
  }
  const h1 = findHeading(manifest?.nodes, "H1");
  if (h1) return h1;
  for (const tag of ["H2", "H3", "H4", "H5", "H6"]) {
    const h = findHeading(manifest?.nodes, tag);
    if (h) return h;
  }

  // 3. Clean filename
  if (filename) {
    return filename
      .replace(/\.pdf$/i, "")
      .replace(/[-_]+/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase())
      .trim();
  }
  return "";
}

function patchNodeInTree(nodes, id, patch) {
  return nodes.map((n) => {
    if (n.id === id) return { ...n, ...patch };
    if (n.children) return { ...n, children: patchNodeInTree(n.children, id, patch) };
    return n;
  });
}

// ── Root App ───────────────────────────────────────────────────────────────
// Screens: idle → processing → hallway (optional) → remediating → done

export default function App() {
  const [screen, setScreen] = useState("idle");
  const [file, setFile] = useState(null);
  const [manifest, setManifest] = useState(null);
  const [questions, setQuestions] = useState([]);
  const [result, setResult] = useState(null);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");

  const doRemediate = useCallback(async (f, m) => {
    setScreen("remediating");
    setProgress("Building your accessible PDF…");
    try {
      const { blob: remBlob, conformance: remConformance } = await api.remediate(f, m);
      const name = (m.source?.filename || "document").replace(/\.pdf$/i, "");
      const filename = `${name}.accessible.pdf`;

      // ── Auto Quick Fix: runs on every remediation path ──────────────────
      setProgress("Applying accessibility fixes…");
      let blob = remBlob;
      let qfResult = null;
      let conformance = remConformance;
      try {
        const qf = await api.quickfix(remBlob, filename);
        blob = qf.blob;
        qfResult = qf.result;
        if (qf.result) {
          const fixes = qf.result.fixes || {};
          const notes = qf.result.notes || [];
          const verapdfPassed = notes.some(n => /compliant/i.test(n));
          conformance = {
            ...remConformance,
            // If AI compliance loop confirmed PDF/UA-1 pass, mark compliant
            compliant: verapdfPassed ? true : remConformance?.compliant,
            // If fonts were embedded by quickfix, clear font errors
            fontIssues: fixes.fontsEmbedded > 0
              ? (remConformance?.fontIssues ?? []).filter(i => i.severity !== "error")
              : remConformance?.fontIssues,
          };
        }
      } catch (_) { /* quickfix optional — use remediated blob */ }

      const downloadUrl = URL.createObjectURL(blob);
      const contrastCount = conformance?.contrastCount ?? 0;
      const contrastPassed = contrastCount === 0;
      // Only truly unfixable link issues (no URL to resolve) penalize the score.
      // Auto-fixed links (descriptive /Alt injected) do not.
      const linkCount = conformance?.linkQualityCount ?? 0;
      const linkPenalty = Math.min(linkCount * 3, 10);
      // Font errors (missing ToUnicode / unembedded) = text invisible to screen readers.
      const fontErrorCount = (conformance?.fontIssues ?? []).filter(f => f.severity === "error").length;
      const fontPenalty = Math.min(fontErrorCount * 5, 20);
      // Non-text contrast (WCAG 1.4.11) — UI components and graphics.
      const nontextContrastCount = conformance?.nontextContrastCount ?? 0;
      const nontextPenalty = Math.min(nontextContrastCount * 2, 10);
      // Structure completeness (PDF/UA §7.1) — orphaned content not in struct tree.
      const orphanedPages = conformance?.structCompleteness?.orphaned_count ?? 0;
      const structPenalty = Math.min(orphanedPages * 5, 15);
      // Metadata issues (PDF/UA XMP requirements).
      const metadataErrorCount = (conformance?.metadataIssues ?? []).filter(i => i.severity === "error").length;
      const metadataPenalty = Math.min(metadataErrorCount * 3, 9);
      // Round-trip check — writeback failures caught by reading the output PDF.
      const rtFailures = conformance?.roundTrip?.failures ?? [];
      const rtErrorCount = rtFailures.filter(f => f.severity === "error").length;
      const rtPenalty = Math.min(rtErrorCount * 8, 25);
      // 100% = veraPDF PDF/UA-1 compliant + no contrast failures + no link issues
      const score = (conformance?.compliant && contrastPassed && linkCount === 0)
        ? 100
        : (() => {
            const { score: s } = scoreManifest(m, { contrastPassed });
            let base = contrastCount > 0 ? Math.min(s, 85) : s;
            if (!conformance?.compliant && (conformance?.failedRules ?? 0) > 0) base = Math.min(base, 94);
            if (fontErrorCount > 0)      base = Math.min(base, 80);
            if (orphanedPages > 0)       base = Math.min(base, 80);
            if (metadataErrorCount > 0)  base = Math.min(base, 90);
            if (rtErrorCount > 0)        base = Math.min(base, 80);
            return Math.max(base - linkPenalty - fontPenalty - nontextPenalty - structPenalty - metadataPenalty - rtPenalty, 0);
          })();
      setResult({ conformance, score, filename, downloadUrl, manifest: m, qfResult });
      setScreen("done");
    } catch (e) {
      setError(String(e.message || e));
      setScreen("idle");
    }
  }, []);

  const handleFile = useCallback(async (f) => {
    if (!f || f.type !== "application/pdf") {
      setError("Please upload a PDF file.");
      return;
    }
    setFile(f);
    setError("");
    setScreen("processing");
    setProgress("Scanning and auto-tagging…");

    try {
      let m = await api.autotag(f);

      // Apply all automatic fixes
      m = fixHeadingOrder(m);
      const derivedTitle = autoTitle(m, f.name);
      m = {
        ...m,
        document: {
          ...m.document,
          language: m.document.language || "en-US",
          title: derivedTitle,
        },
      };

      // Extract image alt questions (title is now always auto-derived)
      const qs = [...(m._questions || [])];
      const { _questions, ...cleanManifest } = m;

      setManifest(cleanManifest);
      setQuestions(qs);

      if (qs.length > 0) {
        setScreen("hallway");
      } else {
        await doRemediate(f, cleanManifest);
      }
    } catch (e) {
      setError(String(e.message || e));
      setScreen("idle");
    }
  }, [doRemediate]);

  const handleHallwayComplete = useCallback((updatedManifest) => {
    setManifest(updatedManifest);
    doRemediate(file, updatedManifest);
  }, [file, doRemediate]);

  // ── Auto-Fix pipeline: autotag → remediate → quickfix, no human prompts ────
  const handleFileAuto = useCallback(async (f) => {
    if (!f || f.type !== "application/pdf") { setError("Please upload a PDF file."); return; }
    setFile(f); setError(""); setScreen("processing"); setProgress("Scanning document…");
    try {
      let m = await api.autotag(f);
      m = fixHeadingOrder(m);
      const derivedTitle = autoTitle(m, f.name);
      m = { ...m, document: { ...m.document, language: m.document.language || "en-US", title: derivedTitle } };
      const { _questions, ...cleanManifest } = m;

      setProgress("AI making accessibility decisions…");
      const { blob: remBlob, conformance: remConformanceAuto } = await api.remediate(f, cleanManifest);

      setProgress("Applying AI compliance fixes…");
      let finalBlob = remBlob;
      let qfResult = null;
      let conformance = remConformanceAuto;
      try {
        const qf = await api.quickfix(remBlob, f.name.replace(/\.pdf$/i, "") + ".accessible.pdf");
        finalBlob = qf.blob; qfResult = qf.result;
        if (qf.result) {
          const fixes = qf.result.fixes || {};
          const notes = qf.result.notes || [];
          const verapdfPassed = notes.some(n => /compliant/i.test(n));
          conformance = {
            ...remConformanceAuto,
            compliant: verapdfPassed ? true : remConformanceAuto?.compliant,
            fontIssues: fixes.fontsEmbedded > 0
              ? (remConformanceAuto?.fontIssues ?? []).filter(i => i.severity !== "error")
              : remConformanceAuto?.fontIssues,
          };
        }
      } catch (_) { /* quickfix optional — use remediated blob */ }

      const name = (cleanManifest.source?.filename || f.name).replace(/\.pdf$/i, "");
      const downloadUrl = URL.createObjectURL(finalBlob);
      const contrastCount = conformance?.contrastCount ?? 0;
      const contrastPassed = contrastCount === 0;
      const linkCount = conformance?.linkQualityCount ?? 0;
      const linkPenalty = Math.min(linkCount * 3, 10);
      const fontErrorCount = (conformance?.fontIssues ?? []).filter(fi => fi.severity === "error").length;
      const fontPenalty = Math.min(fontErrorCount * 5, 20);
      const nontextContrastCount = conformance?.nontextContrastCount ?? 0;
      const nontextPenalty = Math.min(nontextContrastCount * 2, 10);
      const orphanedPages = conformance?.structCompleteness?.orphaned_count ?? 0;
      const structPenalty = Math.min(orphanedPages * 5, 15);
      const metadataErrorCount = (conformance?.metadataIssues ?? []).filter(i => i.severity === "error").length;
      const metadataPenalty = Math.min(metadataErrorCount * 3, 9);
      const rtFailures = conformance?.roundTrip?.failures ?? [];
      const rtErrorCount = rtFailures.filter(ri => ri.severity === "error").length;
      const rtPenalty = Math.min(rtErrorCount * 8, 25);
      // 100% = veraPDF PDF/UA-1 compliant + no contrast failures + no link issues
      const score = (conformance?.compliant && contrastPassed && linkCount === 0)
        ? 100
        : (() => {
          const { score: s } = scoreManifest(cleanManifest, { contrastPassed });
          let base = contrastCount > 0 ? Math.min(s, 85) : s;
          if (!conformance?.compliant && (conformance?.failedRules ?? 0) > 0) base = Math.min(base, 94);
          if (fontErrorCount > 0)     base = Math.min(base, 80);
          if (orphanedPages > 0)      base = Math.min(base, 80);
          if (metadataErrorCount > 0) base = Math.min(base, 90);
          if (rtErrorCount > 0)       base = Math.min(base, 80);
          return Math.max(base - linkPenalty - fontPenalty - nontextPenalty - structPenalty - metadataPenalty - rtPenalty, 0);
        })();
      setResult({ conformance, score, filename: `${name}.accessible.pdf`, downloadUrl, manifest: cleanManifest, autoMode: true, qfResult });
      setScreen("done");
    } catch (e) { setError(String(e.message || e)); setScreen("idle"); }
  }, []);

  const handleReset = useCallback(() => {
    if (result?.downloadUrl) URL.revokeObjectURL(result.downloadUrl);
    setScreen("idle");
    setFile(null);
    setManifest(null);
    setQuestions([]);
    setResult(null);
    setError("");
  }, [result]);

  // Update document title and announce screen changes to screen readers
  useEffect(() => {
    const titles = {
      idle: "PDF Accessibility Remediation",
      processing: "Scanning PDF… — PDF Accessibility Remediation",
      remediating: "Building accessible PDF… — PDF Accessibility Remediation",
      hallway: "Input needed — PDF Accessibility Remediation",
      done: "Remediation complete — PDF Accessibility Remediation",
    };
    document.title = titles[screen] || titles.idle;
  }, [screen]);

  switch (screen) {
    case "idle":        return <UploadScreen onFileManual={handleFile} onFileAuto={handleFileAuto} onBatch={() => setScreen("batch")} error={error} />;
    case "processing":
    case "remediating": return <ProcessingScreen message={progress} />;
    case "hallway":     return <HallwayScreen questions={questions} manifest={manifest} onComplete={handleHallwayComplete} />;
    case "done":        return <DoneScreen result={result} onReset={handleReset} />;
    case "batch":       return <BatchScreen onBack={() => setScreen("idle")} />;
    default:            return null;
  }
}

// ── Screen: Upload ─────────────────────────────────────────────────────────

function UploadScreen({ onFileManual, onFileAuto, onBatch, error }) {
  const manualRef = useRef();
  const autoRef = useRef();
  const headingRef = useRef();

  useEffect(() => { headingRef.current?.focus(); }, []);

  const noBackend = !api.HAS_BACKEND;

  const handleDrop = (e, mode) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (!f || noBackend) return;
    mode === "auto" ? onFileAuto(f) : onFileManual(f);
  };

  return (
    <main id="main-content" className="screen screen--upload">
      <h1 ref={headingRef} tabIndex={-1} className="sr-only">PDF Accessibility Remediation</h1>
      <p className="app-title" aria-hidden="true">PDF Accessibility Remediation</p>
      <p className="app-subtitle"><a href="https://auto-flow.co" target="_blank" rel="noopener noreferrer">auto-flow.co</a></p>

      <div className="upload-paths" role="group" aria-label="Choose how to remediate your PDF">

        {/* ── Manual path ── */}
        <div
          className="upload-path upload-path--manual"
          onDragOver={(e) => { e.preventDefault(); }}
          onDrop={(e) => handleDrop(e, "manual")}
        >
          <p className="upload-path-icon" aria-hidden="true">🔍</p>
          <h2 className="upload-path-title">Review &amp; Edit</h2>
          <p className="upload-path-desc">Review each accessibility decision yourself. Full control over tags, alt text, and reading order.</p>
          <button
            className="primary"
            disabled={noBackend}
            aria-label="Choose a PDF to remediate manually"
            onClick={() => manualRef.current.click()}
          >
            Choose PDF
          </button>
          <input ref={manualRef} type="file" accept="application/pdf" style={{ display: "none" }}
            onChange={(e) => e.target.files[0] && onFileManual(e.target.files[0])} />
        </div>

        <div className="upload-paths-divider" aria-hidden="true"><span>or</span></div>

        {/* ── Auto path ── */}
        <div
          className="upload-path upload-path--auto"
          onDragOver={(e) => { e.preventDefault(); }}
          onDrop={(e) => handleDrop(e, "auto")}
        >
          <p className="upload-path-icon" aria-hidden="true">⚡</p>
          <h2 className="upload-path-title">AI Auto-Fix</h2>
          <p className="upload-path-desc">AI makes all accessibility decisions. One click to a compliant PDF — no questions asked.</p>
          <button
            className="primary upload-path-auto-btn"
            disabled={noBackend}
            aria-label="Choose a PDF to auto-fix with AI"
            onClick={() => autoRef.current.click()}
          >
            Choose PDF
          </button>
          <input ref={autoRef} type="file" accept="application/pdf" style={{ display: "none" }}
            onChange={(e) => e.target.files[0] && onFileAuto(e.target.files[0])} />
        </div>

      </div>

      {noBackend && <p className="err" role="alert">No backend configured. Set VITE_API_BASE to enable remediation.</p>}
      {error && <p className="err" role="alert">{error}</p>}
      <p className="privacy-note">
        Your file is only sent to the engine during processing. We don't store your documents.
        {!noBackend && (
          <> · <a href={`${API_BASE}/docs`} target="_blank" rel="noreferrer" className="api-docs-link">API docs ↗</a></>
        )}
      </p>
      {!noBackend && (
        <button className="batch-link" onClick={onBatch} aria-label="Switch to batch mode — process multiple PDFs at once">
          Process multiple PDFs at once
        </button>
      )}
    </main>
  );
}

// ── Screen: Processing ─────────────────────────────────────────────────────

function ProcessingScreen({ message }) {
  const headingRef = useRef();
  useEffect(() => { headingRef.current?.focus(); }, []);

  return (
    <main id="main-content" className="screen">
      {/* Focus target — tells screen reader we're on a new screen */}
      <h1 ref={headingRef} tabIndex={-1} className="sr-only">Processing your PDF</h1>
      {/* aria-live announces status updates (message changes) without re-focusing */}
      <div role="status" aria-live="polite">
        <div className="spinner" aria-hidden="true" />
        <p className="processing-message">{message}</p>
      </div>
    </main>
  );
}

// ── Screen: Hallway ────────────────────────────────────────────────────────

function HallwayScreen({ questions, manifest, onComplete }) {
  const headingRef = useRef();
  useEffect(() => { headingRef.current?.focus(); }, []);

  const [answers, setAnswers] = useState(() =>
    questions.map(q => ({
      value: q.type === "title" ? (manifest?.document?.title || "") : (q.suggestedAlt || ""),
      decorative: false,
    }))
  );

  const imageQuestions = questions.filter(q => q.type === "image_alt");
  const titleQuestion  = questions.find(q => q.type === "title");

  const setAnswer = (i, patch) =>
    setAnswers(prev => prev.map((a, j) => j === i ? { ...a, ...patch } : a));

  const markAllDecorative = () =>
    setAnswers(prev => prev.map((a, i) =>
      questions[i].type === "image_alt" ? { ...a, decorative: true, value: "" } : a
    ));

  const handleSubmit = (e) => {
    e.preventDefault();
    let updated = manifest;
    questions.forEach((q, i) => {
      const { value, decorative } = answers[i];
      if (q.type === "title") {
        updated = { ...updated, document: { ...updated.document, title: value.trim() || updated.document?.title || "" } };
      } else if (q.type === "image_alt") {
        if (decorative) {
          updated = { ...updated, nodes: patchNodeInTree(updated.nodes, q.nodeId, { decorative: true, alt: "" }) };
        } else if (value.trim()) {
          updated = { ...updated, nodes: patchNodeInTree(updated.nodes, q.nodeId, { alt: value.trim(), decorative: false }) };
        }
      }
    });
    onComplete(updated);
  };

  return (
    <main id="main-content" className="screen hw-screen">
      <h1 ref={headingRef} tabIndex={-1} className="hw-heading">
        Review before remediating
      </h1>
      <p className="hw-sub">
        {imageQuestions.length} image{imageQuestions.length !== 1 ? "s" : ""} need alt text.
        Fill in descriptions, mark decoratives, then click Apply.
      </p>

      <form onSubmit={handleSubmit} aria-label="Alt text review form">
        {titleQuestion && (() => {
          const ti = questions.indexOf(titleQuestion);
          return (
            <div className="hw-section">
              <label className="hw-label" htmlFor="hw-title">Document title</label>
              <input
                id="hw-title"
                className="hw-input"
                type="text"
                value={answers[ti].value}
                onChange={e => setAnswer(ti, { value: e.target.value })}
                placeholder="e.g. Annual Report 2024"
              />
            </div>
          );
        })()}

        {imageQuestions.length > 0 && (
          <div className="hw-section">
            <div className="hw-list-header">
              <span className="hw-label">Image alt text</span>
              <button type="button" className="hw-mark-all" onClick={markAllDecorative}>
                Mark all decorative
              </button>
            </div>
            <ol className="hw-list">
              {questions.map((q, i) => {
                if (q.type !== "image_alt") return null;
                const ans = answers[i];
                return (
                  <li key={i} className={`hw-item${ans.decorative ? " hw-item--decorative" : ""}`}>
                    <div className="hw-item-meta">
                      {q.imageData
                        ? <img src={q.imageData} alt="" className="hw-thumb" aria-hidden="true" />
                        : <div className="hw-thumb hw-thumb--placeholder" aria-hidden="true">p.{q.page}</div>
                      }
                      <span className="hw-item-num">Image {questions.filter((qq,ii) => qq.type === "image_alt" && ii <= i).length}</span>
                      {q.hint && <span className="hw-hint">⚠ {q.hint}</span>}
                    </div>
                    <div className="hw-item-inputs">
                      <textarea
                        className="hw-textarea"
                        value={ans.value}
                        onChange={e => setAnswer(i, { value: e.target.value, decorative: false })}
                        placeholder="Describe what this image shows…"
                        disabled={ans.decorative}
                        aria-label={`Alt text for image ${i + 1}`}
                        rows={2}
                      />
                      <label className="hw-decorative-label">
                        <input
                          type="checkbox"
                          checked={ans.decorative}
                          onChange={e => setAnswer(i, { decorative: e.target.checked, value: e.target.checked ? "" : ans.value })}
                        />
                        Decorative (skip)
                      </label>
                    </div>
                  </li>
                );
              })}
            </ol>
          </div>
        )}

        <div className="hw-actions">
          <button type="submit" className="primary">
            Apply &amp; Remediate →
          </button>
          <p className="hw-skip-note">Images without alt text will use auto-generated descriptions.</p>
        </div>
      </form>
    </main>
  );
}

// ── Tag colours for the PDF overlay ───────────────────────────────────────

const TAG_COLORS = {
  H1: "#2ea043", H2: "#2ea043", H3: "#3fb950", H4: "#56d364", H5: "#56d364", H6: "#56d364",
  P: "#4493f8",
  Figure: "#d29922",
  Table: "#bc8cff", TH: "#e06c75", TD: "#bc8cff", TR: "#bc8cff",
  L: "#56d364", LI: "#79c0ff",
  Caption: "#ffa657",
  Link: "#f0883e",
};

const LEGEND = [
  { tag: "H1", label: "Headings" },
  { tag: "P", label: "Paragraphs" },
  { tag: "Figure", label: "Images" },
  { tag: "Table", label: "Tables" },
  { tag: "TH", label: "Table headers" },
  { tag: "L", label: "Lists" },
  { tag: "Link", label: "Links" },
];

function getNodeLabel(node) {
  const tag = node.tag || "";
  const text = (node.text || "").trim().slice(0, 28);
  const alt  = (node.alt  || "").trim().slice(0, 28);
  if (/^H[1-6]$/.test(tag)) return text ? `${tag}: ${text}` : tag;
  if (tag === "P")       return text ? `P: ${text}` : "Paragraph";
  if (tag === "Figure")  return node.decorative ? "Decorative" : (alt ? `Img: ${alt}` : "Image");
  if (tag === "TH")      return "Header cell";
  if (tag === "TD")      return "Data cell";
  if (tag === "Table")   return "Table";
  if (tag === "TR")      return "Row";
  if (tag === "L")       return "List";
  if (tag === "LI")      return text ? `LI: ${text}` : "List item";
  if (tag === "Link")    return text ? `Link: ${text}` : "Link";
  if (tag === "Caption") return text ? `Caption: ${text}` : "Caption";
  return tag;
}

const PDFJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js";
const PDFJS_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
const MAX_PAGES = 30;

function PDFPreviewPanel({ pdfUrl, manifest }) {
  const containerRef = useRef();
  const [status, setStatus] = useState("loading");

  const nodesByPage = useMemo(() => {
    const byPage = {};
    // Returns true if any direct child is itself drawn in the overlay.
    // Used to skip container nodes (Table, TR, L, outer LI) whose children
    // already provide more precise overlays — avoids large overlapping boxes.
    function hasTaggedKids(node) {
      return (node.children || []).some(
        c => c.bbox && c.bbox.length === 4 && TAG_COLORS[c.tag]
      );
    }
    function collect(nodes) {
      for (const node of nodes || []) {
        const p = node.page;
        if (p && node.bbox && node.bbox.length === 4 && TAG_COLORS[node.tag]) {
          // Always draw TH (header cells must stay visible regardless of P children).
          // For everything else: skip containers whose tagged children already cover them.
          if (node.tag === "TH" || !hasTaggedKids(node)) {
            if (!byPage[p]) byPage[p] = [];
            byPage[p].push(node);
          }
        }
        if (node.children) collect(node.children);
      }
    }
    collect(manifest?.nodes || []);
    return byPage;
  }, [manifest]);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      if (!window.pdfjsLib) {
        await new Promise((res, rej) => {
          const s = document.createElement("script");
          s.src = PDFJS_URL;
          s.onload = res; s.onerror = rej;
          document.head.appendChild(s);
        });
      }
      if (cancelled) return;
      window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
      const pdf = await window.pdfjsLib.getDocument(pdfUrl).promise;
      if (cancelled) return;
      const container = containerRef.current;
      if (!container) return;
      container.innerHTML = "";
      const total = Math.min(pdf.numPages, MAX_PAGES);
      for (let pn = 1; pn <= total; pn++) {
        if (cancelled) return;
        const page = await pdf.getPage(pn);
        const vp = page.getViewport({ scale: 1.3 });
        const wrap = document.createElement("div");
        wrap.style.cssText = "position:relative;margin-bottom:12px;display:inline-block;border-radius:6px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.4)";
        const cvs = document.createElement("canvas");
        cvs.width = vp.width; cvs.height = vp.height;
        cvs.style.display = "block";
        cvs.setAttribute("role", "img");
        cvs.setAttribute("aria-label", `Page ${pn} of PDF with accessibility tag overlays`);
        wrap.appendChild(cvs);
        // page number label
        const lbl = document.createElement("div");
        lbl.textContent = `Page ${pn}`;
        lbl.style.cssText = "position:absolute;top:6px;left:6px;background:rgba(0,0,0,.6);color:#fff;font-size:11px;padding:2px 7px;border-radius:4px";
        wrap.appendChild(lbl);
        container.appendChild(wrap);
        await page.render({ canvasContext: cvs.getContext("2d"), viewport: vp }).promise;
        if (cancelled) return;
        // draw overlays + labels on same canvas
        const ctx = cvs.getContext("2d");
        // Bboxes in the manifest are in PDF coordinate space (origin bottom-left,
        // y increases upward) — the same space convertToViewportPoint expects.
        // No Y-flip needed here; the backend normalises both heuristic (PyMuPDF)
        // and ODL bboxes to PDF coords before writing the manifest.
        for (const node of nodesByPage[pn] || []) {
          const color = TAG_COLORS[node.tag];
          if (!color) continue;
          const bb = node.bbox;
          const [vx0, vy0] = vp.convertToViewportPoint(bb[0], bb[1]);
          const [vx1, vy1] = vp.convertToViewportPoint(bb[2], bb[3]);
          const rx = Math.min(vx0, vx1), ry = Math.min(vy0, vy1);
          const rw = Math.abs(vx1 - vx0), rh = Math.abs(vy1 - vy0);

          // Tinted box
          ctx.fillStyle = color + "28";
          ctx.strokeStyle = color;
          ctx.lineWidth = 1.5;
          ctx.fillRect(rx, ry, rw, rh);
          ctx.strokeRect(rx, ry, rw, rh);

          // Label badge in top-left corner of the box
          const label = getNodeLabel(node);
          if (label && rw > 24 && rh > 8) {
            const fs = 9;
            ctx.font = `bold ${fs}px system-ui,sans-serif`;
            const tw = ctx.measureText(label).width;
            const pad = 3;
            const bw = tw + pad * 2;
            const bh = fs + pad * 2;
            // Badge background
            ctx.fillStyle = color;
            ctx.fillRect(rx, ry, bw, bh);
            // Badge text
            ctx.fillStyle = "#fff";
            ctx.fillText(label, rx + pad, ry + bh - pad - 1);
          }
        }
      }
      setStatus("ready");
    }
    run().catch(() => { if (!cancelled) setStatus("error"); });
    return () => { cancelled = true; };
  }, [pdfUrl, nodesByPage]);

  return (
    <div className="pdf-preview">
      <div className="pdf-legend" role="list" aria-label="Tag colour legend">
        {LEGEND.map(({ tag, label }) => (
          <span key={tag} className="legend-item" role="listitem">
            <span className="legend-dot" style={{ background: TAG_COLORS[tag] }} aria-hidden="true" />
            {label}
          </span>
        ))}
      </div>
      {status === "loading" && <p className="muted" style={{ textAlign: "center", padding: "24px 0" }}>Rendering preview…</p>}
      {status === "error" && <p className="err">Preview failed to load.</p>}
      <div ref={containerRef} className="pdf-pages" />
    </div>
  );
}

// ── Audit report generator ─────────────────────────────────────────────────

function buildAuditReport({ conformance, score, filename, manifest }) {
  // ── Reading order editor ──────────────────────────────────────────────
  const [roOpen, setRoOpen] = useState(false);
  const [roElements, setRoElements] = useState([]);
  const [roOrder, setRoOrder] = useState([]);  // current id order
  const [roBusy, setRoBusy] = useState(false);
  const [roError, setRoError] = useState("");
  const [roSuccess, setRoSuccess] = useState("");
  const [roDragIdx, setRoDragIdx] = useState(null);

  async function handleOpenRO() {
    setRoOpen(true); setRoError(""); setRoSuccess("");
    if (roElements.length > 0) return; // already loaded
    setRoBusy(true);
    try {
      const blob = await _getCurrentBlob();
      const data = await getReadingOrder(blob, filename);
      setRoElements(data.elements || []);
      setRoOrder((data.elements || []).map(e => e.id));
    } catch (e) { setRoError(String(e.message || e)); }
    finally { setRoBusy(false); }
  }

  function handleRoDragStart(idx) { setRoDragIdx(idx); }
  function handleRoDragOver(e, idx) {
    e.preventDefault();
    if (roDragIdx === null || roDragIdx === idx) return;
    const next = [...roOrder];
    const [moved] = next.splice(roDragIdx, 1);
    next.splice(idx, 0, moved);
    setRoOrder(next);
    setRoDragIdx(idx);
  }
  function handleRoDragEnd() { setRoDragIdx(null); }

  async function handleApplyRO() {
    setRoBusy(true); setRoError(""); setRoSuccess("");
    try {
      const blob = await _getCurrentBlob();
      const { blob: reordered, result: roResult } = await apiReorder(blob, filename, roOrder);
      const newUrl = URL.createObjectURL(reordered);
      if (activeBlob && activeBlobUrl) URL.revokeObjectURL(activeBlobUrl);
      setActiveBlob(reordered); setActiveBlobUrl(newUrl);
      setRoSuccess(`Reading order saved — ${roResult?.changes_made || 0} element${roResult?.changes_made !== 1 ? "s" : ""} moved.`);
    } catch (e) { setRoError(String(e.message || e)); }
    finally { setRoBusy(false); }
  }

  const report = conformance?.report || {};
  const now = new Date().toLocaleString();
  const docTitle = manifest?.document?.title || filename || "Unknown document";
  const lang = manifest?.document?.language || "Not set";
  const contrastFailures = conformance?.contrastFailures || [];
  const linkIssues = conformance?.linkQualityIssues || [];
  const failedRules = conformance?.failedRules || 0;

  const escHtml = (s) => String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");

  const statusBadge = (ok) => ok
    ? `<span style="color:#166534;font-weight:700">PASS</span>`
    : `<span style="color:#b91c1c;font-weight:700">FAIL</span>`;

  const readingOrderFixed = conformance?.readingOrderFixed || 0;
  const langAnnotations = conformance?.langAnnotations || 0;
  const linkFixed = conformance?.linkQualityFixed || [];
  const failures = conformance?.failures || [];
  const formFixedR = conformance?.formFixed || 0;
  const formTotalR = conformance?.formTotal || 0;
  const altIssuesR = conformance?.altIssues || [];
  const colorOnlyR = conformance?.colorOnlyWarnings || [];
  const headingIssuesR = conformance?.headingIssues || [];
  const sensoryIssuesR = conformance?.sensoryIssues || [];
  const labelNameIssuesR = conformance?.labelNameIssues || [];
  const aiStatsR = manifest?.source?.aiAnalysis || {};

  const rows = [
    ["Document title",       docTitle,                              true],
    ["Document language",    lang,                                  !!lang && lang !== "Not set"],
    ["veraPDF compliant",    conformance?.compliant ? "Yes" : "No", conformance?.compliant],
    ["Conformance score",    `${score}%`,                           score === 100],
    ["Structure elements",   report.elements || 0,                  (report.elements || 0) > 0],
    ["Bookmarks created",    (report.bookmarks || 0) > 0 ? report.bookmarks : "N/A", true],
    ["Table headers tagged", report.headers || 0,                   true],
    ["Figures described",    report.figures || 0,                   true],
    ["Artifacts hidden",     report.artifacts_decorative || 0,      true],
    ["Reading order nodes corrected", readingOrderFixed,            true],
    ["Language annotations added (WCAG 3.1.2)", langAnnotations,   true],
    ["Links given descriptive names (WCAG 2.4.4)", linkFixed.length, true],
    ["Form fields given accessible names (WCAG 4.1.2)", formFixedR, formTotalR === 0 || formFixedR === formTotalR],
    ["Images with alt text issues (WCAG 1.1.1)", altIssuesR.length, altIssuesR.length === 0],
    ["Color-only pattern warnings (WCAG 1.4.1)", colorOnlyR.length, colorOnlyR.length === 0],
    ["Heading structure issues (WCAG 1.3.1 / 2.4.6)", headingIssuesR.length, headingIssuesR.length === 0],
    ["Sensory-only reference warnings (WCAG 1.3.3)", sensoryIssuesR.length, sensoryIssuesR.length === 0],
    ["Label in Name issues (WCAG 2.5.3)", labelNameIssuesR.length, labelNameIssuesR.length === 0],
    ["Non-descriptive link text (WCAG 2.4.4)", conformance?.linkTextIssueCount || 0, (conformance?.linkTextIssueCount || 0) === 0],
    ["Table structure issues (WCAG 1.3.1 / PDF/UA §7.5)", conformance?.tableStructureIssueCount || 0, (conformance?.tableStructureIssueCount || 0) === 0],
    ["Language tagging issues (WCAG 3.1.1 / 3.1.2)", conformance?.languageIssueCount || 0, (conformance?.languageIssueCount || 0) === 0],
    ["Footnote pairs wired (WCAG 2.4.4)", conformance?.footnotePairsWired || 0, true],
    ["TOC items tagged TOC/TOCI (PDF/UA 7.9)", conformance?.tocItemsTagged || 0, true],
    ["Nested lists repaired (WCAG 1.3.1)", conformance?.nestedListsFixed || 0, true],
    ["AI — pages analyzed for layout", aiStatsR.pages_analyzed || 0, true],
    ["AI — reading order corrections", aiStatsR.reading_order_corrections || 0, true],
    ["AI — table headers identified", aiStatsR.header_cells_updated || 0, true],
    ["AI — formulas described", aiStatsR.formulas_described || 0, true],
    ["AI — captions grouped with figures", aiStatsR.captions_grouped || 0, true],
    ["AI — layout tables marked artifact", aiStatsR.layout_tables_detected || 0, true],
    ["AI — list items split Lbl/LBody", aiStatsR.list_items_split || 0, true],
    ["PDF/UA XMP identifier (pdfuaid:part=1)", conformance?.pdfuaMetadata?.xmpMetadata !== "failed" ? "Written" : "Failed", conformance?.pdfuaMetadata?.xmpMetadata !== "failed"],
    ["MarkInfo /Marked (PDF/UA §7.1-3)", conformance?.pdfuaMetadata?.markInfo ? "Set" : "Not set", !!conformance?.pdfuaMetadata?.markInfo],
    ["ViewerPreferences /DisplayDocTitle (§7.1-5)", conformance?.pdfuaMetadata?.displayDocTitle ? "Set" : "Not set", !!conformance?.pdfuaMetadata?.displayDocTitle],
    ["Annotation /Contents fixed (PDF/UA §7.18.1)", conformance?.annotContentsFixed || 0, true],
    ["Radio button groups structured (§7.18.4)", conformance?.radioGroupsFixed || 0, true],
    ["veraPDF post-repair fixes applied", conformance?.verapdfRepairs || 0, true],
    ["Structure coverage (pages with struct content)", `${(conformance?.structCompleteness || {}).coverage_pct ?? 100}%`, ((conformance?.structCompleteness || {}).coverage_pct ?? 100) >= 95],
    ["AI — table summaries generated (WCAG 1.3.1)", conformance?.tableSummaries || 0, true],
    ["AI — alt text quality issues flagged (WCAG 1.1.1)", (conformance?.altQualityIssues || []).length, (conformance?.altQualityIssues || []).length === 0],
    ["Non-text contrast issues (WCAG 1.4.11)", (conformance?.nontextContrastIssues || []).length, (conformance?.nontextContrastIssues || []).length === 0],
    ["Target size issues (WCAG 2.5.8)", (conformance?.targetSizeIssues || []).length, (conformance?.targetSizeIssues || []).length === 0],
    ["XFA form detected (inaccessible)", conformance?.xfaWarning ? "Yes" : "No", !conformance?.xfaWarning],
    ["Font issues (embedding / ToUnicode)", (conformance?.fontIssues || []).length, (conformance?.fontIssues || []).length === 0],
    ["Reflow / text-spacing issues (WCAG 1.4.10/1.4.12)", (conformance?.reflowIssues || []).length, (conformance?.reflowIssues || []).length === 0],
    ["Metadata issues (WCAG 4.1 / PDF/UA)", (conformance?.metadataIssues || []).length, (conformance?.metadataIssues || []).length === 0],
    ["Watermark / background candidates", (conformance?.watermarkCandidates || []).length, true],
    ["Abbreviations detected (WCAG 3.1.4)", (conformance?.abbreviations || []).length, true],
    ["Reading level (Flesch-Kincaid)", (conformance?.readingLevel || {}).grade_level ?? "—", true],
    ["Contrast fixes",       conformance?.contrastFixes || 0,       true],
    // Sprint 19-25
    ["Figure captions linked (PDF/UA 7.3)", conformance?.captionsLinked || 0, true],
    ["Formulas tagged with /Alt (PDF/UA 7.8)", conformance?.formulasTagged || 0, true],
    ["AI vision alt texts generated (WCAG 1.1.1)", conformance?.aiAltGenerated || 0, true],
    ["Contrast stream repairs (WCAG 1.4.3)", conformance?.contrastRepairs || 0, true],
    ["Fonts embedded (PDF/UA 7.21)", conformance?.fontsEmbedded || 0, true],
    ["Optional content layer issues (PDF/UA 7.11)", (conformance?.ocgIssues || []).filter(i => i.severity === "warning").length, (conformance?.ocgIssues || []).filter(i => i.severity === "warning").length === 0],
    ["Encryption accessibility (PDF/UA 7.16)", (conformance?.security || {}).severity === "error" ? "BLOCKED" : (conformance?.security || {}).encrypted ? "Allowed" : "Not encrypted", (conformance?.security || {}).severity !== "error"],
    ["Background fills whitened", conformance?.backgroundFillsWhitened || 0, true],
    ["Contrast issues remaining", conformance?.contrastCount || 0,  (conformance?.contrastCount || 0) === 0],
    ["Link quality issues",  linkIssues.length,                     linkIssues.length === 0],
    ["veraPDF failed rules", failedRules,                           failedRules === 0],
  ];

  const tableRows = rows.map(([label, value, ok]) =>
    `<tr><td>${escHtml(label)}</td><td>${escHtml(value)}</td><td>${statusBadge(ok)}</td></tr>`
  ).join("\n");

  const contrastSection = contrastFailures.length > 0
    ? `<h2>Remaining Contrast Issues (WCAG 1.4.3)</h2>
       <p>Quick Fix All resolves these automatically — or use the manual path to review each one.</p>
       <table><thead><tr><th>Page</th><th>Text excerpt</th><th>Foreground</th><th>Background</th><th>Ratio</th><th>Required</th></tr></thead><tbody>
       ${contrastFailures.map(f => `<tr><td>${f.page}</td><td>${escHtml((f.text||"").slice(0,60))}</td><td><code>${f.fg}</code></td><td><code>${f.bg}</code></td><td>${f.ratio}:1</td><td>${f.required}:1</td></tr>`).join("\n")}
       </tbody></table>` : "";

  const linkFixedSection = linkFixed.length > 0
    ? `<h2>Link Accessible Names Auto-Generated (WCAG 2.4.4)</h2>
       <p>These links had non-descriptive visible text. A descriptive accessible name has been automatically injected as an /Alt attribute on each Link structure element. Screen readers will announce the accessible name instead of the visible text.</p>
       <table><thead><tr><th>Page</th><th>Visible text</th><th>Accessible name assigned</th><th>URL</th></tr></thead><tbody>
       ${linkFixed.map(l => `<tr><td>${l.page}</td><td>${escHtml(l.text)}</td><td style="color:#166534;font-weight:600">${escHtml(l.autoFixedAlt||"")}</td><td style="word-break:break-all;max-width:180px">${escHtml((l.url||"").slice(0,100))}</td></tr>`).join("\n")}
       </tbody></table>` : "";

  const linkSection = linkIssues.length > 0
    ? `<h2>Links Without Resolvable URL (WCAG 2.4.4)</h2>
       <p>These links have non-descriptive text and no resolvable URL, so an accessible name could not be auto-generated.</p>
       <table><thead><tr><th>Page</th><th>Link text</th><th>Issue</th><th>URL</th></tr></thead><tbody>
       ${linkIssues.map(l => `<tr><td>${l.page}</td><td>${escHtml(l.text)}</td><td>${escHtml(l.issue)}</td><td style="word-break:break-all;max-width:200px">${escHtml((l.url||"").slice(0,100))}</td></tr>`).join("\n")}
       </tbody></table>` : "";

  const formSection = formFixedR > 0
    ? `<h2>Form Fields Remediated (WCAG 4.1.2)</h2>
       <p>${formFixedR} of ${formTotalR} form field${formTotalR !== 1 ? "s" : ""} were given accessible names via the /TU tooltip attribute. Screen readers will now announce the accessible name when the field is focused.</p>
       <table><thead><tr><th>Field name</th><th>Accessible name assigned</th><th>Type</th><th>Required</th></tr></thead><tbody>
       ${(conformance?.formFields || []).filter(f => f.fixed).map(f =>
         `<tr><td><code>${escHtml(f.name)}</code></td><td style="color:#166534;font-weight:600">${escHtml(f.label)}</td><td>${escHtml(f.type)}</td><td>${f.required ? "Yes" : "No"}</td></tr>`
       ).join("\n")}
       </tbody></table>` : "";

  const altSection = altIssuesR.length > 0
    ? `<h2>Images Needing Alt Text (WCAG 1.1.1)</h2>
       <p>The following figures have missing, empty, or non-descriptive alt text. Quick Fix All generates alt text using AI vision. Use the manual path to write your own descriptions.</p>
       <table><thead><tr><th>Page</th><th>Issue type</th><th>Current alt text</th><th>Description</th></tr></thead><tbody>
       ${altIssuesR.map(a => `<tr><td>${a.page || "—"}</td><td>${escHtml(a.type)}</td><td><code>${a.alt !== null && a.alt !== undefined ? escHtml(String(a.alt).slice(0,80)) : "(none)"}</code></td><td>${escHtml(a.description)}</td></tr>`).join("\n")}
       </tbody></table>` : "";

  const colorOnlySection = colorOnlyR.length > 0
    ? `<h2>Potential Color-Only Information (WCAG 1.4.1)</h2>
       <p>Color-only patterns detected. Quick Fix All adds WCAG-compliant labels where possible. For charts, the manual path lets you describe each one.</p>
       <table><thead><tr><th>Page</th><th>Shapes</th><th>Colors detected</th></tr></thead><tbody>
       ${colorOnlyR.map(w => `<tr><td>${w.page}</td><td>${w.swatch_count}</td><td>${w.colors.map(c => `<span style="display:inline-block;width:14px;height:14px;background:${escHtml(c)};border:1px solid #ccc;border-radius:2px;vertical-align:middle;margin-right:3px"></span>${escHtml(c)}`).join(" ")}</td></tr>`).join("\n")}
       </tbody></table>` : "";

  const labelNameSection = labelNameIssuesR.length > 0
    ? `<h2>Label in Name Issues (WCAG 2.5.3)</h2>
       <p>Voice control users activate form fields by speaking their visible label. If the accessible name (/TU) doesn't contain that visible text, voice control fails. Update each field's /TU tooltip to include the visible label text.</p>
       <table><thead><tr><th>Page</th><th>Field name</th><th>Visible label</th><th>Accessible name</th><th>Description</th></tr></thead><tbody>
       ${labelNameIssuesR.map(l => `<tr><td>${l.page || "—"}</td><td><code>${escHtml(l.field_name)}</code></td><td>${escHtml(l.visible_label)}</td><td><code>${escHtml(l.accessible_name)}</code></td><td>${escHtml(l.description)}</td></tr>`).join("\n")}
       </tbody></table>` : "";

  const sensorySection = sensoryIssuesR.length > 0
    ? `<h2>Sensory-Only Reference Warnings (WCAG 1.3.3)</h2>
       <p>Instructions should not rely solely on sensory characteristics (shape, color, size, visual location, orientation, or sound). The following passages may require review. These are advisory — confirm that each reference is supplemented by a non-sensory identifier (text label, heading, or programmatic name) before considering it resolved.</p>
       <table><thead><tr><th>Page</th><th>Type</th><th>Matched phrase</th><th>Text excerpt</th><th>Guidance</th></tr></thead><tbody>
       ${sensoryIssuesR.map(s => `<tr><td>${s.page || "—"}</td><td>${escHtml(s.type.replace(/_/g," "))}</td><td><code>${escHtml(s.match)}</code></td><td>${escHtml((s.text||"").slice(0,80))}</td><td>${escHtml(s.description)}</td></tr>`).join("\n")}
       </tbody></table>` : "";

  const headingSection = headingIssuesR.length > 0
    ? `<h2>Heading Structure Issues (WCAG 1.3.1 / 2.4.6)</h2>
       <p>Heading levels must form a logical hierarchy (H1 → H2 → H3…) with no skipped levels, and a document must contain at least one H1. These issues must be corrected in the source document.</p>
       <table><thead><tr><th>Type</th><th>Level</th><th>Page</th><th>Description</th></tr></thead><tbody>
       ${headingIssuesR.map(h => `<tr><td>${escHtml(h.type)}</td><td>${h.level ? `H${h.level}` : "—"}</td><td>${h.page || "—"}</td><td>${escHtml(h.description)}</td></tr>`).join("\n")}
       </tbody></table>` : "";

  const failureSection = failures.length > 0
    ? `<h2>PDF/UA-1 Conformance Failures</h2>
       <p>The following PDF/UA-1 clauses were not satisfied. Each entry includes a plain-language explanation and remediation hint.</p>
       ${failures.map(f => `
         <div style="border:1px solid #e5e7eb;border-radius:6px;padding:16px;margin-bottom:12px">
           <div style="font-weight:700;color:#ef4444">Clause ${escHtml(f.clause)}.${f.test_number || ""} — ${escHtml(f.plain_title || f.description || "Unknown")}</div>
           <p style="margin:8px 0 4px">${escHtml(f.plain_explanation || f.description || "")}</p>
           <p style="margin:0;color:#6b7280;font-size:.875rem"><strong>How to fix:</strong> ${escHtml(f.plain_hint || "")}</p>
           ${f.failed_checks ? `<p style="margin:4px 0 0;font-size:.8rem;color:#9ca3af">${f.failed_checks} check${f.failed_checks!==1?"s":""} failed</p>` : ""}
         </div>`).join("\n")}` : "";

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Accessibility Audit Report — ${escHtml(docTitle)}</title>
<style>
  body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:900px;margin:40px auto;padding:0 24px;color:#1a1a2e;line-height:1.5}
  h1{font-size:1.6rem;margin-bottom:4px}
  .meta{color:#6b7280;font-size:.875rem;margin-bottom:32px}
  h2{font-size:1.15rem;margin:32px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:6px}
  table{width:100%;border-collapse:collapse;font-size:.875rem;margin-bottom:24px}
  th,td{text-align:left;padding:8px 12px;border:1px solid #e5e7eb}
  th{background:#f9fafb;font-weight:600}
  tr:nth-child(even){background:#f9fafb}
  code{background:#f3f4f6;padding:1px 5px;border-radius:3px;font-size:.8rem}
  .score-badge{display:inline-block;padding:6px 20px;border-radius:999px;font-size:1.4rem;font-weight:800;background:${score===100?"#dcfce7":"#fef3c7"};color:${score===100?"#166534":"#92400e"};margin:12px 0 24px}
  @media print{body{margin:20px}}
</style>
</head>
<body>
<h1>PDF Accessibility Audit Report</h1>
<p class="meta">Generated: ${now} &nbsp;|&nbsp; File: ${escHtml(filename || "unknown")}</p>
<div class="score-badge">${score}% WCAG 2.2 AA</div>
<h2>Summary</h2>
<table>
  <thead><tr><th>Check</th><th>Result</th><th>Status</th></tr></thead>
  <tbody>${tableRows}</tbody>
</table>
${contrastSection}
${linkFixedSection}
${linkSection}
${formSection}
${headingSection}
${sensorySection}
${labelNameSection}
${altSection}
${colorOnlySection}
${failureSection}
${(conformance?.nontextContrastIssues||[]).length > 0 ? `<h2>Non-text Contrast Issues (WCAG 1.4.11)</h2>
<p>UI component boundaries and graphical elements require 3:1 contrast ratio. Quick Fix All darkens borders and strokes to meet the threshold.</p>
<table><thead><tr><th>Page</th><th>Type</th><th>Contrast Ratio</th><th>Description</th></tr></thead><tbody>
${(conformance?.nontextContrastIssues||[]).slice(0,20).map(n=>`<tr><td>${n.page||""}</td><td>${escHtml(n.type||"")}</td><td>${n.ratio||""}</td><td>${escHtml(n.description||"")}</td></tr>`).join("")}
</tbody></table>` : ""}
${(conformance?.targetSizeIssues||[]).length > 0 ? `<h2>Target Size Issues (WCAG 2.5.8)</h2>
<p>Interactive targets (links, form fields) must be at least 24×24 CSS px (≈18 pt). Enlarge small targets in the source document.</p>
<table><thead><tr><th>Page</th><th>Type</th><th>Size (pt)</th><th>Description</th></tr></thead><tbody>
${(conformance?.targetSizeIssues||[]).slice(0,20).map(t=>`<tr><td>${t.page||""}</td><td>${escHtml(t.type||"")}</td><td>${t.width_pt&&t.height_pt?`${t.width_pt}×${t.height_pt}`:""}</td><td>${escHtml(t.description||"")}</td></tr>`).join("")}
</tbody></table>` : ""}
${(conformance?.fontIssues||[]).length > 0 ? `<h2>Font Issues</h2>
<p>Unembedded fonts or fonts missing ToUnicode maps prevent text extraction by assistive technologies.</p>
<table><thead><tr><th>Font</th><th>Severity</th><th>Description</th></tr></thead><tbody>
${(conformance?.fontIssues||[]).slice(0,20).map(f=>`<tr><td>${escHtml(f.font_name||"")}</td><td>${escHtml(f.severity||"")}</td><td>${escHtml(f.description||"")}</td></tr>`).join("")}
</tbody></table>` : ""}
${(conformance?.metadataIssues||[]).length > 0 ? `<h2>Metadata Issues (PDF/UA §7.1)</h2>
<table><thead><tr><th>Field</th><th>Severity</th><th>Description</th></tr></thead><tbody>
${(conformance?.metadataIssues||[]).map(m=>`<tr><td>${escHtml(m.field||"")}</td><td>${escHtml(m.severity||"")}</td><td>${escHtml(m.description||"")}</td></tr>`).join("")}
</tbody></table>` : ""}
${(conformance?.reflowIssues||[]).length > 0 ? `<h2>Reflow / Text Spacing Issues (WCAG 1.4.10/1.4.12)</h2>
<table><thead><tr><th>Page</th><th>Type</th><th>Description</th></tr></thead><tbody>
${(conformance?.reflowIssues||[]).slice(0,20).map(r=>`<tr><td>${r.page||""}</td><td>${escHtml(r.type||"")}</td><td>${escHtml(r.description||"")}</td></tr>`).join("")}
</tbody></table>` : ""}
${(conformance?.watermarkCandidates||[]).length > 0 ? `<h2>Watermarks / Background Elements Detected</h2>
<p>The following elements were detected as likely watermarks or decorative backgrounds. Verify these are correctly tagged as Artifacts in the output PDF.</p>
<table><thead><tr><th>Page</th><th>Confidence</th><th>Description</th></tr></thead><tbody>
${(conformance?.watermarkCandidates||[]).map(w=>`<tr><td>${w.page||""}</td><td>${w.confidence||""}</td><td>${escHtml(w.description||"")}</td></tr>`).join("")}
</tbody></table>` : ""}
${(conformance?.altQualityIssues||[]).length > 0 ? `<h2>Alt Text Quality Review (WCAG 1.1.1 — AI Scored)</h2>
<p>Claude Haiku scored each image description on a 1–5 scale. Descriptions scoring below 4 are listed below.</p>
<table><thead><tr><th>Page</th><th>Score</th><th>Current Alt</th><th>AI Suggestion</th></tr></thead><tbody>
${(conformance?.altQualityIssues||[]).slice(0,20).map(a=>`<tr><td>${a.page||""}</td><td>${a.score}/5</td><td>${escHtml((a.current_alt||"").slice(0,60))}</td><td>${escHtml((a.suggestion||"").slice(0,80))}</td></tr>`).join("")}
</tbody></table>` : ""}
${(conformance?.abbreviations||[]).length > 0 ? `<h2>Abbreviations &amp; Acronyms (WCAG 3.1.4 — Advisory)</h2>
<p>Consider providing expansions on first use or in a glossary.</p>
<table><thead><tr><th>Abbreviation</th><th>Occurrences</th><th>Type</th></tr></thead><tbody>
${(conformance?.abbreviations||[]).slice(0,30).map(a=>`<tr><td>${escHtml(a.abbreviation)}</td><td>${a.count}</td><td>${escHtml(a.type)}</td></tr>`).join("")}
</tbody></table>` : ""}
${conformance?.readingLevel?.grade_level ? `<h2>Reading Level (WCAG 3.1.5 — Advisory)</h2>
<p>Flesch-Kincaid Grade ${conformance.readingLevel.grade_level} (Flesch ease: ${conformance.readingLevel.flesch_ease}) — ${escHtml(conformance.readingLevel.description||"")}</p>` : ""}
<h2>Standards Checked</h2>
<ul>
  <li>WCAG 2.2 Level AA</li>
  <li>PDF/UA-1 (ISO 14289-1) via veraPDF</li>
  <li>WCAG 1.4.3 — Contrast (Minimum)</li>
  <li>WCAG 1.4.11 — Non-text Contrast</li>
  <li>WCAG 2.5.8 — Target Size (Minimum)</li>
  <li>WCAG 2.4.4 — Link Purpose (In Context)</li>
  <li>WCAG 1.3.1 — Info and Relationships (structure tags)</li>
  <li>WCAG 1.3.2 — Meaningful Sequence (reading order)</li>
  <li>WCAG 1.4.10 — Reflow</li>
  <li>WCAG 1.4.12 — Text Spacing</li>
  <li>WCAG 2.4.2 — Page Titled</li>
  <li>WCAG 3.1.1 — Language of Page</li>
  <li>WCAG 3.1.2 — Language of Parts (per-section /Lang)</li>
  <li>WCAG 3.1.4 — Abbreviations (Advisory)</li>
  <li>WCAG 3.1.5 — Reading Level (Advisory)</li>
  <li>WCAG 4.1.2 — Name, Role, Value (form fields)</li>
  <li>WCAG 1.1.1 — Non-text Content (alt text + AI quality scoring)</li>
  <li>WCAG 1.4.1 — Use of Color (color-only detection)</li>
  <li>WCAG 1.3.3 — Sensory Characteristics</li>
  <li>WCAG 1.4.5 — Images of Text</li>
  <li>WCAG 2.5.3 — Label in Name</li>
  <li>PDF/UA-1 clause 7.9 — TOC/TOCI structure elements</li>
  <li>Font embedding and ToUnicode mapping</li>
  <li>PDF metadata completeness (/Title, /Lang, /Author)</li>
</ul>
<p style="color:#6b7280;font-size:.8rem;margin-top:40px">
  This report was generated automatically by PDF Accessibility Remediation Engine.
  Automated checks cannot substitute for manual expert review, particularly for
  reading order, complex tables, scanned content, and mathematical notation.
</p>
</body>
</html>`;
}

// ── Screen: Done ───────────────────────────────────────────────────────────

function DoneScreen({ result, onReset }) {
  const { conformance, score, filename, manifest } = result || {};

  const [activeBlobUrl, setActiveBlobUrl] = useState(result?.downloadUrl || "");
  const [activeBlob, setActiveBlob] = useState(null);
  const [patchBusy, setPatchBusy] = useState(false);
  const [patchError, setPatchError] = useState("");
  const [patchSuccess, setPatchSuccess] = useState("");
  const [patchHeadingResult, setPatchHeadingResult] = useState(null);
  const [metaFormOpen, setMetaFormOpen] = useState(false);
  const [metaTitle, setMetaTitle] = useState(
    conformance?.metadataIssues?.find(i => i.field === "Title") ? (filename || "").replace(/\.accessible\.pdf$/i, "") : ""
  );
  const [metaLang, setMetaLang] = useState("en");

  const downloadUrl = activeBlobUrl || result?.downloadUrl || "";

  async function _getCurrentBlob() {
    if (activeBlob) return activeBlob;
    const res = await fetch(downloadUrl);
    return res.blob();
  }

  async function applyPatch(action, params = {}) {
    setPatchBusy(true); setPatchError(""); setPatchSuccess("");
    try {
      const blob = await _getCurrentBlob();
      const { blob: patched, result: patchResult } = await apiPatch(blob, filename, action, params);
      const newUrl = URL.createObjectURL(patched);
      if (activeBlob && activeBlobUrl) URL.revokeObjectURL(activeBlobUrl);
      setActiveBlob(patched); setActiveBlobUrl(newUrl);
      return patchResult;
    } catch (e) { setPatchError(String(e.message || e)); return null; }
    finally { setPatchBusy(false); }
  }

  async function handleMetaPatch(e) {
    e.preventDefault();
    const res = await applyPatch("metadata", { title: metaTitle, lang: metaLang });
    if (res?.ok) { setMetaFormOpen(false); setPatchSuccess(`Metadata updated: ${(res.patched_fields || []).join(", ")}.`); }
  }

  async function handleHeadingPatch() {
    const res = await applyPatch("headings");
    if (res?.ok) { setPatchHeadingResult(res); setPatchSuccess(res.repairs_made > 0 ? `${res.repairs_made} heading level${res.repairs_made !== 1 ? "s" : ""} corrected.` : "Heading levels are already contiguous."); }
  }

  async function handleTablePatch() {
    const res = await applyPatch("tables");
    if (res?.ok) {
      setPatchSuccess(
        res.repairs_made > 0
          ? `Table headers fixed: ${res.repairs_made} TH cell${res.repairs_made !== 1 ? "s" : ""} assigned /Scope across ${res.tables_found} table${res.tables_found !== 1 ? "s" : ""}.`
          : res.tables_found > 0
            ? "Table header scopes are already set."
            : "No tagged tables found in this PDF."
      );
    }
  }

  // ── Quick Fix All ─────────────────────────────────────────────────────
  const [qfBusy, setQfBusy] = useState(false);
  // Seed from result.qfResult so auto-quickfix (doRemediate) shows ✓ Applied immediately
  const [qfResult, setQfResult] = useState(result?.qfResult || null);

  async function handleQuickFix() {
    setQfBusy(true); setPatchError(""); setPatchSuccess("");
    try {
      const blob = await _getCurrentBlob();
      const { blob: fixed, result: qr } = await apiQuickfix(blob, filename);
      const newUrl = URL.createObjectURL(fixed);
      if (activeBlob && activeBlobUrl) URL.revokeObjectURL(activeBlobUrl);
      setActiveBlob(fixed); setActiveBlobUrl(newUrl);
      setQfResult(qr);
      const total = qr?.totalFixes ?? 0;
      setPatchSuccess(
        total > 0
          ? `Quick Fix applied ${total} fix${total !== 1 ? "es" : ""} automatically.`
          : "Quick Fix ran — no additional fixes needed."
      );
    } catch (e) { setPatchError(String(e.message || e)); }
    finally { setQfBusy(false); }
  }

  // ── Reading order editor ──────────────────────────────────────────────
  const [roOpen, setRoOpen] = useState(false);
  const [roElements, setRoElements] = useState([]);
  const [roOrder, setRoOrder] = useState([]);  // current id order
  const [roBusy, setRoBusy] = useState(false);
  const [roError, setRoError] = useState("");
  const [roSuccess, setRoSuccess] = useState("");
  const [roDragIdx, setRoDragIdx] = useState(null);

  async function handleOpenRO() {
    setRoOpen(true); setRoError(""); setRoSuccess("");
    if (roElements.length > 0) return; // already loaded
    setRoBusy(true);
    try {
      const blob = await _getCurrentBlob();
      const data = await getReadingOrder(blob, filename);
      setRoElements(data.elements || []);
      setRoOrder((data.elements || []).map(e => e.id));
    } catch (e) { setRoError(String(e.message || e)); }
    finally { setRoBusy(false); }
  }

  function handleRoDragStart(idx) { setRoDragIdx(idx); }
  function handleRoDragOver(e, idx) {
    e.preventDefault();
    if (roDragIdx === null || roDragIdx === idx) return;
    const next = [...roOrder];
    const [moved] = next.splice(roDragIdx, 1);
    next.splice(idx, 0, moved);
    setRoOrder(next);
    setRoDragIdx(idx);
  }
  function handleRoDragEnd() { setRoDragIdx(null); }

  async function handleApplyRO() {
    setRoBusy(true); setRoError(""); setRoSuccess("");
    try {
      const blob = await _getCurrentBlob();
      const { blob: reordered, result: roResult } = await apiReorder(blob, filename, roOrder);
      const newUrl = URL.createObjectURL(reordered);
      if (activeBlob && activeBlobUrl) URL.revokeObjectURL(activeBlobUrl);
      setActiveBlob(reordered); setActiveBlobUrl(newUrl);
      setRoSuccess(`Reading order saved — ${roResult?.changes_made || 0} element${roResult?.changes_made !== 1 ? "s" : ""} moved.`);
    } catch (e) { setRoError(String(e.message || e)); }
    finally { setRoBusy(false); }
  }

  const report = conformance?.report || {};
  const contrastFailures = conformance?.contrastFailures || [];
  const contrastCount = conformance?.contrastCount || 0;
  const contrastFixes = conformance?.contrastFixes || 0;
  const linkIssues = conformance?.linkQualityIssues || [];
  const linkCount = conformance?.linkQualityCount || 0;
  const bgFixed = conformance?.backgroundFillsWhitened || 0;
  const hfArtifacts = conformance?.headerFooterArtifacts || 0;
  const readingOrderFixed = conformance?.readingOrderFixed || 0;
  const langAnnotations = conformance?.langAnnotations || 0;
  const linkFixedCount = conformance?.linkQualityFixedCount || 0;
  const formFixed = conformance?.formFixed || 0;
  const formTotal = conformance?.formTotal || 0;
  const altIssues = conformance?.altIssues || [];
  const altIssueCount = conformance?.altIssueCount || 0;
  const colorOnlyWarnings = conformance?.colorOnlyWarnings || [];
  const colorOnlyCount = conformance?.colorOnlyCount || 0;
  const headingIssues = conformance?.headingIssues || [];
  const headingIssueCount = conformance?.headingIssueCount || 0;
  const sensoryIssues = conformance?.sensoryIssues || [];
  const sensoryIssueCount = conformance?.sensoryIssueCount || 0;
  const aiStats = manifest?.source?.aiAnalysis || {};
  const labelNameIssues = conformance?.labelNameIssues || [];
  const labelNameIssueCount = conformance?.labelNameIssueCount || 0;
  const footnotePairsWired = conformance?.footnotePairsWired || 0;
  const tocItemsTagged = conformance?.tocItemsTagged || 0;
  const nestedListsFixed = conformance?.nestedListsFixed || 0;
  // Sprint 8
  const nontextContrastIssues = conformance?.nontextContrastIssues || [];
  const nontextContrastCount = conformance?.nontextContrastCount ?? 0;
  const nontextContrastFixed = conformance?.nontextContrastFixed ?? 0;
  const targetSizeIssues = conformance?.targetSizeIssues || [];
  const xfaWarning = conformance?.xfaWarning || null;
  const fontIssues = conformance?.fontIssues || [];
  const orphanedPages = conformance?.structCompleteness?.orphaned_count ?? 0;
  // Sprint 9
  const reflowIssues = conformance?.reflowIssues || [];
  const metadataIssues = conformance?.metadataIssues || [];
  const metadataErrors = metadataIssues.filter(i => i.severity === "error");
  // Round-trip verification
  const roundTripFailures = conformance?.roundTrip?.failures ?? [];
  const roundTripErrors = roundTripFailures.filter(f => f.severity === "error");
  const roundTripWarnings = roundTripFailures.filter(f => f.severity === "warning");
  // Sprint 11
  const watermarkCandidates = conformance?.watermarkCandidates || [];
  const abbreviations = conformance?.abbreviations || [];
  const readingLevel = conformance?.readingLevel || {};
  // Sprint 12/13
  const tableSummaries = conformance?.tableSummaries || 0;
  const altQualityIssues = conformance?.altQualityIssues || [];
  // Sprint 14-18
  const annotContentsFixed = conformance?.annotContentsFixed || 0;
  const radioGroupsFixed = conformance?.radioGroupsFixed || 0;
  const verapdfRepairs = conformance?.verapdfRepairs || 0;
  const verapdfRepairNotes = conformance?.verapdfRepairNotes || [];
  const structCompleteness = conformance?.structCompleteness || {};
  const pdfuaMetadata = conformance?.pdfuaMetadata || {};
  // Sprint 19-25
  const captionsLinked = conformance?.captionsLinked || 0;
  const formulasTagged = conformance?.formulasTagged || 0;
  const aiAltGenerated = conformance?.aiAltGenerated || 0;
  const contrastRepairs = conformance?.contrastRepairs || 0;
  const fontsEmbedded = conformance?.fontsEmbedded || 0;
  const ocgIssues = conformance?.ocgIssues || [];
  const security = conformance?.security || {};
  const linkTextIssues = conformance?.linkTextIssues || [];
  const linkTextIssueCount = conformance?.linkTextIssueCount || 0;
  const tableStructureIssues = conformance?.tableStructureIssues || [];
  const tableStructureIssueCount = conformance?.tableStructureIssueCount || 0;
  const languageIssues = conformance?.languageIssues || [];
  const languageIssueCount = conformance?.languageIssueCount || 0;
  const pass = conformance?.compliant && score === 100 && contrastCount === 0 && linkCount === 0
    && headingIssueCount === 0 && labelNameIssueCount === 0
    && metadataErrors.length === 0 && fontIssues.filter(i => i.severity === "error").length === 0;
  const [showPreview, setShowPreview] = useState(true);
  const headingRef = useRef();

  useEffect(() => { headingRef.current?.focus(); }, []);

  const handleAuditDownload = () => {
    const html = buildAuditReport({ conformance, score, filename, manifest });
    const blob = new Blob([html], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = (filename || "document").replace(/\.pdf$/i, "") + ".audit-report.html";
    a.click();
    URL.revokeObjectURL(url);
  };

  const fixed = [];
  if (report.elements > 0) fixed.push(`${report.elements} elements tagged for screen readers`);
  if (report.bookmarks > 0) fixed.push(`${report.bookmarks} bookmark${report.bookmarks !== 1 ? "s" : ""} created`);
  if (report.headers > 0) fixed.push(`${report.headers} table header${report.headers !== 1 ? "s" : ""} added`);
  if (report.figures > 0) fixed.push(`${report.figures} image${report.figures !== 1 ? "s" : ""} described`);
  if (hfArtifacts > 0) fixed.push(`${hfArtifacts} running header/footer${hfArtifacts !== 1 ? "s" : ""} hidden from screen readers`);
  if (report.artifacts_decorative > 0) fixed.push(`${report.artifacts_decorative} decorative element${report.artifacts_decorative !== 1 ? "s" : ""} hidden from assistive tech`);
  if (report.links_tagged > 0) fixed.push(`${report.links_tagged} link${report.links_tagged !== 1 ? "s" : ""} tagged`);
  if (bgFixed > 0) fixed.push(`${bgFixed} background fill${bgFixed !== 1 ? "s" : ""} neutralized for contrast`);
  if (contrastFixes > 0) fixed.push(`${contrastFixes} contrast issue${contrastFixes !== 1 ? "s" : ""} corrected`);
  if (readingOrderFixed > 0) fixed.push(`${readingOrderFixed} element${readingOrderFixed !== 1 ? "s" : ""} reordered for logical reading sequence`);
  if (langAnnotations > 0) fixed.push(`${langAnnotations} passage${langAnnotations !== 1 ? "s" : ""} annotated with language (WCAG 3.1.2)`);
  if (linkFixedCount > 0) fixed.push(`${linkFixedCount} link${linkFixedCount !== 1 ? "s" : ""} given descriptive accessible names (WCAG 2.4.4)`);
  if (formFixed > 0) fixed.push(`${formFixed} form field${formFixed !== 1 ? "s" : ""} given accessible names (WCAG 4.1.2)`);
  if ((aiStats.reading_order_corrections || 0) > 0) fixed.push(`AI corrected reading order for ${aiStats.reading_order_corrections} page${aiStats.reading_order_corrections !== 1 ? "s" : ""} (sidebars/callouts)`);
  if ((aiStats.header_cells_updated || 0) > 0) fixed.push(`AI identified ${aiStats.header_cells_updated} table header cell${aiStats.header_cells_updated !== 1 ? "s" : ""} with scope`);
  if ((aiStats.formulas_described || 0) > 0) fixed.push(`AI described ${aiStats.formulas_described} math formula${aiStats.formulas_described !== 1 ? "s" : ""} for screen readers`);
  if ((aiStats.captions_grouped || 0) > 0) fixed.push(`AI grouped ${aiStats.captions_grouped} caption${aiStats.captions_grouped !== 1 ? "s" : ""} with their figure`);
  if ((aiStats.layout_tables_detected || 0) > 0) fixed.push(`AI marked ${aiStats.layout_tables_detected} layout table${aiStats.layout_tables_detected !== 1 ? "s" : ""} as artifacts`);
  if ((aiStats.list_items_split || 0) > 0) fixed.push(`AI split ${aiStats.list_items_split} list item${aiStats.list_items_split !== 1 ? "s" : ""} into Lbl/LBody`);
  if (footnotePairsWired > 0) fixed.push(`${footnotePairsWired} footnote pair${footnotePairsWired !== 1 ? "s" : ""} wired for bidirectional navigation (WCAG 2.4.4)`);
  if (tocItemsTagged > 0) fixed.push(`${tocItemsTagged} table of contents item${tocItemsTagged !== 1 ? "s" : ""} tagged as TOC/TOCI (PDF/UA 7.9)`);
  if (nestedListsFixed > 0) fixed.push(`${nestedListsFixed} nested list${nestedListsFixed !== 1 ? "s" : ""} repaired for proper L/LI/LBody structure`);
  if (tableSummaries > 0) fixed.push(`AI generated ${tableSummaries} table summar${tableSummaries !== 1 ? "ies" : "y"} for screen reader announcement (WCAG 1.3.1)`);
  if (altQualityIssues.filter(i => i.score <= 2).length > 0) fixed.push(`AI improved ${altQualityIssues.filter(i => i.score <= 2).length} low-quality alt text description${altQualityIssues.filter(i => i.score <= 2).length !== 1 ? "s" : ""} (WCAG 1.1.1)`);
  if (pdfuaMetadata?.xmpMetadata && pdfuaMetadata.xmpMetadata !== "already_present") fixed.push("PDF/UA-1 XMP identifier written (pdfuaid:part=1)");
  if (pdfuaMetadata?.markInfo) fixed.push("MarkInfo /Marked set — structure tree declared to reader software");
  if (pdfuaMetadata?.displayDocTitle) fixed.push("ViewerPreferences /DisplayDocTitle — title bar shows document title");
  if (annotContentsFixed > 0) fixed.push(`${annotContentsFixed} annotation${annotContentsFixed !== 1 ? "s" : ""} given accessible descriptions (PDF/UA §7.18.1)`);
  if (radioGroupsFixed > 0) fixed.push(`${radioGroupsFixed} radio button group${radioGroupsFixed !== 1 ? "s" : ""} structured for screen readers`);
  if (verapdfRepairs > 0) fixed.push(`${verapdfRepairs} additional PDF/UA clause${verapdfRepairs !== 1 ? "s" : ""} auto-repaired post-validation`);
  if (captionsLinked > 0) fixed.push(`${captionsLinked} figure caption${captionsLinked !== 1 ? "s" : ""} linked as Caption struct elements (PDF/UA 7.3)`);
  if (formulasTagged > 0) fixed.push(`${formulasTagged} mathematical expression${formulasTagged !== 1 ? "s" : ""} tagged as Formula with text alternative (PDF/UA 7.8)`);
  if (aiAltGenerated > 0) fixed.push(`AI generated alt text for ${aiAltGenerated} figure${aiAltGenerated !== 1 ? "s" : ""} using Claude Vision (WCAG 1.1.1)`);
  if (contrastRepairs > 0) fixed.push(`${contrastRepairs} low-contrast text colour${contrastRepairs !== 1 ? "s" : ""} auto-corrected in content stream (WCAG 1.4.3)`);
  if (fontsEmbedded > 0) fixed.push(`${fontsEmbedded} font${fontsEmbedded !== 1 ? "s" : ""} embedded for reliable text rendering (PDF/UA 7.21)`);

  const autoMode = result?.autoMode || false;
  const qfAutoResult = result?.qfResult || null;

  return (
    <main id="main-content" className="screen screen-preview">
      <div className="screen-card">
        {autoMode && (
          <div className="auto-fix-badge" role="status" aria-label="AI Auto-Fix mode — AI made all accessibility decisions">
            <span aria-hidden="true">⚡</span> AI Auto-Fix
            {qfAutoResult && qfAutoResult.totalFixes > 0 && (
              <span className="auto-fix-count"> · {qfAutoResult.totalFixes} fix{qfAutoResult.totalFixes !== 1 ? "es" : ""} applied</span>
            )}
          </div>
        )}
        <span className="done-icon" aria-hidden="true">{pass ? "✅" : "📄"}</span>
        <h1 ref={headingRef} tabIndex={-1} className="done-headline">{pass ? "PDF is accessible" : "PDF remediated"}</h1>
        {score != null && (
          <>
            <div className="done-score" aria-label={`Conformance score ${score} percent`}>{score}%</div>
            <p className="done-score-label">WCAG 2.2 AA conformance</p>
          </>
        )}

        {conformance && (
          <div className={`verapdf-badge ${conformance.compliant ? "verapdf-pass" : "verapdf-fail"}`}
               role="status"
               aria-label={conformance.compliant ? "PDF/UA-1 passed" : `PDF/UA-1: ${conformance.failedRules} rule${conformance.failedRules !== 1 ? "s" : ""} need fixing`}>
            {conformance.compliant
              ? <><span aria-hidden="true">✓</span> PDF/UA-1 Passed</>
              : <>PDF/UA-1: {conformance.failedRules} rule{conformance.failedRules !== 1 ? "s" : ""} need fixing — Quick Fix All handles this</>}
          </div>
        )}
        {patchSuccess && <div className="patch-success" role="status"><span aria-hidden="true">✓ </span>{patchSuccess}</div>}
        {patchError && <div className="patch-error" role="alert">{patchError}</div>}

        {fixed.length > 0 && (
          <ul className="fixed-list" aria-label="What was fixed">
            {fixed.map((item, i) => <li key={i}>{item}</li>)}
          </ul>
        )}

        {contrastCount > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{contrastCount} contrast issue{contrastCount !== 1 ? "s" : ""} to resolve (WCAG 1.4.3)</strong>
            <p>These text colors use complex color spaces or rendering modes that couldn't be auto-corrected.</p>
            {contrastFailures.slice(0, 5).map((f, i) => (
              <div key={i} className="contrast-item">
                Page {f.page}: "{String(f.text || "").slice(0, 40)}" — {f.ratio}:1 (need {f.required}:1)
              </div>
            ))}
            {contrastCount > 5 && <div className="contrast-item">… and {contrastCount - 5} more (see audit report)</div>}
          </div>
        )}

        {fontIssues.filter(f => f.severity === "error").length > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{fontIssues.filter(f => f.severity === "error").length} font issue{fontIssues.filter(f => f.severity === "error").length !== 1 ? "s" : ""} to fix (PDF/UA §7.21.3)</strong>
            <p>These fonts lack a ToUnicode map or are not embedded. Quick Fix All attempts to embed and repair them.</p>
            {fontIssues.filter(f => f.severity === "error").slice(0, 5).map((f, i) => (
              <div key={i} className="contrast-item">{f.font_name}: {f.description}</div>
            ))}
          </div>
        )}

        {(nontextContrastCount > 0 || nontextContrastFixed > 0) && (
          <div className="contrast-warn" role="note">
            <strong>{nontextContrastCount} non-text contrast issue{nontextContrastCount !== 1 ? "s" : ""} (WCAG 1.4.11)</strong>
            {nontextContrastFixed > 0 && <p>{nontextContrastFixed} auto-fixed (borders darkened / near-white fills cleared).</p>}
            {nontextContrastCount > 0 && <p>Use Quick Fix All to auto-correct remaining issues.</p>}
          </div>
        )}

        {orphanedPages > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{orphanedPages} page{orphanedPages !== 1 ? "s" : ""} with unstructured content (PDF/UA §7.1)</strong>
            <p>Some content isn't in the structure tree. Quick Fix All and manual re-tagging can resolve this.</p>
          </div>
        )}

        {roundTripErrors.length > 0 && (
          <div className="contrast-warn" role="alert">
            <strong>{roundTripErrors.length} writeback check{roundTripErrors.length !== 1 ? "s" : ""} to verify</strong>
            <p>The following properties were not found in the output PDF — writeback may have silently failed:</p>
            {roundTripErrors.map((f, i) => (
              <div key={i} className="contrast-item">[{f.check}] {f.description}</div>
            ))}
          </div>
        )}

        {roundTripWarnings.length > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{roundTripWarnings.length} output PDF note{roundTripWarnings.length !== 1 ? "s" : ""}</strong>
            {roundTripWarnings.map((f, i) => (
              <div key={i} className="contrast-item">[{f.check}] {f.description}</div>
            ))}
          </div>
        )}

        {linkCount > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{linkCount} link{linkCount !== 1 ? "s" : ""} need accessible names (WCAG 2.4.4)</strong>
            <p>These links have no resolvable URL. Add descriptive text to the link in your source, or use Quick Fix All to apply defaults.</p>
            {linkIssues.slice(0, 4).map((l, i) => (
              <div key={i} className="contrast-item">
                Page {l.page}: {l.issue}
              </div>
            ))}
            {linkCount > 4 && <div className="contrast-item">… and {linkCount - 4} more (see audit report)</div>}
          </div>
        )}

        {altIssueCount > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{altIssueCount} image{altIssueCount !== 1 ? "s" : ""} need review (WCAG 1.1.1)</strong>
            <p>These figures need alt text. Quick Fix All generates descriptions using AI vision.</p>
            {altIssues.slice(0, 4).map((a, i) => (
              <div key={i} className="contrast-item">
                {a.page ? `Page ${a.page}: ` : ""}{a.description}
              </div>
            ))}
            {altIssueCount > 4 && <div className="contrast-item">… and {altIssueCount - 4} more (see audit report)</div>}
          </div>
        )}

        {colorOnlyCount > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{colorOnlyCount} color-only pattern{colorOnlyCount !== 1 ? "s" : ""} to review (WCAG 1.4.1)</strong>
            <p>Colored shapes that may convey meaning through color alone. Quick Fix All adds accessible labels where possible.</p>
            {colorOnlyWarnings.slice(0, 3).map((w, i) => (
              <div key={i} className="contrast-item">
                Page {w.page}: {w.swatch_count} shapes — {w.colors.slice(0,5).join(", ")}{w.colors.length > 5 ? "…" : ""}
              </div>
            ))}
            {colorOnlyCount > 3 && <div className="contrast-item">… and {colorOnlyCount - 3} more (see audit report)</div>}
          </div>
        )}

        {headingIssueCount > 0 && !patchHeadingResult?.repairs_made && (
          <div className="contrast-warn" role="note">
            <strong>{headingIssueCount} heading structure issue{headingIssueCount !== 1 ? "s" : ""} to fix (WCAG 1.3.1 / 2.4.6)</strong>
            <p>The heading hierarchy has skipped levels. The structure tree can be auto-corrected to close gaps.</p>
            {headingIssues.slice(0, 4).map((h, i) => (
              <div key={i} className="contrast-item">{h.description}</div>
            ))}
            {headingIssueCount > 4 && <div className="contrast-item">… and {headingIssueCount - 4} more (see audit report)</div>}
            <button className="fix-btn" onClick={handleHeadingPatch} disabled={patchBusy}>
              {patchBusy ? "Fixing…" : "Auto-fix Heading Levels"}
            </button>
          </div>
        )}

        {patchHeadingResult?.repairs_made > 0 && (
          <div className="contrast-warn contrast-warn--ok" role="note">
            <strong><span aria-hidden="true">✓ </span>Heading levels corrected — {patchHeadingResult.repairs_made} level{patchHeadingResult.repairs_made !== 1 ? "s" : ""} adjusted</strong>
            {patchHeadingResult.changes?.slice(0, 4).map((c, i) => (
              <div key={i} className="contrast-item">H{c.from} → H{c.to}{c.text_preview ? `: "${c.text_preview}"` : ""}</div>
            ))}
          </div>
        )}

        {sensoryIssueCount > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{sensoryIssueCount} sensory-only reference{sensoryIssueCount !== 1 ? "s" : ""} to review (WCAG 1.3.3)</strong>
            <p>Instructions that reference only shape, color, size, or visual position may be inaccessible. Quick Fix All adds accessible labels. For complex references, the manual path lets you review each one.</p>
            {sensoryIssues.slice(0, 4).map((s, i) => (
              <div key={i} className="contrast-item">
                {s.page ? `Page ${s.page}: ` : ""}"{s.match}"
              </div>
            ))}
            {sensoryIssueCount > 4 && <div className="contrast-item">… and {sensoryIssueCount - 4} more (see audit report)</div>}
          </div>
        )}

        {linkTextIssueCount > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{linkTextIssueCount} non-descriptive link{linkTextIssueCount !== 1 ? "s" : ""} to fix (WCAG 2.4.4)</strong>
            <p>Links with generic text. Quick Fix All rewrites these with AI-generated descriptions based on the link destination.</p>
            {linkTextIssues.slice(0, 4).map((l, i) => (
              <div key={i} className="contrast-item">
                {l.page ? `Page ${l.page}: ` : ""}"{l.linkText}"
                {l.uri ? ` → ${l.uri.slice(0, 60)}` : ""}
              </div>
            ))}
            {linkTextIssueCount > 4 && <div className="contrast-item">… and {linkTextIssueCount - 4} more (see audit report)</div>}
          </div>
        )}

        {tableStructureIssueCount > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{tableStructureIssueCount} table structure issue{tableStructureIssueCount !== 1 ? "s" : ""} to fix (WCAG 1.3.1 / PDF/UA §7.5)</strong>
            <p>Tables are missing header cells, scope attributes, or captions. Screen readers cannot convey table relationships without proper markup.</p>
            {tableStructureIssues.slice(0, 4).map((t, i) => (
              <div key={i} className="contrast-item">
                {t.page ? `Page ${t.page}: ` : ""}{t.type.replace(/_/g, " ")}
                {t.count ? ` (${t.count} cell${t.count !== 1 ? "s" : ""})` : ""}
              </div>
            ))}
            {tableStructureIssueCount > 4 && <div className="contrast-item">… and {tableStructureIssueCount - 4} more (see audit report)</div>}
          </div>
        )}

        {languageIssueCount > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{languageIssueCount} language tagging issue{languageIssueCount !== 1 ? "s" : ""} to fix (WCAG 3.1.1 / 3.1.2)</strong>
            <p>The document language is missing or language changes within the content are not marked. Screen readers need language tags to switch to the correct speech synthesizer voice.</p>
            {languageIssues.slice(0, 4).map((l, i) => (
              <div key={i} className="contrast-item">
                {l.type === "missing_doc_lang" ? "Document /Lang not set" :
                  `${l.page ? `Page ${l.page}: ` : ""}Suspected ${l.detectedScript} text without /Lang — "${(l.text || "").slice(0, 60)}"`}
              </div>
            ))}
            {languageIssueCount > 4 && <div className="contrast-item">… and {languageIssueCount - 4} more (see audit report)</div>}
          </div>
        )}


        {labelNameIssueCount > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{labelNameIssueCount} form field{labelNameIssueCount !== 1 ? "s" : ""} fail Label in Name (WCAG 2.5.3)</strong>
            <p>The visible label for these fields is not present in their accessible name. Voice control users cannot activate them by speaking the visible label.</p>
            {labelNameIssues.slice(0, 4).map((l, i) => (
              <div key={i} className="contrast-item">
                {l.page ? `Page ${l.page}: ` : ""}"{l.visible_label}" → accessible name "{l.accessible_name}"
              </div>
            ))}
            {labelNameIssueCount > 4 && <div className="contrast-item">… and {labelNameIssueCount - 4} more (see audit report)</div>}
          </div>
        )}

        {xfaWarning && (
          <div className="contrast-warn" role="note">
            <strong>XFA Form Detected — Manual Remediation Required</strong>
            <p>This PDF contains an XFA (XML Forms Architecture) form which cannot be made accessible through structural tagging alone. XFA forms must be rebuilt as AcroForm PDFs or converted to an accessible HTML/WCAG-conformant format. Assistive technologies cannot reliably access XFA content.</p>
          </div>
        )}

        {metadataErrors.length > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{metadataErrors.length} Metadata Error{metadataErrors.length !== 1 ? "s" : ""} (PDF/UA §7.1)</strong>
            {metadataErrors.slice(0, 3).map((m, i) => (
              <div key={i} className="contrast-item">{m.description}</div>
            ))}
            {!metaFormOpen && (
              <button className="fix-btn" onClick={() => setMetaFormOpen(true)} disabled={patchBusy}>
                Fix Now — Set Title &amp; Language
              </button>
            )}
            {metaFormOpen && (
              <form className="patch-form" onSubmit={handleMetaPatch}>
                <label className="patch-label">Document Title
                  <input className="patch-input" type="text" value={metaTitle}
                    onChange={e => setMetaTitle(e.target.value)} placeholder="e.g. Annual Report 2026" required />
                </label>
                <label className="patch-label">Language Code
                  <input className="patch-input" type="text" value={metaLang}
                    onChange={e => setMetaLang(e.target.value)} placeholder="e.g. en, en-US, fr" required />
                </label>
                <div className="patch-form-actions">
                  <button type="submit" className="fix-btn" disabled={patchBusy}>{patchBusy ? "Applying…" : "Apply Metadata Fix"}</button>
                  <button type="button" className="ghost" onClick={() => setMetaFormOpen(false)} disabled={patchBusy}>Cancel</button>
                </div>
              </form>
            )}
          </div>
        )}

        {fontIssues.filter(f => f.severity === "error").length > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{fontIssues.filter(f => f.severity === "error").length} Font Embedding / ToUnicode Error{fontIssues.filter(f => f.severity === "error").length !== 1 ? "s" : ""}</strong>
            <p>Fonts without proper embedding or ToUnicode maps prevent screen readers from reading the text. These must be fixed in the source document.</p>
            {fontIssues.filter(f => f.severity === "error").slice(0, 3).map((f, i) => (
              <div key={i} className="contrast-item">"{f.font_name}": {f.description}</div>
            ))}
          </div>
        )}

        {(nontextContrastIssues.length > 0 || nontextContrastFixed > 0) && (
          <div className="contrast-warn" role="note">
            <strong>Non-text Contrast (WCAG 1.4.11)</strong>
            {nontextContrastFixed > 0 && (
              <div className="contrast-item">✓ {nontextContrastFixed} auto-fixed — borders darkened, near-white fills cleared.</div>
            )}
            {nontextContrastIssues.length > 0 && (
              <>
                <p>{nontextContrastIssues.length} remaining issue{nontextContrastIssues.length !== 1 ? "s" : ""} — run Quick Fix All to auto-correct borders and strokes.</p>
                {nontextContrastIssues.slice(0, 2).map((n, i) => (
                  <div key={i} className="contrast-item">Page {n.page}: {n.description}</div>
                ))}
                {nontextContrastIssues.length > 2 && <div className="contrast-item">… and {nontextContrastIssues.length - 2} more (see audit report)</div>}
              </>
            )}
          </div>
        )}

        {altQualityIssues.length > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{altQualityIssues.length} Low-Quality Alt Text Description{altQualityIssues.length !== 1 ? "s" : ""} (WCAG 1.1.1)</strong>
            <p>These image descriptions were flagged as insufficient by AI review. Score ≥4 is good; ≤2 was auto-improved where possible.</p>
            {altQualityIssues.slice(0, 2).map((a, i) => (
              <div key={i} className="contrast-item">Page {a.page}: score {a.score}/5 — "{a.current_alt?.slice(0, 60)}{a.current_alt?.length > 60 ? "…" : ""}"</div>
            ))}
          </div>
        )}

        {security?.severity === "error" && (
          <div className="contrast-warn" role="alert">
            <strong>🔒 Encryption Blocks Accessibility (PDF/UA §7.16)</strong>
            <p>{security.description}</p>
          </div>
        )}

        {ocgIssues.filter(i => i.severity === "warning").length > 0 && (
          <div className="contrast-warn" role="note">
            <strong>{ocgIssues.filter(i => i.severity === "warning").length} Optional Content Layer Issue{ocgIssues.filter(i => i.severity === "warning").length !== 1 ? "s" : ""} (PDF/UA §7.11)</strong>
            <p>Layers hidden by default may conceal content from screen readers. Verify hidden layers are decorative only.</p>
            {ocgIssues.filter(i => i.severity === "warning").slice(0, 2).map((o, i) => (
              <div key={i} className="contrast-item">{o.layer ? `Layer "${o.layer}": ` : ""}{o.description}</div>
            ))}
          </div>
        )}

        {/* ── Reading Order Editor ───────────────────────────────────────── */}
        <div className="ro-section">
          {!roOpen ? (
            <button className="ro-toggle" onClick={handleOpenRO}>
              <span aria-hidden="true">⇅</span> Edit Reading Order
            </button>
          ) : (
            <div className="ro-panel" role="region" aria-label="Reading order editor">
              <div className="ro-header">
                <strong>Reading Order</strong>
                <button className="ghost ro-close" onClick={() => setRoOpen(false)} aria-label="Close reading order editor">✕</button>
              </div>
              {roBusy && <p className="ro-status">Loading…</p>}
              {roError && <p className="patch-error" role="alert">{roError}</p>}
              {roSuccess && <p className="patch-success" role="status">{roSuccess}</p>}
              {roElements.length > 0 && (
                <>
                  <p className="ro-hint">Drag rows to change reading order. Click Apply when done.</p>
                  <ol className="ro-list" aria-label="Document elements in reading order">
                    {roOrder.map((id, idx) => {
                      const el = roElements.find(e => e.id === id);
                      if (!el) return null;
                      return (
                        <li
                          key={id}
                          className={`ro-item${roDragIdx === idx ? " ro-item--dragging" : ""}`}
                          draggable
                          onDragStart={() => handleRoDragStart(idx)}
                          onDragOver={e => handleRoDragOver(e, idx)}
                          onDragEnd={handleRoDragEnd}
                          aria-label={`${el.type}: ${el.preview || "(no text)"}`}
                        >
                          <span className="ro-handle" aria-hidden="true">⠿</span>
                          <span className={`ro-tag ro-tag--${el.tag.replace("/","").toLowerCase()}`}>{el.type}</span>
                          <span className="ro-preview">{el.preview || <em>—</em>}</span>
                        </li>
                      );
                    })}
                  </ol>
                  <div className="ro-actions">
                    <button className="fix-btn" onClick={handleApplyRO} disabled={roBusy}>
                      {roBusy ? "Applying…" : "Apply Reading Order"}
                    </button>
                    <button className="ghost" onClick={() => setRoOrder(roElements.map(e => e.id))} disabled={roBusy}>
                      Reset
                    </button>
                  </div>
                </>
              )}
              {!roBusy && roElements.length === 0 && !roError && (
                <p className="ro-status">No tagged structure elements found.</p>
              )}
            </div>
          )}
        </div>

        {/* ── Quick Fix All ── */}
        <div className="quickfix-section">
          <div className="quickfix-intro">
            <strong>Quick Fix All</strong>
            <span>Runs every auto-fix in one pass — contrast, table scopes, heading levels, metadata, language tags, alt text, and veraPDF repairs. AI makes the calls.</span>
          </div>
          {qfResult && !qfBusy ? (
            <span className="quickfix-btn" style={{background:"var(--good)", cursor:"default"}} aria-label="All fixes applied">✓ Applied</span>
          ) : (
            <button
              className="quickfix-btn"
              onClick={handleQuickFix}
              disabled={qfBusy || patchBusy}
              aria-busy={qfBusy}
            >
              {qfBusy ? "Fixing…" : "⚡ Quick Fix All"}
            </button>
          )}
          {qfResult && (
            <div className="quickfix-result" role="status">
              {Object.entries(qfResult.fixes || {}).map(([k, v]) =>
                v > 0 ? (
                  <span key={k} className="qf-chip">{k.replace(/([A-Z])/g, " $1").trim()}: {v}</span>
                ) : null
              )}
              {(qfResult.notes || []).some(n => /compliant/i.test(n)) && (
                <span className="qf-chip" style={{background:"rgba(63,185,80,.15)", color:"var(--good)", borderColor:"rgba(63,185,80,.3)"}}>PDF/UA-1 ✓</span>
              )}
              {qfResult.errors?.length > 0 && (
                <span className="qf-chip qf-chip--warn">{qfResult.errors.length} step(s) skipped</span>
              )}
            </div>
          )}
        </div>

        <div className="done-actions">
          <a href={downloadUrl} download={filename} aria-label={`Download ${filename}`}>
            Download accessible PDF
          </a>
          <button className="ghost" onClick={handleAuditDownload} aria-label="Download accessibility audit report as HTML">
            Download audit report
          </button>
          <button className="ghost" onClick={() => setShowPreview(s => !s)} aria-expanded={showPreview}>
            {showPreview ? "Hide tagged preview" : "View tagged PDF"}
             </button>
          <button className="ghost" onClick={onReset}>
            Remediate another PDF
          </button>
        </div>
      </div>

      {showPreview && manifest && (
        <div className="preview-panel">
          <PDFPreviewPanel pdfUrl={downloadUrl} manifest={manifest} />
        </div>
      )}
    </main>
  );
}

// ── Screen: Batch ──────────────────────────────────────────────────────────

const BATCH_STATUS_LABEL = {
  queued:      "Queued",
  scanning:    "Scanning…",
  remediating: "Remediating…",
  done:        "Done",
  error:       "Error",
};
const BATCH_STATUS_ICON = {
  queued:      "○",
  scanning:    "◌",
  remediating: "◑",
  done:        "●",
  error:       "✕",
};

function BatchScreen({ onBack }) {
  const [items, setItems] = useState([]);   // { file, status, downloadUrl, downloadName, compliant, failedRules, error }
  const [running, setRunning] = useState(false);
  const [finished, setFinished] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef();
  const headingRef = useRef();

  useEffect(() => { headingRef.current?.focus(); }, []);

  const addFiles = (fileList) => {
    const pdfs = Array.from(fileList)
      .filter(f => f.type === "application/pdf")
      .slice(0, 10);
    if (!pdfs.length) return;
    setItems(pdfs.map(f => ({ file: f, status: "queued" })));
    setFinished(false);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    addFiles(e.dataTransfer.files);
  };

  const runBatch = async () => {
    setRunning(true);
    const update = (i, patch) =>
      setItems(prev => prev.map((it, j) => j === i ? { ...it, ...patch } : it));

    await api.batchRemediateFiles(
      items.map(it => it.file),
      (i, patch) => update(i, patch)
    );

    setRunning(false);
    setFinished(true);
  };

  const noBackend = !api.HAS_BACKEND;
  const doneCount  = items.filter(it => it.status === "done").length;
  const errorCount = items.filter(it => it.status === "error").length;

  return (
    <main id="main-content" className="screen batch-screen">
      <h1 ref={headingRef} tabIndex={-1} className="batch-heading">Batch Remediation</h1>
      <p className="batch-sub">Remediate up to 10 PDFs at once. Alt text questions are skipped — defaults are applied automatically.</p>

      {items.length === 0 ? (
        <div
          className={`batch-drop${dragOver ? " drag-over" : ""}`}
          role="group"
          aria-label="PDF drop zone"
          onDragOver={e => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
        >
          <p className="drop-zone-heading" aria-hidden="true">Drop PDFs here</p>
          <p>Up to 10 files</p>
          <button
            className="primary"
            disabled={noBackend}
            onClick={() => inputRef.current.click()}
          >
            Choose PDFs
          </button>
          <input
            ref={inputRef}
            type="file"
            accept="application/pdf"
            multiple
            style={{ display: "none" }}
            onChange={e => e.target.files.length && addFiles(e.target.files)}
          />
        </div>
      ) : (
        <div className="batch-list" role="list" aria-label="Files to process">
          {items.map((it, i) => (
            <div key={i} className={`batch-row batch-row--${it.status}`} role="listitem">
              <span className="batch-icon" aria-hidden="true">{BATCH_STATUS_ICON[it.status]}</span>
              <span className="batch-name" title={it.file.name}>{it.file.name}</span>
              <span className="batch-status-label">
                {it.status === "done"
                  ? (it.compliant
                      ? <span className="batch-badge batch-badge--pass">✓ PDF/UA-1</span>
                      : <span className="batch-badge batch-badge--fail">✗ {it.failedRules} rules</span>)
                  : it.status === "error"
                    ? <span className="batch-err-text" title={it.error}>Error</span>
                    : <span>{BATCH_STATUS_LABEL[it.status]}</span>
                }
              </span>
              {it.status === "done" && it.downloadUrl && (
                <a
                  href={it.downloadUrl}
                  download={it.downloadName}
                  className="batch-dl"
                  aria-label={`Download ${it.downloadName}`}
                >
                  ↓ Download
                </a>
              )}
            </div>
          ))}

          {finished && (
            <p className="batch-summary" role="status">
              {doneCount} remediated{errorCount > 0 ? `, ${errorCount} failed` : ""}.
            </p>
          )}

          <div className="batch-actions">
            {!running && !finished && (
              <button className="primary" onClick={runBatch}>
                         Process {items.length} file{items.length !== 1 ? "s" : ""}
              </button>
            )}
            {running && (
              <p className="batch-running" role="status" aria-live="polite">Processing…</p>
            )}
            <button
              className="ghost"
              onClick={() => { setItems([]); setFinished(false); }}
              disabled={running}
            >
              Clear
            </button>
          </div>
        </div>
      )}

      <button className="batch-back" onClick={onBack} disabled={running}>
        ← Back
      </button>
    </main>
  );
}
