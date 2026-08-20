# Alicat Flow Controller

Desktop control, monitoring, sequencing, and logging for addressed Alicat mass
flow controllers on a shared serial bus.

The application was built for an ammonia/hydrogen rich-quench-lean (RQL)
burner rig, but it can also operate as a general multi-channel flow controller.
Use **Standard** mode for ordinary setpoint control and **Staged (RQL)** mode
for stage-aware assignments, flow calculations, and combustion estimates.

> [!WARNING]
> This is a supervisory control interface, not a safety system. Physical
> interlocks and independent emergency shutdown protection must not depend on
> Windows, Python, or the USB serial connection. Closing the application does
> not make a controller forget its last setpoint; use **ZERO ALL** before
> shutdown when the process requires it.

The application uses a PySide6/Qt interface.

## Contents

- [Quick start](#quick-start)
- [First connection](#first-connection)
- [Operating modes](#operating-modes)
- [Controls and safety behavior](#controls-and-safety-behavior)
- [Sequences](#sequences)
- [Logging, graphs, and LabVIEW](#logging-graphs-and-labview)
- [Combustion calculations](#combustion-calculations)
- [Configuration and data files](#configuration-and-data-files)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## Quick start

### Requirements

- Windows 10 or 11
- 64-bit Python 3.11 or newer
- A USB-to-serial adapter matching the rig's electrical interface
  (RS-232 and RS-485 are not interchangeable)
- Alicat controllers configured with unique single-letter addresses

### Install and run on Windows

1. Double-click `install.bat` once.
2. Double-click `run.bat` whenever you want to start the application.

The installer creates a virtual environment at
`%USERPROFILE%\.flow-controller-v3\venv`. Keeping the environment out of the
project folder avoids OneDrive synchronization and common Windows path-length
failures from PySide6's deeply nested files.

To run from PowerShell instead:

```powershell
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" run.py
```

`python -m flow_controller` is equivalent.

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

## Operating modes

The application starts in **Standard** mode on every launch. Connecting a full
RQL assignment does not switch modes automatically.

### Standard

Standard mode provides general multi-channel operation:

- individual and batch setpoints;
- live flow, setpoint, pressure, temperature, setpoint-error, and valve-drive
  telemetry where supported by the controller;
- CSV logging and retained-history export;
- live graphs; and
- aggregate equivalence ratio, firing rate, and inlet bulk velocity.

Every assigned controller participates in monitoring and logging. Controllers
assigned to **General**, or to a gas/zone pair without an RQL meaning, simply do
not participate in stage-aware calculations.

### Staged (RQL)

Staged mode models these seven possible process lines:

| Zone | Required roles |
| --- | --- |
| Stage 1 (rich) | NH3, H2, Air |
| Stage 2 (lean) | NH3, H2, Air |
| Pilot | CH4 |

Auto-calculation is enabled for either of these assignments:

- **Full RQL:** all seven roles.
- **Rich + quench-air:** Stage 1 fuels and air, Stage 2 air, and the CH4 pilot.

The calculator accepts firing rate, hydrogen fraction by volume, Stage 1 fuel
split, Stage 1 equivalence ratio, and global equivalence ratio. It calculates
and stores target flows but **does not send them**. Use **SET ALL FLOWS** only
after reviewing the targets. Impossible requests, such as a Stage 1 mixture
leaner than the requested global mixture, are rejected instead of producing a
negative flow.

## Interface overview

### Connection & Assignment

Use this tab to choose the serial settings, scan the bus, inspect device gas
tables, assign roles, connect controllers, and verify live telemetry. Zones can
be reassigned after connection unless a CSV log is open, because the log's
columns are fixed when recording starts.

### Operation & Monitoring

This is the main run screen. Each connected controller has a card containing
its current values, setpoint entry, full-scale declaration, and ramp setting.
The left column contains logging, RQL auto-calculation, saved sequences, and the
system log. The live combustion card shows the metrics appropriate to the
selected operating mode.

### Logging & Graphs

Choose flow, setpoint, pressure, temperature, internal setpoint error, or valve
drive for each controller. Nothing is rendered until at least one series is
selected, but history begins accumulating as soon as monitoring starts. Leaving
the tab stops graph rendering without stopping acquisition.

Axis limits can be automatic or fixed. Automatic axes use hysteresis so a
rising trace does not continually rescale beneath the operator. The default
history limit is 3,600 samples.

## Controls and safety behavior

The batch controls are available from every tab:

- **SET ALL FLOWS** queues each card's current setpoint.
- **ZERO FUEL** targets every assigned controller whose gas name is not exactly
  `Air`.
- **ZERO ALL** targets every assigned controller.

Two rules are enforced in the control layer:

1. A zero command outranks all pending or new nonzero setpoints for its target
   units.
2. Only the monitoring loop writes to hardware. Typed setpoints, batch sends,
   ramps, and sequence replay all pass through the same queue and interlocks.

Zero commands keep the serial connection and monitoring active so the result
can be verified. They also cancel active ramps and sequence replay. Stopping
monitoring or closing the application is not a substitute for zeroing the rig.

After ten consecutive read timeouts from one unit, the application treats the
port as wedged and restarts monitoring. **Reconnect** closes and reopens the
connections while preserving assignments, history, and confirmed setpoints.

### Full scale and ramp rate

Each controller card remembers three per-unit settings:

- **FULL SCALE** sets the SLPM span of the card's bar. It is a display setting,
  not a hardware limit. Enter the value printed on the meter, or use `auto`/`0`
  to let the application choose a readable span.
- **RAMP** limits how quickly the application's commands may move that line, in
  SLPM/s. It applies to typed, batch, calculated, and replayed setpoints.
- **OFF** disables application-side ramping for that controller. It also
  disables the built-in minimum move time for air and pilot lines.

When no explicit rate is declared, CH4 pilot and air lines are still spread
over a minimum ten-second move. Turning **OFF** on for one of these lines removes
that protection and is shown as a persistent red latch.

## Sequences

A recorded sequence stores what the application was asked to do, not the
measured response of the rig.

- Start monitoring, expand **Record / Replay Sequence**, and click **Record**.
  Every commanded setpoint becomes a keyframe.
- Stopping a recording saves a `.fcseq.json` file under
  `Documents\Flow Controller\sequences`.
- Clicking a saved sequence loads it without moving the rig. The adjacent play
  button loads and runs it once.
- Drag a point to move it, double-click empty space to add a point, or
  right-click a point to edit its exact time, value, and step, linear, or smooth
  transition.
- **Slower** and **Speed up** expand or compress every track and timeline marker
  together. Each 1.2× step is reversible with the opposite button.
- Shift-click selects a continuous group of points and Ctrl-click adds or
  removes individual points. **Smooth selected** eases only the transitions
  enclosed by that group; **Smooth whole sequence** applies the same
  no-overshoot easing to every transition on every track.
- In **All tracks (overview)**, fuel setpoints use the left y-axis and air
  setpoints use the right y-axis, so the larger air range does not flatten the
  fuel traces.
- **Hold if flows lag** pauses the replay clock for all tracks until lagging
  measurements catch up, with a 30-second maximum hold.
- Repeats ramp from the final values back to the opening values rather than
  treating each pass as a fresh, unprotected start.

Replay uses the current assignment, so a role recorded on controller `A` can
later run on controller `D`. Replay is refused if a required role is missing.
Before starting, measured flows are compared with the sequence's opening
setpoints; the operator must resolve or explicitly override any mismatch.

Zero commands, stopping monitoring, and application shutdown cancel replay.

### Sequencing in operation

The following 90-second excerpt shows a recorded sequence being replayed while
the controller interface and burner response are monitored. The demonstration
is shown at 2x speed.

[Watch the sequencing demonstration (MP4, 1:30)](docs/media/sequencing-operation-example-2x-90s.mp4)

## Logging, graphs, and LabVIEW

### Acquisition CSV

The acquisition logger writes one row after each completed serial polling pass:

- timestamp;
- flow, setpoint, pressure, temperature, internal setpoint error, and first
  valve drive for each connected unit; and
- live Stage 1, Stage 2, and global equivalence ratios.

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
- cold-flow inlet bulk velocity when an inlet diameter has been declared.

In Staged mode the pilot CH4 contributes to Stage 1 and the global result. In
Standard mode all assigned controllers are aggregated by gas. Use the menu on
the combustion card to set inlet diameters, the number of Stage 2 inlets, and
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

For a circular inlet with diameter `d` in millimetres:

```text
velocity [m/s] = (total_flow / 60000) / (pi * (d/1000)^2 / 4)
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
