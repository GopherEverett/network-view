import ipaddress
import unittest

from netview import scanner
from netview.vendors import VendorDB, describe_download_error, guess_type, parse_ieee_csv


class MacTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(scanner.normalize_mac("0:1B:2c:3:4:5"), "00:1b:2c:03:04:05")
        self.assertEqual(scanner.normalize_mac("00-1B-2C-03-04-05"), "00:1b:2c:03:04:05")
        self.assertIsNone(scanner.normalize_mac("00:00:00:00:00:00"))
        self.assertIsNone(scanner.normalize_mac("ff:ff:ff:ff:ff:ff"))
        self.assertIsNone(scanner.normalize_mac("(incomplete)"))

    def test_randomized(self):
        self.assertTrue(scanner.is_randomized_mac("da:a1:19:00:00:01"))
        self.assertFalse(scanner.is_randomized_mac("00:1b:2c:03:04:05"))


class ArpParsing(unittest.TestCase):
    def test_proc_net_arp(self):
        text = (
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.1.1      0x1         0x2         a4:2b:b0:11:22:33     *        wlan0\n"
            "192.168.1.50     0x1         0x0         00:00:00:00:00:00     *        wlan0\n"
        )
        self.assertEqual(scanner.parse_proc_net_arp(text), {"192.168.1.1": "a4:2b:b0:11:22:33"})

    def test_macos_arp(self):
        text = (
            "? (192.168.1.1) at a4:2b:b0:11:22:33 on en0 ifscope [ethernet]\n"
            "? (192.168.1.7) at 0:c:29:a:b:c on en0 ifscope [ethernet]\n"
            "? (192.168.1.9) at (incomplete) on en0 ifscope [ethernet]\n"
        )
        self.assertEqual(scanner.parse_arp_a(text), {
            "192.168.1.1": "a4:2b:b0:11:22:33", "192.168.1.7": "00:0c:29:0a:0b:0c"})

    def test_windows_arp(self):
        text = (
            "Interface: 192.168.1.20 --- 0xb\n"
            "  Internet Address      Physical Address      Type\n"
            "  192.168.1.1           a4-2b-b0-11-22-33     dynamic\n"
            "  192.168.1.255         ff-ff-ff-ff-ff-ff     static\n"
        )
        self.assertEqual(scanner.parse_arp_a(text), {"192.168.1.1": "a4:2b:b0:11:22:33"})

    def test_ip_neigh(self):
        text = (
            "192.168.1.1 dev wlan0 lladdr a4:2b:b0:11:22:33 REACHABLE\n"
            "192.168.1.8 dev wlan0  FAILED\n"
        )
        self.assertEqual(scanner.parse_ip_neigh(text), {"192.168.1.1": "a4:2b:b0:11:22:33"})


class SubnetParsing(unittest.TestCase):
    def test_proc_net_route(self):
        text = (
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
            "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
            "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
        )
        self.assertEqual(scanner.parse_proc_net_route(text, "192.168.1.20"), ("wlan0", 24, "192.168.1.1"))

    def test_ip_addr(self):
        text = "2: wlan0    inet 192.168.1.20/24 brd 192.168.1.255 scope global dynamic wlan0\n"
        self.assertEqual(scanner.parse_ip_addr(text, "192.168.1.20"), ("wlan0", 24))

    def test_macos_ifconfig(self):
        text = (
            "en0: flags=8863<UP,BROADCAST,SMART,RUNNING> mtu 1500\n"
            "\tether a4:83:e7:00:00:01\n"
            "\tinet 10.0.0.12 netmask 0xfffff000 broadcast 10.0.15.255\n"
        )
        self.assertEqual(scanner.parse_ifconfig(text, "10.0.0.12"), ("en0", 20))

    def test_ipconfig(self):
        text = (
            "Ethernet adapter Ethernet:\n\n"
            "   Media State . . . . . . . . . . . : Media disconnected\n\n"
            "Wireless LAN adapter Wi-Fi:\n\n"
            "   IPv4 Address. . . . . . . . . . . : 192.168.0.42\n"
            "   Subnet Mask . . . . . . . . . . . : 255.255.255.0\n"
            "   Default Gateway . . . . . . . . . : 192.168.0.1\n"
        )
        self.assertEqual(scanner.parse_ipconfig(text, "192.168.0.42"), ("Wireless LAN adapter Wi-Fi", 24, "192.168.0.1"))

    def test_large_subnet_capped(self):
        net = scanner.Network("eth0", "10.1.2.3", ipaddress.IPv4Network("10.0.0.0/8"))
        hosts = scanner.hosts_to_scan(net)
        self.assertEqual(len(hosts), 1022)
        self.assertIn("10.1.2.3", hosts)


