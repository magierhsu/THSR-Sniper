import assert from 'node:assert/strict';
import test from 'node:test';
import { openingLocal, openingPayload } from '../src/utils/opening';
import { buildBookingFormDefaults } from '../src/utils/bookingPreferences';

test('Taiwan midnight survives round trip independently of browser timezone', () => {
  const payload = openingPayload({opening_mode: true, sales_open_at: '2030-09-02T00:00', burst_minutes: 5, burst_retry_seconds: 3});
  assert.equal(payload.sales_open_at, '2030-09-01T16:00:00.000Z');
  assert.equal(openingLocal(payload.sales_open_at), '2030-09-02T00:00');
});

test('past opening time is cleared but mode and valid burst settings are retained', () => {
  const defaults = buildBookingFormDefaults({version: 1, opening_mode: true, sales_open_at: '2000-01-01T00:00:00Z', burst_minutes: 4, burst_retry_seconds: 8}, [{id:1,name:'A'}, {id:2,name:'B'}], []);
  assert.equal(defaults.sales_open_at, '');
  assert.equal(defaults.opening_mode, true);
  assert.equal(defaults.burst_minutes, 4);
  assert.equal(defaults.burst_retry_seconds, 8);
});

test('invalid stored opening fields fall back independently', () => {
  const defaults = buildBookingFormDefaults({version: 1, sales_open_at: 'broken', burst_minutes: 999, burst_retry_seconds: 0}, [{id:1,name:'A'}, {id:2,name:'B'}], []);
  assert.equal(defaults.sales_open_at, '');
  assert.equal(defaults.burst_minutes, 2);
  assert.equal(defaults.burst_retry_seconds, 5);
});
