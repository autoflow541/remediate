// Generates a richer UNTAGGED PDF: a title heading, two paragraphs, a bordered
// table (header row + 2 data rows), and a bulleted list. Used to inspect how
// OpenDataLoader represents tables/lists so the autotag manifest can mirror it.
//   node make_rich_sample.js   -> sample-rich.pdf
const fs = require("fs");
const out = process.argv[2] || `${__dirname}/sample-rich.pdf`;

// Build one content stream. PDF text origin is bottom-left; y decreases down.
const lines = [];
const T = (x, y, size, s) =>
  lines.push(`BT /F1 ${size} Tf ${x} ${y} Td (${s.replace(/[()\\]/g, "\\$&")}) Tj ET`);
const RECT = (x, y, w, h) => lines.push(`${x} ${y} ${w} ${h} re S`); // stroked rectangle

// Title + paragraphs
T(72, 700, 24, "Quarterly Accessibility Report");
T(72, 660, 12, "This document summarizes remediation progress for Q2 2026.");
T(72, 640, 12, "The table below lists document counts by status.");

// Table: 3 columns x 3 rows starting at top y=600, each row 24 tall, col widths 150
const x0 = 72, colW = 150, rowH = 24, nCols = 3, nRows = 3;
const tableTop = 600;
for (let r = 0; r < nRows; r++) {
  const y = tableTop - (r + 1) * rowH;
  for (let c = 0; c < nCols; c++) {
    RECT(x0 + c * colW, y, colW, rowH);
  }
}
const cells = [
  ["Status", "Count", "Owner"],
  ["Remediated", "128", "Alex"],
  ["In progress", "37", "Sam"],
];
for (let r = 0; r < nRows; r++) {
  const y = tableTop - (r + 1) * rowH + 8;
  for (let c = 0; c < nCols; c++) {
    T(x0 + c * colW + 6, y, 11, cells[r][c]);
  }
}

// Bulleted list below the table
let ly = tableTop - nRows * rowH - 30;
["Review reading order", "Add alternative text", "Flag table headers"].forEach((item) => {
  T(72, ly, 12, "• " + item);
  ly -= 18;
});

const stream = lines.join("\n");
const objects = [
  "<< /Type /Catalog /Pages 2 0 R >>",
  "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
  "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
  `<< /Length ${Buffer.byteLength(stream, "latin1")} >>\nstream\n${stream}\nendstream`,
  "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
];

const header = "%PDF-1.7\n%\xE2\xE3\xCF\xD3\n";
let body = header;
const offsets = [];
objects.forEach((obj, i) => {
  offsets.push(Buffer.byteLength(body, "latin1"));
  body += `${i + 1} 0 obj\n${obj}\nendobj\n`;
});
const xrefStart = Buffer.byteLength(body, "latin1");
const count = objects.length + 1;
let xref = `xref\n0 ${count}\n0000000000 65535 f \n`;
offsets.forEach((off) => (xref += `${String(off).padStart(10, "0")} 00000 n \n`));
const trailer = `trailer\n<< /Size ${count} /Root 1 0 R >>\nstartxref\n${xrefStart}\n%%EOF\n`;
fs.writeFileSync(out, Buffer.from(body + xref + trailer, "latin1"));
console.log(`Wrote ${out} (${fs.statSync(out).size} bytes)`);
