"""Tests for the in-process async job registry (ROADMAP/STRATEGY #6)."""
import threading
import time

from app.jobs import Job, JobRegistry


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


def test_snapshot_never_observes_a_torn_multi_field_update():
    """touch(status="done", result=..., progress=100) applies status BEFORE
    result (kwargs are set in order). Without a lock shared with the reader, a
    poller could see status=="done" while result is still the old value — the
    exact bug this test pins down: /jobs/{id}/result would 410 a job that had
    actually succeeded, just because it read status and result separately.

    Deterministic, not timing-dependent: the writer thread holds the job's
    lock across the whole multi-field update (simulating the worst-case gap
    between the status and result writes), so a snapshot() call from another
    thread MUST block until the update is fully applied — it can never
    observe status=="done" paired with the pre-update result.
    """
    job = Job(id="x", kind="test")
    writer_holds_lock = threading.Event()
    release_writer = threading.Event()

    def slow_multi_field_write():
        with job._lock:
            job.status = "done"          # first field applied...
            writer_holds_lock.set()
            release_writer.wait(timeout=2)
            job.result = {"path": "/tmp/out.pdf"}  # ...second field, much later
        job.updated_at = time.time()

    t = threading.Thread(target=slow_multi_field_write)
    t.start()
    assert writer_holds_lock.wait(timeout=2), "writer never reached the lock"

    reader_result: dict = {}

    def reader():
        reader_result.update(job.snapshot())

    r = threading.Thread(target=reader)
    r.start()
    time.sleep(0.05)  # give the reader a chance to (wrongly) return early
    assert not reader_result, "snapshot() returned before the write finished"

    release_writer.set()
    t.join(timeout=2)
    r.join(timeout=2)

    # The reader is guaranteed to see the fully-applied state, never a mix.
    assert reader_result["status"] == "done"
    assert reader_result["result"] == {"path": "/tmp/out.pdf"}


def test_job_result_endpoint_reads_one_atomic_snapshot(monkeypatch):
    """Regression guard against reverting to job.status / job.result as two
    separate unsynchronized attribute reads: /jobs/{id}/result and
    /jobs/{id}/report must each call snapshot() exactly once per request, so
    status and result always come from the same consistent point in time."""
    from fastapi.testclient import TestClient
    import app.main as main_module

    job = main_module._jobs.submit("test", lambda j: {"path": "/nonexistent.pdf"})
    _wait(main_module._jobs, job.id)

    calls = {"n": 0}
    real_snapshot = job.snapshot

    def counting_snapshot():
        calls["n"] += 1
        return real_snapshot()

    monkeypatch.setattr(job, "snapshot", counting_snapshot)

    client = TestClient(main_module.app)
    client.get(f"/jobs/{job.id}/result")
    assert calls["n"] == 1

    calls["n"] = 0
    client.get(f"/jobs/{job.id}/report")
    assert calls["n"] == 1
