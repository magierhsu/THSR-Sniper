import json
import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BOOKING_PREFERENCES_KEY = "booking_form"
MAX_PREFERRED_TRAIN_NUMBERS = 20
VALID_TIME_RANGES = set(range(30, 12 * 60 + 1, 30))


class BookingPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    opening_mode: bool = False
    sales_open_at: Optional[str] = None
    burst_minutes: int = Field(default=2, ge=1, le=5)
    burst_retry_seconds: int = Field(default=5, ge=3, le=10)

    @field_validator('sales_open_at')
    @classmethod
    def validate_opening_time(cls, value):
        if value is not None:
            from datetime import timezone
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('sales_open_at must include timezone')
            return parsed.astimezone(timezone.utc).isoformat()
        return value
    from_station: int = Field(ge=1, le=12)
    to_station: int = Field(ge=1, le=12)
    date: str
    adult_cnt: int = Field(ge=0, le=10)
    student_cnt: int = Field(ge=0, le=10)
    child_cnt: int = Field(ge=0, le=10)
    senior_cnt: int = Field(ge=0, le=10)
    disabled_cnt: int = Field(ge=0, le=10)
    time: int = Field(ge=1, le=38)
    time_range_minutes: int
    preferred_train_numbers: List[str] = Field(default_factory=list)
    seat_prefer: int = Field(ge=0, le=2)
    class_type: int = Field(ge=0, le=1)
    no_ocr: bool
    interval_minutes: int = Field(ge=1, le=60)
    max_attempts: Optional[int] = Field(default=None, ge=1)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise ValueError("date must use YYYY-MM-DD format") from exc
        return value

    @field_validator("time_range_minutes")
    @classmethod
    def validate_time_range(cls, value: int) -> int:
        if value not in VALID_TIME_RANGES:
            raise ValueError("time_range_minutes must be a 30-minute step from 30 to 720")
        return value

    @field_validator("preferred_train_numbers", mode="before")
    @classmethod
    def normalize_preferred_trains(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError("preferred_train_numbers must be an array")
        if len(value) > MAX_PREFERRED_TRAIN_NUMBERS:
            raise ValueError(
                f"preferred_train_numbers accepts at most {MAX_PREFERRED_TRAIN_NUMBERS} entries"
            )

        normalized: List[str] = []
        seen = set()
        for item in value:
            if not isinstance(item, str):
                raise ValueError("preferred train numbers must be strings")
            text = item.strip()
            if not re.fullmatch(r"\d{1,4}", text):
                raise ValueError("preferred train numbers must contain 1-4 digits")
            text = text.lstrip("0") or "0"
            if text not in seen:
                seen.add(text)
                normalized.append(text)
        return normalized

    @model_validator(mode="after")
    def validate_route_and_tickets(self):
        if self.from_station == self.to_station:
            raise ValueError("departure and arrival stations must differ")

        ticket_total = sum(
            [
                self.adult_cnt,
                self.student_cnt,
                self.child_cnt,
                self.senior_cnt,
                self.disabled_cnt,
            ]
        )
        if not 1 <= ticket_total <= 10:
            raise ValueError("total ticket count must be between 1 and 10")
        return self


class BookingPreferencesResponse(BaseModel):
    # GET remains tolerant of legacy or manually damaged fields so the client can
    # recover each field independently. PUT still validates BookingPreferences.
    preferences: Optional[Dict[str, Any]]


def load_preferences(raw_preferences: Optional[str]) -> Dict[str, Any]:
    if not raw_preferences:
        return {}
    try:
        parsed = json.loads(raw_preferences)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_booking_preferences(
    raw_preferences: Optional[str],
) -> Optional[Dict[str, Any]]:
    stored = load_preferences(raw_preferences).get(BOOKING_PREFERENCES_KEY)
    return stored if isinstance(stored, dict) else None


def merge_booking_preferences(
    raw_preferences: Optional[str], preferences: BookingPreferences
) -> str:
    stored = load_preferences(raw_preferences)
    stored[BOOKING_PREFERENCES_KEY] = preferences.model_dump(mode="json")
    return json.dumps(stored, ensure_ascii=False, separators=(",", ":"))
