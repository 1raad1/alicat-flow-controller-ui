# Agent Terminal & Autonomous Test-Condition Plan

Status: Steps 1–7 implemented and covered by automated tests. Live agent
authority is a visible, explicitly armed, default-off toggle.
Scope: launch AI coding agents (Claude Code / Codex) that can author sequences
and condition-based test sequences and, under explicit toggle authority,
control the rig automatically. Codex retains shell access, so its arming
dialog warns that its sandbox is not a hard COM-port boundary. All exposed MCP
hardware authority stays inside the application and its validated interfaces.

---

## 1. Motivation

Two capabilities are wanted:

1. **Agent-authored sequences.** An agent inspects the current rig configuration
   (roles, assignments, ramp policies, limits, live telemetry) and produces a
   sequence the operator reviews and approves in the existing editor.
2. **Autonomous condition transitions.** The rig walks through a series of test
   conditions ("hold until all flows stable within tolerance for 5 s, then advance"),
   with deterministic timeout and abort behaviour, whether the steps were authored
   by a human or an agent.

The second capability is a gap in the app today independent of any AI involvement,
so it is built for human-authored plans first.

## 2. Verified architectural facts this plan relies on

All checked against the current code (branch `codex/live-combustion-ui`):

- `FlowSession` centralises controller state and operations
  (`flow_controller/core/session.py`).
- All setpoint commands converge on one guarded queue — `queue_setpoint()`
  (session.py:1236) — and only the serial monitor loop writes to hardware via
  `_write_pending_setpoints()` (session.py:1181).
- `set_role_setpoint()` (session.py:1283) preserves role assignment and ramp
  protection; it is the correct live-control boundary for any caller.
- Sequences are portable JSON models with no Qt or serial dependencies
  (`flow_controller/core/sequence.py`); `session.set_sequence()` (session.py:2039)
  already loads one into the app and emits `sequence_changed`.
- The sequence loader's `_clean()` (sequence.py:100) **silently coerces** negative
  and non-finite values to 0.0 and enforces no upper bound.
- The settle gate gives up after `SETTLE_MAX_HOLD_S = 30.0` (sequence.py:97) and
  continues — acceptable for attended replay, unsuitable for unattended transitions.
- `unit_prefs` stores only `full_scale` (display-only, per README) and `ramp`;
  there is **no command ceiling** anywhere (`flow_controller/core/unit_prefs.py:46`).
- The replay clock is a `QTimer` on the GUI thread (session.py:293,
  `PreciseTimer`). Setpoint *writes* survive a GUI stall (serial worker), but the
  timing of the *next transition* does not.
- Tests use per-module fakes (`FakeController`, `FakeProtocol`, `FakeHardware`,
  `FakeClock`); there is no simulated rig with flow dynamics.
- `graph_history.series()` and `session.phi_values()` already provide windowed
  history and derived state for the read model.

## 3. Safety model

### 3.1 The terminal is not a trust boundary

A general coding agent with a shell on the machine that owns the COM port can
bypass any in-app envelope: install pyserial and write setpoints directly, kill the
app and take the port, or edit the source. Every software guard below the agent is
therefore **advisory** against a shell-bearing agent. This plan handles that in
three layers:

1. **Restricted launch profiles.** Claude is launched with a
   harness-enforced profile that omits `Bash`/`Write`/`Edit` and offers only Read
   plus the allowlisted rig MCP tools. Codex's read-only sandbox still exposes a
   shell, so its explicit arming confirmation carries an additional attended-run
   warning. Enforcement lives in the harness, not in the model's goodwill.
2. **In-app validation.** Everything in this document: envelopes, fail-closed
   loading, the live-control toggle, and audit.
3. **Physical interlocks.** The real safety layer, as the README already insists.
   Nothing in this plan reduces the need for independent hardware interlocks.

This layering is a decision, not an assumption: layers 1 and 2 reduce accidents and
raise the bar; layer 3 is what actually protects the rig.

### 3.2 Authority levels

Two visibly distinct modes:

- **Draft** — read telemetry and configuration; create sequence drafts.
  Cannot move hardware.
- **Live control** — after one explicit warning, the agent may set values and
  run saved sequences inside the frozen envelope. No per-setpoint dialog is
  shown. Authority is revoked by disconnects, assignment changes,
  communication faults, or agent termination.

