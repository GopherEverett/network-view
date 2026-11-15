"""macOS integration: run Network View in the background and add an app icon.

    python3 -m netview install [server options]   # e.g. --host 0.0.0.0
    python3 -m netview status
    python3 -m netview uninstall

* A LaunchAgent (~/Library/LaunchAgents/local.network-view.plist) starts the
  server when you log in and restarts it if it stops.
* "Network View.app" in ~/Applications makes sure the service is running and
  opens the dashboard. It's a tiny launcher script, not a second copy.

Nothing here needs admin rights, Xcode, or anything beyond what ships with macOS.
"""

from __future__ import annotations

import argparse
import math
import os
import plistlib
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib
from pathlib import Path

LABEL = "local.network-view"
HOME = Path.home()
PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
APP = HOME / "Applications" / "Network View.app"
LOG = HOME / "Library" / "Logs" / "network-view.log"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Small helpers (kept separate so tests can replace them)
# --------------------------------------------------------------------------

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _server_up(port: int, timeout: float = 1.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/devices", timeout=timeout) as r:
            return r.status == 200 and b'"devices"' in r.read(4096 * 64)
    except Exception:
        return False


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _service_loaded() -> bool:
    return _run(["launchctl", "print", f"{_domain()}/{LABEL}"]).returncode == 0


def _installed_args() -> list[str]:
    """Server options recorded in the installed LaunchAgent."""
    try:
        with open(PLIST, "rb") as f:
            argv = plistlib.load(f)["ProgramArguments"]
        # Leave out the --no-browser the service always adds.
        return [a for a in argv[argv.index("netview") + 1:] if a != "--no-browser"]
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
        return []


def _port_from(server_args: list[str]) -> int:
    from .server import build_parser
    return build_parser().parse_args(server_args).port


# --------------------------------------------------------------------------
# LaunchAgent
# --------------------------------------------------------------------------

def launch_agent_plist(python: str, server_args: list[str]) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "netview", "--no-browser", *server_args],
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": {
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONUNBUFFERED": "1",
            # ping, arp, ifconfig and route live in /sbin and /usr/sbin.
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,  # if it keeps failing, retry every 30 s, not in a tight loop
        "ProcessType": "Standard",  # "Background" would throttle the network and skew speed tests
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }


# --------------------------------------------------------------------------
# App bundle
# --------------------------------------------------------------------------

def launcher_script(port: int) -> str:
    return f"""#!/bin/sh
# Network View launcher: make sure the background service is running, then
# open the dashboard. Created by 'python3 -m netview install'.
URL="http://localhost:{port}/"
up() {{ /usr/bin/curl -fs -m 1 "http://127.0.0.1:{port}/api/devices" -o /dev/null; }}
if ! up; then
  PLIST="$HOME/Library/LaunchAgents/{LABEL}.plist"
  if [ ! -f "$PLIST" ]; then
    /usr/bin/osascript -e 'display alert "Network View isn’t installed" message "Run: python3 -m netview install"'
    exit 1
  fi
  /bin/launchctl bootstrap "gui/$(/usr/bin/id -u)" "$PLIST" 2>/dev/null
  /bin/launchctl kickstart "gui/$(/usr/bin/id -u)/{LABEL}" 2>/dev/null
  i=0
  while [ $i -lt 30 ] && ! up; do sleep 0.5; i=$((i + 1)); done
  if ! up; then
    /usr/bin/osascript -e 'display alert "Network View didn’t start" message "Details are in ~/Library/Logs/network-view.log. Try: python3 -m netview status"'
    exit 1
  fi
fi
/usr/bin/open "$URL"
"""


def app_info_plist() -> dict:
    return {
        "CFBundleName": "Network View",
        "CFBundleDisplayName": "Network View",
        "CFBundleIdentifier": f"{LABEL}.launcher",
        "CFBundleExecutable": "network-view",
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": _version(),
        "CFBundleVersion": _version(),
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": True,  # a launcher: no lingering Dock icon after it opens the browser
        "NSHighResolutionCapable": True,
    }


def _version() -> str:
    from . import __version__
    return __version__


