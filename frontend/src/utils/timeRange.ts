export const DEPARTURE_TIME_RANGE_OPTIONS = Array.from(
  { length: 24 },
  (_, index) => (index + 1) * 30,
);

export const formatDepartureTimeRange = (minutes?: number | null): string => {
  const safeMinutes = typeof minutes === 'number' && Number.isFinite(minutes) && minutes > 0
    ? minutes
    : 30;

  if (safeMinutes < 60) {
    return `${safeMinutes} 分鐘內`;
  }

  const hours = Math.floor(safeMinutes / 60);
  const remainingMinutes = safeMinutes % 60;
  return remainingMinutes === 0
    ? `${hours} 小時內`
    : `${hours} 小時 ${remainingMinutes} 分鐘內`;
};
