# Alicat Flow Controller

A desktop application for running a rack of addressed Alicat mass flow
controllers on one serial bus: find them, say what each one is, drive them,
watch them, log them, and put them all to zero in one gesture when something
looks wrong.

It was written for an ammonia/hydrogen **rich-quench-lean (RQL)** burner rig,
so it knows about stages, equivalence ratios and an ignition sequence. That
half switches off. In **standard** mode the same window is plain multi-channel
flow control — setpoints, readings, logging and graphs — with the staged
arithmetic gone and the combustion estimate reduced to the single inlet such a
rig actually has.

The interface is PySide6 (Qt). The original Tk build is still in the tree and
still starts, as `run.py --legacy`, until the Qt port has been proven against
the hardware.

## The rig it models

Up to seven lines matter to the combustion calculations, named by the gas on
them and the zone they feed:

| Zone | Lines |
| --- | --- |
| Stage 1 (rich) | NH₃, H₂, Air |
| Stage 2 (lean) | NH₃, H₂, Air |
| Pilot | CH₄ |

A controller gets its role from that pair — `NH3` on `Zone 1` is stage-1
ammonia — and roles are what the auto-calculation, the ignition ramp and the
live φ readouts address. Anything assigned to **General**, or to a gas/zone
pair with no combustion meaning, is still connected, still monitored, still
graphed and still logged. It simply takes no part in the arithmetic.

Two configurations are recognised automatically: **Full RQL** (all seven) and
**rich + quench-air** (stage 1, stage-2 air and the pilot, with no second fuel
zone). Anything else runs fine, but with the calculator disabled and a line on
screen saying which assignments are missing.

## The three tabs

`ZERO FUEL` and `ZERO ALL` sit in the corner of the tab strip, so they are
reachable whichever tab is open.

### 1 · Connection & Assignment

Four steps in the order they are actually done.

- **COM port** — pick the port. The application's own baud defaults to 57,600
  and does not reconfigure the instruments; every device on the bus must
  already be set to match.
- **Scan units A–Z** — probe all twenty-six single-letter Alicat addresses.
  Each controller that answers reports its live reading, and then its own
  supported-gas table is read off the device, so the gas list you are offered
  per controller is the list that controller actually has.
- **Assign controllers** — give each discovered unit a gas and a zone. Zones can
  be changed after connecting without reconnecting, unless a CSV log is open —
  the log's columns are fixed when it starts.
- **Connect & monitor** — open the connections and start the polling loop.

A **Live Monitor** panel on the same tab shows what came back, so the
assignment can be checked against real readings before leaving the screen.

### 2 · Operation & Monitoring

The screen a run is driven from. Setup on the left, live values on the right,
with the sequence panel folding out underneath.

**Live controller cards.** One card per assigned unit, grouped by stage in
staged mode, each with a bar tracking flow against setpoint, a setpoint box, and
the per-meter declarations described below. The card is where a single setpoint
is typed and sent.

**Auto-Calculate Flows** (staged mode). Give it a firing rate in kW, the
hydrogen fraction of the fuel blend by volume, the fraction of fuel through
stage 1, and the two equivalence ratios you want (φ stage 1 and φ global). It
returns the SLPM every line has to deliver. Nothing is sent: the targets are
stored and shown on tiles, and a separate button stages them into the setpoint
boxes so every number can be read in the place it would have been typed before
any of it leaves the machine. A request that cannot be met — stage 1 leaner than
the global mixture, which leaves negative air for stage 2 — is refused with the
figure that has to move rather than with a negative number.

**Ignition Sequence** (staged mode). Two steps. *Pre-ignition* ramps every
assigned line to a percentage of its target — 80 % of fuel and 80 % of air by
default — over a given number of steps at a given interval. *Ignite* ramps from
there to the full targets. `ABORT` is `ZERO ALL`.

**Batch Control.** Send every card's setpoint together, or zero every flow while
monitoring continues.

**Combustion estimate.** Derived figures recomputed from the telemetry cache as
the samples land, in a card that follows the mode.

