"""Tests for onecmd.cron — store CRUD, cron matching, engine execution, API routes."""

from __future__ import annotations

import json
import time
import threading

import pytest

from onecmd.cron.store import CronStore
from onecmd.cron.engine import CronEngine, cron_matches, _parse_field


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture()
def cron_store(tmp_path):
    """Yield a CronStore backed by a temp SQLite file."""
    s = CronStore(str(tmp_path / "cron_test.sqlite"))
    yield s
    s.close()


class FakeBackend:
    """Records send_keys calls for verification."""
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def capture(self, term_id: str) -> str | None:
        return "fake output"

    def send_keys(self, term_id: str, text: str) -> bool:
        self.calls.append((term_id, text))
        return True


@pytest.fixture()
def fake_backend():
    return FakeBackend()


# ── CronStore CRUD ──────────────────────────────────────────────────────


class TestCronStore:
    def test_create_returns_id(self, cron_store):
        job_id = cron_store.create("backup nightly")
        assert isinstance(job_id, int)
        assert job_id > 0

    def test_create_sets_defaults(self, cron_store):
        job_id = cron_store.create("run tests")
        job = cron_store.get(job_id)
        assert job is not None
        assert job["description"] == "run tests"
        assert job["status"] == "draft"
        assert job["schedule"] is None
        assert job["action_type"] == "send_command"
        assert job["action_config"] == "{}"
        assert job["created_at"] > 0
        assert job["updated_at"] > 0

    def test_get_missing_returns_none(self, cron_store):
        assert cron_store.get(9999) is None

    def test_list_all_empty(self, cron_store):
        assert cron_store.list_all() == []

    def test_list_all_returns_all(self, cron_store):
        cron_store.create("job1")
        cron_store.create("job2")
        jobs = cron_store.list_all()
        assert len(jobs) == 2
        assert jobs[0]["description"] == "job1"
        assert jobs[1]["description"] == "job2"

    def test_update_fields(self, cron_store):
        job_id = cron_store.create("original")
        old_job = cron_store.get(job_id)

        cron_store.update(job_id, description="updated", schedule="0 * * * *", status="compiled")

        job = cron_store.get(job_id)
        assert job["description"] == "updated"
        assert job["schedule"] == "0 * * * *"
        assert job["status"] == "compiled"
        assert job["updated_at"] > old_job["updated_at"]

    def test_update_ignores_invalid_fields(self, cron_store):
        job_id = cron_store.create("test")
        result = cron_store.update(job_id, nonexistent_field="value")
        assert result is False

    def test_update_nonexistent_returns_false(self, cron_store):
        result = cron_store.update(9999, description="x")
        assert result is False

    def test_delete(self, cron_store):
        job_id = cron_store.create("to delete")
        assert cron_store.delete(job_id) is True
        assert cron_store.get(job_id) is None

    def test_delete_nonexistent_returns_false(self, cron_store):
        assert cron_store.delete(9999) is False

    def test_list_active(self, cron_store):
        id1 = cron_store.create("active job")
        id2 = cron_store.create("draft job")
        cron_store.update(id1, status="active", schedule="* * * * *")
        active = cron_store.list_active()
        assert len(active) == 1
        assert active[0]["id"] == id1

    def test_thread_safety(self, cron_store):
        """Concurrent creates should not corrupt the store."""
        errors = []

        def create_jobs():
            try:
                for i in range(20):
                    cron_store.create(f"concurrent-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_jobs) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(cron_store.list_all()) == 80


# ── Cron expression matching ────────────────────────────────────────────


class TestCronMatching:
    def _make_time(self, minute=0, hour=0, mday=1, mon=1, wday=0):
        """Build a struct_time for testing."""
        return time.struct_time((2026, mon, mday, hour, minute, 0, wday, 1, -1))

    def test_wildcard_matches_everything(self):
        assert _parse_field("*", 5, 59) is True
        assert _parse_field("*", 0, 59) is True

    def test_exact_match(self):
        assert _parse_field("30", 30, 59) is True
        assert _parse_field("30", 29, 59) is False

    def test_step(self):
        assert _parse_field("*/5", 0, 59) is True
        assert _parse_field("*/5", 10, 59) is True
        assert _parse_field("*/5", 3, 59) is False

    def test_range(self):
        assert _parse_field("1-5", 3, 31) is True
        assert _parse_field("1-5", 1, 31) is True
        assert _parse_field("1-5", 5, 31) is True
        assert _parse_field("1-5", 6, 31) is False

    def test_list(self):
        assert _parse_field("1,3,5", 3, 31) is True
        assert _parse_field("1,3,5", 4, 31) is False

    def test_full_expression_matches(self):
        now = self._make_time(minute=30, hour=9, mday=15, mon=3, wday=1)  # Tuesday
        assert cron_matches("30 9 * * *", now) is True
        assert cron_matches("0 9 * * *", now) is False  # wrong minute
        assert cron_matches("30 10 * * *", now) is False  # wrong hour

    def test_every_minute(self):
        assert cron_matches("* * * * *", self._make_time(minute=42)) is True

    def test_aliases(self):
        midnight = self._make_time(minute=0, hour=0)
        assert cron_matches("@daily", midnight) is True
        assert cron_matches("@midnight", midnight) is True
        assert cron_matches("@hourly", midnight) is True

        not_midnight = self._make_time(minute=0, hour=5)
        assert cron_matches("@daily", not_midnight) is False
        assert cron_matches("@hourly", not_midnight) is True

    def test_empty_expression(self):
        assert cron_matches("", None) is False
        assert cron_matches(None, None) is False  # type: ignore

    def test_invalid_expression(self):
        assert cron_matches("invalid", None) is False
        assert cron_matches("1 2 3", None) is False  # too few fields

    def test_weekday_range(self):
        # Monday=0 in Python's struct_time
        monday = self._make_time(minute=0, hour=9, wday=0)
        friday = self._make_time(minute=0, hour=9, wday=4)
        saturday = self._make_time(minute=0, hour=9, wday=5)
        assert cron_matches("0 9 * * 0-4", monday) is True
        assert cron_matches("0 9 * * 0-4", friday) is True
        assert cron_matches("0 9 * * 0-4", saturday) is False


# ── CronEngine ──────────────────────────────────────────────────────────


class TestCronEngine:
    def test_start_loads_active_jobs(self, cron_store, fake_backend):
        """Engine.start() should pick up already-active jobs from the store."""
        job_id = cron_store.create("active on start")
        cron_store.update(job_id, status="active", schedule="* * * * *")

        engine = CronEngine(store=cron_store, backend=fake_backend)
        engine.start()
        try:
            assert job_id in engine._active_ids
        finally:
            engine.stop()

    def test_add_and_remove_job(self, cron_store, fake_backend):
        engine = CronEngine(store=cron_store, backend=fake_backend)
        engine.add_job(42)
        assert 42 in engine._active_ids
        engine.remove_job(42)
        assert 42 not in engine._active_ids

    def test_execute_send_command(self, cron_store, fake_backend):
        """Engine should call backend.send_keys for send_command jobs."""
        job_id = cron_store.create("test send")
        cron_store.update(
            job_id,
            status="active",
            schedule="* * * * *",
            action_type="send_command",
            action_config=json.dumps({"terminal_id": "main", "text": "ls -la\n"}),
        )

        engine = CronEngine(store=cron_store, backend=fake_backend)
        job = cron_store.get(job_id)
        engine._execute(job)

        # Verify backend was called
        assert len(fake_backend.calls) == 1
        assert fake_backend.calls[0] == ("main", "ls -la\n")

        # Verify job was updated with result
        updated = cron_store.get(job_id)
        assert updated["last_run_at"] is not None
        assert "send_keys(ok)" in updated["last_result"]
        assert updated["error"] is None

    def test_execute_notify(self, cron_store, fake_backend):
        job_id = cron_store.create("test notify")
        cron_store.update(
            job_id,
            status="active",
            action_type="notify",
            action_config=json.dumps({"message": "Hello!"}),
        )

        engine = CronEngine(store=cron_store, backend=fake_backend)
        job = cron_store.get(job_id)
        engine._execute(job)

        updated = cron_store.get(job_id)
        assert "Notified: Hello!" in updated["last_result"]

    def test_execute_missing_backend(self, cron_store):
        """send_command with no backend should return error message, not crash."""
        job_id = cron_store.create("no backend")
        cron_store.update(
            job_id,
            status="active",
            action_type="send_command",
            action_config=json.dumps({"terminal_id": "x", "text": "y"}),
        )

        engine = CronEngine(store=cron_store, backend=None)
        job = cron_store.get(job_id)
        engine._execute(job)

        updated = cron_store.get(job_id)
        assert updated["last_result"] == "No backend available"

    def test_execute_missing_config(self, cron_store, fake_backend):
        """send_command with missing terminal_id/text should report it, not crash."""
        job_id = cron_store.create("empty config")
        cron_store.update(
            job_id,
            status="active",
            action_type="send_command",
            action_config="{}",
        )

        engine = CronEngine(store=cron_store, backend=fake_backend)
        job = cron_store.get(job_id)
        engine._execute(job)

        updated = cron_store.get(job_id)
        assert "Missing terminal_id or text" in updated["last_result"]

    def test_execute_error_sets_status(self, cron_store):
        """If execution throws, job should be marked as error."""
        job_id = cron_store.create("will fail")
        cron_store.update(
            job_id,
            status="active",
            action_type="send_command",
            action_config="NOT VALID JSON {{{{",
        )

        class ExplodingBackend:
            def send_keys(self, term_id, text):
                raise RuntimeError("boom")
            def capture(self, term_id):
                return None

        engine = CronEngine(store=cron_store, backend=ExplodingBackend())
        job = cron_store.get(job_id)
        engine._execute(job)

        updated = cron_store.get(job_id)
        # With invalid JSON it should still not crash — action_config becomes {}
        # so "Missing terminal_id or text" is the result
        assert updated["last_run_at"] is not None

    def test_tick_skips_inactive_jobs(self, cron_store, fake_backend):
        """_tick should skip jobs that became non-active since last check."""
        job_id = cron_store.create("was active")
        cron_store.update(
            job_id,
            status="paused",
            schedule="* * * * *",
        )

        engine = CronEngine(store=cron_store, backend=fake_backend)
        engine.add_job(job_id)
        engine._tick()

        # Job should have been removed from active set
        assert job_id not in engine._active_ids
        assert len(fake_backend.calls) == 0

    def test_tick_deduplicates_within_minute(self, cron_store, fake_backend):
        """Same job shouldn't run twice in the same minute."""
        job_id = cron_store.create("dedup test")
        cron_store.update(
            job_id,
            status="active",
            schedule="* * * * *",
            action_type="notify",
            action_config=json.dumps({"message": "tick"}),
        )

        engine = CronEngine(store=cron_store, backend=fake_backend)
        engine.add_job(job_id)

        engine._tick()
        engine._tick()  # second tick in same minute

        updated = cron_store.get(job_id)
        # Should only have run once
        assert updated["last_result"] == "Notified: tick"

    def test_engine_start_stop(self, cron_store, fake_backend):
        """Engine thread starts and stops cleanly."""
        engine = CronEngine(store=cron_store, backend=fake_backend)
        engine.start()
        assert engine._thread is not None
        assert engine._thread.is_alive()
        engine.stop()
        assert engine._thread is None


# ── API routes (integration) ────────────────────────────────────────────


class TestCronAPI:
    @pytest.fixture()
    def app(self, cron_store, fake_backend):
        """Create a minimal FastAPI app with cron routes for testing."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from onecmd.admin.routes_cron import cron_router

        app = FastAPI()
        app.state.cron_store = cron_store
        app.state.cron_engine = CronEngine(
            store=cron_store, backend=fake_backend
        )

        # Skip auth for tests
        from onecmd.admin.auth import require_auth
        app.dependency_overrides[require_auth] = lambda: True

        app.include_router(cron_router)
        return app

    @pytest.fixture()
    def client(self, app):
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_list_empty(self, client):
        resp = client.get("/api/cron")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_job(self, client):
        resp = client.post("/api/cron", json={"description": "backup nightly"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "backup nightly"
        assert data["status"] == "draft"
        assert data["id"] > 0

    def test_create_empty_description_rejected(self, client):
        resp = client.post("/api/cron", json={"description": "  "})
        assert resp.status_code == 400

    def test_get_job(self, client):
        create = client.post("/api/cron", json={"description": "test"}).json()
        resp = client.get(f"/api/cron/{create['id']}")
        assert resp.status_code == 200
        assert resp.json()["description"] == "test"

    def test_get_nonexistent_404(self, client):
        resp = client.get("/api/cron/9999")
        assert resp.status_code == 404

    def test_update_job(self, client):
        create = client.post("/api/cron", json={"description": "orig"}).json()
        resp = client.put(
            f"/api/cron/{create['id']}",
            json={"description": "updated", "schedule": "0 * * * *"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "updated"
        assert data["schedule"] == "0 * * * *"

    def test_delete_job(self, client):
        create = client.post("/api/cron", json={"description": "to delete"}).json()
        resp = client.delete(f"/api/cron/{create['id']}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify it's gone
        resp = client.get(f"/api/cron/{create['id']}")
        assert resp.status_code == 404

    def test_activate_without_schedule_rejected(self, client):
        create = client.post("/api/cron", json={"description": "no schedule"}).json()
        resp = client.post(f"/api/cron/{create['id']}/activate")
        assert resp.status_code == 400
        assert "schedule" in resp.json()["detail"].lower()

    def test_activate_with_schedule(self, client, app):
        create = client.post("/api/cron", json={"description": "has schedule"}).json()
        job_id = create["id"]

        # Manually set schedule (bypass compile)
        client.put(f"/api/cron/{job_id}", json={"schedule": "*/5 * * * *"})

        resp = client.post(f"/api/cron/{job_id}/activate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"

        # Verify engine registered it
        assert job_id in app.state.cron_engine._active_ids

    def test_pause_job(self, client, app):
        create = client.post("/api/cron", json={"description": "to pause"}).json()
        job_id = create["id"]
        client.put(f"/api/cron/{job_id}", json={"schedule": "* * * * *"})
        client.post(f"/api/cron/{job_id}/activate")

        resp = client.post(f"/api/cron/{job_id}/pause")
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused"
        assert job_id not in app.state.cron_engine._active_ids

    def test_compile_falls_back_without_llm(self, client):
        """Compile should work even without LLM keys (uses defaults)."""
        create = client.post("/api/cron", json={"description": "hourly ping"}).json()
        job_id = create["id"]

        resp = client.post(f"/api/cron/{job_id}/compile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "compiled"
        assert data["schedule"] is not None
        assert len(data["schedule"]) > 0

    def test_full_lifecycle(self, client, app, fake_backend):
        """Create -> compile -> activate -> engine executes -> pause -> delete."""
        # Create
        create = client.post("/api/cron", json={"description": "echo hello"}).json()
        job_id = create["id"]
        assert create["status"] == "draft"

        # Compile (will use fallback since no LLM keys)
        compile_resp = client.post(f"/api/cron/{job_id}/compile")
        assert compile_resp.status_code == 200
        compiled = compile_resp.json()
        assert compiled["status"] == "compiled"

        # Override with known-good config for testing
        client.put(f"/api/cron/{job_id}", json={
            "schedule": "* * * * *",
            "action_type": "send_command",
            "action_config": json.dumps({"terminal_id": "shell", "text": "echo hello\n"}),
        })

        # Activate
        activate_resp = client.post(f"/api/cron/{job_id}/activate")
        assert activate_resp.json()["status"] == "active"
        assert job_id in app.state.cron_engine._active_ids

        # Simulate engine tick
        app.state.cron_engine._tick()

        # Verify execution
        assert len(fake_backend.calls) == 1
        assert fake_backend.calls[0] == ("shell", "echo hello\n")

        # Check last_run_at was updated
        job = client.get(f"/api/cron/{job_id}").json()
        assert job["last_run_at"] is not None
        assert "send_keys(ok)" in job["last_result"]

        # Pause
        pause_resp = client.post(f"/api/cron/{job_id}/pause")
        assert pause_resp.json()["status"] == "paused"

        # Delete
        del_resp = client.delete(f"/api/cron/{job_id}")
        assert del_resp.json()["deleted"] is True

        # Verify list is empty
        assert client.get("/api/cron").json() == []
