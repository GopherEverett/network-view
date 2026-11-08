"""Network discovery: find the local subnet, sweep it, and collect device details.

Uses only the Python standard library plus common OS tools (ping, arp, ip,
ifconfig, ipconfig) when they exist, with pure-Python fallbacks otherwise.
Works on Linux, macOS and Windows without root/admin rights.
"""

from __future__ import annotations

import concurrent.futures as cf
import ipaddress
import platform
import re
import shutil
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field

from .discovery import HostInfo, discover

SYSTEM = platform.system()  # "Linux", "Darwin", "Windows"
MAX_HOSTS = 1024  # never sweep more than a /22, even on huge subnets

COMMON_PORTS = {
    21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 80: "HTTP",
    110: "POP3", 139: "NetBIOS", 143: "IMAP", 443: "HTTPS", 445: "SMB",
    515: "LPD", 548: "AFP", 554: "RTSP", 631: "IPP", 1883: "MQTT",
    1900: "UPnP", 3000: "HTTP-alt", 3306: "MySQL", 3389: "RDP",
    5000: "UPnP/HTTP", 5353: "mDNS", 5432: "PostgreSQL", 5900: "VNC",
    7000: "AirPlay", 8000: "HTTP-alt", 8008: "Chromecast", 8009: "Chromecast",
    8080: "HTTP-proxy", 8443: "HTTPS-alt", 8123: "Home Assistant",
    9100: "Printer (RAW)", 32400: "Plex", 49152: "UPnP", 62078: "iPhone sync",
}
# Checked on every online device during each scan: each one says a lot about
# what kind of device it is (printer, camera, Chromecast, iPhone, ...).
QUICK_PORTS = (22, 80, 443, 445, 548, 554, 631, 1883, 3389, 7000, 8008, 8009,
               8123, 9100, 32400, 62078)
# Ports used to detect live hosts when ICMP ping is unavailable or blocked.
PROBE_PORTS = (80, 443, 22, 445, 139, 62078, 8080)

_MAC_RE = re.compile(r"([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}")
_PING_TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


@dataclass
class Network:
    interface: str | None
    local_ip: str
    network: ipaddress.IPv4Network
    gateway: str | None = None
    local_mac: str | None = None

    def to_dict(self) -> dict:
        return {
            "interface": self.interface,
            "local_ip": self.local_ip,
            "cidr": str(self.network),
            "gateway": self.gateway,
            "local_mac": self.local_mac,
            "host_count": self.network.num_addresses - 2 if self.network.prefixlen < 31 else self.network.num_addresses,
        }


@dataclass
class Device:
    ip: str
    mac: str | None = None
    hostname: str | None = None
    latency_ms: float | None = None
    discovered_by: set[str] = field(default_factory=set)
    quick_ports: list[int] | None = None
    info: "HostInfo | None" = None  # what the device announced over mDNS / UPnP


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 5) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout + out.stderr
    except (OSError, subprocess.SubprocessError):
        return ""


def normalize_mac(mac: str | None) -> str | None:
    """Return a MAC as lowercase aa:bb:cc:dd:ee:ff, or None if invalid/empty."""
    if not mac:
        return None
    parts = re.split(r"[:-]", mac.strip())
    if len(parts) != 6:
        return None
    try:
        octets = [int(p, 16) for p in parts]
    except ValueError:
        return None
    if all(o == 0 for o in octets) or all(o == 0xFF for o in octets):
        return None
    return ":".join(f"{o:02x}" for o in octets)


def is_randomized_mac(mac: str | None) -> bool:
    """Locally administered bit set -> usually a privacy/random MAC (phones)."""
    if not mac:
        return False
    return bool(int(mac.split(":")[0], 16) & 0x02)


def raise_file_limit(target: int = 4096) -> None:
    """Parallel probing opens many sockets/pipes; macOS defaults to only 256."""
    try:
        import resource
    except ImportError:  # Windows
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ValueError, OSError):
        pass