In **staged** mode it reads by role: each fuel and air line, the fuel total,
then φ for stage 1, stage 2 and the rig as a whole, the firing rate in kW split
by stage, the stoichiometric air each stage is asking for, and the bulk velocity
at each stage inlet. The pilot's CH₄ counts into stage 1 and into the global
balance, exactly as it does in the CSV.

In **standard** mode there are no roles and one inlet, so the same card
aggregates by assigned *gas*: CH₄, H₂, NH₃ and air totals, one φ, the firing
rate, the stoichiometric air, air/fuel ratios as supplied and at φ = 1 by
volume and by mass, the fuel blend by volume, and the bulk velocity. A gas that
does not burn — nitrogen on a purge line, say — is counted into the velocity and
into nothing else.

**Inlet Ø** is where the bore is declared, in millimetres: one box for the
standard rig, one per stage for the burner. Nothing is assumed if they are left
blank — the velocity tile stays `--` rather than showing a figure computed
against a guessed diameter. **Compute live** and the interval beside it decide
how often the card is redrawn, from every acquisition pass down to every
fiftieth, or not at all; pausing blanks the derived tiles rather than leaving a
stale number on a live card. That setting is *display only*. Acquisition,
logging, ramps, the auto-calculation and the CSV columns are untouched by it,
and the φ columns in the log are computed on the logging path whatever the card
is doing. Both the diameters and the refresh setting live in
`combustion_prefs.json` next to the application, with
`FLOW_CONTROLLER_COMBUSTION_PREFS` overriding the location; a missing or damaged
store costs the declared bores and nothing else. Every formula behind the tiles
is written out under *The combustion estimate* below.

**System Log.** Every action, refusal and confirmation, in one place.

### 3 · Logging & Graphs

Series selection and axis limits on the left, plots on the right. Flow,
setpoint, pressure, temperature, internal setpoint error and valve drive can be
plotted per controller, stacked by metric group, with each axis either automatic
or pinned to typed limits.

**Nothing is plotted until it is asked for.** History accumulates from the
moment the monitor starts, whatever tab is open, so switching to this tab
mid-run shows the whole trace with no gap. Rendering runs only while the tab is
visible *and* at least one series is ticked; leaving the tab stops the render
loop. Axis limits are reconsidered on a hysteresis rather than on every frame,
so a rising trace climbs through its axis instead of re-scaling under the
operator. A rendering fault marks the figure for a redraw and the loop
continues: polling, setpoints and the zero commands are never on that path.

**History & Export** sets how many samples are retained (3,600 by default) and
writes the stored history out as CSV, or as `.xlsx` if `openpyxl` is installed.
The export covers every assigned controller, not only the ticked series.

## Safety rules

Two rules hold everywhere in the control layer, and everything else is built on
top of them.

- **A zero command outranks everything.** It purges pending setpoints for the
  units it targets, and while it is outstanding no nonzero setpoint for a locked
  unit is written, whoever asks — a typed value, a batch send, an ignition ramp,
  a replay or a UDP-triggered anything.
- **Nothing writes to hardware except the monitor loop.** Ramps, the ignition
  sequence and sequence replay all enqueue setpoints like a hand-typed one, so
  none of them can slip past the zero lock.

`ZERO FUEL` commands and verifies zero on every assigned controller whose gas is
not `Air`. `ZERO ALL` does the same for every assigned controller. Both keep the
connections open and the monitor running: closing the port would take the rig
off screen at the moment it matters most. Both cancel any ramp, ignition state
or replay in progress, and both are recorded into an open sequence recording at
the instant the operator asked, so a recording that ends in a shutdown replays
as one.

**Air and pilot lines are never stepped.** A step change on the CH₄ pilot or on
either air line is a pressure transient into a lit burner, not just a different
number, so a setpoint sent to those three is always spread over time whichever
button sent it.

`Reconnect Flow Meters` closes and reopens the serial connections while keeping
assignments, history and confirmed setpoints. Ten consecutive read timeouts on
one unit are taken as a wedged port and the monitor restarts rather than going
quietly stale.

## Per-meter full scale and ramp rate

