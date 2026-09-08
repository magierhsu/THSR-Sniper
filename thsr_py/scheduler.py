from __future__ import annotations

import io
import multiprocessing
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Any
import logging
import threading
import json
from pathlib import Path
import fcntl
import os

from .flows import run as run_booking_flow
from .opening import OPENING_FIELDS, utc, validate_opening
from .worker_protocol import WorkerResult, configure, cooldown_until
from .schema import (
    MAX_DEPARTURE_TIME_RANGE_MINUTES,
    MAX_PREFERRED_TRAIN_NUMBERS,
    STATION_MAP,
    TIME_TABLE,
    TicketType,
    get_taiwan_now,
    is_ticket_sales_open,
    normalize_preferred_train_numbers,
)


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _booking_worker_ready() -> int:
    """Warm a booking worker and return its process ID."""
    return os.getpid()


def _initialize_worker(cooldown, ready):
    os.environ.setdefault('TF_NUM_INTRAOP_THREADS', '1')
    os.environ.setdefault('TF_NUM_INTEROP_THREADS', '1')
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    configure(cooldown)
    started = time.monotonic()
    error = None
    try:
        from .flows import _get_ocr_model
        from PIL import Image
        import tempfile
        model = _get_ocr_model()
        if model is None:
            raise RuntimeError('OCR 模型載入失敗')
        with tempfile.NamedTemporaryFile(suffix='.png') as image:
            Image.new('RGB', (160, 50), 'white').save(image.name)
            model.predict_image(image.name)
    except Exception as exc:
        error = str(exc)
    import resource
    ready.put({'pid': os.getpid(), 'seconds': time.monotonic() - started,
               'cpu_seconds': time.process_time(),
               'rss_kb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
               'error': error})


def _run_booking_flow_worker(args) -> WorkerResult | tuple[str, str, Optional[str]]:
    """Run one booking flow in an isolated process."""
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    original_non_interactive = os.environ.get("THSR_NON_INTERACTIVE")
    error = None
    control = getattr(args, '_control', None)
    if control is not None:
        from . import worker_protocol
        worker_protocol._control = control

    try:
        os.environ["THSR_NON_INTERACTIVE"] = "1"
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            run_booking_flow(args)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if original_non_interactive is None:
            os.environ.pop("THSR_NON_INTERACTIVE", None)
        else:
            os.environ["THSR_NON_INTERACTIVE"] = original_non_interactive

    result = getattr(args, '_booking_result', None)
    if result is None:  # Legacy embedding/tests.
        return stdout_buffer.getvalue(), stderr_buffer.getvalue(), error
    result.error = result.error or error or '訂票未成功，未找到符合條件的班次或驗證未通過'
    result.retry_at = cooldown_until()
    return result


class BookingStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    DELETED = "deleted"
    WAITING = "waiting"
    PAUSING = "pausing"
    PAUSED = "paused"


class TaskNotFoundError(LookupError):
    pass


class TaskOwnershipError(PermissionError):
    pass


class TaskStateError(ValueError):
    pass


