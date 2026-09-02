# Alicat Flow Controller

Control, monitor, sequence, and log addressed Alicat mass flow controllers on a
shared serial bus from a Windows desktop.

The application supports general multi-channel control and an
ammonia/hydrogen rich-quench-lean (RQL) burner rig. Use **Standard** mode for
ordinary setpoints. Use **Staged (RQL)** mode for stage-aware assignments, flow
calculations, and combustion estimates.

> [!WARNING]
> This is a supervisory control interface, not a safety system. Physical
> interlocks and independent emergency shutdown protection must not depend on
> Windows, Python, or the USB serial connection. Closing the application does
> not make a controller forget its last setpoint; use **ZERO ALL** before
> shutdown when the process requires it.

The desktop interface is built with PySide6 and Qt.

## Start here

| I want to... | Read... |
| --- | --- |
| Install and open the application | [Quick start](#quick-start) |
| Find and connect the controllers | [First connection](#first-connection) |
| Choose Standard or Staged mode | [Operating modes](#operating-modes) |
| Understand setpoints, ramps, or zero commands | [Controls and safety behavior](#controls-and-safety-behavior) |
| Record or replay a run | [Sequences](#sequences) |
| Search for low NO with live or manual MEXA readings | [Bayesian optimiser](#bayesian-optimiser) |
| Stream the analyser from another PC | [MEXA two-PC setup](docs/MEXA_SETUP.md) |
| Stream through a separate internet relay | [Relay setup and hosting](docs/MEXA_RELAY.md) |
| Host a temporary tunnel from the flow-controller app | [Quick Tunnel setup](docs/MEXA_QUICK_TUNNEL.md) |
| Host the relay on a CachyOS home PC | [Small host program and setup](CACHYOS_START_HERE.md) |
| Log data, plot history, or use the LabVIEW trigger | [Logging, graphs, and LabVIEW](#logging-graphs-and-labview) |
| Check the RQL equations and constants | [Combustion calculations](#combustion-calculations) |
| Work on the code | [Development](#development) |
| Fix an installation or connection problem | [Troubleshooting](#troubleshooting) |

## Operating modes

The application starts in **Standard** mode on every launch. Connecting a full
RQL assignment does not switch modes automatically.

| Mode | Use it for | Calculations |
| --- | --- | --- |
| **Standard** | General multi-channel control | Totals all assigned controllers by gas |
| **Staged (RQL)** | Stage 1, Stage 2, and pilot operation | Uses each controller's gas and stage role |

### Standard

Standard mode provides:

- individual and batch setpoints;
- live flow, setpoint, pressure, temperature, setpoint-error, and valve-drive
  telemetry where supported by the controller;
- CSV logging and retained-history export;
- live graphs; and
- aggregate equivalence ratio, firing rate, and inlet bulk velocity.

Every assigned controller participates in monitoring and logging. A controller
assigned to **General**, or to a gas and zone pair without an RQL meaning, is
left out of stage-aware calculations.

### Staged (RQL)

Staged mode groups controllers by stage:

| Zone | Supported gases |
| --- | --- |
| Stage 1 | NH3, H2, CH4, Air |
| Stage 2 | NH3, H2, CH4, Air |
| Pilot | NH3, H2, or CH4 (one pilot line) |

CH4 assigned directly to Stage 1 or Stage 2 is included in that stage's live
combustion estimate. The selected pilot fuel is also included in Stage 1. Automatic RQL
target calculation still uses its established seven required lines:

Auto-calculation is enabled for either of these assignments:

- **Full RQL:** NH3, H2, and Air in both stages, plus one NH3, H2, or CH4 pilot.
- **Rich + quench-air:** Stage 1 fuels and air, Stage 2 air, plus one NH3, H2,
  or CH4 pilot.

The calculator accepts firing rate, hydrogen fraction by volume, Stage 1 fuel
split, Stage 1 equivalence ratio, and global equivalence ratio. It stores the
calculated target flows but **does not send them**. Review the targets before
using **SET ALL FLOWS**. It rejects impossible requests, such as a Stage 1
mixture leaner than the requested global mixture, instead of producing a
negative flow.

## Interface overview

| Tab | What you do there |
| --- | --- |
| **Connection & Assignment** | Choose serial settings, scan the bus, inspect gas tables, assign roles, connect controllers, and check live telemetry. |
| **Operation & Monitoring** | Enter setpoints, set ramps and display scales, start logs, calculate RQL targets, run sequences, and read the system log. |
| **Logging & Graphs** | Plot flow, setpoint, pressure, temperature, internal setpoint error, or valve drive, then export retained history. |

You can reassign zones after connection unless a CSV log is open. Log columns
are fixed when recording starts.

Graph history begins accumulating when monitoring starts, even if no series is
selected. Leaving the graph tab stops rendering but does not stop acquisition.

Axis limits can be automatic or fixed. Automatic axes use hysteresis so a
rising trace does not continually rescale beneath the operator. The default
history limit is 3,600 samples.

## Controls and safety behavior

### Batch controls

These controls are available from every tab:

| Control | Action |
| --- | --- |
| **SET ALL FLOWS** | Queue the setpoint shown on every controller card. |
| **ZERO FUEL** | Zero every assigned controller whose gas name is not exactly `Air`. |
| **ZERO ALL** | Zero every assigned controller. |

### Command and connection safety

Two rules are enforced in the control layer:

1. A zero command outranks all pending or new nonzero setpoints for its target
   units.
2. Only the monitoring loop writes to hardware. Typed setpoints, batch sends,
   ramps, and sequence replay all pass through the same queue and interlocks.

Zero commands keep the serial connection and monitoring active so the result
can be verified. They also cancel active ramps and sequence replay. Stopping
monitoring or closing the application is not a substitute for zeroing the rig.

After ten consecutive read timeouts from one unit, the application treats the
port as unresponsive and restarts monitoring. **Reconnect** closes and reopens
the connections while preserving assignments, history, and confirmed
setpoints.

### Controller settings

Open the hamburger menu on a live controller card to edit these settings:

| Setting | Effect |
| --- | --- |
| **FULL SCALE** | Sets the SLPM span of the card's bar. It is a display setting, not a hardware limit. Enter the value printed on the meter, or use `auto` or `0` for an automatic span. |
| **MAX FLOW** | Declares the largest setpoint this application may command to the controller. It applies at enqueue and again immediately before hardware writes/restores. Lowering it below a live last-commanded value requests verified zero for that controller. `none` removes the declaration. |
| **RAMP** | Limits command changes in SLPM/s. It applies to typed, batch, calculated, and replayed setpoints. |
| **OFF** | Disables application-side ramping for that controller, including the built-in minimum move time for air and pilot lines. New controllers start in this state. |

Ramping defaults to **OFF**, so setpoints are written as steps. To use a ramp,
enter a rate and clear the red **OFF** latch. The rate remains editable while
OFF, but it does not take effect until the latch is cleared. With ramping
enabled but no rate declared, pilot and air lines still use the built-in
minimum ten-second move.

## Sequences

A recorded sequence stores what the application was asked to do, not the
measured response of the rig.

### Record

1. Start monitoring.
2. Expand **Record / Replay Sequence** and click **Record**.
3. Send setpoints. Each command becomes a keyframe.
4. Stop recording. The application saves a `.fcseq.json` file under
   `Documents\Flow Controller\sequences`.

### Edit

- Drag a point to move it.
- Double-click empty space to add a point.
- Right-click a point to enter its exact time, value, and transition type. The
  available transitions are step, linear, and smooth.
- Shift-click to select a continuous group. Ctrl-click to add or remove one
  point from the selection.
- **Smooth selected** eases the transitions inside the selection. **Smooth
  whole sequence** applies the same no-overshoot easing to every transition.
- **Slower** and **Speed up** change every track and timeline marker together.
  Each 1.2x step is reversible with the opposite button.

In **All tracks (overview)**, fuel setpoints use the left y-axis and air
setpoints use the right y-axis. The separate scales keep the larger air range
from flattening the fuel traces.

### Replay

Click a saved sequence to load it without moving the rig. The adjacent play
button loads and runs it once.

Replay uses the current assignment, so a role recorded on controller `A` can
later run on controller `D`. Replay is refused if a required role is missing.
Before starting, measured flows are compared with the sequence's opening
setpoints; the operator must resolve or explicitly override any mismatch.

During replay:

- **Hold if flows lag** pauses every track until lagging measurements catch up.
  The maximum hold is 30 seconds.
- Repeats ramp from the final values back to the opening values. They do not
  start again as an unprotected jump.

Zero commands, stopping monitoring, and application shutdown cancel replay.

## Bayesian optimiser

The **Bayesian optimiser** replaces the Agent launcher in the Operation sidebar.
The desktop app no longer launches an agent terminal or starts its IPC gateway.
The optimiser runs locally; it needs neither an API key nor an internet connection.
Legacy agent modules remain in the repository but are not mounted by the app.
For the algorithms, equations, file format and code map, see the
[Bayesian optimiser technical manual](docs/BAYESIAN_OPTIMISER_MANUAL.md).

### Create and run an experiment

1. Expand **Bayesian optimiser** and choose **New experiment**. Enter the nominal
   NH3/H2 thermal input, stage-1 fuel split, and permitted bounds for H2 volume
   percentage, stage-1 phi and overall phi. Optionally select thermal input or
   stage-1 fuel split to add them as fourth and fifth search variables, then enter
   their bounds. Unselected values remain fixed. Bounds are intentionally blank.
   This version supports stage-1 phi >= 1 and overall phi < 1, with both fuels
   present. Every assigned pilot fuel line must be off during measurements.
2. Choose the dry O2 reporting reference (default 15%, not a regulatory claim),
   initial-design size (default 16 completed tests), candidate-pool size and
   minimum averaging window (default 30 seconds). **Use current O₂**, beside the
   reference field, copies one fresh, validated, uncorrected dry MEXA reading;
   receiver logging must be enabled. You can edit it before saving. It does not
   follow later readings or change burner flows. The initial design must contain
   at least one more completed test than the number of variables. Save a new
   `.fcbo.json` experiment. These settings are fixed for the campaign; use a new
   file to change them.
3. Click **Suggest next test**, then **Load target fields**. This fills the
   existing flow fields, including zero for the pilot and unused lines. It
   sends no commands. Review every field and the transition procedure before
   applying through the usual controls. Existing MAX FLOW limits and ramps
   still apply. Do not assume that safe endpoints imply a safe transition.
4. After switching the pilot off and allowing the burner, sample line and
   analyser to settle, check both confirmations. For live measurements, connect
   the bridge in the **MEXA analyser** tab with **Save received MEXA logs on
   this PC** enabled, then select **Capture NO/O2 automatically**. Click
   **Start window**. With live capture off, average
   the analyser's uncorrected dry NO and O2 manually over that window.
5. Click **Finish window** after both streams cover the minimum duration.
   Live capture fills and locks the NO/O2 means; manual mode lets you enter
   those means and an optional NO standard error. Add notes, confirm the
   uncorrected dry basis, then **Save result**. No flows change on save.
6. Suggest the next test. Use the **History** tab to inspect results, repeat
   a completed point, or export CSV. Repeating a point creates a separate test.

The **NO response time** tab can store two settled live conditions and run one
explicitly confirmed A-to-B transition. It measures the combined burner, flow,
sample-line, analyser and acquisition response, then applies the selected result
as a pre-averaging delay for later live MEXA windows. It does not alter the
campaign's averaging duration, and cancellation does not return the rig to A or
zero it. See [NO response-time calibration](docs/BAYESIAN_OPTIMISER_MANUAL.md#5-no-response-time-calibration)
for the procedure, detector criteria, timing definitions and saved provenance.

The optimiser requires one NH3 line, one H2 line and one air line in stage 1,
plus stage-2 air. Stage-2 fuel lines are required when a fixed or proposed fuel
split is below 100%. A pilot controller may remain assigned at zero or be unassigned.
All other assigned gas lines must read off during measurement.

### Measurement basis and limits

The objective is oxygen-corrected dry **NO**, not total NOx or mass per energy:

```text
corrected_NO = raw_dry_NO * (20.9 - reference_O2) / (20.9 - measured_dry_O2)
```

The 20.9% value is the separate [EPA oxygen-correction reporting
convention](https://www.epa.gov/sites/default/files/2017-09/documents/10-6200.pdf).
It is deliberately not replaced by the 20.9390% physical O2 mole fraction used
in the combustion stoichiometry.

This avoids optimising raw ppm merely by adding air. It does not measure NO2,
NH3 slip, N2O or combustion efficiency. Confirm the MEXA sensor's calibration,
sample conditioning and suitability for the NH3/H2 exhaust matrix before making
emissions claims. Do not enter an already oxygen-corrected reading. Readings at
or above 20.9% O2 cannot be corrected; readings close to air concentration amplify
measurement errors. NO input is limited to the published 0–5000 ppm range.

Fresh flow and setpoint readings must track targets within the larger of 3%
or 0.05 SLPM. When power is fixed, measured thermal input must be within 3% of
the campaign setting; when it is searched, the measured value must remain inside
the declared power bounds.
These are data-acceptance tolerances, not safety limits or proof of a stable
flame. The operator confirms that the pilot is off. Missing telemetry, a gap in
polling, a run/configuration change or a non-tracking flow discards an active
window. At least three fresh passes and the configured duration are required.
One capture cannot exceed an hour.

Live MEXA capture also requires at least three new analyser samples, spanning
the configured minimum duration inside the flow window. A disconnect, source
restart, missing sequence number, invalid reading or stale stream discards the
capture. Readings older than five seconds, PC clocks more than one second ahead,
and acquisition cycles over three seconds are rejected. Keep both PC clocks
synchronised and account for the sample-line and sensor settling time before
starting. Simulation, an unknown reporting basis, or an unvalidated serial
reader cannot feed the optimiser. See [MEXA setup](docs/MEXA_SETUP.md).

Live results use the arithmetic mean of each channel, followed by oxygen
correction of those means, consistent with manual entry. Each analyser record
counts once regardless of the flow polling rate. Sample standard deviations,
ranges, sequence IDs and the receiver audit-log path are saved. Sensor samples
may be autocorrelated, so the standard deviation is not converted into an
assumed standard error; the model still fits observation noise.

**Discard window** and **Mark test invalid** do not stop or zero the burner.
Use the existing flow and emergency controls for the physical process. Invalid
tests retain their reason but have no numerical emissions result; they are
excluded from fitting rather than treated as zero emissions.

### Model and records

The initial design selects spread-out points from an N-dimensional scrambled
Sobol candidate pool. Subsequent suggestions fit a Matérn-5/2 Gaussian process and maximise
Monte Carlo noisy expected improvement over a feasible candidate pool. The model
uses measured blend, equivalence ratios, and any selected power or split variables
from the captured flow window, with the requested settings retained alongside them.
It fits residual observation
noise and accepts an optional per-test NO standard error. O2 uncertainty and
systematic calibration bias are not propagated. Suggestions respect the declared
search region and current flow ceilings; no flame-safety boundary is learned.

The `.fcbo.json` campaign is the authoritative record. Experiment changes are
written atomically after each suggestion, completed window and result. Every
newly completed or invalid condition receives a schema-1 `condition_log` inside
that JSON. A condition log is a self-contained per-condition audit record: it
freezes the trial and suggestion provenance, requested variables and target
flows, separate controller-setpoint and measured-flow statistics, assignments,
rig contexts and audit-log path, all available MEXA channel statistics and
receiver provenance, the corrected result or invalid reason, and the response
calibration summary used for that window.
Auxiliary MEXA channels are informational and are not separately validated.
The calibration summary stores the raw sample count and SHA-256 digest rather
than copying its high-frequency raw samples.

CSV export is an on-demand view of the campaign. It includes flattened
provenance fields, JSON cells for nested values and a full compact
`condition_log_json` cell. Raw high-frequency flow and MEXA data remain in their
separate audit files; keep them alongside the authoritative campaign JSON when
sample-level reconstruction is needed. A continuous flow-file path is present
only when the app's flow logger was active during that window.

**Open** resumes a saved campaign, including a pending test, without loading
target fields or applying flows. An unfinished capture is not resumed after
closing the app. Typed measurement text is not durable until **Save result**; it
does survive an appearance refresh. The history identifies the lowest observed
corrected NO, not a certified global minimum. Repeat promising points and
reference conditions to check reproducibility and drift.

The model runs in a background worker so fitting does not block the Qt control
interface. Campaigns are limited to 500 tests. The implementation uses
scikit-learn and SciPy; run `install.bat` when upgrading another installation.

### Sequencing in operation

The following 90-second excerpt shows a recorded sequence being replayed while
the controller interface and burner response are monitored. The demonstration
is shown at 2x speed.

https://github.com/user-attachments/assets/5711b1a9-1fce-4921-918c-9869ef5d3f1e

## Quick start

### Install and run on Windows

1. Double-click `install.bat`. You only need to do this once.
2. Double-click `run.bat` to start the application.

The installer puts the virtual environment in
`%USERPROFILE%\.flow-controller-v3\venv`. This keeps PySide6's deeply nested
files out of OneDrive and avoids common Windows path-length failures.

To run from PowerShell instead:

```powershell
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" run.py
```

`python -m flow_controller` is equivalent.

### Requirements

- Windows 10 or 11
- 64-bit Python 3.11 or newer
- A USB-to-serial adapter that matches the rig's electrical interface. RS-232
  and RS-485 are not interchangeable.
- Alicat controllers with unique single-letter addresses

### Manual installation

The pinned `requirements.txt` installs the application plus Excel-export
support:

```powershell
python -m venv "$env:USERPROFILE\.flow-controller-v3\venv"
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" -m pip install --upgrade pip
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" -m pip install -r requirements.txt
```

For development or a minimal install, use the package metadata:

```powershell
python -m pip install -e .
```

Add `.[xlsx]` for Excel export.

## First connection

The normal setup order is:

1. Open **Connection & Assignment**.
2. Select the COM port and baud rate.
3. Click **Scan A-Z** to probe Alicat addresses `A` through `Z`.
4. Assign a gas and zone to each controller you intend to use.
5. Select those controllers and click **Connect Selected**.
6. Click **Start Live Monitor** and confirm that the readings are plausible.
7. Open **Operation & Monitoring** to enter setpoints, start logging, or run a
   saved sequence.

The application defaults to **57,600 baud**. It does not reconfigure the
instruments: every device on the selected bus must already use the chosen baud
rate. Alicat's common factory default is 19,200, so verify the device settings
before assuming a scan failure is a wiring problem.

During discovery the application reads each controller's supported-gas table.
The assignment list therefore reflects what that device actually reports,
rather than a hard-coded gas list.

## Logging, graphs, and LabVIEW

### Acquisition CSV

The acquisition logger writes one row after each completed serial polling pass:

- timestamp;
- flow, setpoint, pressure, temperature, internal setpoint error, and first
  valve drive for each connected unit; and
- live Stage 1, Stage 2, and global equivalence ratios.

New logs also include `mexa_` columns: NO, O2, acquisition/receipt timestamps,
sample age, source/sequence ID, state, validity, simulation and reporting basis.
These columns exist even if the MEXA is connected after logging starts. A fresh
analyser value can be held across multiple flow rows; `mexa_new_sample=False`
identifies a repeat, not an independent measurement. Stale, invalid or
future-dated measurements have blank values in the original NO/O2 columns.
Fresh invalid NO/O2 readings remain visible with an INVALID label and are retained
in `mexa_reported_no_ppm` / `mexa_reported_o2_percent`, with the reason in
`mexa_quality`. They are not valid measurements or optimiser inputs.
Enable **Save received MEXA
logs on this PC** for a separate CSV/JSONL record, including readings between
flow polls. This is required for live optimiser capture. The bridge's **Save
CSV + raw logs on this PC** switch is independent and defaults off, so the
analyser PC can stream without saving files. The normal flow logger works
with either MEXA logging switch off. Retained graph-history export remains a
flow-only export. See [MEXA setup](docs/MEXA_SETUP.md) for the two-PC workflow.

The analyser PC's bridge also offers confirmed local **MEAS** and **STANDBY**
requests. A mode request invalidates live capture and requires rechecking and
restarting the reader before optimisation. Calibration remains on the front
panel. Network status is separate from analyser readiness: a TCP timeout
requires a listener/firewall/network check, not a change to measurement limits.

The stream and both apps also expose CO, CO2, HC, AFR, lambda, optional RPM
and oil temperature, plus the reported PEF factor. MEXA logs and the normal
flow CSV retain these channels, status/option flags and raw replies. Missing
sensors stay blank. The analyser's automotive AFR/lambda do not replace the
NH3/H2 flow-based phi calculation; the optimiser still uses only NO/O2.

The flow app can **Host temporary relay on this PC** from its MEXA tab. It
starts a private relay and a Wormhole tunnel, then connects the receiver
locally. Copy the temporary URL and publisher key into the analyser bridge.
The Windows x64 helper is downloaded and SHA-256 checked on first use, or you
can select an official executable. No home server or port forwarding is needed.
Wormhole uses approved outbound HTTPS/WSS 443 on both PCs. Cloudflare Quick
Tunnel remains an alternative and needs outbound TCP 7844 from the flow PC.
Temporary tunnels have no uptime guarantee. See
[temporary relay setup](docs/MEXA_QUICK_TUNNEL.md) before publishing data.

Both MEXA apps also offer **Internet relay (outbound WSS)** alongside Direct LAN.
With a separate approved relay, neither lab PC listens for incoming connections.
The host forwards the measurements over TLS; publisher/receiver
access keys are separate from the shared measurement key. The repository
includes the server, not a hosted URL. Local logging, invalid-data rejection
and live-capture safeguards are unchanged. See [relay setup](docs/MEXA_RELAY.md)
for deployment, key handling and outage behaviour.

Column names include gas, zone, and unit. The header is fixed when logging
starts, so assignments cannot change while the file is open. Failed readings
are left blank rather than replaced with stale values. Writes are line-buffered,
and a logging error is reported without stopping control.

The default log directory is `Documents\Flow Controller`.

### Graph-history export

**Logging & Graphs > History & Export** exports the retained in-memory history
for every assigned controller, not just the plotted series. CSV is always
available; `.xlsx` is available when `openpyxl` is installed.

### LabVIEW UDP trigger

The Qt interface can listen for two case-insensitive UDP datagrams:

- `log` starts a new timestamped acquisition log;
- `stop` closes the active log.

The listener defaults to `127.0.0.1:61557` and is started from **Operation &
Monitoring > Logging & Acquisition**. A second `log` command is refused while a
log is already open. Rows are written only while monitoring is running.

## Combustion calculations

The live estimate uses reported volumetric flows only. It does not infer flame
state or correct for pressure, temperature, preheat, incomplete combustion, or
gas analysis. Missing and small negative readings contribute zero.

The card reports:

- equivalence ratio, `phi`;
- fuel firing rate from lower heating value; and
- cold-flow inlet bulk velocity when an inlet diameter or area has been declared.

In Staged mode the selected NH3, H2, or CH4 pilot contributes to Stage 1 and
the global result. In Standard mode, all assigned controllers are aggregated by gas. Use the menu on
the combustion card to enter a circular inlet diameter or the actual inlet area
for square and other shapes. The same menu sets the number of Stage 2 inlets and
the display refresh interval. Pausing the card affects display only; logging
continues to calculate its equivalence-ratio columns.

<details>
<summary>Calculation reference</summary>

SLPM uses Alicat's default standard conditions of **25 °C and 14.696 psia**.
The basis follows Alicat's [default STP
definition](https://www.alicat.com/support/what-is-mass-flow/), and the densities
below are the exact values in the [Alicat Gas Select 5.0
table](https://documents.alicat.com/specifications/Alicat_Preloaded-Gases-and-Properties_Rev0.pdf)
for that same basis.

| Constant | CH4 | H2 | NH3 | Air |
| --- | ---: | ---: | ---: | ---: |
| O2 demand, mol/mol fuel | 2.00 | 0.50 | 0.75 | - |
| Lower heating value, MJ/kg | 50.0 | 120.0 | 18.6 | - |
| Density at 25 °C and 14.696 psia, kg/m3 | 0.65688 | 0.08235 | 0.70352 | 1.18402 |
| Derived volumetric LHV, MJ/m3 | 32.84400 | 9.88200 | 13.08547 | - |
| Firing rate, kW/SLPM | 0.54740 | 0.16470 | 0.21809 | - |

The mass-based LHVs remain 18.6 MJ/kg for NH3, 120 MJ/kg for H2, and
50 MJ/kg for CH4. Volumetric LHV is derived as `density * mass LHV` on the
same Alicat basis; it is not copied from spreadsheet columns that may use
Nm3 or another reference temperature.

The stoichiometric calculation uses the [NIST dry-air mole
fractions](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=921756):
O2 = 0.209390 and N2 = 0.780848. The remaining 0.009762 is argon, CO2, and
other trace gases. N2 is recorded as a shared physical constant, but only the
O2 fraction enters the current stoichiometric-air demand.

Stoichiometric air and equivalence ratio are calculated from:

```text
0.209390 * air_stoich = 2.00*CH4 + 0.50*H2 + 0.75*NH3
phi = air_stoich / air_supplied
```

Firing rate is the sum over all fuel streams:

```text
volumetric_LHV [MJ/m3] = density [kg/m3] * mass_LHV [MJ/kg]
power [kW] = sum(flow_fuel [SLPM] * volumetric_LHV [MJ/m3] / 60)
```

Do not combine a density or MJ/m3 value quoted at 0 °C (`Nm3`) with controller
flows referenced to 25 °C. That mixes two standard-volume bases and biases
mass flow, firing rate, and calculated targets. Check each controller's
calibration sheet: its declared STP overrides Alicat's default. If any hardware
uses a non-default basis, configure or change the software gas-property basis
consistently before using these calculations.

For a circular inlet with diameter `d` in millimetres, the app calculates its
area. For a non-circular inlet, enter area `A` directly in square millimetres.

```text
A [m2] = pi * (d/1000)^2 / 4       # circular diameter input
A [m2] = entered_area_mm2 / 1000000 # direct area input
velocity [m/s] = (total_flow / 60000) / A
```

The total flow used for velocity includes non-reacting gases because they still
occupy the inlet. Stage 2 multiplies the area of one declared inlet by the
declared number of identical inlets.

</details>

## Configuration and data files

| Data | Default location | Override |
| --- | --- | --- |
| Acquisition logs | `Documents\Flow Controller` | Choose a path in the UI |
| Saved sequences | `Documents\Flow Controller\sequences` | Choose a path when saving |
| Per-unit full scale/ramp settings | `unit_prefs.json` beside the project | `FLOW_CONTROLLER_UNIT_PREFS` |
| Combustion inlet/refresh settings | `combustion_prefs.json` beside the project | `FLOW_CONTROLLER_COMBUSTION_PREFS` |
| UI theme | `ui_theme.json` beside the project | `FLOW_CONTROLLER_UI_CONFIG` |

The JSON preference files are optional. Missing or malformed files fall back to
safe defaults rather than preventing the application from starting. Appearance
settings can be previewed with **Apply** and persisted with **Save** from the
settings dialog.

`uninstall.bat` removes the environment at
`%USERPROFILE%\.flow-controller-v3` and an older local `.venv` if present. It
does not remove Python, this source folder, logs, sequences, or preference
files.

## Development

### Project layout

```text
flow_controller/
  domain/          pure assignment, combustion, graphing, RQL, and safety rules
  infrastructure/  Alicat protocol and the serial worker
  services/        controller discovery
  core/            session, telemetry, logging, ramps, sequences, and preferences
  ui/              PySide6 interface
mexa_bridge/        standalone analyser reader, records, transports, and relay
tests/              hardware-free unit and Qt tests
run.py              source-tree launcher
```

The analyser bridge lives outside `flow_controller/`. The flow app imports its
measurement records and receiver transports, not the bridge window or serial
reader. The bridge and relay ZIPs contain no flow-controller code or optimiser
dependencies. Build them independently:

```powershell
python build_mexa_package.py dist/MEXA-584L-bridge.zip
python build_mexa_package.py dist/MEXA-584L-relay.zip --relay
```

Use a new destination filename for each build. Existing ZIPs are never replaced.
The 28 August `MEXA-584L-bridge-wormhole.zip` remains compatible with the updated
flow app; this separation does not require reinstalling it on the analyser PC.

The domain and most core modules do not import serial hardware or a GUI toolkit,
which keeps their behavior testable without a controller. `core.session` is the
deliberate Qt-aware boundary: it receives results from the serial thread and
delivers them to the interface through signals.

### Run the tests

The suite does not require a display or connected controller:

```powershell
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" -m unittest discover -s tests -v
```

It covers protocol parsing, discovery, assignments, safety selection, ramps,
sequences, preferences, graphing, combustion/RQL arithmetic, and Qt behavior.

Tests do not replace hardware acceptance. Before an experiment, verify scanning,
device gas tables, assignments, readback, individual and batch setpoints,
ramping, logging, reconnect behavior, UDP commands, **ZERO FUEL**, and **ZERO
ALL** against the real installation.

## Troubleshooting

### No controllers are found

- Confirm the selected COM port.
- Confirm every controller and the application use the same baud rate.
- Check that controller addresses are unique letters from `A` to `Z`.
- Verify the adapter is the correct RS-232 or RS-485 type and that its driver is
  installed.
- Check cable topology, termination, grounding, and power.
- Close any other program that may have the COM port open.

### Installation fails on a long path

Use `install.bat`, which places the environment under `%USERPROFILE%`. If pip
still reports a deeply nested `No such file or directory` error, move the
project nearer the drive root or enable Windows long paths.

### The environment was created in an unexpected location

Some Microsoft Store Python installations redirect filesystem writes. Install
64-bit Python from python.org, ensure `python.exe` is on `PATH`, and run
`install.bat` again.

### A telemetry field is blank

Alicat firmware varies. The application first attempts combined telemetry and
falls back to individual register reads. Unsupported fields remain blank; the
system log records the detected capability and any communication failures.

## Final safety note

Treat the application as one layer of supervision. Validate the complete
system on the real rig, keep independent hardware interlocks in service, and
make shutdown behavior part of the operating procedure.