Under each card's bar, on one row, are the settings that describe the meter and
the plumbing behind it rather than the session: **FULL SCALE**, then **RAMP**
and its **OFF** latch. Unit `C` is the same 50 SLPM meter tomorrow morning as it
was last night, so all three are remembered per unit in `unit_prefs.json` next
to the application and only ever need typing once per meter.
`FLOW_CONTROLLER_UNIT_PREFS` overrides the file location; a missing or damaged
store costs the cards their declared figures rather than stopping the
application from starting.

**FULL SCALE** is the span the bar is drawn against, in SLPM. The controllers do
not report it over the wire, so it has to be declared: read the figure off the
sticker on the front of the meter and type it in. Setting the box back to `auto`
(or `0`) withdraws the declaration. Nothing here touches control — the full
scale is the bar's span, not a limit the hardware is driven against.

Left at **auto**, the bar spans a round figure *above* the largest flow or
setpoint that line has been asked for, never less than 1 SLPM and never equal to
the peak itself. Both parts matter: an undeclared meter starts with no peak at
all, so a span tracking it exactly showed every bar pegged at the first reading,
and one that merely caught up to each new peak was pegged again a sample later.
The span steps between readable figures (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8 ×
10ⁿ).

**RAMP** does touch control. It is how fast the line may move, in SLPM per
second, and it paces *every* setpoint the application writes to that unit —
typed on the card, sent by batch control, driven by the ignition sequence, or
replayed from a recording. A setpoint 10 SLPM away on a line declared at
2.5 SLPM/s is walked out over four seconds. The steps go through the ordinary
setpoint queue, so a paced move is subject to every interlock a single write is.
Typing a new setpoint while a line is still moving redirects it from wherever
the flow actually is rather than being refused.

Leaving the box at **step** writes the setpoint in one go and lets the
controller travel at its own pace. The pilot and the two air lines are the
exception: with no rate declared, a setpoint sent to them is still spread over
ten seconds. They can be told to move *slower* than that, never faster.

**OFF** is how that exception is lifted. It turns ramping off for that
controller altogether — no rate, and no minimum move time either, so every
setpoint written to that unit goes out in a single write whatever line it is
driving and whatever figure is left in the rate box. On the pilot or on either
air line **this removes a protection**. It latches red while it is on, greys out
the rate box beside it so the figure there cannot be mistaken for something in
force, writes a line to the system log, and is remembered between runs. Turning
it back off restores whatever rate was typed. It is a deliberate, visible state
rather than a rate of zero, which only ever meant *no rate declared*.

## The combustion estimate

Every figure on the card comes from the volumetric flows the meters reported on
that pass and from nothing else: no pressure, no temperature, no gas analysis,
no assumption about what the flame is doing. It describes what is being
*supplied*, which is the part an operator can act on.

**Standard litres.** Alicat's SLPM are referenced to 25 °C and 1 atm, so one
standard litre is the same mole count for every gas on the rig:

```
Vm = 24.465 L/mol
```

A ratio of two standard flows is therefore a ratio of two mole counts, and no
density enters it. Density appears only where a *mass* is genuinely wanted —
heating values are per kilogram, and the mass air/fuel ratio is what a
combustion text tabulates — and it is taken from the same molar volume,

```
ρ = M / Vm          (g/L, which is kg/m³)
```

rather than from the per-gas `RHO_*` figures in `domain/roles.py`, which were
taken at slightly different reference conditions and would make the two routes
disagree in the third decimal.

**The constants.** Everything below is built out of these.

| | CH₄ | H₂ | NH₃ | Air |
|---|---|---|---|---|
| O₂ demanded, mol per mol of fuel | 2.00 | 0.50 | 0.75 | — |
| Molar mass `M`, g/mol | 16.043 | 2.016 | 17.031 | 28.965 |
| Lower heating value, MJ/kg | 50.0 | 120.0 | 18.6 | — |
| Density at 25 °C, 1 atm, kg/m³ | 0.656 | 0.082 | 0.696 | 1.184 |
| Firing rate, kW per SLPM | 0.5465 | 0.1648 | 0.2158 | — |

Dry air is taken as 21 % O₂ by volume.

**Stoichiometric air and φ.** The oxygen demands come from the three complete
reactions:

