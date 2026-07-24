import { expect, test, type Page } from "@playwright/test";

const now = "2026-07-24T10:00:00Z";
const apiData: Record<string, unknown> = {
  "/api/health": { status: "pass", runtime: [{ code: "ffmpeg", status: "pass", message: "ffmpeg is available", details: {} }], providers: { vivoo: true }, active_job: null },
  "/api/jobs": [{ id: "job-1", plan_id: "plan-1", kind: "series", status: "succeeded", argv: [], command_hash: "hash", run_dir: "D:\\runs\\solo-leveling-s01", config_path: "config.yaml", config_snapshot: {}, attempt: 1, cancel_requested: false, created_at: now, finished_at: now }],
  "/api/jobs/job-1": { job: { id: "job-1", plan_id: "plan-1", kind: "series", status: "succeeded", argv: [], command_hash: "hash", run_dir: "D:\\runs\\solo-leveling-s01", config_path: "config.yaml", config_snapshot: {}, attempt: 1, cancel_requested: false, created_at: now, finished_at: now }, stages: [{ job_id: "job-1", stage_key: "render", episode_key: "", attempt: 1, status: "succeeded" }] },
  "/api/jobs/job-1/events?after_seq=0": [],
  "/api/runs": [{ id: "run-1", kind: "series", name: "solo-leveling-s01", path_token: "run-token", episode_keys: ["s01e01", "s01e02"], output_artifact_id: "video-1", execution_status: "succeeded", delivery_status: "warn", modified_at: now }],
  "/api/runs/run-1": { id: "run-1", kind: "series", name: "solo-leveling-s01", path_token: "run-token", episode_keys: ["s01e01", "s01e02"], output_artifact_id: "video-1", execution_status: "succeeded", delivery_status: "warn", modified_at: now },
  "/api/runs/run-1/episodes": [{ episode_key: "s01e01", title: "Episode 1", episode_number: 1, stage_statuses: { storymap: "succeeded", episode_planner: "succeeded", shots: "succeeded" }, translation_ratio: .99, approximate_timecodes: false }, { episode_key: "s01e02", title: "Episode 2", episode_number: 2, stage_statuses: { storymap: "succeeded", episode_planner: "succeeded", shots: "succeeded" }, translation_ratio: .98, approximate_timecodes: false }],
  "/api/runs/run-1/artifacts": [{ id: "video-1", run_id: "run-1", relative_path: "series_recap/series_recap.mp4", name: "series_recap.mp4", kind: "video", size: 1024, modified_at: now, media_type: "video/mp4", previewable: true }],
  "/api/runs/run-1/qa": { run_id: "run-1", kind: "series", status: "warn", checks: [{ code: "minimum_duration", label: "Minimum duration", status: "pass", message: "Render duration meets the 2100 second minimum", details: {} }, { code: "planning_reference", label: "Planning reference", status: "warn", message: "Render duration exceeds the planning reference but remains deliverable", details: {} }, { code: "render_format", label: "Render format", status: "pass", message: "1920x1080 at 30fps", details: {} }], metrics: { composer_estimated_duration_s: 2650, tts_duration_s: 3358, edl_duration_s: 3359, render_duration_s: 3359, minimum_duration_s: 2100, maximum_duration_s: 2700, hard_cap_s: 3000 } },
};

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/events/stream")) { await route.fulfill({ status: 200, contentType: "text/event-stream", body: ": keepalive\n\n" }); return; }
    const key = `${url.pathname}${url.search}`;
    const body = apiData[key] ?? apiData[url.pathname];
    if (body === undefined) { await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: `No mock for ${key}` }) }); return; }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}

test("season control room exposes QA and episode matrix", async ({ page }) => {
  await mockApi(page);
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "Pipeline runs" })).toBeVisible();
  await expect(page.getByText("solo-leveling-s01").first()).toBeVisible();
  await page.getByText("solo-leveling-s01").first().click();
  await expect(page.getByRole("heading", { name: "solo-leveling-s01" })).toBeVisible();
  await expect(page.getByText("Episode 1")).toBeVisible();
  await expect(page.getByText("Minimum duration")).toBeVisible();
  await expect(page.getByText("Planning reference").first()).toBeVisible();
  await expect(page.getByText("Preferred maximum")).toBeVisible();
  await expect(page.getByText("55:59").first()).toBeVisible();
});

test("mobile navigation keeps monitoring routes available", async ({ page }) => {
  await mockApi(page);
  await page.goto("/runs");
  await expect(page.getByRole("link", { name: "New Run" }).last()).toBeVisible();
  await page.getByRole("link", { name: "Settings" }).last().click();
  await expect(page.getByRole("heading", { name: "Settings & health" })).toBeVisible();
});
