import type { JobStatus, QaStatus, StageStatus } from "./types";

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "--:--";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return "never";
  const delta = Date.now() - new Date(value).getTime();
  if (delta < 60_000) return "just now";
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m ago`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h ago`;
  return `${Math.floor(delta / 86_400_000)}d ago`;
}

export function statusTone(status: JobStatus | StageStatus | QaStatus | string): "neutral" | "active" | "pass" | "warn" | "block" {
  if (["running", "starting", "cancel_requested"].includes(status)) return "active";
  if (["succeeded", "skipped_valid", "pass"].includes(status)) return "pass";
  if (["queued", "pending", "unknown", "interrupted", "orphaned", "warning", "warn"].includes(status)) return "warn";
  if (["failed", "cancelled", "blocked", "block", "error"].includes(status)) return "block";
  return "neutral";
}

export function titleCase(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