```
CH4  + 2 O2   ->  CO2 + 2 H2O
2 H2 +   O2   ->  2 H2O
4 NH3 + 3 O2  ->  2 N2 + 6 H2O
```

which give one oxygen balance for whatever mixture is flowing, in SLPM:

```
0.21 · a_stoich = 2.00·CH4 + 0.50·H2 + 0.75·NH3

φ = a_stoich / a_supplied
```

φ is the stoichiometric air requirement over the air actually supplied: above 1
is rich, below 1 is lean. With no fuel flowing, or no air, there is no mixture
to describe and the tile reads `--` rather than 0 or ∞.

In staged mode the balance is struck three times over. Stage 1 is the rich
fuels and the pilot against the rich air (`nh3_rich + h2_rich + ch4_pilot`
against `rich_air`), stage 2 is the lean fuels against the lean air (`nh3_lean +
h2_lean` against `lean_air`), and the global figure is every fuel against every
air — identical to the φ columns in the CSV and to what the auto-calculation
solves against. In standard mode there is one balance, over every assigned fuel
and every assigned air line.

**Firing rate.** Lower heating value per standard litre, summed over the fuels:

```
Q̇ [kW] = Σ  V̇_fuel · LHV_fuel · M_fuel / (Vm · 60)
```

with `V̇` in SLPM. The chain of units is `MJ/kg × g/mol = kJ/mol`, `÷ L/mol =
kJ/L`, `÷ 60 s = kW per L/min`, which is where the per-SLPM figures in the table
come from — methane at about half a kilowatt per SLPM is the one worth carrying
in your head. This is heat *released* by complete combustion, not shaft or
electrical output, and it is not corrected for anything left unburnt. It is the
same quantity the RQL auto-calculation takes as its input, so a stored 10 kW
target should read back as about 10 kW once the flows have settled.

**Air/fuel ratios.** As supplied, and at φ = 1, by volume and then by mass:

```
(A/F)_vol         = a_supplied / Σ V̇_fuel
(A/F)_stoich,vol  = a_stoich   / Σ V̇_fuel
(A/F)_stoich,mass = ρ_air · a_stoich / Σ (ρ_fuel · V̇_fuel)
```

The volume ratios are pure flow arithmetic; the mass ratio is the tabulated one
(17.19 for methane, 34.2 for hydrogen, 6.07 for ammonia), and it is a property
of the fuel blend rather than of the air being supplied, so it stands even with
the air shut. The three agree with φ by construction:
`φ = (A/F)_stoich,vol ÷ (A/F)_vol`.

**Blend fractions.** Each fuel's share of the fuel volume:

```
x_fuel = V̇_fuel / Σ V̇_fuel
```

By volume rather than by mass, because that is what the meters read and what a
blend is set in — "70/30 NH₃/H₂" on this rig has always meant by volume, and the
auto-calculation takes its hydrogen fraction the same way.

**Bulk velocity.** Volumetric flow over the cross-section of a round inlet of
declared diameter `d`, in millimetres:

```
u [m/s] = (V̇_total / 60000) / (π · (d/1000)² / 4)
```

`V̇_total` is everything entering that inlet — fuel, air *and* any non-reacting
line, because nitrogen does not burn but does occupy the duct. 60 SLPM through a
10 mm bore is 12.7 m/s. This is the cold-flow velocity of the mixture at
standard conditions: it is not a flame speed, and it carries no correction for
preheat or for expansion across the flame. Without a declared bore there is no
answer at all, which is the whole reason the box exists.

**What a missing reading does.** A controller that did not answer, or answered
with something unparseable, contributes zero rather than stopping the refresh
halfway through. A small negative flow — what an Alicat reports on a closed line
sitting at zero — is also read as zero, since subtracting it would make a lean
mixture look leaner than it is.

## Recorded sequences

A sequence is what the rig was *asked* to do, not what it did. Open the panel
with **Record / Replay Sequence** in the *Sequence* card. The same card lists
everything already saved under `Documents/Flow Controller/sequences`, newest
first: **clicking a name loads it** into the panel and nothing moves, and the
**▶** beside it loads and runs it once.

