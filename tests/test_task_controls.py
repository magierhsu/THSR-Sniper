import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from thsr_py.scheduler import (
    BookingScheduler,
    BookingStatus,
    BookingTask,
    TaskOwnershipError,
    TaskStateError,
)


def make_task(task_id="task", status=BookingStatus.PENDING, **overrides):
    values = {
        "id": task_id,
        "from_station": 1,
        "to_station": 2,
        "date": "2099/01/01",
        "user_id": "owner",
        "personal_id": "A123456789",
        "use_membership": False,
        "adult_cnt": 1,
        "time": 7,
        "status": status,
    }
    values.update(overrides)
    return BookingTask(**values)


class TaskControlTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = BookingScheduler(enable_persistence=False)

    def add(self, task):
        self.scheduler.add_task(task)
        return task

    def test_pending_and_waiting_tasks_pause_immediately(self):
        pending = self.add(make_task("pending"))
        waiting = self.add(make_task("waiting", BookingStatus.WAITING))

        self.scheduler.pause_task(pending.id, "owner")
        self.scheduler.pause_task(waiting.id, "owner")

        self.assertEqual(BookingStatus.PAUSED, pending.status)
        self.assertEqual(BookingStatus.PAUSED, waiting.status)

    def test_running_task_pauses_after_unsuccessful_worker_result(self):
        task = self.add(make_task(status=BookingStatus.RUNNING))

        self.scheduler.pause_task(task.id, "owner")
        self.assertEqual(BookingStatus.PAUSING, task.status)
        self.scheduler._apply_booking_result(task, "no booking", "", None)

        self.assertEqual(BookingStatus.PAUSED, task.status)

    def test_success_wins_while_task_is_pausing(self):
        task = self.add(make_task(status=BookingStatus.RUNNING))

        self.scheduler.pause_task(task.id, "owner")
        self.scheduler._apply_booking_result(task, "PNR Code: ABC12345", "", None)

        self.assertEqual(BookingStatus.SUCCESS, task.status)
        self.assertEqual("ABC12345", task.success_pnr)

    def test_resume_preserves_attempts_and_waits_when_sales_are_closed(self):
        last_attempt = datetime.now(timezone.utc)
        task = self.add(
            make_task(
                status=BookingStatus.PAUSED,
                attempts=4,
                last_attempt=last_attempt,
            )
        )

        self.scheduler.resume_task(task.id, "owner")

        self.assertEqual(BookingStatus.WAITING, task.status)
        self.assertEqual(4, task.attempts)
        self.assertIsNone(task.last_attempt)

    def test_resume_rejects_exhausted_task(self):
        task = self.add(
            make_task(status=BookingStatus.PAUSED, attempts=3, max_attempts=3)
        )

        with self.assertRaises(TaskStateError):
            self.scheduler.resume_task(task.id, "owner")

    def test_resume_is_immediately_pending_when_sales_are_open(self):
        task = self.add(make_task(status=BookingStatus.PAUSED, attempts=2))

        with patch("thsr_py.scheduler.is_ticket_sales_open", return_value=True):
            self.scheduler.resume_task(task.id, "owner")

        self.assertEqual(BookingStatus.PENDING, task.status)
        self.assertIsNone(task.last_attempt)

    def test_edit_requires_paused_and_resets_execution_state(self):
        created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        task = self.add(
            make_task(
                status=BookingStatus.PAUSED,
                attempts=8,
                last_attempt=datetime.now(timezone.utc),
                success_pnr="OLD",
                error_message="old error",
                created_at=created_at,
            )
        )
        replacement = make_task(
            "replacement",
            from_station=3,
            to_station=4,
            adult_cnt=0,
            child_cnt=2,
            time_range_minutes=360,
            preferred_train_numbers=["825", "838"],
            interval_minutes=3,
            max_attempts=20,
            use_membership=True,
        )

        updated = self.scheduler.update_task(task.id, replacement, "owner")

        self.assertEqual("task", updated.id)
        self.assertEqual("owner", updated.user_id)
        self.assertEqual(created_at, updated.created_at)
        self.assertEqual(BookingStatus.PAUSED, updated.status)
        self.assertEqual((3, 4, 0, 2), (
            updated.from_station,
            updated.to_station,
            updated.adult_cnt,
            updated.child_cnt,
        ))
        self.assertEqual(["825", "838"], updated.preferred_train_numbers)
        self.assertEqual(0, updated.attempts)
        self.assertIsNone(updated.last_attempt)
        self.assertIsNone(updated.success_pnr)
        self.assertIsNone(updated.error_message)

    def test_edit_rejects_non_paused_or_non_owner(self):
        pending = self.add(make_task())
        replacement = make_task("replacement")

        with self.assertRaises(TaskStateError):
            self.scheduler.update_task(pending.id, replacement, "owner")

        pending.status = BookingStatus.PAUSED
        with self.assertRaises(TaskOwnershipError):
            self.scheduler.update_task(pending.id, replacement, "other-user")

    def test_restart_recovers_running_and_pausing_tasks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_path = str(Path(temp_dir) / "scheduler.json")
            writer = BookingScheduler(storage_path=storage_path)
            writer.add_task(make_task("running", BookingStatus.RUNNING))
            writer.add_task(make_task("pausing", BookingStatus.PAUSING))

            recovered = BookingScheduler(storage_path=storage_path)

            self.assertEqual(
                BookingStatus.PENDING, recovered.get_task("running").status
            )
            self.assertEqual(
                BookingStatus.PAUSED, recovered.get_task("pausing").status
            )


if __name__ == "__main__":
    unittest.main()
