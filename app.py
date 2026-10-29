from homey import app
import asyncio
import subprocess
import platform
import os
import json
import ipaddress
import time
import socket
import errno
from collections import deque
from typing import Any


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _is_subnet_route(cidr: str) -> bool:
    """True for advertised LAN/subnet routes, false for the node Tailscale addresses."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False

    if net.version == 4 and net.prefixlen >= 32:
        return False
    if net.version == 6 and net.prefixlen >= 128:
        return False

    if net.version == 4 and net.network_address in ipaddress.ip_network("100.64.0.0/10"):
        return False
    if net.version == 6 and str(net.network_address).startswith("fd7a:115c:a1e0:"):
        return False

    return True


def _normalize_advertise_routes(raw: str) -> tuple[str, str | None]:
    """Return cleaned comma-separated CIDRs, or ("", error)."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        return "", None

    cleaned: list[str] = []
    for part in parts:
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            return "", f"Invalid CIDR: {part}"
        cleaned.append(str(net))
    return ",".join(cleaned), None


def _normalize_advertise_tags(raw: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        return "", None

    cleaned: list[str] = []
    for part in parts:
        tag = part if part.startswith("tag:") else f"tag:{part}"
        name = tag[4:]
        if not name or any(c.isspace() for c in tag):
            return "", f"Invalid tag: {part}"
        cleaned.append(tag)
    return ",".join(cleaned), None


def _resolve_state_dir() -> str:
    """Prefer persistent storage; fall back to /tmp if userdata is not writable."""
    candidates = [
        "/userdata/tailscale-homey",
        "/app/userdata/tailscale-homey",
        "/tmp/tailscale-homey",
    ]
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write_test")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            return path
        except OSError:
            continue
    return "/tmp/tailscale-homey"


def _resolve_bin_arch() -> tuple[str, str | None]:
    """
    Map Homey CPU arch to bundled Tailscale binaries.
    Python Apps SDK only runs on Homey Pro Early 2023+ / mini / self-hosted (aarch64).
    """
    arch = platform.machine().lower()
    if arch in ("aarch64", "arm64"):
        return "aarch64", None
    return "", (
        f"Unsupported CPU architecture '{arch}'. "
        "This Python app requires Homey Pro Early 2023+, Homey Pro mini, "
        "or Homey Self-Hosted Server (aarch64). Homey Pro 2019 is not supported by Athom's Python runtime."
    )


def _select_proxy_addresses(socks_addr: str, http_addr: str) -> tuple[str, str]:
    """Keep preferred loopback ports when free; ask the OS for alternatives otherwise."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as socks, \
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as http:
        addresses = []
        for listener, preferred in ((socks, socks_addr), (http, http_addr)):
            host, port = preferred.rsplit(":", 1)
            try:
                listener.bind((host, int(port)))
            except OSError as err:
                if err.errno != errno.EADDRINUSE:
                    raise
                listener.bind((host, 0))
            addresses.append(f"{host}:{listener.getsockname()[1]}")
        return addresses[0], addresses[1]


class App(app.App):
    async def on_init(self) -> None:
        self.proc = None
        self._cmd_lock = asyncio.Lock()
        self._stopping = False
        self._watchdog_task = None
        self._status_task = None
        self._reader_tasks = []
        self._daemon_stderr = deque(maxlen=10)
        self._daemon_error = ""
        self.tailscale_status_driver = None

        self._reload_settings()

        bin_arch, arch_err = _resolve_bin_arch()
        self.bin_arch = bin_arch or "unknown"
        self.tailscaled_path = f"/app/bin/{bin_arch}/tailscaled" if bin_arch else ""
        self.tailscale_path = f"/app/bin/{bin_arch}/tailscale" if bin_arch else ""

        self.state_dir = _resolve_state_dir()
        self.state_path = f"{self.state_dir}/tailscaled.state"
        self.socket_path = f"{self.state_dir}/tailscaled.sock"
        self.socks5_addr = "127.0.0.1:1055"
        self.http_proxy_addr = "127.0.0.1:1056"

        os.environ["XDG_CACHE_HOME"] = self.state_dir
        os.environ["XDG_STATE_HOME"] = self.state_dir
        os.environ["XDG_DATA_HOME"] = self.state_dir

        # Flow cards
        self._flow_connect = self.homey.flow.get_action_card("connect_tailscale")
        self._flow_disconnect = self.homey.flow.get_action_card("disconnect_tailscale")
        self._flow_reconnect = self.homey.flow.get_action_card("reconnect_tailscale")
        self._flow_refresh = self.homey.flow.get_action_card("refresh_tailscale")
        self._flow_is_connected = self.homey.flow.get_condition_card("tailscale_is_connected")

        self._trigger_connected = self.homey.flow.get_trigger_card("tailscale_connected")
        self._trigger_disconnected = self.homey.flow.get_trigger_card("tailscale_disconnected")
        self._trigger_ip_changed = self.homey.flow.get_trigger_card("tailscale_ip_changed")
        self._trigger_error = self.homey.flow.get_trigger_card("tailscale_error")

        self._flow_connect.register_run_listener(self._flow_connect_listener)
        self._flow_disconnect.register_run_listener(self._flow_disconnect_listener)
        self._flow_reconnect.register_run_listener(self._flow_reconnect_listener)
        self._flow_refresh.register_run_listener(self._flow_refresh_listener)
        self._flow_is_connected.register_run_listener(self._flow_is_connected_listener)

        self.log("=== Tailscale for Homey starting ===")
        self.log(f"OS: {platform.system()} {platform.release()}")
        self.log(f"Architecture: {platform.machine()} -> bin/{self.bin_arch}")
        self.log(f"State dir: {self.state_dir}")
        self.log(f"Hostname: {self.hostname}")
        self.log(f"Auto connect: {self.auto_connect}")
        self.log(f"Accept routes: {self.accept_routes}")
        self.log(f"Enable subnet router: {self.enable_subnet_router}")
        self.log(f"Advertise routes: {self.advertise_routes}")
        self.log(f"Advertise tags: {self.advertise_tags}")

        await self._clear_backend_state()

        if arch_err:
            self.error(arch_err)
            await self._set_runtime_state("error", arch_err)
            return

        if not os.path.isfile(self.tailscaled_path) or not os.path.isfile(self.tailscale_path):
            msg = f"Tailscale binaries missing under /app/bin/{bin_arch}/"
            self.error(msg)
            await self._set_runtime_state("error", msg)
            return

        if self.enable_subnet_router and not self.advertise_routes:
            self.log("Subnet router enabled but no routes configured; nothing will be advertised")

        await self._save_status_text("Starting tailscaled...")
        async with self._cmd_lock:
            ready = await self._ensure_daemon()

        if ready and self.auto_connect and self.auth_key:
            await self.api_connect()
        elif ready:
            await self.api_refresh()

        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        self._status_task = asyncio.create_task(self._status_poll_loop())

    def _reload_settings(self) -> None:
        self.auth_key = self.homey.settings.get("auth_key") or ""
        self.hostname = (self.homey.settings.get("hostname") or "homey-pro").strip() or "homey-pro"
        self.auto_connect = _as_bool(self.homey.settings.get("auto_connect"))
        self.accept_routes = _as_bool(self.homey.settings.get("accept_routes"))
        self.enable_subnet_router = _as_bool(self.homey.settings.get("enable_subnet_router"))
        self.advertise_routes = (self.homey.settings.get("advertise_routes") or "").strip()
        self.advertise_tags = (self.homey.settings.get("advertise_tags") or "").strip()

    def _build_up_cmd(self) -> tuple[list[str] | None, str | None]:
        """Build `tailscale up` with explicit prefs. Returns (cmd, error)."""
        routes = ""
        if self.enable_subnet_router and self.advertise_routes:
            routes, err = _normalize_advertise_routes(self.advertise_routes)
            if err:
                return None, err

        tags, tag_err = _normalize_advertise_tags(self.advertise_tags)
        if tag_err:
            return None, tag_err

        up_cmd = [
            self.tailscale_path,
            f"--socket={self.socket_path}",
            "up",
            f"--hostname={self.hostname}",
            "--accept-dns=false",
            f"--accept-routes={'true' if self.accept_routes else 'false'}",
        ]

        # Reusable auth keys: always pass so first boot / wiped state can login.
        if self.auth_key:
            up_cmd.append(f"--auth-key={self.auth_key}")

        if self.enable_subnet_router and routes:
            up_cmd.append(f"--advertise-routes={routes}")
        else:
            up_cmd.append("--advertise-routes=")

        if tags:
            up_cmd.append(f"--advertise-tags={tags}")

        return up_cmd, None

    async def _wait_for_socket(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stopping or not self.proc or self.proc.poll() is not None:
                return False
            if await self._socket_ready():
                return self.proc.poll() is None
            await asyncio.sleep(0.25)
        return False

    async def _socket_ready(self) -> bool:
        """A socket file alone may be stale or belong to a daemon still starting."""
        writer = None
        try:
            async with asyncio.timeout(1):
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                writer.write(b"GET /localapi/v0/status HTTP/1.0\r\nHost: local-tailscaled.sock\r\n\r\n")
                await writer.drain()
                response = await reader.readline()
                return response.startswith((b"HTTP/1.0 200 ", b"HTTP/1.1 200 "))
        except (OSError, TimeoutError, ValueError):
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def _ensure_daemon(self) -> bool:
        if self._stopping:
            return False
        if self.proc and self.proc.poll() is None and await self._socket_ready():
            return True

        # Port selection is a preflight check. Retry a bounded number of times
        # if another process claims a selected port before tailscaled binds it.
        for attempt in range(3):
            try:
                await self._start_daemon()
                if await self._wait_for_socket():
                    self._daemon_error = ""
                    return True
                code = self.proc.poll() if self.proc else None
                await self._stop_daemon()
                detail = self._daemon_stderr[-1] if self._daemon_stderr else ""
                self._daemon_error = (
                    f"tailscaled exited with code {code}: {detail}" if code is not None
                    else "tailscaled LocalAPI did not become ready"
                )
                conflict = any("bind: address already in use" in line.lower()
                               for line in self._daemon_stderr)
                if conflict and attempt < 2 and not self._stopping:
                    self.log("Proxy port was claimed during startup; selecting ports again")
                    continue
            except OSError as err:
                self._daemon_error = f"Could not start tailscaled: {err}"
                await self._stop_daemon()
            break
        await self._clear_backend_state()
        await self._set_runtime_state("error", self._daemon_error)
        return False

    async def _start_daemon(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.log("tailscaled already running")
            return

        await self._stop_daemon()
        await self._clear_backend_state()
        previous = (self.socks5_addr, self.http_proxy_addr)
        self.socks5_addr, self.http_proxy_addr = _select_proxy_addresses(*previous)
        if previous != (self.socks5_addr, self.http_proxy_addr):
            self.log(f"Proxy port in use; using SOCKS5 {self.socks5_addr}, HTTP {self.http_proxy_addr}")

        # Clear stale socket from a previous crash
        try:
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)
        except OSError as err:
            self.log(f"Could not remove stale socket: {err}")

        # Homey apps cannot create a kernel TUN; userspace netstack + local proxies.
        cmd = [
            self.tailscaled_path,
            "--tun=userspace-networking",
            f"--state={self.state_path}",
            f"--socket={self.socket_path}",
            f"--socks5-server={self.socks5_addr}",
            f"--outbound-http-proxy-listen={self.http_proxy_addr}",
        ]

        self._daemon_stderr = deque(maxlen=10)
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )

        self._reader_tasks = [
            asyncio.create_task(self._read_stdout(self.proc)),
            asyncio.create_task(self._read_stderr(self.proc, self._daemon_stderr)),
        ]
        self.log(f"tailscaled PID: {self.proc.pid}")
        await self._set_runtime_state("starting", "Starting tailscaled...")

    async def _stop_daemon(self) -> None:
        """Reap our child before restarting; never terminate unrelated port owners."""
        proc = self.proc
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.to_thread(proc.wait, 5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 3)
            else:
                await asyncio.to_thread(proc.wait)
        await asyncio.gather(*self._reader_tasks, return_exceptions=True)
        self._reader_tasks = []
        self.proc = None

    async def _watchdog_loop(self) -> None:
        await asyncio.sleep(10)
        while not self._stopping:
            try:
                dead = self.proc is None or self.proc.poll() is not None
                if dead and not self._cmd_lock.locked():
                    self.error("tailscaled is not running; restarting")
                    async with self._cmd_lock:
                        ok = await self._ensure_daemon()
                        if ok and self.auto_connect and self.auth_key:
                            self._reload_settings()
                            up_cmd, err = self._build_up_cmd()
                            if up_cmd and not err:
                                await self._run_cmd(up_cmd, "tailscale up (watchdog)", return_result=True)
                        if ok:
                            await self._refresh_runtime_state()
            except Exception as err:
                self.error(f"Watchdog error: {err}")
            await asyncio.sleep(20)

    async def _status_poll_loop(self) -> None:
        await asyncio.sleep(30)
        while not self._stopping:
            try:
                if not self._cmd_lock.locked():
                    async with self._cmd_lock:
                        await self._refresh_runtime_state()
            except Exception as err:
                self.error(f"Status poll error: {err}")
            await asyncio.sleep(30)

    async def api_get_status(self) -> dict[str, Any]:
        return {
            "runtime_state": self.homey.settings.get("runtime_state") or "unknown",
            "backend_state": self.homey.settings.get("backend_state") or "",
            "tailnet_hostname": self.homey.settings.get("tailnet_hostname") or "",
            "tailnet_dns_name": self.homey.settings.get("tailnet_dns_name") or "",
            "tailnet_online": self.homey.settings.get("tailnet_online"),
            "tailscale_ipv4": self.homey.settings.get("tailscale_ipv4") or "",
            "tailscale_ipv6": self.homey.settings.get("tailscale_ipv6") or "",
            "tailnet_allowed_ips": self.homey.settings.get("tailnet_allowed_ips") or [],
            "accepted_routes": self.homey.settings.get("accepted_routes") or [],
            "peer_count": self.homey.settings.get("peer_count") or 0,
            "auth_key_configured": bool(self.homey.settings.get("auth_key")),
            "auto_connect": _as_bool(self.homey.settings.get("auto_connect")),
            "accept_routes": _as_bool(self.homey.settings.get("accept_routes")),
            "enable_subnet_router": _as_bool(self.homey.settings.get("enable_subnet_router")),
            "advertise_routes": self.homey.settings.get("advertise_routes") or "",
            "advertise_tags": self.homey.settings.get("advertise_tags") or "",
            "last_status_text": self.homey.settings.get("last_status_text") or "No status yet.",
            "userspace_networking": True,
            "socks5_proxy": self.socks5_addr,
            "http_proxy": self.http_proxy_addr,
            "state_dir": self.state_dir,
            "bin_arch": getattr(self, "bin_arch", ""),
        }

    async def api_connect(self) -> dict[str, Any]:
        async with self._cmd_lock:
            self._reload_settings()

            if not self.auth_key:
                await self._set_runtime_state("error", "Auth key is empty")
                return {"ok": False, "error": "auth_key_empty"}

            up_cmd, build_err = self._build_up_cmd()
            if build_err or not up_cmd:
                await self._set_runtime_state("error", build_err or "Invalid settings")
                return {"ok": False, "error": "invalid_settings", "stderr": build_err or ""}

            if not await self._ensure_daemon():
                return {"ok": False, "error": "daemon_not_ready"}

            await self._set_runtime_state("connecting", "Connecting to Tailscale...")

            result = await self._run_cmd(up_cmd, "tailscale up", return_result=True)
            await asyncio.sleep(2)
            await self._refresh_runtime_state()

            ok = bool(result and result.returncode == 0)
            return {
                "ok": ok,
                "stdout": result.stdout.strip() if result else "",
                "stderr": result.stderr.strip() if result else "",
            }

    async def api_disconnect(self) -> dict[str, Any]:
        async with self._cmd_lock:
            result = await self._run_cmd(
                [self.tailscale_path, f"--socket={self.socket_path}", "down"],
                "tailscale down",
                return_result=True
            )
            await asyncio.sleep(1)
            await self._refresh_runtime_state()
            return {
                "ok": bool(result and result.returncode == 0),
                "stdout": result.stdout.strip() if result else "",
                "stderr": result.stderr.strip() if result else "",
            }

    async def api_reconnect(self) -> dict[str, Any]:
        async with self._cmd_lock:
            self._reload_settings()

            if not self.auth_key:
                await self._set_runtime_state("error", "Auth key is empty")
                return {"ok": False, "error": "auth_key_empty"}

            up_cmd, build_err = self._build_up_cmd()
            if build_err or not up_cmd:
                await self._set_runtime_state("error", build_err or "Invalid settings")
                return {"ok": False, "error": "invalid_settings", "up_stderr": build_err or ""}

            if not await self._ensure_daemon():
                return {"ok": False, "error": "daemon_not_ready"}

            down_result = await self._run_cmd(
                [self.tailscale_path, f"--socket={self.socket_path}", "down"],
                "tailscale down",
                return_result=True
            )
            await asyncio.sleep(1)

            await self._set_runtime_state("connecting", "Reconnecting to Tailscale...")

            up_result = await self._run_cmd(up_cmd, "tailscale up", return_result=True)
            await asyncio.sleep(2)
            await self._refresh_runtime_state()

            ok = bool(up_result and up_result.returncode == 0)
            return {
                "ok": ok,
                "down_stdout": down_result.stdout.strip() if down_result else "",
                "down_stderr": down_result.stderr.strip() if down_result else "",
                "up_stdout": up_result.stdout.strip() if up_result else "",
                "up_stderr": up_result.stderr.strip() if up_result else "",
            }

    async def api_refresh(self) -> dict[str, Any]:
        async with self._cmd_lock:
            await self._refresh_runtime_state()
            return await self.api_get_status()

    def _extract_accepted_routes(self, data: dict) -> list[str]:
        """Subnet routes learned from peers (requires --accept-routes=true)."""
        routes: set[str] = set()
        peers = data.get("Peer") or {}

        for peer in peers.values():
            if not isinstance(peer, dict):
                continue

            for key in ("PrimaryRoutes", "AllowedIPs"):
                values = peer.get(key) or []
                if isinstance(values, str):
                    values = [values]
                for cidr in values:
                    if isinstance(cidr, str) and _is_subnet_route(cidr):
                        routes.add(cidr)

        return sorted(routes)

    async def _refresh_runtime_state(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            await self._clear_backend_state()
            await self._set_runtime_state("error", self._daemon_error or "tailscaled is not running")
            return

        status_result = await self._run_cmd(
            [self.tailscale_path, f"--socket={self.socket_path}", "status", "--json"],
            "tailscale status --json",
            return_result=True
        )

        # Failed status queries must invalidate persisted values such as Running.
        if not status_result or status_result.returncode != 0:
            await self._clear_backend_state()
            detail = status_result.stderr.strip() if status_result else "Could not run tailscale status"
            await self._set_runtime_state("error", detail or "Could not read tailscale status")
            return

        ip_result = await self._run_cmd(
            [self.tailscale_path, f"--socket={self.socket_path}", "ip"],
            "tailscale ip",
            return_result=True
        )

        if not self.proc or self.proc.poll() is not None:
            await self._clear_backend_state()
            await self._set_runtime_state("error", "tailscaled stopped while refreshing status")
            return

        summary_lines = []
        runtime_state = "unknown"

        if status_result:
            summary_lines.append(f"status rc: {status_result.returncode}")

            if status_result.stdout:
                try:
                    data = json.loads(status_result.stdout)
                    backend_state = data.get("BackendState", "unknown")
                    self_node = data.get("Self", {}) or {}
                    dns_name = self_node.get("DNSName", "")
                    host_name = self_node.get("HostName", "")
                    online = self_node.get("Online", False)
                    # Self.AllowedIPs is ONLY this node's Tailscale addresses (/32,/128),
                    # never accepted subnet routes from other routers.
                    allowed_ips = self_node.get("AllowedIPs", []) or []
                    accepted_routes = self._extract_accepted_routes(data)
                    peer_count = len(data.get("Peer") or {})
                    tags = self_node.get("Tags") or []

                    summary_lines.append(f"BackendState: {backend_state}")
                    summary_lines.append(f"HostName: {host_name}")
                    summary_lines.append(f"DNSName: {dns_name}")
                    summary_lines.append(f"Online: {online}")
                    summary_lines.append(f"Self AllowedIPs: {', '.join(allowed_ips) if allowed_ips else '-'}")
                    summary_lines.append(
                        f"Accepted routes: {', '.join(accepted_routes) if accepted_routes else '-'}"
                    )
                    summary_lines.append(f"Tags: {', '.join(tags) if tags else '-'}")
                    summary_lines.append(f"Peers: {peer_count}")
                    summary_lines.append(
                        f"Accept routes setting: {_as_bool(self.homey.settings.get('accept_routes'))}"
                    )

                    if _as_bool(self.homey.settings.get("accept_routes")) and not accepted_routes:
                        summary_lines.append(
                            "Note: accept-routes is ON but no peer subnet routes are visible yet. "
                            "Check ACL grants, route approval in the admin console, and that the "
                            "subnet router is online."
                        )
                    if not _as_bool(self.homey.settings.get("accept_routes")):
                        summary_lines.append(
                            "Note: accept-routes is OFF. Enable it in settings and Reconnect "
                            "to receive subnet routes from other nodes."
                        )

                    await self.homey.settings.set("backend_state", backend_state)
                    await self.homey.settings.set("tailnet_hostname", host_name)
                    await self.homey.settings.set("tailnet_dns_name", dns_name)
                    await self.homey.settings.set("tailnet_online", online)
                    await self.homey.settings.set("tailnet_allowed_ips", allowed_ips)
                    await self.homey.settings.set("accepted_routes", accepted_routes)
                    await self.homey.settings.set("peer_count", peer_count)

                    backend_lower = str(backend_state).lower()
                    if backend_lower == "running":
                        runtime_state = "running"
                    elif backend_lower == "needslogin":
                        runtime_state = "needs_login"
                    elif backend_lower == "starting":
                        runtime_state = "starting"
                    elif backend_lower in ("stopped", "stopping"):
                        runtime_state = "disconnected"
                    else:
                        runtime_state = backend_lower

                except Exception as err:
                    await self._clear_backend_state()
                    summary_lines.append(f"JSON status parse error: {err}")
                    runtime_state = "error"
            else:
                await self._clear_backend_state()
                summary_lines.append("status stdout empty")
                if status_result.stderr:
                    summary_lines.append(status_result.stderr.strip())
                runtime_state = "error"
        else:
            summary_lines.append("Could not run tailscale status")
            runtime_state = "error"

        if ip_result and ip_result.returncode == 0 and runtime_state != "error":
            summary_lines.append(f"ip rc: {ip_result.returncode}")
            ip_lines = [line.strip() for line in ip_result.stdout.splitlines() if line.strip()]

            ipv4 = ""
            ipv6 = ""
            for line in ip_lines:
                if ":" in line:
                    ipv6 = line
                else:
                    ipv4 = line

            previous_ipv4 = self.homey.settings.get("tailscale_ipv4") or ""

            await self.homey.settings.set("tailscale_ipv4", ipv4)
            await self.homey.settings.set("tailscale_ipv6", ipv6)

            if previous_ipv4 and ipv4 and previous_ipv4 != ipv4:
                await self._trigger_ip_changed.trigger({
                    "ipv4": ipv4,
                    "ipv6": ipv6,
                })

            summary_lines.append(f"IPv4: {ipv4 or '-'}")
            summary_lines.append(f"IPv6: {ipv6 or '-'}")
        else:
            await self.homey.settings.set("tailscale_ipv4", "")
            await self.homey.settings.set("tailscale_ipv6", "")

        await self._save_status_text("\n".join(summary_lines) if summary_lines else "No data")
        await self._set_runtime_state(runtime_state)
        await self.push_state_to_devices()

    async def _clear_backend_state(self) -> None:
        for key, value in {
            "backend_state": "", "tailnet_hostname": "", "tailnet_dns_name": "",
            "tailnet_online": False, "tailscale_ipv4": "", "tailscale_ipv6": "",
            "tailnet_allowed_ips": [], "accepted_routes": [], "peer_count": 0,
        }.items():
            await self.homey.settings.set(key, value)

    async def _set_runtime_state(self, state: str, status_text: str | None = None) -> None:
        previous_state = self.homey.settings.get("runtime_state") or "unknown"
        current_ipv4 = self.homey.settings.get("tailscale_ipv4") or ""
        current_dns = self.homey.settings.get("tailnet_dns_name") or ""

        await self.homey.settings.set("runtime_state", state)

        if status_text is not None:
            await self._save_status_text(status_text)

        payload = {
            "state": state,
            "backend_state": self.homey.settings.get("backend_state") or "",
            "tailnet_hostname": self.homey.settings.get("tailnet_hostname") or "",
            "tailnet_dns_name": current_dns,
            "tailscale_ipv4": current_ipv4,
            "tailscale_ipv6": self.homey.settings.get("tailscale_ipv6") or "",
            "accepted_routes": self.homey.settings.get("accepted_routes") or [],
            "status_text": self.homey.settings.get("last_status_text") or "",
        }
        await self.homey.api.realtime("tailscale_status_changed", payload)

        if previous_state != state:
            if state == "running":
                await self._trigger_connected.trigger({
                    "ipv4": current_ipv4,
                    "dns_name": current_dns,
                })
            elif previous_state == "running" and state != "running":
                await self._trigger_disconnected.trigger({})
            elif state == "error":
                await self._trigger_error.trigger({
                    "message": self.homey.settings.get("last_status_text") or "Unknown error"
                })

        await self.push_state_to_devices()

    async def _save_status_text(self, text: str) -> None:
        await self.homey.settings.set("last_status_text", text)

    def build_device_snapshot(self) -> dict[str, str | bool]:
        backend_state = self.homey.settings.get("backend_state") or ""
        ipv4 = self.homey.settings.get("tailscale_ipv4") or ""
        ipv6 = self.homey.settings.get("tailscale_ipv6") or ""
        dns_name = self.homey.settings.get("tailnet_dns_name") or ""
        advertised = self.homey.settings.get("advertise_routes") or ""
        accepted = self.homey.settings.get("accepted_routes") or []
        runtime_state = self.homey.settings.get("runtime_state") or ""

        connected = str(runtime_state).lower() == "running"

        if accepted:
            routes_text = "accepted: " + ", ".join(accepted)
            if advertised:
                routes_text += f" | advertised: {advertised}"
        else:
            routes_text = advertised or "-"

        return {
            "connected": connected,
            "backend_state": backend_state,
            "ipv4": ipv4,
            "ipv6": ipv6,
            "dns_name": dns_name,
            "routes": routes_text,
        }

    async def push_state_to_devices(self) -> None:
        if not self.tailscale_status_driver:
            return

        snapshot = self.build_device_snapshot()

        for device in self.tailscale_status_driver.get_devices():
            try:
                await device.apply_snapshot(snapshot)
            except Exception as err:
                self.error(f"Error updating Tailscale Status device: {err}")

    async def _run_cmd(self, cmd: list[str], label: str, return_result: bool = False):
        try:
            safe_cmd = []
            for part in cmd:
                if "--auth-key=" in part:
                    safe_cmd.append("--auth-key=***REDACTED***")
                else:
                    safe_cmd.append(part)

            self.log(f"Running {label}: {' '.join(safe_cmd)}")

            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=45,
                env=os.environ.copy(),
            )

            self.log(f"{label} return code: {result.returncode}")
            if result.stdout.strip():
                self.log(f"{label} STDOUT: {result.stdout.strip()}")
            if result.stderr.strip():
                self.log(f"{label} STDERR: {result.stderr.strip()}")

            if return_result:
                return result

        except Exception as err:
            self.error(f"Error running {label}: {err}")
            if return_result:
                return None

    async def _read_stdout(self, proc) -> None:
        try:
            while proc.stdout:
                line = await asyncio.to_thread(proc.stdout.readline)
                if not line:
                    break
                self.log(f"[TAILSCALED STDOUT] {line.strip()}")
        except Exception as err:
            self.error(f"Error reading stdout: {err}")
        finally:
            if proc.stdout:
                proc.stdout.close()

    async def _read_stderr(self, proc, stderr_lines) -> None:
        fatal_markers = (
            "fatal",
            "panic:",
            "bind: address already in use",
            "no such file",
            "permission denied",
        )
        try:
            while proc.stderr:
                line = await asyncio.to_thread(proc.stderr.readline)
                if not line:
                    break
                text = line.strip()
                stderr_lines.append(text)
                lower = text.lower()
                if any(marker in lower for marker in fatal_markers):
                    self.error(f"[TAILSCALED STDERR] {text}")
                else:
                    # Routine magicsock/netmap/fakeRouter noise stays in normal logs
                    self.log(f"[TAILSCALED STDERR] {text}")
        except Exception as err:
            self.error(f"Error reading stderr: {err}")
        finally:
            if proc.stderr:
                proc.stderr.close()

    async def _flow_connect_listener(self, card_arguments, **trigger_kwargs):
        result = await self.api_connect()
        return bool(result.get("ok", False))

    async def _flow_disconnect_listener(self, card_arguments, **trigger_kwargs):
        result = await self.api_disconnect()
        return bool(result.get("ok", False))

    async def _flow_reconnect_listener(self, card_arguments, **trigger_kwargs):
        result = await self.api_reconnect()
        return bool(result.get("ok", False))

    async def _flow_refresh_listener(self, card_arguments, **trigger_kwargs):
        await self.api_refresh()
        return True

    async def _flow_is_connected_listener(self, card_arguments, **trigger_kwargs):
        runtime_state = self.homey.settings.get("runtime_state") or ""
        return runtime_state.lower() == "running"

    async def on_uninit(self) -> None:
        self.log("=== Tailscale for Homey | on_uninit ===")
        self._stopping = True

        tasks = [task for task in (self._watchdog_task, self._status_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        async with self._cmd_lock:
            await self._stop_daemon()


homey_export = App