- **Record** captures every setpoint the session commands — typed, batched,
  ramped, ignition — as one track of keyframes per assigned controller. The
  monitor has to be running first: nothing is being written to the controllers
  otherwise, so there would be nothing to record. There is no *add key point*
  button, because every setpoint change already lands a keyframe.
- **Stopping** closes the recording and writes it as `*.fcseq.json`.
- **Editing**: drag a point to move it, double-click to add one, right-click to
  delete. A recorded keyframe *holds* until the next one, because a setpoint
  written to a controller does not decay; switch a point to **Ramp (linear)** to
  make the transition out of it smooth. Only the selected track is editable, and
  the axis frames that track, so a 0.4 SLPM pilot is not a flat line on an axis
  scaled for stage air.
- **All tracks (overview)** is the first row of the track list and the view a
  sequence opens on: every controller on one pair of axes, answering what the
  rig as a whole does before you go looking at any one line. Nothing is editable
  there.
- **Replay** walks the keyframes back out through the ordinary setpoint queue,
  so it is subject to every interlock a hand-typed setpoint is. A step edge on
  the pilot or either air line is spread over a second rather than written as a
  jump, and the log says so. Replay needs the monitor running; asking while it
  is stopped offers to start it.
- **Starting is gated on where the rig is standing.** A replay begins by writing
  the sequence's opening setpoints, so starting one from somewhere else makes
  every line jump at `t = 0`. The *measured* flows are compared against those
  opening setpoints, within a couple of percent of each track's largest figure
  and with a small floor so a 0.4 SLPM pilot is not judged on noise. If any line
  disagrees you are told which, with the figure it wants and the figure it has,
  and can either set the flows to match or run it anyway. A line that is not
  reporting counts as no flow.
- **How fast each line moves** during a replay is the **RAMP** figure on that
  controller's own card, so a line that has been slowed down stays slow however
  its setpoint arrives. Sequences recorded before the rate moved onto the cards
  carry a per-track rate of their own; that is still read back, and stands in
  wherever the card has nothing to say.
- **Hold if flows lag** compares each track's measured flow against what it was
  commanded, and stops the replay clock for **every** controller until a lagging
  line catches up, so transitions stay in step rather than the fast lines running
  ahead. Holds are marked on the timeline, named in the log, and time-boxed at
  30 s so a genuinely stuck controller delays the run rather than stalling it
  forever. The check can be turned off, and its tolerance changed, mid-replay.
- **Repeat** (`× N`, or *until stopped*) runs the sequence more than once. Each
  wrap is an ordinary edge rather than a fresh start, so rate-limited lines ramp
  from the closing value back to the opening one instead of stepping, and the log
  names the lines that will move at each wrap before the replay begins.
- **Clear** puts the sequence down so the next recording starts from an empty
  timeline. Files already on disk are untouched.

Stopping the monitor, a zero-flow command, or shutdown all cancel a replay.
Replay is refused rather than half-run if a track's role is not currently
assigned; roles are re-resolved against the assignment in force at replay time,
so a sequence recorded with stage-1 ammonia on unit A still runs after it has
been moved to unit D.

## Logging and data out

**The CSV acquisition log** writes one row per completed serial polling pass:
timestamp, then flow, setpoint, pressure, temperature, internal setpoint error
and first valve drive for each unit, then live φ for stage 1, stage 2 and
global. Column names carry the gas, zone and unit letter. The column set is
decided once, when logging starts, from the units on the monitor at that
moment — including General-zone and custom units — because a file whose columns
shift mid-run cannot be loaded by anything. That is also why zones cannot be
moved while a log is open.

Rows are line-buffered and flushed as they are written, and the file is fsynced
on close. A logging fault can never stop control: a failed write is dropped, not
raised. Failed reads are left blank rather than repeating the previous value, so
a column that holds steady is a controller that is genuinely not moving.

Default destination is `Documents/Flow Controller`.