def build_app(port: int, dest: Path | None = None) -> bool:
    """Create the app bundle. Returns whether a custom icon was added."""
    dest = dest or APP
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "Contents" / "MacOS").mkdir(parents=True)
    (tmp / "Contents" / "Resources").mkdir()
    with open(tmp / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(app_info_plist(), f)
    exe = tmp / "Contents" / "MacOS" / "network-view"
    exe.write_text(launcher_script(port))
    exe.chmod(0o755)
    has_icon = make_icns(tmp / "Contents" / "Resources" / "AppIcon.icns")
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(dest)
    return has_icon


# --------------------------------------------------------------------------
# Icon: drawn here (no image libraries), converted with macOS's sips/iconutil
# --------------------------------------------------------------------------

def _smooth(d: float) -> float:
    """Coverage for a signed distance d (negative = inside), 1px anti-aliasing."""
    return min(1.0, max(0.0, 0.5 - d))


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def render_icon(size: int = 1024) -> bytes:
    """Return a PNG: a blue rounded square with the app's white network glyph."""
    s = size / 1024
    inset, radius = 100 * s, 185 * s           # macOS icon grid: 824px tile, ~185px corners
    lo, hi = inset, size - inset
    top, bottom = (0x4b, 0x93, 0xef), (0x1f, 0x5f, 0xc4)
    # Glyph geometry (matches the favicon): three nodes joined at a hub.
    nodes = [(512, 300), (330, 690), (694, 690)]
    hub = (512, 520)
    node_r, ring, stroke = 74 * s, 30 * s, 34 * s
    nodes = [(x * s, y * s) for x, y in nodes]
    hub = (hub[0] * s, hub[1] * s)
    segs = [(nodes[0], hub), (hub, nodes[1]), (hub, nodes[2])]

    rows = []
    for yi in range(size):
        py = yi + 0.5
        row = bytearray(b"\x00")  # PNG filter: none
        f = (py - lo) / (hi - lo)
        bg = [round(top[c] + (bottom[c] - top[c]) * min(1, max(0, f))) for c in range(3)]
        for xi in range(size):
            px = xi + 0.5
            # Rounded-square signed distance
            qx = abs(px - size / 2) - (size / 2 - inset - radius)
            qy = abs(py - size / 2) - (size / 2 - inset - radius)
            d_tile = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius
            a_tile = _smooth(d_tile)
            if a_tile == 0:
                row += b"\x00\x00\x00\x00"
                continue
            # White glyph: lines stop at each node's edge; nodes are rings.
            d = min(_seg_dist(px, py, *a, *b) - stroke / 2 for a, b in segs)
            for nx, ny in nodes:
                dn = math.hypot(px - nx, py - ny)
                d = max(d, -(dn - node_r))          # cut lines inside nodes
            for nx, ny in nodes:
                dn = math.hypot(px - nx, py - ny)
                d = min(d, abs(dn - node_r + ring / 2) - ring / 2)
            a_glyph = _smooth(d)
            r, g, b = (round(bg[c] + (255 - bg[c]) * a_glyph) for c in range(3))
            row += bytes((r, g, b, round(255 * a_tile)))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def make_icns(dest: Path) -> bool:
    """Build an .icns with sips + iconutil (both ship with macOS)."""
    if not (shutil.which("sips") and shutil.which("iconutil")):
        return False
    with tempfile.TemporaryDirectory() as td:
        master = Path(td) / "icon.png"
        master.write_bytes(render_icon(1024))
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = base * scale
                name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
                if _run(["sips", "-z", str(px), str(px), str(master), "--out", str(iconset / name)]).returncode:
                    return False
        return _run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)]).returncode == 0


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _wait_until_up(port: int, seconds: float = 20) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _server_up(port):
            return True
        time.sleep(0.5)
    return False


def _first_scan_status(port: int, seconds: float = 90) -> dict | None:
    """Wait for the first scan to finish; return its status block."""
    import json
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/devices", timeout=2) as r:
                data = json.loads(r.read())
            st = data["status"]
            if (st.get("lan_blocked") or st.get("state") == "paused"
                    or (st.get("state") != "scanning" and st.get("last_scan"))):
                return {**st, "online": sum(1 for d in data["devices"] if d.get("online"))}
        except Exception:
            pass
        time.sleep(1)
    return None


def _tail_log(lines: int = 15) -> str:
    try:
        return "\n".join(LOG.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log yet)"


