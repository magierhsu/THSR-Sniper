"""Offline integration: real spawn/OCR/IPC, simulated THSR confirmation."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thsr_py.scheduler import BookingScheduler, BookingTask, BookingStatus
from thsr_py.worker_protocol import WorkerResult, confirmation_permission

def simulated_booking(args):
    from thsr_py import worker_protocol
    worker_protocol._control = args._control
    confirmation_permission()
    return WorkerResult(pnr='12345678')

class SimulatedExecutor:
    def __init__(self, actual):
        self.actual = actual

    def submit(self, function, args):
        return self.actual.submit(simulated_booking, args)

    def shutdown(self, **kwargs):
        return self.actual.shutdown(**kwargs)

class SpawnIntegrationTests(unittest.TestCase):
    def test_ocr_workers_and_confirmation_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = BookingScheduler(storage_path=str(Path(temp) / 'tasks.json'))
            try:
                self.assertTrue(scheduler.start_scheduler())
                self.assertEqual(2, len({r['pid'] for r in scheduler._warmup_reports}))
                self.assertFalse(any(r['error'] for r in scheduler._warmup_reports), scheduler._warmup_reports)
                scheduler._booking_executor = SimulatedExecutor(scheduler._booking_executor)
                opening = datetime.now(timezone.utc) + timedelta(seconds=2)
                for i in range(5):
                    scheduler.add_task(BookingTask(id=str(i), from_station=1, to_station=2,
                        date=(opening + timedelta(days=7)).strftime('%Y/%m/%d'),
                        opening_mode=True, sales_open_at=opening, adult_cnt=1, time=1))
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with scheduler._state_lock:
                        if all(t.status == BookingStatus.SUCCESS for t in scheduler.tasks.values()):
                            break
                    time.sleep(0.05)
                self.assertTrue(all(t.status == BookingStatus.SUCCESS for t in scheduler.tasks.values()))
                latencies = [(t.last_attempt - opening).total_seconds() for t in scheduler.tasks.values()]
                self.assertGreaterEqual(min(latencies), 0)
                self.assertLess(sorted(latencies)[1], 1)
                self.assertEqual([1] * 5, [t.attempts for t in scheduler.tasks.values()])
                print('OPENING_BENCHMARK', json.dumps({'workers': scheduler._warmup_reports,
                      'dispatch_seconds': latencies}))
            finally:
                scheduler.stop_scheduler()

if __name__ == '__main__':
    unittest.main()
