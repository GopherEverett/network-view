import ipaddress
import unittest

from netview import scanner
from netview.vendors import VendorDB, guess_type


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
    def test_text_db(self):
        db = VendorDB.__new__(VendorDB)
        db.table, db._lock = {}, __import__("threading").Lock()
        db._load_text_db("A42BB0     Example Networks\n00-0C-29   (hex)\t\tVMware, Inc.\n")
        self.assertEqual(db.lookup("a4:2b:b0:11:22:33"), "Example Networks")
        self.assertEqual(db.lookup("00:0c:29:0a:0b:0c"), "VMware, Inc.")

    def test_guess_type(self):
        self.assertEqual(guess_type(None, None, None, is_gateway=True), "Router")
        self.assertEqual(guess_type("Brother Industries", None, None), "Printer")
        self.assertEqual(guess_type(None, "johns-iPhone.lan", None), "Phone")
        self.assertEqual(guess_type(None, None, [9100]), "Printer")
        # "ring" must not match inside "Engineering"
        self.assertEqual(guess_type("Acme Engineering", None, None), "Unknown")
        self.assertEqual(guess_type(None, None, None, randomized_mac=True), "Phone / Tablet")


if __name__ == "__main__":
    unittest.main()