class VendorTests(unittest.TestCase):
    def test_bundled_db(self):
        import tempfile
        from pathlib import Path
        db = VendorDB(Path(tempfile.mkdtemp()))
        db.load(allow_download=False)
        self.assertEqual(db.source, "built-in list")
        self.assertGreater(len(db.table), 40000)
        self.assertEqual(db.lookup("b8:27:eb:12:34:56"), "Raspberry Pi Foundation")
        self.assertEqual(db.lookup("00:11:32:ab:cd:ef"), "Synology Incorporated")

    def test_longest_prefix_wins(self):
        import tempfile
        from pathlib import Path
        db = VendorDB(Path(tempfile.mkdtemp()))
        db.table = {"70b3d5": "IEEE Registration Authority", "70b3d51": "Small Maker", "70b3d5123": "Tiny Maker"}
        self.assertEqual(db.lookup("70:b3:d5:12:34:56"), "Tiny Maker")
        self.assertEqual(db.lookup("70:b3:d5:1f:00:00"), "Small Maker")
        self.assertEqual(db.lookup("70:b3:d5:ff:00:00"), "IEEE Registration Authority")

    def test_ieee_csv(self):
        text = ("Registry,Assignment,Organization Name,Organization Address\n"
                "MA-L,B827EB,Raspberry Pi Foundation,Cambridge GB\n"
                "MA-S,70B3D5123,Tiny Maker,Somewhere\n")
        self.assertEqual(parse_ieee_csv(text), {"b827eb": "Raspberry Pi Foundation", "70b3d5123": "Tiny Maker"})

    def test_ssl_error_gets_mac_hint(self):
        import ssl
        import urllib.error
        err = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        msg, hint = describe_download_error(err)
        self.assertIn("certificate", msg)
        self.assertIn("Install Certificates", hint)

    def test_guess_type(self):
        self.assertEqual(guess_type(None, None, None, is_gateway=True), "Router")
        self.assertEqual(guess_type("Brother Industries", None, None), "Printer")
        self.assertEqual(guess_type(None, "johns-iPhone.lan", None), "Phone")
        self.assertEqual(guess_type(None, None, [9100]), "Printer")
        # "ring" must not match inside "Engineering"
        self.assertEqual(guess_type("Acme Engineering", None, None), "Unknown")
        self.assertEqual(guess_type(None, None, None, randomized_mac=True), "Phone / Tablet")

    def test_guess_type_from_announcements(self):
        # A chip maker's MAC prefix, but the device says what it is.
        self.assertEqual(guess_type("Hon Hai Precision", None, None, services=["googlecast"]), "TV / Streaming")
        self.assertEqual(guess_type(None, None, None, model="iPhone14,2"), "Phone")
        self.assertEqual(guess_type(None, None, None, model="MacBookPro18,1"), "Computer")
        self.assertEqual(guess_type(None, None, None, model="AudioAccessory5,1"), "Speaker")
        self.assertEqual(guess_type(None, None, None, upnp_types=["MediaRenderer"]), "TV / Streaming")
        self.assertEqual(guess_type("Murata Manufacturing", None, None, friendly_name="Kitchen Sonos"), "Speaker")
        self.assertEqual(guess_type(None, "BRN3C2AF4123456", None), "Printer")
        self.assertEqual(guess_type("Espressif Inc.", None, None), "Smart home")
        # A randomized-MAC Apple device that only advertises companion-link
        self.assertEqual(guess_type(None, None, None, services=["companion-link"], randomized_mac=True),
                         "Apple device")


