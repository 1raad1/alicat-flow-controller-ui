# Temporary relay on the flow-controller PC

The flow app starts an internal loopback MEXA relay and publishes it through
Wormhole (`wormhole.bar`, not Magic Wormhole). No third PC, hosting account,
domain or router port forwarding is needed. The receiver connects locally.
The analyser PC sends measurements to the public WSS address. The two
connection choices are Wormhole and Direct LAN; Direct LAN is the backup on
a trusted, approved network. See [MEXA setup](MEXA_SETUP.md) for that route.

Temporary tunnels have no guaranteed uptime. Keep the flow PC awake. A
registered tunnel does not establish that the analyser PC can reach it or
that measurements are suitable for experimental use.

## Start a session

1. Restart the updated flow app. Use **Wormhole (outbound WSS)** in the analyser
   bridge. The existing **Internet relay (outbound WSS)** option, including the
   28 August bridge package, works unchanged; no reinstall or Wormhole helper
   is needed on the analyser PC. Close the original HORIBA app before the
   replacement bridge takes ownership of the COM port.
2. Copy the analyser bridge's **Shared key**. This authenticates measurements
   and is different from the relay publisher key.
3. In the flow app's **MEXA analyser** tab, select **Wormhole (temporary hosting
   on this PC)**.
   Paste the shared key. Choose whether to save received logs and select a
   folder if logging is on. Live optimiser capture requires receiver logs.
4. Confirm UCL permits the service and the transfer of your data. Tick
   **Allow temporary publishing through Wormhole**, then click
   **Start temporary relay**. The app prepares the helper and connects the
   receiver to its private relay once the tunnel registers.
5. Click **Copy URL** and paste it into the analyser bridge's Relay URL. Click
   **Copy publisher key** and paste it into the bridge's publisher-key field.
   Select **Wormhole (outbound WSS)** on the bridge, or its older **Internet
   relay (outbound WSS)** label, not Direct LAN.
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

There is no provider selector or external receiver URL/key setup. Stop the
current connection before changing between Wormhole and Direct LAN.

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
the pinned Windows hash. Only select an executable you trust.

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

Each connection pair uses a fresh receiver challenge and HMAC-SHA256 sample
signatures, so a previous session's record cannot be replayed as a new
measurement. The relay accepts one publisher and one receiver, refuses
browser-origin connections and rejects commands. MEAS/STANDBY remain confirmed
local actions in the analyser bridge. Do not enable packet/frame debug logging;
authentication frames contain access keys.

Clients use the operating system's configured proxy where supported. A SOCKS
proxy may need the optional `python-socks[asyncio]` dependency, which is not
included. Ask IT for the approved route if proxy connection fails; do not
disable certificate checks.

## Reconnects and troubleshooting

The sender retries after an outage with a bounded delay up to 15 seconds. A
peer disconnect closes the pair and requires a fresh handshake. The bridge
sends only readings acquired after pairing; it does not upload saved logs or
replay an outage backlog. Enable source logging if you need those readings
retained on the analyser PC.

Sequence gaps, duplicate records, changed sources, stale/future timestamps,
authentication failures and disconnects interrupt live capture. The normal
five-second freshness limit and one-second future-clock tolerance still apply.
Resettle and start a new live window after recovery. Reconnection never changes
flows or resumes an experiment.

- **Cannot reach Wormhole:** check the app's startup error and ask IT about
  approved outbound WSS access to the destinations above. Do not change DNS or
  firewall settings to evade a block.
- **Relay access denied:** copy the publisher key from the active flow-app
  session to the analyser bridge. It is not the analyser shared key.
- **Role already connected:** stop the other bridge using that session. The
  relay does not silently replace an existing connection.
- **Waiting for the other PC:** confirm the bridge is running with the current
  URL and publisher key. A restarted host has new credentials.
- **Shared-key authentication failed:** copy the key from the current bridge
  window into the receiver. A bridge relaunch generates a new shared key.
- **Certificate verification failed:** ask IT to investigate the certificate
  chain, hostname or PC clock. Do not use an insecure override.
- **Stale/future samples:** synchronise PC clocks and investigate latency or
  suspended applications. Do not extend the age limit to accept delayed data.

If a publisher key is exposed, stop the temporary relay and start a new session,
then copy the new URL and publisher key. If the analyser shared key is exposed,
relaunch the bridge and copy its new key to the receiver. These are independent
keys. Follow [instrument validation and optimiser guidance](MEXA_SETUP.md)
before using real measurements.

Implementation reference: [Wormhole v0.2.1 client](https://github.com/MuhammadHananAsghar/wormhole/tree/v0.2.1).
