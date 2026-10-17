// Typed client for the CaptionForge FastAPI backend.

export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(
  /\/$/,
  "",
);

export type JobStatus = "pending" | "processing" | "done" | "error";

export interface JobError {
  code: string;
  message: string;
}

export interface CaptionSegment {
  index: number;
  start: number;
  end: number;
  text: string;
}

export interface StatusResponse {
  job_id: string;
  status: JobStatus;
  stage: string | null;
  progress: number;
  queue_position: number | null;
  filename: string | null;
  created_at: string | null;
  error: JobError | null;
}

export interface ResultResponse extends Omit<StatusResponse, "queue_position"> {
  finished_at: string | null;
  language: string | null;
  language_probability: number | null;
  duration: number | null;
  has_video: boolean | null;
  transcript: string | null;
  segments: CaptionSegment[] | null;
  srt_content: string | null;
  edited: boolean;
  model: string | null;
}

export interface ServerConfig {
  supported_extensions: string[];
  max_upload_mb: number;
  max_duration_seconds: number;
  languages: string[];
  export_formats: string[];
  model: string;
}

export interface Health {
  status: string;
  ffmpeg: boolean;
  model: { name: string; device: string; active_device: string | null; loaded: boolean };
}

/** Error carrying the backend's `detail` message and machine-readable `code`. */
export class ApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function parseError(res: Response): Promise<ApiError> {
  let detail = `Request failed (HTTP ${res.status}).`;
  let code = "http_error";
  try {
    const body = await res.json();
    if (typeof body.detail === "string") detail = body.detail;
    if (typeof body.code === "string") code = body.code;
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(detail, res.status, code);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { cache: "no-store", ...init });
  } catch {
    throw new ApiError(
      `Cannot reach the CaptionForge server at ${API_BASE}. Is the backend running?`,
      0,
      "network_error",
    );
  }
  if (!res.ok) throw await parseError(res);
  return (res.status === 204 ? undefined : await res.json()) as T;
}

export const getHealth = () => request<Health>("/api/health");
export const getConfig = () => request<ServerConfig>("/api/config");
export const getStatus = (id: string) => request<StatusResponse>(`/api/status/${id}`);
export const getResult = (id: string) => request<ResultResponse>(`/api/result/${id}`);
export const deleteJob = (id: string) => request<void>(`/api/jobs/${id}`, { method: "DELETE" });

export const saveSegments = (id: string, segments: { start: number; end: number; text: string }[]) =>
  request<ResultResponse>(`/api/result/${id}/segments`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ segments }),
  });

export const downloadUrl = (id: string, format: "srt" | "vtt") =>
  `${API_BASE}/api/download/${id}?format=${format}`;

/** Filename from a Content-Disposition header (plain or RFC 5987 form). */
export function dispositionFilename(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const star = /filename\*=utf-8''([^;]+)/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1]);
    } catch {
      /* fall through */
    }
  }
  const plain = /filename="([^"]+)"/i.exec(header);
  return plain ? plain[1] : fallback;
}

/**
 * Download a subtitle file. Fetching first (instead of navigating to the URL)
 * means a server error is shown in the page rather than replacing the app
 * with a raw JSON error.
 */
export async function downloadFile(id: string, format: "srt" | "vtt"): Promise<void> {
  let res: Response;
  try {
    res = await fetch(downloadUrl(id, format), { cache: "no-store" });
  } catch {
    throw new ApiError(`Cannot reach the CaptionForge server at ${API_BASE}.`, 0, "network_error");
  }
  if (!res.ok) throw await parseError(res);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = dispositionFilename(res.headers.get("Content-Disposition"), `captions.${format}`);
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/**
 * Upload with progress reporting (fetch cannot report upload progress).
 * Resolves with the new job id.
 */
export function uploadFile(
  file: File,
  language: string,
  onProgress: (fraction: number) => void,
): { promise: Promise<string>; abort: () => void } {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<string>((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    if (language && language !== "auto") form.append("language", language);
    xhr.open("POST", `${API_BASE}/api/transcribe`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body: { job_id?: string; detail?: string; code?: string } = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status === 202 && body.job_id) resolve(body.job_id);
      else
        reject(
          new ApiError(
            body.detail || `Upload failed (HTTP ${xhr.status}).`,
            xhr.status,
            body.code || "upload_failed",
          ),
        );
    };
    xhr.onerror = () =>
      reject(
        new ApiError(
          `Cannot reach the CaptionForge server at ${API_BASE}. Is the backend running?`,
          0,
          "network_error",
        ),
      );
    xhr.onabort = () => reject(new ApiError("Upload cancelled.", 0, "aborted"));
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}
