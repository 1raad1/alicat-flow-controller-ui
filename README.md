# Alicat Flow Controller v3

This is a separately runnable copy of v2. The v2 program remains unchanged in
its own folder, so the two can be compared side by side.

## What v3 is

v3 is a PySide6 (Qt) rewrite of the interface. Acquisition, control, logging and
safety behaviour are shared with the original Tk build, which stays reachable as
`run.py --legacy` until the port has been proven against real hardware.

Three tabs, with `ZERO FUEL` / `ZERO ALL` pinned to the corner of all of them:

- **Connection & Assignment** — scan A–Z, per-controller gas and zone, connect.
  Zones can be changed after connecting without reconnecting.
- **Operation & Monitoring** — setpoints grouped by stage, standard/staged mode,
  combustion and ignition, UDP, and sequence record/replay with an editable
  curve per controller, a live timeline, and a repeat count for running a
  captured transition back more than once.
- **Logging & Graphs** — logging controls, export, and the plot panel.

Appearance (colours, fonts, corner radius, spacing, glass) is read from
`ui_theme.json` next to the application and editable in-app from the settings
dialog. `FLOW_CONTROLLER_UI_CONFIG` overrides the file location.

### Per-meter full scale and ramp rate

Each controller card on *Operation & Monitoring* draws a bar tracking flow
against setpoint. Under that bar are the two figures that describe the meter
itself: **FULL SCALE** and **RAMP**.

Both are properties of the device and the plumbing behind it rather than of a
session — unit `C` is the same 50 SLPM meter tomorrow morning as it was last
night — so they are remembered per unit in `unit_prefs.json` next to the
application and only ever need typing once per meter.
`FLOW_CONTROLLER_UNIT_PREFS` overrides the file location. A missing or damaged
store costs the cards their declared figures rather than stopping the
application from starting.

**FULL SCALE** is the span the bar is drawn against, in SLPM. The controllers do
not report it over the wire, so it has to be declared: read the figure off the
sticker on the front of the meter and type it in. Setting the box back to `auto`
(or `0`) withdraws the declaration at any time. Nothing here touches control:
the full scale is the bar's span, not a limit the hardware is driven against.

Left at **auto**, the bar spans a round figure *above* the largest flow or
setpoint that line has been asked for — never less than 1 SLPM, and never equal
to the peak itself. Both parts matter: an undeclared meter starts with no peak
at all, so a span that tracked it exactly showed every bar pegged at the first
reading, and one that merely caught up to each new peak was pegged again a
sample later. The span steps between readable figures (1, 1.2, 1.5, 2, 2.5, 3,
4, 5, 6, 8 × 10ⁿ), so the bar climbs through its track instead of re-scaling
under the operator on every rise.

**RAMP** does touch control. It is how fast the line may move, in SLPM per
second, and it paces *every* setpoint this application writes to that unit —
typed on the card, applied by batch control, driven by the ignition sequence, or
replayed from a recorded sequence. A setpoint 10 SLPM away on a line
declared at 2.5 SLPM/s is walked out over four seconds. The steps still go
through the ordinary setpoint queue, so a paced move is subject to every
interlock a single write is: the zero lock wins, the monitor loop remains the
only thing that talks to hardware, and `ZERO ALL` stops it dead. Typing a new
setpoint while a line is still moving redirects it from wherever the flow
actually is rather than being refused.

Leaving the box at **step** writes the setpoint in one go and lets the
controller travel at its own pace. The pilot and the two air lines are the
exception: they are never stepped, so with no rate declared a setpoint sent to
them is still spread over ten seconds — the same twenty half-second steps as
earlier builds. They can be told to move *slower* than that but never faster.

### Sequences: record, edit, replay, repeat

A sequence is what the rig was *asked* to do, not what it did. Open the panel
with **Record / Replay Sequence** in the *Sequence* card, in the left column of
*Operation & Monitoring* between *Batch Control* and *System Log*.

