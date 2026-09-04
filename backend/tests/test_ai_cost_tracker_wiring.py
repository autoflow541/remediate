"""ai_config.CostTracker was fully unit-tested but never actually wired into
any AI call site -- AI_USD_BUDGET did nothing anywhere in the codebase. These
tests cover the wiring: run_visual_fix() honours a shared tracker's budget and
records spend into it, and POST /jobs/batch shares ONE tracker across every
file so a batch genuinely "can never blow the bill" (STRATEGY.md Tier 0),
rather than each file getting its own fresh, unenforced budget."""

from __future__ import annotations

import io
import json
import time

import pytest

from app.ai_config import CostTracker
from app.ai_visual_fix import run_visual_fix


def test_over_budget_skips_the_api_call_entirely(monkeypatch):
    """No ANTHROPIC_API_KEY needed for this one: the budget check happens
    BEFORE the key/SDK checks, so an already-exhausted tracker must short
    -circuit without ever touching the network."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-unused")
    tracker = CostTracker(usd_budget=1.0)
    tracker.add("claude-sonnet-5", 1_000_000, 0)  # $3 > $1 budget
    assert tracker.over_budget()

    result = run_visual_fix("/does/not/matter.pdf", cost_tracker=tracker)
    assert result["available"] is False
    assert "budget" in result["reason"].lower()


def test_no_tracker_is_backward_compatible(monkeypatch):
    """Existing callers that don't pass cost_tracker must behave exactly as
    before -- disabled via env still short-circuits the same way."""
    monkeypatch.setenv("AI_VISUAL_FIX", "off")
    result = run_visual_fix("/does/not/matter.pdf")
    assert result == {"available": False, "reason": "disabled via AI_VISUAL_FIX env"}


class _FakeUsage:
    def __init__(self, inp, out):
        self.input_tokens = inp
        self.output_tokens = out


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, payload: dict, inp=1000, out=200):
        self.stop_reason = "end_turn"
        self.content = [_FakeTextBlock(json.dumps(payload))]
        self.usage = _FakeUsage(inp, out)


class _FakeMessages:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response, api_key=None):
        self.messages = _FakeMessages(response)


def _stub_pipeline(monkeypatch, response, model="claude-sonnet-5"):
    """Stub every dependency of run_visual_fix up to the API call itself, so
    only the cost-tracking wiring is under test."""
    import app.ai_visual_fix as m

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-unused")
    monkeypatch.setattr(m, "_MODEL", model)
    monkeypatch.setattr(m, "_render_pages", lambda path, max_pages: [(1, b"fakepng")], raising=False)
    monkeypatch.setattr("app.ai_visual_check._render_pages", lambda path, max_pages: [(1, b"fakepng")])
    monkeypatch.setattr("app.ai_visual_check._structure_digest", lambda path: {})
    monkeypatch.setattr(m, "_collect_elements", lambda pdf: [])

    import pikepdf
    monkeypatch.setattr(pikepdf, "open", lambda path: pikepdf.Pdf.new())

    import anthropic
    monkeypatch.setattr(
        anthropic, "Anthropic",
        lambda api_key=None: _FakeAnthropicClient(response, api_key=api_key),
    )


def test_successful_call_records_spend_into_the_shared_tracker(monkeypatch):
    _stub_pipeline(monkeypatch, _FakeResponse(
        {"summary": "ok", "fixes": [], "remaining": []}, inp=10_000, out=1_000,
    ))
    tracker = CostTracker(usd_budget=100.0)

    result = run_visual_fix("/does/not/matter.pdf", cost_tracker=tracker)

    assert result["available"] is True
    assert tracker.calls == 1
    assert tracker.input_tokens == 10_000
    assert tracker.output_tokens == 1_000
    assert tracker.usd > 0


def test_second_call_sees_the_first_calls_spend(monkeypatch):
    """The behavior this whole fix is for: a tracker shared across multiple
    run_visual_fix calls (one per file in a batch) accumulates -- the second
    file's call is refused once the first pushed the shared total over budget."""
    _stub_pipeline(monkeypatch, _FakeResponse(
        {"summary": "ok", "fixes": [], "remaining": []}, inp=1_000_000, out=0,  # $3 on Sonnet
    ))
    tracker = CostTracker(usd_budget=5.0)

    first = run_visual_fix("/file1.pdf", cost_tracker=tracker)
    assert first["available"] is True
    assert tracker.usd == pytest.approx(3.0)
    assert not tracker.over_budget()

    second = run_visual_fix("/file2.pdf", cost_tracker=tracker)
    assert second["available"] is True
    assert tracker.usd == pytest.approx(6.0)
    assert tracker.over_budget()

    third = run_visual_fix("/file3.pdf", cost_tracker=tracker)
    assert third["available"] is False
    assert "budget" in third["reason"].lower()
    assert tracker.calls == 2  # the third call never touched the API
