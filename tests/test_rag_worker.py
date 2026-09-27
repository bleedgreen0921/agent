import psycopg
import pytest

from rag_service import worker


class StopPolling(Exception):
    pass


def test_polling_database_failures_back_off_and_recover(monkeypatch):
    outcomes = iter((
        psycopg.OperationalError("database unavailable"),
        psycopg.OperationalError("database unavailable"),
        psycopg.OperationalError("database unavailable"),
        psycopg.OperationalError("database unavailable"),
        False,
        StopPolling(),
    ))
    sleeps = []

    def run_once(_presence):
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr(worker, "validate_role", lambda _role: None)
    monkeypatch.setattr(worker.Presence, "touch", lambda _self: None)
    monkeypatch.setattr(worker, "remove", lambda *_args: None)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    monkeypatch.setattr(worker, "MAX_POLL_BACKOFF_SECONDS", 3)

    with pytest.raises(StopPolling):
        worker.main()

    assert sleeps == [1, 2, 3, 3, 1]


def test_processing_database_failure_is_retryable(monkeypatch):
    job = {"id": "job", "version_id": "version", "token": "token", "attempts": 1}
    reported = []
    monkeypatch.setattr(worker, "process", lambda claimed: (_ for _ in ()).throw(psycopg.OperationalError("database unavailable")))
    monkeypatch.setattr(worker.os, "setsid", lambda: None)

    class Outcome:
        def put(self, value):
            reported.append(value)

    worker.child_main(job, Outcome())
    assert reported == [("DATABASE_UNAVAILABLE", True)]


def test_failure_recording_outage_does_not_escape_run_once(monkeypatch):
    job = {"id": "job", "version_id": "version", "token": "token", "attempts": 1}
    monkeypatch.setattr(worker, "fail", lambda *args: (_ for _ in ()).throw(psycopg.OperationalError("database still unavailable")))

    worker.fail_safely(job, "DATABASE_UNAVAILABLE", True)
