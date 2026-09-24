from homey import device


class TailscaleStatusDevice(device.Device):
    async def on_init(self) -> None:
        self.log(f"TailscaleStatusDevice initialized: {self.get_name()}")

        if hasattr(self.homey.app, "build_device_snapshot"):
            snapshot = self.homey.app.build_device_snapshot()
            await self.apply_snapshot(snapshot)

    async def on_added(self) -> None:
        self.log("TailscaleStatusDevice added")

        if hasattr(self.homey.app, "build_device_snapshot"):
            snapshot = self.homey.app.build_device_snapshot()
            await self.apply_snapshot(snapshot)

    async def apply_snapshot(self, snapshot: dict) -> None:
        connected = bool(snapshot.get("connected", False))
        backend_state = snapshot.get("backend_state", "") or "-"
        ipv4 = snapshot.get("ipv4", "") or "-"
        ipv6 = snapshot.get("ipv6", "") or "-"
        dns_name = snapshot.get("dns_name", "") or "-"
        routes = snapshot.get("routes", "") or "-"

        await self.set_capability_value("tailscale_connected", connected)
        await self.set_capability_value("tailscale_backend_state", backend_state)
        await self.set_capability_value("tailscale_ipv4", ipv4)
        await self.set_capability_value("tailscale_ipv6", ipv6)
        await self.set_capability_value("tailscale_dns_name", dns_name)
        await self.set_capability_value("tailscale_routes", routes)

        if connected:
            await self.set_available()
            await self.unset_warning()
            await self.set_last_seen_at()
        else:
            await self.set_warning("Tailscale is not connected")


homey_export = TailscaleStatusDevice
