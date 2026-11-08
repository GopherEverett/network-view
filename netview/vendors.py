"""MAC vendor (OUI) lookup and best-effort device type guessing."""

from __future__ import annotations

import csv
import io
import os
import re
import threading
import urllib.request
from pathlib import Path

IEEE_CSV_URL = "https://standards-oui.ieee.org/oui/oui.csv"

# Vendor databases that may already be on the machine.
SYSTEM_DBS = [
    "/usr/share/nmap/nmap-mac-prefixes",
    "/usr/local/share/nmap/nmap-mac-prefixes",
    "/opt/homebrew/share/nmap/nmap-mac-prefixes",
    "/usr/share/ieee-data/oui.txt",
    "/usr/share/misc/oui.txt",
    "/usr/share/wireshark/manuf",
]


class VendorDB:
    def __init__(self, cache_dir: Path):
        self.cache_file = cache_dir / "oui.csv"
        self.table: dict[str, str] = {}
        self.source: str | None = None
        self._lock = threading.Lock()

    def load(self, allow_download: bool = True) -> None:
        if self.cache_file.exists():
            self._load_ieee_csv(self.cache_file.read_text(encoding="utf-8", errors="ignore"))
            self.source = "IEEE (cached)"
            return
        for path in SYSTEM_DBS:
            if os.path.exists(path):
                self._load_text_db(Path(path).read_text(encoding="utf-8", errors="ignore"))
                self.source = path
                break
        if allow_download:
            threading.Thread(target=self._download, daemon=True).start()

    def _download(self) -> None:
        try:
            req = urllib.request.Request(IEEE_CSV_URL, headers={"User-Agent": "network-view"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            return
        if "Assignment" not in text[:200]:
            return
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(text, encoding="utf-8")
        self._load_ieee_csv(text)
        self.source = "IEEE (downloaded)"

    def _load_ieee_csv(self, text: str) -> None:
        table = {}
        for row in csv.DictReader(io.StringIO(text)):
            prefix = (row.get("Assignment") or "").strip().lower()
            name = (row.get("Organization Name") or "").strip()
            if len(prefix) == 6 and name:
                table[prefix] = name
        with self._lock:
            self.table.update(table)

    def _load_text_db(self, text: str) -> None:
        """nmap-mac-prefixes, oui.txt and wireshark manuf formats."""
        table = {}
        for line in text.splitlines():
            m = (re.match(r"^([0-9A-Fa-f]{6})\s+(.+)$", line)
                 or re.match(r"^([0-9A-Fa-f]{2}[-:][0-9A-Fa-f]{2}[-:][0-9A-Fa-f]{2})\s+(?:\(hex\)\s+)?(.+)$", line))
            if m:
                prefix = re.sub(r"[-:]", "", m.group(1)).lower()
                name = m.group(2).split("\t")[-1].strip()
                table.setdefault(prefix, name)
        with self._lock:
            self.table.update(table)

    def lookup(self, mac: str | None) -> str | None:
        if not mac:
            return None
        with self._lock:
            return self.table.get(mac.replace(":", "")[:6].lower())


# --------------------------------------------------------------------------
# Device type guessing
# --------------------------------------------------------------------------

# (device type, keywords matched against vendor + hostname, lowercase)
_TYPE_HINTS = [
    ("Phone", ["iphone", "android", "galaxy", "pixel", "oneplus", "xiaomi", "huawei", "oppo", "motorola"]),
    ("Tablet", ["ipad", "tablet"]),
    ("TV / Streaming", ["roku", "chromecast", "appletv", "apple-tv", "firetv", "fire-tv", "vizio", "tcl",
                        "lg electronics", "hisense", "sonos", "bravia", "shield", "tv"]),
    ("Game console", ["nintendo", "playstation", "xbox", "sony interactive"]),
    ("Printer", ["printer", "epson", "brother", "canon", "hewlett", "lexmark", "kyocera", "xerox"]),
    ("Camera", ["camera", "hikvision", "dahua", "reolink", "wyze", "arlo", "ring", "amcrest", "axis comm"]),
    ("Smart home", ["espressif", "tuya", "shelly", "philips lighting", "signify", "hue", "nest", "ecobee",
                    "tp-link smart", "kasa", "lifx", "amazon", "echo", "google home", "ikea"]),
    ("Network gear", ["router", "gateway", "ubiquiti", "unifi", "netgear", "tp-link", "asus", "linksys",
                      "mikrotik", "cisco", "aruba", "eero", "orbi", "fritz", "arris", "technicolor", "sagemcom"]),
    ("NAS / Server", ["synology", "qnap", "nas", "server", "proxmox", "truenas", "raspberry"]),
    ("Computer", ["macbook", "imac", "mac-mini", "desktop", "laptop", "pc", "dell", "lenovo", "intel corp",
                  "micro-star", "gigabyte", "asrock", "microsoft"]),
]

_PORT_HINTS = [
    ({9100, 631, 515}, "Printer"),
    ({554}, "Camera"),
    ({8008, 8009}, "TV / Streaming"),
    ({62078}, "Phone"),
    ({32400}, "NAS / Server"),
    ({8123, 1883}, "Smart home"),
    ({3389, 445, 139}, "Computer"),
    ({548}, "Computer"),
]


def guess_type(vendor: str | None, hostname: str | None, open_ports: list[int] | None,
               is_gateway: bool = False, is_self: bool = False, randomized_mac: bool = False) -> str:
    if is_self:
        return "This computer"
    if is_gateway:
        return "Router"
    ports = set(open_ports or [])
    for hint_ports, kind in _PORT_HINTS:
        if ports & hint_ports:
            return kind
    text = f"{vendor or ''} {hostname or ''}".lower()
    for kind, words in _TYPE_HINTS:
        # Match at word starts so e.g. "ring" doesn't hit "Engineering".
        if any(re.search(r"(?<![a-z0-9])" + re.escape(w), text) for w in words):
            return kind
    if "apple" in text:
        return "Apple device"
    if randomized_mac:
        return "Phone / Tablet"  # private Wi-Fi addresses are mostly mobile devices
    return "Unknown"
