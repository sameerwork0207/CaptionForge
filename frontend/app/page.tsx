"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import CaptionEditor from "@/components/CaptionEditor";
import JobProgress from "@/components/JobProgress";
import UploadPanel from "@/components/UploadPanel";
import {
  API_BASE,
  ApiError,
  deleteJob,
  getConfig,
  getHealth,
  getResult,
  getStatus,
  uploadFile,
  type Health,
  type ResultResponse,
  type ServerConfig,
  type StatusResponse,
} from "@/lib/api";

type Phase = "idle" | "uploading" | "processing" | "done" | "error";

const POLL_MS = 1000;

function setJobInUrl(jobId: string | null) {
  const url = new URL(window.location.href);
  if (jobId) url.searchParams.set("job", jobId);
  else url.searchParams.delete("job");
  window.history.replaceState(null, "", url);
}

export default function Home() {
  const [config, setConfig] = useState<ServerConfig | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);

  const [phase, setPhase] = useState<Phase>("idle");
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [result, setResult] = useState<ResultResponse | null>(null);
  const [error, setError] = useState<{ message: string; code: string } | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [mediaUrl, setMediaUrl] = useState<string | null>(null);
  const abortUpload = useRef<(() => void) | null>(null);

  // Server capabilities + health.
  const loadServer = useCallback(async () => {
    try {
      const [cfg, h] = await Promise.all([getConfig(), getHealth()]);
      setConfig(cfg);
      setHealth(h);
      setServerError(null);
    } catch (e) {
      setServerError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    void loadServer();
  }, [loadServer]);

  // Resume a job from ?job=<id> after a page reload.
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("job");
    if (id) {
      setJobId(id);
      setPhase("processing");
    }
  }, []);

  // Poll status while processing.
  useEffect(() => {
    if (phase !== "processing" || !jobId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const st = await getStatus(jobId);
        if (stopped) return;
        setStatus(st);
        setError(null); // connection is fine again
        if (st.status === "done") {
          setResult(await getResult(jobId));
          setPhase("done");
          return;
        }
        if (st.status === "error") {
          setError(st.error ?? { code: "unknown", message: "Processing failed." });
          setPhase("error");
          return;
        }
      } catch (e) {
        if (stopped) return;
        if (e instanceof ApiError && e.status === 404) {
          setError({ code: "not_found", message: "This job no longer exists on the server (it may have expired or been deleted)." });
          setPhase("error");
          setJobInUrl(null);
          return;
        }
        // Transient network problem: keep polling but tell the user.
        setError({ code: "network_error", message: e instanceof Error ? e.message : String(e) });
      }
      timer = setTimeout(tick, POLL_MS);
    };
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [phase, jobId]);

  useEffect(() => () => {
    if (mediaUrl) URL.revokeObjectURL(mediaUrl);
  }, [mediaUrl]);

  const start = async (file: File, language: string) => {
    setError(null);
    setResult(null);
    setStatus(null);
    setPhase("uploading");
    setUploadProgress(0);
    setMediaUrl(URL.createObjectURL(file));
    const { promise, abort } = uploadFile(file, language, setUploadProgress);
    abortUpload.current = abort;
    try {
      const id = await promise;
      setJobId(id);
      setJobInUrl(id);
      setStatus({ job_id: id, status: "pending", stage: "queued", progress: 0, queue_position: null, filename: file.name, created_at: null, error: null });
      setPhase("processing");
    } catch (e) {
      const err = e instanceof ApiError ? e : new ApiError(String(e), 0, "unknown");
      if (err.code === "aborted") setPhase("idle");
      else {
        setError({ code: err.code, message: err.message });
        setPhase("error");
      }
      setMediaUrl(null);
    } finally {
      setUploadProgress(null);
      abortUpload.current = null;
    }
  };

  const reset = () => {
    setPhase("idle");
    setJobId(null);
    setStatus(null);
    setResult(null);
    setError(null);
    setMediaUrl(null);
    setJobInUrl(null);
  };

  const cancelJob = async () => {
    if (jobId) {
      try {
        await deleteJob(jobId);
      } catch {
        /* already gone */
      }
    }
    reset();
  };

  return (
    <main className="container">
      <header className="header">
        <h1>CaptionForge</h1>
        <p className="muted">Automatic captions for audio and video: upload, review, edit and export SRT or WebVTT.</p>
      </header>

      {serverError && (
        <div className="banner error" role="alert">
          <span>{serverError}</span>
          <button type="button" className="secondary" onClick={loadServer}>
            Retry
          </button>
        </div>
      )}
      {health && !health.ffmpeg && (
        <div className="banner error" role="alert">
          FFmpeg is not installed on the server, so uploads will fail. See the README for setup.
        </div>
      )}

      {(phase === "idle" || phase === "uploading") && (
        <UploadPanel
          config={config}
          busy={phase === "uploading"}
          uploadProgress={uploadProgress}
          onSubmit={start}
          onCancelUpload={() => abortUpload.current?.()}
        />
      )}

      {phase === "processing" && status && <JobProgress status={status} onCancel={cancelJob} />}
      {phase === "processing" && !status && (
        <section className="card">
          <p>Loading job…</p>
        </section>
      )}
      {phase === "processing" && error?.code === "network_error" && (
        <p className="error small" role="alert">
          {error.message} Retrying…
        </p>
      )}

      {phase === "error" && error && (
        <section className="card" role="alert" aria-labelledby="error-title">
          <h2 id="error-title">Something went wrong</h2>
          <p className="error" data-testid="error-message">
            {error.message}
          </p>
          <p className="muted small">Error code: {error.code}</p>
          <button type="button" className="primary" onClick={reset}>
            Try another file
          </button>
        </section>
      )}

      {phase === "done" && result && (
        <>
          <CaptionEditor key={result.job_id} result={result} mediaUrl={mediaUrl} onSaved={setResult} onDeleted={reset} />
          <div className="row">
            <button type="button" className="secondary" onClick={reset}>
              Caption another file
            </button>
          </div>
        </>
      )}

      <footer className="footer muted small">
        Backend: {API_BASE}
        {health ? ` · model ${health.model.name} on ${health.model.active_device ?? health.model.device}` : ""}
        {" · "}Speech recognition is automatic and can contain mistakes. Please review captions before publishing.
      </footer>
    </main>
  );
}
