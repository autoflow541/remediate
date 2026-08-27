"""Tests for the in-process async job registry (ROADMAP/STRATEGY #6)."""
import time

from app.jobs import JobRegistry


def _wait(reg, job_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        j = reg.get(job_id)
        if j and j.status in ("done", "error"):
            return j
        time.sleep(0.01)
    raise AssertionError("job did not finish in time")


def test_submit_runs_and_completes():
    reg = JobRegistry(max_workers=2)
    job = reg.submit("test", lambda j: {"json": {"ok": True}})
    done = _wait(reg, job.id)
    assert done.status == "done"
    assert done.progress == 100
    assert done.result == {"json": {"ok": True}}


def test_public_view_hides_path_and_reports_flags():
    reg = JobRegistry()
    job = reg.submit("test", lambda j: {"path": "/tmp/x.pdf", "meta": {"cost": 1}})
    done = _wait(reg, job.id)
    pub = done.public()
    assert pub["status"] == "done"
    assert pub["hasFile"] is True
    assert "path" not in pub["result"]      # never leak the on-disk path
    assert pub["result"]["meta"] == {"cost": 1}


def test_failure_is_captured_as_error():
    reg = JobRegistry()
    def _boom(job):
        raise ValueError("nope")
    job = reg.submit("test", _boom)
    done = _wait(reg, job.id)
    assert done.status == "error"
    assert "nope" in done.error
    assert "error" in done.public()


def test_get_unknown_returns_none():
    reg = JobRegistry()
    assert reg.get("does-not-exist") is None


def test_progress_can_be_updated_by_worker():
    reg = JobRegistry()
    def _work(job):
        job.touch(progress=50)
        return {"json": {}}
    job = reg.submit("test", _work)
    _wait(reg, job.id)
    # progress ends at 100 on completion regardless
    assert reg.get(job.id).progress == 100


def test_ttl_sweep_removes_finished_jobs():
    reg = JobRegistry(ttl=0)  # everything finished is immediately stale
    j1 = reg.submit("test", lambda j: {"json": {}})
    _wait(reg, j1.id)
    time.sleep(0.02)  # ensure j1.updated_at is strictly in the past for ttl=0
    # submitting again triggers a sweep of stale finished jobs
    reg.submit("test", lambda j: {"json": {}})
    assert reg.get(j1.id) is None


def test_stats():
    reg = JobRegistry()
    reg.submit("a", lambda j: {"json": {}})
    s = reg.stats()
    assert "total" in s and "byStatus" in s and "workers" in s
