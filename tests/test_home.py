import ipaddress
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from netview import scanner, server
from netview.speedtest import SpeedTester

HOME = scanner.Network("en0", "192.168.1.20", ipaddress.IPv4Network("192.168.1.0/24"), "192.168.1.1")
HOME_ROUTER = "a4:2b:b0:11:22:33"
# A hotel using the very same IP range: only the router's MAC tells them apart.
HOTEL = scanner.Network("en0", "192.168.1.77", ipaddress.IPv4Network("192.168.1.0/24"), "192.168.1.1")
HOTEL_ROUTER = "00:90:7f:aa:bb:cc"


class FakeNet:
    """Stands in for detect_network/gateway_mac/scan; switch .where to move."""

    def __init__(self):
        self.where = "home"
        self.scans = 0

    def detect(self, cidr=None):
        return HOME if self.where == "home" else HOTEL

    def gw_mac(self, gateway):
        return HOME_ROUTER if self.where == "home" else HOTEL_ROUTER

    def scan(self, net, progress=None):
        self.scans += 1
        router = HOME_ROUTER if self.where == "home" else HOTEL_ROUTER
        return [scanner.Device(ip=net.gateway, mac=router, discovered_by={"ping"}),
                scanner.Device(ip=net.local_ip, mac="f0:18:98:aa:bb:cc", discovered_by={"self"})]


class _Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNet()
        self.dir = Path(tempfile.mkdtemp())
        for name, fn in [("detect_network", self.fake.detect), ("gateway_mac", self.fake.gw_mac),
                         ("scan", self.fake.scan), ("local_network_blocked", lambda gw: False)]:
            p = mock.patch.object(scanner, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def inv(self, **kw):
        inv = server.Inventory(self.dir, **kw)
        inv.check_network()
        return inv

    def wait_idle(self, inv):
        deadline = time.time() + 5
        while inv.status["state"] == "scanning" and time.time() < deadline:
            time.sleep(0.02)


class HomeNetworkTests(_Base):
    def test_fresh_install_asks_before_scanning(self):
        inv = self.inv()
        self.assertFalse(inv.is_trusted())
        self.assertFalse(inv.start_scan())
        self.assertEqual(inv.status["state"], "paused")
        self.assertEqual(self.fake.scans, 0)

    def test_trust_then_scan_and_persist(self):
        inv = self.inv()
        entry = inv.trust_current()
        self.assertEqual(entry["id"], f"mac:{HOME_ROUTER}")
        self.assertTrue(inv.start_scan())
        self.wait_idle(inv)
        self.assertEqual(self.fake.scans, 1)
        # Reloaded from disk
        again = self.inv()
        self.assertTrue(again.is_trusted())

    def test_same_ip_range_different_router_is_not_trusted(self):
        inv = self.inv()
        inv.trust_current()
        inv.start_scan()
        self.wait_idle(inv)
        self.assertTrue(any(d["online"] for d in inv.devices.values()))
        self.fake.where = "hotel"
        info = inv.check_network()
        self.assertTrue(info["changed"])
        self.assertFalse(info["trusted"])
        # Home devices aren't reachable from the hotel
        self.assertFalse(any(d["online"] for d in inv.devices.values()))
        self.assertFalse(inv.start_scan())
        self.assertEqual(self.fake.scans, 1)
        # A forced one-off scan is allowed
        self.assertTrue(inv.start_scan(force=True))
        self.wait_idle(inv)
        self.assertEqual(self.fake.scans, 2)
        # Back home: trusted again
        self.fake.where = "home"
        self.assertTrue(inv.check_network()["trusted"])

    def test_upgrade_adopts_single_known_router(self):
        (self.dir / "devices.json").write_text(json.dumps({"created_at": 1, "devices": {
            HOME_ROUTER: {"id": HOME_ROUTER, "ip": "192.168.1.1", "mac": HOME_ROUTER, "is_gateway": True},
        }}))
        self.assertTrue(self.inv().is_trusted())

    def test_upgrade_with_two_known_routers_asks(self):
        (self.dir / "devices.json").write_text(json.dumps({"created_at": 1, "devices": {
            HOME_ROUTER: {"id": HOME_ROUTER, "ip": "192.168.1.1", "mac": HOME_ROUTER, "is_gateway": True},
            HOTEL_ROUTER: {"id": HOTEL_ROUTER, "ip": "192.168.1.1", "mac": HOTEL_ROUTER, "is_gateway": True},
        }}))
        self.assertFalse(self.inv().is_trusted())

    def test_forget_network(self):
        inv = self.inv()
        inv.trust_current()
        self.assertTrue(inv.forget_network(f"mac:{HOME_ROUTER}"))
        self.assertFalse(inv.is_trusted())
        self.assertFalse(inv.forget_network("mac:nope"))

    def test_scan_any_network_flag(self):
        self.fake.where = "hotel"
        self.assertTrue(self.inv(home_only=False).is_trusted())

    def test_blocked_check_overruled_when_devices_answer(self):
        with mock.patch.object(scanner, "local_network_blocked", return_value=True):
            inv = self.inv()
            inv.trust_current()
            inv.start_scan()
            self.wait_idle(inv)
        self.assertFalse(inv.status["lan_blocked"])  # the router answered ping


class HomeApiTests(_Base):
    def setUp(self):
        super().setUp()
        self.inventory = self.inv()
        speed = SpeedTester(self.dir, "http://127.0.0.1:9")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.inventory, speed, {}))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)  # cleanups run last-in first-out
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_api_flow(self):
        status, body = self.call("POST", "/api/scan")
        self.assertEqual(status, 409)
        _, snap = self.call("GET", "/api/devices")
        self.assertFalse(snap["home"]["current"]["trusted"])

        status, body = self.call("POST", "/api/networks/trust")
        self.assertEqual(status, 200)
        self.wait_idle(self.inventory)
        _, snap = self.call("GET", "/api/devices")
        self.assertTrue(snap["home"]["current"]["trusted"])
        self.assertEqual(len(snap["home"]["trusted"]), 1)

        # Network IDs contain ':' and must survive URL encoding
        net_id = urllib.parse.quote(snap["home"]["trusted"][0]["id"], safe="")
        status, body = self.call("DELETE", f"/api/networks/{net_id}")
        self.assertEqual((status, body), (200, {"deleted": True}))
        # ...and must not be mistaken for a device
        self.assertTrue(self.inventory.devices)

    def test_forced_scan_on_untrusted(self):
        status, body = self.call("POST", "/api/scan", {"force": True})
        self.assertEqual(status, 202)
        self.wait_idle(self.inventory)
        self.assertEqual(self.fake.scans, 1)

    def test_unknown_paths(self):
        self.assertEqual(self.call("PATCH", "/api/networks/x", {"name": "y"})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/whatever/x")[0], 404)


if __name__ == "__main__":
    unittest.main()