def get_local_ip() -> str:
    """IP of the interface used for the default route (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------
# Subnet / gateway detection
# --------------------------------------------------------------------------

def parse_ip_addr(output: str, local_ip: str) -> tuple[str | None, int | None]:
    """Parse `ip -o -4 addr show` → (interface, prefixlen) for local_ip."""
    for line in output.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/(\d+)", line)
        if m and m.group(2) == local_ip:
            return m.group(1), int(m.group(3))
    return None, None


def parse_ifconfig(output: str, local_ip: str) -> tuple[str | None, int | None]:
    """Parse macOS/BSD/Linux `ifconfig` output → (interface, prefixlen)."""
    iface = None
    for line in output.splitlines():
        hm = re.match(r"^(\S+?):?\s", line)
        if hm and not line.startswith((" ", "\t")):
            iface = hm.group(1).rstrip(":")
        if local_ip not in line:
            continue
        m = re.search(r"netmask\s+(0x[0-9a-fA-F]+|[\d.]+)", line) or re.search(r"Mask:([\d.]+)", line)
        if m:
            mask = m.group(1)
            if mask.startswith("0x"):
                mask = socket.inet_ntoa(struct.pack(">I", int(mask, 16)))
            return iface, ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    return iface, None


def parse_ipconfig(output: str, local_ip: str) -> tuple[str | None, int | None, str | None]:
    """Parse Windows `ipconfig` → (adapter, prefixlen, gateway) for local_ip."""
    adapter = None
    found = False
    prefix = gateway = None
    for line in output.splitlines():
        if line and not line.startswith(" ") and line.rstrip().endswith(":"):
            if found:
                break
            adapter = line.rstrip(": ").strip()
            continue
        if "IPv4" in line and local_ip in line:
            found = True
        elif found and "Subnet Mask" in line:
            m = re.search(r"([\d.]+)\s*$", line)
            if m:
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{m.group(1)}").prefixlen
        elif found and "Default Gateway" in line:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s*$", line)
            if m:
                gateway = m.group(1)
    return (adapter if found else None), prefix, gateway


def parse_proc_net_route(text: str, local_ip: str | None = None) -> tuple[str | None, int | None, str | None]:
    """Parse Linux /proc/net/route → (iface, prefixlen of local subnet, gateway)."""
    gateway = gw_iface = None
    subnets = []  # (iface, network)
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        iface, dest, gw, mask = cols[0], cols[1], cols[2], cols[7]
        dest_ip = socket.inet_ntoa(struct.pack("<I", int(dest, 16)))
        gw_ip = socket.inet_ntoa(struct.pack("<I", int(gw, 16)))
        mask_ip = socket.inet_ntoa(struct.pack("<I", int(mask, 16)))
        if dest_ip == "0.0.0.0" and mask_ip == "0.0.0.0":
            gateway, gw_iface = gw_ip, iface
        elif gw_ip == "0.0.0.0":
            subnets.append((iface, ipaddress.IPv4Network(f"{dest_ip}/{mask_ip}", strict=False)))
    if local_ip:
        addr = ipaddress.IPv4Address(local_ip)
        matches = [s for s in subnets if addr in s[1]]
        if matches:
            iface, net = max(matches, key=lambda s: s[1].prefixlen)
            return iface, net.prefixlen, gateway
    return gw_iface, None, gateway


def detect_network(cidr_override: str | None = None) -> Network:
    local_ip = get_local_ip()
    iface = prefix = gateway = None

    if SYSTEM == "Windows":
        iface, prefix, gateway = parse_ipconfig(_run(["ipconfig"]), local_ip)
    else:
        if SYSTEM == "Linux":
            try:
                with open("/proc/net/route") as f:
                    iface, prefix, gateway = parse_proc_net_route(f.read(), local_ip)
            except OSError:
                pass
        if prefix is None and shutil.which("ip"):
            iface, prefix = parse_ip_addr(_run(["ip", "-o", "-4", "addr", "show"]), local_ip)
        if prefix is None and shutil.which("ifconfig"):
            iface, prefix = parse_ifconfig(_run(["ifconfig"]), local_ip)
        if gateway is None and SYSTEM == "Darwin":
            m = re.search(r"gateway:\s*([\d.]+)", _run(["route", "-n", "get", "default"]))
            gateway = m.group(1) if m else None

    if cidr_override:
        net = ipaddress.IPv4Network(cidr_override, strict=False)
    else:
        net = ipaddress.IPv4Network(f"{local_ip}/{prefix or 24}", strict=False)
    return Network(iface, local_ip, net, gateway, get_local_mac(iface))


def get_local_mac(iface: str | None) -> str | None:
    if iface and SYSTEM == "Linux":
        try:
            with open(f"/sys/class/net/{iface}/address") as f:
                return normalize_mac(f.read())
        except OSError:
            pass
    import uuid
    node = uuid.getnode()
    if (node >> 40) & 0x01:  # multicast bit → uuid made up a random number
        return None
    return normalize_mac(":".join(f"{(node >> s) & 0xFF:02x}" for s in range(40, -1, -8)))


def hosts_to_scan(net: Network) -> list[str]:
    """All host IPs in the subnet, capped to the MAX_HOSTS closest to us."""
    if net.network.num_addresses <= MAX_HOSTS + 2:
        return [str(h) for h in net.network.hosts()]
    # Very large subnet: scan the /22 that contains our own address.
    sub = ipaddress.IPv4Network(f"{net.local_ip}/22", strict=False)
    return [str(h) for h in sub.hosts()]


# --------------------------------------------------------------------------
# Liveness probing
# --------------------------------------------------------------------------

_PING = shutil.which("ping")


def ping(ip: str, timeout_s: float = 1.0) -> float | None:
    """ICMP ping via the OS tool. Returns latency in ms, or None."""
    if not _PING:
        return None
    if SYSTEM == "Windows":
        cmd = [_PING, "-n", "1", "-w", str(int(timeout_s * 1000)), ip]
    elif SYSTEM == "Darwin":
        cmd = [_PING, "-c", "1", "-W", str(int(timeout_s * 1000)), ip]
    else:
        cmd = [_PING, "-c", "1", "-W", str(max(1, int(timeout_s))), ip]
    start = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 2,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    # Windows returns 0 for "Destination host unreachable" replies; require TTL.
    if r.returncode != 0 or "TTL=" not in r.stdout.upper():
        return None
    m = _PING_TIME_RE.search(r.stdout)
    return float(m.group(1)) if m else round((time.perf_counter() - start) * 1000, 1)


def tcp_probe(ip: str, ports=PROBE_PORTS, timeout_s: float = 0.4) -> float | None:
    """A host is alive if any port accepts or actively refuses a connection."""
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout_s)
        start = time.perf_counter()
        try:
            code = s.connect_ex((ip, port))
        except OSError:
            code = -1
        finally:
            s.close()
        # 0 = open; ECONNREFUSED (111 Linux, 61 macOS, 10061 Windows) = host answered
        if code in (0, 111, 61, 10061):
            return round((time.perf_counter() - start) * 1000, 1)
    return None


def nudge_arp(ip: str) -> None:
    """Send one UDP datagram so the OS resolves (and caches) the host's MAC."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"", (ip, 9))  # discard port
    except OSError:
        pass


