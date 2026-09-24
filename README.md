# Tailscale Connect for Homey Pro

Secure remote access to Homey Pro through your Tailscale tailnet. Manage the connection from Homey settings, monitor status, use Flow cards, and optionally accept or advertise subnet routes.

**Supported hardware:** Homey Pro (Early 2023+, aarch64) and Homey Pro (2019, armv7).  
**Note:** Homey Pro 2019 has less RAM — keep auto-connect on and avoid advertising large subnet sets if the Homey feels sluggish.

---

## Quick start (English)

1. Install **Tailscale Connect** from the Homey App Store.
2. In the [Tailscale admin console](https://login.tailscale.com/admin/settings/keys), create an **auth key** (reusable is fine for Homey).
3. Open the app settings in Homey:
   - Paste the **Auth key**
   - Set a **Hostname** (e.g. `homey-pro`)
   - Enable **Connect automatically on start** (recommended)
4. Tap **Save settings**, then **Connect**.
5. Confirm the node appears online in the Tailscale admin console and that the settings panel shows **Connected** with a Tailscale IPv4 (`100.x.y.z`).

You can now reach Homey at its Tailscale IP from any device on your tailnet.

### Accept subnet routes (reach a remote LAN via another router)

If another machine on your tailnet advertises a subnet (e.g. `192.168.3.0/24`):

1. Approve that route in the Tailscale admin console.
2. Ensure your ACL / grants allow this Homey (or its tag) to use that route.
3. In Homey settings, enable **Accept routes advertised by other nodes**.
4. Optionally set **Advertise tags** (e.g. `tag:homey`) so grants-based ACLs can match Homey.
5. **Save & Reconnect**.
6. Check **Accepted subnet routes** in the status panel — you should see the CIDR(s), not only Homey’s own `/32`.

**Important:** Homey’s own address list (`Self AllowedIPs`) is always just its Tailscale IPs. Accepted routes appear under **Accepted subnet routes** (learned from peers). Looking only at Homey’s `/32` in the admin console is expected and does **not** mean routes failed.

### Advertise Homey’s local subnet (experimental)

Homey runs Tailscale in **userspace** mode. Advertising Homey’s LAN as a subnet router is **experimental** and may not forward traffic reliably. Prefer a NAS/PC as subnet router. Reaching Homey via its `100.x` IP is the supported use case.

If you still want to try:

1. Enable **Advertise subnet(s) from this Homey**.
2. Enter CIDR(s), e.g. `192.168.1.0/24`.
3. Save & Reconnect → approve the route in the admin console.

### About userspace networking

Homey apps cannot create a kernel TUN device. This app runs Tailscale with `--tun=userspace-networking`.

- Access **to Homey’s Tailscale IP** from other nodes works.
- Local SOCKS5 (`127.0.0.1:1055`) and HTTP (`127.0.0.1:1056`) proxies are shown in the status panel.
- Log lines like `fakeRouter.Set: not implemented` are **normal**.
- The app keeps `tailscaled` alive with a watchdog and refreshes status every 30s.

### Troubleshooting

| Symptom | What to check |
|--------|----------------|
| Never connects | Auth key valid and **reusable**? Tap Connect — errors now show in an alert. |
| Connected but no subnet routes | **Accept routes** on? **Save & Reconnect**? Route approved? ACL/tags OK? |
| Only see `/32` AllowedIPs | Expected for Homey’s own node. Check **Accepted subnet routes**. |
| Homey Pro 2019 | Supported (armv7). If it feels slow, disable subnet advertise and keep only remote access to Homey. |

---

## Guía rápida (Español)

1. Instala **Tailscale Connect** desde la Homey App Store.
2. En la [consola de Tailscale](https://login.tailscale.com/admin/settings/keys), crea una **auth key**.
3. En los ajustes de la app en Homey:
   - Pega la **Auth key**
   - Pon un **Hostname** (por ejemplo `homey-pro`)
   - Activa **Conectar automáticamente al arrancar**
4. Pulsa **Guardar ajustes** y luego **Conectar**.
5. Comprueba que el nodo aparece online en Tailscale y que el panel muestra **Conectado** con una IPv4 `100.x.y.z`.

### Aceptar rutas de subred

1. Aprueba la ruta del subnet router en la consola de Tailscale.
2. Revisa que el ACL / grants permitan a Homey (o su tag) usar esa ruta.
3. Activa **Aceptar rutas anunciadas por otros nodos**.
4. **Guardar** → **Reconectar**.
5. Revisa **Rutas de subred aceptadas** en el panel de estado.

### Anunciar la LAN de Homey

1. Activa **Anunciar subred(es)**.
2. Introduce el CIDR, p. ej. `192.168.1.0/24`.
3. Guardar → Reconectar → aprueba la ruta en la consola.

### Si Homey no conecta

- Auth key correcta y no caducada.
- Tras cambiar rutas o hostname, siempre **Reconectar**.
- El mensaje `fakeRouter.Set: not implemented` en los logs es normal (modo userspace).
- Homey Pro (2019) está soportado (armv7); si va justo de RAM, no anuncies subredes.

---

## Flows

Triggers, conditions and actions are available for connect / disconnect / reconnect / refresh and connection state. See the Homey Flow editor after installing the app.

## Development

- Runtime: Homey SDK 3, Python
- Binaries: `bin/aarch64/` (Homey Pro Early 2023+) and `bin/armv7/` (Homey Pro 2019), Tailscale 1.96.2
- Architecture is selected at runtime via `platform.machine()`
- Settings UI: `settings/index.html` (English by default; Spanish if Homey language is `es`)
