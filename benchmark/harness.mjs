#!/usr/bin/env node
/**
 * Conformance benchmark harness.
 *
 * Runs every PDF in a corpus directory through the engine's full pipeline
 * (autotag -> remediate) and aggregates the results into the numbers that
 * matter for the "100% WCAG 2.2 AA" goal:
 *
 *   - full-auto pass rate (veraPDF PDF/UA-1 compliant, no guard, verified)
 *   - top failing veraPDF clauses by document count and check count
 *   - our WCAG checker issue frequencies (contrast, fonts, links, ...)
 *   - per-class breakdown (untagged-rebuild vs tagged-repair, forms, scans)
 *
 * Usage:
 *   node benchmark/harness.mjs --engine http://localhost:8000 --corpus ./corpus --out results.json
 *
 * Requires Node 20+ (native fetch / FormData / Blob). Read-only against the
 * engine; corpus PDFs are never modified.
 */

import { readdir, readFile, writeFile } from "node:fs/promises";
import { join, basename } from "node:path";

const args = Object.fromEntries(
  process.argv.slice(2).map((a, i, all) => (a.startsWith("--") ? [a.slice(2), all[i + 1]] : null)).filter(Boolean)
);
const ENGINE = (args.engine || "http://localhost:8000").replace(/\/$/, "");
const CORPUS = args.corpus || "./corpus";
const OUT = args.out || "benchmark-results.json";

async function postPdf(path, bytes, name, extra = {}) {
  const fd = new FormData();
  fd.append("file", new Blob([bytes], { type: "application/pdf" }), name);
  for (const [k, v] of Object.entries(extra)) fd.append(k, v);
  const res = await fetch(`${ENGINE}${path}`, { method: "POST", body: fd });
  return res;
}

async function runOne(file) {
  const bytes = await readFile(join(CORPUS, file));
  const row = { file, sizeKB: Math.round(bytes.length / 1024) };
  const t0 = Date.now();
  try {
    // 1. autotag
    const at = await postPdf("/autotag", bytes, file);
    if (!at.ok) {
      row.stage = "autotag";
      row.error = `${at.status}: ${(await at.text()).slice(0, 160)}`;
      return row;
    }
    const manifest = await at.json();
    row.pages = manifest?.source?.pageCount ?? null;
    row.nodes = manifest?.source?.nodeCount ?? null;
    // crude scan heuristic: pages exist but layout analysis found almost nothing
    row.likelyScan = row.pages > 0 && (row.nodes ?? 0) / row.pages < 2;

    // 2. remediate
    const fd = new FormData();
    fd.append("file", new Blob([bytes], { type: "application/pdf" }), file);
    fd.append("manifest", new Blob([JSON.stringify(manifest)], { type: "application/json" }), "m.json");
    fd.append("flavour", "ua1");
    const rem = await fetch(`${ENGINE}/remediate`, { method: "POST", body: fd });
    if (!rem.ok) {
      row.stage = "remediate";
      row.error = `${rem.status}: ${(await rem.text()).slice(0, 160)}`;
      return row;
    }
    await rem.arrayBuffer(); // drain
    let conf = null;
    try { conf = JSON.parse(rem.headers.get("x-conformance") || "null"); } catch {}
    if (!conf) { row.stage = "conformance"; row.error = "no X-Conformance header"; return row; }

    row.compliant = conf.compliant === true;
    row.validationComplete = conf.validationComplete !== false;
    row.failedRules = conf.failedRules ?? null;
    row.mode = conf.remediationMode || "rebuild";
    row.guard = conf.regressionGuard?.triggered === true;
    row.form = (conf.formTotal ?? 0) > 0;
    row.failures = (conf.failures || []).map(f => ({
      clause: `${f.clause}-${f.test_number ?? f.testNumber ?? "?"}`,
      checks: f.failed_checks ?? f.failedChecks ?? 0,
      desc: (f.description || "").slice(0, 90),
    }));
    row.checkers = {
      contrast: conf.contrastCount ?? 0,
      nontextContrast: conf.nontextContrastCount ?? 0,
      font: conf.fontIssueCount ?? 0,
      altQuality: conf.altIssueCount ?? 0,
      headings: conf.headingIssueCount ?? 0,
      linkText: conf.linkTextIssueCount ?? 0,
      tableStructure: conf.tableStructureIssueCount ?? 0,
      language: conf.languageIssueCount ?? 0,
      metadata: conf.metadataIssueCount ?? 0,
      colorOnly: conf.colorOnlyCount ?? 0,
      targetSize: conf.targetSizeCount ?? 0,
    };
    row.checklistItems = (conf.humanChecklist || []).length;
    row.visualApplied = (conf.visualReview?.applied || []).length;
    row.visualRemaining = (conf.visualReview?.remaining || []).length;
  } catch (e) {
    row.stage = row.stage || "exception";
    row.error = String(e).slice(0, 200);
  } finally {
    row.seconds = Math.round((Date.now() - t0) / 100) / 10;
  }
  return row;
}

