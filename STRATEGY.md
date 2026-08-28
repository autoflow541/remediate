# Strategy — Winning Automated PDF Remediation

_Decisions (not options) to beat PDFix, Equidox, axesPDF, Allyant/CommonLook.
Pairs with `COMPETITORS.md` (the field) and `ROADMAP.md` (the build)._

## Thesis (one line)
Be the **veraPDF-verified, self-hosted, AI-vision** remediation engine that is
**honest, no-lock-in, and priced in the open** — winning the gov/higher-ed backlog
that PDFix & Equidox chase, plus the SMB/local-gov long tail they ignore behind
"book a demo."

---

## Tier 0 — Cost architecture (do FIRST; it gates scaling)
The "Claude vision on every page" north star is our edge **and** our cost bomb.
Fix unit economics before chasing volume.
1. **Deterministic-first, AI-last.** Run every veraPDF/rule fix first; the LLM only
   sees the residual ambiguous items. **Target: <20% of pages ever touch a paid model.**
2. **Model-tier routing (decision).** Cheap/local vision model for first pass +
   confidence; escalate to **Sonnet** only on low confidence. **Never Opus in the loop.**
3. **Per-job + per-tenant token budgets** with a hard cap — a batch can never blow the
   bill. Emit **cost-per-document** in the audit report.
4. **Decision cache + layout fingerprinting.** Hash page image/layout; identical or
   templated pages reuse the prior decision. This is *exactly* how Equidox/PDFix win
   templated-doc cost — match it.
5. **Local-model backend option** (Ollama / small VLM), pluggable PDFix-Actions-style,
   for **$0 marginal cost** at volume and privacy-sensitive gov.
- **Cost target:** blended **$0.02–0.05 / page** in cloud mode, **$0 marginal** in
  local-model mode.

## Tier 1 — Close the deal-losers (build now)
6. **Async job queue + batch (ROADMAP #6/#8).** THE gap. submit → poll/websocket →
   download, worker pool. Unblocks batch + full-doc vision, and kills the Caddy cold-
   start timeout bug. **Without this we cannot bid gov/enterprise volume. #1 priority.**
7. **Make `/batch` real** on the queue → the "at scale" story vs PDFix/Equidox.
8. **Full-document vision (#1).** "AI made the call" must be true past page 6.
   Gated on #6.
9. **Confidence scoring + auto-accept threshold (#7).** Cost lever *and* the "human
   queue shrinks to only the genuinely unsure" story.

## Tier 2 — Trust + reach (sales unlocks, cheap)
10. **Free public checker** (upload → veraPDF report). Everyone has one (PAC, PDFix,
    Allyant) — it's the #1 lead magnet and pure SEO, and uses our `/validate` with
    **no AI cost.** Ship it.
11. **VPAT / conformance report + before-after screen-reader preview (#10).** The
    artifact enterprise/gov buyers demand (Allyant sells on it). Cheap, raises close rate.
12. **Thin multi-language SDK.** Publish an **OpenAPI spec** + a small Python client;
    auto-generate C#/Java/Node with `openapi-generator`. Matches PDFix's "5-language
    SDK" at ~1/10th the effort.

## Tier 3 — Business model & positioning (decisions)
13. **Publish pricing.** Nobody does; axesPDF's $500–$2,000/license is the only anchor.
    - **SaaS:** ~**$0.50–1.00/page** or monthly tiers for SMB/local-gov.
    - **Self-host license:** annual, for gov/enterprise/privacy (PDFix-style on-prem).
    - **Free checker** = top of funnel.
14. **Two SKUs:** Hosted (SMB) + Self-hosted (gov/enterprise). Matches PDFix's dual
    model; beats **cloud-only** Adobe/ASU and **desktop-only** axesPDF/CommonLook.
15. **Own the keywords:** "**veraPDF-verified**," "**self-hosted / on-prem**," "**batch
    PDF remediation**," "**AI-powered**," beside WCAG 2.2 AA / PDF/UA / Section 508 /
    ADA Title II.
16. **Fix the site:** ADA Title II dates → **2027/2028** (+ HHS May-2026); weave the
    keywords into `accessibility.html` / `pdf-remediation.html`.

---

## Build order (decision)
`#6 queue` → (cost controls #2/#3/#4 in parallel) → **#10 free checker** (quick SEO/
lead win, no AI) → `#7 batch` → `#1 full-doc vision` → `#9 confidence` → `#11 VPAT`
→ `#13 publish pricing` → `#12 SDK` → `#5 local model`.
_Rationale: #6 unblocks everything; cost controls must land before volume; the free
checker is a cheap lead-gen win to run in parallel._

## Don't do (anti-scope)
- **No Windows desktop app** — that's axesPDF/CommonLook's shrinking turf.
- **No hard cloud-AI lock-in** — keep the model backend pluggable (Adobe/ASU's weakness).
- **No "100% no-human" claim** — honesty is the brand; DOJ **and** PDFix concede AI
  can't fully auto-fix. Sell "we tell you exactly what still needs a human."

## Scoreboard (how we know we're winning)
- Cost/page (cloud) ≤ $0.05; local mode = $0 marginal.
- Batch throughput (docs/hour) publishable next to PDFix's "thousands/hour."
- % pages auto-accepted at confidence threshold (target ≥ 80%).
- Corpus machine-conformance (veraPDF pass rate) at/near 100%.
- Free-checker → paid conversion rate.
