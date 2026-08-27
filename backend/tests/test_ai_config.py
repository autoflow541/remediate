"""Tests for the AI model-tier + cost accounting module (STRATEGY.md Tier 0)."""
from app import ai_config
from app.ai_config import (
    CostTracker,
    cost_usd,
    price_for,
    pick_model,
    MODEL_CHEAP,
    MODEL_VISION,
)


def test_default_tiers_are_not_opus():
    # The whole point: the loop must not silently run on Opus.
    assert "opus" not in MODEL_CHEAP.lower()
    assert "opus" not in MODEL_VISION.lower()
    assert "haiku" in MODEL_CHEAP.lower()


def test_cost_usd_math():
    # Haiku: $1/Mtok in, $5/Mtok out.
    assert cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == 1.00
    assert cost_usd("claude-haiku-4-5-20251001", 0, 1_000_000) == 5.00
    # Sonnet: $3 / $15.
    assert abs(cost_usd("claude-sonnet-5", 500_000, 200_000) - (1.5 + 3.0)) < 1e-9


def test_unknown_model_uses_default_price():
    assert price_for("some-future-model") == price_for("claude-sonnet-5") or \
        price_for("some-future-model") == (3.00, 15.00)


def test_pick_model_escalates_only_on_low_confidence():
    assert pick_model(0.9) == MODEL_CHEAP
    assert pick_model(None) == MODEL_CHEAP
    assert pick_model(0.4) == ai_config.MODEL_SMART
    assert pick_model(0.75) == MODEL_CHEAP          # at threshold = still cheap
    assert pick_model(0.74) == ai_config.MODEL_SMART


def test_tracker_accumulates_and_breaks_down_by_model():
    t = CostTracker()
    t.add("claude-haiku-4-5-20251001", 1_000_000, 0)   # $1.00
    t.add("claude-sonnet-5", 0, 1_000_000)              # $15.00
    assert t.calls == 2
    assert t.input_tokens == 1_000_000
    assert t.output_tokens == 1_000_000
    assert abs(t.usd - 16.0) < 1e-9
    assert set(t.by_model) == {"claude-haiku-4-5-20251001", "claude-sonnet-5"}


def test_tracker_records_from_response_object_and_dict():
    class _U:  # mimics anthropic response.usage
        input_tokens = 1000
        output_tokens = 500

    class _R:
        usage = _U()

    t = CostTracker()
    c1 = t.record("claude-haiku-4-5-20251001", _R())
    c2 = t.record("claude-haiku-4-5-20251001", {"usage": {"input_tokens": 1000, "output_tokens": 500}})
    assert c1 == c2 > 0
    assert t.calls == 2


def test_budget_cap():
    t = CostTracker(usd_budget=1.00)
    assert not t.over_budget()
    t.add("claude-sonnet-5", 1_000_000, 0)  # $3.00 > $1 budget
    assert t.over_budget()
    assert t.remaining_usd() == 0.0


def test_no_budget_means_never_over():
    t = CostTracker(usd_budget=None)
    t.add("claude-opus-4-8", 5_000_000, 1_000_000)
    assert t.over_budget() is False
    assert t.remaining_usd() is None


def test_summary_shape():
    t = CostTracker(usd_budget=10.0)
    t.add("claude-haiku-4-5-20251001", 2000, 1000)
    s = t.summary()
    assert s["calls"] == 1 and s["budgetUsd"] == 10.0 and s["overBudget"] is False
    assert "byModel" in s and "costUsd" in s
