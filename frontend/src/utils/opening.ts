import { OpeningSettings } from '@/types';

export const openingLocal = (value?: string | null): string => {
  if (!value || !Number.isFinite(Date.parse(value))) return '';
  return new Date(Date.parse(value) + 8 * 3600000).toISOString().slice(0, 16);
};

export const openingPayload = (data: OpeningSettings): OpeningSettings => ({
  opening_mode: Boolean(data.opening_mode),
  sales_open_at: data.opening_mode && data.sales_open_at
    ? new Date(`${data.sales_open_at}:00+08:00`).toISOString() : null,
  burst_minutes: Number(data.burst_minutes) || 2,
  burst_retry_seconds: Number(data.burst_retry_seconds) || 5,
});
