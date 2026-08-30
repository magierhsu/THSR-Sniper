import unittest
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from pydantic import ValidationError

from thsr_py.api import BookingRequest, _booking_task_to_result
from thsr_py.flows import _confirm_train_flow
from thsr_py.scheduler import BookingStatus, BookingTask
from thsr_py.schema import find_earliest_train_within_range


class TrainSelectionTests(unittest.TestCase):
    def setUp(self):
        self.target_0800 = 7

    def test_selects_earliest_train_at_or_after_query_time(self):
        trains = [
            {"id": "late", "depart": "08:25"},
            {"id": "before", "depart": "07:55"},
            {"id": "early", "depart": "08:10"},
        ]

        selected = find_earliest_train_within_range(
            trains, self.target_0800, range_minutes=30
        )

        self.assertEqual("early", selected["id"])

    def test_never_selects_train_before_query_time(self):
        trains = [{"id": "before", "depart": "07:59"}]

        selected = find_earliest_train_within_range(
            trains, self.target_0800, range_minutes=30
        )

        self.assertIsNone(selected)

    def test_rejects_train_after_configured_range(self):
        trains = [{"id": "outside", "depart": "08:31"}]

        selected = find_earliest_train_within_range(
            trains, self.target_0800, range_minutes=30
        )

        self.assertIsNone(selected)

    def test_accepts_train_within_larger_configured_range(self):
        trains = [{"id": "inside", "depart": "08:45"}]

        selected = find_earliest_train_within_range(
            trains, self.target_0800, range_minutes=60
        )

        self.assertEqual("inside", selected["id"])

    def test_accepts_train_within_twelve_hour_range(self):
        trains = [{"id": "inside", "depart": "19:30"}]

        selected = find_earliest_train_within_range(
            trains, self.target_0800, range_minutes=12 * 60
        )

        self.assertEqual("inside", selected["id"])

    def test_api_accepts_twelve_hours_and_rejects_more(self):
        request_data = {
            "from_station": 1,
            "to_station": 2,
            "date": (datetime.now() + timedelta(days=1)).strftime("%Y/%m/%d"),
            "personal_id": "A123456789",
            "use_membership": False,
            "adult_cnt": 1,
            "time": self.target_0800,
        }

        request = BookingRequest(**request_data, time_range_minutes=12 * 60)
        self.assertEqual(12 * 60, request.time_range_minutes)

        with self.assertRaises(ValidationError):
            BookingRequest(**request_data, time_range_minutes=12 * 60 + 1)

    def test_result_serialization_includes_time_range(self):
        task = BookingTask(
            id="task-id",
            from_station=1,
            to_station=2,
            date="2026/09/01",
            status=BookingStatus.PENDING,
            time_range_minutes=420,
        )

        result = _booking_task_to_result(task)

        self.assertEqual(420, result["time_range_minutes"])

    def test_scheduler_namespace_preserves_all_ticket_counts(self):
        task = BookingTask(
            id="task-id",
            from_station=1,
            to_station=2,
            date="2026/09/01",
            adult_cnt=3,
            student_cnt=2,
            child_cnt=1,
            senior_cnt=4,
            disabled_cnt=5,
        )

        args = task.to_args_namespace()

        self.assertEqual(
            (3, 2, 1, 4, 5),
            (
                args.adult_cnt,
                args.student_cnt,
                args.child_cnt,
                args.senior_cnt,
                args.disabled_cnt,
            ),
        )

    def test_explicit_train_index_cannot_bypass_range(self):
        soup = BeautifulSoup(
            '<label class="result-item"><input querycode="123" '
            'querydeparture="08:45" queryarrival="09:30" '
            'queryestimatedtime="45" value="train-value"></label>',
            "html.parser",
        )

        selected = _confirm_train_flow(
            session=None,
            soup=soup,
            train_index=1,
            target_time_idx=self.target_0800,
            time_range_minutes=30,
        )

        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
