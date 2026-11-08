"""Ask devices to describe themselves: mDNS/Bonjour and SSDP/UPnP.

Most phones, TVs, speakers, printers and smart-home gear announce a friendly
name and model over one of these protocols even when DNS knows nothing about
them. Standard library only, no root needed:

* mDNS queries are sent from an ordinary (non-5353) port, which makes them
  "legacy unicast" queries (RFC 6762 §6.7): responders answer straight back to
  us, so we don't fight the OS's own mDNS daemon for port 5353. We also try to
  listen on 5353 for multicast answers, but carry on if that port is taken.
* SSDP M-SEARCH replies arrive by unicast; each one points at an XML
  description that we fetch (only from addresses inside the scanned subnet).
"""

from __future__ import annotations

import ipaddress
import random
import re
import select
import socket
import struct
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlparse

MDNS_ADDR, MDNS_PORT = "224.0.0.251", 5353
SSDP_ADDR, SSDP_PORT = "239.255.255.250", 1900

# Service types worth asking for directly: many devices don't answer the
# generic "list all services" query.
MDNS_SERVICES = [
    "_services._dns-sd._udp", "_device-info._tcp", "_googlecast._tcp", "_airplay._tcp",
    "_raop._tcp", "_companion-link._tcp", "_apple-mobdev2._tcp", "_ipp._tcp", "_ipps._tcp",
    "_printer._tcp", "_pdl-datastream._tcp", "_uscan._tcp", "_scanner._tcp", "_hap._tcp",
    "_homekit._tcp", "_matter._tcp", "_matterc._udp", "_hue._tcp", "_esphomelib._tcp",
    "_home-assistant._tcp", "_spotify-connect._tcp", "_sonos._tcp", "_amzn-wplay._tcp",
    "_androidtvremote2._tcp", "_smb._tcp", "_afpovertcp._tcp", "_workstation._tcp",
    "_ssh._tcp", "_sftp-ssh._tcp", "_rfb._tcp", "_http._tcp", "_axis-video._tcp",
    "_rtsp._tcp", "_daap._tcp", "_nvstream._tcp", "_adisk._tcp", "_shelly._tcp",
    "_elg._tcp", "_viziocast._tcp", "_roku-rcp._tcp",
]

TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV = 1, 12, 16, 33


@dataclass
class HostInfo:
    """What a device told us about itself."""
    mdns_name: str | None = None      # e.g. "Johns-iPhone" (from Johns-iPhone.local)
    friendly_name: str | None = None  # e.g. "Living Room TV"
    model: str | None = None          # e.g. "Chromecast Ultra", "iPhone14,2"
    manufacturer: str | None = None
    services: set[str] = field(default_factory=set)  # e.g. {"googlecast", "airplay"}
    upnp_types: set[str] = field(default_factory=set)  # e.g. {"MediaRenderer"}
    sources: set[str] = field(default_factory=set)  # {"mdns", "upnp"}
    named_by_txt: bool = False  # friendly_name came from an explicit TXT name field

    def merge(self, other: "HostInfo") -> None:
        if other.named_by_txt and not self.named_by_txt:
            self.friendly_name, self.named_by_txt = other.friendly_name, True
        for attr in ("mdns_name", "friendly_name", "model", "manufacturer"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
        self.services |= other.services
        self.upnp_types |= other.upnp_types
        self.sources |= other.sources


# --------------------------------------------------------------------------
# DNS wire format
# --------------------------------------------------------------------------

def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")[:63]
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(names: list[tuple[str, int]], qid: int = 0) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0, len(names), 0, 0, 0)
    body = b"".join(encode_name(n) + struct.pack(">HH", t, 1) for n, t in names)
    return header + body


