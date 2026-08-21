import time

from averon_import.services.jobs import JobService


def test_finished_jobs_are_cleaned_after_ttl():
    service = JobService(max_workers=1, ttl_seconds=0.01, max_finished_jobs=100)
    job = service.submit(lambda progress: {"ok": True})

    deadline = time.time() + 2
    while job.status not in {"completed", "failed"} and time.time() < deadline:
        time.sleep(0.01)

    assert job.status == "completed"
    time.sleep(0.02)
    assert service.cleanup() == 1
