"""
Unit tests — Security Layer (Credentials)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCredentialStorage:

    def test_store_and_retrieve_credential(self, monkeypatch):
        """store_credential + get_credential round-trip using mocked keyring."""
        import security.credentials as creds

        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "sk-test-12345"
        monkeypatch.setitem(sys.modules, "keyring", mock_keyring)

        import importlib
        importlib.reload(creds)

        creds.store_credential("openai_api_key", "sk-test-12345")
        mock_keyring.set_password.assert_called_with(
            creds.KEYCHAIN_SERVICE, "openai_api_key", "sk-test-12345"
        )

    def test_get_credential_returns_none_when_missing(self, monkeypatch):
        """get_credential should return None (not raise) when key missing."""
        import security.credentials as creds

        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = None
        monkeypatch.setitem(sys.modules, "keyring", mock_keyring)

        import importlib
        importlib.reload(creds)

        result = creds.get_credential("missing_key")
        assert result is None

    def test_credential_not_logged(self, caplog, monkeypatch):
        """Credential values must never appear in log output."""
        import logging
        import security.credentials as creds

        SECRET = "super-secret-token-xyz"
        creds.CredentialRedactFilter.register_secret(SECRET)

        filter_ = creds.CredentialRedactFilter()

        class FakeRecord:
            msg = f"Stored token: {SECRET}"
            args = ()
            def getMessage(self): return self.msg

        record = FakeRecord()
        filter_.filter(record)

        assert SECRET not in record.msg
        assert "[REDACTED]" in record.msg


class TestAESEncryption:

    def test_encrypt_decrypt_roundtrip(self, tmp_path, monkeypatch):
        """AES encrypt then decrypt should return original plaintext."""
        import security.credentials as creds

        key_file = tmp_path / ".master_key"
        cache_file = tmp_path / ".cred_cache.enc"
        monkeypatch.setattr(creds, "KEY_FILE_PATH", key_file)
        monkeypatch.setattr(creds, "CRED_CACHE_PATH", cache_file)

        test_creds = {"github_pat": "ghp_testtoken123", "gmail": "test@gmail.com"}
        creds.save_credential_cache(test_creds)

        assert cache_file.exists()
        loaded = creds.load_credential_cache()
        assert loaded["github_pat"] == "ghp_testtoken123"
        assert loaded["gmail"] == "test@gmail.com"

    def test_corrupted_cache_returns_empty(self, tmp_path, monkeypatch):
        """Corrupted cache file should return empty dict, not raise."""
        import security.credentials as creds

        key_file = tmp_path / ".master_key"
        cache_file = tmp_path / ".cred_cache.enc"
        cache_file.write_bytes(b"this is not valid encrypted data")

        monkeypatch.setattr(creds, "KEY_FILE_PATH", key_file)
        monkeypatch.setattr(creds, "CRED_CACHE_PATH", cache_file)

        result = creds.load_credential_cache()
        assert result == {}


class TestCheckRequiredCredentials:

    def test_returns_missing_list(self, monkeypatch):
        """Should return names of credentials not in keychain."""
        import security.credentials as creds

        mock_keyring = MagicMock()
        # Only "gmail" is set
        mock_keyring.get_password.side_effect = lambda svc, key: "test@test.com" if key == "gmail" else None
        monkeypatch.setitem(sys.modules, "keyring", mock_keyring)

        import importlib
        importlib.reload(creds)

        missing = creds.check_required_credentials()
        assert "gmail" not in missing
        assert "github_pat" in missing
