# Competitive Landscape — Automated PDF Remediation

_Research snapshot: 2026-08-27. Pair with `ROADMAP.md` (our build plan) — this file
is the market it's competing in: who's out there, how they position, what they
ship, and where we can win._

---

## 0. Market drivers (why this market is hot — and the deadline that moved)
- **ADA Title II (US state/local gov):** The 2024 DOJ rule required WCAG 2.1 AA.
  **On 2026-04-20 the DOJ extended the deadlines** — large entities (pop. ≥50k)
  from Apr 2026 → **Apr 26, 2027**; smaller/special districts → **2028**. DOJ
  explicitly cited *"limits of generative AI for remediation"* as a reason.
  **Action:** our own site still cites the old 2026 date (`ada-title-ii-oregon.html`)
  — fix it.
- **HHS §504:** separate healthcare digital-accessibility deadline ~**May 2026**.
- **EU EAA:** took effect **2025-06-28** — private-sector pressure in Europe.
- **Section 508 / WCAG 2.2 AA:** ongoing federal + procurement demand.
- Net: demand is large and durable, now on a longer runway. Buyers = state/local
  gov, higher-ed, healthcare, finance, publishers, and the agencies serving them.

---

## 1. Open-source / GitHub
| Project | What it does | License | AI | Note vs us |
|---|---|---|---|---|
| **OpenDataLoader** (PDF Assoc. + Dual Lab / veraPDF) | Untagged→Tagged auto-tag, veraPDF-validated; #1 parse accuracy 0.907 | Apache-2.0 | Optional Docling (tables) | **We integrate it.** But full **PDF/UA-1/UA-2 export is a paid enterprise add-on** — tagging ≠ full remediation |
| **ASU CIC `PDF_Accessibility`** | Tagging + metadata + AI alt-text; PDF→PDF and PDF→HTML | MIT | **AWS Bedrock (Nova Pro)** + **Adobe PDF Services API** | Closest arch peer, but self-labeled *"not suitable for production"* and **locked to Adobe API + AWS** |
| **accesspdf** (laurenaulet) | Tags, reading order, headings, tables, links | Open | Alt-text via **Ollama** (local) | Local-AI angle like ours; smaller/newer |
| **PDF-Acc-Toolset** (iText/Blazor WASM) | Browser-local remediation webapp | Open | — | Client-side, no verify loop |
| **pdfix examples** | SDK samples | (SDK commercial) | — | Vendor funnel |

**Read:** No OSS project ships our combo — self-hosted **veraPDF-verified
remediate→validate→fix loop** with AI vision making the calls. ODL tags; ASU does
AI alt-text but depends on Adobe+AWS and isn't production-grade.

---

## 2. Commercial vendors — positioning, wording, features
### PDFix — the AI-batch leader (our most direct rival)
- **Tagline:** *"Make PDFs Accessible at Scale."*
- **Lead keywords:** "AI-driven layout recognition", "at scale / high-volume",
  "batch processing", "**reduce manual remediation effort by up to 80%**",
  "on-premises (no data leaves your network)", "millions of PDFs processed monthly".