@dataclass
class BookingTask:
    id: str
    from_station: int
    to_station: int
    date: str
    user_id: Optional[str] = None
    adult_cnt: Optional[int] = None
    student_cnt: Optional[int] = None
    child_cnt: Optional[int] = None
    senior_cnt: Optional[int] = None
    disabled_cnt: Optional[int] = None
    time: Optional[int] = None
    time_range_minutes: int = 30
    train_index: Optional[int] = None
    preferred_train_numbers: List[str] = field(default_factory=list)
    seat_prefer: Optional[int] = None
    class_type: Optional[int] = None
    personal_id: Optional[str] = None
    use_membership: Optional[bool] = None
    no_ocr: bool = False
    
    # Scheduler settings
    interval_minutes: int = 5
    max_attempts: Optional[int] = None  # None means unlimited until expired
    opening_mode: bool = False
    sales_open_at: Optional[datetime] = None
    burst_minutes: int = 2
    burst_retry_seconds: int = 5
    last_finished: Optional[datetime] = None
    retry_not_before: Optional[datetime] = None
    confirmation_pending: bool = False
    needs_confirmation: bool = False

    def sales_open(self, now):
        if self.opening_mode:
            return self.sales_open_at is not None and now >= self.sales_open_at
        return is_ticket_sales_open(self.date)

    def in_burst(self, now):
        return bool(self.opening_mode and self.sales_open_at and
                    self.sales_open_at <= now < self.sales_open_at + timedelta(minutes=self.burst_minutes))

    def due_at(self, now):
        if self.last_attempt is None:
            due = self.sales_open_at if self.opening_mode else self.created_at
        elif self.in_burst(now):
            due = (self.last_finished or self.last_attempt) + timedelta(seconds=self.burst_retry_seconds)
        else:
            due = self.last_attempt + timedelta(minutes=self.interval_minutes)
        due = due or now
        if self.opening_mode and self.sales_open_at:
            due = max(due, self.sales_open_at)
        if self.retry_not_before:
            due = max(due, self.retry_not_before)
        return due
    
    # Status tracking
    status: BookingStatus = BookingStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_attempt: Optional[datetime] = None
    attempts: int = 0
    success_pnr: Optional[str] = None
    error_message: Optional[str] = None
    
    def is_expired(self) -> bool:
        """Check if the booking date has passed."""
        try:
            booking_date = datetime.strptime(self.date, "%Y/%m/%d")
            return datetime.now(timezone.utc).date() > booking_date.date()
        except ValueError:
            return False
    
    def should_stop(self) -> bool:
        """Check if task should stop (expired or max attempts reached)."""
        if self.is_expired():
            return True
        if self.max_attempts and self.attempts >= self.max_attempts:
            return True
        return False
    
    def to_args_namespace(self):
        """Convert to argparse.Namespace for compatibility with existing booking flow."""
        from argparse import Namespace
        return Namespace(
            from_=self.from_station,
            to=self.to_station,
            date=self.date,
            adult_cnt=self.adult_cnt,
            student_cnt=self.student_cnt,
            child_cnt=self.child_cnt,
            senior_cnt=self.senior_cnt,
            disabled_cnt=self.disabled_cnt,
            time=self.time,
            time_range_minutes=self.time_range_minutes,
            train_index=self.train_index,
            preferred_train_numbers=list(self.preferred_train_numbers),
            seat_prefer=self.seat_prefer,
            class_type=self.class_type,
            personal_id=self.personal_id,
            use_membership=self.use_membership,
            no_ocr=self.no_ocr,
            stations=False,
            times=False
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "from_station": self.from_station,
            "to_station": self.to_station,
            "date": self.date,
            "user_id": self.user_id,
            "adult_cnt": self.adult_cnt,
            "student_cnt": self.student_cnt,
            "child_cnt": self.child_cnt,
            "senior_cnt": self.senior_cnt,
            "disabled_cnt": self.disabled_cnt,
            "time": self.time,
            "time_range_minutes": self.time_range_minutes,
            "train_index": self.train_index,
            "preferred_train_numbers": list(self.preferred_train_numbers),
            "seat_prefer": self.seat_prefer,
            "class_type": self.class_type,
            "personal_id": self.personal_id,
            "use_membership": self.use_membership,
            "no_ocr": self.no_ocr,
            "interval_minutes": self.interval_minutes,
            "max_attempts": self.max_attempts,
            "status": self.status.value,
            "created_at": self.created_at.isoformat().replace('+00:00', 'Z'),
            "last_attempt": self.last_attempt.isoformat().replace('+00:00', 'Z') if self.last_attempt else None,
            "attempts": self.attempts,
            "success_pnr": self.success_pnr,
            "error_message": self.error_message,
            "opening_mode": self.opening_mode,
            "burst_minutes": self.burst_minutes,
            "burst_retry_seconds": self.burst_retry_seconds,
            "confirmation_pending": self.confirmation_pending,
            "needs_confirmation": self.needs_confirmation,
            **{key: getattr(self, key).isoformat() if getattr(self, key) else None
               for key in ('sales_open_at', 'last_finished', 'retry_not_before')},
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BookingTask":
        """Create from dictionary (JSON deserialization)."""
        task = cls(
            id=data["id"],
            from_station=data["from_station"],
            to_station=data["to_station"],
            date=data["date"],
            user_id=data.get("user_id"),
            adult_cnt=data.get("adult_cnt"),
            student_cnt=data.get("student_cnt"),
            child_cnt=data.get("child_cnt"),
            senior_cnt=data.get("senior_cnt"),
            disabled_cnt=data.get("disabled_cnt"),
            time=data.get("time"),
            time_range_minutes=data.get("time_range_minutes", 30),
            train_index=data.get("train_index"),
            preferred_train_numbers=normalize_preferred_train_numbers(
                data.get("preferred_train_numbers", [])
            ),
            seat_prefer=data.get("seat_prefer"),
            class_type=data.get("class_type"),
            personal_id=data.get("personal_id"),
            use_membership=data.get("use_membership"),
            no_ocr=data.get("no_ocr", False),
            interval_minutes=data.get("interval_minutes", 5),
            max_attempts=data.get("max_attempts"),
            status=BookingStatus(data.get("status", "pending")),
            attempts=data.get("attempts", 0),
            success_pnr=data.get("success_pnr"),
            error_message=data.get("error_message")
        )
        
        if data.get("created_at"):
            created_at_str = data["created_at"]
            # Handle both Z format and raw format (assume UTC if no timezone)
            if created_at_str.endswith('Z'):
                created_at_str = created_at_str.replace('Z', '+00:00')
            elif '+' not in created_at_str and 'Z' not in created_at_str:
                # No timezone info, assume it's already UTC
                created_at_str += '+00:00'
            task.created_at = datetime.fromisoformat(created_at_str)
        if data.get("last_attempt"):
            last_attempt_str = data["last_attempt"]
            # Handle both Z format and raw format (assume UTC if no timezone)
            if last_attempt_str.endswith('Z'):
                last_attempt_str = last_attempt_str.replace('Z', '+00:00')
            elif '+' not in last_attempt_str and 'Z' not in last_attempt_str:
                # No timezone info, assume it's already UTC
                last_attempt_str += '+00:00'
            task.last_attempt = datetime.fromisoformat(last_attempt_str)
            
        for key in ('opening_mode', 'burst_minutes', 'burst_retry_seconds', 'confirmation_pending', 'needs_confirmation'):
            if key in data:
                setattr(task, key, data[key])
        for key in ('sales_open_at', 'last_finished', 'retry_not_before'):
            setattr(task, key, utc(data.get(key)))
        return task


class BookingScheduler:
    """Main scheduler class that manages multiple booking tasks."""
    
    def __init__(self, storage_path: str = None, enable_persistence: bool = True):
        self.enable_persistence = enable_persistence
        
        if storage_path is None and enable_persistence:
            # Auto-detect storage location
            data_dir = Path("/app/data")
            if data_dir.exists():
                # Running in Docker container - use shared volume
                storage_path = "/app/data/thsr_scheduler.json"
            else:
                # Running locally - create .thsr directory in user's home
                home_dir = Path.home() / ".thsr"
                home_dir.mkdir(exist_ok=True)
                storage_path = str(home_dir / "scheduler.json")
        
        self.storage_path = Path(storage_path) if storage_path else None
        self.tasks: Dict[str, BookingTask] = {}
        self.running = False
        self.scheduler_thread: Optional[threading.Thread] = None
        self.max_concurrent_bookings = _bounded_env_int(
            "THSR_MAX_CONCURRENT_BOOKINGS", 2, 1, 2
        )
        self.poll_interval_seconds = _bounded_env_int(
            "THSR_SCHEDULER_POLL_SECONDS", 1, 1, 30
        )
        self._booking_executor: Optional[ProcessPoolExecutor] = None
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._executor_lock_handle = None
        self._inflight = {}
        self._controls = {}
        self._warmup_reports = []
        self._persisted_cooldown = 0.0
        self._pool_broken = False
        self._pool_retry_at = 0.0
        self._cooldown = multiprocessing.get_context('spawn').Value('d', 0.0)
        configure(self._cooldown)
        self.logger = self._setup_logger()
        
        # Initialize file modification time tracking
        self._last_file_mtime: Optional[float] = None
        
        # Load existing tasks if persistence is enabled
        if self.enable_persistence:
            self._load_tasks()
            self._recover_interrupted_tasks()

    def _recover_interrupted_tasks(self) -> None:
        """Recover states left behind when the service stopped mid-attempt."""
        changed = False
        with self._state_lock:
            for task in self.tasks.values():
                if task.confirmation_pending or task.needs_confirmation:
                    task.needs_confirmation = True
                    task.status = BookingStatus.PAUSED
                    task.error_message = '訂票結果待確認，請先向高鐵確認是否成立訂位'
                    changed = True
                elif task.status == BookingStatus.RUNNING:
                    task.status = BookingStatus.PENDING
                    changed = True
                elif task.status == BookingStatus.PAUSING:
                    task.status = BookingStatus.PAUSED
                    changed = True
            if changed:
                self._save_tasks_locked()
    
    def _setup_logger(self) -> logging.Logger:
        """Setup logging for the scheduler."""
        logger = logging.getLogger("thsr_scheduler")
        logger.setLevel(logging.INFO)
        
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        
        return logger
    
    def _should_reload_tasks(self) -> bool:
        """Check if tasks should be reloaded based on file modification time."""
        if not self.enable_persistence or not self.storage_path or not self.storage_path.exists():
            return False
            
        try:
            current_mtime = self.storage_path.stat().st_mtime
            if self._last_file_mtime is None or current_mtime > self._last_file_mtime:
                self._last_file_mtime = current_mtime
                return True
            return False
        except Exception as e:
            self.logger.debug(f"Error checking file mtime: {e}")
            return True  # Reload on error to be safe

    def _load_tasks(self, force: bool = False) -> None:
        """Load tasks from storage file."""
        if not self.enable_persistence or not self.storage_path:
            return
        
        # If not forcing and we already have tasks, don't reload unless file changed
        if not force and self.tasks and not self._should_reload_tasks():
            return
            
        if self.storage_path.exists():
            # Use file locking to prevent concurrent access during read
            lock_path = self.storage_path.with_suffix('.lock')
            
            # Try to acquire lock with timeout (shorter timeout for reads)
            lock_acquired = False
            lock_fd = None
            for attempt in range(5):  # Try for up to 0.5 seconds
                try:
                    lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    lock_acquired = True
                    break
                except FileExistsError:
                    time.sleep(0.1)  # Wait 100ms before retry
            
            try:
                # If we can't get lock, proceed without it (read operations are safer)
                if not lock_acquired:
                    self.logger.debug("Could not acquire read lock, proceeding anyway")
                
                # Check if file is empty
                if self.storage_path.stat().st_size == 0:
                    self.logger.debug("Storage file is empty, starting with no tasks")
                    return
                
                # Clear existing tasks before loading
                self.tasks.clear()
                
                with open(self.storage_path, 'r') as f:
                    content = f.read().strip()
                    if not content:
                        self.logger.debug("Storage file is empty, starting with no tasks")
                        return
                    
                    data = json.loads(content)
                    self._cooldown.value = max(self._cooldown.value, data.get('cooldown_until', 0.0))
                    for task_data in data.get("tasks", []):
                        task = BookingTask.from_dict(task_data)
                        self.tasks[task.id] = task
                    
                self.logger.info(f"Loaded {len(self.tasks)} tasks from {self.storage_path}")
                
            except json.JSONDecodeError as e:
                self.logger.error(f"Failed to parse JSON from storage file: {e}")
                self.logger.info("Creating backup of corrupted file and starting fresh")
                # Backup corrupted file
                backup_path = self.storage_path.with_suffix('.corrupted')
                self.storage_path.replace(backup_path)
                self.logger.info(f"Corrupted file backed up to {backup_path}")
                
            except Exception as e:
                self.logger.error(f"Failed to load tasks from storage: {e}")
                import traceback
                self.logger.debug(f"Load error traceback: {traceback.format_exc()}")
            
            finally:
                # Always release lock if acquired
                if lock_acquired and lock_fd is not None:
                    try:
                        os.close(lock_fd)
                        lock_path.unlink()
                    except:
                        pass
        else:
            self.logger.debug(f"No storage file found at {self.storage_path}, starting with empty task list")

    def _acquire_executor_lock(self) -> bool:
        """Ensure only one process executes booking tasks for a shared data file."""
        if not self.enable_persistence or not self.storage_path:
            return True
        if self._executor_lock_handle is not None:
            return True

        lock_path = self.storage_path.with_suffix('.executor.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(lock_path, 'a+')

        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_handle.close()
            return False

        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(str(os.getpid()))
        lock_handle.flush()
        self._executor_lock_handle = lock_handle
        return True

    def _release_executor_lock(self) -> None:
        """Release the process-wide booking executor lock."""
        if self._executor_lock_handle is None:
            return

        try:
            fcntl.flock(self._executor_lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._executor_lock_handle.close()
            self._executor_lock_handle = None
    
    def _save_tasks(self) -> None:
        """Serialize in-process writers before updating shared task storage."""
        with self._state_lock:
            self._save_tasks_locked()

    def _save_tasks_locked(self) -> bool:
        """Save tasks to storage file with simplified locking."""
        if not self.enable_persistence or not self.storage_path:
            return True
            
        try:
            # Ensure parent directory exists
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                "tasks": [task.to_dict() for task in self.tasks.values()],
                "cooldown_until": self._cooldown.value,
                "last_updated": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            }
            
            # Write to temporary file first, then move to final location (atomic write)
            temp_path = self.storage_path.with_suffix('.tmp')
            
            # Simplified locking - try once, if fails, log warning but continue
            lock_path = self.storage_path.with_suffix('.lock')
            lock_acquired = False
            
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                lock_acquired = True
            except FileExistsError:
                self.logger.debug("File is locked by another process, skipping save")
                return False
            
            try:
                with open(temp_path, 'w') as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                
                # Atomic move
                temp_path.replace(self.storage_path)
                directory_fd = os.open(self.storage_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                
                # Update our tracked modification time
                if self.storage_path.exists():
                    self._last_file_mtime = self.storage_path.stat().st_mtime
                    
                self.logger.debug(f"Tasks saved successfully to {self.storage_path}")
                
            finally:
                # Always release lock
                if lock_acquired:
                    try:
                        os.close(lock_fd)
                        lock_path.unlink()
                    except:
                        pass
                    
        except Exception as e:
            self.logger.error(f"Failed to save tasks to storage: {e}")
            import traceback
            self.logger.debug(f"Save error traceback: {traceback.format_exc()}")
            return False
        return True
    
    def add_task(self, task: BookingTask) -> str:
        """Add a new booking task."""
        with self._state_lock:
            if not task.id:
                task.id = str(uuid.uuid4())
            self.tasks[task.id] = task
            self._save_tasks_locked()
        self.logger.info(f"Added new booking task: {task.id}")
        return task.id
    
    def get_task(self, task_id: str) -> Optional[BookingTask]:
        """Get a specific task by ID."""
        # Only reload if we don't have the task in memory
        if task_id not in self.tasks:
            self._load_tasks()
        return self.tasks.get(task_id)
    
    def list_tasks(self, force_reload: bool = False, include_deleted: bool = False) -> List[BookingTask]:
        """List all tasks, optionally excluding deleted tasks."""
        # Only reload if we have no tasks in memory, to avoid overwriting
        if not self.tasks or force_reload:
            self._load_tasks()
        
        tasks = list(self.tasks.values())
        
        # Filter out deleted tasks unless specifically requested
        if not include_deleted:
            tasks = [task for task in tasks if task.status != BookingStatus.DELETED]
        
        return tasks
    
    def cancel_task(self, task_id: str, user_id: Optional[str] = None) -> bool:
        """Cancel a specific task."""
        with self._state_lock:
            self._load_tasks()
            if task_id in self.tasks:
                task = self.tasks[task_id]
                if user_id is not None and task.user_id != user_id:
                    self.logger.warning(f"User {user_id} attempted to cancel task {task_id} owned by {task.user_id}")
                    return False
                task.status = BookingStatus.CANCELLED
                self._save_tasks_locked()
                self.logger.info(f"Cancelled task: {task_id}")
                return True
            return False

    def pause_task(self, task_id: str, user_id: Optional[str] = None) -> BookingTask:
        """Pause immediately when idle, or request a pause after a running attempt."""
        with self._state_lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if user_id is not None and task.user_id != user_id:
                raise TaskOwnershipError(task_id)

            if task.status in [BookingStatus.PENDING, BookingStatus.WAITING]:
                task.status = BookingStatus.PAUSED
            elif task.status == BookingStatus.RUNNING:
                task.status = BookingStatus.PAUSING
            elif task.status not in [BookingStatus.PAUSING, BookingStatus.PAUSED]:
                raise TaskStateError(
                    f"Task cannot be paused from {task.status.value} status"
                )

            self._save_tasks_locked()
            return task

    def resume_task(self, task_id: str, user_id: Optional[str] = None) -> BookingTask:
        """Resume a paused task and make it immediately eligible to run."""
        with self._state_lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if user_id is not None and task.user_id != user_id:
                raise TaskOwnershipError(task_id)
            if task.needs_confirmation or task.confirmation_pending:
                raise TaskStateError("請先確認是否已訂位成功")
            if task.status != BookingStatus.PAUSED:
                raise TaskStateError("Only paused tasks can be resumed")
            if task.max_attempts and task.attempts >= task.max_attempts:
                raise TaskStateError(
                    "Maximum attempts reached; edit the task before resuming"
                )

            task.status = (
                BookingStatus.PENDING
                if task.sales_open(datetime.now(timezone.utc))
                else BookingStatus.WAITING
            )
            task.last_attempt = None
            self._save_tasks_locked()
            return task

    def update_task(
        self,
        task_id: str,
        replacement: BookingTask,
        user_id: Optional[str] = None,
    ) -> BookingTask:
        """Replace editable booking settings while retaining task identity."""
        editable_fields = (
            "from_station",
            "to_station",
            "date",
            "adult_cnt",
            "student_cnt",
            "child_cnt",
            "senior_cnt",
            "disabled_cnt",
            "time",
            "time_range_minutes",
            "train_index",
            "preferred_train_numbers",
            "seat_prefer",
            "class_type",
            "personal_id",
            "use_membership",
            "no_ocr",
            "interval_minutes",
            "max_attempts",
        )

        with self._state_lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if user_id is not None and task.user_id != user_id:
                raise TaskOwnershipError(task_id)
            if task.needs_confirmation or task.confirmation_pending:
                raise TaskStateError("請先確認是否已訂位成功")
            if task.status != BookingStatus.PAUSED:
                raise TaskStateError("Only paused tasks can be edited")

            for field_name in (*editable_fields, *OPENING_FIELDS):
                value = getattr(replacement, field_name)
                if field_name == "preferred_train_numbers":
                    value = list(value)
                setattr(task, field_name, value)

            task.attempts = 0
            task.last_finished = None
            task.retry_not_before = None
            task.last_attempt = None
            task.success_pnr = None
            task.error_message = None
            if hasattr(task, "result"):
                task.result = None
            self._save_tasks_locked()
            return task
    
    def remove_task(self, task_id: str, user_id: Optional[str] = None) -> bool:
        """Mark a task as deleted instead of removing it completely."""
        # Reload to get latest state
        with self._state_lock:
            self._load_tasks()
            if task_id in self.tasks:
                task = self.tasks[task_id]
            
            # Check user ownership if user_id is provided
                if user_id is not None and task.user_id != user_id:
                    self.logger.warning(f"User {user_id} attempted to remove task {task_id} owned by {task.user_id}")
                    return False
            
            # Mark as deleted instead of removing
                task.status = BookingStatus.DELETED
                self._save_tasks_locked()
                self.logger.info(f"Marked task as deleted: {task_id}")
                return True
            return False
    
    def start_scheduler(self) -> bool:
        """Start the scheduler in a background thread."""
        if self.running:
            self.logger.warning("Scheduler is already running")
            return True
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.logger.warning("Previous scheduler thread is still stopping")
            return False

        if not self._acquire_executor_lock():
            self.logger.warning(
                "Scheduler executor lock is held by another process; "
                "this process will not execute booking tasks"
            )
            return False

        try:
            worker_context = multiprocessing.get_context("spawn")
            self._ready_queue = worker_context.Queue()
            self._booking_executor = ProcessPoolExecutor(
                max_workers=self.max_concurrent_bookings,
                mp_context=worker_context,
                initializer=_initialize_worker,
                initargs=(self._cooldown, self._ready_queue),
            )
            warmup_futures = [
                self._booking_executor.submit(_booking_worker_ready)
                for _ in range(self.max_concurrent_bookings)
            ]
            for _ in warmup_futures:
                report = self._ready_queue.get(timeout=90)
                self._warmup_reports.append(report)
                self.logger.info("OCR worker warmup: %s", report)
            for future in warmup_futures:
                future.result(timeout=90)
        except Exception as exc:
            self.logger.error(f"Failed to initialize booking workers: {exc}")
            if self._booking_executor is not None:
                self._booking_executor.shutdown(wait=False, cancel_futures=True)
                self._booking_executor = None
            self._release_executor_lock()
            return False

        self._stop_event.clear()
        self.running = True
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        self.logger.info(
            "Scheduler started with %s booking workers and a %ss poll interval",
            self.max_concurrent_bookings,
            self.poll_interval_seconds,
        )
        return True
    
    def stop_scheduler(self) -> None:
        """Stop the scheduler."""
        self.running = False
        self._stop_event.set()
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=30)
        if not self.scheduler_thread or not self.scheduler_thread.is_alive():
            self._release_executor_lock()
        else:
            self.logger.warning("Scheduler is waiting for active booking workers to finish")
        self.logger.info("Scheduler stopped")
    
    def _scheduler_loop(self) -> None:
        """Main scheduler loop that runs in background."""
        try:
            while self.running:
                try:
                    self._process_tasks()
                    if self._stop_event.wait(0.1):
                        break
                except Exception as e:
                    self.logger.error(f"Error in scheduler loop: {e}")
                    if self._stop_event.wait(5):
                        break
        finally:
            self.running = False
            while self._inflight:
                self._collect_workers()
                time.sleep(0.1)
            if self._booking_executor is not None:
                self._booking_executor.shutdown(wait=True, cancel_futures=False)
                self._booking_executor = None
            self._release_executor_lock()
    
    def _process_tasks(self) -> None:
        """Collect each completed worker, then dispatch fairly into free slots."""
        self._collect_workers()
        if self._pool_broken:
            if self._inflight or time.monotonic() < self._pool_retry_at:
                return
            try:
                self._booking_executor.shutdown(wait=False, cancel_futures=True)
                context = multiprocessing.get_context('spawn')
                self._ready_queue = context.Queue()
                self._booking_executor = ProcessPoolExecutor(
                    max_workers=self.max_concurrent_bookings, mp_context=context,
                    initializer=_initialize_worker, initargs=(self._cooldown, self._ready_queue))
                ready = [self._booking_executor.submit(_booking_worker_ready)
                         for _ in range(self.max_concurrent_bookings)]
                reports = [self._ready_queue.get(timeout=90) for _ in ready]
                for future in ready:
                    future.result(timeout=90)
                with self._state_lock:
                    self._warmup_reports = reports
                    self._pool_broken = False
                self.logger.info('Booking workers recovered after process failure')
            except Exception:
                self._pool_retry_at = time.monotonic() + 5
                self.logger.exception('Unable to recover booking workers')
                return
        with self._state_lock:
            now = datetime.now(timezone.utc)
            if self._cooldown.value != self._persisted_cooldown:
                if self._save_tasks_locked():
                    self._persisted_cooldown = self._cooldown.value
            due, reservations = [], 0
            changed = False
            for task in self.tasks.values():
                if task.status not in (BookingStatus.PENDING, BookingStatus.WAITING):
                    continue
                if task.needs_confirmation or task.confirmation_pending:
                    task.status = BookingStatus.PAUSED
                    changed = True
                    continue
                if task.is_expired() or task.should_stop():
                    task.status = BookingStatus.EXPIRED if task.is_expired() else BookingStatus.FAILED
                    changed = True
                    continue
                if task.opening_mode and task.sales_open_at and timedelta(0) < task.sales_open_at - now <= timedelta(seconds=30):
                    reservations += 1
                if not task.sales_open(now):
                    changed |= task.status != BookingStatus.WAITING
                    task.status = BookingStatus.WAITING
                    continue
                changed |= task.status != BookingStatus.PENDING
                task.status = BookingStatus.PENDING
                if task.due_at(now) <= now:
                    due.append(task)
            due.sort(key=lambda t: (not t.in_burst(now), t.due_at(now), t.created_at, t.id))
            if self._cooldown.value <= now.timestamp():
                free = self.max_concurrent_bookings - len(self._inflight)
                selected = []
                for task in due:
                    if len(selected) >= free:
                        break
                    if not task.in_burst(now) and free - len(selected) <= min(reservations, self.max_concurrent_bookings):
                        continue
                    selected.append(task)
                self._execute_booking_tasks(selected, now)
            if changed:
                self._save_tasks_locked()
            self._cleanup_deleted_tasks(now)

    def _cleanup_deleted_tasks(self, current_time: datetime) -> None:
        """Clean up tasks that have been marked as deleted for more than 1 hour."""
        if not hasattr(self, '_last_cleanup_time'):
            self._last_cleanup_time = current_time
            return
        
        # Only run cleanup once per hour
        if current_time - self._last_cleanup_time < timedelta(hours=1):
            return
        
        self._last_cleanup_time = current_time
        
        # Remove tasks that have been deleted for more than 1 hour
        deleted_cutoff = current_time - timedelta(hours=1)
        tasks_to_remove = []
        
        for task_id, task in self.tasks.items():
            if (task.status == BookingStatus.DELETED and 
                task.last_attempt and task.last_attempt < deleted_cutoff):
                tasks_to_remove.append(task_id)
        
        for task_id in tasks_to_remove:
            del self.tasks[task_id]
            self.logger.debug(f"Permanently removed deleted task: {task_id}")
        
        if tasks_to_remove:
            self.logger.info(f"Cleaned up {len(tasks_to_remove)} old deleted tasks")
    
    def _save_tasks_safe(self) -> None:
        """Save tasks with conflict detection."""
        if not self.enable_persistence or not self.storage_path:
            return
            
        # Check if file was modified by another process since our last load
        if self._should_reload_tasks():
            self.logger.debug("File was modified by another process, merging changes before save")
            # Load the latest state
            current_tasks = dict(self.tasks)  # Save our current state
            self._load_tasks(force=True)      # Load latest from file
            
            # Merge our changes back - prioritize completed tasks and recent updates
            for task_id, task in current_tasks.items():
                if task_id in self.tasks:
                    existing_task = self.tasks[task_id]
                    
                    # ALWAYS preserve completed tasks (SUCCESS, CANCELLED, DELETED)
                    if task.status in [BookingStatus.SUCCESS, BookingStatus.CANCELLED, BookingStatus.DELETED]:
                        self.tasks[task_id] = task
                        self.logger.debug(f"Preserved completed task {task_id} status: {task.status.value}")
                    # For other tasks, keep our version if it's more recent or has more attempts
                    elif (task.last_attempt and existing_task.last_attempt and 
                        task.last_attempt > existing_task.last_attempt) or \
                       task.attempts > existing_task.attempts:
                        self.tasks[task_id] = task
                else:
                    # Add new task that doesn't exist in file
                    self.tasks[task_id] = task
        
        self._save_tasks()
    
    def _execute_booking_tasks(self, tasks, attempt_time):
        if self._booking_executor is None:
            return
        with self._state_lock:
            for task in tasks:
                if len(self._inflight) >= self.max_concurrent_bookings:
                    break
                if task.status != BookingStatus.PENDING or task.id in self._inflight:
                    continue
                parent, child = multiprocessing.get_context('spawn').Pipe()
                args = task.to_args_namespace()
                args._control = child
                previous_attempt = task.last_attempt
                task.status = BookingStatus.RUNNING
                task.last_attempt = attempt_time
                task.attempts += 1
                try:
                    if not self._save_tasks_locked():
                        raise RuntimeError('無法保存任務，未派發訂票')
                    future = self._booking_executor.submit(_run_booking_flow_worker, args)
                    self.logger.info('Dispatch task %s attempt %s', task.id, task.attempts)
                    self._inflight[task.id] = future
                    self._controls[task.id] = (parent, child)
                except Exception as exc:
                    if isinstance(exc, BrokenProcessPool):
                        self._pool_broken = True
                    parent.close()
                    child.close()
                    task.status = BookingStatus.PENDING
                    task.last_attempt = previous_attempt
                    task.attempts -= 1
                    task.error_message = str(exc)
                    self._save_tasks_locked()
            self._collect_workers()

    def _collect_workers(self):
        with self._state_lock:
            for task_id, future in list(self._inflight.items()):
                task = self.tasks[task_id]
                parent, child = self._controls[task_id]
                if parent.poll():
                    try:
                        message = parent.recv()
                        allowed = False
                        if message == 'confirm' and task.status == BookingStatus.RUNNING and not task.needs_confirmation:
                            task.confirmation_pending = True
                            allowed = bool(self._save_tasks_locked())
                            if not allowed:
                                task.confirmation_pending = False
                        parent.send('allowed' if allowed else 'denied')
                    except (EOFError, OSError):
                        pass
                if not future.done():
                    continue
                try:
                    result = future.result()
                except Exception as exc:
                    if isinstance(exc, BrokenProcessPool):
                        self._pool_broken = True
                    result = WorkerResult(error=type(exc).__name__, uncertain=task.confirmation_pending)
                if isinstance(result, WorkerResult):
                    task.last_finished = datetime.now(timezone.utc)
                    task.retry_not_before = datetime.fromtimestamp(result.retry_at, timezone.utc) if result.retry_at else None
                    if result.pnr:
                        task.success_pnr = result.pnr
                        task.status = BookingStatus.SUCCESS
                        task.error_message = None
                        task.needs_confirmation = task.confirmation_pending = False
                    elif result.uncertain:
                        task.needs_confirmation = True
                        task.confirmation_pending = True
                        task.status = BookingStatus.PAUSED
                        task.error_message = '訂票結果待確認，請先向高鐵確認是否成立訂位'
                    else:
                        task.confirmation_pending = False
                        task.error_message = result.error
                        if task.status not in (BookingStatus.CANCELLED, BookingStatus.DELETED):
                            task.status = BookingStatus.PAUSED if task.status == BookingStatus.PAUSING else BookingStatus.PENDING
                else:
                    self._apply_booking_result(task, *result)
                    task.last_finished = datetime.now(timezone.utc)
                self._save_tasks_locked()
                self.logger.info('Task %s completed attempt %s: %s', task.id, task.attempts, task.status.value)
                parent.close()
                child.close()
                del self._inflight[task_id]
                del self._controls[task_id]

    def resolve_confirmation(self, task_id, user_id, pnr=None):
        with self._state_lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if user_id is not None and task.user_id != user_id:
                raise TaskOwnershipError(task_id)
            if not task.needs_confirmation or task_id in self._inflight:
                raise TaskStateError('此任務沒有待確認的訂位結果')
            task.needs_confirmation = task.confirmation_pending = False
            task.error_message = None
            if pnr:
                task.success_pnr = pnr
                task.status = BookingStatus.SUCCESS
            elif task.should_stop():
                task.status = BookingStatus.FAILED
                task.error_message = '已達嘗試上限或任務已過期，請修改後再繼續'
            else:
                task.status = BookingStatus.PENDING if task.sales_open(datetime.now(timezone.utc)) else BookingStatus.WAITING
                task.last_attempt = None
            if not self._save_tasks_locked():
                task.needs_confirmation = task.confirmation_pending = True
                task.status = BookingStatus.PAUSED
                raise TaskStateError('儲存失敗，仍維持結果待確認')
            return task

    def execution_info(self, task):
        with self._state_lock:
            now = datetime.now(timezone.utc)
            due = max(task.due_at(now), datetime.fromtimestamp(self._cooldown.value, timezone.utc))
            phase = task.status.value
            if task.needs_confirmation:
                phase = 'needs_confirmation'
            elif task.status in (BookingStatus.PENDING, BookingStatus.WAITING):
                phase = 'waiting_opening' if not task.sales_open(now) else ('waiting_resource' if due <= now else 'waiting_retry')
            return {
                'execution_phase': phase,
                'in_burst': task.in_burst(now) and task.status in (BookingStatus.PENDING, BookingStatus.RUNNING),
                'next_attempt_at': due.isoformat() if task.status in (BookingStatus.PENDING, BookingStatus.WAITING) else None,
                'concurrency_limit': self.max_concurrent_bookings,
                'same_opening_tasks': sum(1 for t in self.tasks.values() if t.user_id == task.user_id and t.opening_mode and t.sales_open_at == task.sales_open_at and t.status in (BookingStatus.PENDING, BookingStatus.WAITING, BookingStatus.RUNNING)),
                'warmup_warning': any(r['error'] for r in self._warmup_reports),
            }

    def _apply_booking_result(
        self,
        task: BookingTask,
        output: str,
        stderr_output: str,
        worker_error: Optional[str],
    ) -> None:
        """Apply one isolated worker result to its booking task."""
        self.logger.debug(
            f"Task {task.id} output length: stdout={len(output)}, "
            f"stderr={len(stderr_output)}"
        )

        if "PNR Code:" in output:
            import re

            for line in output.split("\n"):
                if "PNR Code:" in line:
                    pnr = line.split("PNR Code:")[-1].strip()
                    task.success_pnr = re.sub(r"\033\[[0-9;]*m", "", pnr).strip()
                    break

            task.status = BookingStatus.SUCCESS
            task.error_message = None
            self.logger.info(
                f"Task {task.id} completed successfully! PNR: {task.success_pnr}"
            )
            return

        if task.status in [BookingStatus.CANCELLED, BookingStatus.DELETED]:
            self.logger.info(
                f"Task {task.id} finished after being {task.status.value}; "
                "preserving its current status"
            )
            return

        if worker_error:
            task.error_message = f"Booking execution error: {worker_error}"[:500]
        elif stderr_output:
            task.error_message = stderr_output.strip()[:500]
        elif "error" in output.lower():
            error_lines = [
                line for line in output.split("\n") if "error" in line.lower()
            ]
            task.error_message = (
                "; ".join(error_lines[:3])[:500]
                if error_lines
                else "Booking failed - no PNR code found"
            )
        else:
            task.error_message = "Booking failed - no PNR code found"

        task.status = (
            BookingStatus.PAUSED
            if task.status == BookingStatus.PAUSING
            else BookingStatus.PENDING
        )
        self.logger.warning(
            f"Task {task.id} attempt {task.attempts} failed: {task.error_message}"
        )


# Global scheduler instance
_scheduler_instance: Optional[BookingScheduler] = None


def get_scheduler() -> BookingScheduler:
    """Get or create the global scheduler instance."""
    global _scheduler_instance
    if _scheduler_instance is None:
        # Check if we're running in the API service
        # API service should only read, not write to avoid conflicts
        import os
        is_api_service = os.environ.get('THSR_API_MODE') == '1'
        
        if is_api_service:
            # API service uses read-write mode with shared storage
            _scheduler_instance = BookingScheduler(enable_persistence=True)
            # Load tasks from shared storage
            _scheduler_instance._load_tasks(force=True)
        else:
            # Normal scheduler service with full read/write
            _scheduler_instance = BookingScheduler()
        
        # Tasks are already loaded in __init__ or above
    # Don't reload automatically - let individual methods decide
    return _scheduler_instance


def create_booking_task(
    from_station: int,
    to_station: int,
    date: str,
    personal_id: str,
    use_membership: bool,
    user_id: Optional[str] = None,
    adult_cnt: Optional[int] = None,
    student_cnt: Optional[int] = None,
    child_cnt: Optional[int] = None,
    senior_cnt: Optional[int] = None,
    disabled_cnt: Optional[int] = None,
    time: Optional[int] = None,
    time_range_minutes: int = 30,
    train_index: Optional[int] = None,
    preferred_train_numbers: Optional[List[str]] = None,
    seat_prefer: Optional[int] = None,
    class_type: Optional[int] = None,
    interval_minutes: int = 5,
    max_attempts: Optional[int] = None,
    no_ocr: bool = False,  # Default to False to enable OCR for automated booking
    **kwargs
) -> BookingTask:
    """Create a new booking task with validation for real booking scenarios."""
    
    # Validate required parameters for real booking
    if not personal_id or not personal_id.strip():
        raise ValueError("Personal ID is required for booking")
    
    if use_membership is None:
        raise ValueError("Membership preference must be specified (True/False)")
    
    # Validate stations
    if not 1 <= from_station <= len(STATION_MAP):
        raise ValueError(f"Invalid from_station: {from_station} (must be 1-{len(STATION_MAP)})")
    if not 1 <= to_station <= len(STATION_MAP):
        raise ValueError(f"Invalid to_station: {to_station} (must be 1-{len(STATION_MAP)})")
    
    if from_station == to_station:
        raise ValueError("Departure and arrival stations cannot be the same")
    
    # Validate date format and future date
    try:
        booking_date = datetime.strptime(date, "%Y/%m/%d")
        if booking_date.date() < datetime.now(timezone.utc).date():
            raise ValueError(f"Booking date must be in the future: {date}")
    except ValueError as e:
        if "does not match format" in str(e):
            raise ValueError(f"Invalid date format (use YYYY/MM/DD): {date}")
        raise e
    
    # Validate ticket counts - require at least one ticket
    total_tickets = (adult_cnt or 0) + (student_cnt or 0) + (child_cnt or 0) + (senior_cnt or 0) + (disabled_cnt or 0)
    if total_tickets == 0:
        raise ValueError("At least one ticket must be specified")
    if total_tickets > 10:
        raise ValueError("Total ticket count cannot exceed 10")
    
    if adult_cnt is not None and not 0 <= adult_cnt <= 10:
        raise ValueError(f"Adult ticket count must be 0-10: {adult_cnt}")
    if student_cnt is not None and not 0 <= student_cnt <= 10:
        raise ValueError(f"Student ticket count must be 0-10: {student_cnt}")
    if child_cnt is not None and not 0 <= child_cnt <= 10:
        raise ValueError(f"Child ticket count must be 0-10: {child_cnt}")
    if senior_cnt is not None and not 0 <= senior_cnt <= 10:
        raise ValueError(f"Senior ticket count must be 0-10: {senior_cnt}")
    if disabled_cnt is not None and not 0 <= disabled_cnt <= 10:
        raise ValueError(f"Disabled ticket count must be 0-10: {disabled_cnt}")
    
    # Validate optional parameters
    if time is None:
        raise ValueError("Departure time is required")
    if not 1 <= time <= len(TIME_TABLE):
        raise ValueError(f"Invalid time slot: {time} (must be 1-{len(TIME_TABLE)})")

    if not 1 <= time_range_minutes <= MAX_DEPARTURE_TIME_RANGE_MINUTES:
        raise ValueError(
            f"Departure time range must be 1-{MAX_DEPARTURE_TIME_RANGE_MINUTES} "
            f"minutes: {time_range_minutes}"
        )
    
    if train_index is not None and train_index < 1:
        raise ValueError(f"Invalid train index: {train_index} (must be >= 1)")

    normalized_preferred_trains = normalize_preferred_train_numbers(
        preferred_train_numbers
    )
    if train_index is not None and normalized_preferred_trains:
        raise ValueError(
            "train_index and preferred_train_numbers cannot be used together"
        )
    if len(normalized_preferred_trains) > MAX_PREFERRED_TRAIN_NUMBERS:
        raise ValueError(
            f"At most {MAX_PREFERRED_TRAIN_NUMBERS} preferred trains are allowed"
        )
    
    if seat_prefer is not None and seat_prefer not in [0, 1, 2]:
        raise ValueError(f"Invalid seat preference: {seat_prefer} (must be 0, 1, or 2)")
    
    if class_type is not None and class_type not in [0, 1]:
        raise ValueError(f"Invalid class type: {class_type} (must be 0 or 1)")
    
    # Validate interval
    if interval_minutes < 1:
        raise ValueError(f"Interval must be at least 1 minute: {interval_minutes}")
    if interval_minutes > 60:
        raise ValueError(f"Interval should not exceed 60 minutes: {interval_minutes}")
    if max_attempts is not None and max_attempts < 1:
        raise ValueError(f"Maximum attempts must be at least 1: {max_attempts}")
    
    # Validate personal ID format (basic check)
    personal_id = personal_id.strip().upper()
    if len(personal_id) != 10:
        raise ValueError("Personal ID must be 10 characters long")
    
    if 'opening_mode' in kwargs or 'sales_open_at' in kwargs:
        kwargs['sales_open_at'] = validate_opening(
            kwargs.get('opening_mode', False), kwargs.get('sales_open_at'),
            kwargs.get('burst_minutes', 2), kwargs.get('burst_retry_seconds', 5), date)
    task = BookingTask(
        id=str(uuid.uuid4()),
        from_station=from_station,
        to_station=to_station,
        date=date,
        user_id=user_id,
        personal_id=personal_id,
        use_membership=use_membership,
        adult_cnt=adult_cnt,
        student_cnt=student_cnt,
        child_cnt=child_cnt,
        senior_cnt=senior_cnt,
        disabled_cnt=disabled_cnt,
        time=time,
        time_range_minutes=time_range_minutes,
        train_index=train_index,
        preferred_train_numbers=normalized_preferred_trains,
        seat_prefer=seat_prefer,
        class_type=class_type,
        interval_minutes=interval_minutes,
        max_attempts=max_attempts,
        no_ocr=no_ocr,  # Force no OCR for automated execution
        **kwargs
    )
    
    return task
