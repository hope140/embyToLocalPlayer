import multiprocessing
import tempfile
import unittest
from pathlib import Path

from utils.instance_lock import InstanceLock


def _hold_lock(path, ready, release):
    lock = InstanceLock(path)
    if not lock.acquire():
        ready.put('failed')
        return
    ready.put('acquired')
    release.wait(timeout=10)
    lock.release()


class InstanceLockTests(unittest.TestCase):
    def test_two_processes_compete_and_released_lock_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'etlp.lock'
            context = multiprocessing.get_context('spawn')
            ready = context.Queue()
            release = context.Event()
            process = context.Process(target=_hold_lock, args=(path, ready, release))
            process.start()
            try:
                self.assertEqual(ready.get(timeout=10), 'acquired')
                second = InstanceLock(path)
                self.assertFalse(second.acquire())

                release.set()
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
                self.assertTrue(second.acquire())
                second.release()
                self.assertTrue(path.exists())
            finally:
                release.set()
                if process.is_alive():
                    process.join(timeout=2)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)

    def test_lock_file_residue_does_not_prevent_reacquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'etlp.lock'
            first = InstanceLock(path)
            self.assertTrue(first.acquire())
            first.release()
            self.assertTrue(path.exists())

            second = InstanceLock(path)
            self.assertTrue(second.acquire())
            second.release()


if __name__ == '__main__':
    unittest.main()