def probe_host(ip: str) -> tuple[str, float | None, str | None]:
    nudge_arp(ip)
    latency = ping(ip)
    if latency is not None:
        return ip, latency, "ping"
    latency = tcp_probe(ip)
    if latency is not None:
        return ip, latency, "tcp"
    return ip, None, None


# --------------------------------------------------------------------------
# ARP / neighbour table
# --------------------------------------------------------------------------

def parse_proc_net_arp(text: str) -> dict[str, str]:
    table = {}
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 4 and cols[2] != "0x0":
            mac = normalize_mac(cols[3])
            if mac:
                table[cols[0]] = mac
    return table


def parse_arp_a(text: str) -> dict[str, str]:
    """Parse `arp -a` output from macOS/BSD/Linux-net-tools/Windows."""
    table = {}
    for line in text.splitlines():
        ip_m = re.search(r"\(?(\d+\.\d+\.\d+\.\d+)\)?", line)
        mac_m = _MAC_RE.search(line)
        if ip_m and mac_m:
            mac = normalize_mac(mac_m.group(0))
            if mac:
                table[ip_m.group(1)] = mac
    return table


def parse_ip_neigh(text: str) -> dict[str, str]:
    table = {}
    for line in text.splitlines():
        m = re.match(r"^(\d+\.\d+\.\d+\.\d+)\s.*lladdr\s+(\S+)", line)
        if m and "FAILED" not in line and "INCOMPLETE" not in line:
            mac = normalize_mac(m.group(2))
            if mac:
                table[m.group(1)] = mac
    return table


def read_arp_table() -> dict[str, str]:
    table: dict[str, str] = {}
    if SYSTEM == "Linux":
        try:
            with open("/proc/net/arp") as f:
                table.update(parse_proc_net_arp(f.read()))
        except OSError:
            pass
        if shutil.which("ip"):
            table.update(parse_ip_neigh(_run(["ip", "neigh", "show"])))
    if shutil.which("arp"):
        table.update(parse_arp_a(_run(["arp", "-an"] if SYSTEM != "Windows" else ["arp", "-a"], timeout=10)))
    return table


# --------------------------------------------------------------------------
# Names and ports
# --------------------------------------------------------------------------

