import asyncio
import errno
from collections import deque
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tailscale_homey_app", ROOT / "app.py")
app_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"homey": SimpleNamespace(app=SimpleNamespace(App=object))}):
    spec.loader.exec_module(app_module)


class Settings:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.app = app_module.App()
        a = self.app
        a.proc = None
        a._cmd_lock = asyncio.Lock()
        a._stopping = False
        a._reader_tasks = []
        a._daemon_stderr = deque(maxlen=10)
        a._daemon_error = ""
        a._watchdog_task = a._status_task = None
        a.tailscale_status_driver = None
        a.state_dir = self.temp.name
        a.state_path = str(Path(self.temp.name) / "state")
        a.socket_path = str(Path(self.temp.name) / "daemon.sock")
        # Executable wrapper allows the real Popen lifecycle to run on macOS/Linux.
        wrapper = Path(self.temp.name) / "tailscaled"
        wrapper.write_text(f"#!{sys.executable}\n" + (ROOT / "tests/fixtures/tailscaled.py").read_text())
        wrapper.chmod(0o755)
        a.tailscaled_path = str(wrapper)
        a.tailscale_path = "unused-cli"
        a.socks5_addr, a.http_proxy_addr = "127.0.0.1:0", "127.0.0.1:0"
        a.homey = SimpleNamespace(settings=Settings(), api=SimpleNamespace(realtime=AsyncMock()))
        a.log = Mock()
        a.error = Mock()
        for name in ("connected", "disconnected", "error", "ip_changed"):
            setattr(a, f"_trigger_{name}", SimpleNamespace(trigger=AsyncMock()))

    async def asyncTearDown(self):
        await self.app._stop_daemon()
        self.temp.cleanup()

    async def test_busy_proxy_ports_start_on_free_loopback_ports(self):
        with socket.socket() as socks, socket.socket() as http:
            socks.bind(("127.0.0.1", 0))
            http.bind(("127.0.0.1", 0))
            socks.listen()
            http.listen()
            original = tuple(f"127.0.0.1:{s.getsockname()[1]}" for s in (socks, http))
            self.app.socks5_addr, self.app.http_proxy_addr = original
            self.assertTrue(await self.app._ensure_daemon())
            self.assertNotIn(self.app.socks5_addr, original)
            self.assertNotIn(self.app.http_proxy_addr, original)
            self.assertNotEqual(self.app.socks5_addr, self.app.http_proxy_addr)
            for addr in (self.app.socks5_addr, self.app.http_proxy_addr):
                host, port = addr.rsplit(":", 1)
                with socket.create_connection((host, int(port)), timeout=1):
                    pass
            # Both original listeners remain available to their owners.
            for addr in original:
                host, port = addr.rsplit(":", 1)
                with socket.create_connection((host, int(port)), timeout=1):
                    pass

    async def test_free_preferred_ports_are_preserved_and_child_reused(self):
        addresses = app_module._select_proxy_addresses("127.0.0.1:0", "127.0.0.1:0")
        self.app.socks5_addr, self.app.http_proxy_addr = addresses
        self.assertTrue(await self.app._ensure_daemon())
        pid = self.app.proc.pid
        self.assertEqual((self.app.socks5_addr, self.app.http_proxy_addr), addresses)
        self.assertTrue(await self.app._ensure_daemon())
        self.assertEqual(self.app.proc.pid, pid)

    async def test_port_claimed_after_preflight_is_retried(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            claimed = f"127.0.0.1:{busy.getsockname()[1]}"
            select = app_module._select_proxy_addresses
            calls = 0

            def racing_select(*addresses):
                nonlocal calls
                calls += 1
                return (claimed, "127.0.0.1:0") if calls == 1 else select(*addresses)

            with patch.object(app_module, "_select_proxy_addresses", side_effect=racing_select):
                self.assertTrue(await self.app._ensure_daemon())
            self.assertEqual(calls, 2)
            self.assertNotEqual(self.app.socks5_addr, claimed)
            self.assertEqual(self.app._daemon_error, "")

    async def test_repeated_bind_failure_is_bounded_and_clears_running(self):
        self.seed_running()
        with patch.dict("os.environ", {"HOMEY_TEST_DAEMON_FAIL": "SOCKS5 listener: bind: address already in use"}):
            with patch.object(app_module, "_select_proxy_addresses", wraps=app_module._select_proxy_addresses) as select:
                self.assertFalse(await self.app._ensure_daemon())
                self.assertEqual(select.call_count, 3)
        self.assertIsNone(self.app.proc)
        self.assertIn("bind: address already in use", self.app.homey.settings.get("last_status_text"))
        self.assert_cleared()
        # Polling must preserve the useful startup diagnostic.
        await self.app.api_refresh()
        self.assertIn("bind: address already in use", self.app.homey.settings.get("last_status_text"))

    async def test_non_port_exit_keeps_diagnostic_and_does_not_retry(self):
        with patch.dict("os.environ", {"HOMEY_TEST_DAEMON_FAIL": "state: permission denied"}):
            with patch.object(app_module, "_select_proxy_addresses", wraps=app_module._select_proxy_addresses) as select:
                self.assertFalse(await self.app._ensure_daemon())
                self.assertEqual(select.call_count, 1)
        self.assertIn("permission denied", self.app.homey.settings.get("last_status_text"))

    async def test_stale_socket_does_not_imply_ready(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.app.socket_path)
        stale.close()
        self.assertFalse(await self.app._socket_ready())
        self.assertTrue(await self.app._ensure_daemon())

    async def test_unresponsive_localapi_is_not_ready(self):
        server = await asyncio.start_unix_server(lambda r, w: w.close(), path=self.app.socket_path)
        async with server:
            self.assertFalse(await self.app._socket_ready())

    async def test_localapi_error_response_is_not_ready(self):
        async def unavailable(reader, writer):
            await reader.read(4096)
            writer.write(b"HTTP/1.0 503 Service Unavailable\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(unavailable, path=self.app.socket_path)
        async with server:
            self.assertFalse(await self.app._socket_ready())

    async def test_permission_failure_is_reported_without_port_fallback(self):
        self.seed_running()
        with patch.object(app_module, "_select_proxy_addresses", side_effect=PermissionError(errno.EACCES, "permission denied")) as select:
            self.assertFalse(await self.app._ensure_daemon())
        self.assertEqual(select.call_count, 1)
        self.assertIn("permission denied", self.app.homey.settings.get("last_status_text"))
        self.assert_cleared()

    async def test_timeout_terminates_child_before_retry(self):
        with patch.object(self.app, "_wait_for_socket", AsyncMock(return_value=False)):
            self.assertFalse(await self.app._ensure_daemon())
        self.assertIsNone(self.app.proc)
        self.assertEqual(self.app._reader_tasks, [])
        self.assertIn("LocalAPI did not become ready", self.app.homey.settings.get("last_status_text"))

    async def test_shutdown_reaps_daemon_and_stops_background_tasks(self):
        self.assertTrue(await self.app._ensure_daemon())
        proc = self.app.proc
        self.app._watchdog_task = asyncio.create_task(asyncio.sleep(100))
        self.app._status_task = asyncio.create_task(asyncio.sleep(100))
        await self.app.on_uninit()
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(self.app.proc)
        self.assertTrue(self.app._watchdog_task.cancelled())
        self.assertTrue(self.app._status_task.cancelled())
        self.assertFalse(await self.app._ensure_daemon())

    def seed_running(self):
        self.app.homey.settings.values.update(
            runtime_state="running", backend_state="Running", tailnet_online=True,
            tailnet_hostname="old-homey", tailnet_dns_name="old.tailnet.ts.net",
            tailscale_ipv4="100.64.0.1", tailscale_ipv6="fd7a:115c:a1e0::1",
            accepted_routes=["192.168.0.0/24"], tailnet_allowed_ips=["100.64.0.1/32"], peer_count=2,
        )

    def assert_cleared(self):
        values = self.app.homey.settings
        self.assertEqual(values.get("runtime_state"), "error")
        for key in ("backend_state", "tailnet_hostname", "tailnet_dns_name", "tailscale_ipv4", "tailscale_ipv6"):
            self.assertEqual(values.get(key), "", key)
        self.assertFalse(values.get("tailnet_online"))
        self.assertEqual(values.get("accepted_routes"), [])
        self.assertEqual(values.get("tailnet_allowed_ips"), [])
        self.assertEqual(values.get("peer_count"), 0)
        self.assertFalse(self.app.build_device_snapshot()["connected"])

    async def test_dead_daemon_clears_persisted_running_without_cli(self):
        self.seed_running()
        self.app._run_cmd = AsyncMock()
        await self.app.api_refresh()
        self.assert_cleared()
        self.app._run_cmd.assert_not_called()
        self.assertFalse(await self.app._flow_is_connected_listener({}))

    async def test_cli_failure_and_malformed_status_clear_old_backend(self):
        self.assertTrue(await self.app._ensure_daemon())
        for code, output in ((1, ""), (0, "not json"), (0, "[]"), (0, ""), (0, '{"Self": "invalid"}')):
            with self.subTest(code=code, output=output):
                self.seed_running()
                result = subprocess.CompletedProcess([], code, stdout=output, stderr="status unavailable")
                self.app._run_cmd = AsyncMock(return_value=result)
                await self.app.api_refresh()
                self.assert_cleared()

    async def test_valid_status_retains_ip_routes_and_connected_flow(self):
        self.assertTrue(await self.app._ensure_daemon())
        status = '{"BackendState":"Running","Self":{"Online":true,"DNSName":"homey.ts.net"},"Peer":{"router":{"PrimaryRoutes":["192.168.1.0/24","100.64.0.2/32"]}}}'
        self.app._run_cmd = AsyncMock(side_effect=[
            subprocess.CompletedProcess([], 0, stdout=status, stderr=""),
            subprocess.CompletedProcess([], 0, stdout="100.64.0.1\nfd7a:115c:a1e0::1\n", stderr=""),
        ])
        await self.app.api_refresh()
        self.assertEqual(self.app.homey.settings.get("runtime_state"), "running")
        self.assertEqual(self.app.homey.settings.get("tailscale_ipv4"), "100.64.0.1")
        self.assertEqual(self.app.homey.settings.get("accepted_routes"), ["192.168.1.0/24"])
        self.assertTrue(self.app.build_device_snapshot()["connected"])
        self.assertTrue(await self.app._flow_is_connected_listener({}))

    async def test_daemon_exit_during_status_query_cannot_report_running(self):
        self.assertTrue(await self.app._ensure_daemon())
        self.seed_running()

        async def stopped_during_query(*args, **kwargs):
            await self.app._stop_daemon()
            return subprocess.CompletedProcess([], 0, stdout='{"BackendState":"Running"}', stderr="")

        self.app._run_cmd = AsyncMock(side_effect=stopped_during_query)
        await self.app.api_refresh()
        self.assert_cleared()


if __name__ == "__main__":
    unittest.main()
