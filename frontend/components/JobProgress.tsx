"use client";

import type { StatusResponse } from "@/lib/api";

const STAGE_LABELS: Record<string, string> = {
  queued: "Waiting in queue",
  extracting: "Extracting audio",
  transcribing: "Recognising speech",
  segmenting: "Building captions",
  exporting: "Writing subtitle files",
};

export default function JobProgress({
  status,
  onCancel,
}: {
  status: StatusResponse;
  onCancel: () => void;
}) {
  const stage = status.stage ?? (status.status === "pending" ? "queued" : "transcribing");
  const pct = Math.round((status.progress ?? 0) * 100);
  const indeterminate = stage !== "transcribing" || pct === 0;

  return (
    <section className="card" aria-labelledby="progress-title" aria-live="polite">
      <h2 id="progress-title">2. Processing</h2>
      <p>
        <strong>{status.filename}</strong>
      </p>
      <p data-testid="stage">
        {STAGE_LABELS[stage] ?? stage}
        {status.queue_position ? ` (position ${status.queue_position})` : ""}
        {stage === "transcribing" && pct > 0 ? ` — ${pct}%` : "…"}
      </p>
      <div className={`progress${indeterminate ? " indeterminate" : ""}`}>
        <div style={{ width: indeterminate ? "30%" : `${pct}%` }} />
      </div>
      <p className="muted small">
        The first job after the server starts also loads the speech model, which can take a while.
        Progress is measured by how far into the audio recognition has reached.
      </p>
      <button type="button" className="secondary" onClick={onCancel}>
        Cancel job
      </button>
    </section>
  );
}