- **Features:** auto-tag + structure detection; PDF/UA validation + fixing; batch
  (thousands of docs); **layout templates** for repeating doc types; REST API +
  SDK (C++, C#, Java, Python, Node); **GitHub Actions**; PDF→HTML5; JSON extraction;
  compliance reporting; screen-reader optimization.
- **Pricing:** not public; free "Desktop Lite" checker; trials.
- **Honesty:** openly says AI handles ~80%, *"focus only on what requires final
  human judgment."* Also published *"Can AI automatically fix PDF accessibility?"*
  (answer: not fully).
- **Buyers:** enterprise, gov, banks, insurance, universities, a11y consultants.

### axesPDF (by axes4) — the PDF/UA quality leader (assisted desktop)
- **Tagline:** *"Find and fix accessibility issues to comply easily with PDF/UA —
  in just a few seconds."*
- **Lead keywords:** "PDF/UA", "in seconds", one-click fix, structured report.
- **Features:** checks all **89 machine-checkable** PDF/UA conditions; **one-click
  fixes 50+** of the hardest; WCAG 2 + Section 508.
- **Pricing:** ~**$500–$2,000 / license** (one of the few with a public range).
- **Model:** operator-driven desktop tool, not batch/AI. Buyers skew EU gov
  (Austrian Parliament, German agencies) + higher-ed.

### Equidox — high-volume + AI, templated
- **Taglines:** *"Easy PDF Remediation"* / *"Enabling PDF accessibility through
  intelligent, automated solutions."*
- **Lead keywords:** "fastest tools on the market", "intelligent/automated",
  "scalable", "AI", "zone detection".
- **Two products:** **Equidox Software** (SaaS, auto-detect + tagging, human
  review for complex docs) and **Equidox AI** ("**fully automated**", ML +
  computer vision, **batch**, best for **templated/repetitive** docs).
- **Pricing:** not public (demo/discovery call).
- **Admits:** human QA "remains essential" for complex/one-off docs.

### Allyant (formerly CommonLook) — software + done-for-you service
- **Tagline:** *"Simple. Seamless. Accessibility."*
- **Keywords:** ADA compliance, PDF remediation, **VPAT services**, litigation
  support, braille/large-print, "equitable access".
- **Offerings:** CommonLook Accessibility Suite (software), **PDF Remediation
  Services** (human experts), free checker, training, alternate formats.
- **Buyers/trust logos:** Citi, Verizon, Yale, MetLife, **Library of Congress**.
- **Model:** enterprise software **+ heavy professional services**.

### Others (context)
- **Adobe** — Acrobat Pro + **Auto-Tag API**; the incumbent baseline everyone
  benchmarks against.
- **PREP (Continual Engine)** — AI remediation platform, batch + compliance
  reporting, enterprise.
- **Crawford Tech, tabnav, Aelira** — platform/service; tabnav = upload → auto-
  remediate → validate workflow; Aelira = newer AI entrant.

---

## 2b. Tech stack (what they're built on)
| Vendor | Core engine | Bindings / API | Deploy | AI approach | Platforms |
|---|---|---|---|---|---|
| **PDFix SDK** | High-perf **C++** library, **no external deps**; multi-threaded batch + worker queue | **Python, C#, Java, C++, Node.js** (NuGet `PDFix.SDK`); CLI; unified API | **On-prem only** (no cloud), **Docker/Kubernetes**, CI/CD | Pluggable **"Actions Marketplace"** — Docker/local AI models: **IBM Docling, OpenAI, Amazon Textract** for alt-text, OCR, formulas, lang detect, table summaries | Linux (x86_64/aarch64), Windows x64, macOS (x64/arm64) |
| **axesPDF** (axes4 GmbH, DE) | **Windows desktop**, **.NET Framework 4.8**; installer = bootstrapper + MSIs; sibling `axesFlip` on .NET | Desktop app (no public SDK) | **Local desktop** (Win 10/11) | **AI added in 2.5** to *assist* manual checks (human stays in control); screen-reader preview | Windows only |
| **Equidox** (Onix) | **SaaS** + **Equidox AI**; **ML + computer vision** "Smart Zone Detector" (detects elements in untagged PDFs) | **REST API** (delivery/integration); managed service | **Cloud (Google Cloud Storage, encrypted)** or on-prem; batch or on-demand | ML/CV models auto-tag; best on **templated** docs | Cloud / on-prem |
| **Allyant / CommonLook** | Legacy **Acrobat plugin + standalone desktop** (.NET/Windows heritage); now suite + **human services** | Desktop; service delivery | Desktop + professional services | Limited AI; **human experts** do the heavy remediation | Windows |
| **Adobe** | Acrobat + **PDF Services / Auto-Tag API** (cloud) | REST API, SDKs | **Cloud API** | ML auto-tag; Sensei | Cloud / desktop |
| **ASU CIC** (OSS) | **AWS**: S3, Lambda, Step Functions, **ECS Fargate**, CloudWatch; **Adobe PDF Services API** | AWS-native | AWS only | **AWS Bedrock (Nova Pro)** for alt-text/enhance | AWS |
| **OpenDataLoader** (OSS) | **Java 11** + **Python 3.10+**; veraPDF-validated tagging | CLI / library | Local / container | Optional **Docling** for tables | Cross-platform (JVM) |
| **Auto-Flow (us)** | **Python 3.12 (FastAPI)** + **pikepdf** + **fontTools** + **veraPDF (JRE 11)** + **OpenDataLoader**; React/Vite studio | FastAPI REST (`/validate`, `/autotag`, `/remediate`, …) | **Self-hosted Docker**, Caddy | **Claude (Anthropic) vision** makes per-page calls; veraPDF verify→fix loop | Linux/Docker |

**Stack reads:**
- **PDFix is the architectural benchmark**: a dependency-free C++ core with 5-language
  bindings, on-prem, container/queue-ready, and a *pluggable* AI marketplace. Our
  FastAPI/Python core is more approachable but **single-language + no worker queue
  yet** — the batch/throughput gap again (ROADMAP #6).
- **We're the only one wiring a frontier vision LLM (Claude) as the decision-maker**;
  others use narrower ML/CV (Equidox), cloud AI services (ASU→Bedrock, Adobe→Sensei),
  or pluggable model slots (PDFix). That's our differentiator **and** our cost/latency
  risk to manage.
- **axesPDF & CommonLook are Windows-desktop-bound**; we (and PDFix/Equidox) win on
  server/automation. **ASU is AWS-locked; Adobe is cloud-only** — our **self-hosted,
  no-lock-in** posture is a real enterprise/gov selling point.
- **veraPDF is the shared source of truth** (us, ODL, axes4's PAC checker all lean on
  it) — leaning into "veraPDF-verified" is credible, not marketing fluff.

---

## 3. Messaging & keyword patterns (what they ALL say)
Recurring keywords to know (and rank for): **PDF/UA (ISO 14289-1)**, **WCAG 2.1/2.2
AA**, **Section 508**, **ADA Title II**, **European Accessibility Act**, "**at
scale / batch / high-volume**", "**AI-powered / AI-driven**", "**automated
tagging**", "**reading order**", "**alt text**", "**compliance report / VPAT**",
"**on-premises**", "**screen-reader tested**", "**templated documents**".

Recurring proof devices: gov + marquee-enterprise trust logos; "in seconds" speed
claims; an **"80% automated, human for the rest"** honesty frame; free checker as a
lead magnet; **no public pricing** (all "book a demo").

---

## 4. Feature matrix (✓ = advertised)
| Capability | PDFix | axesPDF | Equidox | Allyant | Adobe | **Auto-Flow (us)** |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| Auto-tagging / structure | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| veraPDF / PDF-UA validation | ✓ | ✓ | ✓ | ✓ | – | ✓ (**verify→fix loop**) |
| **Batch / high-volume** | ✓ | – | ✓ | ✓ (svc) | ✓ (API) | **✗ (ROADMAP #6)** |
| AI alt-text | ✓ | – | ✓ | – | ✓ | ✓ |
| **AI full-page vision "makes the call"** | partial | – | partial | – | – | **✓ (our wedge)** |
| Reading-order engine | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Templates for repeat docs | ✓ | – | ✓ | – | – | ✗ |
| REST API / SDK | ✓✓ | – | ✓ | – | ✓ | partial |
| On-prem / self-host | ✓ | ✓ (desktop) | ✓ | – | – | **✓** |
| No Adobe/cloud lock-in | ✓ | ✓ | – | – | – | **✓** |
| Confidence scoring / triage | – | – | – | – | – | **✗ (ROADMAP #7)** |
| VPAT / conformance report | – | report | – | ✓ | – | **✗ (ROADMAP #10)** |
| Public pricing | – | **✓** | – | – | – | ? |

---

## 5. Where we win / where we're exposed
**Differentiators (defensible):**
- **veraPDF-verified remediate→fix loop** — most tools tag-and-hope; we prove
  conformance and iterate. (Verified live: e.g. a base-14 font goes to PDF/UA
  compliant, machine-checked.)
- **Self-hosted, no Adobe/AWS lock-in** — privacy + cost story vs ASU/SaaS.
- **AI vision "makes the call" per page** — beyond templated automation; can
  handle arbitrary/one-off docs the template-based players struggle with.
- **Honesty** — we disclose machine vs the ~47 human-judgment Matterhorn
  conditions. DOJ + PDFix both concede AI can't fully auto-fix, so this reads as
  credible, not weak.

**Gaps that lose deals (all on ROADMAP):**
- **No batch / async job queue (#6)** — this is table-stakes vs PDFix/Equidox/PREP
  for gov/enterprise backlogs. Biggest strategic risk.
- **No full-document vision review (#1)** — "AI made the call" is only true to
  ~page 6 today.
- **No confidence scoring (#7)** and **no VPAT/conformance report + before/after
  preview (#10)** — the trust artifacts buyers expect.
- **Thin API/SDK** vs PDFix's multi-language SDK + GitHub Actions.

---

## 6. Positioning + keyword recommendations for the site
- **Adopt the batch/scale + AI vocabulary** we currently under-use: "AI-powered",
  "batch PDF remediation", "at scale", alongside our existing (good) "WCAG 2.2 AA,
  PDF/UA, Section 508". Add **"veraPDF-verified"** as a unique proof term.
- **Lead with our honesty + verification** as the differentiator vs "tag-and-hope":
  "every file machine-checked against veraPDF; we tell you exactly what still needs
  a human."
- **Consider transparent pricing** (SMB / local-gov) — literally nobody else shows
  it; axesPDF's public $500–$2,000 is the only anchor. A published tier could win
  the long tail the enterprise players ignore.
- **Fix the stale ADA "April 2026" references** → 2027/2028 (Title II) and note the
  HHS May-2026 healthcare deadline.

---

## Sources
- OpenDataLoader — https://github.com/opendataloader-project/opendataloader-pdf
- ASU PDF_Accessibility — https://github.com/ASUCICREPO/PDF_Accessibility
- accesspdf — https://github.com/laurenaulet/accesspdf
- pdf-accessibility topic — https://github.com/topics/pdf-accessibility
- PDFix — https://pdfix.net/ · https://pdfix.net/best-pdf-accessibility-tools-2026/ · https://pdfix.net/can-ai-automatically-fix-pdf-accessibility-issues/
- PDFix SDK (stack) — https://pdfix.net/products/pdfix-sdk/ · https://github.com/pdfix/PDFixSDK-Documentation · https://www.nuget.org/packages/PDFix.SDK
- axes4 / axesPDF (stack) — https://www.axes4.com/en/desktop-software/axespdf · https://support.axes4.com/hc/en-us/articles/7371808093714-System-requirements · https://www.axes4.com/en/technology/axesflip
- Equidox — https://equidox.co/ · https://equidox.co/pdf-solutions/high-volume-solutions/ · https://equidox.co/blog/integrating-artificial-intelligence-and-automation-into-equidox/
- Allyant (CommonLook) — https://allyant.com/
- Continual Engine (PREP) — https://www.continualengine.com/blog/top-pdf-remediation-service-providers/
- DOJ Title II extension — https://www.consumerfinancialserviceslawmonitor.com/2026/04/doj-extends-title-ii-ada-web-accessibility-rule-compliance-deadlines-for-state-and-local-governments/
- Venable, ADA Title II 2026 — https://www.venable.com/insights/publications/2026/04/ada-title-ii-website-accessibility-regulations
- LLM accessibility benchmark — https://arxiv.org/pdf/2509.18965
