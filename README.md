# PDF Accessibility Remediation

Upload a PDF. Get a WCAG 2.2 AA + PDF/UA-1 compliant PDF back.

**Live:** [pdf.auto-flow.co](https://pdf.auto-flow.co)

---

## What it does

- Auto-tags structure (headings, tables, figures, lists, forms)
- Embeds fonts, sets language, title, and reading order
- Fixes table header scopes, alt text, contrast
- Validates with veraPDF — outputs a real PDF/UA-1 file
- Runs entirely on your server. No third-party APIs required.

## Stack

- **Backend:** FastAPI + pikepdf + veraPDF (Docker)
- **Frontend:** React + Vite
- **AI passes:** Claude Vision (alt text, OCR, layout analysis) — optional, needs `ANTHROPIC_API_KEY`

## Run it

```bash
docker build -f docker/Dockerfile -t remediation-engine .
docker run --rm -p 8000:8000 remediation-engine
```

```bash
cd frontend
npm install
npm run dev   # http://localhost:5173
```

Set `VITE_API_BASE` to your engine URL at build time. Set `CORS_ORIGINS` on the engine to lock it to your frontend.

## API

| Endpoint | Description |
|---|---|
| `POST /remediate` | PDF → tagged, validated PDF/UA |
| `POST /validate` | PDF → veraPDF conformance report |
| `POST /autotag` | PDF → structure manifest (JSON) |
| `POST /quickfix` | Apply all auto-fixes in one pass |
| `POST /batch` | Process multiple PDFs |
| `POST /patch` | Patch metadata, headings, language |
| `POST /reading-order` | Extract structure tree |
| `POST /reorder` | Apply new reading order |

Full docs: [pdf.auto-flow.co/docs](https://pdf.auto-flow.co/docs)

## License

Code: see `LICENSE`. Bundled tools retain their own licenses (veraPDF: GPL+MPL; pikepdf: MPL-2.0).
