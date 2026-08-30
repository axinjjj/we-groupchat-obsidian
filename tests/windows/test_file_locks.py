from __future__ import annotations

import multiprocessing
from pathlib import Path
import queue
import tempfile
import unittest

from core.platform import LockBusy, LockMode, create_file_lock, detect_platform
from core.platform.contracts import PlatformName


def _acquire_worker(path, mode_value, blocking, started, result, release):
    service = create_file_lock()
    started.set()
    try:
        handle = service.acquire(
            path,
            mode=LockMode(mode_value),
            blocking=blocking,
        )
    except LockBusy as exc:
        result.put(("busy", exc.code))
        return
    result.put(("acquired", handle.fileno()))
    if release is not None:
        release.wait(10)
    handle.close()


@unittest.skipUnless(
    detect_platform() in {PlatformName.MACOS, PlatformName.WINDOWS},
    "W0.2A lock backends target macOS and Windows",
)
class FileLockContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "contract.lock")
        self.context = multiprocessing.get_context("spawn")
        self.service = create_file_lock()

    def _run_nonblocking_child(self, parent_mode, child_mode):
        parent = self.service.acquire(
            self.path,
            mode=parent_mode,
            blocking=True,
        )
        started = self.context.Event()
        result = self.context.Queue()
        process = self.context.Process(
            target=_acquire_worker,
            args=(
                self.path,
                child_mode.value,
                False,
                started,
                result,
                None,
            ),
        )
        try:
            process.start()
            self.assertTrue(started.wait(10))
            outcome = result.get(timeout=10)
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            return outcome
        finally:
            parent.close()
            if process.is_alive():
                process.terminate()
                process.join(10)

    def test_nonblocking_conflicts_use_stable_worker_busy_code(self):
        outcome = self._run_nonblocking_child(
            LockMode.EXCLUSIVE,
            LockMode.EXCLUSIVE,
        )
        self.assertEqual(outcome, ("busy", "worker_busy"))

    def test_shared_owners_are_compatible(self):
        outcome = self._run_nonblocking_child(LockMode.SHARED, LockMode.SHARED)
        self.assertEqual(outcome[0], "acquired")

    def test_shared_owner_blocks_exclusive_owner(self):
        outcome = self._run_nonblocking_child(
            LockMode.SHARED,
            LockMode.EXCLUSIVE,
        )
        self.assertEqual(outcome, ("busy", "worker_busy"))

    def test_blocking_owner_waits_until_release(self):
        parent = self.service.acquire(
            self.path,
            mode=LockMode.EXCLUSIVE,
            blocking=True,
        )
        started = self.context.Event()
        result = self.context.Queue()
        process = self.context.Process(
            target=_acquire_worker,
            args=(
                self.path,
                LockMode.EXCLUSIVE.value,
                True,
                started,
                result,
                None,
            ),
        )
        try:
            process.start()
            self.assertTrue(started.wait(10))
            with self.assertRaises(queue.Empty):
                result.get(timeout=0.5)
            parent.close()
            parent = None
            self.assertEqual(result.get(timeout=10)[0], "acquired")
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        finally:
            if parent is not None:
                parent.close()
            if process.is_alive():
                process.terminate()
                process.join(10)

    def test_process_death_releases_operating_system_lock(self):
        started = self.context.Event()
        result = self.context.Queue()
        release = self.context.Event()
        process = self.context.Process(
            target=_acquire_worker,
            args=(
                self.path,
                LockMode.EXCLUSIVE.value,
                True,
                started,
                result,
                release,
            ),
        )
        process.start()
        self.assertTrue(started.wait(10))
        self.assertEqual(result.get(timeout=10)[0], "acquired")
        process.terminate()
        process.join(10)
        self.assertFalse(process.is_alive())

        recovered = self.service.acquire(
            self.path,
            mode=LockMode.EXCLUSIVE,
            blocking=False,
        )
        recovered.close()

    def test_handle_retains_descriptor_and_close_is_idempotent(self):
        handle = self.service.acquire(
            self.path,
            mode=LockMode.EXCLUSIVE,
            blocking=True,
        )
        self.assertGreaterEqual(handle.fileno(), 0)
        handle.close()
        handle.close()
        with self.assertRaises(ValueError):
            handle.fileno()


if __name__ == "__main__":
    unittest.main()
