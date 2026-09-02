# MEXA-584L reader and two-PC setup

The replacement reader acquires all channels exposed by the original MEXA
application and streams them to the flow-controller PC. Either PC can save
logs independently. Keep the reader
running to acquire and stream data. The original HORIBA application must stay
closed while the reader uses the analyser's COM port.

Choose **Wormhole (temporary hosting on this PC)** in the flow app and **Wormhole
(outbound WSS)** in the analyser bridge for the internet route. The flow app
runs its own loopback relay and pinned Wormhole helper; no third PC is needed.
Older bridges labelled **Internet relay (outbound WSS)** work unchanged.
See [Wormhole setup](MEXA_QUICK_TUNNEL.md). Use this only where UCL permits the
tunnel and data transfer. **Direct LAN** is the backup for a trusted, approved
network. The IPv4/TCP instructions below apply to Direct LAN only.

Hardware validation is not complete. Automated tests use synthetic data and
fake serial replies. The read protocol was recovered from the supplied HORIBA
software; compare real readings and status behaviour before enabling the
optimiser. The validation checkbox records your confirmation, not a test the
software performs itself.

## Install on the analyser PC

Already using the 28 August `MEXA-584L-bridge-wormhole.zip`? Keep that installation.
This flow-app update does not change its serial commands, measurement format,
or network authentication. New receiver-side
summary fields are calculated on the flow-controller PC. You do not need to
reinstall the bridge for those changes.

New builds keep the reader in a standalone `mexa_bridge/` package, outside
`flow_controller/`. The bridge ZIP contains no burner-control code. Extract a
new build into its own folder; do not mix its files with an older installation.
Use the launchers included in that folder. The new module entrypoint is
`python -m mexa_bridge.app`; older ZIPs keep their original launchers.

1. Copy and extract the latest MEXA-584L bridge ZIP onto that PC.
   Do not run a launcher inside the ZIP. Alternatively, copy the full flow
   controller project; it contains the same reader.
2. Install 64-bit Python 3.11 or newer if needed. Run
   `install_mexa_bridge.bat`. This installs PySide6, pyserial and websockets into
   `%USERPROFILE%\.mexa-584l\venv`; it does not install the optimiser.
3. Start `run_mexa_bridge.bat`. Nothing connects until you click Start.
   On a PC with the existing flow-controller environment, the launcher can
   also use that environment without a separate installation.
4. Choose the analyser's COM port. **Refresh ports** lists Windows ports without
   probing instruments. The reader uses 9600 baud, 8 data bits, no parity,
   one stop bit, no handshaking, and DTR/RTS configured off before opening.
5. For **Direct LAN**, set **Listen IPv4 address** to the analyser PC's address on your lab LAN.
   **Local IPv4…** lists active adapter addresses without probing the network.
   `127.0.0.1` is only for running both applications on the same PC. The
   default TCP port is `61234`. Leave it unchanged unless it conflicts.
6. Leave **Save CSV + raw logs on this PC** off to stream without saving
   files. This is the default. For a local backup, enable it and choose a
   folder, preferably outside OneDrive. The default folder is
   `%LOCALAPPDATA%\MEXA-584L\logs`. Each start with logging enabled creates
   new CSV and raw JSONL files without overwriting previous runs. Stop the
   reader before changing this choice.
7. Leave the two validation/basis checkboxes unchecked for commissioning.
   Use **Copy key** to transfer the shared key to the receiver. A new key is
   generated at each reader launch; it is not written into logs or preferences.
8. Click **Start reader and stream**. This starts acquisition, not MEAS.
   Select MEAS on the front panel or use the local mode controls below.
   Perform calibration and normal instrument checks on the front panel.

The original HORIBA application must not be using the same serial port. Stop
this reader before reopening HORIBA. Stopping the reader leaves the instrument
in its existing mode; use the front panel for standby or shutdown.

### Local MEAS and STANDBY controls

Select **Enable local analyser mode controls**, then choose **MEAS…** or
**STANDBY…** and confirm the request. These controls are on the analyser PC
only. No network command can operate the analyser or burner.

MEAS requires a fresh standby status; STANDBY requires a fresh measuring
status. Both are disabled during warm-up, missing/stale status or a pending
command. An out-of-range channel does not by itself prevent a mode change.
Commands run between read cycles on the same serial connection. There are no
automatic mode changes on startup, reconnection, stopping or closing the reader.

