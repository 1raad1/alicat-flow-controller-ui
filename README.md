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
clear the red **OFF** latch and enter a rate. With ramping enabled but no rate
declared, CH4 pilot and air lines still use the built-in minimum ten-second move.

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

## Agent launcher and saved sequences

The collapsible **Agent launcher** in the Operation sidebar opens Claude Code
or Codex in an embedded Windows terminal and connects a local, authenticated
MCP server. Click inside the terminal and type normally; terminal input is sent
directly to the running agent without a separate message field. The terminal
tracks the visible sidebar width and resizes its PTY columns so output wraps at
the card edge. Claude and Codex are launched from their compact provider icons;
hover either icon for its restricted-profile details.
On a new PC, open **Agent setup** to sign in, refresh CLI detection, or open the
official installation guide. Sign-in runs in the same embedded terminal through
the provider CLI. The app does not read or store the account credentials.
Claude is given an explicit Read-plus-allowlisted-MCP tool profile. Codex runs
in its read-only sandbox but retains shell access, so its live-control arming
dialog carries an additional warning. Live authority is always off when either
agent starts. The launcher injects the MCP server and its rig-only instructions,
so prompts do not need to say "use MCP." Tool selection remains model-driven;
Codex users can enter `/mcp` to verify that `flow_controller` is connected.
Both agents can:

- read copied assignments, Alicat telemetry, recent history, derived state,
  ramp policies, and declared command ceilings;
- list saved sequences and see whether the current rig can run each one;
- submit a sequence draft to the existing sequence editor;
- change role setpoints automatically while live control is enabled; or
- run a saved sequence once per request while live control is enabled.

The red **LIVE CONTROL** toggle is default-off and is enabled while either
supported agent is running. Enabling it shows the captured role-to-unit
mapping, MAX FLOW, and ramp ceilings. It also explains that the agent may
set values without further confirmation and select any valid `.fcseq.json`
file in the app's sequence folder. This is the only control warning.
Only roles with both a positive MAX FLOW and positive enabled ramp rate enter
the envelope. Authority remains enabled until the toggle is switched off, and
is also revoked by stopping the agent, a communication fault, disconnecting,
stopping monitoring, or changing assignments, limits, or ramps. Turning it off
prevents new agent actions. It does not silently stop or zero a sequence that
is already replaying; use the existing replay controls for that run. There is
no agent zero-flow tool. Agent read calls
are rate-limited to 10 calls/s per method and agent before audit I/O (throttled
calls are not logged individually). Saved-sequence names cannot contain paths,
files are size-bounded, and each file is re-read after the durable
pre-execution audit. Every track and keyframe must remain inside the frozen
authority envelope. Replay is refused if another sequence is active or the
measured flows do not match its opening. The audit is written to
`Documents\Flow Controller\agent_audit.jsonl`.

There is no separate automated-test editor in the Sequences card. Build and
save ordinary flow sequences, then ask the agent to choose and run them in the
order required by the test. This keeps one sequence format and one replay path
for manual and agent-driven operation.

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
