import { openingLocal, openingPayload } from './opening';
import {
  BookingFormData,
  BookingPreferences,
  StationInfo,
  TimeSlotInfo,
} from '@/types';
import { DEPARTURE_TIME_RANGE_OPTIONS } from './timeRange';
import { parsePreferredTrainNumbers } from './trainPreferences';

const DEFAULT_TICKET_COUNTS = [1, 0, 0, 0, 0] as const;

const integerInRange = (
  value: unknown,
  minimum: number,
  maximum: number,
): number | undefined => {
  return typeof value === 'number'
    && Number.isInteger(value)
    && value >= minimum
    && value <= maximum
    ? value
    : undefined;
};

const isValidDate = (value: unknown): value is string => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return false;
  }

  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year
    && date.getUTCMonth() === month - 1
    && date.getUTCDate() === day;
};

export const getTaiwanToday = (now = new Date()): string => {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Taipei',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
};

export type BookingFormDefaults = Omit<BookingFormData, 'departureTime'> & {
  departureTime?: number;
};

export const buildBookingFormDefaults = (
  rawPreferences: Partial<BookingPreferences> | null | undefined,
  stations: StationInfo[],
  timeSlots: TimeSlotInfo[],
  today = getTaiwanToday(),
): BookingFormDefaults => {
  const preferences = rawPreferences?.version === 1 ? rawPreferences : undefined;
  const stationIds = new Set(stations.map(station => station.id));
  const defaultFrom = stationIds.has(1) ? 1 : stations[0]?.id || 1;
  const defaultTo = stationIds.has(2) && defaultFrom !== 2
    ? 2
    : stations.find(station => station.id !== defaultFrom)?.id || 2;

  const preferredFrom = integerInRange(preferences?.from_station, 1, 12);
  const fromStation = preferredFrom && stationIds.has(preferredFrom)
    ? preferredFrom
    : defaultFrom;
  const preferredTo = integerInRange(preferences?.to_station, 1, 12);
  const toStation = preferredTo
    && stationIds.has(preferredTo)
    && preferredTo !== fromStation
    ? preferredTo
    : (defaultTo === fromStation
      ? stations.find(station => station.id !== fromStation)?.id || 2
      : defaultTo);

  const storedDate = isValidDate(preferences?.date)
    && preferences.date >= today
    ? preferences.date
    : today;

  const timeIds = new Set(timeSlots.map(slot => slot.id));
  const preferredTime = integerInRange(preferences?.time, 1, 38);
  const departureTime = preferredTime && timeIds.has(preferredTime)
    ? preferredTime
    : undefined;

  let ticketCounts = [
    integerInRange(preferences?.adult_cnt, 0, 10) ?? DEFAULT_TICKET_COUNTS[0],
    integerInRange(preferences?.student_cnt, 0, 10) ?? DEFAULT_TICKET_COUNTS[1],
    integerInRange(preferences?.child_cnt, 0, 10) ?? DEFAULT_TICKET_COUNTS[2],
    integerInRange(preferences?.senior_cnt, 0, 10) ?? DEFAULT_TICKET_COUNTS[3],
    integerInRange(preferences?.disabled_cnt, 0, 10) ?? DEFAULT_TICKET_COUNTS[4],
  ];
  const ticketTotal = ticketCounts.reduce((total, count) => total + count, 0);
  if (ticketTotal < 1 || ticketTotal > 10) {
    ticketCounts = [...DEFAULT_TICKET_COUNTS];
  }

  const preferredTrains = Array.isArray(preferences?.preferred_train_numbers)
    && preferences.preferred_train_numbers.every(value => typeof value === 'string')
    ? parsePreferredTrainNumbers(preferences.preferred_train_numbers.join(', '))
    : { values: [] as string[] };

  const storedRange = typeof preferences?.time_range_minutes === 'number'
    ? preferences.time_range_minutes
    : Number.NaN;
  const departureTimeRangeMinutes = DEPARTURE_TIME_RANGE_OPTIONS.includes(storedRange)
    ? storedRange
    : 30;

  return {
    opening_mode: preferences?.opening_mode === true,
    sales_open_at: preferences?.sales_open_at && Date.parse(preferences.sales_open_at) > Date.now()
      ? openingLocal(preferences.sales_open_at) : '',
    burst_minutes: integerInRange(preferences?.burst_minutes, 1, 5) ?? 2,
    burst_retry_seconds: integerInRange(preferences?.burst_retry_seconds, 3, 10) ?? 5,
    fromStation,
    toStation,
    date: storedDate,
    adultCount: ticketCounts[0],
    studentCount: ticketCounts[1],
    childCount: ticketCounts[2],
    seniorCount: ticketCounts[3],
    disabledCount: ticketCounts[4],
    departureTime,
    departureTimeRangeMinutes,
    preferredTrainNumbers: preferredTrains.error
      ? ''
      : preferredTrains.values.join(', '),
    seatPreference: integerInRange(preferences?.seat_prefer, 0, 2) ?? 0,
    classType: integerInRange(preferences?.class_type, 0, 1) ?? 0,
    useOCR: typeof preferences?.no_ocr === 'boolean' ? !preferences.no_ocr : true,
    intervalMinutes: integerInRange(preferences?.interval_minutes, 1, 60) ?? 5,
    maxAttempts: integerInRange(
      preferences?.max_attempts,
      1,
      Number.MAX_SAFE_INTEGER,
    ),
  };
};

export const buildBookingPreferences = (
  form: BookingFormData,
  preferredTrainNumbers: string[],
): BookingPreferences => ({
  version: 1,
  ...openingPayload(form),
  from_station: Number(form.fromStation),
  to_station: Number(form.toStation),
  date: form.date,
  adult_cnt: Number(form.adultCount) || 0,
  student_cnt: Number(form.studentCount) || 0,
  child_cnt: Number(form.childCount) || 0,
  senior_cnt: Number(form.seniorCount) || 0,
  disabled_cnt: Number(form.disabledCount) || 0,
  time: Number(form.departureTime),
  time_range_minutes: Number(form.departureTimeRangeMinutes),
  preferred_train_numbers: preferredTrainNumbers,
  seat_prefer: Number(form.seatPreference),
  class_type: Number(form.classType),
  no_ocr: !form.useOCR,
  interval_minutes: Number(form.intervalMinutes),
  max_attempts: form.maxAttempts ? Number(form.maxAttempts) : null,
});
