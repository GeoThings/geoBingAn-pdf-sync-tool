"""Tests for jwt_auth module.

Tests decode_jwt_payload, is_token_expired, refresh_access_token,
and get_valid_token without requiring external services.
"""
import sys
import os
import json
import base64
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jwt_auth import decode_jwt_payload, is_token_expired, refresh_access_token, get_valid_token


def _make_jwt(payload: dict) -> str:
    """Helper: create a fake JWT token with the given payload."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b'=').decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b'=').decode()
    signature = "fake_signature"
    return f"{header}.{body}.{signature}"


class TestDecodeJwtPayload:
    def test_valid_token(self):
        payload = {"sub": "user@example.com", "exp": 9999999999}
        token = _make_jwt(payload)
        result = decode_jwt_payload(token)
        assert result["sub"] == "user@example.com"
        assert result["exp"] == 9999999999

    def test_invalid_token_no_dots(self):
        result = decode_jwt_payload("not-a-jwt-token")
        assert result == {}

    def test_invalid_token_bad_base64(self):
        result = decode_jwt_payload("a.!!!invalid!!!.c")
        assert result == {}

    def test_empty_string(self):
        result = decode_jwt_payload("")
        assert result == {}

    def test_two_parts(self):
        result = decode_jwt_payload("header.payload")
        assert result == {}


class TestIsTokenExpired:
    def test_not_expired(self):
        future_exp = int(time.time()) + 3600  # 1 hour from now
        token = _make_jwt({"exp": future_exp})
        assert is_token_expired(token) is False

    def test_expired(self):
        past_exp = int(time.time()) - 100
        token = _make_jwt({"exp": past_exp})
        assert is_token_expired(token) is True

    def test_within_buffer(self):
        # Token expires in 200 seconds, but buffer is 300 -> should be "expired"
        near_exp = int(time.time()) + 200
        token = _make_jwt({"exp": near_exp})
        assert is_token_expired(token, buffer_seconds=300) is True

    def test_outside_buffer(self):
        near_exp = int(time.time()) + 600
        token = _make_jwt({"exp": near_exp})
        assert is_token_expired(token, buffer_seconds=300) is False

    def test_no_exp_field(self):
        token = _make_jwt({"sub": "test"})
        assert is_token_expired(token) is True

    def test_invalid_token(self):
        assert is_token_expired("bad.token.here") is True


class TestRefreshAccessToken:
    @patch('jwt_auth.requests.post')
    def test_success_with_access_key(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access": "new_token_123"}
        mock_post.return_value = mock_response

        access, refresh = refresh_access_token("refresh_tok", "https://example.com/refresh")
        assert access == "new_token_123"
        assert refresh is None
        mock_post.assert_called_once()

    @patch('jwt_auth.requests.post')
    def test_success_with_access_token_key(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "new_token_456"}
        mock_post.return_value = mock_response

        access, refresh = refresh_access_token("refresh_tok", "https://example.com/refresh")
        assert access == "new_token_456"

    @patch('jwt_auth.requests.post')
    def test_success_with_both_tokens(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access": "new_access", "refresh": "new_refresh"}
        mock_post.return_value = mock_response

        access, refresh = refresh_access_token("refresh_tok", "https://example.com/refresh")
        assert access == "new_access"
        assert refresh == "new_refresh"

    @patch('jwt_auth.requests.post')
    def test_failure_status_code(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_post.return_value = mock_response

        access, refresh = refresh_access_token("refresh_tok", "https://example.com/refresh")
        assert access is None
        assert refresh is None

    @patch('jwt_auth.requests.post')
    def test_network_error(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")

        access, refresh = refresh_access_token("refresh_tok", "https://example.com/refresh")
        assert access is None
        assert refresh is None

    @patch('jwt_auth.requests.post')
    def test_missing_token_in_response(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message": "ok"}
        mock_post.return_value = mock_response

        access, refresh = refresh_access_token("refresh_tok", "https://example.com/refresh")
        assert access is None


class TestGetValidToken:
    def test_valid_token_not_refreshed(self):
        future_exp = int(time.time()) + 3600
        token = _make_jwt({"exp": future_exp})

        result_token, was_refreshed, new_refresh = get_valid_token(token, "refresh", "https://example.com/refresh")
        assert result_token == token
        assert was_refreshed is False
        assert new_refresh is None

    @patch('jwt_auth.refresh_access_token')
    def test_expired_token_refreshed(self, mock_refresh):
        past_exp = int(time.time()) - 100
        old_token = _make_jwt({"exp": past_exp})
        mock_refresh.return_value = ("fresh_token", "fresh_refresh")

        result_token, was_refreshed, new_refresh = get_valid_token(old_token, "refresh", "https://example.com/refresh")
        assert result_token == "fresh_token"
        assert was_refreshed is True
        assert new_refresh == "fresh_refresh"
        mock_refresh.assert_called_once_with("refresh", "https://example.com/refresh")

    @patch('jwt_auth.refresh_access_token')
    def test_expired_token_refresh_fails(self, mock_refresh):
        past_exp = int(time.time()) - 100
        old_token = _make_jwt({"exp": past_exp})
        mock_refresh.return_value = (None, None)

        result_token, was_refreshed, new_refresh = get_valid_token(old_token, "refresh", "https://example.com/refresh")
        assert result_token == old_token
        assert was_refreshed is False

    def test_thread_safety(self):
        """Verify get_valid_token can be called from multiple threads."""
        import threading

        future_exp = int(time.time()) + 3600
        token = _make_jwt({"exp": future_exp})
        results = []

        def call_get_valid():
            t, refreshed, _ = get_valid_token(token, "refresh", "https://example.com/refresh")
            results.append((t, refreshed))

        threads = [threading.Thread(target=call_get_valid) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        for t, refreshed in results:
            assert t == token
            assert refreshed is False
