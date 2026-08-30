export const MAX_PREFERRED_TRAIN_NUMBERS = 20;

export interface PreferredTrainParseResult {
  values: string[];
  error?: string;
}

const normalizeTrainNumber = (value: string): string =>
  value.replace(/^0+(?=\d)/, '');

export const parsePreferredTrainNumbers = (
  input: string | null | undefined
): PreferredTrainParseResult => {
  const tokens = (input || '')
    .trim()
    .split(/[,，\s]+/)
    .filter(Boolean);

  if (tokens.length > MAX_PREFERRED_TRAIN_NUMBERS) {
    return {
      values: [],
      error: `偏好車次最多 ${MAX_PREFERRED_TRAIN_NUMBERS} 筆`,
    };
  }

  const values: string[] = [];
  const seen = new Set<string>();
  for (const token of tokens) {
    if (!/^\d{1,4}$/.test(token)) {
      return { values: [], error: `「${token}」必須是 1–4 位數字` };
    }

    const normalized = normalizeTrainNumber(token);
    if (seen.has(normalized)) {
      return { values: [], error: `車次 ${token} 重複` };
    }
    seen.add(normalized);
    values.push(normalized);
  }

  return { values };
};

export const formatPreferredTrainNumbers = (
  values: string[] | null | undefined
): string => (values || []).join(', ');