function aggregate(rows) {
  const ok = rows.filter(r => !r.error);
  const agg = {
    corpus: rows.length,
    errored: rows.filter(r => r.error).map(r => `${r.file} @${r.stage}: ${r.error}`),
    fullAutoPass: ok.filter(r => r.compliant && r.validationComplete && !r.guard).length,
    verifiedNotCompliant: ok.filter(r => !r.compliant && r.validationComplete).length,
    unverified: ok.filter(r => !r.validationComplete).length,
    guardFired: ok.filter(r => r.guard).length,
    byMode: {},
    clauseFrequency: {},
    checkerTotals: {},
    slowest: [...ok].sort((a, b) => b.seconds - a.seconds).slice(0, 3).map(r => `${r.file} ${r.seconds}s`),
  };
  for (const r of ok) {
    const m = `${r.mode}${r.likelyScan ? "+scan" : ""}${r.form ? "+form" : ""}`;
    agg.byMode[m] ??= { total: 0, pass: 0 };
    agg.byMode[m].total++;
    if (r.compliant) agg.byMode[m].pass++;
    for (const f of r.failures || []) {
      agg.clauseFrequency[f.clause] ??= { docs: 0, checks: 0, desc: f.desc };
      agg.clauseFrequency[f.clause].docs++;
      agg.clauseFrequency[f.clause].checks += f.checks;
    }
    for (const [k, v] of Object.entries(r.checkers || {})) {
      if (v > 0) {
        agg.checkerTotals[k] ??= { docs: 0, issues: 0 };
        agg.checkerTotals[k].docs++;
        agg.checkerTotals[k].issues += v;
      }
    }
  }
  agg.clauseFrequency = Object.fromEntries(
    Object.entries(agg.clauseFrequency).sort((a, b) => b[1].docs - a[1].docs)
  );
  return agg;
}

const files = (await readdir(CORPUS)).filter(f => f.toLowerCase().endsWith(".pdf")).sort();
console.log(`Benchmarking ${files.length} PDFs against ${ENGINE}\n`);
const rows = [];
for (const f of files) {
  const row = await runOne(f);
  rows.push(row);
  const status = row.error
    ? `ERROR @${row.stage}`
    : `${row.compliant ? "PASS" : "fail(" + row.failedRules + "r)"}${row.validationComplete ? "" : " UNVERIFIED"}${row.guard ? " GUARD" : ""} [${row.mode}]`;
  console.log(`${basename(f).padEnd(18)} ${status.padEnd(28)} ${row.seconds ?? "?"}s`);
}
const agg = aggregate(rows);
await writeFile(OUT, JSON.stringify({ engine: ENGINE, date: new Date().toISOString(), rows, agg }, null, 2));
console.log(`\n=== AGGREGATE ===`);
console.log(JSON.stringify(agg, null, 2));
console.log(`\nwritten: ${OUT}`);
