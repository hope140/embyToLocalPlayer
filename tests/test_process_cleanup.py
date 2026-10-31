import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
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
        service_modules = {
            'utils.downloader': SimpleNamespace(prefetch_resume_tv=mock.Mock()),
            'utils.http_server': SimpleNamespace(run_server=mock.Mock()),
            'utils.net_tools': SimpleNamespace(
                check_redirect_cache_expired_loop=mock.Mock()),
        }
        with mock.patch.object(launcher.os, 'chdir'), \
                mock.patch.object(launcher, 'InstanceLock', return_value=instance_lock), \
                mock.patch.object(tools, 'clean_tmp_dir') as clean, \
                mock.patch.object(tools.configs, 'necessary_setting_when_server_start') as config, \
                mock.patch.dict(sys.modules, service_modules):
            self.assertEqual(launcher.main(), 1)

        clean.assert_not_called()
        config.assert_not_called()
        service_modules['utils.http_server'].run_server.assert_not_called()
        instance_lock.release.assert_not_called()

    def test_startup_port_collision_does_not_kill_processes(self):
        events = []
        instance_lock = mock.Mock()
        instance_lock.acquire.side_effect = lambda: events.append('lock') or True

        def mark(name):
            events.append(name)

        with mock.patch.object(launcher.os, 'chdir'), \
                mock.patch.object(tools.configs, 'cwd', 'test-cwd'), \
                mock.patch.object(tools.configs, 'print_version'), \
                mock.patch.object(launcher, 'InstanceLock', return_value=instance_lock), \
                mock.patch.object(tools, 'clean_tmp_dir', side_effect=lambda: mark('clean')), \
                mock.patch.object(tools.configs, 'necessary_setting_when_server_start',
                                  side_effect=lambda: mark('config')), \
                mock.patch.object(launcher.threading, 'Thread') as thread_cls, \
                mock.patch.object(tools, 'MyLogger', return_value=mock.Mock()), \
                mock.patch.dict(sys.modules, {
                    'utils.downloader': SimpleNamespace(prefetch_resume_tv=mock.Mock()),
                    'utils.http_server': SimpleNamespace(
                        run_server=mock.Mock(
                            side_effect=OSError(10048, 'address already in use'))),
                    'utils.net_tools': SimpleNamespace(
                        check_redirect_cache_expired_loop=mock.Mock()),
                }), \
                mock.patch.object(launcher, 'kill_multi_process', create=True) as kill:
            with self.assertRaises(OSError):
                launcher.main()

        kill.assert_not_called()
        instance_lock.acquire.assert_called_once_with()
        instance_lock.release.assert_called_once_with()
        self.assertLess(events.index('lock'), events.index('clean'))
        self.assertLess(events.index('clean'), events.index('config'))
        self.assertEqual(thread_cls.call_count, 2)

    def test_service_import_failure_releases_instance_lock(self):
        instance_lock = mock.Mock()
        instance_lock.acquire.return_value = True
        with mock.patch.object(launcher, 'InstanceLock', return_value=instance_lock), \
                mock.patch.dict(sys.modules, {
                    'utils.downloader': SimpleNamespace(),
                }):
            with self.assertRaises(ImportError):
                launcher.main()

        instance_lock.release.assert_called_once_with()

    def test_lock_contention_exits_before_service_module_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            source_entry, _ = self._prepare_real_entry_fixture(temp_root)
            marker = temp_root / 'service-imports.log'
            lock_probe = temp_root / 'lock-probe.py'
            lock_probe.write_text(
                'import importlib.abc\n'
                'import os\n'
                'from pathlib import Path\n'
                'import runpy\n'
                'import sys\n'
                '\n'
                'class RecordServiceImports(importlib.abc.MetaPathFinder):\n'
                '    _TARGETS = frozenset({\n'
                "        'utils.configs', 'utils.dependency_bootstrap',\n"
                "        'utils.downloader', 'utils.http_server',\n"
                "        'utils.net_tools', 'utils.tools',\n"
                '    })\n'
                '\n'
                '    def find_spec(self, fullname, path=None, target=None):\n'
                '        if fullname in self._TARGETS:\n'
                '            Path(os.environ["ETLP_IMPORT_MARKER"]).write_text(\n'
                '                fullname, encoding="utf-8")\n'
                '            raise ImportError(f"blocked service import: {fullname}")\n'
                '        return None\n'
                '\n'
                'sys.meta_path.insert(0, RecordServiceImports())\n'
                'runpy.run_path(sys.argv[1], run_name="__main__")\n',
                encoding='utf-8')

            lock_temp = temp_root / 'tmp'
            lock_temp.mkdir()
            saved_tempdir = tempfile.tempdir
            saved_env = {
                name: os.environ.get(name)
                for name in ('TEMP', 'TMP', 'ETLP_IMPORT_MARKER')
            }
            owner = None
            try:
                os.environ['TEMP'] = str(lock_temp)
                os.environ['TMP'] = str(lock_temp)
                os.environ['ETLP_IMPORT_MARKER'] = str(marker)
                tempfile.tempdir = str(lock_temp)
                owner = launcher.InstanceLock()
                self.assertTrue(owner.acquire())
                completed = subprocess.run(
                    [sys.executable, str(lock_probe), str(source_entry)],
                    cwd=temp_root,
                    env=os.environ.copy(),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            finally:
                if owner is not None:
                    owner.release()
                tempfile.tempdir = saved_tempdir
                for name, value in saved_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

            details = (
                f'returncode={completed.returncode}, stdout={completed.stdout!r}, '
                f'stderr={completed.stderr!r}, marker={marker.read_text(encoding="utf-8") if marker.exists() else ""!r}'
            )
            self.assertEqual(completed.returncode, 1, details)
            self.assertIn('instance lock is busy', completed.stderr.lower(), details)
            self.assertFalse(marker.exists(), details)

    @staticmethod
    def _prepare_real_entry_fixture(temp_root):
        source_root = Path(__file__).resolve().parents[1]
        entry = temp_root / 'embyToLocalPlayer.py'
        shutil.copy2(source_root / 'embyToLocalPlayer.py', entry)
        shutil.copytree(
            source_root / 'utils',
            temp_root / 'utils',
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
        )

        cache_path = temp_root / 'cache'
        cache_path.mkdir()
        cache_value = cache_path.as_posix()
        (temp_root / 'embyToLocalPlayer_config.ini').write_text(
            f'''[exe]
mpv = mpv

[emby]
player = mpv

[gui]
enable = yes
auto_resume = yes
cache_path = {cache_value}
read_only = no
speed_limit = 0
http_poxy =

[dev]
listen_on_localhost = yes
use_system_proxy = no
log_level = ERROR
log_file =
server_data_group =
prefetch_conf =
redirect_expire_minute =

[clouddrive2]
enable = no

[playlist]

[remote_control]
enable = no
''',
            encoding='utf-8',
        )

        probe = temp_root / 'probe.py'
        probe.write_text(
            '''import importlib.abc
from importlib.machinery import PathFinder
from pathlib import Path
import os
import runpy
import sys
import threading

PORT = int(os.environ['ETLP_TEST_PORT'])
MARKER = Path(os.environ['ETLP_TEST_MARKER'])


def mark(value):
    with MARKER.open('a', encoding='utf-8') as stream:
        stream.write(value + '\\n')


original_thread_start = threading.Thread.start


def recording_thread_start(self, *args, **kwargs):
    target_name = getattr(getattr(self, '_target', None), '__name__', '')
    if target_name in ('update_db_loop', 'resume_or_pause'):
        mark('thread:' + target_name)
    return original_thread_start(self, *args, **kwargs)


threading.Thread.start = recording_thread_start


class HttpServerLoader:
    def __init__(self, loader):
        self.loader = loader

    def create_module(self, spec):
        create_module = getattr(self.loader, 'create_module', None)
        return create_module(spec) if create_module else None

    def exec_module(self, module):
        self.loader.exec_module(module)
        original_run_server = module.run_server

        def run_server(*args, **kwargs):
            mark('http-started')
            return original_run_server(ip='127.0.0.1', port=PORT)

        module.run_server = run_server


class HttpServerFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'utils.http_server':
            return None
        spec = PathFinder.find_spec(fullname, path, target)
        if spec is None:
            return None
        spec.loader = HttpServerLoader(spec.loader)
        return spec


sys.meta_path.insert(0, HttpServerFinder())
runpy.run_path(sys.argv[1], run_name='__main__')
''',
            encoding='utf-8',
        )
        return entry, probe

    @staticmethod
    def _wait_for_server(process, port):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    f'ETLP exited before listening: returncode={process.returncode}, '
                    f'stdout={stdout!r}, stderr={stderr!r}')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.02)
        raise AssertionError(f'ETLP did not listen on temporary port {port}')

    @staticmethod
    def _request_shutdown(port):
        request = (
            f'POST /shutdown/ HTTP/1.1\r\n'
            f'Host: 127.0.0.1:{port}\r\n'
            'Content-Type: application/json\r\n'
            'Content-Length: 2\r\n'
            'X-ETLP-Protocol: 1\r\n'
            'Connection: close\r\n'
            '\r\n'
            '{}'
        ).encode('ascii')
        with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
            client.sendall(request)
            response = bytearray()
            while chunk := client.recv(4096):
                response.extend(chunk)
            return bytes(response)

    @staticmethod
    def _assert_default_lock_released(runtime_temp):
        saved_tempdir = tempfile.tempdir
        tempfile.tempdir = str(runtime_temp)
        try:
            replacement = launcher.InstanceLock()
            try:
                if not replacement.acquire():
                    raise AssertionError('ETLP default lock remained held after child exit')
            finally:
                replacement.release()
        finally:
            tempfile.tempdir = saved_tempdir

    def _start_real_entry(self, root, entry, probe, port, marker, runtime_temp):
        env = os.environ.copy()
        env.update({
            'TEMP': str(runtime_temp),
            'TMP': str(runtime_temp),
            'ETLP_TEST_PORT': str(port),
            'ETLP_TEST_MARKER': str(marker),
        })
        return subprocess.Popen(
            [sys.executable, str(probe), str(entry)],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_real_entry_starts_http_and_releases_lock_after_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry, probe = self._prepare_real_entry_fixture(root)
            runtime_temp = root / 'runtime-temp'
            runtime_temp.mkdir()
            marker = root / 'events.log'
            port_holder = socket.socket()
            port_holder.bind(('127.0.0.1', 0))
            port = port_holder.getsockname()[1]
            port_holder.close()

            process = None
            try:
                for restart in range(2):
                    process = self._start_real_entry(
                        root, entry, probe, port, marker, runtime_temp)
                    try:
                        self._wait_for_server(process, port)
                        response = self._request_shutdown(port)
                        self.assertIn(b'"shutdown": true', response)
                        stdout, stderr = process.communicate(timeout=5)
                        self.assertEqual(process.returncode, 0, stderr)
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            process.communicate(timeout=2)
                    if restart == 0:
                        self._assert_default_lock_released(runtime_temp)

                events = marker.read_text(encoding='utf-8').splitlines()
                self.assertEqual(events.count('http-started'), 2)
                self.assertIn('thread:update_db_loop', events)
                self.assertIn('thread:resume_or_pause', events)
                self._assert_default_lock_released(runtime_temp)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=2)

    def test_real_entry_port_collision_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry, probe = self._prepare_real_entry_fixture(root)
            runtime_temp = root / 'runtime-temp'
            runtime_temp.mkdir()
            marker = root / 'events.log'
            port_holder = socket.socket()
            port_holder.bind(('127.0.0.1', 0))
            port = port_holder.getsockname()[1]

            process = self._start_real_entry(
                root, entry, probe, port, marker, runtime_temp)
            try:
                stdout, stderr = process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0, stdout)
                self.assertIn('http-started', marker.read_text(encoding='utf-8'))
                self._assert_default_lock_released(runtime_temp)
            finally:
                port_holder.close()
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=2)

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