### 3.3 Arming vs. abort semantics

Killing an agent revokes its authority to issue anything **new**. A saved
sequence already replaying stays under the app's existing replay and stop
controls. The app, not the agent process, owns the replay clock and command
queue.

### 3.4 Timeout defaults

The default timeout action for every plan stage is **safe shutdown** (the plan's
declared abort procedure). "Hold" means holding a flame at an unverified condition
unattended, so hold is a per-stage opt-in, never the default. The abort/shutdown
procedure is a **required** field of any armed plan, validated at approval time.

### 3.5 Audit

Every admitted agent request (read or write) is logged with agent identity,
timestamp, previous/new values, authority source, and result. Repeated reads of
the same method above 10 calls/s are rejected by in-memory backpressure before
audit I/O and are not logged individually; this prevents polling from turning the
synchronous audit path into a GUI-availability problem. Stopping an agent is a
distinct operation from zeroing the rig and is presented as such in the UI.

## 4. Implementation steps

Ordered so each step is independently useful and the risky work comes after the
infrastructure that de-risks it.

### Step 1 — Read model and simulated rig

- Read-only snapshot API: roles, assignments, current setpoints/flows/pressure/
  temperature, ramp policies, declared limits, connection state.
- Windowed history and derived state, built on `graph_history.series()` and
  `phi_values()` — "stable for N seconds" and "sweep until blowoff" need history,
  not instantaneous values.
- A simulated rig fake with first-order lag on flows, so settle conditions and the
  future plan runner can be exercised headlessly in tests. Prerequisite, not
  follow-up.

*Zero hardware risk; determines whether everything later is useful.*

### Step 2 — Fail-closed sequence loading

- Loading a `.fcseq.json` rejects (or at minimum loudly flags) values that
  `_clean()` would coerce: negatives, NaN/inf, and — once Step 4 lands — values
  above the line's command ceiling.
- A sequence authored for a 100 SLPM meter must fail to load against a 10 SLPM
  limit, not load with silently altered values.

*Small, independent, and must precede agent-authored files: otherwise an operator
can approve a sequence that is not what the agent wrote.*

### Step 3 — Terminal pane, no rig tools

- Collapsible terminal card in the Operation sidebar, launching `claude` /
  `codex` in the project directory under the restricted profiles of §3.1.
  Both profiles remain default-off and require explicit live arming; Codex's
  shell-bearing profile receives the additional security-boundary warning.
- Run each agent as a child process in a Windows ConPTY, with ANSI output
  rendered in the card and direct keyboard input written back through the
  pseudo-terminal; there is no separate message field or interrupt button.
  Reading remains on a background thread so terminal output cannot block Qt.
- Agents author `.fcseq.json` files; the operator opens them through the (now
  fail-closed) loader and reviews in the existing editor.

*First milestone delivered here: agent inspects the rig, generates a sequence,
operator approves — with no live hardware authority.*

### Step 4 — Command ceilings for everyone

- Add `max_flow` to `unit_prefs` alongside `full_scale` and `ramp`.
- Enforce it in `set_role_setpoint()` (session.py:1283) for **all** callers —
  human or agent. One definition of "allowed"; the agent path inherits it for free.
- `full_scale` remains display-only; `max_flow` is the command limit.
- Sequence validation (Step 2) checks drafts against these ceilings.

### Step 5 — Experiment-plan runner (human-authored first)

A deterministic runner above sequences. Each stage declares:

- entry setpoints or a saved sequence;
- advance conditions (e.g. "all flows within tolerance continuously for 5 s"),
  evaluated by the app against live telemetry;
- minimum dwell time;
- timeout, and an explicit timeout action — safe shutdown (default), hold
  (opt-in), abort, or request operator input;
- the next stage.

Requirements:

- No Qt in the runner (follow the `sequence.py` precedent); testable headlessly
  against the Step 1 simulated rig.
- Required abort procedure per plan (§3.4).
- Conditions initially limited to Alicat telemetry (flow, setpoint, pressure,
  temperature). Emissions/flame-detection conditions need a separate telemetry
  adapter — the existing LabVIEW UDP interface only accepts logging commands.
- The app evaluates conditions and performs transitions; agents only author or
  revise plans. Timing and failure handling stay deterministic even if the agent
  process stalls or disappears.

