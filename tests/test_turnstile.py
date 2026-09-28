from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import httpx

from app import turnstile


class TurnstileTest(unittest.TestCase):
    settings = SimpleNamespace(
        turnstile_secret_key="test-secret",
        oidc_redirect_uri="https://codex.nethub.wiki/api/auth/callback",
    )

    def test_siteverify_checks_action_and_hostname(self):
        with patch.object(turnstile, "settings", self.settings), patch.object(
            turnstile.httpx, "post",
            return_value=httpx.Response(
                200,
                json={"success": True, "hostname": "codex.nethub.wiki", "action": "comment"},
                request=httpx.Request("POST", "https://challenges.cloudflare.com"),
            ),
        ):
            turnstile.verify_turnstile("token", "comment")
            with self.assertRaises(HTTPException) as mismatch:
                turnstile.verify_turnstile("token", "message")
        self.assertEqual(mismatch.exception.status_code, 400)

    def test_missing_token_and_provider_outage_fail_closed(self):
        with self.assertRaises(HTTPException) as missing:
            turnstile.verify_turnstile("", "comment")
        self.assertEqual(missing.exception.status_code, 400)

        with patch.object(turnstile, "settings", self.settings), patch.object(
            turnstile.httpx, "post", side_effect=httpx.ConnectError("offline")
        ):
            with self.assertRaises(HTTPException) as outage:
                turnstile.verify_turnstile("token", "comment")
        self.assertEqual(outage.exception.status_code, 503)
