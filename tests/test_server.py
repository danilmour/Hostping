"""Live HTTP tests for hostping.server (SPEC §6, AC7/AC8).

Starts the real ThreadingHTTPServer on an ephemeral port in a background
thread and talks to it with urllib. The static files and hosts file live in a
temporary directory so the tests do not depend on the frontend build.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from hostping import server as server_module

INDEX_HTML = (
    "<!doctype html><html><head><title>hostping</title></head>"
    "<body><h1>hostping</h1></body></html>\n"
)
APP_JS = "/* hostping */\nconsole.log('hostping');\n"
STYLES_CSS = "body { color: #111; }\n"


def _up_checker(target, timeout):
    return {"status": "up", "method": "icmp", "latency_ms": 0.05, "error": None}


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = cls._tmp.name

        cls.web_dir = os.path.join(root, "web")
        os.makedirs(cls.web_dir)
        with open(os.path.join(cls.web_dir, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(INDEX_HTML)
        with open(os.path.join(cls.web_dir, "app.js"), "w", encoding="utf-8") as fh:
            fh.write(APP_JS)
        with open(os.path.join(cls.web_dir, "styles.css"), "w", encoding="utf-8") as fh:
            fh.write(STYLES_CSS)

        cls.hosts_file = os.path.join(root, "hosts")
        with open(cls.hosts_file, "w", encoding="utf-8") as fh:
            fh.write("# test hosts file\n127.0.0.1 localhost\n")

        cls.httpd = server_module.create_server(
            "127.0.0.1",
            0,
            hosts_file=cls.hosts_file,
            timeout=1.0,
            web_dir=cls.web_dir,
            checker=_up_checker,
        )
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(
            target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls._tmp.cleanup()

    def _request(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return (
                    response.status,
                    response.headers.get("Content-Type", ""),
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.headers.get("Content-Type", ""), exc.read()
            finally:
                exc.close()

    def test_root_serves_html(self):
        status, content_type, body = self._request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"hostping", body)

    def test_app_js_content_type(self):
        status, content_type, body = self._request("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", content_type)
        self.assertEqual(body.decode("utf-8"), APP_JS)

    def test_styles_css_content_type(self):
        status, content_type, body = self._request("/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("css", content_type)
        self.assertEqual(body.decode("utf-8"), STYLES_CSS)

    def test_api_hosts_returns_expected_payload(self):
        status, content_type, body = self._request("/api/hosts")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(
            set(payload),
            {
                "generated_at",
                "hosts_file",
                "duration_ms",
                "timeout_ms",
                "summary",
                "entries",
            },
        )
        self.assertEqual(payload["summary"]["total"], len(payload["entries"]))
        self.assertEqual(len(payload["entries"]), 1)
        entry = payload["entries"][0]
        self.assertEqual(entry["ip"], "127.0.0.1")
        self.assertEqual(entry["status"], "up")
        self.assertEqual(entry["ip_result"]["status"], "up")

    def test_api_health(self):
        status, content_type, body = self._request("/api/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertEqual(json.loads(body.decode("utf-8")), {"status": "ok"})

    def test_unknown_api_returns_404_json(self):
        status, content_type, body = self._request("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("application/json", content_type)
        self.assertEqual(json.loads(body.decode("utf-8")), {"error": "not found"})

    def test_unknown_non_api_path_returns_404_text(self):
        status, content_type, body = self._request("/nope")
        self.assertEqual(status, 404)
        self.assertIn("text/plain", content_type)
        self.assertIn(b"404", body)

    def test_unreadable_hosts_file_returns_500_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd = server_module.create_server(
                "127.0.0.1",
                0,
                hosts_file=os.path.join(tmp, "missing-hosts"),
                timeout=0.5,
                web_dir=self.web_dir,
                checker=_up_checker,
            )
            port = httpd.server_address[1]
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            thread.start()
            try:
                url = "http://127.0.0.1:%d/api/hosts" % port
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(url, timeout=10)
                self.assertEqual(ctx.exception.code, 500)
                self.assertIn("application/json", ctx.exception.headers.get("Content-Type", ""))
                body = json.loads(ctx.exception.read().decode("utf-8"))
                ctx.exception.close()
                self.assertIn("error", body)
                self.assertIn("hosts_file", body)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