def _dns_response(records):
    """Build an mDNS response packet; names after the first use compression."""
    import struct
    from netview.discovery import encode_name
    body, first_name_off = b"", None
    for name, rtype, rdata in records:
        if first_name_off is not None and name == records[0][0]:
            enc = struct.pack(">H", 0xC000 | first_name_off)
        else:
            enc = encode_name(name)
            if first_name_off is None:
                first_name_off = 12
        body += enc + struct.pack(">HHIH", rtype, 0x8001, 120, len(rdata)) + rdata
    return struct.pack(">HHHHHH", 0, 0x8400, 0, len(records), 0, 0) + body


class MdnsTests(unittest.TestCase):
    def test_parse_and_interpret_chromecast(self):
        import socket
        import struct
        from netview.discovery import TYPE_A, TYPE_PTR, TYPE_SRV, TYPE_TXT, encode_name, interpret_mdns, parse_dns
        inst = "Chromecast-Ultra-0123456789abcdef0123._googlecast._tcp.local"
        txt = b"".join(bytes([len(x)]) + x for x in [b"md=Chromecast Ultra", b"fn=Living Room TV", b"id=abc"])
        pkt = _dns_response([
            ("_googlecast._tcp.local", TYPE_PTR, encode_name(inst)),
            (inst, TYPE_TXT, txt),
            (inst, TYPE_SRV, struct.pack(">HHH", 0, 0, 8009) + encode_name("0123-abcd.local")),
            ("0123-abcd.local", TYPE_A, socket.inet_aton("192.168.1.50")),
            ("_googlecast._tcp.local", TYPE_PTR, encode_name(inst)),  # compressed name
        ])
        records = parse_dns(pkt)
        self.assertEqual(len(records), 5)
        hosts = {}
        interpret_mdns(records, "192.168.1.50", hosts)
        h = hosts["192.168.1.50"]
        self.assertEqual(h.friendly_name, "Living Room TV")
        self.assertEqual(h.model, "Chromecast Ultra")
        self.assertEqual(h.mdns_name, "0123-abcd")
        self.assertEqual(h.services, {"googlecast"})

    def test_reverse_lookup_and_raop_name(self):
        from netview.discovery import TYPE_PTR, encode_name, interpret_mdns, parse_dns
        pkt = _dns_response([
            ("20.1.168.192.in-addr.arpa", TYPE_PTR, encode_name("Johns-iPhone.local")),
            ("_raop._tcp.local", TYPE_PTR, encode_name("A1B2C3D4E5F6@Kitchen HomePod._raop._tcp.local")),
        ])
        hosts = {}
        interpret_mdns(parse_dns(pkt), "192.168.1.20", hosts)
        self.assertEqual(hosts["192.168.1.20"].mdns_name, "Johns-iPhone")
        self.assertEqual(hosts["192.168.1.20"].friendly_name, "Kitchen HomePod")

    def test_malformed_packets_dont_crash(self):
        from netview.discovery import parse_dns
        self.assertEqual(parse_dns(b""), [])
        self.assertEqual(parse_dns(b"\x00" * 11), [])
        # Pointer loop
        loop = bytes.fromhex("000084000000000100000000") + b"\xc0\x0c"
        self.assertEqual(parse_dns(loop), [])


class UpnpTests(unittest.TestCase):
    def test_description(self):
        from netview.discovery import parse_ssdp_response, parse_upnp_description
        xml = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
  <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
  <friendlyName>Bedroom TV</friendlyName><manufacturer>Samsung Electronics</manufacturer>
  <modelName>QN55Q60</modelName><modelNumber>AAFXZA</modelNumber>
</device></root>"""
        info = parse_upnp_description(xml)
        self.assertEqual(info.friendly_name, "Bedroom TV")
        self.assertEqual(info.manufacturer, "Samsung Electronics")
        self.assertEqual(info.model, "QN55Q60 AAFXZA")
        self.assertEqual(info.upnp_types, {"MediaRenderer"})
        self.assertEqual(parse_upnp_description("<not xml").friendly_name, None)
        hdr = parse_ssdp_response(b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.9:8060/\r\nST: upnp:rootdevice\r\n\r\n")
        self.assertEqual(hdr["location"], "http://192.168.1.9:8060/")


if __name__ == "__main__":
    unittest.main()