The same card lists everything already saved under
`Documents/Flow Controller/sequences`, newest first. **Clicking one loads it and
runs it once** — no repeat count, no transport to set up, which is the usual
case: run the transition that was recorded last week, exactly as recorded. The
panel opens with it so the timeline, the curves and the Stop button are there
while it runs.

That click is gated on where the rig is standing. A replay begins by writing the
sequence's opening setpoints, so starting one from somewhere else makes every
line jump at `t = 0` — on the pilot and the air lines that is a transient into
the burner rather than a transition. The *measured* flows are compared against
those opening setpoints (within a couple of percent of each track's largest
figure, with a small floor so a 0.4 SLPM pilot is not judged on noise); if any
line disagrees you are told which, with the figure it wants and the figure it
has, and can either set the flows to match first or run it anyway. A line that
is not reporting counts as no flow. The list is inert while a recording or a
replay is already running.

- **Record** captures every setpoint the session commands — typed, batched,
  ramped, ignition — as one track of keyframes per assigned controller. The
  monitor has to be running first: nothing is being written to the controllers
  otherwise, so there would be nothing to record. A line put down by `ZERO ALL`
  is captured like any other move, at the instant the operator asked for it, so
  a recording that ends in a shutdown replays as one. There is no *add key
  point* button: every setpoint change already lands a keyframe, and an anchor
  at the value a track is already holding adds nothing to the curve.
- **Stopping** closes the recording and writes it to
  `Documents/Flow Controller/sequences` as `*.fcseq.json`.
- **Editing**: drag a point to move it, double-click to add one, right-click to
  delete. A recorded keyframe *holds* until the next one, because a setpoint
  written to a controller does not decay; switch a point to **Ramp (linear)** to
  make the transition out of it smooth. Only the selected track is editable and
  the axis frames that track, so a 0.4 SLPM pilot is not a flat line on an axis
  scaled for stage air.
- **All tracks (overview)** is the first row of the track list, and the view a
  sequence opens on: every controller drawn on one pair of axes at full weight,
  which answers what the rig as a whole does before you go looking at any one
  line. Nothing is editable there — pick a track below it to edit that curve,
  and the others drop back to context.
- **Replay** walks the keyframes back out through the ordinary setpoint queue,
  so a replay is subject to every interlock a hand-typed setpoint is: the zero
  lock still wins, the monitor loop is still the only thing that writes to
  hardware, and `ZERO ALL` still stops it dead. A step edge on the pilot or
  either air line is spread over one second rather than written as a jump, and
  the log says so. A replay writes through the monitor loop, so it needs the
  monitor running; asking to replay while it is stopped offers to start it rather
  than doing nothing.
- **How fast each line moves** during a replay is not set here. It is the
  **RAMP** figure on that controller's own card, described above, so a line that
  has been slowed down stays slow however its setpoint arrives. With no rate
  declared the default holds: the pilot and the two air lines are spread over a
  second and everything else replays as recorded, steps and all. A declared rate
  can only make those three lines slower, never faster. Sequences recorded before
  the rate moved onto the cards carry a per-track rate of their own; that is
  still read back, and stands in wherever the card has nothing to say.
- **Clear** puts the sequence down so the next recording starts from an empty
  timeline instead of replacing it. Files already written to disk are untouched,
  and the log names what was cleared.
- **Hold if flows lag** is the discrepancy check. While replaying, each track's
  measured flow is compared against what it was commanded; if any line is
  further off than the tolerance (a percentage of that track's largest
  setpoint, with a small floor so a 0.4 SLPM pilot is not judged on noise), the
  replay clock stops for **every** controller until it catches up. Transitions
  therefore stay in step with each other rather than the fast lines running
  ahead while a slow one is still settling. Holds are marked on the timeline,
  named in the log with the offending line, and time-boxed at 30 s so a genuinely
  stuck controller delays the run rather than stalling it forever. The check can
  be turned off, and its tolerance changed, mid-replay.
