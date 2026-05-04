"""
Unit tests — Auto-Update System
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCheckForUpdates:

    def test_update_available_when_remote_newer(self, tmp_path, monkeypatch):
        """Should report update_available=True when remote > local."""
        import updater.auto_update as au
        version_file = tmp_path / "VERSION"
        version_file.write_text("1.0.0")
        monkeypatch.setattr(au, "VERSION_FILE", version_file)

        manifest = {"latest_version": "1.2.0", "download_url": "https://x.com/d.zip", "changelog": "New stuff"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = manifest
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            result = au.check_for_updates("https://firebase.json")

        assert result["update_available"] is True
        assert result["version"] == "1.2.0"

    def test_no_update_when_same_version(self, tmp_path, monkeypatch):
        """Should report update_available=False when versions match."""
        import updater.auto_update as au
        version_file = tmp_path / "VERSION"
        version_file.write_text("1.2.0")
        monkeypatch.setattr(au, "VERSION_FILE", version_file)

        manifest = {"latest_version": "1.2.0", "download_url": "", "changelog": ""}
        mock_resp = MagicMock()
        mock_resp.json.return_value = manifest
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            result = au.check_for_updates("https://firebase.json")

        assert result["update_available"] is False

    def test_no_update_when_network_fails(self, tmp_path, monkeypatch):
        """Network failure should return update_available=False gracefully."""
        import updater.auto_update as au
        import requests

        version_file = tmp_path / "VERSION"
        version_file.write_text("1.0.0")
        monkeypatch.setattr(au, "VERSION_FILE", version_file)

        with patch("requests.get", side_effect=requests.exceptions.ConnectionError()):
            result = au.check_for_updates("https://firebase.json")

        assert result["update_available"] is False

    def test_no_url_skips_check(self, tmp_path, monkeypatch):
        """Missing URL should skip check without error."""
        import updater.auto_update as au
        version_file = tmp_path / "VERSION"
        version_file.write_text("1.0.0")
        monkeypatch.setattr(au, "VERSION_FILE", version_file)
        monkeypatch.delenv("FIREBASE_UPDATE_URL", raising=False)

        result = au.check_for_updates(firebase_url=None)
        assert result["update_available"] is False


class TestKeepPreviousVersion:

    def test_creates_backup_directory(self, tmp_path, monkeypatch):
        """keep_previous_version should create a backup directory."""
        import updater.auto_update as au

        # Set up a fake project structure
        fake_base = tmp_path / "project"
        fake_base.mkdir()
        (fake_base / "VERSION").write_text("1.0.0")
        (fake_base / "main.py").write_text("# main")

        backup_dir = tmp_path / ".backups"

        monkeypatch.setattr(au, "BASE_DIR", fake_base)
        monkeypatch.setattr(au, "BACKUP_DIR", backup_dir)
        monkeypatch.setattr(au, "VERSION_FILE", fake_base / "VERSION")

        path = au.keep_previous_version()

        assert path.exists()
        assert (path / "main.py").exists()


class TestVersionParsing:

    def test_parse_version_normal(self):
        import updater.auto_update as au
        assert au._parse_version("1.2.3") == (1, 2, 3)

    def test_parse_version_invalid_returns_zero(self):
        import updater.auto_update as au
        assert au._parse_version("not-a-version") == (0, 0, 0)

    def test_parse_version_comparison(self):
        import updater.auto_update as au
        assert au._parse_version("2.0.0") > au._parse_version("1.9.9")
