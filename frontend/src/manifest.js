// Manifest helpers for the studio: tree traversal, reading-order flattening,
// and a live conformance score against the WCAG 2.1 AA / PDF-UA checks a human
// can judge in the browser (the authoritative check is veraPDF on the backend).

export const HEADING_TAGS = ["H1", "H2", "H3", "H4", "H5", "H6"];
export const BLOCK_TAGS = [
  "H1", "H2", "H3", "H4", "H5", "H6",
  "P", "Figure", "Caption", "L", "Table", "Artifact", "Formula", "Code", "BlockQuote",
];

/** Depth-first pre-order traversal yielding { node, parent, depth }. */
export function walk(nodes, cb, parent = null, depth = 0) {
  for (const node of nodes || []) {
    cb(node, parent, depth);
    if (node.children) walk(node.children, cb, node, depth + 1);
  }
}

/** Flat list of nodes in reading order with depth, for the structure panel. */
export function flatten(manifest) {
  const out = [];
  walk(manifest?.nodes || [], (node, parent, depth) => out.push({ node, parent, depth }));
  return out;
}

export function findById(manifest, id) {
  let found = null;
  walk(manifest?.nodes || [], (node) => {
    if (node.id === id) found = node;
  });
  return found;
}

function collectFigures(manifest) {
  const figs = [];
  walk(manifest?.nodes || [], (n) => {
    if (n.tag === "Figure") figs.push(n);
  });
  return figs;
}

function collectTables(manifest) {
  const tables = [];
  walk(manifest?.nodes || [], (n) => {
    if (n.tag === "Table") tables.push(n);
  });
  return tables;
}

/**
 * Auto-correct heading level skips in-place (pure — returns a new manifest).
 * Rule: level may only increase by 1 at a time going down; going back up is fine.
 * Example: H1 H3 H2 → H1 H2 H2  (H3 corrected to H2, H2 unchanged)
 */
export function fixHeadingOrder(manifest) {
  let prevLevel = 0;

  function fixNodes(nodes) {
    return (nodes || []).map((node) => {
      let result = node;

      if (node.tag && /^H[1-6]$/.test(node.tag)) {
        const level = parseInt(node.tag[1], 10);
        const maxAllowed = prevLevel === 0 ? 1 : prevLevel + 1;
        const corrected = Math.min(level, maxAllowed);
        prevLevel = corrected;
        if (corrected !== level) {
          result = { ...node, tag: `H${corrected}`, headingLevel: corrected };
        }
      }

      if (result.children && result.children.length > 0) {
        result = { ...result, children: fixNodes(result.children) };
      }

      return result;
    });
  }

  return { ...manifest, nodes: fixNodes(manifest.nodes) };
}

/** Count how many headings would be changed by fixHeadingOrder. */
export function countHeadingFixes(manifest) {
  const fixed = fixHeadingOrder(manifest);
  let count = 0;
  walk(manifest?.nodes || [], (n) => { if (n.tag && /^H[1-6]$/.test(n.tag)) count++; });
  let same = 0;
  walk(fixed?.nodes || [], (n, _, __, orig) => { if (n.tag && /^H[1-6]$/.test(n.tag)) same++; });
  // simpler: just diff tags
  const origTags = [];
  const fixedTags = [];
  walk(manifest?.nodes || [], (n) => { if (/^H[1-6]$/.test(n.tag || "")) origTags.push(n.tag); });
  walk(fixed?.nodes || [], (n) => { if (/^H[1-6]$/.test(n.tag || "")) fixedTags.push(n.tag); });
  return origTags.filter((t, i) => t !== fixedTags[i]).length;
}

/**
 * Live, judgment-level conformance checks. Returns { score, checks: [...] }.
 * score is 0..100 over the applicable checks.
 * Pass contrastPassed=true/false to include contrast as an 8th check (post-remediation).
 */
