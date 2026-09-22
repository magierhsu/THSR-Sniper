"""Short-lived, allowlisted booking diagnostics, independent of captured print output."""
import contextvars
import json
import os
import time
from datetime import datetime, timezone

_context = contextvars.ContextVar('booking_diagnostics', default=None)
FIELDS = frozenset(('stage', 'endpoint', 'method', 'http_status', 'classification',
                    'basis', 'duration_ms', 'request_attempt', 'retry_delay_ms',
                    'retry_at', 'outcome', 'error_type', 'browser', 'dispatch_delay_ms'))


def emit(event, **fields):
    context = _context.get()
    if context is None:
        return
    record = {k: context[k] for k in ('task_id', 'run_id', 'attempt')}
    record.update(timestamp=datetime.now(timezone.utc).isoformat(),
                  event=event, stage=context['stage'])
    record.update({k: v for k, v in fields.items() if k in FIELDS})
    try:
        # A single small write survives redirect_stdout and is atomic on POSIX pipes.
        os.write(1, (json.dumps(record, ensure_ascii=True, allow_nan=False) + '\n').encode())
    except (OSError, ValueError, TypeError):
        pass  # Debug output must never change booking behavior.


def begin(task_id, run_id, attempt, dispatched_at=None):
    now = time.monotonic()
    token = _context.set(dict(task_id=task_id, run_id=run_id, attempt=attempt,
                             stage='session', started=now, stage_started=now))
    emit('attempt_started', dispatch_delay_ms=round((now - dispatched_at) * 1000, 2)
         if dispatched_at is not None else None)
    return token


def stage(name):
    context = _context.get()
    if context is not None:
        now = time.monotonic()
        emit('stage_finished', duration_ms=round((now - context['stage_started']) * 1000, 2))
        context.update(stage=name, stage_started=now)
        emit('stage_started')


def rejection(message):
    """Only emit fixed categories, never server-provided text."""
    text = message.lower()
    category = ('captcha_error' if any(s in text for s in ('驗證碼', '验证码', 'captcha'))
                else 'busy' if any(s in text for s in ('忙碌', '稍後再試', '維護'))
                else 'unavailable' if any(s in text for s in ('無座位', '售完', '無符合'))
                else 'form_error')
    emit('booking_rejected', classification=category, basis='text_heuristic')


def finish(token, outcome, retry_at=0):
    context = _context.get()
    try:
        now = time.monotonic()
        emit('stage_finished', duration_ms=round((now - context['stage_started']) * 1000, 2))
        emit('attempt_finished', outcome=outcome, retry_at=retry_at,
             duration_ms=round((now - context['started']) * 1000, 2))
    finally:
        _context.reset(token)