- **Repeat** (`× N`, or *until stopped*) runs the sequence more than once. Each
  wrap is treated as an ordinary edge rather than a fresh start, so rate-limited
  lines ramp from the closing value back to the opening one instead of stepping.
  Because a repeat drives the rig from the end state back to the start state on
  every pass, the log names the lines that will move at each wrap when the
  replay begins. Stopping the monitor, a zero-flow command, or shutdown all
  cancel a repeating replay.

Replay is refused rather than half-run if a track's role is not currently
assigned; roles are re-resolved against the assignment in force at replay time,
so a sequence recorded with stage-1 ammonia on unit A still runs after it has
been moved to unit D.

### Graph rendering (both builds)

v3 began as a graph-rendering performance release; the Qt panel keeps the same
behaviour on pyqtgraph, and the notes below describe the legacy Tk renderer.

- **Graphs are lazy.** Nothing is plotted until the operator opens the
  *Logging & Graphs* tab **and** ticks at least one controller/measurement.
  Series checkboxes now start unticked instead of defaulting to flow and
  setpoint.
- **History collection is independent of rendering.** Samples keep accumulating
  while the tab is hidden or nothing is selected, so switching to the tab
  mid-run shows the full trace with no gap. Leaving the tab stops the render
  loop and releases the figure; returning rebuilds it from stored history.
- **The animation is blitted.** `FuncAnimation(blit=False)`, which repainted the
  entire figure several times a second, is replaced by a Tk `after` loop at
  200 ms that caches the static background (`copy_from_bbox`) and repaints only
  the trace artists (`restore_region` + `draw_artist` + `blit`).
- **Per-frame work was removed from the hot path.** The grid is configured once
  when the figure is built rather than on every frame, and `relim()` /
  `autoscale_view()` are gone. Axis limits are reconsidered only every fifth
  rendered frame, and only actually moved when `should_rescale()` decides the
  data has left the axis or shrunk into a small part of it — hysteresis that
  keeps the cheap blit path in use instead of forcing a full repaint.
- **Rendering faults cannot stop control.** Every draw path is guarded; a
  failure marks the figure for a full redraw and the render loop continues,
  leaving polling, setpoints, and E-STOP untouched.
- `numpy` is now a declared direct dependency (line data is built with
  `np.fromiter`); it was previously only pulled in transitively by matplotlib.
- New pure helpers `padded_limits()` and `should_rescale()` in
  `flow_controller/domain/graphing.py`, covered by unit tests.

## Inherited from v2

- The asyncio serial worker is isolated in `flow_controller/infrastructure`.
- Raw Alicat gas-table/register commands and response parsing are isolated from
  Tkinter in `flow_controller/infrastructure/alicat_protocol.py`.
- Combustion calculations and typed controller/telemetry records live in
  `flow_controller/domain` and can be tested without opening the UI.
- Reusable Tk widgets live in `flow_controller/ui`.
- Discovered controllers carry their own supported-gas table.
- Graph history receives one point per completed hardware poll rather than one
  point per UI refresh, avoiding duplicate samples.
- The graph display can be configured per controller and measurement, with
  automatic or manual time, flow, pressure, temperature, error, and valve axes.
- `ZERO FUEL` immediately verifies zero on every assigned non-Air controller;
  `ZERO ALL` does the same for every assigned controller. Both retain the live
  monitor and open connections.
- `Reconnect Flow Meters` closes and reopens the serial connections while
  retaining assignments, graph/log history, and confirmed setpoints.
- Live combustion values are calculated from the telemetry cache rather than
  reading formatted text back out of Tk labels.
- Default logs are written to the top-level `Logs` folder.

The main window is intentionally still recognisable and preserves the existing
controller, optional combustion/ignition, logging, UDP, and graph workflows.
Further service extraction can now be done behind stable module boundaries.

