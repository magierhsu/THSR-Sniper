import tempfile
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from thsr_py.booking_session import SessionHandshakeResult
from thsr_py.scheduler import (
    BookingScheduler,
    BookingStatus,
    BookingTask,
    _run_pre_entry_observation_worker,
)
from thsr_py.api import ScheduledBookingRequest
from thsr_py.worker_protocol import WorkerResult


OPENING = datetime(2030, 9, 2, tzinfo=timezone.utc)


class Clock(datetime):
    current = OPENING

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz else cls.current.replace(tzinfo=None)


class FakeSession:
    def __init__(self):
        self.cookies = FakeCookies()
        self.calls = 0
        self.closed = False

    def close(self):
        self.closed = True


class FakeCookies:
    def __init__(self):
        self.jar = [SimpleNamespace(name='JSESSIONID', value='same')]

    def get(self, name):
        return 'same' if name == 'JSESSIONID' else None

    def keys(self):
        return ['JSESSIONID']


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, args):
        future = Future()
        self.calls.append((fn, args, future))
        return future


class FakeControl:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class PreEntryObservationTests(unittest.TestCase):
    def test_api_accepts_only_explicit_pre_entry_options(self):
        base = dict(
            from_station=1, to_station=2, date='2030/09/20',
            personal_id='A123456789', use_membership=False,
            adult_cnt=1, time=1, opening_mode=True,
            sales_open_at='2030-09-02T00:00:00+08:00',
        )
        self.assertEqual(30, ScheduledBookingRequest(**base, pre_entry_seconds=30).pre_entry_seconds)
        for value in (1, 29, 45, 90):
            with self.assertRaises(ValueError):
                ScheduledBookingRequest(**base, pre_entry_seconds=value)
        with self.assertRaises(ValueError):
            disabled = {**base, 'opening_mode': False, 'sales_open_at': None}
            ScheduledBookingRequest(**disabled, pre_entry_seconds=30)

    def test_worker_uses_one_session_and_two_get_handshakes(self):
        session = FakeSession()
        handshakes = [
            SessionHandshakeResult(
                response=SimpleNamespace(status_code=200, cookies=session.cookies), jsession_id='same',
                classification='booking-page', title='entry', attempts=1,
                elapsed_seconds=0.01, browser_impersonate='firefox',
            ),
            SessionHandshakeResult(
                response=SimpleNamespace(status_code=200, cookies=session.cookies), jsession_id='same',
                classification='booking-page', title='entry', attempts=1,
                elapsed_seconds=0.01, browser_impersonate='firefox',
            ),
        ]
        args = SimpleNamespace(
            _task_id='observe', _run_id='run', _attempt=0,
            _dispatched_at=None, sales_open_at=OPENING,
            pre_entry_seconds=30,
        )
        with patch('thsr_py.scheduler.create_booking_session', return_value=session), \
             patch('thsr_py.scheduler._observation_handshake', side_effect=handshakes), \
             patch('thsr_py.scheduler._wait_until_epoch', return_value=False):
            result = _run_pre_entry_observation_worker(args)

        self.assertTrue(result.observation)
        self.assertEqual('session-reused', result.observation_result['outcome'])
        self.assertTrue(result.observation_result['session_reused'])
        self.assertIsNone(result.pnr)
        self.assertTrue(session.closed)

    def test_queue_token_takes_precedence_over_busy_page_classification(self):
        session = FakeSession()
        handshakes = [
            SessionHandshakeResult(
                response=SimpleNamespace(status_code=503, cookies=session.cookies, text='Waiting Room'),
                jsession_id=None, classification='maintenance-or-overloaded', title='busy',
                attempts=1, elapsed_seconds=0.01, browser_impersonate='firefox',
            ),
            SessionHandshakeResult(
                response=SimpleNamespace(status_code=503, cookies=session.cookies, text='Waiting Room'),
                jsession_id=None, classification='maintenance-or-overloaded', title='busy',
                attempts=1, elapsed_seconds=0.01, browser_impersonate='firefox',
            ),
        ]
        args = SimpleNamespace(
            _task_id='queue', _run_id='run', _attempt=0, _dispatched_at=None,
            sales_open_at=OPENING, pre_entry_seconds=30,
        )
        with patch('thsr_py.scheduler.create_booking_session', return_value=session), \
             patch('thsr_py.scheduler._observation_handshake', side_effect=handshakes), \
             patch('thsr_py.scheduler._wait_until_epoch', return_value=False), \
             patch('thsr_py.scheduler.has_queue_token', return_value=True):
            result = _run_pre_entry_observation_worker(args)
        self.assertEqual('queue-or-waiting', result.observation_result['outcome'])

    def test_scheduler_dispatches_observation_without_counting_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = BookingScheduler(storage_path=str(Path(temp) / 'tasks.json'))
            executor = FakeExecutor()
            scheduler._booking_executor = executor
            task = BookingTask(
                id='observe', from_station=1, to_station=2, date='2030/09/20',
                adult_cnt=1, time=1, opening_mode=True, sales_open_at=OPENING,
                pre_entry_seconds=30,
            )
            scheduler.add_task(task)
            Clock.current = OPENING - timedelta(seconds=20)
            with patch('thsr_py.scheduler.datetime', Clock):
                scheduler._process_tasks()

            self.assertEqual(1, len(executor.calls))
            self.assertIs(executor.calls[0][0], _run_pre_entry_observation_worker)
            self.assertEqual(BookingStatus.OBSERVING, task.status)
            self.assertEqual(0, task.attempts)

            executor.calls[0][2].set_result(WorkerResult(
                observation=True,
                observation_result={'outcome': 'session-reused', 'session_reused': True},
            ))
            scheduler._collect_workers()
            self.assertEqual(BookingStatus.OBSERVED, task.status)
            self.assertEqual(0, task.attempts)
            self.assertIsNotNone(task.last_finished)

            restored = BookingTask.from_dict(task.to_dict())
            self.assertEqual(30, restored.pre_entry_seconds)
            self.assertEqual('session-reused', task.observation_result['outcome'])

    def test_worker_error_is_not_reported_as_observed(self):
        scheduler = BookingScheduler(enable_persistence=False)
        executor = FakeExecutor()
        scheduler._booking_executor = executor
        task = BookingTask(
            id='observe-error', from_station=1, to_station=2, date='2030/09/20',
            adult_cnt=1, time=1, opening_mode=True, sales_open_at=OPENING,
            pre_entry_seconds=30,
        )
        scheduler.add_task(task)
        Clock.current = OPENING - timedelta(seconds=20)
        with patch('thsr_py.scheduler.datetime', Clock):
            scheduler._process_tasks()
        executor.calls[0][2].set_result(WorkerResult(
            observation=True,
            error='Session 觀察失敗：TimeoutError',
            observation_result={'outcome': 'worker-error', 'error_type': 'TimeoutError'},
        ))
        scheduler._collect_workers()
        self.assertEqual(BookingStatus.FAILED, task.status)
        self.assertNotEqual(BookingStatus.OBSERVED, task.status)

    def test_missing_observation_payload_is_treated_as_worker_error(self):
        scheduler = BookingScheduler(enable_persistence=False)
        executor = FakeExecutor()
        scheduler._booking_executor = executor
        task = BookingTask(
            id='observe-invalid', from_station=1, to_station=2, date='2030/09/20',
            adult_cnt=1, time=1, opening_mode=True, sales_open_at=OPENING,
            pre_entry_seconds=30,
        )
        scheduler.add_task(task)
        Clock.current = OPENING - timedelta(seconds=20)
        with patch('thsr_py.scheduler.datetime', Clock):
            scheduler._process_tasks()
        executor.calls[0][2].set_result(WorkerResult(observation=True))
        scheduler._collect_workers()
        self.assertEqual(BookingStatus.FAILED, task.status)
        self.assertEqual('worker-error', task.observation_result['outcome'])

    def test_removing_observing_task_interrupts_worker(self):
        scheduler = BookingScheduler(enable_persistence=False)
        task = BookingTask(
            id='observe-delete', from_station=1, to_station=2, date='2030/09/20',
            adult_cnt=1, time=1, opening_mode=True, sales_open_at=OPENING,
            pre_entry_seconds=60, status=BookingStatus.OBSERVING,
        )
        scheduler.add_task(task)
        control = FakeControl()
        scheduler._controls[task.id] = (control, object())

        self.assertTrue(scheduler.remove_task(task.id))
        self.assertEqual(BookingStatus.DELETED, task.status)
        self.assertEqual(['shutdown'], control.messages)

    def test_invalid_observation_result_is_ignored_when_loading(self):
        task = BookingTask.from_dict({
            'id': 'legacy', 'from_station': 1, 'to_station': 2,
            'date': '2030/09/20', 'opening_mode': True,
            'pre_entry_seconds': 30, 'observation_result': ['not', 'a', 'mapping'],
        })
        self.assertIsNone(task.observation_result)

    def test_pausing_observer_becomes_paused_after_worker_finishes(self):
        scheduler = BookingScheduler(enable_persistence=False)
        executor = FakeExecutor()
        scheduler._booking_executor = executor
        task = BookingTask(
            id='observe', from_station=1, to_station=2, date='2030/09/20',
            adult_cnt=1, time=1, opening_mode=True, sales_open_at=OPENING,
            pre_entry_seconds=30,
        )
        scheduler.add_task(task)
        Clock.current = OPENING - timedelta(seconds=20)
        with patch('thsr_py.scheduler.datetime', Clock):
            scheduler._process_tasks()
        scheduler.pause_task(task.id)
        self.assertEqual(BookingStatus.PAUSING, task.status)
        executor.calls[0][2].set_result(WorkerResult(
            observation=True, observation_result={'outcome': 'opening-busy'}
        ))
        scheduler._collect_workers()
        self.assertEqual(BookingStatus.PAUSED, task.status)

    def test_pausing_observing_task_interrupts_worker(self):
        scheduler = BookingScheduler(enable_persistence=False)
        task = BookingTask(
            id='observe-pause', from_station=1, to_station=2, date='2030/09/20',
            adult_cnt=1, time=1, opening_mode=True, sales_open_at=OPENING,
            pre_entry_seconds=60, status=BookingStatus.OBSERVING,
        )
        scheduler.add_task(task)
        control = FakeControl()
        scheduler._controls[task.id] = (control, object())

        scheduler.pause_task(task.id)
        self.assertEqual(BookingStatus.PAUSING, task.status)
        self.assertEqual(['shutdown'], control.messages)

    def test_interrupted_pausing_observer_preserves_pause_intent(self):
        scheduler = BookingScheduler(enable_persistence=False)
        executor = FakeExecutor()
        scheduler._booking_executor = executor
        task = BookingTask(
            id='observe-interrupted', from_station=1, to_station=2, date='2030/09/20',
            adult_cnt=1, time=1, opening_mode=True, sales_open_at=OPENING,
            pre_entry_seconds=30,
        )
        scheduler.add_task(task)
        Clock.current = OPENING - timedelta(seconds=20)
        with patch('thsr_py.scheduler.datetime', Clock):
            scheduler._process_tasks()
        scheduler.pause_task(task.id)
        executor.calls[0][2].set_result(WorkerResult(
            observation=True,
            error='Session 觀察已中斷',
            observation_result={'outcome': 'interrupted'},
        ))
        scheduler._collect_workers()
        self.assertEqual(BookingStatus.PAUSED, task.status)

    def test_restart_recovers_observing_task_to_waiting(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'tasks.json')
            writer = BookingScheduler(storage_path=path)
            writer.add_task(BookingTask(
                id='observe', from_station=1, to_station=2, date='2030/09/20',
                adult_cnt=1, time=1, opening_mode=True, sales_open_at=OPENING,
                pre_entry_seconds=60, status=BookingStatus.OBSERVING,
            ))
            recovered = BookingScheduler(storage_path=path)
            self.assertEqual(BookingStatus.WAITING, recovered.tasks['observe'].status)
            self.assertEqual('interrupted', recovered.tasks['observe'].observation_result['outcome'])

    def test_observation_workers_are_bounded_and_release_slots(self):
        scheduler = BookingScheduler(enable_persistence=False)
        executor = FakeExecutor()
        scheduler._booking_executor = executor
        for index in range(3):
            scheduler.add_task(BookingTask(
                id=f'observe-{index}', from_station=1, to_station=2,
                date='2030/09/20', adult_cnt=1, time=1,
                opening_mode=True, sales_open_at=OPENING,
                pre_entry_seconds=30,
            ))
        Clock.current = OPENING - timedelta(seconds=20)
        with patch('thsr_py.scheduler.datetime', Clock):
            scheduler._process_tasks()
        self.assertEqual(2, len(scheduler._inflight))
        self.assertEqual(0, sum(task.attempts for task in scheduler.tasks.values()))
        first_id = next(iter(scheduler._inflight))
        scheduler._inflight[first_id].set_result(WorkerResult(
            observation=True, observation_result={'outcome': 'session-reused'}
        ))
        with patch('thsr_py.scheduler.datetime', Clock):
            scheduler._process_tasks()
        self.assertEqual(2, len(scheduler._inflight))
        self.assertEqual(BookingStatus.OBSERVED, scheduler.tasks[first_id].status)
        self.assertEqual(2, sum(task.status == BookingStatus.OBSERVING for task in scheduler.tasks.values()))

    def test_queued_observation_is_marked_missed_after_opening(self):
        scheduler = BookingScheduler(enable_persistence=False)
        executor = FakeExecutor()
        scheduler._booking_executor = executor
        for index in range(3):
            scheduler.add_task(BookingTask(
                id=f'observe-{index}', from_station=1, to_station=2,
                date='2030/09/20', adult_cnt=1, time=1,
                opening_mode=True, sales_open_at=OPENING,
                pre_entry_seconds=30,
            ))
        Clock.current = OPENING - timedelta(seconds=20)
        with patch('thsr_py.scheduler.datetime', Clock):
            scheduler._process_tasks()
        self.assertEqual(2, len(scheduler._inflight))
        queued = scheduler.tasks['observe-2']
        self.assertEqual(BookingStatus.PENDING, queued.status)

        Clock.current = OPENING
        with patch('thsr_py.scheduler.datetime', Clock):
            scheduler._process_tasks()
        self.assertEqual(BookingStatus.OBSERVED, queued.status)
        self.assertEqual('window-missed', queued.observation_result['outcome'])
        self.assertEqual(2, len(scheduler._inflight))


if __name__ == '__main__':
    unittest.main()
