import plistlib
import shutil
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from netview import macos


class PlistTests(unittest.TestCase):
    def test_launch_agent(self):
        p = macos.launch_agent_plist("/opt/homebrew/bin/python3", ["--host", "0.0.0.0"])
        self.assertEqual(p["Label"], "local.network-view")
        self.assertEqual(p["ProgramArguments"],
                         ["/opt/homebrew/bin/python3", "-m", "netview", "--no-browser", "--host", "0.0.0.0"])
        self.assertTrue(p["RunAtLoad"])
        self.assertTrue(p["KeepAlive"])
        self.assertIn("/usr/sbin", p["EnvironmentVariables"]["PATH"])  # arp, ping live in sbin
        self.assertEqual(p["EnvironmentVariables"]["PYTHONPATH"], str(macos.PROJECT_ROOT))
        plistlib.dumps(p)  # serializable

    def test_app_info(self):
        info = macos.app_info_plist()
        self.assertEqual(info["CFBundleExecutable"], "network-view")
        self.assertTrue(info["LSUIElement"])
        plistlib.dumps(info)


class AppBundleTests(unittest.TestCase):
    def test_build_app(self):
        dest = Path(tempfile.mkdtemp()) / "Applications" / "Network View.app"
        with mock.patch.object(macos, "make_icns", return_value=False):
            macos.build_app(8800, dest)
        exe = dest / "Contents" / "MacOS" / "network-view"
        self.assertTrue(exe.stat().st_mode & 0o111)
        script = exe.read_text()
        self.assertIn("http://127.0.0.1:8800/api/devices", script)
        self.assertIn("http://localhost:8800/", script)
        with open(dest / "Contents" / "Info.plist", "rb") as f:
            self.assertEqual(plistlib.load(f)["CFBundleName"], "Network View")
        # Rebuilding replaces it cleanly
        with mock.patch.object(macos, "make_icns", return_value=False):
            macos.build_app(8801, dest)
        self.assertIn("8801", exe.read_text())
        self.assertFalse(dest.with_name(dest.name + ".tmp").exists())

    @unittest.skipUnless(shutil.which("sh"), "needs sh")
    def test_launcher_script_is_valid_shell(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(macos.launcher_script(8765))
        r = subprocess.run(["sh", "-n", f.name], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class IconTests(unittest.TestCase):
    def test_png(self):
        png = macos.render_icon(64)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        # Decode IDAT and check the corners are transparent and the centre is opaque
        idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
        raw = zlib.decompress(idat)
        stride = 1 + 64 * 4
        self.assertEqual(len(raw), 64 * stride)
        px = lambda x, y: raw[y * stride + 1 + x * 4: y * stride + 1 + x * 4 + 4]
        self.assertEqual(px(0, 0)[3], 0)
        self.assertEqual(px(32, 50)[3], 255)


def _fake_interpreter(where: Path, works: bool) -> Path:
    """Stand-in for Python.app's executable: a script that runs this Python."""
    exe = where / ("python-ok" if works else "python-broken")
    exe.write_text(f"#!/bin/sh\nexec {__import__('sys').executable} \"$@\"\n" if works else "#!/bin/sh\nexit 1\n")
    exe.chmod(0o755)
    return exe


class AgentTests(unittest.TestCase):
    def test_agent_bundle(self):
        app = Path(tempfile.mkdtemp()) / "Network View.app"
        with mock.patch.object(macos, "real_interpreter", return_value=_fake_interpreter(app.parent, True)):
            exe = macos.build_agent(app)
        self.assertEqual(exe.name, "Network View Agent")
        with open(app / macos.AGENT_REL / "Contents/Info.plist", "rb") as f:
            info = plistlib.load(f)
        self.assertEqual(info["CFBundleName"], "Network View")
        self.assertEqual(info["CFBundleExecutable"], exe.name)
        self.assertIn("NSLocalNetworkUsageDescription", info)
        self.assertTrue(info["LSBackgroundOnly"])

    def test_no_framework_python(self):
        with mock.patch.object(macos, "real_interpreter", return_value=None):
            self.assertIsNone(macos.build_agent(Path(tempfile.mkdtemp())))

    def test_launch_agent_names_the_app(self):
        p = macos.launch_agent_plist("/x/Network View Agent", [])
        self.assertEqual(p["AssociatedBundleIdentifiers"], ["local.network-view.launcher"])


class WaitForScanTests(unittest.TestCase):
    """_first_scan_status against a sequence of real-shaped API responses."""

    def run_sequence(self, responses, **kw):
        import io
        import json as _json
        it = iter(responses)

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, url, timeout=None):
                return Resp(_json.dumps(next(it)).encode())

        with mock.patch.object(macos.urllib.request, "build_opener", return_value=Opener()), \
                mock.patch.object(macos.time, "sleep"):
            return macos._first_scan_status(8765, seconds=3600, **kw)

    def test_allow_clicked_during_first_scan(self):
        # What happened on a real Mac: the first scan starts blocked; the user
        # clicks Allow; that same scan then finds devices and lifts the block.
        dev = lambda online: {"online": online}
        seq = [
            {"status": {"state": "scanning", "lan_blocked": True, "last_scan": None}, "devices": [dev(True)]},
            {"status": {"state": "scanning", "lan_blocked": True, "last_scan": None}, "devices": [dev(True)]},
            {"status": {"state": "idle", "lan_blocked": False, "last_scan": 50},
             "devices": [dev(True)] * 12},
        ]
        first = self.run_sequence(seq[:1])
        self.assertTrue(first["lan_blocked"])
        done = self.run_sequence(seq[1:], unblocked=True)
        self.assertEqual(done["online"], 12)

    def test_unblocked_waits_while_still_blocked(self):
        seq = [{"status": {"state": "idle", "lan_blocked": True, "last_scan": 1}, "devices": []}] * 3 + [
            {"status": {"state": "scanning", "lan_blocked": False, "last_scan": 1}, "devices": []},
            {"status": {"state": "idle", "lan_blocked": False, "last_scan": 2}, "devices": [{"online": True}] * 4},
        ]
        self.assertEqual(self.run_sequence(seq, unblocked=True)["online"], 4)


class InstallFlowTests(unittest.TestCase):
    """Install/uninstall with launchctl and the network replaced by fakes."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.calls = []
        self.loaded = False
        patches = [
            mock.patch.object(macos, "PLIST", self.home / "Library/LaunchAgents/local.network-view.plist"),
            mock.patch.object(macos, "APP", self.home / "Applications/Network View.app"),
            mock.patch.object(macos, "LOG", self.home / "Library/Logs/network-view.log"),
            mock.patch.object(macos, "HOME", self.home),
            mock.patch.object(macos, "_run", side_effect=self.fake_run),
            mock.patch.object(macos, "_service_loaded", side_effect=lambda: self.loaded),
            mock.patch.object(macos, "make_icns", return_value=True),
            mock.patch.object(macos, "_domain", return_value="gui/501"),
            mock.patch.object(macos.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_run(self, cmd):
        self.calls.append(cmd)
        if cmd[:2] == ["launchctl", "bootstrap"]:
            self.loaded = True
        if cmd[:2] == ["launchctl", "bootout"]:
            self.loaded = False
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def test_install_happy_path(self):
        status = {"state": "idle", "last_scan": 1, "lan_blocked": False, "online": 7}
        with mock.patch.object(macos, "_port_busy", return_value=False), \
                mock.patch.object(macos, "_server_up", return_value=True), \
                mock.patch.object(macos, "_first_scan_status", return_value=status), \
                mock.patch("builtins.print") as out:
            self.assertEqual(macos.install(["--host", "0.0.0.0", "--no-browser"]), 0)
        text = "\n".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("Found 7 devices", text)
        with open(macos.PLIST, "rb") as f:
            argv = plistlib.load(f)["ProgramArguments"]
        self.assertEqual(argv[-2:], ["--host", "0.0.0.0"])
        self.assertEqual(argv.count("--no-browser"), 1)
        self.assertTrue((macos.APP / "Contents/MacOS/network-view").exists())
        self.assertIn(["launchctl", "bootstrap", "gui/501", str(macos.PLIST)], self.calls)
        self.assertNotIn(["open", "http://localhost:8765/"], self.calls)  # --no-browser respected

        # status reads the recorded options back
        self.assertEqual(macos._installed_args(), ["--host", "0.0.0.0"])

        # uninstall removes both
        with mock.patch("builtins.print"):
            self.assertEqual(macos.uninstall(), 0)
        self.assertFalse(macos.PLIST.exists())
        self.assertFalse(macos.APP.exists())
        self.assertFalse(self.loaded)

    def test_install_refuses_when_port_taken(self):
        with mock.patch.object(macos, "_port_busy", return_value=True), mock.patch("builtins.print") as out:
            self.assertEqual(macos.install([]), 1)
        self.assertIn("in use", out.call_args_list[0].args[0])
        self.assertFalse(macos.PLIST.exists())

    def test_install_waits_for_allow(self):
        statuses = [{"lan_blocked": True, "state": "idle", "last_scan": 100},
                    {"lan_blocked": False, "state": "idle", "last_scan": 200, "online": 9}]
        with mock.patch.object(macos, "_port_busy", return_value=False), \
                mock.patch.object(macos, "_server_up", return_value=True), \
                mock.patch.object(macos, "_first_scan_status", side_effect=statuses) as fss, \
                mock.patch("builtins.print") as out:
            macos.install(["--no-browser"])
        text = "\n".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("Privacy & Security → Local Network", text)
        self.assertIn("Found 9 devices", text)
        self.assertTrue(fss.call_args_list[1].kwargs["unblocked"])

    def test_install_still_blocked(self):
        with mock.patch.object(macos, "_port_busy", return_value=False), \
                mock.patch.object(macos, "_server_up", return_value=True), \
                mock.patch.object(macos, "_first_scan_status", side_effect=[{"lan_blocked": True}, None]), \
                mock.patch("builtins.print") as out:
            macos.install(["--no-browser"])
        self.assertIn("Still blocked", "\n".join(str(c.args[0]) for c in out.call_args_list))

    def test_service_runs_agent_when_available(self):
        fake_python = _fake_interpreter(self.home, works=True)
        with mock.patch.object(macos, "real_interpreter", return_value=fake_python), \
                mock.patch.object(macos, "_port_busy", return_value=False), \
                mock.patch.object(macos, "_server_up", return_value=True), \
                mock.patch.object(macos, "_first_scan_status", return_value={"state": "idle", "last_scan": 1,
                                                                             "online": 3}), \
                mock.patch("builtins.print"):
            macos.install(["--no-browser"])
        with open(macos.PLIST, "rb") as f:
            program = plistlib.load(f)["ProgramArguments"][0]
        self.assertEqual(program, str(macos.APP / macos.AGENT_REL / "Contents/MacOS/Network View Agent"))
        self.assertTrue(Path(program).exists())

    def test_service_falls_back_to_python3(self):
        fake_python = _fake_interpreter(self.home, works=False)
        with mock.patch.object(macos, "real_interpreter", return_value=fake_python), \
                mock.patch.object(macos, "_port_busy", return_value=False), \
                mock.patch.object(macos, "_server_up", return_value=True), \
                mock.patch.object(macos, "_first_scan_status", return_value={"state": "idle", "last_scan": 1,
                                                                             "online": 3}), \
                mock.patch("builtins.print") as out:
            macos.install(["--no-browser"])
        with open(macos.PLIST, "rb") as f:
            self.assertEqual(plistlib.load(f)["ProgramArguments"][0], __import__("sys").executable)
        self.assertFalse((macos.APP / macos.AGENT_REL).exists())
        self.assertIn("can't run as the service's own app", "\n".join(str(c.args[0]) for c in out.call_args_list))

    def test_install_shows_log_when_service_fails(self):
        macos.LOG.parent.mkdir(parents=True)
        macos.LOG.write_text("Traceback...\nOSError: boom\n")
        with mock.patch.object(macos, "_port_busy", return_value=False), \
                mock.patch.object(macos, "_server_up", return_value=False), \
                mock.patch.object(macos, "_wait_until_up", return_value=False), \
                mock.patch("builtins.print") as out:
            self.assertEqual(macos.install([]), 1)
        self.assertIn("OSError: boom", out.call_args_list[-1].args[0])

    def test_bad_options_rejected_before_installing(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(macos.install(["--port", "not-a-number"]), 2)
        self.assertFalse(macos.PLIST.exists())


class EntryPointTests(unittest.TestCase):
    def test_non_mac_explains(self):
        r = subprocess.run([__import__("sys").executable, "-m", "netview", "status"],
                           capture_output=True, text=True, cwd=macos.PROJECT_ROOT)
        if __import__("sys").platform != "darwin":
            self.assertEqual(r.returncode, 1)
            self.assertIn("macOS", r.stdout)


if __name__ == "__main__":
    unittest.main()