An ACK means the analyser acknowledged the request, not that the requested
mode has been reached. Check the subsequent reported state and front panel.
If a request fails or times out, its outcome is uncertain. The reader reports
the failure and does not retry it; check the instrument before trying again.
Calibration, zero/span settings and separate pump controls are not implemented.

Every mode request invalidates live optimiser capture and clears serial
validation for the remainder of that reader run. After checking the new mode,
stop the reader, repeat the required instrument checks, confirm validation and
restart. Re-settle before starting a new optimiser window. Starting/stopping
the reader does not itself return the analyser to MEAS or STANDBY.

## Connect with Direct LAN

For Wormhole, follow [Wormhole setup](MEXA_QUICK_TUNNEL.md) instead. The logging,
measurement and validation guidance below applies to both modes.

1. Restart the updated flow-controller application when safe for the rig.
2. Open **MEXA analyser** and select **Direct LAN** in both apps.
   Enter the analyser PC's IPv4 address, the same port,
   and the copied shared key. **Save received MEXA logs on this PC** is on
   by default; choose a folder, or turn it off if you only need the display
   or combined flow CSV. Live optimiser capture requires it to be on.
3. Click **Connect MEXA**. The NO/O2 display updates when fresh readings arrive,
   including out-of-range values labelled INVALID. Receiver CSV and raw JSONL
   logs start with the connection only if selected. Disconnect before changing
   the logging choice.
4. Start the normal flow CSV logger when needed. It includes MEXA values,
   timestamps, sequence IDs, sample age and quality flags alongside the flows.
   It is independent of both MEXA logging switches.

For receiver-only logging, leave the analyser PC's logging switch off and the
receiver's switch on. If both are off, neither creates standalone MEXA logs;
the normal flow CSV still records MEXA values if you start that logger.

### Channels and metadata

Both displays show NO/O2 and a separate panel of other reported channels.
The stream, source and receiver CSV/JSONL logs, and combined flow CSV carry:

| Field | Unit / meaning |
| --- | --- |
| NO | ppm, not total NOx |
| O2, CO, CO2 | vol% |
| HC | ppm as reported by the instrument |
| AFR | Analyser-reported air-to-fuel ratio |
| Lambda | Analyser-reported excess-air ratio |
| RPM | Optional engine-speed sensor, rpm |
| Oil temperature | Optional sensor, degrees Celsius; not exhaust temperature |
| PEF | Reported PEF factor; not reapplied to HC |

Records also carry timestamps, source/sequence IDs, option-presence flags,
acquisition duration, analyser state, alarms, warnings and raw replies. RPM,
temperature, NO and O2 are blank if their option is absent. Nothing substitutes
zero or a previous reading for an unavailable channel.

PEF is a separate read query. A failed PEF request leaves PEF blank and adds
`pef_unavailable` plus the query error; the channel frame is retained. Normal
freshness and acquisition-duration checks still apply. PEF raw bytes use the
separate `raw_pef` field so the original three-frame `raw` object remains
compatible with older receivers.

