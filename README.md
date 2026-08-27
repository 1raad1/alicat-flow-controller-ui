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
| Pilot | CH4 |

CH4 assigned directly to Stage 1 or Stage 2 is included in that stage's live
combustion estimate. The CH4 pilot is also included in Stage 1. Automatic RQL
target calculation still uses its established seven required lines:

Auto-calculation is enabled for either of these assignments:

- **Full RQL:** NH3, H2, and Air in both stages, plus the CH4 pilot.
- **Rich + quench-air:** Stage 1 fuels and air, Stage 2 air, and the CH4 pilot.

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
enabled but no rate declared, CH4 pilot and air lines still use the built-in
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

### Create and run an experiment

1. Expand **Bayesian optimiser** and choose **New experiment**. Enter the fixed
   NH3/H2 thermal input, stage-1 fuel split, and permitted bounds for H2 volume
   percentage, stage-1 phi and overall phi. Bounds are intentionally blank.
   This version supports stage-1 phi >= 1 and overall phi < 1, with both fuels
   present. The methane pilot must be off during measurements.
2. Choose the dry O2 reporting reference (default 15%, not a regulatory claim),
   initial-design size (default 16 completed tests) and minimum averaging window
   (default 30 seconds). Save a new `.fcbo.json` experiment. These settings are
   fixed for the campaign; use a new file to change them.
3. Click **Suggest next test**, then **Load target fields**. This fills the
   existing flow fields, including zero for the pilot and unused lines. It
   sends no commands. Review every field and the transition procedure before
   applying through the usual controls. Existing MAX FLOW limits and ramps
   still apply. Do not assume that safe endpoints imply a safe transition.
4. After switching the pilot off and allowing the burner, sample line and
   analyser to settle, check both confirmations. For live measurements, connect
   the bridge in the **MEXA analyser** tab and select **Capture NO/O2
   automatically**. Click **Start window**. With live capture off, average
   the analyser's uncorrected dry NO and O2 manually over that window.
5. Click **Finish window** after both streams cover the minimum duration.
   Live capture fills and locks the NO/O2 means; manual mode lets you enter
   those means and an optional NO standard error. Add notes, confirm the
   uncorrected dry basis, then **Save result**. No flows change on save.
6. Suggest the next test. Use the **History** tab to inspect results, repeat
   a completed point, or export CSV. Repeating a point creates a separate test.

The optimiser requires one NH3 line, one H2 line and one air line in stage 1,
plus stage-2 air. Stage-2 fuel lines are required only when the fixed fuel split
is below 100%. A pilot controller may remain assigned at zero or be unassigned.
All other assigned gas lines must read off during measurement.

### Measurement basis and limits

The objective is oxygen-corrected dry **NO**, not total NOx or mass per energy:

```text
corrected_NO = raw_dry_NO * (20.9 - reference_O2) / (20.9 - measured_dry_O2)
```

This avoids optimising raw ppm merely by adding air. It does not measure NO2,
NH3 slip, N2O or combustion efficiency. Confirm the MEXA sensor's calibration,
sample conditioning and suitability for the NH3/H2 exhaust matrix before making
emissions claims. Do not enter an already oxygen-corrected reading. Readings at
or above 20.9% O2 cannot be corrected; readings close to air concentration amplify
measurement errors. NO input is limited to the published 0–5000 ppm range.

Fresh flow and setpoint readings must track targets within the larger of 3%
or 0.05 SLPM. Measured thermal input must be within 3% of the campaign setting.
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

The initial design selects spread-out points from a scrambled Sobol candidate
pool. Subsequent suggestions fit a Matérn-5/2 Gaussian process and maximise
Monte Carlo noisy expected improvement over a feasible candidate pool. The model
uses measured blend and equivalence ratios from the captured flow window, with
the requested settings retained alongside them. It fits residual observation
noise and accepts an optional per-test NO standard error. O2 uncertainty and
systematic calibration bias are not propagated. Suggestions respect the declared
search region and current flow ceilings; no flame-safety boundary is learned.

Experiment changes are written atomically after each suggestion, completed
window and result. **Open** resumes a saved campaign, including a pending test,
without loading target fields or applying flows. An unfinished capture is not
resumed after closing the app. Typed measurement text is not durable until
**Save result**; it does survive an appearance refresh. The history identifies
the lowest observed corrected NO, not a certified global minimum. Repeat
promising points and reference conditions to check reproducibility and drift.

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
future-dated measurements have blank NO/O2 values. Use the receiver's separate
CSV/JSONL for the complete analyser record, including readings between flow
polls. Retained graph-history export remains a flow-only export.

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

In Staged mode the pilot CH4 contributes to Stage 1 and the global result. In
In Standard mode, all assigned controllers are aggregated by gas. Use the menu on
the combustion card to enter a circular inlet diameter or the actual inlet area
for square and other shapes. The same menu sets the number of Stage 2 inlets and
the display refresh interval. Pausing the card affects display only; logging
continues to calculate its equivalence-ratio columns.

<details>
<summary>Calculation reference</summary>

Standard litres are referenced to 25 C and 1 atm, using a molar volume of
`24.465 L/mol`. Dry air is treated as 21% oxygen by volume.

| Constant | CH4 | H2 | NH3 | Air |
| --- | ---: | ---: | ---: | ---: |
| O2 demand, mol/mol fuel | 2.00 | 0.50 | 0.75 | - |
| Molar mass, g/mol | 16.043 | 2.016 | 17.031 | 28.965 |
| Lower heating value, MJ/kg | 50.0 | 120.0 | 18.6 | - |
| Density at 25 C and 1 atm, kg/m3 | 0.656 | 0.082 | 0.696 | 1.184 |
| Firing rate, kW/SLPM | 0.5465 | 0.1648 | 0.2158 | - |

Stoichiometric air and equivalence ratio are calculated from:

```text
0.21 * air_stoich = 2.00*CH4 + 0.50*H2 + 0.75*NH3
phi = air_stoich / air_supplied
```

Firing rate is the sum over all fuel streams:

```text
power [kW] = sum(flow_fuel * LHV_fuel * molar_mass_fuel / (24.465 * 60))
```

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
tests/              hardware-free unit and Qt tests
run.py              source-tree launcher
```

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