def install(server_args: list[str]) -> int:
    from .server import build_parser
    try:
        opts = build_parser().parse_args(server_args)  # reject bad options now, not at login
    except SystemExit as e:
        return int(e.code or 2)
    if opts.no_browser:
        server_args = [a for a in server_args if a != "--no-browser"]
    port = opts.port

    was_loaded = _service_loaded()
    if was_loaded:
        _run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])
        for _ in range(20):  # give the old process a moment to release the port
            if not _port_busy(port):
                break
            time.sleep(0.25)
    if _port_busy(port):
        print(f"Port {port} is in use by another program, probably a copy of Network View you started in "
              f"Terminal.\nStop it first (Ctrl+C in its window, or: kill $(lsof -tiTCP:{port} -sTCP:LISTEN)), "
              f"then run this again.\nOr install on another port: python3 -m netview install --port {port + 1}")
        return 1

    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(launch_agent_plist(sys.executable, server_args), f)
    r = _run(["launchctl", "bootstrap", _domain(), str(PLIST)])
    if r.returncode != 0 and not _service_loaded():
        print(f"macOS wouldn't start the background service:\n  {r.stderr.strip() or r.stdout.strip()}")
        return 1

    has_icon = build_app(port)
    print(f"✓ Background service installed{' (updated)' if was_loaded else ''}. It starts when you log in.")
    print(f"✓ App added: {APP}{'' if has_icon else ' (without a custom icon)'}")

    if not _wait_until_up(port):
        print(f"\n✗ The service didn't start. Last lines of {LOG}:\n{_tail_log()}")
        return 1
    print(f"✓ Running at http://localhost:{port}/")
    print("\nChecking it can see your network (the first scan takes up to a minute)…")
    st = _first_scan_status(port)
    if st and st.get("lan_blocked"):
        print("✗ macOS is blocking local network access for the background service.\n"
              "  Open System Settings → Privacy & Security → Local Network and turn on Python\n"
              "  (it may be listed as “python3”). Then run: python3 -m netview status\n"
              "  If Python isn't listed there, see the README's macOS section.")
    elif st and st.get("state") == "paused":
        print("• This network isn't marked as your home network, so scanning is paused.\n"
              "  If it is yours, open the app and click “Trust this network”.")
    elif st:
        print(f"✓ Found {st['online']} device{'s' if st['online'] != 1 else ''} on your network.")
    else:
        print("… The first scan is still running; open the app to watch it.")

    print("\nOpen it any time from Launchpad or Spotlight (“Network View”), or drag it from\n"
          f"{APP.parent} to your Dock.")
    if PROJECT_ROOT.is_relative_to(HOME / "Downloads") or PROJECT_ROOT.is_relative_to(HOME / "Desktop"):
        print(f"\nNote: the service runs the code in {PROJECT_ROOT}. If you move or delete that folder,\n"
              "run 'python3 -m netview install' again from its new location.")
    if not opts.no_browser:
        _run(["open", f"http://localhost:{port}/"])
    return 0


def uninstall() -> int:
    was = _service_loaded()
    if was:
        _run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])
    removed = []
    if PLIST.exists():
        PLIST.unlink()
        removed.append(str(PLIST))
    if APP.exists():
        shutil.rmtree(APP)
        removed.append(str(APP))
    if not (was or removed):
        print("Network View isn't installed as a background service.")
        return 0
    print("✓ Background service stopped and removed." if was else "✓ Removed.")
    for r in removed:
        print(f"  deleted {r}")
    print(f"Your device history and speed tests are still in {HOME / '.network-view'}; delete that folder\n"
          f"to remove them too. The log is at {LOG}.")
    return 0


def status() -> int:
    args = _installed_args()
    if not PLIST.exists():
        print("Not installed. Run: python3 -m netview install")
        return 1
    port = _port_from(args)
    loaded, up = _service_loaded(), _server_up(port)
    print(f"Background service: {'loaded' if loaded else 'NOT loaded'} ({PLIST})")
    print(f"Options:            {shlex.join(args) or '(defaults)'}")
    print(f"Dashboard:          {'running at http://localhost:%d/' % port if up else 'not responding'}")
    print(f"App:                {APP if APP.exists() else 'missing (run install again)'}")
    print(f"Log:                {LOG}")
    if up:
        st = _first_scan_status(port, seconds=5)
        if st and st.get("lan_blocked"):
            print("\n✗ macOS is blocking local network access. Turn on Python in System Settings →\n"
                  "  Privacy & Security → Local Network.")
    else:
        print(f"\nLast lines of the log:\n{_tail_log()}")
    return 0 if up else 1


def main(argv: list[str]) -> int:
    command, rest = argv[0], argv[1:]
    if sys.platform != "darwin":
        print("These commands set Network View up as a macOS background service and app.\n"
              "On Linux, use a systemd user service; on Windows, Task Scheduler. See the README.")
        return 1
    if command == "install":
        if rest[:1] in (["-h"], ["--help"]):
            print("usage: python3 -m netview install [server options]\n\n"
                  "Runs Network View in the background (starting at login) and adds\n"
                  f"'Network View' to {APP.parent}. Server options are the same as for\n"
                  "'python3 -m netview', e.g. --host 0.0.0.0 to use the Wi-Fi test from your phone.\n"
                  "Run it again to change options. See also: status, uninstall.")
            return 0
        return install(rest)
    if command == "uninstall":
        return uninstall()
    if command == "status":
        return status()
    argparse.ArgumentParser().error(f"unknown command {command}")
    return 2