## Install

Install 64-bit Python 3.11 or newer. Then, from this directory, **double-click
`install.bat` once**.

The environment is created in `%USERPROFILE%\.flow-controller-v3\venv`, not
beside the application. Two reasons, both learned the hard way:

- **Path length.** PySide6 unpacks QML resource files about 160 characters deep,
  and Windows rejects any path beyond 260 characters unless long paths have been
  enabled machine-wide. A program folder inside
  `OneDrive - <organisation>\Desktop\...` spends most of that budget by itself,
  and pip fails partway through with a misleading
  `OSError: [Errno 2] No such file or directory: '...qrc_qmake_Qt_labs_assetdownloader_init.cpp.obj'`.
- **Store Python.** The Microsoft Store build redirects writes to
  `%LOCALAPPDATA%` into its own package `LocalCache`, so an environment created
  there is not where it was asked for. `%USERPROFILE%` is not redirected.

Keeping it out of the program folder also keeps a 250 MB environment out of
OneDrive's sync.

To do the same by hand in PowerShell:

```powershell
python -m venv "$env:USERPROFILE\.flow-controller-v3\venv"
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" -m pip install --upgrade pip
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" -m pip install -r requirements.txt
```

`install.bat` prefers the `py` launcher and falls back to `python`, because not
every Python build installs `py` — the Microsoft Store build in particular puts
only `python.exe` on PATH.

## Run

After installing, **double-click `run.bat`**. It uses the environment above, or
a `.venv` beside the application if an older install left one there.

To start it by hand, from this directory:

```powershell
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" run.py
```

Equivalently, `-m flow_controller` in place of `run.py`. Either form must be run
with the program folder as the working directory, or with its full path given —
the application is imported from this source tree, not from site-packages.

`run.bat` passes its arguments through, so `run.bat --legacy` starts the
original Tk interface. That build needs matplotlib and a Python built with
Tcl/Tk; `requirements.txt` installs matplotlib, but the packaged `legacy` extra
in `pyproject.toml` treats it as optional, so an environment installed from
`pyproject.toml` needs `pip install matplotlib` first.

Any environment with the requirements installed will do — nothing in the program
depends on where the interpreter lives.

## Uninstall

**Double-click `uninstall.bat`** and type `YES` when it asks. It removes
`%USERPROFILE%\.flow-controller-v3` (and a `.venv` beside the program, if an
older install left one), and nothing else.

It deliberately leaves the program folder, the `Logs` folder, saved sequences
and `ui_theme.json` alone — that is experiment data, and throwing it away should
be a deliberate act. Delete the folder by hand once you are sure. Python itself
is also left installed, since other programs may be using it.

## USB serial adapter notes

The application defaults to 57,600 baud. The adapter is most likely
USB-to-RS-232 or USB-to-RS-485, depending on the controller wiring, and is not
normally the limiting factor; many adapters can operate at 115,200 baud or
above. Confirm all of the following before increasing the application baud:

- every controller on the bus has been configured to the same baud;
- the adapter supports the electrical interface in use (RS-232 is not RS-485);
- the correct adapter driver is installed on the target PC;
- cable length, grounding, termination, and topology are suitable;
- communication remains reliable under repeated multi-controller polling.

Higher baud reduces byte-transmission time, but controller response latency and
the number of command/response round trips can still limit the effective polling
rate.

## Tests

Tests do not require a controller or Tk display:

```powershell
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" -m unittest discover -s tests -v
```

Before using v3 in an experiment, perform a hardware acceptance test covering
scan, per-controller gas choices, connect/readback, setpoints, monitoring,
logging, reconnect, UDP commands, verified fuel zero, and verified all-flow zero.

The desktop application should remain a supervisory control interface. Physical
interlocks and independent emergency shutdown protection should not depend only
on Windows, Python, or the USB serial connection.
