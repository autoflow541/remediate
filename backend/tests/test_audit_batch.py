"""POST /jobs/audit — batch AUDIT of a document backlog without modifying any
file: an audit company's actual workflow (findings first, remediation scoped
and billed separately). Exercises the real submit -> poll -> download flow
through the async job queue, with safe_validate_pdf mocked so no real PDFs or
veraPDF binary are needed."""

from __future__ import annotations

import io
import time

from fastapi.testclient import TestClient

import app.main as main_module
from app.validate import RuleResult, ValidationResult


def _client():
    return TestClient(main_module.app)


def _result(compliant: bool, clauses: list[tuple[str, int]] = ()):
    failures = [
        RuleResult(
            specification="ISO 14289-1:2014", clause=c, test_number=t,
            status="failed", description=f"failure {c}-{t}",
            passed_checks=0, failed_checks=1, contexts=[],
        )
        for c, t in clauses
    ]
    return ValidationResult(
        compliant=compliant, flavour="PDF/UA-1",
        passed_rules=10 - len(failures), failed_rules=len(failures),
        passed_checks=50, failed_checks=len(failures), failures=failures,
    )


def _poll_result(client, job_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        status = client.get(f"/jobs/{job_id}").json()
        if status["status"] in ("done", "error"):
            return client.get(f"/jobs/{job_id}/result")
        time.sleep(0.01)
    raise AssertionError("job did not finish in time")


def _submit(client, filenames, flavour="ua1"):
    files = [("files", (name, io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")) for name in filenames]
    return client.post("/jobs/audit", files=files, data={"flavour": flavour})


def _stub_validate_in_order(monkeypatch, results: list):
    """_work() calls safe_validate_pdf once per file, strictly in submission
    order (single worker thread iterating the payload list) -- so a plain
    FIFO queue reproduces each file's canned result correctly."""
    queue = list(results)

    def fake_validate(path, flavour):
        return queue.pop(0)

    monkeypatch.setattr(main_module, "safe_validate_pdf", fake_validate)


def test_audit_batch_rollup_across_mixed_files(monkeypatch):
    # a.pdf and b.pdf share failing clause 7.3-1; c.pdf is fully compliant.
    order = ["a.pdf", "b.pdf", "c.pdf"]
    _stub_validate_in_order(monkeypatch, [
        _result(False, [("7.3", 1), ("7.1", 1)]),
        _result(False, [("7.3", 1)]),
        _result(True, []),
    ])

    client = _client()
    submit = _submit(client, order)
    assert submit.status_code == 202
    assert submit.json()["count"] == 3

    result = _poll_result(client, submit.json()["id"])
    assert result.status_code == 200
    rollup = result.json()

    assert rollup["totalFiles"] == 3
    assert rollup["processedFiles"] == 3
    assert rollup["erroredFiles"] == 0
    assert rollup["compliantFiles"] == 1
    assert rollup["nonCompliantFiles"] == 2
    assert rollup["compliancePct"] == round(100 / 3, 1)

    # 7.3-1 fails on 2 of 3 files -> the top issue, correctly cited.
    top = rollup["topIssues"][0]
    assert top["clause"] == "7.3-1"
    assert top["fileCount"] == 2
    assert top["title"] == "Figure missing alternative text"
    assert top["wcag"] == "1.1.1 Non-text Content (A)"

    assert {f["filename"] for f in rollup["files"]} == set(order)


def test_audit_batch_one_broken_file_does_not_abort_the_batch(monkeypatch):
    def fake_validate(path, flavour):
        raise RuntimeError("corrupt PDF")

    monkeypatch.setattr(main_module, "safe_validate_pdf", fake_validate)
    client = _client()
    submit = _submit(client, ["broken.pdf"])
    result = _poll_result(client, submit.json()["id"])
    rollup = result.json()
    assert rollup["erroredFiles"] == 1
    assert rollup["processedFiles"] == 0
    assert rollup["files"][0]["ok"] is False
    assert "corrupt PDF" in rollup["files"][0]["error"]


def test_audit_batch_enforces_file_limit():
    client = _client()
    too_many = [f"f{i}.pdf" for i in range(main_module.MAX_BATCH_FILES + 1)]
    resp = _submit(client, too_many)
    assert resp.status_code == 400


def test_audit_batch_pdf_a_flavour_not_wcag_enriched(monkeypatch):
    _stub_validate_in_order(monkeypatch, [_result(False, [("7.3", 1)])])
    client = _client()
    submit = _submit(client, ["a.pdf"], flavour="2b")
    result = _poll_result(client, submit.json()["id"])
    rollup = result.json()
    assert rollup["files"][0]["failures"][0].get("plain_wcag") is None
    assert rollup["topIssues"][0]["wcag"] is None
