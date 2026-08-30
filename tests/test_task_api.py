import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException

from thsr_py.api import TaskUpdateRequest, pause_task, resume_task, update_task
from thsr_py.scheduler import BookingScheduler, BookingStatus, BookingTask


def request_payload():
    return {
        "from_station": 2,
        "to_station": 3,
        "date": (datetime.now() + timedelta(days=10)).strftime("%Y/%m/%d"),
        "personal_id": "A123456789",
        "use_membership": True,
        "adult_cnt": 0,
        "child_cnt": 2,
        "time": 8,
        "time_range_minutes": 120,
        "preferred_train_numbers": ["0825", "838"],
        "seat_prefer": 1,
        "class_type": 0,
        "no_ocr": False,
        "interval_minutes": 3,
        "max_attempts": 10,
    }


def make_task(status=BookingStatus.PAUSED, user_id="owner"):
    return BookingTask(
        id="api-task",
        from_station=1,
        to_station=2,
        date="2099/01/01",
        user_id=user_id,
        personal_id="A123456789",
        use_membership=False,
        adult_cnt=1,
        time=7,
        status=status,
    )


class TaskApiTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = BookingScheduler(enable_persistence=False)
        self.scheduler_patch = patch(
            "thsr_py.api.get_scheduler", return_value=self.scheduler
        )
        self.scheduler_patch.start()

    def tearDown(self):
        self.scheduler_patch.stop()

    def test_pause_and_resume_endpoints(self):
        self.scheduler.add_task(make_task(BookingStatus.PENDING))

        paused = asyncio.run(pause_task("api-task", current_user_id="owner"))
        resumed = asyncio.run(resume_task("api-task", current_user_id="owner"))

        self.assertEqual("paused", paused.status)
        self.assertEqual("waiting", resumed.status)

    def test_update_endpoint_replaces_full_content_and_resets_attempts(self):
        task = make_task()
        task.attempts = 12
        task.error_message = "old error"
        self.scheduler.add_task(task)

        response = asyncio.run(
            update_task(
                "api-task",
                TaskUpdateRequest(**request_payload()),
                current_user_id="owner",
            )
        )

        self.assertEqual("paused", response.status)
        self.assertEqual(["825", "838"], response.preferred_train_numbers)
        self.assertEqual(0, response.attempts)
        self.assertEqual(2, response.child_cnt)

    def test_update_rejects_non_paused_task(self):
        self.scheduler.add_task(make_task(BookingStatus.PENDING))

        with self.assertRaises(HTTPException) as context:
            asyncio.run(
                update_task(
                    "api-task",
                    TaskUpdateRequest(**request_payload()),
                    current_user_id="owner",
                )
            )

        self.assertEqual(409, context.exception.status_code)

    def test_task_mutation_rejects_non_owner(self):
        self.scheduler.add_task(make_task(user_id="other"))

        with self.assertRaises(HTTPException) as pause_context:
            asyncio.run(pause_task("api-task", current_user_id="owner"))
        with self.assertRaises(HTTPException) as update_context:
            asyncio.run(
                update_task(
                    "api-task",
                    TaskUpdateRequest(**request_payload()),
                    current_user_id="owner",
                )
            )

        self.assertEqual(403, pause_context.exception.status_code)
        self.assertEqual(403, update_context.exception.status_code)


if __name__ == "__main__":
    unittest.main()