**The LabVIEW UDP listener** accepts two datagrams and only two: `log` opens a
timestamped copy of the destination file and starts recording, `stop` closes it.
Anything else is reported and discarded. The point is that one operator action
can start both systems' records at the same instant, which is the only way the
two logs line up afterwards. A LabVIEW-triggered log is timestamped because
those arrive unattended and repeatedly, and overwriting the previous capture
would be a silent data loss. A second `log` while one is open is refused rather
than allowed to clobber it. Rows are only written while the monitor is running.

**Graph export** is a separate thing: a dump of the plotted history for someone
doing arithmetic afterwards, from the *Logging & Graphs* tab.

## Appearance

Colours, fonts, corner radii, spacing and the glass alphas are read from
`ui_theme.json` next to the application, and editable in-app from the settings
dialog — which writes that same file, so a hand-edit and a dialog edit cannot
drift apart. **Apply** re-themes the running window without touching the disk,
which is how you actually pick a colour; **Save** writes it. The file is
optional and partial, and anything malformed falls back to defaults rather than
stopping the application. `FLOW_CONTROLLER_UI_CONFIG` overrides its location.

Gas colours are not part of the theme by default: an operator learns that
hydrogen is blue, and it should not move when the theme does.

The window is frameless and draws its own title bar, with minimise, maximise and
close on the same line as the name, so no light strip sits above a dark
instrument panel. Dragging and double-clicking that bar are handed to the window
manager, so snapping and monitor-to-monitor moves work as usual.

## How it is built

```
flow_controller/
  domain/          pure rules and arithmetic — no hardware, no widgets
    roles.py         the rig's vocabulary: roles, zones, gases, colours
    rql.py           firing rate + φ  ->  SLPM per role
    combustion.py    the oxygen balance, firing rate, velocity, A/F
    assignments.py   which auto-calc configuration an assignment supports
    safety.py        which units a zero command targets
    graphing.py      axis limits and rescale hysteresis
    models.py        typed controller and telemetry records
  infrastructure/  the wire
    alicat_protocol.py   raw gas-table and register commands, response parsing
    serial_worker.py     the one asyncio loop all serial work runs on
  services/
    discovery.py     scan a port, then read each device's own gas table
  core/            the control layer, with no widgets in it
    session.py       connecting, polling, setpoints, zero, ramps, ignition
    sequence.py      record, edit, replay, the settle gate
    ramps.py         paced setpoint moves
    telemetry.py     live reads and the per-device capability cache
    csv_logger.py    the acquisition log
    graph_history.py retained samples
    unit_prefs.py    per-meter declarations, remembered between runs
    combustion_prefs.py  inlet bores and how often the estimate runs
    udp_listener.py  the LabVIEW trigger socket
  ui/              PySide6 views, plus the legacy Tk widgets
  app.py           the original Tk application (run.py --legacy)
```

The split is what lets the arithmetic be tested without a controller and
without a display: `domain` and most of `core` import neither serial nor a
toolkit. `core.session` is the one deliberate Qt dependency outside `ui`, and it
is there because results arriving off the serial thread have to reach the GUI
thread, which Qt signals already do correctly.

Views render session signals and call session methods. No serial call, no state
rule and no interlock lives in a tab: the Connect button does not check whether
monitoring is running, it asks, and the session says no.

Alicat firmware varies in what it will answer. The fast path asks for six
statistics in one transaction; units that cannot do that are polled one
statistic at a time instead, and each unit's answer is remembered so the probe
is not repeated.

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

It deliberately leaves the program folder, logs, saved sequences and
`ui_theme.json` alone — that is experiment data, and throwing it away should be
a deliberate act. Delete them by hand once you are sure. Python itself is also
left installed, since other programs may be using it.

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

Tests do not require a controller or a display:

```powershell
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" -m unittest discover -s tests -v
```

They cover the combustion and RQL arithmetic, assignment assessment, the zero
selection rule, protocol parsing, discovery, ramps, the graphing helpers, unit
preferences and the sequence engine.

Before using this build in an experiment, perform a hardware acceptance test
covering scan, per-controller gas choices, connect/readback, setpoints,
monitoring, logging, reconnect, UDP commands, verified fuel zero, and verified
all-flow zero.

## Safety

This is a supervisory control interface. Physical interlocks and independent
emergency shutdown protection should not depend only on Windows, Python, or the
USB serial connection.
