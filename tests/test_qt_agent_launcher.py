"""Tests for the embedded, restricted agent terminal."""

from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from flow_controller.ui import qt_theme as theme
from flow_controller.ui.qt_agent_launcher import (
    AGENT_PROFILES, AgentLauncherPane, AgentProcessManager,
    _WinPtyProcess, discover_agents, authentication_command, launch_command,
)


class FakeProcess:
    def __init__(self, pid=3141):
        self.pid = pid
        self.returncode = None
        self.terminate_calls = 0
        self.writes = []
        self.resizes = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15

    def close(self, force=False):
        self.returncode = -9 if force else self.returncode

    def write(self, chars):
        self.writes.append(chars)

    def resize(self, rows, columns):
        self.resizes.append((rows, columns))


class FakeAuthority(QObject):
    changed = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.enabled = False
        self.envelope = {
            "roles": {"nh3_rich": {
                "unit": "A", "max_flow": None, "ramp_rate": None,
                "ramp_off": True}},
            "excluded_roles": [], "plan": None,
        }

    def status(self):
        return {
            "enabled": self.enabled,
            "envelope": self.envelope if self.enabled else None,
        }

    def revoke(self, reason):
        changed = self.enabled
        self.enabled = False
        if changed:
            self.changed.emit(False, reason)
        return changed


class FakeAuthorityService:
    def __init__(self):
        self.authority = FakeAuthority()

    def preview_live_authority(self):
        return self.authority.envelope

    def set_live_enabled(self, enabled, **_kwargs):
        self.authority.enabled = bool(enabled)
        self.authority.changed.emit(bool(enabled), "operator")
        return self.authority.status()


class AgentLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self, process, available=('claude', 'codex')):
        paths = {name: f'C:/agents/{name}.exe' for name in available}
        popen = Mock(return_value=process)
        manager = AgentProcessManager(
            Path('C:/flow-controller-project'), which=paths.get,
            process_factory=popen, platform='nt')
        return manager, popen

    def test_claude_launch_uses_list_arguments_and_read_only_profile(self):
        process = FakeProcess()
        manager, popen = self._manager(process)

        self.assertTrue(manager.start('claude'))

        command = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(command[0], 'C:/agents/claude.exe')
        self.assertIn('--setting-sources', command)
        self.assertEqual(command[command.index('--setting-sources') + 1], '')
        self.assertEqual(command[command.index('--tools') + 1], 'Read')
        self.assertEqual(command[command.index('--permission-mode') + 1], 'default')
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['cwd'], 'C:\\flow-controller-project')
        self.assertEqual(manager.active_agent, 'claude')

    def test_codex_launch_has_its_read_only_sandbox_and_project_root(self):
        command = launch_command('codex', 'C:/agents/codex.exe', Path('C:/project'))

        self.assertEqual(command[:5], [
            'C:/agents/codex.exe', '--sandbox', 'read-only',
            '--ask-for-approval', 'on-request',
        ])
        self.assertEqual(command[-2:], ['--cd', 'C:\\project'])

    def test_provider_login_commands_are_explicit_argument_lists(self):
        self.assertEqual(
            authentication_command('codex', 'codex.exe'),
            ['codex.exe', 'login'])
        self.assertEqual(
            authentication_command('claude', 'claude.exe'),
            ['claude.exe', 'auth', 'login'])

    def test_sign_in_uses_terminal_without_starting_mcp_or_live_authority(self):
        process = FakeProcess()
        gateway = Mock()
        service = FakeAuthorityService()
        popen = Mock(return_value=process)
        manager = AgentProcessManager(
            Path('C:/flow-controller-project'),
            which={'codex': 'C:/agents/codex.exe'}.get,
            process_factory=popen, platform='nt', gateway=gateway,
            authority_service=service)
        self.addCleanup(manager.shutdown)
        pane = AgentLauncherPane(manager, collapsed=False)
        self.addCleanup(pane.close)

        self.assertTrue(manager.start_auth('codex'))

        self.assertEqual(
            popen.call_args.args[0], ['C:/agents/codex.exe', 'login'])
        gateway.start.assert_not_called()
        self.assertFalse(manager.live_authority_available())
        self.assertFalse(pane.live_toggle.isEnabled())
        self.assertFalse(service.authority.enabled)

    def test_default_launcher_uses_embedded_conpty_backend(self):
        process = FakeProcess()
        manager = AgentProcessManager(
            Path('C:/flow-controller-project'),
            which={'codex': 'C:/agents/codex.exe'}.get, platform='nt')
        self.addCleanup(manager.shutdown)
        with patch(
                'flow_controller.ui.qt_agent_launcher._WinPtyProcess.spawn',
                return_value=process) as spawn:
            self.assertTrue(manager.start('codex'))
        command = spawn.call_args.args[0]
        self.assertEqual(command[0], 'C:/agents/codex.exe')
        self.assertEqual(
            spawn.call_args.kwargs['cwd'], Path('C:/flow-controller-project'))

    def test_codex_desktop_cli_is_found_when_it_is_missing_from_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = (Path(directory) / 'OpenAI' / 'Codex' / 'bin' /
                          'build-id' / 'codex.exe')
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch(
                    'flow_controller.ui.qt_agent_launcher.shutil.which',
                    return_value=None):
                available = discover_agents(
                    None, local_appdata=directory)
        self.assertEqual(available['codex'], str(executable))

    def test_mcp_configuration_exposes_allowlisted_gated_live_tools(self):
        mcp = {"address": r"\\.\pipe\test", "family": "AF_PIPE",
               "token": "ab" * 32}
        claude = launch_command(
            "claude", "claude.exe", Path("C:/project"), mcp=mcp,
            python_executable="python.exe", agent_id="test-agent")
        tools = claude[claude.index("--tools") + 1]
        self.assertIn("mcp__flow_controller__read_snapshot", tools)
        self.assertIn("mcp__flow_controller__list_saved_sequences", tools)
        self.assertIn("mcp__flow_controller__read_sequence", tools)
        self.assertIn("mcp__flow_controller__submit_sequence_draft", tools)
        self.assertIn("mcp__flow_controller__create_sequence_variant", tools)
        self.assertIn(
            "mcp__flow_controller__prepare_combustion_condition", tools)
        self.assertIn("mcp__flow_controller__set_role_setpoint", tools)
        self.assertIn("mcp__flow_controller__run_saved_sequence", tools)
        allowed = claude[claude.index("--allowedTools") + 1]
        self.assertIn("mcp__flow_controller__set_role_setpoint", allowed)
        self.assertIn("mcp__flow_controller__run_saved_sequence", allowed)
        config = json.loads(claude[claude.index("--mcp-config") + 1])
        server = config["mcpServers"]["flow_controller"]
        self.assertEqual(server["command"], "python.exe")
        self.assertEqual(server["env"]["FLOW_AGENT_ID"], "test-agent")

        codex = launch_command(
            "codex", "codex.exe", Path("C:/project"), mcp=mcp,
            python_executable="python.exe", agent_id="test-agent")
        mcp_override = next(item for item in codex
                            if item.startswith("mcp_servers={"))
        self.assertIn("flow_controller={command=", mcp_override)
        self.assertIn('default_tools_approval_mode="approve"', mcp_override)

    def test_stop_is_nonblocking_and_explicitly_does_not_change_setpoints(self):
        process = FakeProcess()
        manager, _popen = self._manager(process, available=('codex',))
        manager.start('codex')

        self.assertTrue(manager.stop())
        self.assertEqual(process.terminate_calls, 1)
        self.assertFalse(manager.is_running())
        self.assertIn('setpoints are unchanged', manager.status)

        manager._poll_process()
        self.assertIsNone(manager.active_agent)
        self.assertIn('setpoints are unchanged', manager.status)

    def test_shutdown_requests_stop_without_waiting_for_the_console(self):
        class StubbornProcess(FakeProcess):
            def terminate(self):
                self.terminate_calls += 1

        process = StubbornProcess()
        manager, _popen = self._manager(process, available=('claude',))
        manager.start('claude')

        manager.shutdown()

        self.assertEqual(process.terminate_calls, 1)
        self.assertFalse(manager._poll_timer.isActive())
        self.assertFalse(manager.is_running())
        self.assertIsNone(manager.active_agent)

    def test_winpty_adapter_force_closes_when_terminate_fails(self):
        terminal = Mock()
        terminal.pid = 42
        terminal.terminate.side_effect = OSError('terminate failed')
        terminal.isalive.return_value = True
        process = _WinPtyProcess(terminal)

        process.terminate()

        terminal.close.assert_called_once_with(force=True)
        self.assertEqual(process.poll(), -9)

    def test_winpty_adapter_uses_process_tree_kill_if_force_close_fails(self):
        terminal = Mock()
        terminal.pid = 43
        terminal.terminate.return_value = False
        terminal.isalive.return_value = True
        terminal.close.side_effect = OSError('pty close failed')
        process = _WinPtyProcess(terminal)
        result = Mock(returncode=0, stdout=b'', stderr=b'')

        with patch(
                'flow_controller.ui.qt_agent_launcher.subprocess.run',
                return_value=result) as taskkill:
            with self.assertRaises(OSError):
                process.terminate()
            process.force_kill_tree()

        self.assertEqual(process.poll(), -9)
        self.assertEqual(taskkill.call_args.args[0],
                         ['taskkill', '/PID', '43', '/T', '/F'])

    def test_winpty_status_error_is_unknown_not_a_confirmed_exit(self):
        terminal = Mock()
        terminal.pid = 44
        terminal.isalive.side_effect = OSError('status unavailable')
        terminal.exitstatus = None

        self.assertIsNone(_WinPtyProcess(terminal).poll())

    def test_failed_taskkill_with_unknown_status_is_not_reported_as_exit(self):
        terminal = Mock()
        terminal.pid = 45
        terminal.isalive.side_effect = OSError('status unavailable')
        terminal.exitstatus = None
        process = _WinPtyProcess(terminal)
        result = Mock(returncode=5, stdout=b'', stderr=b'access denied')

        with patch(
                'flow_controller.ui.qt_agent_launcher.subprocess.run',
                return_value=result):
            with self.assertRaisesRegex(OSError, 'access denied'):
                process.force_kill_tree()

        self.assertIsNone(process.poll())

    def test_shutdown_accepts_successful_force_close_retry(self):
        terminal = Mock()
        terminal.pid = 46
        terminal.terminate.return_value = False
        terminal.isalive.side_effect = OSError('status unavailable')
        terminal.exitstatus = None
        terminal.close.side_effect = [OSError('first close failed'), None]
        process = _WinPtyProcess(terminal)
        manager = AgentProcessManager(
            Path('C:/flow-controller-project'), which=lambda _name: None,
            process_factory=Mock(), platform='nt')
        manager._process = process
        manager._agent = 'codex'
        manager._poll_timer.start()

        with patch(
                'flow_controller.ui.qt_agent_launcher.subprocess.run') as taskkill:
            self.assertTrue(manager.shutdown())

        taskkill.assert_not_called()
        self.assertIsNone(manager.active_agent)
        self.assertEqual(terminal.close.call_count, 2)

    def test_shutdown_failure_keeps_process_tracked_and_polling(self):
        class UnkillableProcess(FakeProcess):
            def terminate(self):
                self.terminate_calls += 1
                raise OSError('terminate failed')

            def close(self, force=False):
                raise OSError('close failed')

            def force_kill_tree(self):
                raise OSError('tree kill failed')

        process = UnkillableProcess()
        manager, _popen = self._manager(process, available=('codex',))
        manager.start('codex')

        self.assertFalse(manager.shutdown())
        self.assertTrue(manager.is_running())
        self.assertEqual(manager.active_agent, 'codex')
        self.assertTrue(manager._poll_timer.isActive())
        self.assertIn('close was blocked', manager.status)
        manager._poll_timer.stop()
        manager._terminal_timer.stop()

    def test_missing_agent_is_not_started(self):
        process = FakeProcess()
        manager, popen = self._manager(process, available=())

        self.assertFalse(manager.start('claude'))
        popen.assert_not_called()
        self.assertIn('was not found', manager.status)

    def test_start_clears_a_finished_process_before_launching_replacement(self):
        first = FakeProcess(pid=1)
        second = FakeProcess(pid=2)
        popen = Mock(side_effect=(first, second))
        service = FakeAuthorityService()
        manager = AgentProcessManager(
            Path('C:/flow-controller-project'),
            which={'claude': 'C:/agents/claude.exe'}.get,
            process_factory=popen, platform='nt', authority_service=service)
        manager.start('claude')
        service.authority.enabled = True
        first.returncode = 0

        self.assertTrue(manager.start('claude'))

        self.assertFalse(service.authority.enabled)
        self.assertEqual(manager._process.pid, 2)
        self.assertEqual(popen.call_count, 2)

    def test_qt_pane_is_collapsible_and_only_enables_detected_agents(self):
        process = FakeProcess()
        manager, _popen = self._manager(process, available=('claude',))
        pane = AgentLauncherPane(manager, collapsed=True)

        self.assertTrue(pane.is_collapsed())
        self.assertTrue(pane.buttons['claude'].isEnabled())
        self.assertFalse(pane.buttons['codex'].isEnabled())
        self.assertEqual(pane.buttons['claude'].text(), '')
        self.assertFalse(pane.buttons['claude'].icon().isNull())
        self.assertEqual(
            pane.buttons['claude'].accessibleName(), 'Launch Claude Code')
        self.assertEqual(pane.buttons['codex'].text(), '')
        self.assertFalse(pane.buttons['codex'].icon().isNull())
        self.assertEqual(pane.buttons['codex'].accessibleName(), 'Launch Codex')
        self.assertEqual(
            pane.buttons['claude'].size(),
            pane.buttons['codex'].size())
        self.assertEqual(
            pane.buttons['claude'].width(), theme.scale(36))
        self.assertEqual(
            pane.buttons['claude'].iconSize().width(), theme.scale(22))
        self.assertFalse(pane.stop_button.isEnabled())
        self.assertTrue(pane.setup_button.isEnabled())
        self.assertEqual(set(pane.sign_in_actions), {'claude', 'codex'})
        pane.set_collapsed(False, animate=False)
        self.assertFalse(pane.is_collapsed())
        self.assertIn('not a security boundary', pane._warning.text())
        self.assertTrue(pane.terminal.isReadOnly())
        self.assertFalse(hasattr(pane, 'terminal_input'))
        self.assertFalse(hasattr(pane, 'interrupt_button'))
        pane.close()

    def test_terminal_columns_follow_the_visible_sidebar_width(self):
        process = FakeProcess()
        manager, _popen = self._manager(process, available=('codex',))
        pane = AgentLauncherPane(manager, collapsed=False)
        self.addCleanup(pane.close)
        self.assertTrue(manager.start('codex'))

        pane.terminal.resize(300, 210)
        self.app.processEvents()
        pane._fit_terminal()

        self.assertLess(pane._terminal_screen.columns, 80)
        self.assertEqual(
            process.resizes[-1],
            (pane._terminal_screen.lines, pane._terminal_screen.columns))
        self.assertEqual(
            pane.terminal.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def test_embedded_terminal_renders_output_and_sends_input(self):
        process = FakeProcess()
        manager, _popen = self._manager(process, available=('codex',))
        pane = AgentLauncherPane(manager, collapsed=False)
        self.addCleanup(pane.close)
        self.assertTrue(manager.start('codex'))

        manager.terminal_output.emit('Codex ready\r\n')
        self.app.processEvents()
        self.assertIn('Codex ready', pane.terminal.toPlainText())

        pane.terminal.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_I,
            Qt.KeyboardModifier.NoModifier, 'i'))
        pane.terminal.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Return,
            Qt.KeyboardModifier.NoModifier, '\r'))
        pane.terminal.keyPressEvent(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier, ''))
        self.assertEqual(process.writes, ['i', '\r', '\x03'])

    def test_embedded_terminal_keeps_scrolled_output_history(self):
        process = FakeProcess()
        manager, _popen = self._manager(process, available=('codex',))
        pane = AgentLauncherPane(manager, collapsed=False)
        self.addCleanup(pane.close)
        self.assertTrue(manager.start('codex'))

        visible_lines = pane._terminal_screen.lines
        output = ''.join(
            f'line {number}\r\n'
            for number in range(visible_lines + 8))
        manager.terminal_output.emit(output)
        self.app.processEvents()

        rendered = pane.terminal.toPlainText()
        self.assertIn('line 0', rendered)
        self.assertIn(f'line {visible_lines + 7}', rendered)
        self.assertGreater(
            pane.terminal.document().blockCount(), visible_lines)

    def test_terminal_output_is_coalesced_and_bounded(self):
        process = FakeProcess()
        manager, _popen = self._manager(process, available=('codex',))
        chunks = []
        manager.terminal_output.connect(chunks.append)

        manager._queue_terminal_output('one')
        manager._queue_terminal_output('two')
        self.assertEqual(chunks, [])
        manager._flush_terminal_output()
        self.assertEqual(chunks, ['onetwo'])

        manager._queue_terminal_output('x' * (300 * 1024))
        self.assertLessEqual(
            len(manager._terminal_pending), 256 * 1024)
        manager._flush_terminal_output()
        self.assertIn('terminal output truncated', chunks[-1])

    def test_profile_text_is_explicit_about_codex_limit(self):
        self.assertIn('a hard shell-denial boundary',
                      AGENT_PROFILES['codex'].description)

    def test_live_toggle_is_default_off_and_requires_explicit_confirmation(self):
        process = FakeProcess()
        paths = {'claude': 'C:/agents/claude.exe'}
        service = FakeAuthorityService()
        manager = AgentProcessManager(
            Path('C:/flow-controller-project'), which=paths.get,
            process_factory=Mock(return_value=process), platform='nt',
            authority_service=service)
        self.addCleanup(manager.shutdown)
        pane = AgentLauncherPane(manager, collapsed=False)
        self.addCleanup(pane.close)
        self.assertFalse(pane.live_toggle.isChecked())
        self.assertFalse(pane.live_toggle.isEnabled())
        self.assertFalse(hasattr(pane, 'duration_minutes'))

        manager.start('claude')
        self.assertTrue(pane.live_toggle.isEnabled())
        with patch.object(
                QMessageBox, 'warning',
                return_value=QMessageBox.StandardButton.Yes) as confirm:
            pane.live_toggle.click()
        self.assertTrue(confirm.called)
        self.assertIn('ONLY CONTROL WARNING', confirm.call_args.args[2])
        self.assertIn('same setpoint rules', confirm.call_args.args[2])
        self.assertIn('no MAX FLOW, ramp OFF (step)', confirm.call_args.args[2])
        self.assertTrue(service.authority.enabled)
        self.assertEqual(pane.live_toggle.text(), 'LIVE CONTROL ON')

        pane.live_toggle.click()
        self.assertFalse(service.authority.enabled)
        self.assertEqual(pane.live_toggle.text(), 'LIVE CONTROL OFF')

    def test_codex_profile_can_explicitly_enable_live_authority(self):
        process = FakeProcess()
        service = FakeAuthorityService()
        manager = AgentProcessManager(
            Path('C:/flow-controller-project'),
            which={'codex': 'C:/agents/codex.exe'}.get,
            process_factory=Mock(return_value=process), platform='nt',
            authority_service=service)
        self.addCleanup(manager.shutdown)
        pane = AgentLauncherPane(manager, collapsed=False)
        self.addCleanup(pane.close)

        self.assertTrue(manager.start('codex'))
        self.assertTrue(manager.live_authority_available())
        self.assertTrue(pane.live_toggle.isEnabled())
        with patch.object(
                QMessageBox, 'warning',
                return_value=QMessageBox.StandardButton.Yes) as confirm:
            pane.live_toggle.click()
        self.assertTrue(service.authority.enabled)
        self.assertIn('Codex retains shell access', confirm.call_args.args[2])

if __name__ == '__main__':
    unittest.main()
