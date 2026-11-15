"""Internet speed test (against Cloudflare's speed-test servers) + result history.

The Wi-Fi test runs in the browser against this app's own server (see
server.py's /api/speedtest/lan/* endpoints and static/speedtest.js); its
results are saved here too.

How the internet test measures:
* latency/jitter: a dozen tiny requests over one kept-alive connection; the
  first (TLS setup) is discarded, and Cloudflare's reported server time is
  subtracted, so what's left is the round trip.
* download/upload: several parallel streams (one connection can't fill a fast
  link), sampled every 200 ms. The first 2 s are ignored while TCP ramps up.
  A phase ends after 10 s, or after 4 s once 150 MB has moved, to limit data use.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import statistics
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_SERVER = "https://speed.cloudflare.com"
STREAMS = 6
DOWN_CHUNK = 25_000_000      # bytes per download request
UP_CHUNK = 8_000_000         # bytes per upload request
BLOCK = 64 * 1024
WARMUP_S = 2.0
MIN_PHASE_S = 4.0
MAX_PHASE_S = 10.0
DATA_CAP = 150_000_000       # per direction, once MIN_PHASE_S has passed
HISTORY_LIMIT = 200
_PAYLOAD = os.urandom(BLOCK)  # incompressible upload data


class SpeedTestError(Exception):
    pass


class SpeedTester:
    def __init__(self, data_dir: Path, server: str = DEFAULT_SERVER):
        self.file = data_dir / "speedtests.json"
        self.server = server.rstrip("/")
        self.lock = threading.Lock()
        self.state = {"state": "idle", "phase": None, "progress": 0.0,
                      "live_mbps": None, "partial": {}, "error": None}
        try:
            self.history: list[dict] = json.loads(self.file.read_text())
            if not isinstance(self.history, list):
                self.history = []
        except (OSError, ValueError):
            self.history = []

    # ---- public API --------------------------------------------------------
    def start(self) -> bool:
        with self.lock:
            if self.state["state"] == "running":
                return False
            self.state = {"state": "running", "phase": "latency", "progress": 0.0,
                          "live_mbps": None, "partial": {}, "error": None}
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def add_wifi_result(self, data: dict) -> dict | None:
        """Validate and store a result measured by the browser."""
        def num(key, lo, hi):
            v = data.get(key)
            if isinstance(v, (int, float)) and lo <= v <= hi:
                return round(float(v), 2)
            return None
        down, up = num("download_mbps", 0, 100_000), num("upload_mbps", 0, 100_000)
        if down is None and up is None:
            return None
        rec = {"kind": "wifi", "at": time.time(), "download_mbps": down, "upload_mbps": up,
               "latency_ms": num("latency_ms", 0, 60_000), "jitter_ms": num("jitter_ms", 0, 60_000),
               "client": str(data.get("client") or "")[:60] or None}
        self._save_result(rec)
        return rec

    def status(self) -> dict:
        with self.lock:
            return {**self.state, "partial": dict(self.state["partial"]),
                    "history": list(self.history[-HISTORY_LIMIT:])}

    # ---- internals ---------------------------------------------------------
    def _set(self, **kw) -> None:
        with self.lock:
            partial = kw.pop("partial", None)
            self.state.update(kw)
            if partial:
                self.state["partial"].update(partial)

    def _save_result(self, rec: dict) -> None:
        with self.lock:
            self.history.append(rec)
            self.history = self.history[-HISTORY_LIMIT:]
            try:
                self.file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.file.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.history, indent=1))
                tmp.replace(self.file)
            except OSError:
                pass

    def _connect(self) -> http.client.HTTPConnection:
        u = urlparse(self.server)
        if u.scheme == "https":
            return http.client.HTTPSConnection(u.hostname, u.port, timeout=15,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(u.hostname, u.port, timeout=15)

    def _run(self) -> None:
        started = time.time()
        try:
            latency, jitter, location = self._latency()
            self._set(phase="download", progress=0.0, partial={"latency_ms": latency, "jitter_ms": jitter})
            down, down_bytes = self._throughput("download")
            self._set(phase="upload", progress=0.0, live_mbps=None, partial={"download_mbps": down})
            up, up_bytes = self._throughput("upload")
        except Exception as e:
            self._set(state="error", phase=None, live_mbps=None, error=describe_error(e))
            return
        rec = {"kind": "internet", "at": started, "download_mbps": down, "upload_mbps": up,
               "latency_ms": latency, "jitter_ms": jitter, "server": location,
               "data_mb": round((down_bytes + up_bytes) / 1e6)}
        self._save_result(rec)
        self._set(state="idle", phase=None, progress=1.0, live_mbps=None, partial={}, result=rec)

    def _latency(self, count: int = 12) -> tuple[float, float, str | None]:
        conn = self._connect()
        rtts, location = [], None
        try:
            for i in range(count + 1):
                t0 = time.perf_counter()
                conn.request("GET", "/__down?bytes=0", headers={"Cache-Control": "no-store"})
                resp = conn.getresponse()
                resp.read()
                rtt = (time.perf_counter() - t0) * 1000
                if resp.status >= 400:
                    raise SpeedTestError(f"the speed-test server answered HTTP {resp.status}")
                if i == 0:  # includes connection + TLS setup
                    ray = resp.getheader("cf-ray") or ""
                    location = ray.rsplit("-", 1)[1] if "-" in ray else None
                    continue
                m = re.search(r"dur=([\d.]+)", resp.getheader("server-timing") or "")
                rtts.append(max(0.1, rtt - (float(m.group(1)) if m else 0)))
                self._set(progress=i / count)
        finally:
            conn.close()
        jitter = statistics.mean(abs(a - b) for a, b in zip(rtts, rtts[1:])) if len(rtts) > 1 else 0.0
        return round(statistics.median(rtts), 1), round(jitter, 1), location

    def _throughput(self, direction: str) -> tuple[float, int]:
        stop = threading.Event()
        counts = [0] * STREAMS
        errors: list[Exception] = []

        def download(i: int) -> None:
            conn = self._connect()
            try:
                while not stop.is_set():
                    conn.request("GET", f"/__down?bytes={DOWN_CHUNK}", headers={"Cache-Control": "no-store"})
                    resp = conn.getresponse()
                    if resp.status >= 400:
                        raise SpeedTestError(f"the speed-test server answered HTTP {resp.status}")
                    while not stop.is_set() and (block := resp.read(BLOCK)):
                        counts[i] += len(block)
            finally:
                conn.close()

        def upload(i: int) -> None:
            conn = self._connect()
            try:
                while not stop.is_set():
                    conn.putrequest("POST", "/__up")
                    conn.putheader("Content-Type", "application/octet-stream")
                    conn.putheader("Content-Length", str(UP_CHUNK))
                    conn.endheaders()
                    sent = 0
                    while sent < UP_CHUNK:
                        if stop.is_set():
                            return
                        n = min(BLOCK, UP_CHUNK - sent)
                        conn.send(_PAYLOAD[:n])
                        sent += n
                        counts[i] += n
                    resp = conn.getresponse()
                    resp.read()
                    if resp.status >= 400:
                        raise SpeedTestError(f"the speed-test server answered HTTP {resp.status}")
            finally:
                conn.close()

        def worker(i: int) -> None:
            fn = download if direction == "download" else upload
            failures = 0
            while not stop.is_set():
                try:
                    fn(i)
                except Exception as e:  # reconnect a couple of times, then give up
                    if stop.is_set():
                        return
                    failures += 1
                    if failures >= 3:
                        errors.append(e)
                        return
                    time.sleep(0.3)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(STREAMS)]
        start = time.perf_counter()
        for t in threads:
            t.start()

        samples: list[tuple[float, int]] = [(0.0, 0)]
        while True:
            time.sleep(0.2)
            t, total = time.perf_counter() - start, sum(counts)
            samples.append((t, total))
            recent = [s for s in samples if s[0] >= t - 1.0]
            live = (recent[-1][1] - recent[0][1]) * 8 / 1e6 / max(0.2, recent[-1][0] - recent[0][0])
            self._set(live_mbps=round(live, 1), progress=min(1.0, t / MAX_PHASE_S))
            if len(errors) == STREAMS:
                stop.set()
                raise errors[0]
            if t >= MAX_PHASE_S or (t >= MIN_PHASE_S and total >= DATA_CAP):
                break
        stop.set()
        for th in threads:
            th.join(timeout=3)

        t_end, b_end = samples[-1]
        warm = next((s for s in samples if s[0] >= WARMUP_S), samples[0])
        if t_end - warm[0] < 1.0:  # very short run: use everything
            warm = samples[0]
        if b_end == 0:
            raise SpeedTestError(f"no data could be {'downloaded' if direction == 'download' else 'uploaded'}")
        mbps = (b_end - warm[1]) * 8 / 1e6 / (t_end - warm[0])
        return round(mbps, 1), b_end


def describe_error(e: Exception) -> str:
    reason = getattr(e, "reason", e)
    if isinstance(e, ssl.SSLCertVerificationError) or isinstance(reason, ssl.SSLCertVerificationError):
        return ("Couldn't verify the speed-test server's certificate. On macOS with Python from python.org, "
                "run “Install Certificates.command” in your Applications/Python 3.x folder.")
    if isinstance(e, SpeedTestError):
        return f"The test failed: {e}."
    if isinstance(e, (OSError, http.client.HTTPException)):
        return f"Couldn't reach the speed-test server ({e}). Check your internet connection."
    return f"The test failed: {e}"
