from __future__ import annotations

import importlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from authlib.jose import JsonWebKey, jwt
from cryptography.hazmat.primitives.asymmetric import rsa


def response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://auth.test/"))


class OidcAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        root = Path(cls.temp_dir.name)
        os.environ["DATABASE_PATH"] = str(root / "cas.db")
        os.environ["OIDC_ISSUER"] = "https://auth.test"
        os.environ["OIDC_CLIENT_ID"] = "cas"
        os.environ["OIDC_CLIENT_SECRET"] = "client-secret-long-enough-for-tests"
        os.environ["OIDC_REDIRECT_URI"] = "https://cas.test/api/auth/callback"
        os.environ["OIDC_COOKIE_SECURE"] = "false"
        os.environ["CAS_ADMIN_SUBS"] = "admin-sub"
        import app.config
        import app.database
        import app.auth
        import app.main
        importlib.reload(app.config)
        importlib.reload(app.database)
        importlib.reload(app.auth)
        importlib.reload(app.main)
        cls.auth, cls.database, cls.main = app.auth, app.database, app.main
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwks = JsonWebKey.import_key(cls.key, {"kty": "RSA", "kid": "test-key"}).as_dict(is_private=False)
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        cls.temp_dir.cleanup()

    def token(self, sub: str, nonce: str, sid: str = "central-sid") -> str:
        now = int(time.time())
        claims = {"iss": "https://auth.test", "aud": ["cas"], "sub": sub, "nonce": nonce,
                  "sid": sid, "iat": now, "exp": now + 300, "auth_time": now,
                  "preferred_username": "reader", "name": "读者"}
        key = JsonWebKey.import_key(self.key, {"kty": "RSA", "kid": "test-key"})
        return jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key).decode()

    def mock_oidc(self, sub: str = "reader-sub", sid: str = "central-sid"):
        connection = self.database.connect()
        try:
            state = connection.execute(
                """SELECT nonce, code_verifier FROM oidc_login_states
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        finally:
            connection.close()

        def get(url, **kwargs):
            if url.endswith("openid-configuration"):
                return response(200, {"issuer": "https://auth.test", "token_endpoint": "https://auth.test/oauth/token",
                    "userinfo_endpoint": "https://auth.test/oauth/userinfo", "jwks_uri": "https://auth.test/jwks"})
            if url.endswith("/jwks"):
                return response(200, {"keys": [self.jwks]})
            if url.endswith("userinfo"):
                return response(200, {"sub": sub, "preferred_username": "reader", "name": "读者"})
            raise AssertionError(url)

        def post(url, **kwargs):
            self.assertEqual(url, "https://auth.test/oauth/token")
            self.assertEqual(
                kwargs["auth"], ("cas", "client-secret-long-enough-for-tests")
            )
            self.assertEqual(kwargs["data"]["code_verifier"], state["code_verifier"])
            return response(
                200,
                {
                    "access_token": "access-token",
                    "id_token": self.token(sub, state["nonce"], sid),
                },
            )
        return patch("app.auth.httpx.get", side_effect=get), patch("app.auth.httpx.post", side_effect=post)

    def begin_login(self) -> dict:
        result = self.client.get("/login?next=/admin", follow_redirects=False)
        self.assertEqual(result.status_code, 302)
        query = parse_qs(urlsplit(result.headers["location"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["state"] and query["nonce"])
        return {key: value[0] for key, value in query.items()}

    def test_oidc_callback_creates_member_and_cookie(self) -> None:
        request = self.begin_login()
        get_patch, post_patch = self.mock_oidc()
        with get_patch, post_patch:
            result = self.client.get(f"/api/auth/callback?code=code&state={request['state']}", follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["location"], "/admin")
        self.assertIn("cas_session=", result.headers["set-cookie"])
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["data"]["authSub"], "reader-sub")
        self.assertEqual(me.json()["data"]["role"], "user")
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)

    def test_id_token_with_non_rs256_algorithm_is_rejected(self) -> None:
        request = self.begin_login()
        connection = self.database.connect()
        try:
            nonce = connection.execute(
                "SELECT nonce FROM oidc_login_states ORDER BY id DESC LIMIT 1"
            ).fetchone()["nonce"]
        finally:
            connection.close()
        now = int(time.time())
        token = jwt.encode(
            {"alg": "HS256"},
            {
                "iss": "https://auth.test",
                "aud": ["cas"],
                "sub": "wrong-alg-sub",
                "nonce": nonce,
                "sid": "wrong-alg-sid",
                "iat": now,
                "exp": now + 300,
                "auth_time": now,
            },
            b"not-an-rsa-key-but-long-enough",
        ).decode()

        def get(url, **kwargs):
            if url.endswith("openid-configuration"):
                return response(200, {
                    "issuer": "https://auth.test",
                    "token_endpoint": "https://auth.test/oauth/token",
                    "userinfo_endpoint": "https://auth.test/oauth/userinfo",
                    "jwks_uri": "https://auth.test/jwks",
                })
            return response(200, {"keys": [self.jwks]})

        with (
            patch("app.auth.httpx.get", side_effect=get),
            patch(
                "app.auth.httpx.post",
                return_value=response(
                    200, {"access_token": "access-token", "id_token": token}
                ),
            ),
        ):
            result = self.client.get(
                f"/api/auth/callback?code=code&state={request['state']}"
            )
        self.assertEqual(result.status_code, 401)

    def test_existing_subject_is_not_created_twice_and_admin_is_explicit(self) -> None:
        for _ in range(2):
            request = self.begin_login()
            get_patch, post_patch = self.mock_oidc("admin-sub", "admin-sid")
            with get_patch, post_patch:
                self.client.get(f"/api/auth/callback?code=code&state={request['state']}", follow_redirects=False)
        connection = self.database.connect()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users WHERE auth_sub = 'admin-sub'").fetchone()[0], 1)
        finally:
            connection.close()
        self.assertEqual(self.client.get("/api/auth/me").json()["data"]["role"], "admin")

    def test_callback_state_is_one_time_and_legacy_endpoints_are_closed(self) -> None:
        request = self.begin_login()
        get_patch, post_patch = self.mock_oidc()
        with get_patch, post_patch:
            self.client.get(f"/api/auth/callback?code=code&state={request['state']}")
        self.assertEqual(self.client.get(f"/api/auth/callback?code=code&state={request['state']}").status_code, 400)
        self.assertEqual(self.client.post("/api/auth/login", json={}).status_code, 410)
        self.assertEqual(self.client.post("/api/auth/register", json={}).status_code, 410)

    def test_callback_state_is_bound_to_the_browser_that_started_login(self) -> None:
        request = self.begin_login()
        from fastapi.testclient import TestClient

        other_browser = TestClient(self.main.app)
        rejected = other_browser.get(
            f"/api/auth/callback?code=code&state={request['state']}",
            follow_redirects=False,
        )
        self.assertEqual(rejected.status_code, 400)

        get_patch, post_patch = self.mock_oidc("bound-sub", "bound-sid")
        with get_patch, post_patch:
            accepted = self.client.get(
                f"/api/auth/callback?code=code&state={request['state']}",
                follow_redirects=False,
            )
        self.assertEqual(accepted.status_code, 303)
        self.assertIn("cas_oidc_flow=", accepted.headers["set-cookie"])
        self.assertIn("Max-Age=0", accepted.headers["set-cookie"])

    def test_error_callback_does_not_require_code_and_consumes_state(self) -> None:
        request = self.begin_login()
        result = self.client.get(
            f"/api/auth/callback?error=access_denied&state={request['state']}"
        )
        self.assertEqual(result.status_code, 401)
        replay = self.client.get(
            f"/api/auth/callback?error=access_denied&state={request['state']}"
        )
        self.assertEqual(replay.status_code, 400)

    def test_cross_origin_mutation_is_rejected(self) -> None:
        cross_site = self.client.post(
            "/api/auth/logout",
            headers={"Origin": "https://evil.test", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(cross_site.status_code, 403)
        same_origin = self.client.post(
            "/api/auth/logout",
            headers={"Origin": "https://cas.test", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(same_origin.status_code, 204)

    def test_existing_local_session_survives_accounts_outage(self) -> None:
        request = self.begin_login()
        get_patch, post_patch = self.mock_oidc("offline-sub", "offline-sid")
        with get_patch, post_patch:
            self.client.get(f"/api/auth/callback?code=code&state={request['state']}")
        with patch("app.auth.httpx.get", side_effect=httpx.ConnectError("offline")):
            result = self.client.get("/api/auth/me")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["data"]["authSub"], "offline-sub")

    def test_local_logout_and_backchannel_logout_revoke_sessions(self) -> None:
        request = self.begin_login()
        get_patch, post_patch = self.mock_oidc("logout-sub", "logout-sid")
        with get_patch, post_patch:
            self.client.get(f"/api/auth/callback?code=code&state={request['state']}")
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

        request = self.begin_login()
        get_patch, post_patch = self.mock_oidc("logout-sub", "logout-sid")
        with get_patch, post_patch:
            self.client.get(f"/api/auth/callback?code=code&state={request['state']}")
        now = int(time.time())
        token = jwt.encode({"alg": "RS256", "kid": "test-key"}, {
            "iss": "https://auth.test", "aud": ["cas"], "sub": "logout-sub", "sid": "logout-sid",
            "iat": now, "exp": now + 300, "jti": "logout-event",
            "events": {self.auth.LOGOUT_EVENT: {}},
        }, JsonWebKey.import_key(self.key, {"kty": "RSA", "kid": "test-key"})).decode()
        def backchannel_get(url, **kwargs):
            if url.endswith("openid-configuration"):
                return response(200, {
                    "issuer": "https://auth.test",
                    "token_endpoint": "https://auth.test/oauth/token",
                    "userinfo_endpoint": "https://auth.test/oauth/userinfo",
                    "jwks_uri": "https://auth.test/jwks",
                })
            return response(200, {"keys": [self.jwks]})
        with patch("app.auth.httpx.get", side_effect=backchannel_get):
            self.assertEqual(self.client.post("/api/auth/backchannel-logout", data={"logout_token": token}).status_code, 204)
            self.assertEqual(self.client.post("/api/auth/backchannel-logout", data={"logout_token": token}).status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        connection = self.database.connect()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM oidc_logout_events").fetchone()[0], 1)
        finally:
            connection.close()

    def test_stale_backchannel_logout_token_is_rejected(self) -> None:
        issued_at = int(time.time()) - 301
        token = jwt.encode({"alg": "RS256", "kid": "test-key"}, {
            "iss": "https://auth.test", "aud": ["cas"], "sub": "logout-sub",
            "iat": issued_at, "jti": "stale-event", "events": {self.auth.LOGOUT_EVENT: {}},
        }, JsonWebKey.import_key(self.key, {"kty": "RSA", "kid": "test-key"})).decode()

        def get(url, **kwargs):
            if url.endswith("openid-configuration"):
                return response(200, {
                    "issuer": "https://auth.test",
                    "token_endpoint": "https://auth.test/oauth/token",
                    "userinfo_endpoint": "https://auth.test/oauth/userinfo",
                    "jwks_uri": "https://auth.test/jwks",
                })
            return response(200, {"keys": [self.jwks]})

        with patch("app.auth.httpx.get", side_effect=get):
            result = self.client.post(
                "/api/auth/backchannel-logout", data={"logout_token": token}
            )
        self.assertEqual(result.status_code, 400)


if __name__ == "__main__":
    unittest.main()