*This is a real feature on its own: useful with zero AI involvement.*

### Step 6 — MCP server (draft authority)

- Local MCP server over authenticated IPC exposing:
  - all Step 1 reads (snapshot, history, derived state, limits);
  - `list_saved_sequences` and `submit_sequence_draft`, which use bounded local
    files and the existing sequence editor.
- No CLI (`flowctl`): one surface to secure, validate, and test. Claude Code and
  Codex both speak MCP natively.
- MCP live calls never import the Alicat library, touch serial objects, or reach
  internal queues; they pass through the authenticated MCP and existing guarded
  session boundary. Codex retains shell access outside that route, which is why
  its arming confirmation explicitly requires an attended, interlocked run.

### Step 7 — Toggle-authorized execution

- **Automatic setpoints:** one operator warning enables
  `set_role_setpoint` calls inside the frozen envelope. Each call is audited but
  does not open another dialog.
- **Saved-sequence execution:** while the operator's live-control toggle is on,
  an agent may call `run_saved_sequence` for a local sequence. Every track and
  keyframe must fit the frozen role, max-flow, and ramp envelope. The file is
  fingerprinted and re-read after pre-execution audit, and replay is refused
  unless measured flows match its opening. Each request starts one pass.
- Only after the runner (Step 5) has been exercised against the simulated rig and
  in attended human-authored runs.

## 5. Explicitly out of scope / rejected

- **`flowctl` CLI** — redundant with MCP; two surfaces to secure for one audience.
- **Agents writing `.fcseq.json` as the *long-term* interface** — Step 3 uses it
  as a stopgap behind the fail-closed loader; Step 6's draft submission replaces it.
- **Agent-only control envelope** — limits are enforced for all callers (Step 4);
  an agent-only envelope leaves the weaker rules for the 2 a.m. human operator.
- **Treating the terminal as a security boundary** — see §3.1.

## 6. Resolved implementation decisions

- The local MCP server uses a named pipe with a rotating per-session token on
  Windows (and an authenticated local socket on other platforms).
- Telemetry adapter design for non-Alicat conditions (emissions, flame detection) —
  needed before plans can gate on them; not needed for Steps 1–7.
- One visible live-control warning authorizes automatic setpoints and saved
  sequences inside the frozen envelope until the toggle is switched off or
  authority is revoked.

## 7. Implemented milestone (2026-08-26)

- Read-only snapshots/history/derived state and a deterministic first-order-lag
  simulated rig.
- Fail-closed sequence parsing and universal per-unit `max_flow` enforcement.
  Limits are rechecked at hardware-write and reconnect time; lowering a limit
  below the current last command invokes verified zero on that controller.
- Embedded ConPTY Claude/Codex terminal in the Operation sidebar. Both profiles
  can be explicitly armed; Codex displays an additional warning because it
  retains shell access. The Agent setup menu runs Codex or Claude sign-in in
  the same terminal, links to each official installation guide, and can refresh
  CLI detection after installation. Authentication sessions never start the MCP
  gateway or expose live authority, and the app does not store provider credentials.
- Authenticated MCP server connected to the running Qt application through a
  per-session local pipe. Credentials rotate when the agent is stopped. Five
  read/draft tools are always available; two live tools are default-off and
  gated by the operator's visible authority envelope.
- Phased JSONL request audit including identity, timestamp, previous/new values,
  approval state, and outcome. An unwritable audit refuses a draft before it
  mutates state and refuses a live action before it executes.
- Default-off persistent **LIVE CONTROL** toggle. Its confirmation
  shows the frozen permitted roles, role-to-unit mapping, MAX FLOW and ramp
  ceilings, plus the rules for agent-selected saved sequences.
- `set_role_setpoint` MCP requests are authorized by the live-control toggle,
  durably audited, and revalidated immediately before entering the existing
  ramped session boundary. `run_saved_sequence` starts one saved sequence pass
  after bounded file loading, validation, fingerprint recheck, authority
  checks, and opening flow matching.
- Live authority is revoked by switching the toggle off, communication fault, disconnect,
  monitoring stop, assignment, limit/ramp change, or agent termination.
  Revocation prevents new actions; an already-running sequence remains under
  the existing replay controls. Saved files are limited to the app sequence
  directory, and repeated read calls are rate-limited before synchronous audit I/O.
