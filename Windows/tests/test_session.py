"""
Unit tests — Session Recorder
"""

import sys
import json
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestSessionRecorder:

    def test_log_step_records_entry(self, tmp_path, monkeypatch):
        """log_step should append a record to internal steps list."""
        import analytics.session as sess
        monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

        rec = sess.SessionRecorder("test@example.com", "test_workflow")
        rec.log_step("step_one", "SUCCESS", 2.5, "direct")

        assert len(rec._steps) == 1
        assert rec._steps[0]["step_name"] == "step_one"
        assert rec._steps[0]["status"] == "SUCCESS"
        assert rec._steps[0]["duration_seconds"] == 2.5

    def test_success_rate_all_success(self, tmp_path, monkeypatch):
        """100% success rate when all steps succeed."""
        import analytics.session as sess
        monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

        rec = sess.SessionRecorder("a@b.com", "workflow")
        for i in range(5):
            rec.log_step(f"step_{i}", "SUCCESS", 1.0, "direct")

        assert rec.calculate_success_rate() == 1.0

    def test_success_rate_with_failures(self, tmp_path, monkeypatch):
        """Success rate should be 0.5 when half the steps fail."""
        import analytics.session as sess
        monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

        rec = sess.SessionRecorder("a@b.com", "workflow")
        rec.log_step("s1", "SUCCESS", 1.0, "direct")
        rec.log_step("s2", "SUCCESS", 1.0, "direct")
        rec.log_step("s3", "FAILURE", 1.0, "direct")
        rec.log_step("s4", "FAILURE", 1.0, "direct")

        assert rec.calculate_success_rate() == 0.5

    def test_skip_steps_excluded_from_rate(self, tmp_path, monkeypatch):
        """SKIP steps should not affect the success rate calculation."""
        import analytics.session as sess
        monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

        rec = sess.SessionRecorder("a@b.com", "workflow")
        rec.log_step("s1", "SUCCESS", 1.0, "direct")
        rec.log_step("s2", "SKIP", 0.0, "skip_condition")

        assert rec.calculate_success_rate() == 1.0

    def test_empty_session_rate_is_one(self, tmp_path, monkeypatch):
        """Empty session should return 1.0 (perfect, no failures)."""
        import analytics.session as sess
        monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

        rec = sess.SessionRecorder("a@b.com", "workflow")
        assert rec.calculate_success_rate() == 1.0

    def test_save_session_writes_json(self, tmp_path, monkeypatch):
        """save_session should write a readable JSON file."""
        import analytics.session as sess
        monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

        rec = sess.SessionRecorder("client@test.com", "salon")
        rec.log_step("create_project", "SUCCESS", 5.0, "direct")
        path = rec.save_session("https://salon.vercel.app")

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["user_email"] == "client@test.com"
        assert data["live_url"] == "https://salon.vercel.app"
        assert data["success_rate"] == 1.0
        assert len(data["steps"]) == 1

    def test_most_failed_steps(self, tmp_path, monkeypatch):
        """most_failed_steps should rank by failure count."""
        import analytics.session as sess
        monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

        rec = sess.SessionRecorder("a@b.com", "wf")
        for _ in range(3):
            rec.log_step("push_github", "FAILURE", 1.0, "direct")
        for _ in range(1):
            rec.log_step("vercel_deploy", "FAILURE", 1.0, "direct")

        top = rec.most_failed_steps(top_n=1)
        assert top[0]["step_name"] == "push_github"
        assert top[0]["failure_count"] == 3

    def test_context_manager_saves_on_exit(self, tmp_path, monkeypatch):
        """Using SessionRecorder as a context manager should auto-save."""
        import analytics.session as sess
        monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

        with sess.SessionRecorder("a@b.com", "auto_save") as rec:
            rec.log_step("s1", "SUCCESS", 1.0, "direct")

        # File should exist after context exit
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
