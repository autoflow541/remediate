"""Central AI model tiers + cost accounting (STRATEGY.md Tier 0).

One place to pick models and account for spend, so the vision passes don't
silently run on the most expensive model and a batch can't blow the budget.

Decisions baked in:
  - Default to a CHEAP tier; escalate to SMART only when confidence is low.
  - Never Opus in the loop by default (it was silently the visual-check model —
    ~5x Sonnet, ~15x Haiku per token).
  - Everything env-overridable so operators can trade cost for quality per deploy.
"""
from __future__ import annotations

import os

# ── Model tiers (env-overridable) ─────────────────────────────────────────────
MODEL_CHEAP = os.environ.get("AI_MODEL_CHEAP", "claude-haiku-4-5-20251001")
MODEL_SMART = os.environ.get("AI_MODEL_SMART", "claude-sonnet-5")
# Vision judgment/triage: Sonnet by default — strong vision, far cheaper than
# Opus. Override with AI_VISUAL_CHECK_MODEL (e.g. Haiku for max savings).
MODEL_VISION = os.environ.get("AI_VISUAL_CHECK_MODEL", MODEL_SMART)

# ── Pricing: USD per 1M tokens as (input, output). Update as prices change. ────
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5":           (3.00, 15.00),
    "claude-opus-4-8":           (15.00, 75.00),
}
_DEFAULT_PRICE = (3.00, 15.00)  # unknown model → assume Sonnet-class


def price_for(model: str) -> tuple[float, float]:
    return PRICING.get(model, _DEFAULT_PRICE)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost of a single call."""
    pin, pout = price_for(model)
    return (input_tokens / 1_000_000) * pin + (output_tokens / 1_000_000) * pout


def pick_model(confidence: float | None = None, *, threshold: float = 0.75) -> str:
    """Cheap by default; escalate to SMART when confidence is below threshold."""
    if confidence is not None and confidence < threshold:
        return MODEL_SMART
    return MODEL_CHEAP


class CostTracker:
    """Accumulate token usage + USD across a job, with an optional hard cap.

    Usage:
        tracker = CostTracker()                     # budget from AI_USD_BUDGET
        if tracker.over_budget(): skip_paid_call()
        resp = client.messages.create(model=m, ...)
        tracker.record(m, resp)
        ... tracker.summary()  # -> put in the audit report as cost-per-doc
    """

    def __init__(self, usd_budget: float | None = None):
        if usd_budget is None:
            env = os.environ.get("AI_USD_BUDGET", "").strip()
            usd_budget = float(env) if env else None
        self.usd_budget = usd_budget
        self.input_tokens = 0
        self.output_tokens = 0
        self.usd = 0.0
        self.calls = 0
        self.by_model: dict[str, dict] = {}

    def add(self, model: str, input_tokens: int, output_tokens: int) -> float:
        input_tokens = max(0, int(input_tokens or 0))
        output_tokens = max(0, int(output_tokens or 0))
        c = cost_usd(model, input_tokens, output_tokens)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.usd += c
        self.calls += 1
        m = self.by_model.setdefault(model, {"input": 0, "output": 0, "usd": 0.0, "calls": 0})
        m["input"] += input_tokens
        m["output"] += output_tokens
        m["usd"] += c
        m["calls"] += 1
        return c

    def record(self, model: str, response) -> float:
        """Pull token usage from an Anthropic Messages response (or a dict)."""
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        it = getattr(usage, "input_tokens", None)
        ot = getattr(usage, "output_tokens", None)
        if (it is None or ot is None) and isinstance(usage, dict):
            it = usage.get("input_tokens", it)
            ot = usage.get("output_tokens", ot)
        return self.add(model, it or 0, ot or 0)

    def over_budget(self) -> bool:
        return self.usd_budget is not None and self.usd >= self.usd_budget

    def remaining_usd(self) -> float | None:
        return None if self.usd_budget is None else max(0.0, self.usd_budget - self.usd)

    def summary(self) -> dict:
        """JSON-ready cost-per-document block for the audit report / response."""
        return {
            "calls": self.calls,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "costUsd": round(self.usd, 4),
            "budgetUsd": self.usd_budget,
            "overBudget": self.over_budget(),
            "byModel": {
                k: {**v, "usd": round(v["usd"], 4)} for k, v in self.by_model.items()
            },
        }