def _read_name(data: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end, hops = [], False, off, 0
    while True:
        if off >= len(data):
            raise ValueError("name runs past end of packet")
        length = data[off]
        if length & 0xC0 == 0xC0:  # compression pointer
            if off + 1 >= len(data):
                raise ValueError("truncated pointer")
            if not jumped:
                end = off + 2
            off = ((length & 0x3F) << 8) | data[off + 1]
            jumped = True
            hops += 1
            if hops > 20:
                raise ValueError("pointer loop")
            continue
        off += 1
        if length == 0:
            break
        labels.append(data[off:off + length].decode("utf-8", "replace"))
        off += length
    return ".".join(labels), (end if jumped else off)


def parse_dns(data: bytes) -> list[dict]:
    """Return every resource record (answer, authority, additional) in a packet."""
    if len(data) < 12:
        return []
    _, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    if not flags & 0x8000:  # not a response
        return []
    off = 12
    records = []
    try:
        for _ in range(qd):
            _, off = _read_name(data, off)
            off += 4
        for _ in range(an + ns + ar):
            name, off = _read_name(data, off)
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
            off += 10
            rdata_off, off = off, off + rdlen
            if off > len(data):
                break
            rec = {"name": name, "type": rtype}
            if rtype == TYPE_A and rdlen == 4:
                rec["value"] = socket.inet_ntoa(data[rdata_off:off])
            elif rtype == TYPE_PTR:
                rec["value"] = _read_name(data, rdata_off)[0]
            elif rtype == TYPE_SRV:
                rec["value"] = _read_name(data, rdata_off + 6)[0]
            elif rtype == TYPE_TXT:
                txt, p = {}, rdata_off
                while p < off:
                    ln = data[p]
                    item = data[p + 1:p + 1 + ln].decode("utf-8", "replace")
                    p += 1 + ln
                    if "=" in item:
                        k, v = item.split("=", 1)
                        txt[k.lower()] = v
                rec["value"] = txt
            else:
                continue
            records.append(rec)
    except (ValueError, struct.error, IndexError):
        pass
    return records


def reverse_name(ip: str) -> str:
    return ".".join(reversed(ip.split("."))) + ".in-addr.arpa"


# --------------------------------------------------------------------------
# mDNS
# --------------------------------------------------------------------------

def _service_type(instance: str) -> tuple[str | None, str | None]:
    """'Living Room TV._googlecast._tcp.local' → ('Living Room TV', 'googlecast')."""
    m = re.match(r"^(.*?)\._([A-Za-z0-9-]+)\._(?:tcp|udp)\.local$", instance)
    return (m.group(1), m.group(2).lower()) if m else (None, None)


def _clean_instance(label: str | None) -> str | None:
    if not label:
        return None
    label = label.replace("\\032", " ").replace("\\.", ".").strip()
    # "Living Room@Johns-MacBook" (RAOP uses "MAC@name")
    if "@" in label:
        label = label.split("@", 1)[1]
    # Strip trailing hex IDs some devices append: "Chromecast-1a2b3c4d..."
    label = re.sub(r"[-_ ]?[0-9a-fA-F]{12,}$", "", label)
    # Strip the "[aa:bb:cc:dd:ee:ff]" suffix some devices append
    label = re.sub(r"\s*\[[0-9a-fA-F:]{17}\]$", "", label)
    return label or None


# TXT keys that carry a model / friendly name, per service
_MODEL_KEYS = ("md", "model", "ty", "usb_mdl", "product", "am", "rpmd", "modelname")
_NAME_KEYS = ("fn", "n", "name", "friendly_name")


def interpret_mdns(records: list[dict], source_ip: str, hosts: dict[str, HostInfo]) -> None:
    """Fold one response's records into per-IP HostInfo entries."""
    a_records = {r["name"].lower(): r["value"] for r in records if r["type"] == TYPE_A}
    srv_targets = {r["name"]: r["value"] for r in records if r["type"] == TYPE_SRV}

    def info_for(hostname: str | None) -> HostInfo:
        ip = a_records.get((hostname or "").lower(), source_ip)
        return hosts.setdefault(ip, HostInfo())

    for name, target in srv_targets.items():
        info = info_for(target)
        if target.endswith(".local") and not info.mdns_name:
            info.mdns_name = target[:-len(".local")]

    for host, ip in a_records.items():
        if host.endswith(".local"):
            h = hosts.setdefault(ip, HostInfo())
            h.mdns_name = h.mdns_name or host[:-len(".local")]

    for rec in records:
        if rec["type"] == TYPE_PTR and rec["name"].endswith(".in-addr.arpa"):
            ip = ".".join(reversed(rec["name"][:-len(".in-addr.arpa")].split(".")))
            if rec["value"].endswith(".local"):
                h = hosts.setdefault(ip, HostInfo())
                h.mdns_name = h.mdns_name or rec["value"][:-len(".local")]
            continue
        instance = rec["value"] if rec["type"] == TYPE_PTR else rec["name"]
        label, svc = _service_type(instance)
        if not svc:
            continue
        info = info_for(srv_targets.get(instance))
        info.services.add(svc)
        if rec["type"] == TYPE_TXT:
            txt = rec["value"]
            model = next((txt[k] for k in _MODEL_KEYS if txt.get(k)), None)
            if model and not info.model:
                info.model = model.strip()
            fname = next((txt[k] for k in _NAME_KEYS if txt.get(k)), None)
            if fname and svc in ("googlecast", "amzn-wplay", "androidtvremote2", "viziocast"):
                info.friendly_name = fname.strip()  # beats the generic instance label
                info.named_by_txt = True
            if txt.get("manufacturer") or txt.get("usb_mfg"):
                info.manufacturer = info.manufacturer or (txt.get("manufacturer") or txt.get("usb_mfg"))
        # Service instance names are usually what the owner called the device.
        if (svc not in ("device-info", "workstation", "services", "sleep-proxy")
                and not info.friendly_name):
            info.friendly_name = _clean_instance(label)


def _mdns_listener() -> socket.socket | None:
    """Best effort: join the mDNS group on 5353 to hear multicast answers."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("", MDNS_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(MDNS_ADDR), socket.inet_aton("0.0.0.0"))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setblocking(False)
        return s
    except OSError:
        return None


def mdns_discover(target_ips: list[str], subnet: ipaddress.IPv4Network,
                  local_ip: str, timeout_s: float = 3.0) -> dict[str, HostInfo]:
    hosts: dict[str, HostInfo] = {}
    query = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        query.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        try:
            query.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        except OSError:
            pass
        query.bind((local_ip if local_ip != "127.0.0.1" else "", 0))
        query.setblocking(False)
    except OSError:
        query.close()
        return hosts
    listener = _mdns_listener()
    socks = [query] + ([listener] if listener else [])

    qid = random.randint(1, 0xFFFF)
    services = [(f"{s}.local", TYPE_PTR) for s in MDNS_SERVICES]
    try:
        # Multicast service discovery, a few types per packet.
        for i in range(0, len(services), 8):
            query.sendto(build_query(services[i:i + 8], qid), (MDNS_ADDR, MDNS_PORT))
        # Unicast to each live host: "what's your name?" and "what do you run?"
        for ip in target_ips:
            if ip == local_ip:
                continue
            pkt = build_query([(reverse_name(ip), TYPE_PTR), ("_services._dns-sd._udp.local", TYPE_PTR),
                               ("_device-info._tcp.local", TYPE_PTR)], qid)
            try:
                query.sendto(pkt, (ip, MDNS_PORT))
            except OSError:
                pass
    except OSError:
        pass

    deadline = time.monotonic() + timeout_s
    asked_services: set[str] = set()
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            ready, _, _ = select.select(socks, [], [], remaining)
        except (OSError, ValueError):
            break
        for s in ready:
            try:
                data, (src, _port) = s.recvfrom(9000)
            except OSError:
                continue
            try:
                if ipaddress.IPv4Address(src) not in subnet:
                    continue
            except ValueError:
                continue
            records = parse_dns(data)
            interpret_mdns(records, src, hosts)
            # Follow up on service types we hadn't thought to ask about.
            new = {r["value"] for r in records if r["type"] == TYPE_PTR
                   and r["name"] == "_services._dns-sd._udp.local"} - asked_services
            if new:
                asked_services |= new
                try:
                    query.sendto(build_query([(n, TYPE_PTR) for n in list(new)[:12]], qid), (src, MDNS_PORT))
                except OSError:
                    pass
    for s in socks:
        s.close()
    hosts.pop(local_ip, None)
    for h in hosts.values():
        h.sources.add("mdns")
    return hosts


# --------------------------------------------------------------------------
# SSDP / UPnP
# --------------------------------------------------------------------------

def parse_ssdp_response(data: bytes) -> dict[str, str]:
    headers = {}
    for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers


def parse_upnp_description(xml_text: str) -> HostInfo:
    info = HostInfo()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return info
    # Ignore XML namespaces: match on local tag names.
    def find(tag: str) -> str | None:
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == tag and el.text and el.text.strip():
                return el.text.strip()
        return None
    info.friendly_name = find("friendlyName")
    info.manufacturer = find("manufacturer")
    model_name, model_num = find("modelName"), find("modelNumber")
    if model_name:
        info.model = model_name if not model_num or model_num in model_name else f"{model_name} {model_num}"
    dtype = find("deviceType")  # urn:schemas-upnp-org:device:MediaRenderer:1
    if dtype:
        parts = dtype.split(":")
        if len(parts) >= 4:
            info.upnp_types.add(parts[3])
    return info


def _fetch_description(url: str, max_bytes: int = 256 * 1024) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "network-view"})
        # No proxy: these are LAN addresses.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=2.5) as resp:
            return resp.read(max_bytes).decode("utf-8", "replace")
    except Exception:
        return None


def ssdp_discover(subnet: ipaddress.IPv4Network, local_ip: str, timeout_s: float = 3.0) -> dict[str, HostInfo]:
    hosts: dict[str, HostInfo] = {}
    locations: dict[str, set[str]] = {}
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: ssdp:all\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        except OSError:
            pass
        s.bind((local_ip if local_ip != "127.0.0.1" else "", 0))
        s.settimeout(0.3)
        for _ in range(2):
            s.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                data, (src, _port) = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                if ipaddress.IPv4Address(src) not in subnet or src == local_ip:
                    continue
            except ValueError:
                continue
            headers = parse_ssdp_response(data)
            loc = headers.get("location")
            # Only fetch descriptions served by the device that answered.
            if loc and urlparse(loc).hostname == src and urlparse(loc).scheme == "http":
                locations.setdefault(src, set()).add(loc)
            hosts.setdefault(src, HostInfo()).sources.add("upnp")
    except OSError:
        pass
    finally:
        s.close()

    import concurrent.futures as cf

    def describe(item):
        ip, urls = item
        info = HostInfo()
        for url in sorted(urls)[:4]:
            text = _fetch_description(url)
            if text:
                info.merge(parse_upnp_description(text))
        return ip, info

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        for ip, info in pool.map(describe, locations.items()):
            hosts[ip].merge(info)
    return hosts


def discover(target_ips: list[str], subnet: ipaddress.IPv4Network, local_ip: str) -> dict[str, HostInfo]:
    """Run mDNS and SSDP side by side and merge what each device said."""
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        m = pool.submit(mdns_discover, target_ips, subnet, local_ip)
        u = pool.submit(ssdp_discover, subnet, local_ip)
        mdns, ssdp = m.result(), u.result()
    result: dict[str, HostInfo] = {}
    for source in (mdns, ssdp):
        for ip, info in source.items():
            result.setdefault(ip, HostInfo()).merge(info)
    return result
