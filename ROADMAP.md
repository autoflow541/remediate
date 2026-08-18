# Competitive Roadmap — toward 100% automated remediation

North star: **the AI takes a picture of every page and makes the call.** A
document goes in, a fully-tagged PDF/UA file comes out, and the human queue
shrinks to only the genuinely ambiguous. Commercial tools (Adobe, CommonLook,
axesPDF) still lean on a human operator for the judgment items — beating them
means automating those judgments with calibrated confidence, not skipping them.

Status legend: ✅ shipped · 🔨 ready to build · 🧪 needs veraPDF/VM to land safely

---

## 1. 🧪 Full-document AI vision review
Today `run_visual_fix` renders only the first ~4–6 pages. A 20-page document is
mostly unreviewed, so "AI made the call" is false past page 6. Render **every**
page (batched, token-capped, page-budget configurable), so the vision pass
covers the whole document. This is the single biggest gap between "assistive"
and "authoritative." *Cost/latency tradeoff — pair with async jobs (#6).*

## 2. 🧪 AI decides decorative vs. informative (`mark_decorative`)
A new safe dispatch action: when the model is confident a figure is purely
decorative, remove its `Figure` element from the structure tree **and** re-mark
its content-stream MCID as `/Artifact`. This is the highest-value judgment call
a human normally makes. Must be verified against veraPDF (orphan-MCID / 7.1-3
risk) before shipping — the apply path exists as a guarded skip until then.

## 3. 🧪 AI-driven reading order
Reading order is currently manual (top-level up/down only). Let the vision model
emit the correct linear order from the render; the engine rewrites the structure
tree to match. Reading order is the #1 real-world accessibility failure that
machine layout analysis gets wrong on multi-column / sidebar pages.

## 4. 🧪 Close the base-14 font-embedding gap (veraPDF 7.21.4.1)
The last remaining machine-conformance failure in the corpus (t02/t19:
unembedded Times-Roman). Harden `_locate_font_with_fallback` (weight/style-aware
substitute selection) and the Type1→TrueType `/Subtype` switch so embedding a
base-14 font doesn't trade 7.21.4.1 for a 7.21.5 width mismatch. Touches every
document's fonts — needs its own benchmark run.

## 5. ✅ AI language detection (`set_lang`) — document level shipped
The vision pass now corrects a wrong/missing `/Lang` from what the page is
visibly written in (BCP-47 normalised, `dc:language` mirrored). *Next:*
per-element `/Lang` for mixed-language documents (a phrase in another language
inside an English paragraph).

## 6. 🔨 Async job queue + live progress
Long synchronous `/remediate` requests (deep-fix + AI passes run ~55–150s) die
at the Caddy proxy on cold starts (HTTP 000). Move to a job model: submit →
poll/websocket → download, with a real progress bar. Fixes a reliability bug and
unlocks #1 (full-document review) without timeouts.

## 7. 🧪 Confidence scoring + auto-accept threshold
Each AI decision should carry a calibrated confidence; only sub-threshold items
land in the human queue. "100% automated" doesn't mean *never* ask a human — it
means asking only when the model itself is unsure. Surface the confidence in the
audit report so reviewers triage fastest-first.

## 8. 🔨 Self-healing validate→fix loop to convergence
Make the remediate → veraPDF → targeted-fix cycle the backbone: iterate until
compliant or no further progress (capped, with the existing regression guard).
Pieces exist (deep-fix, AI compliance loop) but run ad hoc; unify them so any
document converges to its best reachable state automatically.

## 9. ✅ Alt-text / link / form quality hardening
Shipped over recent commits and unit-tested:
- Radio-button widgets grouped into one `/Form` element; hidden widgets skipped;
  struct labels humanised (`fullName` → "Full Name").
- Alt-text checker now catches filename-as-alt (`banner.png`) and too-short alt.
- Link accessible names handle `mailto:`/`tel:` and fixed a dead acronym-casing
  bug (`WCAG` was silently becoming `Wcag` on every link).

## 10. 🔨 Trust: key hygiene, conformance report, before/after preview
Competitive credibility. Rotate the `ANTHROPIC_API_KEY` that leaked to a log,
stop echoing it, and ship a formal conformance/VPAT report plus a before/after
screen-reader preview so buyers can *see* the remediation, not just trust a
veraPDF badge. The tool should never claim "fully remediated" off a machine
pass alone — the ~47 human-judgment Matterhorn conditions stay disclosed.

---

### Sequencing
`#6 async` unblocks `#1 full-document review`, which makes `#2 decorative`,
`#3 reading order`, and `#7 confidence` meaningful across a whole document.
`#4 fonts` and `#10 trust` are independent and can land in parallel. `#5` and
`#9` are partially done. Every 🧪 item needs a veraPDF benchmark run on the VM
(Docker is unavailable on the dev machine) before it goes live.