The analyser's AFR/lambda are calculated for its automotive application.
Do not interpret them as NH3/H2 burner equivalence ratios or replace the
flow controller's flow-based phi values with them. HC is an instrument-reported
hydrocarbon signal, not an ammonia measurement. The optimiser still uses NO/O2;
its validity flag does not validate the other channels for an experiment.
[HORIBA's specifications](https://www.horiba.com/aut/mobility/products/detail/action/show/Product/mexa-584l-120/)
describe the automotive AFR/lambda calculations and optional sensors.

New receivers still accept older bridge packets; channels those packets do
not contain display as blank. Update both PCs to display and save the full set.

In Direct LAN mode, both PCs need a network route to each other. Some university Wi-Fi networks
isolate clients; use a lab network approved for instrument communication.
If Windows or university policy blocks the link, ask IT to allow inbound TCP
`61234` on the analyser PC from the flow-controller PC, on the appropriate
trusted network profile. Do not disable the firewall or expose this port to
the internet. The installer does not change firewall settings.

The Direct LAN stream uses a random challenge and HMAC-SHA256 authentication, including
per-record signatures tied to the connection. It is not encrypted. Use a
trusted LAN or an institution-managed secure network. Only one receiver is
served at a time. The network accepts no burner or analyser control commands.

### Out-of-range readings versus a failed connection

Network status and measurement quality are displayed separately. A connected
stream can carry invalid measurements. An **Analyser not ready** warning is
not itself a network error; a **TCP connection timed out** error is separate.

Fresh out-of-range values are shown on both PCs with an INVALID label and the
affected channel's limits. The software checks NO against 0–5000 ppm and O2
against 0–25%. Negative or excessive reported values are not clipped to zero
or accepted by the optimiser. These values are for diagnostics: they do not
extend the analyser's measurement range. Compare with the front panel and
saved serial frames before interpreting them. Missing channels and stale data
still display dashes.

Out-of-range records are streamed without requiring valid NO/O2 measurements
and are saved by whichever MEXA loggers you enable. The combined flow CSV
retains all fresh channel values in its `mexa_reported_` columns, alongside
`mexa_quality` and `mexa_valid=False`. Its original `mexa_no_ppm` and
`mexa_o2_percent` columns remain blank for invalid data. Live optimisation
continues to reject those records.

For a Direct LAN connection timeout (Wormhole diagnostics are in the [Wormhole guide](MEXA_QUICK_TUNNEL.md)):

1. Check that the bridge is started. Its listener label shows the bound address
   and port. `127.0.0.1` only accepts a receiver on the same PC. Use the analyser
   PC's active Wi-Fi/LAN IPv4 in both apps, not the receiving PC's address.
2. On the analyser PC, run `Get-NetTCPConnection -LocalPort 61234 -State Listen`
   in PowerShell. The local address must match the selected adapter address.
   If no listener exists, resolve the bridge's startup error first.
3. From the receiving PC, test that exact address and port:

   ```powershell
   $mexaAddress = Read-Host "Analyser PC IPv4"
   Test-NetConnection -ComputerName $mexaAddress -Port 61234
   ```

4. If TCP fails despite a matching listener, investigate the analyser PC's
   inbound firewall, the receiver's outbound policy and network filtering.
   A shared-key change cannot fix a failure before authentication. Ask IT to
   permit only the receiving PC to reach the analyser PC on TCP 61234, on the
   active network profile. Do not disable the firewall or bypass managed policy.
5. If TCP succeeds but authentication fails, copy the key from the currently
   running bridge. Check that another receiver is not already connected.

Windows can classify an eduroam connection as Public. An allow rule limited
to the Private profile would then not apply. Firewall rules should be scoped
to the required addresses and port, and approved for the actual profile.
Recheck both addresses after reconnecting to Wi-Fi.

### Using UCL eduroam

The application does not need your eduroam username or password. Both PCs join
eduroam through Windows as usual; the bridge's shared key is a separate key for
this measurement link.

[UCL's Wi-Fi FAQ](https://www.ucl.ac.uk/isd/services/get-connected/wi-fi/wi-fi-help/wi-fi-frequently-asked-questions-faqs)
states that addresses are assigned by DHCP. The public guidance does not confirm
whether arbitrary peer-to-peer TCP connections are allowed between your two
PCs. That must be checked on the network you actually use.

Choose the analyser PC's connected Wi-Fi adapter using **Local IPv4…**, then
enter that address in the receiver. Do not use an address from a web search for
"my IP": that is generally the network's public/NAT address, not this PC's
adapter address. Recheck the adapter address after reconnecting to Wi-Fi.

Test with simulation before involving the analyser. A message saying **Bridge
authenticated** confirms a working route and matching key. **TCP reached**
followed by a handshake/authentication failure points to the key, endpoint or
another connected receiver. A connection timeout
does not identify its cause: wrong IP, no listener, the PC firewall or university
network filtering can all prevent the connection.

If it is blocked, give departmental IT the requirement: one authenticated TCP
connection from the flow-controller PC to the analyser PC on port `61234`,
carrying approximately one measurement record per second, with no control
commands. Ask whether this is permitted over eduroam or should use an approved
wired lab network. If needed, an approved isolated wired link can carry the
measurements while Wi-Fi is used separately. Do not enable Internet Connection
Sharing, bridge networks, create a hotspot, disable the firewall, or install a
tunnel to work around UCL policy. The app does not change firewall rules or
network adapters; its optional Wormhole tunnel must be permitted by UCL.

If no direct route can be approved, ask IT whether the app's
[Wormhole route](MEXA_QUICK_TUNNEL.md) is permitted. Both PCs connect outward on
port 443; the relay runs inside the flow app. Do not use it to evade network
policy. Enable source logging to retain readings during outages. Offline log
import is not implemented.

## Validate before experimental use

- First test **Simulation only** with `127.0.0.1` on one PC, then over the lab
  LAN. Check that both displays say SIMULATION and stopping the reader clears
  the receiver. With logging enabled, check the saved readings on each PC.
  With it off, check that no log files are created. Simulation cannot enter
  the experimental optimiser and does not validate the serial protocol. Test
  simulated MEAS/STANDBY transitions, including confirmation and validation reset.
- With the burner in a suitable safe commissioning state, use your approved
  analyser procedure to compare the reader with the instrument display and
  known calibration checks. Confirm NO in ppm and O2 in vol%, including
  channel presence, zero/span behaviour and the actual sampling configuration.
  Compare the additional channels and PEF with the original application or
  front panel separately. Stop the bridge before opening the original program.
- Check warm-up, standby, known alarm indications and a disconnected serial
  cable. Invalid readings should be flagged and excluded, not converted to
  zero. Enable logging during these checks so the raw JSONL is available to
  investigate any mismatch. Do not validate a reader that disagrees with the
  instrument.
- Commission MEAS/STANDBY separately from burner experiments. Verify each
  acknowledged request against the front panel and subsequent status. Check
  that rejected or timed-out requests are reported without automatic retries.
- Confirm with HORIBA or your validated method that the NO/O2 channels and
  sample-conditioning system are suitable for NH3/H2 combustion exhaust.
  Streaming does not resolve cross-sensitivity, ammonia slip, sample losses
  or calibration bias. NO is not total NOx, and this analyser does not provide
  the NH3/N2O constraints needed to establish overall emissions performance.
- Confirm the readings are uncorrected and on the dry basis required by the
  optimiser. A water trap alone is not a software-verifiable reporting basis.
- Synchronise both PCs' clocks. Aim for agreement within one second. Samples
  must arrive no more than five seconds old and no more than one second in the
  future; receive-side monotonic time also guards against a frozen stream.
- Determine the sample-line transport and analyser response/settling time for
  your setup. The acquisition timestamp is when the analyser PC finishes its
  read cycle, not when gas left the burner. The app does not infer or subtract
  that physical delay. Hold each condition until the complete sampling system
  has settled, then start the measurement window.

After these checks, stop the reader, confirm the dry-basis and serial-validation
checkboxes, and restart it. Both confirmations apply to that reader run. Recheck
the setup after changing sensors, sampling equipment, calibration or wiring.

## Use live data in the optimiser

1. Create/open a campaign and obtain a suggestion as usual. Review and apply
   flows with the existing controls, following your transition procedure.
2. Switch off the pilot and allow the burner and analyser to settle.
3. Connect the MEXA with **Save received MEXA logs on this PC** enabled.
   Source logging on the analyser PC is optional. Select **Capture NO/O2
   automatically from the MEXA network link**, confirm pilot off and settled,
   then click **Start window**.
4. Keep the condition steady. **Finish window** requires three or more new
   analyser records and three or more flow passes. Both streams must cover
   the campaign's minimum duration. Allow a few extra seconds because the
   analyser and flow polls do not occur at exactly the same instant.
5. Review the automatically filled NO/O2 means, add notes and confirm the
   reporting basis. **Save result** adds the oxygen-corrected result to the
   optimiser. The captured means are read-only and cannot be replaced by
   typing different values into a live measurement.

The live objective uses the arithmetic NO and O2 means over the selected
analyser samples, then applies the same oxygen correction as manual entry.
Individual samples count once, not once per flow-log row. The record includes
standard deviations, ranges, source/sequence IDs and the receiver log path.
Standard deviation is not treated as standard error because successive sensor
readings may be strongly correlated.

A gap, duplicate sequence, restart, stale reading or reported fault discards
an active live window. Re-settle and start a new one after recovery. Nothing
automatically changes flows, zeros the burner or starts the next experiment.
The existing emergency controls and physical interlocks remain your means of
controlling the rig. Manual entry remains available by leaving live capture
off before starting a new window; a failed live window never silently switches
to manual mode.

## Logs and reconnects

With source logging enabled, the reader keeps saving locally if the network
drops. With it off, readings missed during an outage are not retained and
cannot be recovered. The receiver retries the connection but resumes with
current samples only; it does not insert an outage backlog into a new live
window. If both PCs save logs, use source IDs and sequence numbers to match
them when investigating gaps.

When logging is enabled, CSV files contain every reported channel, quality
flags and raw reply columns. JSONL also retains the complete structured packet.
The three status/channel replies and the separate PEF reply are hexadecimal.
Local mode-request records have blank channels, `valid=False`, and a `control`
object containing the requested mode, phase, reply bytes and outcome detail.
They are retained in source JSONL when enabled and in receiver JSONL if received.
Normal readings are nominally at most 1 Hz; mode requests add diagnostic records.
These records can interrupt/restart a live stream just like other invalid data.
Records are flushed on each
sample and fsynced on normal close. A reader log-write failure stops publication;
a receiver log-write failure stops reception and invalidates its active window.
An unexpected power failure can still lose recent OS-buffered data.

In the combined flow CSV, `mexa_new_sample=False` means the same recent value
was held across another flow row, or no valid value was available. Do not count
held rows as independent analyser samples. `mexa_valid` describes acquisition
quality, not suitability for an experiment: also inspect `mexa_simulated`,
`mexa_validated` and `mexa_basis`. Invalid values are blank in the original
NO/O2 columns; fresh reported values remain in the diagnostic `mexa_reported_`
columns. Stale or future values are blank in both sets. Raw analyser logs retain
the diagnostic record if enabled.

The combined flow CSV includes `mexa_reported_` columns for all ten numeric
fields in the table, plus `mexa_options`, `mexa_cycle_s`, `mexa_alarms`,
`mexa_warnings`, `mexa_pef_error` and `mexa_raw_` reply columns. These are
snapshots at flow-poll times, not a replacement for the per-record MEXA logs.

Keep the receiver logs with the `.fcbo.json` campaign, and any source logs saved
as a backup. The campaign stores window statistics and audit references, not
a copy of every serial frame. Moving a log requires preserving that association
manually.

## Protocol provenance

The implementation was checked against the managed code in the supplied
`MEXA584L.exe`, HORIBA version 1.0.0.0, SHA-256:

```text
4F6D8BA04303992CEB8149B3F31E6CA6C7FC0CED5CD60B4EC06286E8C9B64112
```

Read requests are analyser status `02 01 01 FC`, subsystem status
`02 01 AA 53`, and channel data `02 01 40 BD`. Replies must match the expected
ACK, command ID, exact length and additive two's-complement checksum. NO uses
the signed big-endian value at channel offsets 21–22; O2 uses offsets 11–12
divided by 100. Option-presence bits are checked on every cycle. No old channel
value is reused if a cycle fails. Nominal acquisition is at most 1 Hz, not a
claim about independent sensor response time.

Other channel offsets follow `ResponseParsers.ParseForData`: CO2 at 5 /100,
CO at 7 /100, HC at 9, AFR at 13 /10, lambda at 15 /1000, RPM at 23 and oil
temperature at 25. All use signed big-endian 16-bit values. Subsystem byte 6
uses masks 1, 2, 4 and 8 for O2, NO, RPM and temperature presence respectively.
The remaining frame bytes are retained as raw data without invented meanings.
Return PEF is `02 03 18 00 00 E3`, with a six-byte ACK and 230 ms delay; the
signed value at reply offsets 3–4 is divided by 1000, matching
`PrecisionThreeDatum.FromChannelDatum` in the original application.

Local mode requests were also recovered from `CommandFactory.GetCommand`:
MEAS `02 01 A6 57` and STANDBY `02 01 A7 56`, with a 240 ms execution delay.
`ResponseParsers.CheckResponse` requires a four-byte ACK, matching command ID
and checksum. Five-byte NAK replies are retained for diagnostics and rejected.
The telemetry protocol identifier retains its original `readonly` name for
compatibility; the network remains receive-only for commands. Only the local
operator can request a mode change.

Known warm-up, calibration-error, leak, hang, filter, temperature and RPM alarm
flags follow the legacy parser. Its automotive probe warning means CO2 below
1% while measuring. That warning is preserved, but does not reject carbon-free
NH3/H2 samples. It is not evidence that sampling is adequate. Unmapped instrument
states, protocol variants and calibration-in-progress behaviour require the
hardware checks above; this is not a certified HORIBA replacement or safety
interlock.

[HORIBA's product page](https://www.horiba.com/aut/mobility/products/detail/action/show/Product/mexa-584l-120/)
lists RS-232C acquisition and the optional NO/O2 channels. It does not certify
this replacement reader or its suitability for your burner.
