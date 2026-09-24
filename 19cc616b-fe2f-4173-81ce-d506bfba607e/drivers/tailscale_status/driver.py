from homey import driver
from homey.driver import ListDeviceProperties


class TailscaleStatusDriver(driver.Driver):
    async def on_init(self) -> None:
        self.log("TailscaleStatusDriver inicializado")
        self.homey.app.tailscale_status_driver = self

    async def on_pair_list_devices(self, view_data: dict) -> list[ListDeviceProperties]:
        self.log(f"on_pair_list_devices llamado con view_data={view_data}")

        devices = [
            {
                "name": "Tailscale Status",
                "data": {
                    "id": "tailscale-status-main"
                }
            }
        ]

        self.log(f"Devolviendo devices para pairing: {devices}")
        return devices


homey_export = TailscaleStatusDriver