import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildBookingFormDefaults,
  buildBookingPreferences,
  getTaiwanToday,
} from '../src/utils/bookingPreferences';
import type {
  BookingFormData,
  BookingPreferences,
  StationInfo,
  TimeSlotInfo,
} from '../src/types';

const stations: StationInfo[] = [
  { id: 1, name: '南港' },
  { id: 2, name: '台北' },
  { id: 7, name: '台中' },
];

const timeSlots: TimeSlotInfo[] = [
  { id: 1, time: '00:00', formatted_time: '00:00' },
  { id: 11, time: '05:00', formatted_time: '05:00' },
];

const preferences: BookingPreferences = {
  version: 1,
  from_station: 2,
  to_station: 7,
  date: '2026-09-25',
  adult_cnt: 1,
  student_cnt: 0,
  child_cnt: 2,
  senior_cnt: 0,
  disabled_cnt: 0,
  time: 11,
  time_range_minutes: 120,
  preferred_train_numbers: ['825', '838'],
  seat_prefer: 1,
  class_type: 0,
  no_ocr: false,
  interval_minutes: 3,
  max_attempts: 20,
};

test('uses system defaults when no preferences have been saved', () => {
  const defaults = buildBookingFormDefaults(
    null,
    stations,
    timeSlots,
    '2026-08-31',
  );

  assert.equal(defaults.fromStation, 1);
  assert.equal(defaults.toStation, 2);
  assert.equal(defaults.date, '2026-08-31');
  assert.equal(defaults.adultCount, 1);
  assert.equal(defaults.departureTime, undefined);
  assert.equal(defaults.departureTimeRangeMinutes, 30);
  assert.equal(defaults.preferredTrainNumbers, '');
  assert.equal(defaults.intervalMinutes, 5);
});

test('restores every valid booking field', () => {
  const defaults = buildBookingFormDefaults(
    preferences,
    stations,
    timeSlots,
    '2026-08-31',
  );

  assert.deepEqual(defaults, {
    fromStation: 2,
    toStation: 7,
    date: '2026-09-25',
    adultCount: 1,
    studentCount: 0,
    childCount: 2,
    seniorCount: 0,
    disabledCount: 0,
    departureTime: 11,
    departureTimeRangeMinutes: 120,
    preferredTrainNumbers: '825, 838',
    seatPreference: 1,
    classType: 0,
    useOCR: true,
    intervalMinutes: 3,
    maxAttempts: 20,
  });
});

test('replaces a past date with Taiwan today', () => {
  const defaults = buildBookingFormDefaults(
    { ...preferences, date: '2026-08-30' },
    stations,
    timeSlots,
    '2026-08-31',
  );

  assert.equal(defaults.date, '2026-08-31');
});

test('falls back per field when stored values are stale or invalid', () => {
  const damaged = {
    ...preferences,
    from_station: 99,
    date: '2026-02-31',
    adult_cnt: '1',
    time: 38,
    time_range_minutes: '120',
    preferred_train_numbers: ['825', '0825'],
    seat_prefer: 9,
    interval_minutes: 0,
    max_attempts: 0,
  } as unknown as BookingPreferences;
  const defaults = buildBookingFormDefaults(
    damaged,
    stations,
    timeSlots,
    '2026-08-31',
  );

  assert.equal(defaults.fromStation, 1);
  assert.equal(defaults.toStation, 7);
  assert.equal(defaults.date, '2026-08-31');
  assert.equal(defaults.adultCount, 1);
  assert.equal(defaults.childCount, 2);
  assert.equal(defaults.departureTime, undefined);
  assert.equal(defaults.departureTimeRangeMinutes, 30);
  assert.equal(defaults.preferredTrainNumbers, '');
  assert.equal(defaults.seatPreference, 0);
  assert.equal(defaults.intervalMinutes, 5);
  assert.equal(defaults.maxAttempts, undefined);
});

test('builds the versioned API payload without personal information', () => {
  const form: BookingFormData = {
    fromStation: 2,
    toStation: 7,
    date: '2026-09-25',
    adultCount: 1,
    studentCount: 0,
    childCount: 2,
    seniorCount: 0,
    disabledCount: 0,
    departureTime: 11,
    departureTimeRangeMinutes: 120,
    preferredTrainNumbers: '0825, 838',
    seatPreference: 1,
    classType: 0,
    useOCR: true,
    intervalMinutes: 3,
    maxAttempts: 20,
  };

  const payload = buildBookingPreferences(form, ['825', '838']);
  assert.deepEqual(payload, preferences);
  assert.equal('personal_id' in payload, false);
  assert.equal('use_membership' in payload, false);
});

test('computes today in the Taiwan timezone', () => {
  assert.equal(getTaiwanToday(new Date('2026-08-30T16:30:00Z')), '2026-08-31');
});
