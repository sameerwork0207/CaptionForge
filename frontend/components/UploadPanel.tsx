"use client";

import { useRef, useState } from "react";
import type { ServerConfig } from "@/lib/api";

interface Props {
  config: ServerConfig | null;
  busy: boolean;
  uploadProgress: number | null;
  onSubmit: (file: File, language: string) => void;
  onCancelUpload: () => void;
}

const LANGUAGE_NAMES: Record<string, string> = {
  en: "English", es: "Spanish", fr: "French", de: "German", it: "Italian", pt: "Portuguese",
  nl: "Dutch", hi: "Hindi", ja: "Japanese", zh: "Chinese", ko: "Korean", ru: "Russian",
  ar: "Arabic", tr: "Turkish", pl: "Polish", uk: "Ukrainian", sv: "Swedish", id: "Indonesian",
};

function formatBytes(n: number): string {
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function validateFile(file: File, config: ServerConfig | null): string | null {
  const dot = file.name.lastIndexOf(".");
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (config && !config.supported_extensions.includes(ext)) {
    return `"${ext || file.name}" files are not supported. Use one of: ${config.supported_extensions.join(", ")}.`;
  }
  if (file.size === 0) return "This file is empty.";
  if (config && file.size > config.max_upload_mb * 1024 * 1024) {
    return `This file is ${formatBytes(file.size)}; the maximum is ${config.max_upload_mb} MB.`;
  }
  return null;
}

export default function UploadPanel({ config, busy, uploadProgress, onSubmit, onCancelUpload }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [language, setLanguage] = useState("auto");
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const choose = (f: File | undefined | null) => {
    if (!f) return;
    const problem = validateFile(f, config);
    setError(problem);
    setFile(problem ? null : f);
  };

  const languages = config?.languages ?? [];
  const sortedLanguages = [
    ...languages.filter((l) => LANGUAGE_NAMES[l]).sort((a, b) => LANGUAGE_NAMES[a].localeCompare(LANGUAGE_NAMES[b])),
    ...languages.filter((l) => !LANGUAGE_NAMES[l]),
  ];

  return (
    <section className="card" aria-labelledby="upload-title">
      <h2 id="upload-title">1. Upload media</h2>

      <div
        className={`dropzone${dragging ? " dragging" : ""}${busy ? " disabled" : ""}`}
        role="button"
        tabIndex={0}
        aria-disabled={busy}
        onClick={() => !busy && inputRef.current?.click()}
        onKeyDown={(e) => {
          if (!busy && (e.key === "Enter" || e.key === " ")) inputRef.current?.click();
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (!busy) choose(e.dataTransfer.files?.[0]);
        }}
      >
        <input
          ref={inputRef}
          type="file"
          data-testid="file-input"
          hidden
          accept={config?.supported_extensions.join(",") ?? "audio/*,video/*"}
          onChange={(e) => {
            choose(e.target.files?.[0]);
            e.target.value = "";
          }}
        />
        {file ? (
          <p>
            <strong>{file.name}</strong> <span className="muted">({formatBytes(file.size)})</span>
          </p>
        ) : (
          <p>
            Drop an audio or video file here, or <span className="link">browse</span>
          </p>
        )}
        <p className="muted small">
          {config
            ? `Supported: ${config.supported_extensions.join(" ")} · up to ${config.max_upload_mb} MB · up to ${Math.round(config.max_duration_seconds / 60)} min`
            : "Loading server limits…"}
        </p>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div className="row">
        <label className="field">
          <span>Spoken language</span>
          <select value={language} onChange={(e) => setLanguage(e.target.value)} disabled={busy}>
            <option value="auto">Detect automatically</option>
            {sortedLanguages.map((l) => (
              <option key={l} value={l}>
                {LANGUAGE_NAMES[l] ? `${LANGUAGE_NAMES[l]} (${l})` : l}
              </option>
            ))}
          </select>
        </label>

        {uploadProgress !== null ? (
          <div className="grow">
            <div className="progress" aria-label="Upload progress">
              <div style={{ width: `${Math.round(uploadProgress * 100)}%` }} />
            </div>
            <div className="row tight">
              <span className="muted small">Uploading… {Math.round(uploadProgress * 100)}%</span>
              <button type="button" className="secondary" onClick={onCancelUpload}>
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            className="primary"
            disabled={!file || busy || !config}
            onClick={() => file && onSubmit(file, language)}
          >
            Generate captions
          </button>
        )}
      </div>
    </section>
  );
}
