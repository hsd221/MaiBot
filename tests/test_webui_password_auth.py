from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.webui import (
    auth,
    emoji_routes,
    logs_ws,
    plugin_progress_ws,
    rate_limiter,
    routes as webui_routes,
    token_manager,
    webui_server,
)
from src.webui.token_manager import TokenManager


class WebUIPasswordAuthTest(unittest.TestCase):
    def test_new_config_does_not_generate_password_and_initial_setup_is_one_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "webui.json"
            manager = TokenManager(path)

            self.assertFalse(manager.is_password_configured())
            self.assertTrue(manager.is_first_setup())
            self.assertEqual(manager.get_token(), "")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("access_token", saved)
            self.assertNotIn("password", saved)
            self.assertNotIn("password_hash", saved)
            self.assertNotIn("session_secret", saved)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            self.assertEqual(manager.set_initial_password("abc12345")[0], True)
            self.assertTrue(manager.is_password_configured())
            self.assertTrue(manager.verify_password("abc12345"))
            self.assertFalse(manager.set_initial_password("def45678")[0])

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("access_token", saved)
            self.assertNotIn("password", saved)
            self.assertNotEqual(saved["password_hash"], "abc12345")

    def test_existing_corrupt_config_fails_closed_without_becoming_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "webui.json"
            path.write_text("{not-valid-json", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                TokenManager(path)

            self.assertEqual(path.read_text(encoding="utf-8"), "{not-valid-json")

    def test_webui_config_rejects_oversized_files_and_removes_parent_write_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            os.chmod(root, 0o777)
            path = root / "webui.json"
            manager = TokenManager(path)

            self.assertEqual(root.stat().st_mode & 0o022, 0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            path.write_text(
                json.dumps({"padding": "x" * (manager.MAX_CONFIG_BYTES + 1)}),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                TokenManager(path)

    def test_webui_config_supports_platforms_without_fchmod(self) -> None:
        os_without_fchmod = SimpleNamespace(**{name: getattr(os, name) for name in dir(os) if name != "fchmod"})

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(token_manager, "os", os_without_fchmod):
            path = Path(tmp_dir) / "webui.json"
            manager = TokenManager(path)
            self.assertFalse(manager.is_password_configured())

    @unittest.skipUnless(hasattr(os, "symlink"), "platform does not support symlinks")
    def test_webui_config_and_lock_files_reject_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(tmp_dir)
            outside = Path(outside_dir)
            external_config = outside / "external.json"
            original_config = json.dumps({"access_token": "LegacyToken123", "first_setup_completed": True})
            external_config.write_text(original_config, encoding="utf-8")
            linked_config = root / "webui.json"
            linked_config.symlink_to(external_config)

            with self.assertRaises(RuntimeError):
                TokenManager(linked_config)

            self.assertTrue(linked_config.is_symlink())
            self.assertEqual(external_config.read_text(encoding="utf-8"), original_config)

            linked_config.unlink()
            external_lock = outside / "external.lock"
            external_lock.write_text("do-not-open", encoding="utf-8")
            (root / "webui.json.lock").symlink_to(external_lock)

            with self.assertRaises(RuntimeError):
                TokenManager(linked_config)

            self.assertEqual(external_lock.read_text(encoding="utf-8"), "do-not-open")

    def test_password_policy_allows_long_passphrases_symbols_and_unicode(self) -> None:
        valid = (
            "a1b2c3d4",
            "abcDEF1234567890",
            "correct horse battery staple 7!",
            "璃夜安全密码123!",
            "A1" + "x" * 126,
        )
        invalid = (
            "",
            "abc1234",
            "abcdefgh",
            "12345678",
            "abc1234\n",
            "A1" + "x" * 127,
        )

        for password in valid:
            with self.subTest(password=password):
                self.assertEqual(TokenManager.validate_password(password), (True, "密码格式正确"))

        for password in invalid:
            with self.subTest(password=password):
                self.assertFalse(TokenManager.validate_password(password)[0])

        longest = "A1" + "x" * 126
        self.assertEqual(webui_routes.PasswordSetupRequest(password=longest).password, longest)
        self.assertEqual(
            webui_routes.PasswordChangeRequest(current_password="old-password", new_password=longest).new_password,
            longest,
        )
        with self.assertRaises(ValidationError):
            webui_routes.PasswordSetupRequest(password="A1" + "x" * 127)

    def test_untrusted_scrypt_parameters_are_rejected_before_hashing(self) -> None:
        encoded = "scrypt$1048576$8$1$c2FsdHNhbHRzYWx0c2FsdA$ZGlnZXN0"

        with patch("src.webui.token_manager.hashlib.scrypt") as scrypt:
            self.assertFalse(TokenManager._verify_password_hash("abc12345", encoded))

        scrypt.assert_not_called()

    def test_session_is_independent_from_password_and_can_be_revoked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TokenManager(Path(tmp_dir) / "webui.json")
            self.assertTrue(manager.set_initial_password("abc12345")[0])

            session = manager.create_session()
            self.assertNotEqual(session, "abc12345")
            self.assertTrue(manager.verify_token(session))
            self.assertFalse(manager.verify_token("abc12345"))
            self.assertFalse(manager.verify_token(None))  # type: ignore[arg-type]
            old_secret = json.loads(manager.config_path.read_text(encoding="utf-8"))["session_secret"]

            self.assertEqual(manager.update_password("abc12345", "def45678")[0], True)
            self.assertFalse(manager.verify_token(session))
            self.assertFalse(manager.verify_password("abc12345"))
            self.assertTrue(manager.verify_password("def45678"))
            new_secret = json.loads(manager.config_path.read_text(encoding="utf-8"))["session_secret"]
            self.assertNotEqual(old_secret, new_secret)

    def test_malformed_session_values_fail_closed_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TokenManager(Path(tmp_dir) / "webui.json")
            self.assertTrue(manager.set_initial_password("abc12345")[0])

            malformed_values = (
                "1.9999999999.nonce.not-hex",
                "1.9999999999.nonce.é" + "0" * 63,
                "1.9999999999.含有中文." + "0" * 64,
                "1.9999999999.nonce." + "0" * 63,
                "1.9999999999.nonce." + "g" * 64,
                "1.9999999999.nonce.extra." + "0" * 64,
            )

            for value in malformed_values:
                with self.subTest(value=value):
                    self.assertFalse(manager.verify_session(value))
                    self.assertFalse(manager.verify_token(value))

    def test_legacy_update_api_cannot_bypass_current_password_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TokenManager(Path(tmp_dir) / "webui.json")
            self.assertTrue(manager.set_initial_password("abc12345")[0])

            success, message = manager.update_token("def45678")

            self.assertFalse(success)
            self.assertIn("已停用", message)
            self.assertTrue(manager.verify_password("abc12345"))
            self.assertFalse(manager.verify_password("def45678"))

    def test_legacy_access_token_is_migrated_after_successful_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "webui.json"
            path.write_text(
                json.dumps({"access_token": "LegacyToken123", "first_setup_completed": True}), encoding="utf-8"
            )
            manager = TokenManager(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            self.assertEqual(manager.get_token(), "")
            self.assertTrue(manager.authenticate("LegacyToken123"))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("access_token", saved)
            self.assertIn("password_hash", saved)
            self.assertTrue(manager.verify_password("LegacyToken123"))
            self.assertFalse(manager.verify_token("LegacyToken123"))

    def test_legacy_token_migrates_when_used_by_a_compatibility_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "webui.json"
            path.write_text(json.dumps({"access_token": "LegacyToken123"}), encoding="utf-8")
            manager = TokenManager(path)

            self.assertTrue(manager.verify_token("LegacyToken123"))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("access_token", saved)
            self.assertIn("password_hash", saved)


class WebUIPasswordRoutesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.manager = TokenManager(Path(self.tmpdir.name) / "webui.json")
        self.request = SimpleNamespace(
            headers={"sec-fetch-site": "same-origin"},
            client=SimpleNamespace(host="127.0.0.1"),
            url=SimpleNamespace(scheme="http"),
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_only_public_auth_entrypoints_have_ip_rate_limit_dependency(self) -> None:
        limited_paths = {
            route.path
            for route in webui_routes.router.routes
            if any(
                dependency.call is rate_limiter.check_auth_rate_limit
                for dependency in getattr(getattr(route, "dependant", None), "dependencies", ())
            )
        }

        self.assertEqual(limited_paths, set(rate_limiter.PUBLIC_AUTH_RATE_LIMIT_PATHS))

    async def test_auth_check_and_initial_password_setup_issue_a_session_once(self) -> None:
        with patch.object(webui_routes, "get_token_manager", return_value=self.manager):
            initial = await webui_routes.check_auth_status(self.request, maibot_session=None, authorization=None)
        self.assertEqual(initial, {"authenticated": False, "password_configured": False})

        response = Response()
        with (
            patch.object(webui_routes, "get_token_manager", return_value=self.manager),
            patch.object(webui_routes, "set_auth_cookie") as set_cookie,
        ):
            result = await webui_routes.setup_password(
                webui_routes.PasswordSetupRequest(password="abc12345"),
                self.request,
                response,
                _rate_limit=None,
            )

        self.assertTrue(result.success)
        session = set_cookie.call_args.args[1]
        self.assertNotEqual(session, "abc12345")
        self.assertTrue(self.manager.verify_token(session))

        with patch.object(webui_routes, "get_token_manager", return_value=self.manager):
            configured = await webui_routes.check_auth_status(self.request, maibot_session=session)
            with self.assertRaises(HTTPException) as repeated:
                await webui_routes.setup_password(
                    webui_routes.PasswordSetupRequest(password="def45678"),
                    self.request,
                    Response(),
                    _rate_limit=None,
                )
        self.assertEqual(configured, {"authenticated": True, "password_configured": True})
        self.assertEqual(repeated.exception.status_code, 409)

    async def test_login_uses_password_but_cookie_contains_only_a_session(self) -> None:
        self.assertTrue(self.manager.set_initial_password("abc12345")[0])
        response = Response()

        with (
            patch.object(webui_routes, "get_token_manager", return_value=self.manager),
            patch.object(
                webui_routes, "get_rate_limiter", return_value=SimpleNamespace(reset_failures=lambda request: None)
            ),
            patch.object(webui_routes, "set_auth_cookie") as set_cookie,
        ):
            result = await webui_routes.login(
                webui_routes.PasswordLoginRequest(password="abc12345"),
                self.request,
                response,
                _rate_limit=None,
            )

        self.assertTrue(result.valid)
        self.assertNotEqual(set_cookie.call_args.args[1], "abc12345")

    async def test_compatibility_login_rejects_oversized_input_before_hashing(self) -> None:
        manager = SimpleNamespace(
            is_password_configured=Mock(return_value=True),
            authenticate=Mock(return_value=False),
        )

        with patch.object(webui_routes, "get_token_manager", return_value=manager):
            result = webui_routes._complete_password_login("a" * 1025, self.request, Response())

        self.assertFalse(result.valid)
        manager.authenticate.assert_not_called()

    async def test_password_change_requires_current_password_and_revokes_old_session(self) -> None:
        self.assertTrue(self.manager.set_initial_password("abc12345")[0])
        session = self.manager.create_session()
        limiter = Mock()

        with (
            patch.object(webui_routes, "get_token_manager", return_value=self.manager),
            patch.object(webui_routes, "get_rate_limiter", return_value=limiter),
        ):
            with self.assertRaises(HTTPException) as wrong_password:
                await webui_routes.change_password(
                    webui_routes.PasswordChangeRequest(current_password="wrong123", new_password="def45678"),
                    Response(),
                    self.request,
                    maibot_session=session,
                )
        self.assertEqual(wrong_password.exception.status_code, 400)
        limiter.record_failed_attempt.assert_not_called()

        with (
            patch.object(webui_routes, "get_token_manager", return_value=self.manager),
            patch.object(webui_routes, "clear_auth_cookie") as clear_cookie,
        ):
            changed = await webui_routes.change_password(
                webui_routes.PasswordChangeRequest(current_password="abc12345", new_password="def45678"),
                Response(),
                self.request,
                maibot_session=session,
            )

        self.assertTrue(changed.success)
        self.assertFalse(self.manager.verify_token(session))
        clear_cookie.assert_called_once()

    def test_cross_site_auth_writes_are_rejected(self) -> None:
        request = SimpleNamespace(headers={"sec-fetch-site": "cross-site"})
        with self.assertRaises(HTTPException) as blocked:
            webui_routes.require_same_site_request(request)
        self.assertEqual(blocked.exception.status_code, 403)

        external_origin = SimpleNamespace(
            headers={"origin": "https://attacker.example"},
            url=SimpleNamespace(scheme="http", hostname="127.0.0.1", port=8001),
        )
        with self.assertRaises(HTTPException) as origin_blocked:
            webui_routes.require_same_site_request(external_origin)
        self.assertEqual(origin_blocked.exception.status_code, 403)

        local_dev_origin = SimpleNamespace(
            headers={"origin": "http://localhost:5173"},
            url=SimpleNamespace(scheme="http", hostname="127.0.0.1", port=8001),
        )
        webui_routes.require_same_site_request(local_dev_origin)


class WebUIPasswordHTTPIntegrationTest(unittest.TestCase):
    def test_initial_setup_issues_a_cookie_that_authenticates_follow_up_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TokenManager(Path(tmp_dir) / "webui.json")
            app = FastAPI()
            app.include_router(webui_routes.router)
            limiter = SimpleNamespace(reset_failures=lambda request: None)

            with (
                patch.object(webui_routes, "get_token_manager", return_value=manager),
                patch.object(webui_routes, "get_rate_limiter", return_value=limiter),
                TestClient(app, base_url="http://testserver") as client,
            ):
                initial = client.get("/api/webui/auth/check")
                self.assertEqual(initial.status_code, 200)
                self.assertEqual(initial.json(), {"authenticated": False, "password_configured": False})

                invalid = client.post(
                    "/api/webui/auth/setup",
                    json={"password": "onlyletters"},
                    headers={"Origin": "http://testserver"},
                )
                self.assertEqual(invalid.status_code, 422)

                configured = client.post(
                    "/api/webui/auth/setup",
                    json={"password": "abc12345"},
                    headers={"Origin": "http://testserver"},
                )
                self.assertEqual(configured.status_code, 200)
                cookie = configured.headers["set-cookie"]
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite=strict", cookie)
                self.assertNotIn("abc12345", cookie)

                authenticated = client.get("/api/webui/auth/check")
                self.assertEqual(
                    authenticated.json(),
                    {"authenticated": True, "password_configured": True},
                )

    def test_thumbnail_query_does_not_accept_a_full_session_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TokenManager(Path(tmp_dir) / "webui.json")
            self.assertTrue(manager.set_initial_password("abc12345")[0])
            session = manager.create_session()
            app = FastAPI()
            app.include_router(webui_routes.router)

            with (
                patch.object(emoji_routes, "get_token_manager", return_value=manager),
                TestClient(app, base_url="http://testserver") as client,
            ):
                response = client.get(f"/api/webui/emoji/1/thumbnail?token={session}")

            self.assertEqual(response.status_code, 401)


class WebUISessionTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_query_rejects_full_session_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TokenManager(Path(tmp_dir) / "webui.json")
            self.assertTrue(manager.set_initial_password("abc12345")[0])
            session = manager.create_session()

            handlers = (
                (logs_ws, logs_ws.websocket_logs),
                (plugin_progress_ws, plugin_progress_ws.websocket_plugin_progress),
            )
            for module, handler in handlers:
                with self.subTest(handler=handler.__name__):
                    websocket = SimpleNamespace(cookies={}, close=AsyncMock())
                    with (
                        patch.object(module, "verify_ws_token", return_value=False),
                        patch.object(module, "get_token_manager", return_value=manager),
                    ):
                        await handler(websocket, token=session)

                    websocket.close.assert_awaited_once()


class WebUISecurityHardeningTest(unittest.TestCase):
    def test_request_validation_errors_do_not_echo_secret_input(self) -> None:
        server = object.__new__(webui_server.WebUIServer)
        server.app = FastAPI()
        server._setup_exception_handlers()

        @server.app.post("/password")
        async def validate_password_request(payload: webui_routes.PasswordSetupRequest):
            return payload

        secret = "A1" + "secret-value-" * 20
        with TestClient(server.app, raise_server_exceptions=False) as client:
            response = client.post("/password", json={"password": secret})

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertNotIn('"input"', response.text)

    def test_auth_cookie_is_strict_and_automatically_secure_on_https(self) -> None:
        fake_config = SimpleNamespace(webui=SimpleNamespace(secure_cookie=False, mode="development"))
        request = SimpleNamespace(headers={}, url=SimpleNamespace(scheme="https"))
        response = Response()

        with patch.object(auth, "global_config", fake_config):
            auth.set_auth_cookie(response, "signed-session", request)

        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Secure", cookie)

    def test_forwarded_https_is_only_trusted_from_configured_proxy(self) -> None:
        fake_config = SimpleNamespace(
            webui=SimpleNamespace(
                secure_cookie=False,
                mode="development",
                trust_xff=False,
                trusted_proxies="10.0.0.2",
            )
        )
        request = SimpleNamespace(
            headers={"x-forwarded-proto": "https"},
            client=SimpleNamespace(host="10.0.0.2"),
            url=SimpleNamespace(scheme="http"),
        )

        with patch.object(auth, "global_config", fake_config):
            insecure_response = Response()
            auth.set_auth_cookie(insecure_response, "signed-session", request)
            self.assertNotIn("Secure", insecure_response.headers["set-cookie"])

            fake_config.webui.trust_xff = True
            secure_response = Response()
            auth.set_auth_cookie(secure_response, "signed-session", request)
            self.assertIn("Secure", secure_response.headers["set-cookie"])

    def test_rate_limiter_ignores_forwarded_ip_unless_direct_peer_is_trusted(self) -> None:
        limiter = rate_limiter.RateLimiter()
        request = SimpleNamespace(
            headers={"X-Forwarded-For": "203.0.113.5"},
            client=SimpleNamespace(host="10.0.0.2"),
        )
        fake_webui_config = SimpleNamespace(trust_xff=False, trusted_proxies="10.0.0.2")

        with patch.object(rate_limiter, "global_config", SimpleNamespace(webui=fake_webui_config)):
            self.assertEqual(limiter._get_client_ip(request), "10.0.0.2")
            fake_webui_config.trust_xff = True
            self.assertEqual(limiter._get_client_ip(request), "203.0.113.5")

        fake_webui_config.trusted_proxies = "10.0.0.3"
        with patch.object(rate_limiter, "global_config", SimpleNamespace(webui=fake_webui_config)):
            self.assertEqual(limiter._get_client_ip(request), "10.0.0.2")

    def test_security_headers_include_csp_and_hsts_only_for_https(self) -> None:
        response = Response()
        webui_server.apply_security_headers(response, is_https=False)

        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        content_security_policy = response.headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", content_security_policy)
        self.assertIn(
            "connect-src 'self' https://maibot-plugin-stats.maibot-webui.workers.dev",
            content_security_policy,
        )
        self.assertNotIn("http://localhost:8001", content_security_policy)
        self.assertNotIn("http://127.0.0.1:8001", content_security_policy)
        self.assertNotIn("Strict-Transport-Security", response.headers)

        secure_response = Response()
        webui_server.apply_security_headers(secure_response, is_https=True)
        self.assertIn("max-age=31536000", secure_response.headers["Strict-Transport-Security"])


if __name__ == "__main__":
    unittest.main()
