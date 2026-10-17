"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { activeCueIndex, formatTime, parseTime, toVtt, validateCues, type Cue } from "@/lib/captions";
import { deleteJob, downloadFile, saveSegments, type ResultResponse } from "@/lib/api";

interface Row extends Cue {
  key: number;
  startText: string;
  endText: string;
}

let nextKey = 1;
const toRows = (r: ResultResponse): Row[] =>
  (r.segments ?? []).map((s) => ({
    key: nextKey++,
    start: s.start,
    end: s.end,
    text: s.text,
    startText: formatTime(s.start),
    endText: formatTime(s.end),
  }));

export default function CaptionEditor({
  result,
  mediaUrl,
  onSaved,
  onDeleted,
}: {
  result: ResultResponse;
  mediaUrl: string | null;
  onSaved: (r: ResultResponse) => void;
  onDeleted: () => void;
}) {
  const [rows, setRows] = useState<Row[]>(() => toRows(result));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [current, setCurrent] = useState(-1);
  const mediaRef = useRef<HTMLVideoElement & HTMLAudioElement>(null);

  const cues: Cue[] = useMemo(
    () =>
      rows.map((r) => ({
        start: parseTime(r.startText) ?? Number.NaN,
        end: parseTime(r.endText) ?? Number.NaN,
        text: r.text,
      })),
    [rows],
  );
  const problems = validateCues(cues);

  // Live caption preview on the media element via a WebVTT blob track.
  const vttUrl = useMemo(() => {
    if (!mediaUrl) return null;
    return URL.createObjectURL(new Blob([toVtt(cues)], { type: "text/vtt" }));
  }, [mediaUrl, cues]);
  useEffect(() => () => {
    if (vttUrl) URL.revokeObjectURL(vttUrl);
  }, [vttUrl]);
  useEffect(() => {
    const el = mediaRef.current;
    if (el && el.textTracks[0]) el.textTracks[0].mode = "showing";
  }, [vttUrl]);

  const update = (key: number, patch: Partial<Row>) => {
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
    setDirty(true);
  };
  const remove = (key: number) => {
    setRows((rs) => rs.filter((r) => r.key !== key));
    setDirty(true);
  };
  const insertAfter = (i: number) => {
    setRows((rs) => {
      const prevEnd = i >= 0 ? (parseTime(rs[i].endText) ?? 0) : 0;
      const nextStart = i + 1 < rs.length ? parseTime(rs[i + 1].startText) : null;
      const start = Math.round((prevEnd + 0.05) * 1000) / 1000;
      const end = nextStart !== null ? Math.max(start + 0.1, nextStart - 0.05) : start + 2;
      const row: Row = { key: nextKey++, start, end, text: "", startText: formatTime(start), endText: formatTime(end) };
      return [...rs.slice(0, i + 1), row, ...rs.slice(i + 1)];
    });
    setDirty(true);
  };
  const seek = (t: number) => {
    const el = mediaRef.current;
    if (el && Number.isFinite(t)) {
      el.currentTime = t;
      void el.play().catch(() => undefined);
    }
  };

  const save = async (): Promise<boolean> => {
    if (problems.length) return false;
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await saveSegments(result.job_id, cues);
      setRows(toRows(updated));
      setDirty(false);
      setSavedAt(new Date().toLocaleTimeString());
      onSaved(updated);
      return true;
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const download = async (fmt: "srt" | "vtt") => {
    setActionError(null);
    if (dirty && !(await save())) return;
    try {
      await downloadFile(result.job_id, fmt);
    } catch (e) {
      setActionError(`Download failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const removeJob = async () => {
    if (!window.confirm("Delete this job and its captions from the server? This cannot be undone.")) return;
    setActionError(null);
    try {
      await deleteJob(result.job_id);
      onDeleted();
    } catch (e) {
      setActionError(`Delete failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const MediaTag = result.has_video ? "video" : "audio";

  return (
    <section className="card" aria-labelledby="editor-title">
      <div className="row spread">
        <h2 id="editor-title">3. Review &amp; edit captions</h2>
        <div className="row tight">
          <button type="button" className="secondary" onClick={() => download("srt")} disabled={saving || problems.length > 0}>
            Download .srt
          </button>
          <button type="button" className="secondary" onClick={() => download("vtt")} disabled={saving || problems.length > 0}>
            Download .vtt
          </button>
          <button type="button" className="secondary danger" onClick={removeJob} disabled={saving}>
            Delete from server
          </button>
        </div>
      </div>

      <p className="muted small" data-testid="result-meta">
        {rows.length} captions · language {result.language ?? "?"}
        {result.language_probability != null ? ` (${Math.round(result.language_probability * 100)}% confidence)` : ""}
        {result.duration != null ? ` · ${formatTime(result.duration)}` : ""}
        {result.model ? ` · model ${result.model}` : ""}
        {result.edited ? " · edited" : ""}
      </p>

      {mediaUrl ? (
        <div className="player">
          <MediaTag
            ref={mediaRef}
            src={mediaUrl}
            controls
            onTimeUpdate={(e) => setCurrent(activeCueIndex(cues, e.currentTarget.currentTime))}
          >
            {vttUrl && <track key={vttUrl} kind="captions" src={vttUrl} srcLang={result.language ?? "en"} label="Captions" default />}
          </MediaTag>
          {MediaTag === "audio" && (
            <p className="now-playing" aria-live="polite">
              {current >= 0 ? cues[current].text : " "}
            </p>
          )}
        </div>
      ) : (
        <p className="muted small">
          Media preview is available only in the browser session that uploaded the file (the server does not keep uploads).
        </p>
      )}

      {rows.length === 0 ? (
        <div className="empty">
          <p>
            <strong>No speech was detected</strong> in this file, so there are no captions.
          </p>
          <p className="muted small">You can still add captions manually.</p>
          <button type="button" className="secondary" onClick={() => insertAfter(-1)}>
            Add caption
          </button>
        </div>
      ) : (
        <ol className="cues" data-testid="cue-list">
          {rows.map((r, i) => {
            const startBad = parseTime(r.startText) === null;
            const endBad = parseTime(r.endText) === null;
            return (
              <li key={r.key} className={i === current ? "active" : undefined}>
                <div className="cue-times">
                  <span className="cue-num">{i + 1}</span>
                  <input
                    aria-label={`Caption ${i + 1} start`}
                    className={startBad ? "invalid" : undefined}
                    value={r.startText}
                    onChange={(e) => update(r.key, { startText: e.target.value })}
                  />
                  <span aria-hidden>→</span>
                  <input
                    aria-label={`Caption ${i + 1} end`}
                    className={endBad ? "invalid" : undefined}
                    value={r.endText}
                    onChange={(e) => update(r.key, { endText: e.target.value })}
                  />
                </div>
                <textarea
                  aria-label={`Caption ${i + 1} text`}
                  rows={Math.max(1, r.text.split("\n").length)}
                  value={r.text}
                  onChange={(e) => update(r.key, { text: e.target.value })}
                />
                <div className="cue-actions">
                  {mediaUrl && (
                    <button type="button" className="icon" title="Play from here" onClick={() => seek(parseTime(r.startText) ?? NaN)}>
                      ▶
                    </button>
                  )}
                  <button type="button" className="icon" title="Insert caption after" onClick={() => insertAfter(i)}>
                    ＋
                  </button>
                  <button type="button" className="icon danger" title="Delete caption" onClick={() => remove(r.key)}>
                    ✕
                  </button>
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {problems.length > 0 && (
        <ul className="error" role="alert">
          {problems.slice(0, 5).map((p) => (
            <li key={p}>{p}</li>
          ))}
          {problems.length > 5 && <li>…and {problems.length - 5} more</li>}
        </ul>
      )}
      {actionError && (
        <p className="error" role="alert">
          {actionError}
        </p>
      )}
      {saveError && (
        <p className="error" role="alert">
          Could not save: {saveError}
        </p>
      )}

      <div className="row spread sticky-actions">
        <span className="muted small" data-testid="save-state">
          {saving ? "Saving…" : dirty ? "Unsaved changes" : savedAt ? `Saved at ${savedAt}` : "No changes"}
        </span>
        <button type="button" className="primary" onClick={save} disabled={!dirty || saving || problems.length > 0}>
          Save changes
        </button>
      </div>

      {result.transcript && (
        <details>
          <summary>Full transcript (original recognition)</summary>
          <p className="transcript">{result.transcript}</p>
        </details>
      )}
    </section>
  );
}
