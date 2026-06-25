# PDF Accessibility Remediation Engine

A free, open-source tool that does the PDF accessibility work people pay
$50–200/doc for — made urgent by ADA Title II and HHS Section 504 deadlines.

Two pieces:

- **`frontend/`** — an in-browser studio (React + Vite) for the human-judgment
  work: alt text, reading order, heading levels, table headers, title, language.
  No file leaves the device. *(Ported from a single-file Studio.jsx — Phase 1+.)*
- **`backend/`** — a stateless FastAPI engine that auto-tags, writes structure
  back into a real PDF/UA file, and proves it conforms.

## Endpoints

| Endpoint     | Does                                            | Phase |
|--------------|-------------------------------------------------|-------|
| `POST /validate`  | PDF → veraPDF conformance report           | **1 (done)** |
| `POST /autotag`   | PDF → draft manifest (OpenDataLoader)      | **2 (done)** |
| `POST /remediate` | PDF + manifest → tagged PDF/UA + report    | **3 (done)** |

The **manifest** (`.tags.json`) is a structure tree: an ordered `nodes` array
where containers (`Table` → `TR` → `TD`/`TH`, `L` → `LI`) hold `children`.
Reading order = pre-order traversal. `/autotag` produces a draft (headings,
paragraphs, tables, figures); the studio refines alt text, table headers, title
and language; `/remediate` writes it back. Schema lives in `backend/app/manifest.py`.

## Stack

- **Auto-tag:** OpenDataLoader (Apache 2.0) — local, no GPU, no API key.
- **Write-back:** pikepdf (MPL-2.0) — license-clean vs. iText (AGPL-3.0).
- **Validate:** veraPDF — industry-standard PDF/UA checker, run as a subprocess.
- **Deploy:** one Docker container, `python:3.12` + headless JDK. Stateless.

## Build order (each step ships value alone)

1. ✅ **Dockerized `/validate` (veraPDF)** — a free PDF/UA checker.
2. ✅ `/autotag` (OpenDataLoader) — studio pre-fills with real structure.
3. ✅ `/remediate` write-back (pikepdf) — the "it actually works" milestone.
4. ✅ **Tables** — header detection (`TH` + `/Scope`), row/col spans, and
   `Headers`/`ID` association for complex tables. Forms are deferred (phase 2.5).

Backend phases 1–4 are complete and verified end-to-end in Docker. Remaining:
the frontend studio port and forms.

## Phase 1 — build & run

From `remediation/`:

```bash
# Build the image (build context is the repo root so backend/ is in scope)
docker build -f docker/Dockerfile -t remediation-engine .

# Prove the Java/veraPDF plumbing with no server:
docker run --rm remediation-engine /opt/verapdf/verapdf --version

# Run the service
docker run --rm -p 8000:8000 remediation-engine
# or: docker compose -f docker/docker-compose.yml up --build

# Health (proves JRE + veraPDF are live)
curl http://localhost:8000/health

# Validate the bundled untagged sample (expect compliant: false)
node samples/make_sample.js
curl -F "file=@samples/sample-untagged.pdf" -F "flavour=ua1" \
  http://localhost:8000/validate

# Auto-tag a richer sample into a draft manifest (headings, table, etc.)
node samples/make_rich_sample.js
curl -F "file=@samples/sample-rich.pdf" http://localhost:8000/autotag > sample-rich.manifest.json

# Remediate: PDF + manifest -> tagged PDF/UA file. Conformance is returned in
# the X-Conformance / X-VeraPDF-* response headers; the fixed PDF is the body.
curl -F "file=@samples/sample-rich.pdf" -F "manifest=@sample-rich.manifest.json" \
  -D - -o sample-rich.remediated.pdf http://localhost:8000/remediate
```

> Remediation writes the full structure tree, marked content (MCID-bound via
> OpenDataLoader bounding boxes), `/Lang`, title, and the PDF/UA-1 XMP claim. A
> source PDF that embeds its fonts reaches **full PDF/UA-1 conformance**
> (veraPDF `compliant: true`); a source using unembedded standard fonts is left
> with only the font-embedding clause failing, which is a content issue beyond
> structure write-back.
>
> **Tables:** `/autotag` proposes the first row as column headers (`TH` +
> `/Scope Column`); pass `detect_headers=false` to skip. The studio confirms or
> overrides. Complex tables (spanning cells, or both row and column headers) get
> `/Headers`+`/ID` associations and an `/IDTree` so veraPDF can resolve them.

## Frontend — the studio

React + Vite, with **pdf.js bundled locally** (no CDN). The judgment work
(render, retag, reading order, alt text, table headers, title, language, live
WCAG/PDF-UA score, `.tags.json` export) runs entirely in the browser; the engine
is only called for auto-tag / validate / remediate.

```bash
cd frontend
npm install
cp .env.example .env        # set VITE_API_BASE to your engine URL
npm run dev                 # http://localhost:5173
npm run build               # static site in frontend/dist/
```

If `VITE_API_BASE` is unset (or the engine is offline) the studio still loads and
works for manual tagging + manifest export — it just disables the engine buttons.

## Deployment

The two pieces are independent (CORS + `VITE_API_BASE`), so they can live apart:

- **Studio → any static host** (incl. **DreamHost shared hosting**): `npm run
  build`, upload `frontend/dist/` to your web directory. `vite.config.js` uses a
  relative `base`, so a subdirectory works too.
- **Engine → a root-capable host** (Docker required): **DreamHost DreamCompute**
  or a dedicated server; or any small VPS / Fly.io / Render. DreamHost *shared*
  hosting and their *managed VPS* cannot run it (no Docker/Java/root).

Point the studio at the engine with `VITE_API_BASE=https://engine.example.com`
at build time, and lock the engine's CORS to the studio origin via the
`CORS_ORIGINS` env var (default `*`).

### Running without Docker (local dev)

Needs Python 3.10+ and a JRE 11+ with veraPDF on `PATH` (or `VERAPDF_PATH` set).

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## License

Code: see `LICENSE`. Bundled tools retain their own licenses (veraPDF: GPL+MPL
dual; OpenDataLoader: Apache 2.0; pikepdf: MPL-2.0).
