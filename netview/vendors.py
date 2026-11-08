"""MAC vendor (OUI) lookup and best-effort device type guessing."""

from __future__ import annotations

import csv
import gzip
import io
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

IEEE_CSV_URLS = [
    "https://standards-oui.ieee.org/oui/oui.csv",        # MA-L (/24 prefixes)
    "https://standards-oui.ieee.org/oui28/mam.csv",      # MA-M (/28)
    "https://standards-oui.ieee.org/oui36/oui36.csv",    # MA-S (/36)
]
BUNDLED_DB = Path(__file__).parent / "data" / "oui.tsv.gz"
REFRESH_AFTER_S = 30 * 86400


class VendorDB:
    """Prefix → vendor table.

    Starts from the copy bundled with the app (always available, works
    offline), then refreshes from the IEEE in the background once a month.
    """

    def __init__(self, cache_dir: Path):
        self.cache_file = cache_dir / "oui-ieee.tsv.gz"
        self.table: dict[str, str] = {}
        self.source: str | None = None
        self.updated_at: float | None = None
        self.download = {"state": "idle", "error": None, "hint": None}
        self._lock = threading.Lock()

    # ---- loading -----------------------------------------------------------
    def load(self, allow_download: bool = True) -> None:
        for path, label in ((self.cache_file, "IEEE registry (downloaded)"), (BUNDLED_DB, "built-in list")):
            try:
                self._load_tsv(gzip.decompress(path.read_bytes()).decode("utf-8"))
                self.source = label
                self.updated_at = path.stat().st_mtime
                break
            except (OSError, ValueError, EOFError):
                continue
        stale = (self.source != "IEEE registry (downloaded)"
                 or time.time() - (self.updated_at or 0) > REFRESH_AFTER_S)
        if allow_download and stale:
            self.start_download()

    def _load_tsv(self, text: str) -> None:
        table = {}
        for line in text.splitlines():
            if line and not line.startswith("#"):
                prefix, _, name = line.partition("\t")
                if name:
                    table[prefix] = name
        if not table:
            raise ValueError("empty vendor table")
        with self._lock:
            self.table = table

    # ---- downloading -------------------------------------------------------
    def start_download(self) -> bool:
        with self._lock:
            if self.download["state"] == "downloading":
                return False
            self.download = {"state": "downloading", "error": None, "hint": None}
        threading.Thread(target=self._download, daemon=True).start()
        return True

    def _download(self) -> None:
        try:
            table: dict[str, str] = {}
            for url in IEEE_CSV_URLS:
                req = urllib.request.Request(url, headers={"User-Agent": "network-view"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    table.update(parse_ieee_csv(resp.read().decode("utf-8", errors="replace")))
            if len(table) < 10000:
                raise ValueError(f"only {len(table)} entries; the IEEE site may have changed format")
        except Exception as e:
            error, hint = describe_download_error(e)
            with self._lock:
                self.download = {"state": "failed", "error": error, "hint": hint}
            return
        body = "\n".join(f"{k}\t{v}" for k, v in sorted(table.items()))
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_bytes(gzip.compress(body.encode("utf-8")))
        except OSError:
            pass  # still use it for this run
        with self._lock:
            self.table = table
            self.source = "IEEE registry (downloaded)"
            self.updated_at = time.time()
            self.download = {"state": "done", "error": None, "hint": None}

    # ---- lookup ------------------------------------------------------------
    def lookup(self, mac: str | None) -> str | None:
        if not mac:
            return None
        hexmac = mac.replace(":", "").lower()
        with self._lock:
            # Longest match first: MA-S (36 bit), MA-M (28 bit), MA-L (24 bit).
            for n in (9, 7, 6):
                if name := self.table.get(hexmac[:n]):
                    return name
        return None

    def status(self) -> dict:
        with self._lock:
            downloaded = self.source == "IEEE registry (downloaded)"
            return {"source": self.source, "entries": len(self.table),
                    "updated_at": self.updated_at if downloaded else None,
                    "download": dict(self.download)}


def parse_ieee_csv(text: str) -> dict[str, str]:
    table = {}
    for row in csv.DictReader(io.StringIO(text)):
        prefix = (row.get("Assignment") or "").strip().lower()
        name = (row.get("Organization Name") or "").strip()
        if prefix and name and re.fullmatch(r"[0-9a-f]{6,9}", prefix):
            table[prefix] = name
    return table


def describe_download_error(e: Exception) -> tuple[str, str | None]:
    """Turn a download exception into a message and, where we know one, a fix."""
    reason = getattr(e, "reason", None)
    if isinstance(e, ssl.SSLCertVerificationError) or isinstance(reason, ssl.SSLCertVerificationError):
        return ("Couldn't verify the IEEE website's security certificate.",
                "On macOS with Python from python.org, run “Install Certificates.command” in your "
                "Applications/Python 3.x folder, then click Retry. The built-in vendor list is used meanwhile.")
    if isinstance(e, urllib.error.HTTPError):
        return f"The IEEE website answered HTTP {e.code}.", "Try again later."
    if isinstance(e, (urllib.error.URLError, OSError)):
        return (f"Couldn't reach the IEEE website ({reason or e}).",
                "Check your internet connection or firewall, then click Retry.")
    return str(e), None


# --------------------------------------------------------------------------
# Device type guessing
# --------------------------------------------------------------------------

# Strongest signal: what the device itself advertises over mDNS.
_SERVICE_HINTS = [
    ({"ipp", "ipps", "printer", "pdl-datastream", "uscan", "scanner"}, "Printer"),
    ({"googlecast", "amzn-wplay", "androidtvremote2", "viziocast", "roku-rcp", "nvstream"}, "TV / Streaming"),
    ({"sonos", "spotify-connect"}, "Speaker"),
    ({"axis-video"}, "Camera"),
    ({"hue", "esphomelib", "shelly", "matter", "matterc", "hap", "homekit", "elg"}, "Smart home"),
    ({"home-assistant", "adisk"}, "NAS / Server"),
]

# Model strings from mDNS TXT records / UPnP descriptions.
_MODEL_HINTS = [
    (r"^iphone|^ipod", "Phone"),
    (r"^ipad", "Tablet"),
    (r"^appletv", "TV / Streaming"),
    (r"^audioaccessory|homepod", "Speaker"),
    (r"^(macbook|imac|macmini|macpro|mac\d|virtualmac)", "Computer"),
    (r"^watch\d", "Smart watch"),
    (r"chromecast|google tv|shield|fire ?tv|roku|bravia|webos", "TV / Streaming"),
    (r"nest (audio|mini)|google home|echo|sonos|bose|denon|yamaha|marantz|harman", "Speaker"),
    (r"nest (hub|cam|doorbell)|camera|cam\b", "Camera"),
    (r"printer|laserjet|officejet|deskjet|envy|pixma|ecotank|workforce|\bhl-|\bmfc-|\bdcp-", "Printer"),
    (r"diskstation|rackstation|qnap|truenas|unraid", "NAS / Server"),
]

# UPnP device types (urn:schemas-upnp-org:device:<type>:1)
_UPNP_HINTS = {
    "InternetGatewayDevice": "Router", "WFADevice": "Network gear", "WLANAccessPointDevice": "Network gear",
    "Printer": "Printer", "MediaServer": "NAS / Server", "MediaRenderer": "TV / Streaming",
    "ZonePlayer": "Speaker", "dial": "TV / Streaming", "tvdevice": "TV / Streaming",
    "Basic": None, "DigitalSecurityCamera": "Camera",
}

# Keywords matched against vendor + hostname + friendly name (at word starts).
_TYPE_HINTS = [
    ("Phone", ["iphone", "android", "galaxy", "pixel", "oneplus", "xiaomi", "redmi", "oppo",
               "motorola", "moto g", "realme", "vivo", "honor"]),
    ("Tablet", ["ipad", "tablet", "kindle", "fire hd"]),
    ("Smart watch", ["watch", "fitbit", "garmin"]),
    ("TV / Streaming", ["roku", "chromecast", "appletv", "apple-tv", "apple tv", "firetv", "fire-tv", "fire tv",
                        "vizio", "tcl", "lg electronics", "lgwebos", "hisense", "bravia", "shield", "tv",
                        "nvidia", "sharp", "funai", "skyworth", "tivo"]),
    ("Speaker", ["sonos", "bose", "echo", "homepod", "denon", "marantz", "yamaha", "harman", "bang & olufsen",
                 "nest-audio", "nest-mini", "google-home", "speaker", "soundbar"]),
    ("Game console", ["nintendo", "playstation", "ps4", "ps5", "xbox", "sony interactive", "steamdeck",
                      "steam deck", "valve"]),
    ("Printer", ["printer", "epson", "brother", "canon", "hewlett", "hp ", "lexmark", "kyocera", "xerox",
                 "ricoh", "konica", "brn", "brw", "npi", "officejet", "laserjet"]),
    ("Camera", ["camera", "cam-", "ipcam", "hikvision", "dahua", "reolink", "wyze", "arlo", "ring llc",
                "amcrest", "axis comm", "eufy", "blink", "foscam", "doorbell"]),
    ("Smart home", ["espressif", "esp_", "esp-", "esp32", "esp8266", "tuya", "shelly", "tasmota", "wled",
                    "philips lighting", "signify", "hue", "nest", "ecobee", "kasa", "lifx", "wemo",
                    "belkin", "ikea", "sonoff", "itead", "meross", "govee", "lutron", "ecovacs", "irobot",
                    "roborock", "dreame", "switchbot", "aqara", "lumi united", "smartthings", "tado",
                    "netatmo", "myq", "chamberlain", "rachio", "ezviz", "tplink-plug", "hs100", "hs110",
                    "kp115", "ep10", "thermostat", "plug", "bulb", "light", "switch-", "smart"]),
    ("Network gear", ["router", "gateway", "ubiquiti", "unifi", "netgear", "tp-link", "asus", "linksys",
                      "mikrotik", "cisco", "aruba", "eero", "orbi", "fritz", "avm", "arris", "technicolor",
                      "sagemcom", "zyxel", "d-link", "tenda", "nokia solutions",
                      "plume", "google wifi", "access point", "extender", "mesh", "switch"]),
    ("NAS / Server", ["synology", "qnap", "nas", "server", "proxmox", "truenas", "unraid", "raspberry",
                      "pihole", "pi-hole", "homeassistant", "home-assistant", "western digital", "buffalo",
                      "asustor", "terramaster", "docker", "vmware", "virtualbox", "pcs systemtechnik"]),
    ("Computer", ["macbook", "imac", "mac-mini", "macmini", "mac-studio", "desktop", "laptop", "pc",
                  "workstation", "dell", "lenovo", "intel corp", "micro-star", "gigabyte", "asrock",
                  "microsoft", "surface", "thinkpad", "hp-", "acer", "framework", "liteon", "azurewave",
                  "rivet", "killer", "chromebook"]),
]

_PORT_HINTS = [
    ({9100, 631, 515}, "Printer"),
    ({554}, "Camera"),
    ({8008, 8009}, "TV / Streaming"),
    ({62078}, "Phone"),
    ({32400}, "NAS / Server"),
    ({8123, 1883}, "Smart home"),
    ({3389, 445, 139, 548}, "Computer"),
]


def _keyword_match(text: str, words: list[str]) -> bool:
    # Match at word starts so e.g. "ring" doesn't hit "Engineering".
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w), text) for w in words)


