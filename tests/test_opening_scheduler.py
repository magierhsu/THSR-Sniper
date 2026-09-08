import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import Future
from unittest.mock import patch

from thsr_py.scheduler import BookingScheduler, BookingTask, BookingStatus, TaskStateError, TaskOwnershipError
from thsr_py.worker_protocol import WorkerResult, configure, defer_until, check_cooldown, DeferredRequest
from thsr_py.opening import utc, validate_opening
from thsr_py.api import ScheduledBookingRequest, ResolveBookingRequest

NOW = datetime(2030, 9, 1, 16, tzinfo=timezone.utc)

class Clock(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz else cls.current.replace(tzinfo=None)

class Executor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, args):
        future = Future()
        self.calls.append((future, args))
        return future

class OpeningSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.scheduler = BookingScheduler(storage_path=str(Path(self.temp.name) / 'tasks.json'))
        self.executor = Executor()
        self.scheduler._booking_executor = self.executor
        Clock.current = NOW
        self.clock_patch = patch('thsr_py.scheduler.datetime', Clock)
        self.clock_patch.start()

    def tearDown(self):
        for parent, child in self.scheduler._controls.values():
            parent.close()
            child.close()
        self.clock_patch.stop()
        configure()
        self.temp.cleanup()

    def add(self, count, **kwargs):
        for i in range(count):
            task = BookingTask(id=str(i), from_station=1, to_station=2, date='2030/09/20',
                user_id=str(i % 2), time=1, adult_cnt=1, opening_mode=True,
                sales_open_at=NOW, created_at=NOW - timedelta(days=1), **kwargs)
            self.scheduler.add_task(task)

    def test_two_five_ten_tasks_are_fair_and_only_dispatch_counts(self):
        for count in (2, 5, 10):
            with self.subTest(count=count):
                self.scheduler.tasks.clear()
                self.add(count)
                self.scheduler._process_tasks()
                seen = set()
                while len(seen) < count:
                    active = list(self.scheduler._inflight)
                    self.assertLessEqual(len(active), 2)
                    for task_id in active:
                        self.assertNotIn(task_id, seen)
                        seen.add(task_id)
                        self.scheduler._inflight[task_id].set_result(WorkerResult(error='no seats'))
                    self.scheduler._process_tasks()
                self.assertEqual(count, sum(t.attempts for t in self.scheduler.tasks.values()))
                self.assertFalse(self.scheduler._inflight)

    def test_slow_task_does_not_block_other_tasks_or_their_next_attempt(self):
        self.add(3)
        self.scheduler._process_tasks()
        self.scheduler._inflight['1'].set_result(WorkerResult(error='no seats'))
        self.scheduler._process_tasks()
        self.assertEqual({'0', '2'}, set(self.scheduler._inflight))
        self.scheduler._inflight['2'].set_result(WorkerResult(error='no seats'))
        self.scheduler._process_tasks()
        Clock.current += timedelta(seconds=5)
        self.scheduler._process_tasks()
        self.assertEqual({'0', '1'}, set(self.scheduler._inflight))
        self.assertEqual(1, self.scheduler.tasks['0'].attempts)

    def test_no_dispatch_before_opening_and_normal_tasks_wait_for_reserved_slot(self):
        self.add(2)
        Clock.current = NOW - timedelta(seconds=20)
        self.scheduler.add_task(BookingTask(id='normal', from_station=1, to_station=2,
            date='2030/09/20', created_at=NOW-timedelta(days=1)))
        with patch('thsr_py.scheduler.is_ticket_sales_open', return_value=True):
            self.scheduler._process_tasks()
        self.assertEqual([], self.executor.calls)
        Clock.current = NOW
        self.scheduler._process_tasks()
        self.assertEqual({'0', '1'}, set(self.scheduler._inflight))

    def test_burst_end_uses_original_interval_and_not_completion_time(self):
        self.add(1, interval_minutes=5)
        task = self.scheduler.tasks['0']
        task.last_attempt = NOW
        task.last_finished = NOW + timedelta(seconds=10)
        self.assertEqual(NOW + timedelta(seconds=15), task.due_at(NOW + timedelta(seconds=11)))
        self.assertEqual(NOW + timedelta(minutes=5), task.due_at(NOW + timedelta(minutes=2)))

    def test_confirmation_is_persisted_before_ack_and_survives_restart(self):
        self.add(1)
        self.scheduler._process_tasks()
        child = self.executor.calls[0][1]._control
        child.send('confirm')
        self.scheduler._collect_workers()
        self.assertEqual('allowed', child.recv())
        disk = json.loads(self.scheduler.storage_path.read_text())
        self.assertTrue(disk['tasks'][0]['confirmation_pending'])
        restarted = BookingScheduler(storage_path=str(self.scheduler.storage_path))
        self.assertTrue(restarted.tasks['0'].needs_confirmation)
        self.assertEqual(BookingStatus.PAUSED, restarted.tasks['0'].status)

    def test_persistence_failure_denies_post(self):
        self.add(1)
        self.scheduler._process_tasks()
        child = self.executor.calls[0][1]._control
        child.send('confirm')
        with patch.object(self.scheduler, '_save_tasks_locked', return_value=False):
            self.scheduler._collect_workers()
        self.assertEqual('denied', child.recv())

    def test_uncertain_result_requires_resolution_and_ownership(self):
        self.add(1)
        self.scheduler._process_tasks()
        self.scheduler._inflight['0'].set_result(WorkerResult(uncertain=True))
        self.scheduler._collect_workers()
        task = self.scheduler.tasks['0']
        with self.assertRaises(TaskStateError):
            self.scheduler.resume_task('0', '0')
        with self.assertRaises(TaskStateError):
            self.scheduler.update_task('0', task, '0')
        with self.assertRaises(TaskOwnershipError):
            self.scheduler.resolve_confirmation('0', '1', '12345678')
        self.scheduler.resolve_confirmation('0', '0', '12345678')
        self.assertEqual(BookingStatus.SUCCESS, task.status)
        self.assertFalse(task.needs_confirmation)

    def test_success_wins_over_pause(self):
        self.add(1)
        self.scheduler._process_tasks()
        self.scheduler.pause_task('0')
        self.scheduler._inflight['0'].set_result(WorkerResult(pnr='12345678'))
        self.scheduler._collect_workers()
        self.assertEqual(BookingStatus.SUCCESS, self.scheduler.tasks['0'].status)

    def test_worker_crash_after_confirmation_is_preserved_for_review(self):
        from concurrent.futures.process import BrokenProcessPool
        self.add(1)
        self.scheduler._process_tasks()
        self.scheduler.tasks['0'].confirmation_pending = True
        self.scheduler._inflight['0'].set_exception(BrokenProcessPool('worker died'))
        self.scheduler._collect_workers()
        self.assertTrue(self.scheduler._pool_broken)
        self.assertTrue(self.scheduler.tasks['0'].needs_confirmation)
        self.assertEqual(BookingStatus.PAUSED, self.scheduler.tasks['0'].status)

    def test_cooldown_stops_all_accounts_and_is_persisted(self):
        self.add(2)
        self.scheduler._cooldown.value = NOW.timestamp() + 120
        self.scheduler._save_tasks_locked()
        self.scheduler._process_tasks()
        self.assertFalse(self.executor.calls)
        configure(self.scheduler._cooldown)
        with patch('thsr_py.worker_protocol.time.time', return_value=NOW.timestamp()):
            with self.assertRaises(DeferredRequest):
                check_cooldown()
        restarted = BookingScheduler(storage_path=str(self.scheduler.storage_path))
        self.assertEqual(NOW.timestamp() + 120, restarted._cooldown.value)

    def test_legacy_json_defaults_and_new_json_roundtrip(self):
        task = BookingTask.from_dict({'id':'old','from_station':1,'to_station':2,'date':'2030/09/20'})
        self.assertFalse(task.opening_mode)
        self.add(1)
        restored = BookingTask.from_dict(self.scheduler.tasks['0'].to_dict())
        self.assertEqual(NOW, restored.sales_open_at)
        self.assertTrue(restored.opening_mode)

    def test_opening_validation_and_taiwan_timezone(self):
        self.assertEqual(NOW, utc('2030-09-02T00:00:00+08:00'))
        with self.assertRaises(ValueError):
            validate_opening(True, None, 2, 5, '2030/09/20')
        with self.assertRaises(ValueError):
            validate_opening(True, '2030-10-01T00:00:00+08:00', 2, 5, '2030/09/20')
        with self.assertRaises(ValueError):
            ResolveBookingRequest(booked=True)

    def test_api_request_validates_complete_opening_fields(self):
        payload = dict(from_station=1, to_station=2, date='2030/09/20',
            personal_id='A123456789', use_membership=False, time=1, adult_cnt=1,
            opening_mode=True, sales_open_at='2030-09-02T00:00:00+08:00')
        request = ScheduledBookingRequest(**payload)
        self.assertEqual(NOW, request.sales_open_at)
        for extra in ({'sales_open_at':None}, {'burst_minutes':0}, {'burst_retry_seconds':11}):
            with self.assertRaises(ValueError):
                ScheduledBookingRequest(**{**payload, **extra})

    def test_max_attempts_applies_during_burst(self):
        self.add(1, max_attempts=1)
        self.scheduler._process_tasks()
        self.scheduler._inflight['0'].set_result(WorkerResult(error='no seats'))
        self.scheduler._process_tasks()
        Clock.current += timedelta(seconds=5)
        self.scheduler._process_tasks()
        self.assertEqual(BookingStatus.FAILED, self.scheduler.tasks['0'].status)
        self.assertEqual(1, len(self.executor.calls))

    def test_resolve_endpoint_rejects_anonymous_and_other_owner(self):
        import asyncio
        from fastapi import HTTPException
        from thsr_py.api import resolve_booking
        self.add(1)
        self.scheduler.tasks['0'].needs_confirmation = True
        self.scheduler.tasks['0'].status = BookingStatus.PAUSED
        with patch('thsr_py.api.get_scheduler', return_value=self.scheduler):
            for user, code in ((None, 401), ('other', 403)):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(resolve_booking('0', ResolveBookingRequest(booked=False), user))
                self.assertEqual(code, ctx.exception.status_code)
            result = asyncio.run(resolve_booking('0', ResolveBookingRequest(booked=False), '0'))
            self.assertFalse(result.needs_confirmation)
            self.assertEqual('pending', result.status)

    def test_account_capacity_does_not_disclose_other_tasks(self):
        self.add(5)
        info = self.scheduler.execution_info(self.scheduler.tasks['0'])
        self.assertEqual(3, info['same_opening_tasks'])
        self.assertEqual(2, info['concurrency_limit'])

if __name__ == '__main__':
    unittest.main()
