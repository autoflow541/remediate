"""In-process async job queue for long operations (ROADMAP/STRATEGY #6).

`/remediate` runs ~55-150s and dies at the Caddy proxy on cold starts (HTTP 000).
This provides a **submit -> poll -> download** model: submit returns a job id
immediately (202), the work runs in a background worker, and the client polls
status and downloads the result when done.

MVP scope (deliberate): single-process, in-memory registry + a bounded thread
pool. Finished jobs and their result files are swept after JOB_TTL_SECONDS. Not
durable across restarts — fine for a stateless remediation worker; swap the
registry for Redis/DB when horizontal scaling is needed.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

MAX_WORKERS = int(os.environ.get("JOB_WORKERS", "2"))
RESULT_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "3600"))


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | error
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: int = 0       # 0-100, worker may update
    result: Any = None      # e.g. {"path": <pdf>, "meta": {...}} or {"json": {...}}
    error: str | None = None

    def touch(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.updated_at = time.time()

    def public(self) -> dict:
        """JSON-safe status view (never leaks the on-disk result path)."""
        d = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.status == "error":
            d["error"] = self.error
        if self.status == "done" and isinstance(self.result, dict):
            d["result"] = {k: v for k, v in self.result.items() if k not in ("path", "json", "reportData")}
            d["hasFile"] = bool(self.result.get("path"))
            d["hasJson"] = self.result.get("json") is not None
        return d


class JobRegistry:
    def __init__(self, max_workers: int = MAX_WORKERS, ttl: int = RESULT_TTL_SECONDS):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._ttl = ttl

    def submit(self, kind: str, fn: Callable[[Job], Any]) -> Job:
        """Register a job and run fn(job) in the background. fn's return value
        becomes job.result; any exception marks the job errored."""
        self._sweep()
        job = Job(id=uuid.uuid4().hex, kind=kind)
        with self._lock:
            self._jobs[job.id] = job
        self._pool.submit(self._run, job, fn)
        return job

    def _run(self, job: Job, fn: Callable[[Job], Any]) -> None:
        job.touch(status="running")
        try:
            result = fn(job)
            job.touch(status="done", result=result, progress=100)
        except Exception as exc:  # surface any worker failure as a job error
            log.warning("job %s (%s) failed: %s", job.id, job.kind, exc)
            job.touch(status="error", error=str(exc)[:500])

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _sweep(self) -> None:
        cutoff = time.time() - self._ttl
        with self._lock:
            stale = [
                jid for jid, j in self._jobs.items()
                if j.status in ("done", "error") and j.updated_at < cutoff
            ]
            for jid in stale:
                j = self._jobs.pop(jid)
                path = j.result.get("path") if isinstance(j.result, dict) else None
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def stats(self) -> dict:
        with self._lock:
            by: dict[str, int] = {}
            for j in self._jobs.values():
                by[j.status] = by.get(j.status, 0) + 1
            return {"total": len(self._jobs), "byStatus": by, "workers": MAX_WORKERS}


# Module-level singleton used by the API.
registry = JobRegistry()
