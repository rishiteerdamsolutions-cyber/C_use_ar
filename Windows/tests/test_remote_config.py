"""
Unit tests — Remote Config Module
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestFetchRemoteConfig:

    def test_returns_dict_from_firebase(self, tmp_path, monkeypatch):
        """Should return config dict when Firebase responds successfully."""
        import config.remote_config as rc
        monkeypatch.setattr(rc, "CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(rc, "VERSION_FILE", tmp_path / "VERSION")

        fake_config = {"version": "1.2.0", "platforms": {}}
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_config
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            result = rc.fetch_remote_config("https://fake-firebase.json")

        assert result["version"] == "1.2.0"
        assert isinstance(result, dict)

    def test_falls_back_to_cache_when_firebase_unreachable(self, tmp_path, monkeypatch):
        """Should silently fall back to cache when network fails."""
        import config.remote_config as rc
        import requests

        cache_data = {"version": "1.0.0-cached", "platforms": {}}
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps(cache_data))

        monkeypatch.setattr(rc, "CACHE_FILE", cache_file)
        monkeypatch.setattr(rc, "VERSION_FILE", tmp_path / "VERSION")

        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("no internet")):
            result = rc.fetch_remote_config("https://fake-firebase.json")

        assert result["version"] == "1.0.0-cached"

    def test_raises_when_no_firebase_and_no_cache(self, tmp_path, monkeypatch):
        """Should raise RuntimeError when both Firebase and cache unavailable."""
        import config.remote_config as rc
        import requests

        monkeypatch.setattr(rc, "CACHE_FILE", tmp_path / "nonexistent_cache.json")

        with patch("requests.get", side_effect=requests.exceptions.ConnectionError()):
            with pytest.raises(RuntimeError, match="Remote config unavailable"):
                rc.fetch_remote_config("https://fake-firebase.json")

    def test_cache_is_written_after_successful_fetch(self, tmp_path, monkeypatch):
        """Cache file should be created after successful Firebase fetch."""
        import config.remote_config as rc

        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(rc, "CACHE_FILE", cache_file)
        monkeypatch.setattr(rc, "VERSION_FILE", tmp_path / "VERSION")

        fake_config = {"version": "2.0.0"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_config
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            rc.fetch_remote_config("https://fake-firebase.json")

        assert cache_file.exists()
        cached = json.loads(cache_file.read_text())
        assert cached["version"] == "2.0.0"

    def test_get_config_value_dot_notation(self):
        """get_config_value should support dot-separated key paths."""
        import config.remote_config as rc

        config = {"platforms": {"github": {"button_labels": ["New Repo", "Create"]}}}
        result = rc.get_config_value("platforms.github.button_labels", config=config)
        assert result == ["New Repo", "Create"]

    def test_get_config_value_missing_key_returns_default(self):
        """Missing key should return default without raising."""
        import config.remote_config as rc

        config = {"platforms": {}}
        result = rc.get_config_value("platforms.github.missing_key", default="fallback", config=config)
        assert result == "fallback"


class TestVersionParsing:
    def test_local_version_file(self, tmp_path, monkeypatch):
        """Should read version from VERSION file."""
        import config.remote_config as rc

        version_file = tmp_path / "VERSION"
        version_file.write_text("1.3.7")
        monkeypatch.setattr(rc, "VERSION_FILE", version_file)

        assert rc._read_local_version() == "1.3.7"

    def test_missing_version_file_returns_zero(self, tmp_path, monkeypatch):
        """Missing VERSION file should return 0.0.0."""
        import config.remote_config as rc

        monkeypatch.setattr(rc, "VERSION_FILE", tmp_path / "no_version")
        assert rc._read_local_version() == "0.0.0"
