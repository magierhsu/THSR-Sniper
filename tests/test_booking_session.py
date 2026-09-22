import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from curl_cffi import requests

from thsr_py.booking_session import (
    DEFAULT_BROWSER_IMPERSONATE,
    classify_session_response,
    create_booking_session,
    establish_booking_session,
    resolve_browser_impersonate,
    retry_after_seconds,
)


BOOKING_HTML = (
    "<html><head><title>THSR Booking</title></head><body>"
    '<img id="BookingS1Form_homeCaptcha_passCode" src="/captcha">'
    "</body></html>"
)


class FakeCookies:
    def __init__(self, values=None):
        self._values = values or {}
        self.jar = [
            SimpleNamespace(name=name, value=value)
            for name, value in self._values.items()
        ]

    def get(self, name):
        return self._values.get(name)

    def keys(self):
        return self._values.keys()


class FakeResponse:
    def __init__(self, status_code=200, text=BOOKING_HTML, headers=None, cookies=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.cookies = FakeCookies(cookies)
        self.ok = 200 <= status_code < 400


class FakeSession:
    def __init__(self, outcomes, cookies=None, browser="firefox"):
        self.outcomes = list(outcomes)
        self.cookies = FakeCookies(cookies)
        self._thsr_browser_impersonate = browser
        self.calls = 0

    def get(self, _url, timeout, allow_redirects):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BookingSessionTests(unittest.TestCase):
    def test_browser_setting_defaults_to_firefox_and_accepts_chrome(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(DEFAULT_BROWSER_IMPERSONATE, resolve_browser_impersonate())
        self.assertEqual("firefox", resolve_browser_impersonate("FIREFOX"))
        self.assertEqual("chrome", resolve_browser_impersonate(" chrome "))
        self.assertEqual(DEFAULT_BROWSER_IMPERSONATE, resolve_browser_impersonate("safari"))

    @patch("thsr_py.booking_session.BookingSession")
    def test_create_session_passes_impersonation_to_curl_cffi(self, session_factory):
        session = MagicMock()
        session.headers = {}
        session_factory.return_value = session

        created = create_booking_session("chrome", logger=lambda _message: None)

        self.assertIs(session, created)
        session_factory.assert_called_once_with(impersonate="chrome")
        self.assertEqual(
            "zh-TW,zh;q=0.8,en-US;q=0.5,en;q=0.3",
            session.headers["Accept-Language"],
        )

    def test_response_classification(self):
        cases = (
            (FakeResponse(), "booking-page"),
            (FakeResponse(status_code=429), "rate-limited"),
            (FakeResponse(status_code=503), "maintenance-or-overloaded"),
            (FakeResponse(text="<title>Waiting Room</title>queue-it"), "queue"),
            (FakeResponse(text="<title>Maintenance</title>"), "maintenance-or-overloaded"),
            (FakeResponse(text="<title>Unknown</title>"), "unexpected-page"),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                classification, _title = classify_session_response(response)
                self.assertEqual(expected, classification)

    def test_endpoint_response_classification(self):
        cases = (
            ("captcha", FakeResponse(text="binary image"), "captcha-image"),
            ("query", FakeResponse(text="<form name='BookingS2Form'></form>"), "query-page"),
            ("train_selection", FakeResponse(text="<form name='BookingS3Form'></form>"), "train-selection-page"),
            ("confirmation", FakeResponse(text="<p class='pnr-code'><span>hidden</span></p>"), "confirmation-page"),
            ("confirmation", FakeResponse(text="<span class='feedbackPanelERROR'>error</span>"), "confirmation-error"),
        )
        for endpoint, response, expected in cases:
            with self.subTest(endpoint=endpoint):
                classification, _title = classify_session_response(response, endpoint)
                self.assertEqual(expected, classification)

    def test_retry_after_supports_seconds_and_http_dates(self):
        seconds_response = FakeResponse(headers={"Retry-After": "45"})
        self.assertEqual(45.0, retry_after_seconds(seconds_response))

        now_epoch = 1_800_000_000.0
        retry_at = datetime.fromtimestamp(now_epoch + 12, tz=timezone.utc)
        date_response = FakeResponse(
            headers={"Retry-After": format_datetime(retry_at, usegmt=True)}
        )
        self.assertEqual(
            12.0,
            retry_after_seconds(date_response, now_epoch=now_epoch),
        )

    def test_handshake_retries_and_uses_retry_after_without_double_waiting(self):
        session = FakeSession(
            [
                FakeResponse(status_code=429, headers={"Retry-After": "7"}),
                FakeResponse(cookies={"JSESSIONID": "secret-value"}),
            ],
            browser="chrome",
        )
        sleeps = []
        logs = []

        result = establish_booking_session(
            session,
            max_attempts=2,
            retry_delays=(0, 2),
            sleep=sleeps.append,
            monotonic=lambda: 0.0,
            wall_time=lambda: 0.0,
            logger=logs.append,
        )

        self.assertTrue(result.ok)
        self.assertEqual(2, result.attempts)
        self.assertEqual("chrome", result.browser_impersonate)
        self.assertEqual([7.0], sleeps)
        self.assertIn("JSESSIONID", " ".join(logs))
        self.assertNotIn("secret-value", " ".join(logs))

    def test_booking_page_without_jsessionid_is_rejected(self):
        result = establish_booking_session(
            FakeSession([FakeResponse()]),
            max_attempts=1,
            retry_delays=(0,),
            monotonic=lambda: 0.0,
            logger=lambda _message: None,
        )

        self.assertFalse(result.ok)
        self.assertEqual("missing-jsessionid", result.classification)

    def test_connection_failures_stop_at_max_attempts(self):
        session = FakeSession(
            [
                requests.exceptions.Timeout("first timeout"),
                requests.exceptions.Timeout("second timeout"),
            ]
        )
        sleeps = []

        result = establish_booking_session(
            session,
            max_attempts=2,
            retry_delays=(0, 2),
            sleep=sleeps.append,
            monotonic=lambda: 0.0,
            logger=lambda _message: None,
        )

        self.assertFalse(result.ok)
        self.assertEqual("connection-error", result.classification)
        self.assertEqual(2, result.attempts)
        self.assertEqual(2, session.calls)
        self.assertEqual([2.0], sleeps)


if __name__ == "__main__":
    unittest.main()
