import os
import tempfile
import unittest
from argparse import Namespace
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from thsr_py.scheduler import (
    BookingScheduler,
    BookingStatus,
    BookingTask,
    _run_booking_flow_worker,
)


class ImmediateExecutor:
    def __init__(self):
        self.submissions = []

    def submit(self, function, args):
        self.submissions.append((function, args))
        future = Future()
        future.set_result(("", "", None))
        return future


class SchedulerExecutorLockTests(unittest.TestCase):
    def test_only_one_scheduler_can_hold_executor_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_path = str(Path(temp_dir) / "scheduler.json")
            first = BookingScheduler(storage_path=storage_path)
            second = BookingScheduler(storage_path=storage_path)

            try:
                self.assertTrue(first._acquire_executor_lock())
                self.assertFalse(second._acquire_executor_lock())

                first._release_executor_lock()
                self.assertTrue(second._acquire_executor_lock())
            finally:
                first._release_executor_lock()
                second._release_executor_lock()

    def test_due_tasks_are_dispatched_in_one_bounded_batch(self):
        scheduler = BookingScheduler(enable_persistence=False)
        executor = ImmediateExecutor()
        scheduler._booking_executor = executor
        scheduler.tasks = {
            task_id: BookingTask(
                id=task_id,
                from_station=1,
                to_station=2,
                date="2099/01/01",
                adult_cnt=1,
                time=1,
            )
            for task_id in ("task-one", "task-two")
        }

        with patch("thsr_py.scheduler.is_ticket_sales_open", return_value=True):
            scheduler._process_tasks()

        tasks = list(scheduler.tasks.values())
        self.assertEqual(2, len(executor.submissions))
        self.assertEqual([1, 1], [task.attempts for task in tasks])
        self.assertEqual(tasks[0].last_attempt, tasks[1].last_attempt)
        self.assertEqual(
            [BookingStatus.PENDING, BookingStatus.PENDING],
            [task.status for task in tasks],
        )

    def test_booking_worker_captures_output_and_restores_environment(self):
        def fake_booking_flow(_args):
            print("PNR Code: TEST1234")

        with patch.dict(os.environ, {}, clear=True):
            with patch("thsr_py.scheduler.run_booking_flow", fake_booking_flow):
                output, stderr_output, error = _run_booking_flow_worker(Namespace())

            self.assertNotIn("THSR_NON_INTERACTIVE", os.environ)

        self.assertIn("PNR Code: TEST1234", output)
        self.assertEqual("", stderr_output)
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