export function scoreManifest(manifest, { contrastPassed = null } = {}) {
  const doc = manifest?.document || {};
  const figures = collectFigures(manifest);
  const tables = collectTables(manifest);

  const headingLevels = [];
  walk(manifest?.nodes || [], (n) => {
    if (HEADING_TAGS.includes(n.tag)) headingLevels.push(Number(n.tag.slice(1)));
  });

  const figuresResolved = figures.every((f) => f.decorative || (f.alt && f.alt.trim()));
  const tablesHaveHeaders = tables.every((t) => {
    let hasTH = false;
    walk([t], (n) => {
      if (n.tag === "TH") hasTH = true;
    });
    return hasTH;
  });

  // Headings shouldn't skip levels (e.g. H1 -> H3).
  let headingOrderOk = true;
  let prev = 0;
  for (const lvl of headingLevels) {
    if (prev && lvl > prev + 1) headingOrderOk = false;
    prev = lvl;
  }

  const checks = [
    {
      id: "title",
      label: "Document has a title",
      wcag: "WCAG 2.4.2 / PDF-UA",
      applicable: true,
      pass: Boolean((doc.title || "").trim()),
    },
    {
      id: "language",
      label: "Document language is set",
      wcag: "WCAG 3.1.1",
      applicable: true,
      pass: Boolean((doc.language || "").trim()),
    },
    {
      id: "alt",
      label: "All images have alt text or are marked decorative",
      wcag: "WCAG 1.1.1",
      applicable: figures.length > 0,
      pass: figuresResolved,
      detail: figures.length ? `${figures.length} image(s)` : "none",
    },
    {
      id: "headings",
      label: "Headings present and don't skip levels",
      wcag: "WCAG 1.3.1",
      applicable: headingLevels.length > 0,
      pass: headingOrderOk,
    },
    {
      id: "tableHeaders",
      label: "Tables have header cells",
      wcag: "WCAG 1.3.1 / PDF-UA",
      applicable: tables.length > 0,
      pass: tablesHaveHeaders,
      detail: tables.length ? `${tables.length} table(s)` : "none",
    },
  ];

  checks.push({
    id: "bookmarks",
    label: "Headings present for bookmark generation",
    wcag: "WCAG 2.4.1",
    applicable: true,
    pass: headingLevels.length > 0,
    detail: headingLevels.length ? `${headingLevels.length} heading(s)` : "none — add headings",
  });

  checks.push({
    id: "headingOrder",
    label: "Heading levels don't skip",
    wcag: "WCAG 1.3.1",
    applicable: headingLevels.length > 0,
    pass: headingOrderOk,
    fixable: true,
  });

  // Remove the original headings check (now split into bookmarks + headingOrder above)
  const dedupedChecks = checks.filter((c) => c.id !== "headings");

  // Contrast is only known after remediation — add it when caller supplies the result.
  if (contrastPassed !== null) {
    dedupedChecks.push({
      id: "contrast",
      label: "Text color contrast meets WCAG 1.4.3 (4.5:1)",
      wcag: "WCAG 1.4.3",
      applicable: true,
      pass: contrastPassed === true,
    });
  }

  const applicable = dedupedChecks.filter((c) => c.applicable);
  const passed = applicable.filter((c) => c.pass).length;
  const score = applicable.length ? Math.round((passed / applicable.length) * 100) : 100;
  return { score, checks: dedupedChecks };
}

/** Move a nested node up/down within its sibling list. */
export function moveNode(manifest, id, dir) {
  function moveInList(nodes) {
    const i = nodes.findIndex((n) => n.id === id);
    if (i >= 0) {
      const j = dir === "up" ? i - 1 : i + 1;
      if (j < 0 || j >= nodes.length) return nodes;
      const next = [...nodes];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    }
    return nodes.map((n) =>
      n.children ? { ...n, children: moveInList(n.children) } : n
    );
  }
  return { ...manifest, nodes: moveInList(manifest.nodes) };
}

/** Move a top-level node up/down to adjust reading order. */
export function moveTopLevel(manifest, id, dir) {
  const nodes = manifest.nodes;
  const i = nodes.findIndex((n) => n.id === id);
  if (i < 0) return manifest;
  const j = dir === "up" ? i - 1 : i + 1;
  if (j < 0 || j >= nodes.length) return manifest;
  const next = { ...manifest, nodes: [...nodes] };
  [next.nodes[i], next.nodes[j]] = [next.nodes[j], next.nodes[i]];
  return next;
}
