import json
import unittest
from types import SimpleNamespace
from unittest import mock

import embyToLocalPlayer as launcher
import utils.tools as tools
import utils.windows_tool as windows_tool


class WindowsProcessCleanupTests(unittest.TestCase):
    def test_startup_lock_failure_skips_initialization(self):
        instance_lock = mock.Mock()
        instance_lock.acquire.return_value = False
        with mock.patch.object(launcher.os, 'chdir'), \
                mock.patch.object(launcher.configs, 'cwd', 'test-cwd'), \
                mock.patch.object(launcher, 'InstanceLock', return_value=instance_lock), \
                mock.patch.object(launcher, 'clean_tmp_dir') as clean, \
                mock.patch.object(launcher.configs, 'necessary_setting_when_server_start') as config, \
                mock.patch.object(launcher, 'run_server') as run_server:
            self.assertEqual(launcher.main(), 1)

        clean.assert_not_called()
        config.assert_not_called()
        run_server.assert_not_called()
        instance_lock.release.assert_not_called()

    def test_startup_port_collision_does_not_kill_processes(self):
        events = []
        instance_lock = mock.Mock()
        instance_lock.acquire.side_effect = lambda: events.append('lock') or True

        def mark(name):
            events.append(name)

        with mock.patch.object(launcher.os, 'chdir'), \
                mock.patch.object(launcher.configs, 'cwd', 'test-cwd'), \
                mock.patch.object(launcher.configs, 'print_version'), \
                mock.patch.object(launcher, 'InstanceLock', return_value=instance_lock), \
                mock.patch.object(launcher, 'clean_tmp_dir', side_effect=lambda: mark('clean')), \
                mock.patch.object(launcher.configs, 'necessary_setting_when_server_start',
                                  side_effect=lambda: mark('config')), \
                mock.patch.object(launcher.threading, 'Thread') as thread_cls, \
                mock.patch.object(launcher, 'run_server',
                                  side_effect=OSError(10048, 'address already in use')), \
                mock.patch.object(launcher, 'kill_multi_process', create=True) as kill:
            with self.assertRaises(OSError):
                launcher.main()

        kill.assert_not_called()
        instance_lock.acquire.assert_called_once_with()
        instance_lock.release.assert_called_once_with()
        self.assertLess(events.index('lock'), events.index('clean'))
        self.assertLess(events.index('clean'), events.index('config'))
        self.assertEqual(thread_cls.call_count, 2)

    def test_process_image_name_excludes_embedded_mpv_from_electron(self):
        processes = [
            {
                'ProcessId': 101,
                'Name': 'electron.exe',
                'ExecutablePath': r'C:\Emby Theater\electron.exe',
                'CommandLine': r'"C:\Emby Theater\electron.exe" C:\Emby Theater\mpv\mpv.exe',
            },
            {
                'ProcessId': 102,
                'Name': 'mpv.exe',
                'ExecutablePath': r'C:\Program140\mpv\mpv.exe',
                'CommandLine': r'"C:\Program140\mpv\mpv.exe" --idle',
            },
            {
                'ProcessId': 103,
                'Name': 'mpc-hc64.exe',
                'ExecutablePath': r'C:\MPC-HC\mpc-hc64.exe',
                'CommandLine': r'"C:\MPC-HC\mpc-hc64.exe"',
            },
            {
                'ProcessId': 104,
                'Name': 'mpc.exe',
                'ExecutablePath': r'C:\MPC\mpc.exe',
                'CommandLine': r'"C:\MPC\mpc.exe"',
            },
            {
                'ProcessId': 105,
                'Name': 'vlc.exe',
                'ExecutablePath': r'C:\VLC\vlc.exe',
                'CommandLine': r'"C:\VLC\vlc.exe"',
            },
            {
                'ProcessId': 106,
                'Name': 'PotPlayerMini64.exe',
                'ExecutablePath': r'C:\PotPlayer\PotPlayerMini64.exe',
                'CommandLine': r'"C:\PotPlayer\PotPlayerMini64.exe"',
            },
            {
                'ProcessId': 107,
                'Name': 'python.exe',
                'ExecutablePath': r'C:\Python\python.exe',
                'CommandLine': r'python.exe "F:\\embyToLocalPlayer\\embyToLocalPlayer.py"',
            },
            {
                'ProcessId': 108,
                'Name': 'helper.exe',
                'ExecutablePath': r'C:\Helper\helper.exe',
                'CommandLine': r'helper.exe C:\\mpv\\mpv.exe',
            },
        ]
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(processes))

        with mock.patch.object(windows_tool.subprocess, 'run', return_value=completed):
            result = windows_tool.list_pid_and_cmd(
                name_re=r'embyToLocalPlayer\.py(?=["\s]|$)',
                executable_names=windows_tool.WINDOWS_PLAYER_EXECUTABLE_NAMES,
            )

        self.assertEqual({pid for pid, _ in result}, {102, 103, 104, 105, 106, 107})
        self.assertNotIn(101, {pid for pid, _ in result})
        self.assertNotIn(108, {pid for pid, _ in result})

    def test_current_and_parent_processes_remain_protected(self):
        candidates = [
            (100, 'current'),
            (101, 'parent'),
            (102, 'old player'),
        ]
        with mock.patch.object(tools.os, 'name', 'nt'), \
                mock.patch.object(tools.os, 'getpid', return_value=100), \
                mock.patch.object(tools.os, 'getppid', return_value=101), \
                mock.patch('utils.windows_tool.list_pid_and_cmd', return_value=candidates) as list_processes, \
                mock.patch.object(tools.os, 'kill') as kill, \
                mock.patch.object(tools.time, 'sleep'):
            tools.kill_multi_process(
                name_re=r'embyToLocalPlayer\.py',
                executable_names={'mpv.exe'},
            )

        list_processes.assert_called_once_with(
            r'embyToLocalPlayer\.py', executable_names={'mpv.exe'}
        )
        kill.assert_called_once_with(102, tools.signal.SIGABRT)


if __name__ == '__main__':
    unittest.main()
