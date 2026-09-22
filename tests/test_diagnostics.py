import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from thsr_py import diagnostics
from thsr_py.booking_session import BookingSession, BOOKING_PAGE_URL
from thsr_py.scheduler import _run_booking_flow_worker
from thsr_py.worker_protocol import WorkerResult


class DiagnosticsTests(unittest.TestCase):
    def test_capture_privacy_and_context_reset(self):
        with patch('thsr_py.diagnostics.os.write') as write:
            token = diagnostics.begin('task', 'run', 2)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                diagnostics.emit('http_response', http_status=503,
                                 url='secret-session', pnr='secret-pnr', body='secret-id')
                diagnostics.stage('query')
            diagnostics.finish(token, 'success')
            count = write.call_count
            diagnostics.emit('outside_attempt')
            self.assertEqual(count, write.call_count)
            records = [json.loads(c.args[1]) for c in write.call_args_list]
            self.assertTrue(all(r['run_id'] == 'run' for r in records))
            self.assertNotIn('secret', str(records))
            self.assertEqual(records[-1]['outcome'], 'success')
            self.assertIn('duration_ms', records[-1])

    def test_logging_failure_does_not_abort(self):
        with patch('thsr_py.diagnostics.os.write', side_effect=OSError('full')):
            token = diagnostics.begin('task', 'run', 1)
            diagnostics.finish(token, 'failed')

    def test_rejection_only_keeps_category(self):
        with patch('thsr_py.diagnostics.os.write') as write:
            token = diagnostics.begin('task', 'run', 1)
            diagnostics.rejection('驗證碼錯誤 SECRET')
            diagnostics.finish(token, 'failed')
            records = [json.loads(c.args[1]) for c in write.call_args_list]
            self.assertEqual(records[1]['classification'], 'captcha_error')
            self.assertNotIn('SECRET', str(records))

    def test_worker_keeps_success_diagnostics_without_pnr(self):
        def flow(args):
            print('sensitive raw output')
            args._booking_result = WorkerResult(pnr='SECRET-PNR')
        with patch('thsr_py.scheduler.run_booking_flow', side_effect=flow), \
             patch('thsr_py.diagnostics.os.write') as write:
            result = _run_booking_flow_worker(SimpleNamespace())
            records = [json.loads(c.args[1]) for c in write.call_args_list]
            self.assertEqual(result.pnr, 'SECRET-PNR')
            self.assertEqual(records[-1]['outcome'], 'success')
            self.assertNotIn('SECRET-PNR', str(records))
            self.assertNotIn('sensitive raw output', str(records))

    def test_http_error_does_not_log_exception_url(self):
        with patch('thsr_py.diagnostics.os.write') as write, \
             patch('curl_cffi.requests.Session.request', side_effect=RuntimeError('SECRET-URL')):
            token = diagnostics.begin('task', 'run', 1)
            session = BookingSession()
            try:
                with self.assertRaises(RuntimeError):
                    session.get(BOOKING_PAGE_URL)
            finally:
                session.close()
                diagnostics.finish(token, 'failed')
            records = [json.loads(c.args[1]) for c in write.call_args_list]
            self.assertNotIn('SECRET-URL', str(records))
            self.assertTrue(any(r['event'] == 'http_exception' for r in records))
