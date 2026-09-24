from homey import app
import asyncio
import subprocess
import platform
import os
import json
import ipaddress
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

    # Tailscale CGNAT / ULA node addresses are not "accepted subnet routes"
    if net.version == 4 and net.network_address in ipaddress.ip_network("100.64.0.0/10"):
        return False
    if net.version == 6 and str(net.network_address).startswith("fd7a:115c:a1e0:"):
        return False

    return True


class App(app.App):
    async def on_init(self) -> None:
        self.proc = None
        self._cmd_lock = asyncio.Lock()
        self.tailscale_status_driver = None

        self.auth_key = self.homey.settings.get("auth_key") or ""
        self.hostname = self.homey.settings.get("hostname") or "homey-pro"
        self.auto_connect = _as_bool(self.homey.settings.get("auto_connect"))
        self.accept_routes = _as_bool(self.homey.settings.get("accept_routes"))
        self.enable_subnet_router = _as_bool(self.homey.settings.get("enable_subnet_router"))
        self.advertise_routes = (self.homey.settings.get("advertise_routes") or "").strip()

        arch = platform.machine().lower()
        if arch in ("aarch64", "arm64"):
            bin_arch = "aarch64"
        else:
            bin_arch = "aarch64"
            self.error(
                f"Unsupported CPU architecture '{arch}'. "
                "This app currently ships aarch64 binaries (Homey Pro Early 2023+)."
            )

        self.tailscaled_path = f"/app/bin/{bin_arch}/tailscaled"
        self.tailscale_path = f"/app/bin/{bin_arch}/tailscale"

        self.state_dir = "/tmp/tailscale-homey"
        self.state_path = f"{self.state_dir}/tailscaled.state"
        self.socket_path = f"{self.state_dir}/tailscaled.sock"
        self.socks5_addr = "127.0.0.1:1055"
        self.http_proxy_addr = "127.0.0.1:1056"

        os.makedirs(self.state_dir, exist_ok=True)
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
        self.log(f"Architecture: {platform.machine()}")
        self.log(f"Hostname: {self.hostname}")
        self.log(f"Auto connect: {self.auto_connect}")
        self.log(f"Accept routes: {self.accept_routes}")
        self.log(f"Enable subnet router: {self.enable_subnet_router}")
        self.log(f"Advertise routes: {self.advertise_routes}")

        if self.enable_subnet_router and not self.advertise_routes:
            self.log("Subnet router enabled but no routes configured; nothing will be advertised")

        await self._save_status_text("Starting tailscaled...")
        await self._start_daemon()

        if self.auto_connect and self.auth_key:
            await self.api_connect()
        else:
            await self.api_refresh()

    def _reload_settings(self) -> None:
        self.auth_key = self.homey.settings.get("auth_key") or ""
        self.hostname = self.homey.settings.get("hostname") or "homey-pro"
        self.accept_routes = _as_bool(self.homey.settings.get("accept_routes"))
        self.enable_subnet_router = _as_bool(self.homey.settings.get("enable_subnet_router"))
        self.advertise_routes = (self.homey.settings.get("advertise_routes") or "").strip()

    def _build_up_cmd(self) -> list[str]:
        """Build `tailscale up` with explicit prefs so reconnect always applies UI toggles."""
        up_cmd = [
            self.tailscale_path,
            f"--socket={self.socket_path}",
            "up",
            f"--auth-key={self.auth_key}",
            f"--hostname={self.hostname}",
            "--accept-dns=false",
            f"--accept-routes={'true' if self.accept_routes else 'false'}",
        ]

        if self.enable_subnet_router and self.advertise_routes:
            cleaned = ",".join(
                part.strip() for part in self.advertise_routes.split(",") if part.strip()
            )
            up_cmd.append(f"--advertise-routes={cleaned}")
        else:
            # Clear any previously advertised routes when the toggle is off
            up_cmd.append("--advertise-routes=")

        return up_cmd

    async def _start_daemon(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.log("tailscaled already running")
            return

        # Homey apps cannot create a kernel TUN; userspace netstack + local proxies.
        cmd = [
            self.tailscaled_path,
            "--tun=userspace-networking",
            f"--state={self.state_path}",
            f"--socket={self.socket_path}",
            f"--socks5-server={self.socks5_addr}",
            f"--outbound-http-proxy-listen={self.http_proxy_addr}",
            "--verbose=1",
        ]

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )

        self.log(f"tailscaled PID: {self.proc.pid}")
        await self._set_runtime_state("starting", "Starting tailscaled...")

        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())
        asyncio.create_task(self._post_start_check())

    async def _post_start_check(self) -> None:
        await asyncio.sleep(5)

        if not self.proc:
            await self._set_runtime_state("error", "No tailscaled process")
            return

        rc = self.proc.poll()
        if rc is None:
            self.log("tailscaled still alive after 5 seconds")
            await self.homey.api.realtime("tailscale_status_changed", {
                "state": "daemon_running"
            })
        else:
            msg = f"tailscaled exited too early with code {rc}"
            self.error(msg)
            await self._set_runtime_state("error", msg)

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
            "last_status_text": self.homey.settings.get("last_status_text") or "No status yet.",
            "userspace_networking": True,
            "socks5_proxy": self.socks5_addr,
            "http_proxy": self.http_proxy_addr,
        }

    async def api_connect(self) -> dict[str, Any]:
        async with self._cmd_lock:
            self._reload_settings()

            if not self.auth_key:
                await self._set_runtime_state("error", "Auth key is empty")
                return {"ok": False, "error": "auth_key_empty"}

            if not self.proc or self.proc.poll() is not None:
                await self._start_daemon()
                await asyncio.sleep(3)

            await self._set_runtime_state("connecting", "Connecting to Tailscale...")

            result = await self._run_cmd(self._build_up_cmd(), "tailscale up", return_result=True)

            await asyncio.sleep(3)
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
            await asyncio.sleep(2)
            await self._refresh_runtime_state()
            return {
                "ok": bool(result and result.returncode == 0),
                "stdout": result.stdout.strip() if result else "",
                "stderr": result.stderr.strip() if result else "",
            }

    async def api_reconnect(self) -> dict[str, Any]:
        async with self._cmd_lock:
            down_result = await self._run_cmd(
                [self.tailscale_path, f"--socket={self.socket_path}", "down"],
                "tailscale down",
                return_result=True
            )
            await asyncio.sleep(2)

            self._reload_settings()

            if not self.auth_key:
                await self._set_runtime_state("error", "Auth key is empty")
                return {"ok": False, "error": "auth_key_empty"}

            await self._set_runtime_state("connecting", "Reconnecting to Tailscale...")

            up_result = await self._run_cmd(self._build_up_cmd(), "tailscale up", return_result=True)

            await asyncio.sleep(3)
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
        status_result = await self._run_cmd(
            [self.tailscale_path, f"--socket={self.socket_path}", "status", "--json"],
            "tailscale status --json",
            return_result=True
        )

        ip_result = await self._run_cmd(
            [self.tailscale_path, f"--socket={self.socket_path}", "ip"],
            "tailscale ip",
            return_result=True
        )

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

                    summary_lines.append(f"BackendState: {backend_state}")
                    summary_lines.append(f"HostName: {host_name}")
                    summary_lines.append(f"DNSName: {dns_name}")
                    summary_lines.append(f"Online: {online}")
                    summary_lines.append(f"Self AllowedIPs: {', '.join(allowed_ips) if allowed_ips else '-'}")
                    summary_lines.append(
                        f"Accepted routes: {', '.join(accepted_routes) if accepted_routes else '-'}"
                    )
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
                    summary_lines.append(f"JSON status parse error: {err}")
                    runtime_state = "error"
            else:
                summary_lines.append("status stdout empty")
                runtime_state = "error"

        if ip_result:
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

        await self._save_status_text("\n".join(summary_lines) if summary_lines else "No data")
        await self._set_runtime_state(runtime_state)
        await self.push_state_to_devices()

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

        connected = str(backend_state).lower() == "running" or str(runtime_state).lower() == "running"

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
                timeout=30,
                env=os.environ.copy(),
            )

            self.log(f"{label} return code: {result.returncode}")
            self.log(f"{label} STDOUT: {result.stdout.strip()}")
            self.log(f"{label} STDERR: {result.stderr.strip()}")

            if return_result:
                return result

        except Exception as err:
            self.error(f"Error running {label}: {err}")
            if return_result:
                return None

    async def _read_stdout(self) -> None:
        try:
            while self.proc and self.proc.stdout:
                line = await asyncio.to_thread(self.proc.stdout.readline)
                if not line:
                    break
                self.log(f"[TAILSCALED STDOUT] {line.strip()}")
        except Exception as err:
            self.error(f"Error reading stdout: {err}")

    async def _read_stderr(self) -> None:
        try:
            while self.proc and self.proc.stderr:
                line = await asyncio.to_thread(self.proc.stderr.readline)
                if not line:
                    break
                text = line.strip()
                # Expected with --tun=userspace-networking (no kernel router)
                if "fakeRouter.Set: not implemented" in text:
                    self.log(f"[TAILSCALED STDERR] {text}")
                else:
                    self.error(f"[TAILSCALED STDERR] {text}")
        except Exception as err:
            self.error(f"Error reading stderr: {err}")

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
        backend_state = self.homey.settings.get("backend_state") or ""
        runtime_state = self.homey.settings.get("runtime_state") or ""
        return backend_state.lower() == "running" or runtime_state.lower() == "running"

    async def on_uninit(self) -> None:
        self.log("=== Tailscale for Homey | on_uninit ===")

        if self.proc:
            try:
                self.log(f"Stopping process PID {self.proc.pid}")
                self.proc.terminate()
                await asyncio.to_thread(self.proc.wait, 5)
                self.log("Process stopped")
            except Exception as err:
                self.error(f"Error stopping process: {err}")


homey_export = App
