import os
import sys
import threading


try:
    sys.path.insert(0, os.path.dirname(__file__))
except Exception:
    pass

try:
    from utils.dependency_bootstrap import ensure_bundled_dependencies
    ensure_bundled_dependencies()
except Exception:
    # Dependency loading is best-effort; the regular fallback/error handling
    # should keep non-CD2 playback available if extraction is not possible.
    pass

from utils.downloader import prefetch_resume_tv
from utils.http_server import run_server
from utils.tools import (configs, MyLogger, kill_multi_process, clean_tmp_dir)
from utils.net_tools import check_redirect_cache_expired_loop

if __name__ == '__main__':
    os.chdir(configs.cwd)
    configs.print_version()
    if configs.raw.getboolean('dev', 'kill_process_at_start', fallback=True):
        if os.name == 'nt':
            # On Windows, player selection must use the process image name.
            # Keep command-line matching only for the legacy ETLP Python
            # process (and its historical AutoHotkey helper marker).
            from utils.windows_tool import WINDOWS_PLAYER_EXECUTABLE_NAMES

            process_name_re = (r'embyToLocalPlayer\.py(?=["\'\s]|$)|'
                               r'autohotkey_tool')
            executable_names = WINDOWS_PLAYER_EXECUTABLE_NAMES
        else:
            # Preserve the existing ps/command-line behavior on non-Windows.
            process_name_re = (f'(embyToLocalPlayer.py|autohotkey_tool|' +
                               r'mpv.*exe|mpc-.*exe|vlc.exe|PotPlayer.*exe|' +
                               r'/IINA|/VLC|/mpv)')
            executable_names = None
        kill_multi_process(name_re=process_name_re,
                           executable_names=executable_names,
                           not_re='(tmux|greasyfork|github)')

    logger = MyLogger()
    logger.info(__file__)
    clean_tmp_dir()
    configs.necessary_setting_when_server_start()
    threading.Thread(target=prefetch_resume_tv, daemon=True).start()
    threading.Thread(target=check_redirect_cache_expired_loop, daemon=True).start()
    run_server()  # 主要逻辑入口：utils.http_server.py
