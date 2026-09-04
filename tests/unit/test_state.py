#!/usr/bin/env python3
"""The durable state store: transitions, idempotency, evidence, and bounded waits."""

from __future__ import annotations

import sqlite3
import threading

import pytest
from agent_duet.models import Phase
from agent_duet.state import RunRecord, StateError, StateStore, utcnow


def make_record(tmp_path, run_id="00000000-0000-4000-8000-000000000001") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        created_at=utcnow(),
        updated_at=utcnow(),
        phase=Phase.QUEUED,
        terminal=False,
        repo_path=str(tmp_path / "repo"),
        git_common_dir=str(tmp_path / "repo" / ".git"),
        delivery_mode="review_branch",
        task="do the thing",
        run_dir=str(tmp_path / "run"),
    )


def test_create_and_get(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    loaded = store.get(record.run_id)
    assert loaded.task == "do the thing"
    assert loaded.phase is Phase.QUEUED
    assert loaded.terminal is False


def test_unknown_run_raises(store):
    with pytest.raises(StateError, match="unknown run_id"):
        store.get("00000000-0000-4000-8000-00000000dead")


def test_legal_transition_records_an_event(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING, reason="starting")
    events = store.events(record.run_id)
    assert [phase for _, phase, _ in events] == ["QUEUED", "CLAUDE_IMPLEMENTING"]
    assert events[-1][2] == "starting"


def test_illegal_transition_is_refused(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    with pytest.raises(StateError, match="illegal transition"):
        store.transition(record.run_id, Phase.AWAITING_FINALIZE)


def test_cannot_leave_a_terminal_phase(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    store.transition(record.run_id, Phase.FAILED, reason="boom")
    with pytest.raises(StateError, match="illegal transition"):
        store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING)


def test_terminal_flag_is_derived_not_supplied(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    updated = store.transition(record.run_id, Phase.CANCELLED, reason="stop")
    assert updated.terminal is True


def test_reentering_the_same_phase_is_allowed(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING)
    store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING, reason="resumed")
    assert store.get(record.run_id).phase is Phase.CLAUDE_IMPLEMENTING


def test_idempotency_key_is_unique(store, tmp_path):
    first = make_record(tmp_path)
    first.idempotency_key = "same"
    store.create_run(first)
    second = make_record(tmp_path, run_id="00000000-0000-4000-8000-000000000002")
    second.idempotency_key = "same"
    with pytest.raises(StateError):
        store.create_run(second)


def test_find_by_idempotency_key(store, tmp_path):
    record = make_record(tmp_path)
    record.idempotency_key = "key-1"
    store.create_run(record)
    assert store.find_by_idempotency_key("key-1").run_id == record.run_id
    assert store.find_by_idempotency_key("absent") is None


def test_null_idempotency_keys_do_not_collide(store, tmp_path):
    store.create_run(make_record(tmp_path))
    store.create_run(make_record(tmp_path, run_id="00000000-0000-4000-8000-000000000002"))
    assert len(store.all_runs()) == 2


def test_evidence_merges_rather_than_replaces(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    store.merge_evidence(record.run_id, {"a": 1})
    store.merge_evidence(record.run_id, {"b": 2})
    evidence = store.get(record.run_id).evidence
    assert evidence == {"a": 1, "b": 2}


def test_transition_merges_evidence_too(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    store.merge_evidence(record.run_id, {"kept": True})
    store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING, evidence={"added": 1})
    assert store.get(record.run_id).evidence == {"kept": True, "added": 1}


def test_active_runs_excludes_terminal(store, tmp_path):
    first = store.create_run(make_record(tmp_path))
    second = store.create_run(
        make_record(tmp_path, run_id="00000000-0000-4000-8000-000000000002")
    )
    store.transition(second.run_id, Phase.FAILED, reason="done")
    active = store.active_runs()
    assert [item.run_id for item in active] == [first.run_id]


def test_active_runs_filters_by_repo(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    assert store.active_runs(record.repo_path)
    assert store.active_runs("/some/other/repo") == []


def test_list_columns_round_trip(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    store.update(record.run_id, owned_paths=["a.py", "b/c.py"])
    assert store.get(record.run_id).owned_paths == ["a.py", "b/c.py"]


def test_cancel_flag_persists(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    store.request_cancel(record.run_id)
    assert store.get(record.run_id).cancel_requested is True


def test_update_on_unknown_run_raises(store):
    with pytest.raises(StateError, match="unknown run_id"):
        store.update("00000000-0000-4000-8000-00000000dead", summary="x")


def test_wait_returns_on_change(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    since = store.get(record.run_id).updated_at

    def flip():
        import time

        time.sleep(0.6)
        store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING)

    thread = threading.Thread(target=flip)
    thread.start()
    result = store.wait_for_change(record.run_id, since=since, timeout_seconds=10)
    thread.join()
    assert result.phase is Phase.CLAUDE_IMPLEMENTING


def test_wait_is_bounded_and_still_returns(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    since = store.get(record.run_id).updated_at
    result = store.wait_for_change(record.run_id, since=since, timeout_seconds=1)
    assert result.phase is Phase.QUEUED


def test_wait_returns_immediately_when_awaiting_finalize(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    for phase in (
        Phase.CLAUDE_IMPLEMENTING,
        Phase.HANDOFF_VALIDATING,
        Phase.CODEX_REVIEWING,
        Phase.REVIEW_INTEGRITY_CHECK,
        Phase.CLAUDE_RECONCILING,
        Phase.FINAL_VALIDATING,
        Phase.AWAITING_FINALIZE,
    ):
        store.transition(record.run_id, phase)
    result = store.wait_for_change(record.run_id, since="never", timeout_seconds=300)
    assert result.phase is Phase.AWAITING_FINALIZE


def test_database_uses_wal_and_is_private(store, config):
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert oct(config.db_path.stat().st_mode & 0o777) == "0o600"


def test_reopening_the_store_sees_prior_runs(config, store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    reopened = StateStore(config.db_path)
    assert reopened.get(record.run_id).run_id == record.run_id


def test_status_projection_carries_evidence(store, tmp_path):
    record = store.create_run(make_record(tmp_path))
    store.merge_evidence(record.run_id, {"working_diff_sha256": "abc"})
    status = store.get(record.run_id).to_status()
    assert status.evidence.working_diff_sha256 == "abc"
    assert status.next_action


def test_foreign_keys_are_enforced(store):
    with store.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (run_id, at, phase, reason) VALUES ('missing','now','QUEUED','x')"
        )