def guess_type(vendor: str | None, hostname: str | None, open_ports: list[int] | None,
               is_gateway: bool = False, is_self: bool = False, randomized_mac: bool = False,
               services: list[str] | None = None, model: str | None = None,
               friendly_name: str | None = None, upnp_types: list[str] | None = None) -> str:
    """Pick the most likely device type, strongest evidence first."""
    if is_self:
        return "This computer"
    if is_gateway:
        return "Router"
    svcs = set(services or [])
    for hint, kind in _SERVICE_HINTS:
        if svcs & hint:
            return kind
    model_l = (model or "").lower().replace(" ", "") if model else ""
    if model:
        for pattern, kind in _MODEL_HINTS:
            if re.search(pattern, model.lower()) or re.search(pattern, model_l):
                return kind
    for t in upnp_types or []:
        if kind := _UPNP_HINTS.get(t):
            return kind
    ports = set(open_ports or [])
    for hint_ports, kind in _PORT_HINTS[:6]:  # device-specific ports first
        if ports & hint_ports:
            return kind
    text = " ".join(x for x in (friendly_name, hostname, model) if x).lower()
    vendor_l = (vendor or "").lower()
    # Names the owner/device chose beat the chip maker in the MAC prefix.
    for source in (text, vendor_l):
        if not source:
            continue
        for kind, words in _TYPE_HINTS:
            if _keyword_match(source, words):
                return kind
        if "apple" in source:
            return "Apple device"
    if svcs & {"companion-link", "apple-mobdev2"}:
        return "Apple device"
    if svcs & {"smb", "afpovertcp", "workstation", "rfb", "sftp-ssh"}:
        return "Computer"
    if ports & _PORT_HINTS[6][0]:
        return "Computer"
    if randomized_mac:
        return "Phone / Tablet"  # private Wi-Fi addresses are mostly mobile devices
    return "Unknown"