def resolve_hostname(ip: str) -> str | None:
    try:
        name = socket.gethostbyaddr(ip)[0]
        return name if name and name != ip else None
    except (OSError, UnicodeError):
        return None


def netbios_name(ip: str, timeout_s: float = 0.6) -> str | None:
    """Ask a host for its NetBIOS name (Windows PCs, NAS, Samba)."""
    # NBSTAT query for the wildcard name "*"
    packet = (b"\x13\x37\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x20"
              + b"CK" + b"A" * 30 + b"\x00\x00\x21\x00\x01")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout_s)
            s.sendto(packet, (ip, 137))
            data, _ = s.recvfrom(1024)
    except OSError:
        return None
    try:
        count = data[56]
        for i in range(count):
            entry = data[57 + i * 18: 57 + (i + 1) * 18]
            name, suffix, flags = entry[:15], entry[15], entry[16]
            if suffix == 0x00 and not flags & 0x80:  # unique workstation name
                return name.decode("ascii", "ignore").strip() or None
    except (IndexError, ValueError):
        pass
    return None


def scan_ports(ip: str, ports=None, timeout_s: float = 0.5, workers: int = 32) -> list[int]:
    ports = list(ports or COMMON_PORTS)

    def check(port: int) -> int | None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            try:
                return port if s.connect_ex((ip, port)) == 0 else None
            except OSError:
                return None

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        return sorted(p for p in pool.map(check, ports) if p is not None)


# --------------------------------------------------------------------------
# Full scan
# --------------------------------------------------------------------------

def scan(net: Network, progress=None, workers: int = 128) -> list[Device]:
    """Sweep the subnet and return every device found."""
    ips = hosts_to_scan(net)
    devices: dict[str, Device] = {}
    done = 0

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for ip, latency, method in pool.map(probe_host, ips):
            done += 1
            if method:
                devices[ip] = Device(ip=ip, latency_ms=latency, discovered_by={method})
            if progress and done % 8 == 0:
                progress("probing", done, len(ips))

    # Hosts that block ICMP and TCP still answer ARP, so the table finds them.
    in_subnet = {ip for ip in ips}
    for ip, mac in read_arp_table().items():
        if ip in in_subnet or ip == net.gateway:
            dev = devices.setdefault(ip, Device(ip=ip))
            dev.mac = mac
            dev.discovered_by.add("arp")

    # This computer
    me = devices.setdefault(net.local_ip, Device(ip=net.local_ip, latency_ms=0.0))
    me.mac = me.mac or net.local_mac
    me.hostname = socket.gethostname()
    me.discovered_by.add("self")

    if progress:
        progress("identifying devices", 0, len(devices))

    def name_of(dev: Device):
        if dev.hostname:
            return dev.ip, dev.hostname
        return dev.ip, resolve_hostname(dev.ip) or netbios_name(dev.ip)

    def quick_ports(dev: Device):
        if dev.ip == net.local_ip:
            return dev.ip, None
        return dev.ip, scan_ports(dev.ip, QUICK_PORTS, timeout_s=0.6, workers=len(QUICK_PORTS))

    # Names, mDNS/UPnP and the quick port check all wait on the network, so
    # run them side by side. Pool sizes keep open sockets well under macOS's
    # default 256-file limit (12 devices x 16 ports at a time).
    with cf.ThreadPoolExecutor(max_workers=1) as disc_pool, \
            cf.ThreadPoolExecutor(max_workers=24) as name_pool, \
            cf.ThreadPoolExecutor(max_workers=12) as port_pool:
        announced_f = disc_pool.submit(discover, list(devices), net.network, net.local_ip)
        names = name_pool.map(name_of, list(devices.values()))
        ports = port_pool.map(quick_ports, list(devices.values()))
        for ip, name in names:
            devices[ip].hostname = name
        for ip, open_ports in ports:
            devices[ip].quick_ports = open_ports
        try:
            announced = announced_f.result()
        except Exception:
            announced = {}

    arp = None
    for ip, info in announced.items():
        dev = devices.get(ip)
        if dev is None and ipaddress.IPv4Address(ip) in net.network:
            # Answered mDNS/SSDP but nothing else (rare, e.g. strict firewalls).
            arp = arp if arp is not None else read_arp_table()
            dev = devices[ip] = Device(ip=ip, mac=arp.get(ip))
        if dev is not None:
            dev.info = info
            dev.discovered_by |= info.sources
            dev.hostname = dev.hostname or info.mdns_name

    return sorted(devices.values(), key=lambda d: ipaddress.IPv4Address(d.ip))
