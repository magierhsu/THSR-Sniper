import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request


AUTH_SERVICE_DIR = Path(__file__).resolve().parent
if str(AUTH_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(AUTH_SERVICE_DIR))

import database
from booking_preferences import (
    BOOKING_PREFERENCES_KEY,
    BookingPreferences,
    load_preferences,
    merge_booking_preferences,
    read_booking_preferences,
)

with patch.object(database, "init_database"):
    import auth_api


def preference_payload(**overrides):
    payload = {
        "version": 1,
        "from_station": 2,
        "to_station": 7,
        "date": "2026-09-25",
        "adult_cnt": 1,
        "student_cnt": 0,
        "child_cnt": 2,
        "senior_cnt": 0,
        "disabled_cnt": 0,
        "time": 11,
        "time_range_minutes": 120,
        "preferred_train_numbers": ["0825", "838", "825"],
        "seat_prefer": 1,
        "class_type": 0,
        "no_ocr": False,
        "interval_minutes": 3,
        "max_attempts": 20,
    }
    payload.update(overrides)
    return payload


class FakeDatabase:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1


def request():
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/me/booking-preferences",
            "headers": [],
            "client": ("test-client", 1234),
        }
    )


class BookingPreferenceModelTests(unittest.TestCase):
    def test_opening_settings_roundtrip_and_validation(self):
        model = BookingPreferences(**preference_payload(opening_mode=True,
            sales_open_at='2030-09-02T00:00:00+08:00', burst_minutes=5, burst_retry_seconds=3))
        stored = read_booking_preferences(merge_booking_preferences('{"theme":"dark"}', model))
        self.assertEqual('2030-09-01T16:00:00+00:00', stored['sales_open_at'])
        self.assertEqual(5, stored['burst_minutes'])
        for values in ({'burst_minutes':6}, {'burst_retry_seconds':2}, {'sales_open_at':'broken'}):
            with self.assertRaises(ValidationError):
                BookingPreferences(**preference_payload(**values))

    def test_normalizes_preferred_train_numbers(self):
        preferences = BookingPreferences(**preference_payload())
        self.assertEqual(["825", "838"], preferences.preferred_train_numbers)

    def test_rejects_invalid_route_ticket_total_range_and_extra_fields(self):
        invalid_payloads = [
            preference_payload(to_station=2),
            preference_payload(adult_cnt=0, child_cnt=0),
            preference_payload(time_range_minutes=45),
            preference_payload(date="2026-02-31"),
            preference_payload(adult_cnt="1"),
            preference_payload(preferred_train_numbers=[825]),
            preference_payload(preferred_train_numbers=["825"] * 21),
            preference_payload(unexpected=True),
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                BookingPreferences(**payload)

    def test_missing_and_corrupt_json_return_no_preferences(self):
        self.assertIsNone(read_booking_preferences(None))
        self.assertIsNone(read_booking_preferences("not-json"))
        self.assertEqual(
            {"version": 1, "time_range_minutes": 45},
            read_booking_preferences(
                '{"booking_form":{"version":1,"time_range_minutes":45}}'
            ),
        )

    def test_non_object_booking_form_returns_no_preferences(self):
        self.assertIsNone(read_booking_preferences('{"booking_form":"broken"}'))

    def test_merge_preserves_other_preferences(self):
        preferences = BookingPreferences(**preference_payload())
        merged = merge_booking_preferences(
            '{"theme":"dark","notifications":{"enabled":true}}', preferences
        )
        stored = load_preferences(merged)

        self.assertEqual("dark", stored["theme"])
        self.assertTrue(stored["notifications"]["enabled"])
        self.assertEqual(
            ["825", "838"], stored[BOOKING_PREFERENCES_KEY]["preferred_train_numbers"]
        )

    def test_merge_recovers_from_corrupt_root_json(self):
        preferences = BookingPreferences(**preference_payload())
        stored = load_preferences(
            merge_booking_preferences("not-json", preferences)
        )
        self.assertEqual(1, stored[BOOKING_PREFERENCES_KEY]["version"])


class BookingPreferenceApiTests(unittest.TestCase):
    def test_endpoint_requires_authentication(self):
        route = next(
            route
            for route in auth_api.app.routes
            if getattr(route, "path", None) == "/me/booking-preferences"
            and "GET" in getattr(route, "methods", set())
        )
        self.assertIn(
            auth_api.get_current_user,
            [dependency.call for dependency in route.dependant.dependencies],
        )

        unauthenticated_request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/me/booking-preferences",
                "headers": [],
            }
        )
        with self.assertRaises(HTTPException) as context:
            asyncio.run(auth_api.security(unauthenticated_request))
        self.assertIn(context.exception.status_code, (401, 403))

    def test_empty_round_trip_and_user_isolation(self):
        first_user = SimpleNamespace(
            id=1, preferences=None, updated_at=None
        )
        second_user = SimpleNamespace(
            id=2, preferences=None, updated_at=None
        )
        db = FakeDatabase()

        empty = asyncio.run(
            auth_api.get_booking_preferences(current_user=first_user)
        )
        self.assertIsNone(empty.preferences)

        saved = asyncio.run(
            auth_api.update_booking_preferences(
                BookingPreferences(**preference_payload()),
                request(),
                current_user=first_user,
                db=db,
            )
        )
        loaded = asyncio.run(
            auth_api.get_booking_preferences(current_user=first_user)
        )
        isolated = asyncio.run(
            auth_api.get_booking_preferences(current_user=second_user)
        )

        self.assertEqual(saved.preferences, loaded.preferences)
        self.assertIsNone(isolated.preferences)
        self.assertGreaterEqual(db.commits, 2)


if __name__ == "__main__":
    unittest.main()
