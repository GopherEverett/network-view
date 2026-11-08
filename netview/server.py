"""HTTP server: JSON API + static dashboard. Standard library only."""

from __future__ import annotations

import argparse
import ipaddress
import json
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import scanner
from .vendors import VendorDB, guess_type

STATIC_DIR = Path(__file__).parent / "static"


class Inventory:
    """Remembers every device ever seen, plus user-assigned names and notes."""

    def __init__(self, data_dir: Path, cidr: str | None = None):
        self.data_dir = data_dir
        self.file = data_dir / "devices.json"
        self.cidr = cidr
        self.lock = threading.RLock()
        self.devices: dict[str, dict] = {}
        self.network: scanner.Network | None = None
        self.status = {"state": "idle", "phase": None, "done": 0, "total": 0,
                       "last_scan": None, "duration_s": None, "error": None}
        self.vendors = VendorDB(data_dir)
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            saved = json.loads(self.file.read_text())
            self.devices = saved["devices"]
            self.created_at = saved["created_at"]
        except (OSError, ValueError, KeyError, TypeError):
            self.devices = {}
            self.created_at = None  # set on the first completed scan
        for d in self.devices.values():
            d["online"] = False

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"created_at": self.created_at, "devices": self.devices},
                                  indent=2, sort_keys=True))
        tmp.replace(self.file)

    # ---- scanning ----------------------------------------------------------
    def start_scan(self) -> bool:
        with self.lock:
            if self.status["state"] == "scanning":
                return False
            self.status.update(state="scanning", phase="starting", done=0, total=0, error=None)
        threading.Thread(target=self._scan, daemon=True).start()
        return True

    def _progress(self, phase: str, done: int, total: int) -> None:
        with self.lock:
            self.status.update(phase=phase, done=done, total=total)

    def _scan(self) -> None:
        started = time.time()
        try:
            net = scanner.detect_network(self.cidr)
            self.network = net
            found = scanner.scan(net, progress=self._progress)
            self._merge(found, net, started)
            with self.lock:
                self.status.update(state="idle", phase=None, last_scan=started,
                                   duration_s=round(time.time() - started, 1))
        except Exception as e:  # never let the background thread die silently
            with self.lock:
                self.status.update(state="error", phase=None, error=str(e))

    @staticmethod
    def key_for(dev: scanner.Device) -> str:
        return dev.mac or f"ip:{dev.ip}"

    def _merge(self, found: list[scanner.Device], net: scanner.Network, now: float) -> None:
        with self.lock:
            if self.created_at is None:
                self.created_at = now
            seen = set()
            for dev in found:
                key = self.key_for(dev)
                # A device first seen without a MAC may now have one; fold it in.
                old_ip_key = f"ip:{dev.ip}"
                if dev.mac and old_ip_key in self.devices and key not in self.devices:
                    self.devices[key] = self.devices.pop(old_ip_key)
                rec = self.devices.setdefault(key, {"first_seen": now, "name": "", "notes": "",
                                                    "open_ports": None, "ports_scanned_at": None})
                seen.add(key)
                rec.update(
                    id=key, ip=dev.ip, mac=dev.mac, last_seen=now, online=True,
                    hostname=dev.hostname or rec.get("hostname"),
                    latency_ms=dev.latency_ms,
                    discovered_by=sorted(dev.discovered_by),
                    is_gateway=dev.ip == net.gateway,
                    is_self=dev.ip == net.local_ip,
                    randomized_mac=scanner.is_randomized_mac(dev.mac),
                )
                rec["vendor"] = self.vendors.lookup(dev.mac) or rec.get("vendor")
                self._classify(rec)
            for key, rec in self.devices.items():
                if key not in seen:
                    rec["online"] = False
                    rec["latency_ms"] = None
            self._save()

    def _classify(self, rec: dict) -> None:
        rec["type"] = guess_type(rec.get("vendor"), rec.get("hostname"), rec.get("open_ports"),
                                 rec.get("is_gateway", False), rec.get("is_self", False),
                                 rec.get("randomized_mac", False))

    def scan_ports(self, key: str) -> dict | None:
        with self.lock:
            rec = self.devices.get(key)
            if not rec:
                return None
            ip = rec["ip"]
        ports = scanner.scan_ports(ip)
        with self.lock:
            rec.update(open_ports=ports, ports_scanned_at=time.time())
            self._classify(rec)
            self._save()
            return dict(rec)

    def update(self, key: str, fields: dict) -> dict | None:
        with self.lock:
            rec = self.devices.get(key)
            if not rec:
                return None
            for f in ("name", "notes"):
                if f in fields and isinstance(fields[f], str):
                    rec[f] = fields[f][:200 if f == "name" else 2000]
            self._save()
            return dict(rec)

    def forget(self, key: str) -> bool:
        with self.lock:
            if self.devices.pop(key, None) is None:
                return False
            self._save()
            return True

    def snapshot(self) -> dict:
        with self.lock:
            # Vendor DB may have finished downloading since the last scan.
            for rec in self.devices.values():
                if not rec.get("vendor") and rec.get("mac"):
                    if v := self.vendors.lookup(rec["mac"]):
                        rec["vendor"] = v
                        self._classify(rec)
            devices = sorted(self.devices.values(),
                             key=lambda d: (not d.get("online"), ipaddress.IPv4Address(d["ip"])))
            return {
                "network": self.network.to_dict() if self.network else None,
                "status": dict(self.status),
                "vendor_db": {"source": self.vendors.source, "entries": len(self.vendors.table)},
                "port_names": scanner.COMMON_PORTS,
                # Devices found on the very first scan are the baseline, not "new".
                "baseline_until": (self.created_at + 60) if self.created_at else None,
                "devices": devices,
            }


