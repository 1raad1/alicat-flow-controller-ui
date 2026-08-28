# Temporary relay on the flow-controller PC

The flow app can start a private MEXA relay and publish it through Wormhole
(`wormhole.bar`, not Magic Wormhole). Wormhole is the default; Cloudflare Quick
Tunnel is an alternative. No home server, account, domain or router port
forwarding is needed. The receiver connects locally. The analyser PC sends
measurements to the public WSS address.

Temporary tunnels have no guaranteed uptime. Keep the flow PC awake and test
from both PCs before an experiment. For sustained use, arrange a stable,
approved endpoint using the [separate relay guide](MEXA_RELAY.md).

## Start a session

1. Restart the updated flow app. The analyser bridge needs its existing
   **Internet relay (outbound WSS)** option; no Wormhole installation is needed
   on the analyser PC. Close the original HORIBA app before the replacement
   bridge takes ownership of the COM port.
2. Copy the analyser bridge's **Shared key**. This authenticates measurements
   and is different from the relay publisher key.
3. In the flow app's **MEXA analyser** tab, select **Host temporary relay on
   this PC**. Leave **Tunnel provider** on **Wormhole (wormhole.bar, port 443)**.
   Paste the shared key. Choose whether to save received logs and select a
   folder if logging is on. Live optimiser capture requires receiver logs.
4. Confirm UCL permits the service and the transfer of your data. Tick
   **Allow temporary publishing through Wormhole**, then click
   **Start temporary relay**. The app prepares the helper and connects the
   receiver to its private relay once the tunnel registers.
5. Click **Copy URL** and paste it into the analyser bridge's Relay URL. Click
   **Copy publisher key** and paste it into the bridge's publisher-key field.
   Select **Internet relay (outbound WSS)** on the bridge, not Direct LAN.
6. Start the bridge with **Simulation only** first. Confirm fresh readings at
   the flow app. A registered tunnel alone does not prove that the analyser
   PC can reach it. Simulation cannot be used for live optimisation.
7. Stop the bridge before switching from simulation to real acquisition.
   Check sampling suitability and real readings before confirming hardware
   validation. This network setup does not validate NH3/H2 exhaust measurements.

The analyser PC and flow PC can save logs independently. Fresh out-of-range
readings and all available channels still arrive, with invalid flags intact.
Invalid and simulated readings remain excluded from live optimisation.

## Stopping and interruptions

**Stop temporary relay** closes the receiver, private relay and helper.
Closing the flow app also stops its host. These actions add no burner commands.
An interruption invalidates live capture; the operator must restart capture
after the connection recovers. Stale readings cannot become optimiser inputs.

The sender retries when its connection drops. If the Wormhole helper
re-registers with a different public address, the flow app displays the new
URL and asks you to copy it to the bridge and reconnect. The publisher key
stays the same during that host session. Stop/start creates fresh keys and a
new address, so copy both again. If recovery times out, start a fresh session.

Changing provider clears consent and a manually selected helper path. Stop
the current host before changing provider. The app never switches providers
automatically.

## Helper installation

Leave **Tunnel helper** blank for automatic installation on Windows x64.
The app downloads the official Wormhole v0.2.1 ZIP (3.6 MB) from GitHub,
verifies its SHA-256, and copies only `wormhole.exe` from it. It then verifies
the executable's size and SHA-256. Unexpected downloads are not executed.

```text
ZIP: 2ce5e4ae45044231d31d42f221bbe1dee4af3b1f434c286d82f15ac540e8e0a7
EXE: 7ecd85e1c545871f39ac0a4c64ffc06280f21bd7532aa127c6782c8816ae95ef
```

The helper is cached at
`%USERPROFILE%\.flow-controller-v3\tools\wormhole-0.2.1\wormhole.exe`.
Each start checks the cached executable. A mismatched existing file is not
overwritten. The app does not install a service, change PATH, request admin
rights or configure startup at login. Wormhole runs anonymously, with its
traffic inspector disabled. Existing Wormhole account configuration is unused.

On another platform, or if downloads are restricted, obtain the executable
from the [official Wormhole release](https://github.com/MuhammadHananAsghar/wormhole/releases/tag/v0.2.1)
and use **Select helper**. Manually selected helpers are not checked against
the pinned Windows hash. Only select an executable you trust; its updates
are your responsibility.

## Network access and privacy

Wormhole needs approved outbound HTTPS/WSS on TCP 443. The flow PC connects
to `relay.wormhole.bar`; the analyser connects to the generated
`*.wormhole.bar` address. GitHub HTTPS access is needed for automatic helper
download. The flow app listens only on `127.0.0.1`, never on the Wi-Fi address.
No inbound firewall rule or port forwarding is required on either PC.

If startup fails, ask IT to review these destinations and outbound WebSocket
access. Network policies can differ between PCs or change later. Do not
disable certificate verification, change DNS to evade a block, or disable
Windows Firewall. A working browser or Jump connection is not proof that a
tunnel service is permitted.

The relay requires separate random publisher and receiver keys. The receiver
key stays inside the flow app. The analyser shared key signs samples and is
never sent to the tunnel provider. Keys are not saved in preferences,
measurement logs or helper arguments/environment. Clear the clipboard after
pasting keys. The public URL alone is not an access-control mechanism.

Wormhole and its hosting infrastructure terminate TLS and can see forwarded
measurements and the publisher access key. Sample signatures protect
authenticity, not confidentiality from the provider. Confirm data-transfer
approval before sending real measurements. The local relay saves no data;
the two apps retain their independent logging choices.

## Cloudflare alternative

Select **Cloudflare Quick Tunnel (port 7844)** and give separate consent.
The flow PC needs outbound TCP 7844, plus HTTPS for registration and download.
The analyser needs HTTPS/WSS 443 to `*.trycloudflare.com`. The helper uses
HTTP/2, so UDP is not required. This option downloads `cloudflared` 2026.8.2
(55 MB) and checks SHA-256:

```text
c29eee2b121f5436a642eed69fd9767da7e7b8c510fa50aaa130337f931357b5
```

Its cache is `%USERPROFILE%\.flow-controller-v3\tools\cloudflared-2026.8.2\cloudflared.exe`.
Existing Cloudflare configuration is untouched; the helper uses a private
temporary configuration and a loopback-only metrics endpoint. Stop removes
the temporary configuration, not the cached executable.

If the app reports `blocked-due-to-malware.ucl.ac.uk`, ask IT to review
`api.trycloudflare.com`, `*.trycloudflare.com` and Cloudflare's tunnel endpoints.
Certificate verification remains enabled. Cloudflare can see forwarded data.

References: [Wormhole client](https://github.com/MuhammadHananAsghar/wormhole/tree/v0.2.1),
[Cloudflare Quick Tunnel limitations](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/),
[Cloudflare firewall requirements](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/).
