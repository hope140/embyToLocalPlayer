"""Cross-process startup lock for the ETLP service."""

import os
import tempfile


_LOCK_FILE_NAME = 'embyToLocalPlayer.instance.lock'

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when another ETLP process owns the startup lock."""


def default_lock_path():
    """Return the machine-wide lock-file path used by the service."""

    return os.path.join(tempfile.gettempdir(), _LOCK_FILE_NAME)


class InstanceLock:
    """Hold an OS-backed lock without treating the lock file as state."""

    def __init__(self, path=None):
        self.path = os.path.abspath(os.fspath(path or default_lock_path()))
        self._file = None

    def acquire(self):
        """Try to acquire the lock and return whether it was obtained."""

        if self._file is not None:
            return True

        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handle = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                # msvcrt.locking starts at the current file position and
                # requires a byte to exist in the file.
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'\0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

        self._file = handle
        return True

    def release(self):
        """Release the OS lock while leaving the residual lock file intact."""

        handle = self._file
        if handle is None:
            return
        self._file = None
        try:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            # Closing the descriptor still releases the OS lock.  Do not make
            # shutdown fail merely because the descriptor was already closing.
            pass
        finally:
            handle.close()

    def __enter__(self):
        if not self.acquire():
            raise InstanceAlreadyRunningError('another ETLP instance is running')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False