def make_handler(inv: Inventory):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(STATIC_DIR), **kw)

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _device_key(self) -> str | None:
            parts = urlparse(self.path).path.split("/")
            # /api/devices/<key>[/ports]
            return unquote(parts[3]) if len(parts) >= 4 and parts[3] else None

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(min(length, 65536)))
                return data if isinstance(data, dict) else {}
            except ValueError:
                return {}

        def _same_origin(self) -> bool:
            # Block other websites from driving the API via the user's browser.
            origin = self.headers.get("Origin")
            return origin is None or urlparse(origin).netloc == self.headers.get("Host")

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/devices":
                return self._json(inv.snapshot())
            if path.startswith("/api/"):
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return super().do_GET()

        def do_POST(self):
            if not self._same_origin():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            path = urlparse(self.path).path
            if path == "/api/scan":
                started = inv.start_scan()
                return self._json({"started": started}, HTTPStatus.ACCEPTED)
            if path.startswith("/api/devices/") and path.endswith("/ports"):
                rec = inv.scan_ports(self._device_key() or "")
                return self._json(rec) if rec else self._json({"error": "unknown device"}, HTTPStatus.NOT_FOUND)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_PATCH(self):
            if not self._same_origin():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            rec = inv.update(self._device_key() or "", self._body())
            return self._json(rec) if rec else self._json({"error": "unknown device"}, HTTPStatus.NOT_FOUND)

        def do_DELETE(self):
            if not self._same_origin():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            ok = inv.forget(self._device_key() or "")
            return self._json({"deleted": ok}, HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND)

    return Handler


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="See every device on your local network.")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default 127.0.0.1; use 0.0.0.0 to reach it from other devices)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cidr", help="subnet to scan, e.g. 192.168.1.0/24 (default: auto-detect)")
    p.add_argument("--interval", type=int, default=300,
                   help="seconds between automatic rescans; 0 disables (default 300)")
    p.add_argument("--data-dir", type=Path, default=Path.home() / ".network-view",
                   help="where device history is stored (default ~/.network-view)")
    p.add_argument("--no-download", action="store_true", help="don't download the IEEE vendor database")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = p.parse_args(argv)

    inv = Inventory(args.data_dir, args.cidr)
    inv.vendors.load(allow_download=not args.no_download)
    inv.network = scanner.detect_network(args.cidr)
    inv.start_scan()

    if args.interval > 0:
        def rescan_loop():
            while True:
                time.sleep(args.interval)
                inv.start_scan()
        threading.Thread(target=rescan_loop, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(inv))
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    net = inv.network
    print(f"Network View running at {url}")
    print(f"Scanning {net.network} on {net.interface or 'default interface'} (this computer: {net.local_ip})")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
