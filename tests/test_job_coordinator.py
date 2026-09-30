from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from averon_import.services.jobs import (
    DOCUMENT_PROCESSING,
    SOURCING,
    JobAdmissionError,
    JobCoordinator,
)


def _wait_status(service: JobCoordinator, job_id: str, status: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get(job_id)
        if job.status == status:
            return job
        time.sleep(0.005)
    raise AssertionError(f"job did not reach {status}")


def test_blocked_sourcing_lane_does_not_block_document_processing():
    jobs = JobCoordinator()
    sourcing_started = threading.Event()
    release_sourcing = threading.Event()
    document_finished = threading.Event()
    try:
        sourcing = jobs.submit(
            lambda _progress: (sourcing_started.set(), release_sourcing.wait(2))[1],
            lane=SOURCING,
            owner_id="actor-a",
        )
        assert sourcing_started.wait(1)
        document = jobs.submit(
            lambda _progress: document_finished.set() or {"ok": True},
            lane=DOCUMENT_PROCESSING,
            owner_id="actor-a",
        )
        assert document_finished.wait(1)
        assert jobs.get(document.id).status == "completed"
        assert jobs.get(sourcing.id).status == "running"
    finally:
        release_sourcing.set()
        jobs.executor.shutdown(wait=True)


@pytest.mark.parametrize("lane", [DOCUMENT_PROCESSING, SOURCING])
def test_each_serialized_lane_never_runs_two_jobs_at_once(lane: str):
    jobs = JobCoordinator()
    first_started = threading.Event()
    release_first = threading.Event()
    guard = threading.Lock()
    active = 0
    maximum = 0

    def run(block: bool):
        def operation(_progress):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            if block:
                first_started.set()
                release_first.wait(2)
            with guard:
                active -= 1
            return True
        return operation

    try:
        first = jobs.submit(run(True), lane=lane)
        assert first_started.wait(1)
        rest = [jobs.submit(run(False), lane=lane) for _ in range(2)]
        release_first.set()
        _wait_status(jobs, rest[-1].id, "completed")
        assert maximum == 1
    finally:
        release_first.set()
        jobs.executor.shutdown(wait=True)


def test_lane_capacity_allows_one_running_and_exactly_two_queued():
    jobs = JobCoordinator()
    started = threading.Event()
    release = threading.Event()
    released_rejected = []
    try:
        first = jobs.submit(
            lambda _progress: (started.set(), release.wait(2))[1], lane=SOURCING
        )
        assert started.wait(1)
        queued = [jobs.submit(lambda _progress: True, lane=SOURCING) for _ in range(2)]
        with pytest.raises(JobAdmissionError) as error:
            jobs.submit(
                lambda _progress: True,
                lane=SOURCING,
                release_callbacks=[lambda: released_rejected.append("released")],
            )
        assert error.value.code == "JOB_LANE_BUSY"
        assert [jobs.get(item.id).status for item in queued] == ["queued", "queued"]
        assert released_rejected == ["released"]
        assert jobs.get(first.id).status == "running"
    finally:
        release.set()
        jobs.executor.shutdown(wait=True)


def test_lane_capacity_is_reserved_before_executor_starts_jobs():
    class DeferredExecutor:
        def __init__(self):
            self.submitted = []

        def submit(self, function):
            future = Future()
            self.submitted.append((function, future))
            return future

        def shutdown(self, wait=True, *, cancel_futures=False):
            return None

    jobs = JobCoordinator()
    deferred = DeferredExecutor()
    jobs._executors[SOURCING] = deferred
    try:
        accepted = [jobs.submit(lambda _progress: True, lane=SOURCING) for _ in range(3)]
        assert len(deferred.submitted) == 3
        assert [job.status for job in accepted] == ["queued", "queued", "queued"]
        assert sum(job.active and job.lane == SOURCING for job in jobs.jobs.values()) == 3
        with pytest.raises(JobAdmissionError) as error:
            jobs.submit(lambda _progress: True, lane=SOURCING)
        assert error.value.code == "JOB_LANE_BUSY"
        assert len(deferred.submitted) == 3
        assert [job.status for job in accepted] == ["queued", "queued", "queued"]
    finally:
        jobs.executor.shutdown(wait=True)


def test_get_public_returns_a_public_snapshot():
    jobs = JobCoordinator()
    started = threading.Event()
    release = threading.Event()
    try:
        job = jobs.submit(
            lambda _progress: (started.set(), release.wait(2))[1],
            lane=SOURCING,
            owner_id="private-owner",
        )
        assert started.wait(1)
        original_public = job.public
        lock_attempted = threading.Event()
        lock_acquired = threading.Event()

        def verify_snapshot_lock():
            def contend_for_snapshot_lock():
                acquired = jobs.lock.acquire(blocking=False)
                if acquired:
                    lock_acquired.set()
                    jobs.lock.release()
                lock_attempted.set()

            contender = threading.Thread(target=contend_for_snapshot_lock)
            contender.start()
            assert lock_attempted.wait(1)
            contender.join(1)
            assert not lock_acquired.is_set()
            return original_public()

        job.public = verify_snapshot_lock
        snapshot = jobs.get_public(job.id, owner_id="private-owner")
        assert snapshot["id"] == job.id
        assert snapshot["status"] == "running"
        assert "owner_id" not in snapshot
        assert "traceback" not in snapshot
        assert "_future" not in snapshot
    finally:
        release.set()
        jobs.executor.shutdown(wait=True)


def test_active_duplicate_is_owner_scoped_and_terminal_runs_can_repeat():
    jobs = JobCoordinator()
    started = threading.Event()
    release = threading.Event()
    operation = lambda _progress: (started.set(), release.wait(2))[1]
    try:
        first = jobs.submit(
            operation, lane=SOURCING, owner_id="owner-a", dedupe_key="stable-key"
        )
        assert started.wait(1)
        duplicate = jobs.submit(
            lambda _progress: pytest.fail("duplicate was executed"),
            lane=SOURCING,
            owner_id="owner-a",
            dedupe_key="stable-key",
        )
        other_owner = jobs.submit(
            lambda _progress: True,
            lane=SOURCING,
            owner_id="owner-b",
            dedupe_key="stable-key",
        )
        assert duplicate.id == first.id
        assert other_owner.id != first.id
        assert jobs.get(first.id, owner_id="owner-a") is first
        with pytest.raises(KeyError):
            jobs.get(first.id, owner_id="owner-b")
        release.set()
        _wait_status(jobs, first.id, "completed")
        repeated = jobs.submit(
            lambda _progress: "new run",
            lane=SOURCING,
            owner_id="owner-a",
            dedupe_key="stable-key",
        )
        assert repeated.id != first.id
        assert _wait_status(jobs, repeated.id, "completed").result == "new run"
    finally:
        release.set()
        jobs.executor.shutdown(wait=True)


def test_same_document_conflict_across_lanes_and_distinct_documents_progress():
    jobs = JobCoordinator()
    started = threading.Event()
    release = threading.Event()
    try:
        first = jobs.submit(
            lambda _progress: (started.set(), release.wait(2))[1],
            lane=DOCUMENT_PROCESSING,
            owner_id="owner",
            document_id="a" * 32,
            dedupe_key="ocr-a",
        )
        assert started.wait(1)
        assert jobs.submit(
            lambda _progress: pytest.fail("active duplicate was executed"),
            lane=DOCUMENT_PROCESSING,
            owner_id="owner",
            document_id="a" * 32,
            dedupe_key="ocr-a",
        ).id == first.id
        with pytest.raises(JobAdmissionError) as error:
            jobs.submit(
                lambda _progress: True,
                lane=SOURCING,
                owner_id="owner",
                document_id="a" * 32,
                dedupe_key="source-a",
            )
        assert error.value.code == "DOCUMENT_JOB_BUSY"
        second_doc = threading.Event()
        separate = jobs.submit(
            lambda _progress: second_doc.set() or True,
            lane=SOURCING,
            owner_id="owner",
            document_id="b" * 32,
        )
        assert second_doc.wait(1)
        assert jobs.get(separate.id).status == "completed"
    finally:
        release.set()
        jobs.executor.shutdown(wait=True)


def test_queued_expiry_cancels_unstarted_future_and_releases_resources_once():
    jobs = JobCoordinator(queue_wait_seconds=0.03)
    started = threading.Event()
    release_first = threading.Event()
    queued_started = threading.Event()
    releases = []
    try:
        running = jobs.submit(
            lambda _progress: (started.set(), release_first.wait(2))[1], lane=DOCUMENT_PROCESSING
        )
        assert started.wait(1)
        queued = jobs.submit(
            lambda _progress: queued_started.set(),
            lane=DOCUMENT_PROCESSING,
            release_callbacks=[lambda: releases.append("once")],
        )
        time.sleep(0.06)
        expired = jobs.get(queued.id)
        assert expired.status == "expired"
        assert expired.error_code == "JOB_QUEUE_EXPIRED"
        assert not queued_started.is_set()
        assert releases == ["once"]
        assert jobs.get(queued.id).status == "expired"
        assert releases == ["once"]
        release_first.set()
        _wait_status(jobs, running.id, "completed")
    finally:
        release_first.set()
        jobs.executor.shutdown(wait=True)


def test_all_terminal_paths_release_callbacks_once_and_retention_keeps_active_jobs():
    jobs = JobCoordinator(terminal_ttl_seconds=0.02, max_terminal_jobs=2)
    active_started = threading.Event()
    release_active = threading.Event()
    releases = {"complete": 0, "fail": 0, "reject": 0}

    def bump(key):
        return lambda: releases.__setitem__(key, releases[key] + 1)

    try:
        completed = jobs.submit(
            lambda _progress: "ok",
            release_callbacks=[bump("complete")],
        )
        failed = jobs.submit(
            lambda _progress: (_ for _ in ()).throw(RuntimeError("private token")),
            lane=SOURCING,
            release_callbacks=[bump("fail")],
        )
        _wait_status(jobs, completed.id, "completed")
        _wait_status(jobs, failed.id, "failed")
        assert releases["complete"] == releases["fail"] == 1
        assert jobs.get(failed.id).public()["error"]
        assert "traceback" not in failed.public()

        active = jobs.submit(
            lambda _progress: (active_started.set(), release_active.wait(2))[1],
            lane=DOCUMENT_PROCESSING,
        )
        assert active_started.wait(1)
        for _ in range(3):
            job = jobs.submit(lambda _progress: True, lane=SOURCING)
            _wait_status(jobs, job.id, "completed")
        assert jobs.get(active.id).status == "running"
        terminals = [job for job in jobs.jobs.values() if not job.active]
        assert len(terminals) <= 2
        time.sleep(0.03)
        assert jobs.get(active.id).status == "running"
        release_active.set()
        _wait_status(jobs, active.id, "completed")
    finally:
        release_active.set()
        jobs.executor.shutdown(wait=True)


def test_executor_submission_failure_releases_resource_once():
    jobs = JobCoordinator()
    jobs.executor.shutdown(wait=True)
    releases = []
    with pytest.raises(RuntimeError):
        jobs.submit(
            lambda _progress: True,
            release_callbacks=[lambda: releases.append(1)],
        )
    assert releases == [1]


def test_public_job_error_code_does_not_leak_untrusted_code_text():
    class UnsafeCodeError(RuntimeError):
        code = "secret-token-value"

    jobs = JobCoordinator()
    try:
        job = jobs.submit(
            lambda _progress: (_ for _ in ()).throw(UnsafeCodeError("authorization token secret")),
            lane=SOURCING,
        )
        failed = _wait_status(jobs, job.id, "failed")
        assert failed.public()["error_code"] == "JOB_FAILED"
        assert "secret" not in failed.public()["error"].casefold()
        assert "token" not in failed.public()["error"].casefold()
        assert "traceback" not in failed.public()
    finally:
        jobs.executor.shutdown(wait=True)
