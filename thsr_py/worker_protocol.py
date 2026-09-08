"""Process-local transport hooks; shared cooldown is provided by scheduler."""
import time
from dataclasses import dataclass

_cooldown = None
_control = None

@dataclass
class WorkerResult:
    pnr: str = None
    error: str = None
    stage: str = 'session'
    uncertain: bool = False
    retry_at: float = 0.0

class DeferredRequest(Exception):
    pass

def configure(cooldown=None, control=None):
    global _cooldown, _control
    _cooldown, _control = cooldown, control

def cooldown_until():
    if _cooldown is None:
        return 0.0
    with _cooldown.get_lock():
        return _cooldown.value

def defer_until(epoch):
    if _cooldown is not None:
        with _cooldown.get_lock():
            _cooldown.value = max(_cooldown.value, epoch)

def check_cooldown():
    if cooldown_until() > time.time():
        raise DeferredRequest('高鐵限流冷卻中，稍後重試')

def confirmation_permission():
    if _control is None:
        return  # Interactive CLI has no automatic retry.
    _control.send('confirm')
    if not _control.poll(15) or _control.recv() != 'allowed':
        raise RuntimeError('無法持久化訂位確認階段，未送出確認請求')
