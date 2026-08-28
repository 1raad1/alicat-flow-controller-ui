# MEXA relay host for your CachyOS home PC

This is a small terminal program, not the flow-controller app. It forwards
measurements between the two lab PCs. It needs Python and one Python package
(`websockets`); it does not install Qt, serial drivers or the optimiser.

The host has no analyser or burner controls and saves no measurements. Both
lab PCs still connect outward. Your home PC supplies the separate internet
endpoint, so it must stay powered on, connected and awake during measurements.

## 1. Install the host

Extract `MEXA-584L-relay-cachyos.zip` into a permanent folder on the home PC.
Open a terminal in the extracted `MEXA-584L-relay` folder.

CachyOS uses the Arch package manager. If Python and Caddy are not installed,
review and run this normal system-update/package-install command:

```bash
sudo pacman -Syu --needed python caddy
```

It can update other system packages as part of the transaction; review what
pacman proposes. The relay's scripts do not run pacman or sudo themselves.
[CachyOS package guidance](https://wiki.cachyos.org/cachyos_basic/faq/)
and [the Caddy package](https://archlinux.org/packages/extra/x86_64/caddy/).

Then, as your normal user:

```bash
bash install_relay_host.sh
bash run_relay_host.sh setup
```

Setup asks for a public DNS hostname you control, such as
`relay.your-domain.net`. It does not register a domain, discover a public IP,
set up DNS or change your router. Use a real hostname, not the example, your
home PC's `192.168.x.x` address, or either lab PC's eduroam address.

The installer creates a `.venv` inside the extracted folder. Setup writes to
`~/.config/mexa-relay/` (or `$XDG_CONFIG_HOME/mexa-relay` when set):

- `host.json`: hostname, backend port and two persistent role keys; mode 600.
- `Caddyfile`: the HTTPS site block to add to your Caddy configuration.
- `mexa-relay.service`: an optional user-service definition; not installed or
  enabled automatically.

The directory has mode 700. Do not upload `host.json` or show it in screenshots.
Rerunning setup preserves the keys, including when you change the hostname.
The ZIP contains no preconfigured keys. Use the host on a Linux filesystem;
its private-permission checks may reject FAT or other non-POSIX storage.

## 2. Make the home PC reachable securely

The host program alone cannot make a private home address reachable from the
internet. This setup uses your router plus Caddy's HTTPS endpoint:

1. Arrange a public IPv4 address that permits incoming connections. A dynamic
   address is fine if you keep its DNS record updated, manually or with your
   chosen dynamic-DNS service. You do not need to buy a static address just
   for this software.
2. Point the hostname's DNS A record to that public IPv4 address. Only publish
   an AAAA record if incoming IPv6 is also correctly configured.
3. Reserve a stable LAN address for the home PC in the router. Configure TCP
   port forwarding for **80 and 443** from your home router to Caddy on that
   home PC, and permit those ports through the home PC's firewall. Do not
   forward 8765 or the old bridge port 61234. Do not disable the firewall,
   enable a router DMZ, or enable automatic UPnP mapping for this program.
4. Copy the generated site block from `~/.config/mexa-relay/Caddyfile` into
   `/etc/caddy/Caddyfile`, using `sudoedit /etc/caddy/Caddyfile`. If Caddy
   already serves anything, merge the block; do not overwrite existing sites.
   Then validate and start/reload Caddy:

   ```bash
   sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   sudo systemctl enable --now caddy
   sudo systemctl reload caddy
   ```

Caddy obtains and renews the certificate when DNS and incoming access are
correct. This standard setup uses ports 80/443 for HTTPS and certificate
validation. [Caddy HTTPS requirements](https://caddyserver.com/docs/quick-starts/https).
The Python relay remains on `127.0.0.1:8765`; Caddy forwards `/mexa` to it.
Do not expose that plaintext backend directly.

### If your ISP uses carrier-grade NAT

Check the router's **WAN/internet address**, not the address shown by `ip addr`
on your PC. A WAN address in `100.64.0.0`–`100.127.255.255` indicates shared
address space commonly used for CGNAT. Private WAN addresses such as `10.x.x.x`
or `192.168.x.x` can indicate another layer of NAT. Ask your ISP to confirm
whether unsolicited incoming connections can reach your router.
[Shared-address range](https://www.rfc-editor.org/info/rfc6598/).

Forwarding a port on your own router cannot by itself configure the ISP's
upstream NAT. If incoming connections are unavailable, ask for a public address
or use an approved hosted relay/tunnel instead. This package does not install
a tunnel. Do not spend time changing the lab apps' keys to fix CGNAT.

You must also be permitted to send your experimental data through the home
host, and UCL must allow outbound WSS to its hostname. The home host can see
forwarded measurements because it terminates the TLS connections.

## 3. Start it and connect the lab PCs

From the extracted host folder:

```bash
bash run_relay_host.sh run
```

Leave this terminal open. The host shows whether the analyser and receiver
are connected. Ctrl+C stops it. Connection status here means relay access was
authenticated; the lab apps separately check measurement signatures and quality.

In another terminal in the same folder:

```bash
bash run_relay_host.sh show-keys
```

This explicitly displays the URL and two keys for copying. The normal running
host does not print keys to its log.

| Setting | Analyser-PC bridge | Flow-controller MEXA tab |
| --- | --- | --- |
| Mode | Internet relay (outbound WSS) | Internet relay (outbound WSS) |
| Relay URL | The host's `wss://hostname/mexa` | Same URL |
| Role key | Publisher key | Receiver key |
| Shared key | Generated by bridge | Copy from current bridge window |

The analyser shared key is separate from the host's two access keys. The home
host does not need it. Update both Windows apps and their dependencies before
using relay mode; the old LAN-only app cannot use a WSS URL.

Test **Simulation only** first. From outside your home network, open
`https://your-hostname/healthz`. It should say `MEXA relay running`. This checks
public HTTPS reachability, not analyser readiness or keys. Test the two lab
apps through that URL before connecting the real instrument.

`bash run_relay_host.sh status` checks only the local backend. A successful
local check does not prove that the router, DNS or HTTPS front end works.

## Optional: keep the host running without an open terminal

After foreground operation works, stop it with Ctrl+C to free the port.
The setup-generated service runs as your normal user and contains no keys:

```bash
systemctl --user link "$HOME/.config/mexa-relay/mexa-relay.service"
systemctl --user enable --now mexa-relay.service
systemctl --user status mexa-relay.service
```

Adjust that path if you use `XDG_CONFIG_HOME`. To keep the user service running
after logout and start it at boot, explicitly enable lingering for your user:

```bash
sudo loginctl enable-linger "$USER"
```

This does not prevent the PC from sleeping. Configure your intended power
settings separately. Caddy must also remain running. To stop the relay:

```bash
systemctl --user stop mexa-relay.service
```

Use `systemctl --user disable --now mexa-relay.service` to turn off its
auto-start. If you move the extracted folder, rerun setup from the new location
to regenerate the absolute service paths, then run
`systemctl --user daemon-reload` before restarting it. On CachyOS, a major
system Python upgrade may require rerunning the installer to refresh `.venv`.

## Measurements and outages

The host never uploads old logs or replays an outage backlog. When an internet
link drops, enabled source logging on the analyser PC continues. After
reconnection, the receiver accepts new readings with fresh authentication.
An interrupted optimiser window must be restarted; flows never change because
of a reconnect. Keep both lab PC clocks synchronised.

No real-instrument validation is implied by hosting successfully. Follow the
existing MEXA commissioning procedure, keep MEAS/STANDBY controls local, and
verify NO/O2 suitability for your experiment. NO is not total NOx.

For protocol/security details and other hosting options, see
[the full relay guide](docs/MEXA_RELAY.md).
