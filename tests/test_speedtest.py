import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from netview import speedtest
from netview.speedtest import SpeedTester

DOWN_BPS = 25_000_000   # ~200 Mbps
UP_BPS = 5_000_000      # ~40 Mbps


class FakeCloudflare(BaseHTTPRequestHandler):
    """Mimics speed.cloudflare.com's /__down and /__up, throttled."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _throttle(self, nbytes, rate, started):
        ahead = started + nbytes / rate - time.perf_counter()
        if ahead > 0:
            time.sleep(ahead)

    def do_GET(self):
        size = int(parse_qs(urlparse(self.path).query).get("bytes", ["0"])[0])
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("cf-ray", "8c5f0e1a2b3c4d5e-SJC")
        self.send_header("Server-Timing", "cfRequestDuration;dur=0.5")
        self.end_headers()
        # Spread the rate across the streams the client opens.
        rate, sent, block, start = DOWN_BPS / speedtest.STREAMS, 0, bytes(65536), time.perf_counter()
        try:
            while sent < size:
                n = min(len(block), size - sent)
                self.wfile.write(block[:n])
                sent += n
                self._throttle(sent, rate, start)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_POST(self):
        size = int(self.headers["Content-Length"])
        rate, got, start = UP_BPS / speedtest.STREAMS, 0, time.perf_counter()
        try:
            while got < size:
                chunk = self.rfile.read(min(65536, size - got))
                if not chunk:
                    return
                got += len(chunk)
                self._throttle(got, rate, start)
        except ConnectionResetError:
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class SpeedTestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloudflare)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        # Shorter phases so the test runs in a few seconds.
        cls.saved = (speedtest.MAX_PHASE_S, speedtest.MIN_PHASE_S, speedtest.WARMUP_S)
        speedtest.MAX_PHASE_S, speedtest.MIN_PHASE_S, speedtest.WARMUP_S = 3.0, 2.0, 1.0

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        speedtest.MAX_PHASE_S, speedtest.MIN_PHASE_S, speedtest.WARMUP_S = cls.saved

    def test_internet_test_measures_the_link(self):
        data_dir = Path(tempfile.mkdtemp())
        st = SpeedTester(data_dir, self.url)
        self.assertTrue(st.start())
        self.assertFalse(st.start())  # only one at a time
        deadline = time.time() + 30
        while st.status()["state"] == "running" and time.time() < deadline:
            time.sleep(0.2)
        status = st.status()
        self.assertEqual(status["state"], "idle", status.get("error"))
        r = status["history"][-1]
        self.assertEqual(r["kind"], "internet")
        self.assertEqual(r["server"], "SJC")
        self.assertAlmostEqual(r["download_mbps"], DOWN_BPS * 8 / 1e6, delta=DOWN_BPS * 8 / 1e6 * 0.25)
        self.assertAlmostEqual(r["upload_mbps"], UP_BPS * 8 / 1e6, delta=UP_BPS * 8 / 1e6 * 0.35)
        self.assertLess(r["latency_ms"], 50)
        # Saved to disk
        saved = json.loads((data_dir / "speedtests.json").read_text())
        self.assertEqual(saved[-1]["download_mbps"], r["download_mbps"])

    def test_unreachable_server_reports_error(self):
        st = SpeedTester(Path(tempfile.mkdtemp()), "http://127.0.0.1:9")
        st.start()
        deadline = time.time() + 20
        while st.status()["state"] == "running" and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(st.status()["state"], "error")
        self.assertIn("Couldn't reach", st.status()["error"])

    def test_wifi_result_validation(self):
        st = SpeedTester(Path(tempfile.mkdtemp()))
        self.assertIsNone(st.add_wifi_result({}))
        self.assertIsNone(st.add_wifi_result({"download_mbps": "fast"}))
        self.assertIsNone(st.add_wifi_result({"download_mbps": -5}))
        rec = st.add_wifi_result({"download_mbps": 412.345, "upload_mbps": 220, "latency_ms": 3.2,
                                  "client": "iPhone · Safari" + "x" * 100})
        self.assertEqual(rec["download_mbps"], 412.35)
        self.assertEqual(len(rec["client"]), 60)
        self.assertEqual(st.status()["history"][-1]["kind"], "wifi")


class RateLimitingCloudflare(FakeCloudflare):
    """FakeCloudflare that answers some requests with HTTP 429."""
    policy = "none"   # "latency-once", "some-streams", "always"
    counts = {"latency": 0, "download": 0}
    lock = threading.Lock()

    def _too_many(self, retry_after):
        self.send_response(429)
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        size = int(parse_qs(urlparse(self.path).query).get("bytes", ["0"])[0])
        kind = "latency" if size == 0 else "download"
        with self.lock:
            self.counts[kind] += 1
            n = self.counts[kind]
        p = self.policy
        if p == "always" or (p == "latency-once" and kind == "latency" and n == 2) \
                or (p == "some-streams" and kind == "download" and n <= 3):
            return self._too_many(120 if p == "always" else (1 if kind == "latency" else 300))
        return super().do_GET()


class RateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RateLimitingCloudflare)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.saved = (speedtest.MAX_PHASE_S, speedtest.MIN_PHASE_S, speedtest.WARMUP_S)
        speedtest.MAX_PHASE_S, speedtest.MIN_PHASE_S, speedtest.WARMUP_S = 2.5, 2.0, 0.5

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        speedtest.MAX_PHASE_S, speedtest.MIN_PHASE_S, speedtest.WARMUP_S = cls.saved

    def run_test(self, policy):
        RateLimitingCloudflare.policy = policy
        RateLimitingCloudflare.counts = {"latency": 0, "download": 0}
        st = SpeedTester(Path(tempfile.mkdtemp()), self.url)
        st.start()
        deadline = time.time() + 30
        while st.status()["state"] == "running" and time.time() < deadline:
            time.sleep(0.1)
        return st

    def test_brief_limit_during_ping_is_retried(self):
        st = self.run_test("latency-once")
        status = st.status()
        self.assertEqual(status["state"], "idle", status.get("error"))
        self.assertNotIn("limited", status["history"][-1])

    def test_some_streams_limited_still_measures(self):
        st = self.run_test("some-streams")
        status = st.status()
        self.assertEqual(status["state"], "idle", status.get("error"))
        r = status["history"][-1]
        self.assertTrue(r["limited"])
        self.assertGreater(r["download_mbps"], 0)

    def test_hard_limit_cools_down(self):
        st = self.run_test("always")
        status = st.status()
        self.assertEqual(status["state"], "error")
        self.assertIn("Try again in about 2 minutes", status["error"])
        self.assertGreater(status["cooldown_until"], time.time() + 100)
        self.assertEqual(status["history"], [])
        # Starting again during the cooldown doesn't touch the server
        before = dict(RateLimitingCloudflare.counts)
        self.assertFalse(st.start())
        self.assertEqual(RateLimitingCloudflare.counts, before)
        self.assertIn("Cloudflare is limiting", st.status()["error"])


class LanEndpointTests(unittest.TestCase):
    """The Wi-Fi test endpoints on the real Network View server."""

    @classmethod
    def setUpClass(cls):
        from netview import server
        data_dir = Path(tempfile.mkdtemp())
        inv = server.Inventory(data_dir)
        speed = SpeedTester(data_dir, "http://127.0.0.1:9")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(inv, speed, {"lan_url": None}))
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_download_and_caps(self):
        with self.opener.open(f"{self.base}/api/speedtest/lan/down?bytes=3000000") as r:
            self.assertEqual(len(r.read()), 3_000_000)
        with self.opener.open(f"{self.base}/api/speedtest/lan/down?bytes=999999999999") as r:
            self.assertEqual(r.headers["Content-Length"], "100000000")

    def test_upload_and_results(self):
        req = urllib.request.Request(f"{self.base}/api/speedtest/lan/up", data=bytes(2_000_000), method="POST")
        with self.opener.open(req) as r:
            self.assertEqual(json.loads(r.read())["received"], 2_000_000)
        body = json.dumps({"download_mbps": 300, "upload_mbps": 150, "latency_ms": 4}).encode()
        req = urllib.request.Request(f"{self.base}/api/speedtest/results", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req) as r:
            self.assertEqual(json.loads(r.read())["kind"], "wifi")
        with self.opener.open(f"{self.base}/api/speedtest") as r:
            self.assertEqual(json.loads(r.read())["history"][-1]["download_mbps"], 300)

    def test_cross_origin_upload_rejected(self):
        req = urllib.request.Request(f"{self.base}/api/speedtest/lan/up", data=bytes(1000), method="POST",
                                     headers={"Origin": "http://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.opener.open(req)
        self.assertEqual(cm.exception.code, 403)

    def test_keepalive_survives_unread_body(self):
        """A rejected request with a body must not corrupt the next request."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1])
        conn.request("POST", "/api/nope", body=b"GET /api/devices HTTP/1.1\r\n\r\n")
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 404)
        self.assertEqual(r.getheader("Connection"), "close")
        conn.close()


if __name__ == "__main__":
    unittest.main()
