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
from utils.instance_lock import InstanceLock
from utils.tools import configs, MyLogger, clean_tmp_dir
from utils.net_tools import check_redirect_cache_expired_loop


def main():
    os.chdir(configs.cwd)
    instance_lock = InstanceLock()
    if not instance_lock.acquire():
        MyLogger().error('ETLP instance lock is busy, exit')
        return 1
    try:
        configs.print_version()
        logger = MyLogger()
        logger.info(__file__)
        # The OS-backed instance lock makes broad startup process cleanup both
        # unnecessary and unsafe: an occupied HTTP port must never trigger a
        # kill attempt against an unrelated player process.
        clean_tmp_dir()
        configs.necessary_setting_when_server_start()
        threading.Thread(target=prefetch_resume_tv, daemon=True).start()
        threading.Thread(target=check_redirect_cache_expired_loop, daemon=True).start()
        run_server()  # 主要逻辑入口：utils.http_server.py
    finally:
        instance_lock.release()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
