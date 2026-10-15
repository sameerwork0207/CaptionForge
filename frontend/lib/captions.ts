// Pure caption helpers shared by the editor and preview (no React, no DOM).

export interface Cue {
  start: number; // seconds
  end: number; // seconds
  text: string;
}

/** 75.5 → "00:01:15.500" */
export function formatTime(seconds: number): string {
  const totalMs = Math.round(Math.max(0, seconds) * 1000);
  const h = Math.floor(totalMs / 3_600_000);
  const m = Math.floor((totalMs % 3_600_000) / 60_000);
  const s = Math.floor((totalMs % 60_000) / 1000);
  const ms = totalMs % 1000;
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}.${pad(ms, 3)}`;
}

/**
 * Parse "HH:MM:SS.mmm", "MM:SS.mmm", "SS.mmm" (comma also accepted).
 * Returns null for anything else.
 */
export function parseTime(value: string): number | null {
  const v = value.trim().replace(",", ".");
  const m = /^(?:(\d+):)?(?:(\d{1,2}):)?(\d{1,2}(?:\.\d{1,3})?)$/.exec(v);
  if (!m) return null;
  const [, a, b, c] = m;
  let h = 0;
  let min = 0;
  if (a !== undefined && b !== undefined) {
    h = Number(a);
    min = Number(b);
  } else if (a !== undefined) {
    min = Number(a);
  }
  const sec = Number(c);
  if (min >= 60 || sec >= 60) return null;
  return Math.round((h * 3600 + min * 60 + sec) * 1000) / 1000;
}

/** Mirrors the backend rules so problems are shown before saving. */
export function validateCues(cues: Cue[]): string[] {
  const errors: string[] = [];
  cues.forEach((c, i) => {
    const n = i + 1;
    if (!Number.isFinite(c.start) || !Number.isFinite(c.end)) {
      errors.push(`Caption ${n}: invalid time.`);
      return;
    }
    if (c.start < 0) errors.push(`Caption ${n}: start time cannot be negative.`);
    if (c.end <= c.start) errors.push(`Caption ${n}: end time must be after start time.`);
    if (!c.text.trim()) errors.push(`Caption ${n}: text cannot be empty.`);
    if (i > 0 && c.start < cues[i - 1].end)
      errors.push(`Caption ${n} starts before caption ${i} ends.`);
  });
  return errors;
}

function escapeVtt(line: string): string {
  return line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** WebVTT for the in-browser preview <track> (downloads come from the backend). */
export function toVtt(cues: Cue[]): string {
  const body = cues
    .filter((c) => c.end > c.start && c.text.trim())
    .map((c, i) => {
      const lines = c.text.split("\n").map((l) => l.trim()).filter(Boolean).map(escapeVtt);
      return `${i + 1}\n${formatTime(c.start)} --> ${formatTime(c.end)}\n${lines.join("\n")}`;
    });
  return ["WEBVTT", "", ...body.map((b) => b + "\n")].join("\n");
}

export function activeCueIndex(cues: Cue[], time: number): number {
  return cues.findIndex((c) => time >= c.start && time < c.end);
}
