import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { renderUi } from "../test/render";
import { RunsPage } from "./RunsPage";

describe("RunsPage", () => {
  it("renders runtime state and discovered season runs", async () => {
    vi.spyOn(api, "health").mockResolvedValue({ status: "pass", active_job_id: "job-1", queue_depth: 2, checks: [] });
    vi.spyOn(api, "jobs").mockResolvedValue([{ id: "job-1", kind: "series", title: "Solo Leveling S01", run_dir: "solo-leveling-s01", status: "running", current_stage: "series_composer", created_at: "2026-07-24T00:00:00Z" }]);
    vi.spyOn(api, "runs").mockResolvedValue([{ id: "run-1", kind: "series", title: "Solo Leveling S01", run_dir: "solo-leveling-s01", status: "running", delivery_status: "warn", episode_count: 12, current_stage: "series_composer", updated_at: new Date().toISOString() }]);
    renderUi(<RunsPage />);
    expect(await screen.findByRole("heading", { name: "Solo Leveling S01" })).toBeInTheDocument();
    expect(screen.getByText("12 eps", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("0 waiting")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /new run/i })).toHaveAttribute("href", "/runs/new");
  });
});
