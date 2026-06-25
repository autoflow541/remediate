// Generates a minimal, single-page, UNTAGGED PDF with correct xref offsets.
// This is intentionally non-conformant: no structure tree, no /MarkInfo, no
// /Lang, no title — so veraPDF's PDF/UA-1 check will fail it, which is exactly
// what we want to demonstrate the /validate endpoint catching real problems.
//
//   node make_sample.js              -> writes sample-untagged.pdf
//   node make_sample.js out.pdf      -> writes out.pdf
//
// Node is used only because it is the runtime available on this machine; the
// output is a plain .pdf with no Node-specific anything.

const fs = require("fs");

const out = process.argv[2] || `${__dirname}/sample-untagged.pdf`;

// Each object's body. Object 0 is the free head (handled in the xref table).
const objects = [
  // 1: Catalog (no MarkInfo, no StructTreeRoot, no Lang -> not tagged)
  "<< /Type /Catalog /Pages 2 0 R >>",
  // 2: Pages
  "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
  // 3: Page
  "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] " +
    "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
  // 4: Content stream (drawn text, no marked-content tagging)
  (() => {
    const stream =
      "BT /F1 24 Tf 72 700 Td (Untagged sample document) Tj ET\n" +
      "BT /F1 12 Tf 72 660 Td (This PDF has no tags, no language, no title.) Tj ET";
    return `<< /Length ${Buffer.byteLength(stream, "latin1")} >>\nstream\n${stream}\nendstream`;
  })(),
  // 5: Font
  "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
];

// Assemble the file while recording the byte offset of each object.
const header = "%PDF-1.7\n%\xE2\xE3\xCF\xD3\n";
let body = header;
const offsets = [];
objects.forEach((obj, i) => {
  offsets.push(Buffer.byteLength(body, "latin1"));
  body += `${i + 1} 0 obj\n${obj}\nendobj\n`;
});

// Cross-reference table.
const xrefStart = Buffer.byteLength(body, "latin1");
const count = objects.length + 1; // +1 for the free object 0
let xref = `xref\n0 ${count}\n0000000000 65535 f \n`;
offsets.forEach((off) => {
  xref += `${String(off).padStart(10, "0")} 00000 n \n`;
});

const trailer =
  `trailer\n<< /Size ${count} /Root 1 0 R >>\n` +
  `startxref\n${xrefStart}\n%%EOF\n`;

fs.writeFileSync(out, Buffer.from(body + xref + trailer, "latin1"));
console.log(`Wrote ${out} (${fs.statSync(out).size} bytes)`);
