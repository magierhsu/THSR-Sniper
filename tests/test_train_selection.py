import unittest
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from pydantic import ValidationError

from thsr_py.api import BookingRequest, _booking_task_to_result
from thsr_py.flows import _confirm_train_flow
from thsr_py.scheduler import BookingStatus, BookingTask
from thsr_py.schema import (
    find_earliest_train_within_range,
    find_preferred_train_within_range,
    normalize_preferred_train_numbers,
)


class FakeSession:
    def __init__(self):
        self.payload = None

    def post(self, _url, headers, data, timeout):
        self.payload = data

        class Response:
            text = "<html></html>"

            @staticmethod
            def raise_for_status():
                return None

        return Response()


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

    def test_preferred_order_wins_over_departure_order(self):
        trains = [
            {"id": "0825", "depart": "08:10"},
            {"id": "838", "depart": "08:20"},
        ]

        selected = find_preferred_train_within_range(
            trains, self.target_0800, ["838", "825"], range_minutes=30
        )

        self.assertEqual("838", selected["id"])

    def test_preferred_train_matching_ignores_leading_zeroes(self):
        selected = find_preferred_train_within_range(
            [{"id": "0825", "depart": "08:10"}],
            self.target_0800,
            ["825"],
            range_minutes=30,
        )

        self.assertEqual("0825", selected["id"])

    def test_preferred_train_outside_range_is_ignored(self):
        selected = find_preferred_train_within_range(
            [{"id": "1320", "depart": "08:45"}],
            self.target_0800,
            ["1320"],
            range_minutes=30,
        )

        self.assertIsNone(selected)

    def test_preference_normalization_deduplicates_in_order(self):
        self.assertEqual(
            ["825", "838", "1320"],
            normalize_preferred_train_numbers(["0825", "825", 838, "1320"]),
        )

    def test_unmatched_preference_falls_back_to_earliest_train(self):
        soup = BeautifulSoup(
            '<label class="result-item"><input querycode="0838" '
            'querydeparture="08:20" queryarrival="09:00" '
            'queryestimatedtime="40" value="late"></label>'
            '<label class="result-item"><input querycode="0825" '
            'querydeparture="08:10" queryarrival="08:50" '
            'queryestimatedtime="40" value="early"></label>',
            "html.parser",
        )
        session = FakeSession()

        _confirm_train_flow(
            session=session,
            soup=soup,
            target_time_idx=self.target_0800,
            time_range_minutes=30,
            preferred_train_numbers=["999"],
        )

        self.assertEqual("early", session.payload["TrainQueryDataViewPanel:TrainGroup"])

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

    def test_api_normalizes_preferences_and_rejects_train_index_conflict(self):
        request_data = {
            "from_station": 1,
            "to_station": 2,
            "date": (datetime.now() + timedelta(days=1)).strftime("%Y/%m/%d"),
            "personal_id": "A123456789",
            "use_membership": False,
            "adult_cnt": 1,
            "time": self.target_0800,
        }
        request = BookingRequest(
            **request_data, preferred_train_numbers=["0825", "825", 838]
        )
        self.assertEqual(["825", "838"], request.preferred_train_numbers)

        with self.assertRaises(ValidationError):
            BookingRequest(
                **request_data,
                train_index=1,
                preferred_train_numbers=["825"],
            )

    def test_api_rejects_invalid_or_too_many_preferences(self):
        request_data = {
            "from_station": 1,
            "to_station": 2,
            "date": (datetime.now() + timedelta(days=1)).strftime("%Y/%m/%d"),
            "personal_id": "A123456789",
            "use_membership": False,
            "adult_cnt": 1,
            "time": self.target_0800,
        }
        with self.assertRaises(ValidationError):
            BookingRequest(**request_data, preferred_train_numbers=["82A"])
        with self.assertRaises(ValidationError):
            BookingRequest(
                **request_data,
                preferred_train_numbers=[str(value) for value in range(1, 22)],
            )

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

    def test_old_task_json_defaults_to_empty_preferences(self):
        task = BookingTask.from_dict(
            {
                "id": "old-task",
                "from_station": 1,
                "to_station": 2,
                "date": "2099/01/01",
            }
        )

        self.assertEqual([], task.preferred_train_numbers)
        self.assertEqual([], task.to_dict()["preferred_train_numbers"])

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
