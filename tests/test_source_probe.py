import socket
import subprocess
import io
import json
import unittest
from unittest.mock import patch

from jarvis_mark2 import Mark2Runtime
from source_probe import (
    MAX_PAGE_BYTES, UnsafeSource, _exchange, _public_addresses, _target,
    _worker, inspect_public_page, probe_public_url,
)


class PublicSourceProbeTests(unittest.TestCase):
    def test_rejects_private_and_non_http_targets_without_network(self):
        for url in (
            "file:///etc/passwd", "http://user:pass@example.org/secret",
            "http://example.org:8080/", "http://example.org/\r\nInjected: yes",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeSource):
                _target(url)

    def test_private_dns_answer_is_rejected_before_connection(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]
        with patch("source_probe.socket.getaddrinfo", return_value=private):
            with self.assertRaisesRegex(UnsafeSource, "private"):
                _public_addresses("example.org", 443)

    def test_mixed_dns_answer_is_rejected(self):
        mixed = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443)),
        ]
        with patch("source_probe.socket.getaddrinfo", return_value=mixed):
            with self.assertRaises(UnsafeSource):
                _public_addresses("example.org", 443)

    def test_redirect_target_is_revalidated(self):
        def fake_head(url):
            if url == "https://public.example/a":
                return 302, "http://127.0.0.1/private", "", ""
            _target(url)
            raise UnsafeSource("private redirect")

        with patch("source_probe._head", side_effect=fake_head):
            result = probe_public_url("https://public.example/a")
        self.assertEqual(result["status"], "unsafe")

    def test_parent_process_timeout_is_bounded(self):
        with patch("jarvis_mark2.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 5)):
            self.assertEqual(Mark2Runtime._probe_research_source("https://example.org"), {"status": "timeout"})

    def test_page_inspection_returns_bounded_untrusted_match(self):
        html = b"<title>Sample report</title><script>ignore secret</script><main>The result was 42 units.</main>"
        with patch("source_probe._request", return_value=(200, "", "", "text/html", html)):
            result = inspect_public_page("https://example.org/report", "42 units")
        self.assertEqual(result["status"], "page_read")
        self.assertEqual(result["quote_match"], "exact_text_found")
        self.assertEqual(result["page_title"], "Sample report")
        self.assertNotIn("ignore secret", result["excerpt"])
        self.assertIn("does not verify", result["caveat"])

    def test_digest_prefers_article_over_long_navigation_and_footer(self):
        html = (
            "<title>Research page</title><nav>" + "Menu link " * 500 + "</nav>"
            + "<main><header>" + "Cookie settings " * 30 + "</header>"
            + "<article>" + "The core finding is a documented result with detailed context. " * 8
            + "</article><aside>" + "Related links " * 100 + "</aside></main>"
            + "<footer>" + "Legal links " * 100 + "</footer>"
        ).encode()
        with patch("source_probe._request", return_value=(200, "", "", "text/html", html)):
            result = inspect_public_page("https://example.org/report", excerpt_limit=3_000)
        self.assertEqual(result["status"], "page_read")
        self.assertTrue(result["excerpt"].startswith("The core finding"))
        self.assertNotIn("Menu link", result["excerpt"])
        self.assertNotIn("Cookie settings", result["excerpt"])
        self.assertNotIn("Related links", result["excerpt"])

    def test_declared_charset_preserves_source_words(self):
        body = ("<title>Résumé</title><main>"
                + "Café results were independently documented. " * 5 + "</main>")
        with patch("source_probe._request", return_value=(
            200, "", "", "text/html; charset=iso-8859-1", body.encode("iso-8859-1"),
        )):
            result = inspect_public_page("https://example.org/report", excerpt_limit=3_000)
        self.assertEqual(result["page_title"], "Résumé")
        self.assertIn("Café results", result["excerpt"])
        self.assertNotIn("\ufffd", result["excerpt"])

    def test_missing_quote_is_limited_to_inspected_sample(self):
        with patch("source_probe._request", return_value=(200, "", "", "text/plain", b"A short page.")):
            result = inspect_public_page("https://example.org/report", "different claim")
        self.assertEqual(result["quote_match"], "not_found_in_sample")
        self.assertEqual(result["bytes_read"], 13)

    def test_digest_mode_expands_only_bounded_extract(self):
        page = ("This is public source text. " * 160).encode()
        with patch("source_probe._request", return_value=(200, "", "", "text/plain", page)):
            short = inspect_public_page("https://example.org/report")
            digest = inspect_public_page("https://example.org/report", excerpt_limit=3_000)
        self.assertLessEqual(len(short["excerpt"]), 700)
        self.assertEqual(len(digest["excerpt"]), 3_000)
        self.assertEqual(digest["status"], "page_read")

    def test_worker_emits_ascii_json_for_non_latin_page_text(self):
        payload = io.StringIO(json.dumps({"mode": "digest", "url": "https://example.org/"}))
        output = io.StringIO()
        with patch("source_probe.sys.stdin", payload), patch("source_probe.sys.stdout", output), \
                patch("source_probe._request", return_value=(200, "", "", "text/plain",
                                                       ("اردو 中文 emoji 😀 " * 100).encode())):
            self.assertEqual(_worker(), 0)
        self.assertTrue(output.getvalue().isascii())
        self.assertIn("اردو", json.loads(output.getvalue())["excerpt"])

    def test_page_inspection_rechecks_private_redirect(self):
        def fake_request(url, method):
            if url == "https://example.org/report":
                return 302, "http://127.0.0.1/private", "", "", b""
            raise UnsafeSource("source resolves to a private address")

        with patch("source_probe._request", side_effect=fake_request):
            result = inspect_public_page("https://example.org/report")
        self.assertEqual(result["status"], "unsafe")

    def test_page_read_rejects_oversized_content_before_body(self):
        class Response:
            status = 200

            def __init__(self, *_args, **_kwargs):
                self.read_called = False

            def begin(self):
                pass

            def getheader(self, name):
                return {"Content-Type": "text/html", "Content-Length": str(MAX_PAGE_BYTES + 1)}.get(name)

            def read(self, _limit):
                self.read_called = True
                return b""

            def close(self):
                pass

        connection = unittest.mock.Mock()
        with patch("source_probe.http.client.HTTPResponse", Response):
            with self.assertRaisesRegex(UnsafeSource, "bounded read size"):
                _exchange(connection, "example.org", "/report", "GET")


if __name__ == "__main__":
    unittest.main()
