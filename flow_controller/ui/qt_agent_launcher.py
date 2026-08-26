"""Embedded ConPTY terminal for restricted coding-agent sessions.

The agent runs as a child process in a Windows pseudo-terminal rendered inside
the launcher card. It receives no reference to
:class:`~flow_controller.core.session.FlowSession`; rig access is through the
authenticated MCP surface. The harness flags remain guardrails rather than a
security boundary for a machine that owns the serial port.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from typing import Callable

from PySide6.QtCore import QObject, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
)

import pyte

from . import qt_theme as theme
from .qt_widgets import Card, StatusDot, label, mono


POLL_INTERVAL_MS = 500
TERMINAL_FLUSH_INTERVAL_MS = 33
TERMINAL_MAX_PENDING_CHARS = 256 * 1024
TERMINAL_MAX_FLUSH_CHARS = 32 * 1024


class _WinPtyProcess:
    """Small poll/write adapter over pywinpty's ConPTY process."""

    def __init__(self, process):
        self._process = process
        self.pid = process.pid
        self._forced_exit = False

    @classmethod
    def spawn(cls, command, *, cwd, rows=24, columns=120):
        from winpty import PtyProcess
        return cls(PtyProcess.spawn(
            list(command), cwd=str(cwd), dimensions=(rows, columns)))

    def poll(self):
        if self._forced_exit:
            return -9
        try:
            if self._process.isalive():
                return None
        except OSError:
            # A broken status handle is not evidence that the child exited.
            # Preserve ownership unless pywinpty has an actual exit status.
            status = self._process.exitstatus
            return status if status is not None else None
        return self._process.exitstatus if self._process.exitstatus is not None else 0

    def terminate(self):
        try:
            terminated = self._process.terminate(force=True)
        except (EOFError, OSError, RuntimeError):
            # A failed graceful request still gets the documented force-close
            # path below. Only the force-close failure is allowed to escape.
            terminated = False
        try:
            alive = self._process.isalive()
        except OSError:
            alive = True
        if terminated is False or alive:
            # pywinpty documents close(force=True) as the operation that
            # guarantees a kill or raises. Do not silently orphan an embedded
            # shell when terminate() merely reports False.
            self._process.close(force=True)
        # Either terminate was confirmed and isalive was false, or the
        # documented force-close returned successfully. Both are positive
        # termination results rather than an inference from a broken handle.
        self._forced_exit = True

    def close(self, force=True):
        force = bool(force)
        self._process.close(force=force)
        if force:
            # A successful documented force-close is positive termination
            # proof, even when the PTY status handle can no longer answer.
            self._forced_exit = True

    def force_kill_tree(self):
        """Use Windows' process-tree termination if ConPTY cannot close."""
        if self.poll() is not None:
            return
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        result = subprocess.run(
            ['taskkill', '/PID', str(self.pid), '/T', '/F'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=5, check=False,
            creationflags=creationflags,
        )
        if result.returncode != 0:
            try:
                alive = self._process.isalive()
            except OSError as exc:
                detail = (result.stderr or result.stdout).decode(
                    errors='replace').strip()
                raise OSError(
                    detail or 'taskkill failed and process state is unknown') \
                    from exc
            if alive:
                detail = (result.stderr or result.stdout).decode(
                    errors='replace').strip()
                raise OSError(
                    detail or f'taskkill failed with code {result.returncode}')
        self._forced_exit = True
        try:
            self._process.close(force=False)
        except (EOFError, OSError, RuntimeError):
            # taskkill succeeded; this close only releases the local PTY
            # handle and does not determine whether the child survived.
            pass

    def read(self, size=4096):
        return self._process.read(size)

    def write(self, chars):
        return self._process.write(chars)

    def resize(self, rows, columns):
        self._process.setwinsize(max(2, int(rows)), max(20, int(columns)))


class _EmbeddedTerminal(QPlainTextEdit):
    """Read-only rendering surface that forwards keystrokes to the PTY."""

    control = Signal(str)
    viewport_resized = Signal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.viewport_resized.emit()

    def keyPressEvent(self, event):
        modifiers = event.modifiers()
        if (modifiers & Qt.KeyboardModifier.ControlModifier
                and modifiers & Qt.KeyboardModifier.ShiftModifier
                and event.key() == Qt.Key.Key_C):
            self.copy()
            return
        if (modifiers & Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_V):
            pasted = QApplication.clipboard().text()
            if pasted:
                self.control.emit(pasted)
            return
        if (event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_C):
            self.control.emit('\x03')
            return
        sequences = {
            Qt.Key.Key_Return: '\r',
            Qt.Key.Key_Enter: '\r',
            Qt.Key.Key_Backspace: '\x7f',
            Qt.Key.Key_Up: '\x1b[A',
            Qt.Key.Key_Down: '\x1b[B',
            Qt.Key.Key_Right: '\x1b[C',
            Qt.Key.Key_Left: '\x1b[D',
            Qt.Key.Key_Home: '\x1b[H',
            Qt.Key.Key_End: '\x1b[F',
            Qt.Key.Key_Delete: '\x1b[3~',
            Qt.Key.Key_PageUp: '\x1b[5~',
            Qt.Key.Key_PageDown: '\x1b[6~',
            Qt.Key.Key_Tab: '\t',
            Qt.Key.Key_Escape: '\x1b',
        }
        sequence = sequences.get(event.key())
        if sequence is not None:
            self.control.emit(sequence)
            return
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            key = event.key()
            if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
                self.control.emit(chr(key - Qt.Key.Key_A + 1))
                return
        text = event.text()
        if text and not (modifiers & Qt.KeyboardModifier.ControlModifier):
            self.control.emit(text)
            return
        super().keyPressEvent(event)


@dataclass(frozen=True)
class AgentProfile:
    """The command-line profile for one supported interactive agent."""

    key: str
    label: str
    executable: str
    arguments: tuple[str, ...]
    description: str


# These flags are intentionally conservative. Claude loads no user/project
# settings or slash commands, and its explicit tool list denies shell/edit/write
# tools while still allowing the one command-line MCP configuration. Codex's
# public interactive CLI
# exposes a read-only sandbox and approval policy, but no equivalent public
# "disable shell tool" flag; its profile is therefore read-only best effort,
# not a promise that this window can secure the host machine.
AGENT_PROFILES = {
    'claude': AgentProfile(
        key='claude',
        label='Claude Code',
        executable='claude',
        arguments=('--setting-sources', '', '--disable-slash-commands',
                   '--permission-mode', 'default', '--tools', 'Read'),
        description=('Claude Code: no user/project settings or slash commands; '
                     'only Read and the allowlisted rig MCP tools are offered; '
                     'live rig authority remains default-off in the app.'),
    ),
    'codex': AgentProfile(
        key='codex',
        label='Codex',
        executable='codex',
        arguments=('--sandbox', 'read-only', '--ask-for-approval', 'on-request'),
        description=('Codex: read-only sandbox and approval requests. The Codex '
                     'CLI has no public flag that makes this pane a hard '
                     'shell-denial boundary; live MCP actions still require '
                     'the app toggle and operator approval.'),
    ),
}


def _bundled_codex_path(local_appdata=None):
    """Find the Codex Desktop CLI when its bin folder is absent from PATH."""
    root = Path(local_appdata or os.environ.get('LOCALAPPDATA', ''))
    if not str(root):
        return None
    bin_dir = root / 'OpenAI' / 'Codex' / 'bin'
    candidates = [bin_dir / 'codex.exe']
    try:
        candidates.extend(bin_dir.glob('*/codex.exe'))
    except OSError:
        return None
    existing = []
    for path in candidates:
        try:
            if path.is_file():
                existing.append(path)
        except OSError:
            continue
    if not existing:
        return None
    try:
        return str(max(existing, key=lambda path: path.stat().st_mtime))
    except OSError:
        return str(existing[-1])


def discover_agents(which: Callable[[str], str | None] | None = None, *,
                    local_appdata=None):
    """Return installed launchers as ``{profile_key: executable_path}``.

    Discovery is kept separate from process creation so tests never need to
    rely on an agent being installed on the test machine.
    """
    use_desktop_fallback = which is None
    which = which or shutil.which
    available = {
        key: path
        for key, profile in AGENT_PROFILES.items()
        if (path := which(profile.executable))
    }
    if use_desktop_fallback and 'codex' not in available:
        codex = _bundled_codex_path(local_appdata)
        if codex:
            available['codex'] = codex
    return available


MCP_TOOL_NAMES = (
    'read_snapshot', 'read_history', 'read_derived_state',
    'submit_sequence_draft', 'submit_plan_draft',
    'set_role_setpoint', 'start_armed_plan',
)


def format_plan_authority_review(plan):
    """Render every executable plan decision for the arming confirmation."""
    if plan is None:
        return ""
    lines = [f"Abort procedure: {plan.abort.action}"]
    for index, stage in enumerate(plan.stages, 1):
        if stage.setpoints:
            command = ", ".join(
                f"{role}={value:g} SLPM"
                for role, value in sorted(stage.setpoints.items()))
        else:
            command = f"sequence {stage.sequence}"
        lines.append(f"Stage {index}: {stage.name}")
        lines.append(f"  Command: {command}")
        lines.append(
            f"  Minimum dwell: {stage.min_dwell_s:g} s; timeout: "
            f"{stage.timeout_s:g} s → {stage.on_timeout}")
        if stage.condition is not None:
            targets = ", ".join(
                f"{role}={value:g}"
                for role, value in sorted(stage.condition.targets.items()))
            lines.append(
                f"  Advance when {stage.condition.metric} [{targets}] is within "
                f"±{stage.condition.tolerance:g} for "
                f"{stage.condition.stable_s:g} s")
        else:
            lines.append("  Advance after the minimum dwell/sequence completes")
        if stage.next_stage:
            lines.append(f"  Next stage override: {stage.next_stage}")
    return "\n".join(lines)


def _toml_string(value):
    return json.dumps(str(value))


def launch_command(agent: str, executable: str, project_dir: Path, *,
                   mcp=None, python_executable=None, agent_id=None):
    """Build an argument list for *agent*, with no shell interpolation."""
    profile = AGENT_PROFILES[agent]
    arguments = [str(executable), *profile.arguments]
    if mcp:
        python_executable = str(python_executable or sys.executable)
        agent_id = str(agent_id or uuid.uuid4())
        environment = {
            'FLOW_AGENT_PIPE': mcp['address'],
            'FLOW_AGENT_PIPE_FAMILY': mcp['family'],
            'FLOW_AGENT_TOKEN': mcp['token'],
            'FLOW_AGENT_ID': agent_id,
        }
        server_args = ['-m', 'flow_controller.agent.mcp_server']
        if agent == 'claude':
            # Replace the static Read-only list with Read plus the allowlisted
            # rig tools. Live calls remain default-off inside the application.
            tool_index = arguments.index('--tools')
            arguments[tool_index + 1] = ','.join(
                ('Read', *(f'mcp__flow_controller__{name}'
                           for name in MCP_TOOL_NAMES)))
            config = {
                'mcpServers': {
                    'flow_controller': {
                        'type': 'stdio', 'command': python_executable,
                        'args': server_args, 'env': environment,
                    }
                }
            }
            arguments.extend((
                '--strict-mcp-config', '--mcp-config',
                json.dumps(config, separators=(',', ':'))))
        else:
            environment_table = '{' + ','.join(
                f'{key}={_toml_string(value)}'
                for key, value in environment.items()) + '}'
            server_table = (
                '{command=' + _toml_string(python_executable)
                + ',args=' + json.dumps(server_args)
                + ',env=' + environment_table + '}')
            arguments.extend((
                # Replace the whole configured MCP table: user-configured
                # external tools must not leak into this restricted session.
                '-c', f'mcp_servers={{flow_controller={server_table}}}',
            ))
    # Codex supports an explicit working root.  ``cwd`` is still supplied to
    # Popen for both tools, so Claude opens in the project as well.
    if agent == 'codex':
        arguments.extend(('--cd', str(project_dir)))
    return arguments


class AgentProcessManager(QObject):
    """Own one embedded pseudo-terminal process without blocking Qt."""

    status_changed = Signal(str, str)
    terminal_output = Signal(str)
    terminal_cleared = Signal()

    def __init__(self, project_dir: Path, *, which=None,
                 process_factory=None, platform=None, gateway=None,
                 python_executable=None, authority_service=None, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self._which = which
        self._available = discover_agents(which)
        self._process_factory = process_factory
        self._platform = os.name if platform is None else platform
        self._gateway = gateway
        self.authority_service = authority_service
        self._python_executable = python_executable or sys.executable
        self._process = None
        self._reader_thread = None
        self._agent = None
        self._stopping = False
        self._terminal_lock = threading.Lock()
        self._terminal_pending = ''
        self._terminal_dropped = 0
        self._status = 'Idle — no agent is running and no rig tools are exposed.'

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_process)
        self._terminal_timer = QTimer(self)
        self._terminal_timer.setInterval(TERMINAL_FLUSH_INTERVAL_MS)
        self._terminal_timer.timeout.connect(self._flush_terminal_output)

    @property
    def available_agents(self):
        """Installed agent paths, keyed by ``claude`` or ``codex``."""
        return dict(self._available)

    def refresh_available_agents(self):
        """Refresh PATH/Desktop discovery without restarting the application."""
        self._available = discover_agents(self._which)
        return self.available_agents

    @property
    def active_agent(self):
        return self._agent

    @property
    def status(self):
        return self._status

    def is_running(self):
        return self._process is not None and self._process.poll() is None

    def live_authority_available(self):
        """A running supported profile may be explicitly armed in the app."""
        return self.is_running() and self._agent in AGENT_PROFILES

    def start(self, agent: str):
        """Launch *agent* in the card's Windows ConPTY terminal."""
        if agent not in AGENT_PROFILES:
            raise ValueError(f'Unknown agent launcher: {agent}')
        self.refresh_available_agents()
        if self._process is not None and self._process.poll() is not None:
            self._clear_finished()
        if self.is_running():
            self._set_status('busy', 'An agent is already running. Stop it before launching another.')
            return False
        executable = self._available.get(agent)
        if not executable:
            self._set_status('error', f'{AGENT_PROFILES[agent].label} was not found on this computer.')
            return False

        # Every process begins in Draft mode, even if authority was somehow
        # left enabled by a test harness or external integration.
        self._revoke_live("new agent session started")
        mcp = self._gateway.start() if self._gateway is not None else None
        command = launch_command(
            agent, executable, self.project_dir, mcp=mcp,
            python_executable=self._python_executable,
            agent_id=f'{agent}-{uuid.uuid4()}')
        try:
            if self._process_factory is None:
                if self._platform != 'nt':
                    raise OSError(
                        'The embedded terminal currently requires Windows ConPTY.')
                self._process = _WinPtyProcess.spawn(
                    command, cwd=self.project_dir)
            else:
                self._process = self._process_factory(
                    command, cwd=str(self.project_dir), shell=False)
        except (ImportError, OSError, RuntimeError) as exc:
            self._process = None
            self._revoke_live("agent launch failed")
            if self._gateway is not None:
                self._gateway.shutdown()
            self._set_status('error', f'Could not launch {AGENT_PROFILES[agent].label}: {exc}')
            return False

        self._agent = agent
        self._stopping = False
        self._discard_terminal_output()
        self.terminal_cleared.emit()
        if hasattr(self._process, 'read'):
            self._start_reader(self._process)
        self._poll_timer.start()
        self._terminal_timer.start()
        self._set_status(
            'running',
            f'{AGENT_PROFILES[agent].label} running in the embedded terminal '
            f'(PID {getattr(self._process, "pid", "?")}).',
        )
        return True

    def stop(self):
        """Request that the owned agent exits, without altering any flow."""
        if not self.is_running():
            self._clear_finished()
            return False
        self._stopping = True
        self._revoke_live("agent terminated by operator")
        if self._gateway is not None:
            # Revoke read/draft authority before asking the process to exit.
            self._gateway.shutdown()
        try:
            self._process.terminate()
        except (EOFError, OSError, RuntimeError) as exc:
            self._set_status('error', f'Could not stop the agent: {exc}')
            return False
        self._set_status('stopping', 'Stopping agent — controller setpoints are unchanged.')
        return True

    def shutdown(self):
        """End the owned terminal process and revoke every app capability."""
        self.stop()
        self._revoke_live("application shutting down")
        process = self._process
        failure = None
        if process is not None and process.poll() is None:
            close = getattr(process, 'close', None)
            if close is not None:
                try:
                    close(force=True)
                except TypeError:
                    close()
                except (EOFError, OSError, RuntimeError) as exc:
                    failure = exc
        if process is not None and process.poll() is None:
            force_kill_tree = getattr(process, 'force_kill_tree', None)
            if force_kill_tree is not None:
                try:
                    force_kill_tree()
                except (EOFError, OSError, RuntimeError,
                        subprocess.SubprocessError) as exc:
                    failure = exc
            else:
                failure = failure or OSError(
                    'No process-tree termination fallback is available.')
        if process is not None and process.poll() is None:
            self._set_status(
                'error', 'The agent process is still running; application '
                f'close was blocked. {failure or "Termination failed."}')
            if self._gateway is not None:
                self._gateway.shutdown()
            return False
        if process is not None and process.poll() is not None:
            self._clear_finished()
        self._poll_timer.stop()
        self._terminal_timer.stop()
        self._discard_terminal_output()
        if self._gateway is not None:
            self._gateway.shutdown()
        return True

    def send_input(self, chars):
        """Write user input to the active embedded terminal."""
        if not self.is_running() or not hasattr(self._process, 'write'):
            return False
        try:
            self._process.write(str(chars))
            return True
        except (EOFError, OSError, RuntimeError):
            return False

    def resize_terminal(self, rows, columns):
        if not self.is_running() or not hasattr(self._process, 'resize'):
            return False
        try:
            self._process.resize(rows, columns)
            return True
        except (OSError, RuntimeError):
            return False

    def _start_reader(self, process):
        def read_output():
            while process is self._process:
                try:
                    chunk = process.read(4096)
                except (EOFError, OSError, RuntimeError):
                    break
                if chunk:
                    self._queue_terminal_output(str(chunk))
                elif process.poll() is not None:
                    break

        self._reader_thread = threading.Thread(
            target=read_output, name='flow-agent-terminal', daemon=True)
        self._reader_thread.start()

    def _queue_terminal_output(self, chunk):
        """Bound the reader backlog; Qt consumes it at a fixed frame rate."""
        if not chunk:
            return
        with self._terminal_lock:
            pending = self._terminal_pending + str(chunk)
            overflow = len(pending) - TERMINAL_MAX_PENDING_CHARS
            if overflow > 0:
                pending = pending[overflow:]
                self._terminal_dropped += overflow
            self._terminal_pending = pending

    def _flush_terminal_output(self):
        """Emit at most one bounded terminal update per UI frame."""
        with self._terminal_lock:
            if not self._terminal_pending and not self._terminal_dropped:
                if self._process is None:
                    self._terminal_timer.stop()
                return
            chunk = self._terminal_pending[:TERMINAL_MAX_FLUSH_CHARS]
            self._terminal_pending = self._terminal_pending[len(chunk):]
            dropped = self._terminal_dropped
            self._terminal_dropped = 0
        if dropped:
            self.terminal_cleared.emit()
            chunk = (f'[terminal output truncated: {dropped} characters]\r\n'
                     + chunk)
        if chunk:
            self.terminal_output.emit(chunk)

    def _discard_terminal_output(self):
        with self._terminal_lock:
            self._terminal_pending = ''
            self._terminal_dropped = 0

    def _poll_process(self):
        if self._process is None:
            self._poll_timer.stop()
            return
        if self._process.poll() is None:
            return
        self._clear_finished()

    def _clear_finished(self):
        process = self._process
        agent = self._agent
        stopping = self._stopping
        self._process = None
        self._agent = None
        self._stopping = False
        self._poll_timer.stop()
        self._flush_terminal_output()
        self._revoke_live("agent process exited")
        if self._gateway is not None:
            self._gateway.shutdown()
        if process is None:
            return
        code = process.poll()
        if stopping:
            self._set_status('idle', 'Agent stopped — controller setpoints are unchanged.')
        elif code in (0, None):
            self._set_status('idle', f'{AGENT_PROFILES[agent].label} exited normally.')
        else:
            self._set_status('error', f'{AGENT_PROFILES[agent].label} exited with code {code}.')

    def _revoke_live(self, reason):
        service = self.authority_service
        if service is not None:
            service.authority.revoke(reason)

    def _set_status(self, kind, text):
        self._status = text
        self.status_changed.emit(kind, text)


class AgentLauncherPane(Card):
    """A compact, collapsible view over :class:`AgentProcessManager`."""

    def __init__(self, manager: AgentProcessManager, *, collapsed=True, parent=None):
        super().__init__(
            'Agent launcher', collapsed=collapsed,
            help_text=('Launches Claude Code or Codex in the embedded terminal. '
                       'Live rig authority is an explicit, visible, '
                       'default-off toggle.'),
            parent=parent,
        )
        self.manager = manager
        self.service = manager.authority_service
        self._live_guard = False

        self._status_dot = StatusDot(theme.TEXT_DIM)
        self._header_status = label('idle', color=theme.TEXT_DIM, size=8,
                                    monospace=True)
        self.add_header_widget(self._status_dot)
        self.add_header_widget(self._header_status)

        intro = label(
            'Embedded agent terminal. Reads and drafts are always available. '
            'Live tools remain disabled until the operator explicitly arms '
            'the running agent below.',
            color=theme.TEXT_MUTED, size=8,
        )
        intro.setWordWrap(True)
        self.add(intro)

        self._terminal_screen = pyte.Screen(80, 18)
        self._terminal_stream = pyte.Stream(self._terminal_screen)
        self.terminal = _EmbeddedTerminal()
        self.terminal.setObjectName('AgentEmbeddedTerminal')
        self.terminal.setReadOnly(True)
        self.terminal.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.terminal.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.terminal.setFont(mono(8))
        self.terminal.setMinimumHeight(theme.scale(210))
        self.terminal.setPlaceholderText(
            'Launch Claude Code or Codex to start the embedded terminal.')
        self.terminal.control.connect(self.manager.send_input)
        self.terminal.viewport_resized.connect(self._fit_terminal)
        self.add(self.terminal)

        authority_row = QHBoxLayout()
        authority_row.setContentsMargins(0, 0, 0, 0)
        authority_row.setSpacing(theme.PAD_SM)
        self.live_toggle = QPushButton('LIVE CONTROL OFF')
        self.live_toggle.setCheckable(True)
        self.live_toggle.setProperty('variant', 'quiet')
        self.live_toggle.setProperty('density', 'compact')
        self._live_tooltip = (
            'Default off. When enabled, each individual setpoint still needs '
            'operator confirmation; the currently loaded plan may be armed '
            'inside its exact frozen envelope.')
        self.live_toggle.setToolTip(self._live_tooltip)
        self.live_toggle.toggled.connect(self._on_live_toggled)
        authority_row.addWidget(self.live_toggle)
        self.authority_status = label(
            'No live authority', color=theme.TEXT_DIM, size=8, monospace=True)
        authority_row.addWidget(self.authority_status)
        authority_row.addStretch(1)
        self.add_layout(authority_row)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.PAD_SM)
        self.buttons = {}
        for key, profile in AGENT_PROFILES.items():
            button = QPushButton()
            button.setObjectName('AgentLaunchButton')
            button.setProperty('variant', 'quiet')
            side = theme.scale(36)
            button.setFixedSize(side, side)
            button.setIcon(QIcon(str(
                Path(__file__).with_name('assets') / f'{key}.svg')))
            button.setIconSize(QSize(theme.scale(22), theme.scale(22)))
            button.setAccessibleName(f'Launch {profile.label}')
            button.setEnabled(key in manager.available_agents)
            button.setToolTip(
                f'Launch {profile.label} (restricted)\n\n{profile.description}')
            button.clicked.connect(lambda _checked=False, name=key: self.manager.start(name))
            row.addWidget(button)
            self.buttons[key] = button
        row.addStretch(1)
        self.stop_button = QPushButton('Stop agent')
        self.stop_button.setProperty('variant', 'danger')
        self.stop_button.setProperty('density', 'compact')
        self.stop_button.setToolTip(
            'Ends the external agent process only. This does not zero the rig '
            'or change any controller setpoint.')
        self.stop_button.clicked.connect(self.manager.stop)
        row.addWidget(self.stop_button)
        help_button = QPushButton('Help')
        help_button.setProperty('variant', 'quiet')
        help_button.setProperty('density', 'compact')
        help_button.clicked.connect(self._show_help)
        row.addWidget(help_button)
        self.add_layout(row)

        self._status = QLabel()
        self._status.setObjectName('AgentLauncherStatus')
        self._status.setWordWrap(True)
        self.add(self._status)
        path = label(f'Project directory  {manager.project_dir}', color=theme.TEXT_DIM,
                     size=7, monospace=True)
        path.setWordWrap(True)
        self.add(path)
        self._warning = label(
            'Read-only profiles are guardrails, not a security boundary. '
            'Independent physical interlocks remain required.',
            color=theme.WARN, size=8,
        )
        self._warning.setWordWrap(True)
        self.add(self._warning)

        manager.status_changed.connect(self._on_status_changed)
        manager.terminal_output.connect(self._on_terminal_output)
        manager.terminal_cleared.connect(self._clear_terminal)
        if self.service is not None:
            self.service.set_approval_handler(self._confirm_setpoint)
            self.service.authority.changed.connect(self._on_authority_changed)
        self._on_status_changed('running' if manager.is_running() else 'idle',
                                manager.status)
        self._sync_authority()
        QTimer.singleShot(0, self._fit_terminal)

    def _on_status_changed(self, kind, text):
        colors = {
            'running': theme.OK,
            'stopping': theme.WARN,
            'error': theme.DANGER,
            'busy': theme.WARN,
        }
        color = colors.get(kind, theme.TEXT_DIM)
        self._status_dot.set_color(color)
        self._header_status.setText(kind)
        self._header_status.setStyleSheet(f'color: {color}; background: transparent;')
        self._status.setText(text)
        active = self.manager.is_running()
        if not active:
            self.manager.refresh_available_agents()
        self.stop_button.setEnabled(active)
        self.terminal.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus if active else Qt.FocusPolicy.NoFocus)
        for key, button in self.buttons.items():
            button.setEnabled(not active and key in self.manager.available_agents)
        live_available = (
            active and self.service is not None
            and self.manager.live_authority_available())
        self.live_toggle.setEnabled(live_available)
        self.live_toggle.setToolTip(self._live_tooltip)

    def _clear_terminal(self):
        self._terminal_screen.reset()
        self.terminal.clear()

    def _fit_terminal(self):
        """Match the emulated PTY columns to the sidebar's visible width."""
        viewport = self.terminal.viewport()
        metrics = self.terminal.fontMetrics()
        char_width = max(1, metrics.horizontalAdvance('M'))
        line_height = max(1, metrics.lineSpacing())
        margin = int(self.terminal.document().documentMargin() * 2) + 2
        columns = max(20, (viewport.width() - margin) // char_width)
        rows = max(2, (viewport.height() - margin) // line_height)
        if (columns == self._terminal_screen.columns
                and rows == self._terminal_screen.lines):
            return
        self._terminal_screen.resize(lines=rows, columns=columns)
        self.manager.resize_terminal(rows, columns)

    def _on_terminal_output(self, chunk):
        try:
            self._terminal_stream.feed(str(chunk))
            text = '\n'.join(self._terminal_screen.display).rstrip()
        except (TypeError, ValueError):
            text = self.terminal.toPlainText() + str(chunk)
        self.terminal.setPlainText(text)
        bar = self.terminal.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_live_checked(self, checked):
        self._live_guard = True
        try:
            self.live_toggle.setChecked(bool(checked))
        finally:
            self._live_guard = False

    def _sync_authority(self):
        status = (self.service.authority.status()
                  if self.service is not None else {"enabled": False})
        enabled = bool(status.get('enabled'))
        self._set_live_checked(enabled)
        self.live_toggle.setText(
            'LIVE CONTROL ON' if enabled else 'LIVE CONTROL OFF')
        self.live_toggle.setProperty('variant', 'danger' if enabled else 'quiet')
        self.live_toggle.style().unpolish(self.live_toggle)
        self.live_toggle.style().polish(self.live_toggle)
        if enabled:
            roles = sorted((status.get('envelope') or {}).get('roles', {}))
            self.authority_status.setText(
                f"ARMED · {len(roles)} role(s)")
            self.authority_status.setStyleSheet(
                f'color: {theme.DANGER}; background: transparent;')
        else:
            self.authority_status.setText('No live authority')
            self.authority_status.setStyleSheet(
                f'color: {theme.TEXT_DIM}; background: transparent;')

    def _on_authority_changed(self, _enabled, _reason):
        self._sync_authority()

    def _on_live_toggled(self, checked):
        if self._live_guard:
            return
        if self.service is None or not self.manager.is_running():
            self._set_live_checked(False)
            QMessageBox.warning(
                self, 'Live agent control',
                'Launch an agent before enabling live authority.')
            return
        if not checked:
            try:
                self.service.set_live_enabled(
                    False, reason='operator disabled live control')
            except Exception as exc:
                QMessageBox.critical(self, 'Live agent control', str(exc))
            self._sync_authority()
            return

        try:
            envelope = self.service.preview_live_authority()
        except Exception as exc:
            self._set_live_checked(False)
            QMessageBox.critical(self, 'Cannot enable live control', str(exc))
            return
        roles = envelope.get('roles', {})
        role_lines = '\n'.join(
            f"  {role} → Unit {policy['unit']}: max {policy['max_flow']:g} "
            f"SLPM, ramp {policy['ramp_rate']:g} SLPM/s"
            for role, policy in sorted(roles.items()))
        plan = envelope.get('plan')
        plan_line = (f"Armed plan: {plan['name']} (one start only)\n"
                     f"SHA-256: {plan['fingerprint']}"
                     if plan else 'Armed plan: none')
        loaded_plan = None
        session = getattr(self.service, 'session', None)
        if session is not None:
            loaded_plan = getattr(
                getattr(session, 'experiment_plans', None), 'plan', None)
        plan_review = format_plan_authority_review(loaded_plan)
        if plan_review:
            plan_line += "\n\nExecutable plan review:\n" + plan_review
        profile_warning = ""
        if self.manager.active_agent == 'codex':
            profile_warning = (
                "\n\nIMPORTANT: Codex retains shell access. Its read-only "
                "sandbox is not a hard boundary around the COM port. Enable "
                "only for an attended run with physical interlocks active.")
        answer = QMessageBox.warning(
            self, 'Enable live agent control?',
            "Live control stays enabled until you switch it off or the app "
            "revokes it after a safety-relevant change.\n\n"
            "Every individual setpoint still opens a separate operator "
            "confirmation. The agent cannot exceed these snapshotted limits:\n"
            f"{role_lines}\n\n{plan_line}\n\n"
            "Disconnection, communication fault, monitoring stop, "
            "assignment/limit/ramp changes, "
            "or agent termination revoke authority. A plan already running "
            "continues to its declared end or abort procedure."
            f"{profile_warning}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self._set_live_checked(False)
            return
        try:
            self.service.set_live_enabled(
                True, expected_envelope=envelope)
        except Exception as exc:
            self._set_live_checked(False)
            QMessageBox.critical(self, 'Cannot enable live control', str(exc))
        self._sync_authority()

    def _confirm_setpoint(self, action):
        previous = action.get('previous')
        previous_text = 'unknown' if previous is None else f'{float(previous):g}'
        answer = QMessageBox.warning(
            self, 'Approve agent setpoint?',
            f"Agent: {action['agent']}\n"
            f"Role: {action['role']} (Unit {action['unit']})\n"
            f"Previous command: {previous_text} SLPM\n"
            f"Requested command: {action['value']:g} SLPM\n\n"
            "Approve this one action? The request will be revalidated against "
            "the live assignment, MAX FLOW, ramp ceiling, and toggle state before "
            "it is queued.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _show_help(self):
        QMessageBox.information(
            self, 'Agent launcher',
            'This pane starts an interactive agent in its embedded terminal at '
            'the project directory. Claude Code is launched with Read plus the '
            'allowlisted rig tools; Codex is launched in its read-only sandbox '
            'and asks for approval. Codex still retains shell access, so its '
            'live-control confirmation includes an additional warning: the '
            'sandbox is not a hard COM-port security boundary.\n\n'
            'The flow-controller MCP server exposes telemetry/configuration reads '
            'and draft submission. Live tools are default-off. The operator '
            'must enable the red toggle; every setpoint then needs '
            'a separate confirmation, and only the exact armed plan can be '
            'started once. Stopping an agent '
            'ends that process; it never zeroes controllers or changes their '
            'setpoints. These profiles are not a hard security boundary for '
            'the computer or the rig. Keep independent physical interlocks in use.',
        )
