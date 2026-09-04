"""POST /jobs/batch must share ONE CostTracker across every file in the batch
-- the actual production wiring, not just run_visual_fix's own budget logic
(covered separately in test_ai_cost_tracker_wiring.py). Exercises the real
submit -> poll -> download flow through the async job queue."""

from __future__ import annotations

import io
import time

from fastapi.testclient import TestClient

import app.main as main_module


def _client():
    return TestClient(main_module.app)


def _poll_result(client, job_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        status = client.get(f"/jobs/{job_id}").json()
        if status["status"] in ("done", "error"):
            return client.get(f"/jobs/{job_id}/result")
        time.sleep(0.01)
    raise AssertionError("job did not finish in time")


def test_one_tracker_instance_is_shared_across_every_file_in_the_batch(monkeypatch):
    monkeypatch.setattr(main_module, "autotag_pdf", lambda path, detect_headers=True: {"nodes": []})

    seen_trackers = []

    def fake_remediate_impl(file, manifest, flavour=main_module.DEFAULT_FLAVOUR, cost_tracker=None):
        seen_trackers.append(cost_tracker)
        return main_module.Response(
            content=b"%PDF-fake",
            media_type="application/pdf",
            headers={"X-Conformance": '{"compliant": true, "failedRules": 0}'},
        )

    monkeypatch.setattr(main_module, "_remediate_impl", fake_remediate_impl)

    client = _client()
    files = [
        ("files", ("a.pdf", io.BytesIO(b"%PDF-1.4 fake a"), "application/pdf")),
        ("files", ("b.pdf", io.BytesIO(b"%PDF-1.4 fake b"), "application/pdf")),
        ("files", ("c.pdf", io.BytesIO(b"%PDF-1.4 fake c"), "application/pdf")),
    ]
    submit = client.post("/jobs/batch", files=files)
    assert submit.status_code == 202

    result = _poll_result(client, submit.json()["id"])
    assert result.status_code == 200

    # Every file's _remediate_impl call must have received a cost_tracker --
    # and it must be the literal SAME object every time, not a fresh one per
    # file (a fresh one per file is exactly the unenforced-budget bug).
    assert len(seen_trackers) == 3
    assert all(t is not None for t in seen_trackers)
    assert seen_trackers[0] is seen_trackers[1] is seen_trackers[2]


def test_batch_result_surfaces_ai_cost_summary(monkeypatch):
    monkeypatch.setattr(main_module, "autotag_pdf", lambda path, detect_headers=True: {"nodes": []})

    def fake_remediate_impl(file, manifest, flavour=main_module.DEFAULT_FLAVOUR, cost_tracker=None):
        if cost_tracker is not None:
            cost_tracker.add("claude-sonnet-5", 1000, 200)
        return main_module.Response(
            content=b"%PDF-fake",
            media_type="application/pdf",
            headers={"X-Conformance": '{"compliant": true, "failedRules": 0}'},
        )

    monkeypatch.setattr(main_module, "_remediate_impl", fake_remediate_impl)

    client = _client()
    files = [("files", ("a.pdf", io.BytesIO(b"%PDF-1.4 fake a"), "application/pdf"))]
    submit = client.post("/jobs/batch", files=files)
    job_id = submit.json()["id"]
    _poll_result(client, job_id)

    # /jobs/{id}/result serves the ZIP for a batch job, so aiCost (a metadata
    # field on the job result, not part of the downloadable file) is read
    # straight off the job object rather than through the HTTP file route.
    snap = main_module._jobs.get(job_id).snapshot()
    assert snap["result"]["aiCost"]["calls"] == 1
    assert snap["result"]["aiCost"]["costUsd"] > 0
