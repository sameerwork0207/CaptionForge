// Small display helpers (pure, unit-tested).

/** 10 → "10 s", 5400 → "90 min", 10800 → "3 h" */
export function formatDuration(seconds: number): string {
  if (seconds < 120) return `${seconds} s`;
  if (seconds < 3 * 3600 || seconds % 3600 !== 0) return `${Math.round(seconds / 60)} min`;
  return `${seconds / 3600} h`;
}
