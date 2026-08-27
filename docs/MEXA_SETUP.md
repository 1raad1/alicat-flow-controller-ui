# MEXA-584L reader and two-PC setup

The replacement reader acquires NO and O2 on the analyser PC, logs locally,
and streams to the flow-controller PC. It does not need the HORIBA executable.
Keep the original application as a fallback, but close it before this reader
opens the analyser's COM port.

Hardware validation is not complete. Automated tests use synthetic data and
fake serial replies. The read protocol was recovered from the supplied HORIBA
software; compare real readings and status behaviour before enabling the
optimiser. The validation checkbox records your confirmation, not a test the
software performs itself.

## Install on the analyser PC

1. Copy and extract the complete `MEXA-584L-bridge.zip` folder onto that PC.
   Do not run a launcher inside the ZIP. Alternatively, copy the full flow
   controller project; it contains the same reader.
2. Install 64-bit Python 3.11 or newer if needed. Run
   `install_mexa_bridge.bat`. This installs only PySide6 and pyserial into
   `%USERPROFILE%\.mexa-584l\venv`; it does not install the optimiser.
3. Start `run_mexa_bridge.bat`. Nothing connects until you click Start.
   On a PC with the existing flow-controller environment, the launcher can
   also use that environment without a separate installation.
4. Choose the analyser's COM port. **Refresh ports** lists Windows ports without
   probing instruments. The reader uses 9600 baud, 8 data bits, no parity,
   one stop bit, no handshaking, and DTR/RTS configured off before opening.
5. Set **Listen IPv4 address** to the analyser PC's address on your lab LAN.
   **Local IPv4…** lists active adapter addresses without probing the network.
   `127.0.0.1` is only for running both applications on the same PC. The
   default TCP port is `61234`. Leave it unchanged unless it conflicts.
6. Choose a local log folder, preferably outside OneDrive. The default is
   `%LOCALAPPDATA%\MEXA-584L\logs`. Each start creates new CSV and raw JSONL
   files, without overwriting previous runs.
7. Leave the two validation/basis checkboxes unchecked for commissioning.
   Use **Copy key** to transfer the shared key to the receiver. A new key is
   generated at each reader launch; it is not written into logs or preferences.
8. Click **Start reader and stream**. Use the instrument's front panel to
   select MEAS and perform its normal checks and calibration. This application
   sends only read queries, not MEAS, standby, calibration or pump commands.

The original HORIBA application must not be using the same serial port. Stop
this reader before reopening HORIBA. Stopping the reader leaves the instrument
in its existing mode; use the front panel for standby or shutdown.

## Connect the flow-controller PC

1. Restart the updated flow-controller application when safe for the rig.
2. Open **MEXA analyser**. Enter the analyser PC's IPv4 address, the same port,
   and the copied shared key. Choose the local received-data log folder.
3. Click **Connect MEXA**. The NO/O2 display updates when complete, valid
   readings arrive. Receiver CSV and raw JSONL logs start with the connection.
4. Start the normal flow CSV logger when needed. It includes MEXA values,
   timestamps, sequence IDs, sample age and quality flags alongside the flows.

Both PCs need a network route to each other. Some university Wi-Fi networks
isolate clients; use a lab network approved for instrument communication.
If Windows or university policy blocks the link, ask IT to allow inbound TCP
`61234` on the analyser PC from the flow-controller PC, on the appropriate
trusted network profile. Do not disable the firewall or expose this port to
the internet. The installer does not change firewall settings.

The stream uses a random challenge and HMAC-SHA256 authentication, including
per-record signatures tied to the connection. It is not encrypted. Use a
trusted LAN or an institution-managed secure network. Only one receiver is
served at a time. The network accepts no burner or analyser control commands.

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
authenticated** confirms a working route and matching key. **Bridge reached,
but authentication failed** points to the key or endpoint. A connection timeout
does not identify its cause: wrong IP, no listener, the PC firewall or university
network filtering can all prevent the connection.

If it is blocked, give departmental IT the requirement: one authenticated TCP
connection from the flow-controller PC to the analyser PC on port `61234`,
carrying approximately one measurement record per second, with no control
commands. Ask whether this is permitted over eduroam or should use an approved
wired lab network. If needed, an approved isolated wired link can carry the
measurements while Wi-Fi is used separately. Do not enable Internet Connection
Sharing, bridge networks, create a hotspot, disable the firewall, or install a
tunnel to work around UCL policy. No such network changes are made by this app.

Once a TCP route is available, the acquisition and optimiser code are unchanged.
If no direct route can be approved, the source logs remain usable, but a relay
or offline import would be a separate implementation; neither is included here.

## Validate before experimental use

- First test **Simulation only** with `127.0.0.1` on one PC, then over the lab
  LAN. Check that both displays say SIMULATION, both logs contain readings,
  and stopping the reader clears the receiver. Simulation cannot enter the
  experimental optimiser and does not validate the serial protocol.
- With the burner in a suitable safe commissioning state, use your approved
  analyser procedure to compare the reader with the instrument display and
  known calibration checks. Confirm NO in ppm and O2 in vol%, including
  channel presence, zero/span behaviour and the actual sampling configuration.
- Check warm-up, standby, known alarm indications and a disconnected serial
  cable. Invalid readings should be flagged and excluded, not converted to
  zero. Save the raw JSONL to investigate any mismatch. Do not validate a
  reader that disagrees with the instrument.
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
3. Select **Capture NO/O2 automatically from the MEXA network link**, confirm
   pilot off and settled, then click **Start window**.
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

The reader keeps logging locally if the network drops. The receiver retries
the connection but resumes with current samples only; it does not insert an
outage backlog into a new live window. Use source IDs and sequence numbers to
match the two logs when investigating gaps.

CSV files contain decoded readings and quality flags. Raw JSONL additionally
contains all three serial replies in hexadecimal. Records are flushed on each
sample and fsynced on normal close. A reader log-write failure stops publication;
a receiver log-write failure stops reception and invalidates its active window.
An unexpected power failure can still lose recent OS-buffered data.

In the combined flow CSV, `mexa_new_sample=False` means the same recent value
was held across another flow row, or no valid value was available. Do not count
held rows as independent analyser samples. `mexa_valid` describes acquisition
quality, not suitability for an experiment: also inspect `mexa_simulated`,
`mexa_validated` and `mexa_basis`. Invalid or stale values are blank in the
combined CSV; the raw analyser logs retain the diagnostic record.

Keep the source/receiver logs with the `.fcbo.json` campaign. The campaign stores
window statistics and audit references, not a copy of every serial frame.
Moving a log requires preserving that association manually.

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
