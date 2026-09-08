"""Opening-mode settings shared by API, CLI and scheduler."""
from datetime import datetime, timedelta, timezone

OPENING_FIELDS = ('opening_mode', 'sales_open_at', 'burst_minutes', 'burst_retry_seconds')

def utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('開賣時間必須包含時區（台灣為 +08:00）')
    return value.astimezone(timezone.utc)

def validate_opening(enabled, opening, minutes, seconds, date):
    if not 1 <= minutes <= 5 or not 3 <= seconds <= 10:
        raise ValueError('快速期間須為 1–5 分鐘，等待須為 3–10 秒')
    if not enabled:
        return None
    opening = utc(opening)
    if opening is None or opening <= datetime.now(timezone.utc):
        raise ValueError('請指定未來的開賣時間')
    end = datetime.strptime(date.replace('-', '/'), '%Y/%m/%d').replace(
        tzinfo=timezone(timedelta(hours=8))) + timedelta(days=1)
    if opening >= end:
        raise ValueError('開賣時間不可晚於乘車日結束')
    return opening
