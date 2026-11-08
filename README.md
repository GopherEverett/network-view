# Network View

A small web app that shows every device connected to your local network: IP and MAC address, hostname, manufacturer, a best guess at the device type, latency, open ports, and when each device was first and last seen.

It uses only the Python standard library, so there's nothing to install, and it doesn't need root or admin rights. It runs on Linux, macOS and Windows.

## Run it

You need Python 3.10 or newer.

```bash
python3 -m netview
```

Your browser opens to <http://localhost:8765>. The first scan of a typical home network (/24) takes about 5–30 seconds. After that the app rescans every 5 minutes.

### Options

| Flag | Default | What it does |
|---|---|---|
| `--port` | `8765` | Port for the web UI |
| `--host` | `127.0.0.1` | Address to listen on. Use `0.0.0.0` to open the dashboard from your phone (see the security note below). |
| `--cidr` | auto | Subnet to scan, e.g. `192.168.1.0/24` |
| `--interval` | `300` | Seconds between automatic rescans; `0` turns them off |
| `--data-dir` | `~/.network-view` | Where device history and the vendor database are kept |
| `--no-download` | off | Don't download the IEEE vendor list |
| `--no-browser` | off | Don't open a browser on start |

## What you get

- **Live device table**: online/offline status, name, IP, MAC, vendor, type and latency. It can be sorted, searched, and filtered by type or status.
- **History**: each device is remembered by its MAC address, with first-seen and last-seen times. Devices that join after the first scan get a **new** badge for 24 hours.
- **Device details**: click a row to give a device a friendly name and notes, scan its common ports (web UIs open in one click), or forget it.
- **Type guessing** from the vendor, hostname and open ports: router, phone, TV/streaming, printer, camera, smart home, NAS and so on.
- **Private MAC detection**: phones that use randomized Wi-Fi addresses get a ⟳ icon.

## How discovery works

1. Find this computer's IP, subnet and router from the OS routing table or `ip`, `ifconfig` or `ipconfig`.
2. Sweep every address in the subnet in parallel. Each address gets an ICMP ping through the system `ping` tool, falling back to a TCP connect probe on common ports. Subnets larger than /22 are capped at the 1,022 addresses around you.
3. Read the OS ARP/neighbour table. This also catches devices that ignore ping, because they still have to answer ARP.
4. Look up names with reverse DNS, falling back to a NetBIOS query (Windows PCs, NAS boxes).
5. Look up the vendor from the MAC prefix. On first run the app downloads the official IEEE OUI list (~3 MB) and caches it. Until that finishes it uses any local copy from nmap or Wireshark, if one is installed.

Some devices may be missing or show partial details:
- Devices on a guest Wi-Fi or another VLAN can't be seen, because ARP doesn't cross routers.
- Some devices sleep (phones especially) and only show up once they wake.
- Many phones use randomized MACs, so a phone can look like a new device after it rotates its address.

## Security note

The dashboard shows a map of your network, so by default it only listens on `localhost`. The API rejects cross-origin requests that change data. If you use `--host 0.0.0.0`, anyone on your network can open the dashboard and start port scans. Only do that on a network you trust.

Only scan networks you own or have permission to scan.

## Development

```bash
python3 -m unittest discover -s tests
```

```
netview/
  scanner.py   subnet detection, ping/TCP sweep, ARP parsing, names, port scan
  vendors.py   MAC vendor database + device-type heuristics
  server.py    JSON API, device history (devices.json), CLI entry point
  static/      dashboard (plain HTML/CSS/JS, no build step)
tests/         parser and heuristic tests with sample OS output
```

### API

| Method | Path | |
|---|---|---|
| `GET` | `/api/devices` | Network info, scan status, all devices |
| `POST` | `/api/scan` | Start a scan (no-op if one is running) |
| `POST` | `/api/devices/{id}/ports` | Scan common ports on a device |
| `PATCH` | `/api/devices/{id}` | Set `name` / `notes` |
| `DELETE` | `/api/devices/{id}` | Forget a device |

`{id}` is the URL-encoded MAC address, or `ip:<address>` for a device whose MAC isn't known.
