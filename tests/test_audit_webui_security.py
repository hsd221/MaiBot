import ipaddress
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Request, Response

from src.webui import anti_crawler, auth, chat_routes, rate_limiter, webui_server
import tests.test_webui_chat_routes as chat_tests


def request_with_chain(chain, peer="10.0.0.2"):
    return SimpleNamespace(headers={"X-Forwarded-For": chain}, client=SimpleNamespace(host=peer))


class ForwardedAddressAuditTest(unittest.TestCase):
    def test_trusted_proxy_discards_spoofed_leftmost_address(self):
        config = SimpleNamespace(webui=SimpleNamespace(trust_xff=True, trusted_proxies="10.0.0.0/8"))
        limiter = rate_limiter.RateLimiter()
        crawler = anti_crawler.AntiCrawlerMiddleware.__new__(anti_crawler.AntiCrawlerMiddleware)
        with (
            patch.object(rate_limiter, "global_config", config),
            patch.object(anti_crawler, "TRUST_XFF", True),
            patch.object(anti_crawler, "TRUSTED_PROXIES", [ipaddress.ip_network("10.0.0.0/8")]),
        ):
            for chain, expected in [
                ("127.0.0.1, 203.0.113.5", "203.0.113.5"),
                ("127.0.0.1, 203.0.113.5, 10.0.0.3", "203.0.113.5"),
                ("127.0.0.1, invalid, 10.0.0.3", "unknown"),
                ("127.0.0.1, , 10.0.0.3", "unknown"),
            ]:
                with self.subTest(chain=chain):
                    self.assertEqual(limiter._get_client_ip(request_with_chain(chain)), expected)
                    self.assertEqual(crawler._get_client_ip(request_with_chain(chain)), expected)

    def test_spoofed_addresses_share_one_failure_counter(self):
        config = SimpleNamespace(webui=SimpleNamespace(trust_xff=True, trusted_proxies="10.0.0.2"))
        limiter = rate_limiter.RateLimiter()
        with patch.object(rate_limiter, "global_config", config):
            for i in range(5):
                limiter.record_failed_attempt(request_with_chain(f"198.51.100.{i}, 203.0.113.5"))
            self.assertTrue(limiter.is_blocked(request_with_chain("198.51.100.99, 203.0.113.5"))[0])
            self.assertNotIn("198.51.100.0", limiter._blocked)

    def test_bearer_parsing_preserves_token_contents(self):
        with patch.object(auth, "get_token_manager") as manager:
            manager.return_value.verify_token.return_value = True
            token = "abcBearer xyz"
            self.assertEqual(auth.get_current_token(None, None, f"Bearer {token}"), token)
            auth.verify_auth_token_from_cookie_or_header(None, f"Bearer {token}")
            self.assertEqual(manager.return_value.verify_token.call_args.args, (token,))


class ChatDeletionAuditTest(chat_tests.WebUIChatRoutesTestCase):
    async def test_real_group_cannot_be_cleared(self):
        self.create_db_message(
            "protected", group_id="123456", user_id="user", user_nickname="User", content="keep", timestamp=1
        )
        with self.assertRaises(HTTPException) as error:
            await chat_routes.clear_chat_history("123456", True)
        self.assertEqual(error.exception.status_code, 400)
        self.assertTrue(chat_routes.Messages.select().where(chat_routes.Messages.message_id == "protected").exists())


class ContentSecurityPolicyAuditTest(unittest.TestCase):
    def test_websocket_policy_is_limited_to_current_origin(self):
        request = Request(
            {
                "type": "http",
                "scheme": "https",
                "server": ("example.com", 443),
                "path": "/",
                "headers": [(b"host", b"example.com")],
            }
        )
        response = Response()
        webui_server.apply_security_headers(response, is_https=True, request=request)
        directive = next(
            part.strip()
            for part in response.headers["content-security-policy"].split(";")
            if part.strip().startswith("connect-src")
        )
        self.assertIn("wss://example.com", directive)
        self.assertNotIn("ws:", directive.split())
        self.assertNotIn("wss:", directive.split())


class ForwardedAddressAllowlistAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_proxy_headers_do_not_inherit_loopback_allowlist(self):
        from unittest.mock import AsyncMock

        middleware = anti_crawler.AntiCrawlerMiddleware(lambda scope, receive, send: None, mode="strict")
        with (
            patch.object(anti_crawler, "TRUST_XFF", True),
            patch.object(anti_crawler, "TRUSTED_PROXIES", [ipaddress.ip_address("127.0.0.1")]),
            patch.object(anti_crawler, "ALLOWED_IPS", [ipaddress.ip_address("127.0.0.1")]),
        ):
            for forwarded in (
                {"X-Forwarded-For": "invalid"},
                {"X-Forwarded-For": ""},
                {"X-Real-IP": "invalid"},
                {"X-Real-IP": ""},
            ):
                with self.subTest(forwarded=forwarded):
                    request = SimpleNamespace(
                        headers={"User-Agent": "Googlebot", **forwarded},
                        client=SimpleNamespace(host="127.0.0.1"),
                        url=SimpleNamespace(path="/api/webui/person/list"),
                    )
                    call_next = AsyncMock(return_value=Response())
                    response = await middleware.dispatch(request, call_next)
                    self.assertEqual(response.status_code, 403)
                    call_next.assert_not_awaited()
