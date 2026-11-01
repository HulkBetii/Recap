import { expect, test, type Page, type Route } from "@playwright/test";

const now = "2026-07-24T10:00:00Z";

type FixtureKind = "series" | "single";

function fixture(kind: FixtureKind = "series") {
  const series = kind === "series";
  const jobId = `job-${kind}`;
  const runId = `run-${kind}`;
  const artifactRunId = "run-artifact";
  const title = series ? "Solo Leveling | Season 1" : "Sample movie";
  const episodes = series
    ? [{ episode_key: "s01e01", title: "Episode 1", episode_number: 1 }, { episode_key: "s01e02", title: "Episode 2", episode_number: 2 }]
    : [];
  const job = { id: jobId, plan_id: "plan-1", kind, status: "succeeded", run_dir: `D:\\runs\\${runId}`, display_title: title, attempt: 1, heartbeat_at: now, exit_code: 0, created_at: now, finished_at: now };
  const stages = series
    ? [{ stage_key: "series_composer", episode_key: "", attempt: 1, status: "succeeded" }, { stage_key: "tts", episode_key: "", attempt: 1, status: "succeeded" }, { stage_key: "render", episode_key: "", attempt: 1, status: "succeeded" }]
    : [{ stage_key: "render", episode_key: "", attempt: 1, status: "succeeded" }];
  const qa = { run_id: runId, kind, status: "warn", checks: [{ code: "minimum_duration", label: "Minimum duration", status: "pass", message: "Render duration meets the minimum", details: {} }, { code: "planning_reference", label: "Planning reference", status: "warn", message: "Render duration exceeds planning reference but remains deliverable", details: {} }, { code: "render_format", label: "Render format", status: "pass", message: "1920x1080 at 30fps", details: {} }], metrics: { composer_estimated_duration_s: 2650, tts_duration_s: 3358, edl_duration_s: 3359, render_duration_s: 3359, minimum_duration_s: 2100, maximum_duration_s: 2700, hard_cap_s: 3000 } };
  const artifacts = [
    { id: "video-1", run_id: runId, relative_path: "series_recap/series_recap.mp4", name: "series_recap.mp4", kind: "video", size: 1024, modified_at: now, media_type: "video/mp4" },
    { id: "log-1", run_id: runId, relative_path: "series_recap/series_recap.log", name: "series_recap.log", kind: "log", size: 512, modified_at: now, media_type: "text/plain" },
    { id: "edl-1", run_id: runId, relative_path: "series_recap/edl.json", name: "edl.json", kind: "json", size: 128, modified_at: now, media_type: "application/json" },
  ];
  const managed = { id: runId, job_id: jobId, kind, name: runId, display_title: title, path_token: "run-token", episode_keys: series ? ["s01e01", "s01e02"] : [], output_artifact_id: "video-1", execution_status: "succeeded", delivery_status: "warn", management_mode: "managed", available_actions: ["view", "resume", "rerun", "cancel"], modified_at: now };
  const imported = { id: artifactRunId, job_id: null, kind: "series", name: "old-season-artifacts", display_title: "Imported Season Archive", path_token: "artifact-token", episode_keys: ["s01e01"], output_artifact_id: "video-1", execution_status: "succeeded", delivery_status: "pass", management_mode: "artifact_only", available_actions: ["view"], read_only_reason: "Registered artifact folder has no managed job.", modified_at: now };
  return { jobId, runId, job, stages, qa, artifacts, episodes, managed, imported, title, series };
}

async function mockApi(page: Page, kind: FixtureKind = "series", capture: Record<string, unknown> = {}) {
  const data = fixture(kind);
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith("/events/stream")) {
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: ": keepalive\n\n" });
      return;
    }
    if (request.method() === "POST") {
      if (path === "/api/inspect/series") {
        capture.inspect = JSON.parse(request.postData() ?? "{}");
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ kind: "series", manifest_token: "manifest-token", manifest_name: "series_manifest.yaml", series_id: "solo-leveling-s01", display_title: "Solo Leveling | Season 1", suggested_run_name: "solo-leveling-s01", episodes: [{ episode_key: "s01e01", episode_number: 1, title: "Episode 1", arc: "Awakening", source_available: true }, { episode_key: "s01e02", episode_number: 2, title: "Episode 2", arc: "Dungeon", source_available: true }], missing_source_count: 0, duplicate_source_count: 0, total_duration_s: 5100, arc_preview: ["Awakening", "Dungeon"] }) });
        return;
      }
      if (path === "/api/inspect/single") {
        capture.inspect = JSON.parse(request.postData() ?? "{}");
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ kind: "single", source_token: "source-token", source_name: "sample.mp4", display_title: "Sample movie", suggested_run_name: "sample-movie", media_valid: true, duration_s: 3600 }) });
        return;
      }
      if (path === "/api/preflight") {
        capture.preflight = JSON.parse(request.postData() ?? "{}");
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ can_start: true, checks: [{ code: "ffmpeg", label: "ffmpeg", status: "pass", message: "Ready" }], warnings: [], plan_id: "plan-1" }) });
        return;
      }
      if (path === "/api/plans/series" || path === "/api/plans/single") {
        capture.plan = JSON.parse(request.postData() ?? "{}");
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ plan_id: "plan-1", kind: path.endsWith("series") ? "series" : "single", command: ["python", "-m", "series_recap"], run_dir: "D:\\runs\\solo-leveling-s01", dag: [{ key: "s01e01:storymap", label: "s01e01 storymap", status: "ready" }, { key: "s01e02:storymap", label: "s01e02 storymap", status: "ready" }, { key: "series_composer", label: "Series composer", status: "ready" }, { key: "render", label: "Render", status: "ready" }], checks: [], output_paths: ["series_recap/series_recap.mp4"], warnings: [], can_start: true, dry_run_output: "dry-run ok" }) });
        return;
      }
      if (path === "/api/jobs") {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...data.job, status: "queued" }) });
        return;
      }
      if (path.endsWith("/rerun")) {
        capture.rerun = JSON.parse(request.postData() ?? "{}");
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...data.job, id: "job-rerun", status: "queued", attempt: 2 }) });
        return;
      }
      if (path === "/api/runs/register") {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data.imported) });
        return;
      }
      if (path === "/api/health/chatgpt-profile") {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "pass", composer_available: true, sent: false }) });
        return;
      }
    }
    let body: unknown;
    if (path === "/api/health") body = { status: "pass", runtime: [{ code: "ffmpeg", status: "pass", message: "ffmpeg is available", details: {} }], providers: { vivoo: true }, active_job: null };
    else if (path === "/api/session") body = { token: "test-token" };
    else if (path === "/api/meta") body = { host: "127.0.0.1", port: 8765, locale: "vi" };
    else if (path === "/api/presets") body = [{ id: "series-production", name: "Production", token: "preset-token", kind: "series", description: "Production preset", summary: { series: { arc_size: 3, target_total_min_s: 2100, target_total_max_s: 2700, target_total_hard_cap_s: 3000 } } }];
    else if (path === "/api/fs/roots") body = [{ id: "workspace", label: "Workspace", token: "workspace-token", path: "D:\\VibeCoding\\Recap" }];
    else if (path === "/api/fs/listing") body = { current: { root_id: "workspace", token: "workspace-token", name: "Recap", at_root: true }, entries: [{ token: "manifest-token", name: "series_manifest.yaml", suffix: ".yaml", kind: "file", size: 128 }, { token: "source-token", name: "sample.mp4", suffix: ".mp4", kind: "file", size: 1024 }, { token: "output-token", name: "runs", suffix: "", kind: "directory", size: 0 }] };
    else if (path === "/api/jobs") body = [data.job];
    else if (path === `/api/jobs/${data.jobId}`) body = { job: data.job, stages: data.stages };
    else if (path === `/api/jobs/${data.jobId}/events`) body = [{ seq: 1, timestamp: now, event_type: "log", level: "info", stage: "render", message: "render complete", payload: { source: "pipeline.log" } }];
    else if (path === "/api/runs") body = [data.managed, data.imported];
    else if (path === `/api/runs/${data.runId}`) body = data.managed;
    else if (path === `/api/runs/${data.imported.id}`) body = data.imported;
    else if (path === `/api/runs/${data.runId}/episodes`) body = data.episodes.map((episode) => ({ ...episode, stage_statuses: { storymap: "succeeded", episode_planner: "succeeded", shots: "succeeded" }, translation_ratio: .99, approximate_timecodes: false }));
    else if (path === `/api/runs/${data.imported.id}/episodes`) body = [{ episode_key: "s01e01", title: "Episode 1", episode_number: 1, stage_statuses: { storymap: "succeeded" }, translation_ratio: .99, approximate_timecodes: false }];
    else if (path === `/api/runs/${data.runId}/artifacts` || path === `/api/runs/${data.imported.id}/artifacts`) body = data.artifacts;
    else if (path === `/api/runs/${data.runId}/qa` || path === `/api/runs/${data.imported.id}/qa`) body = { ...data.qa, run_id: path.includes(data.imported.id) ? data.imported.id : data.runId, kind: path.includes(data.imported.id) ? "series" : kind };
    else if (path.endsWith("/artifacts/edl-1")) body = [{ tl_start: 0, tl_end: 4, src: "s01e01/source.mp4" }];
    else body = undefined;
    if (body === undefined) {
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: `No mock for ${path}` }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  return data;
}

test("Vietnamese default, managed/imported runs, QA and artifact-only read-only detail", async ({ page }) => {
  await mockApi(page);
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "Các lượt chạy" })).toBeVisible();
  await expect(page.getByText("Run do worker quản lý")).toBeVisible();
  await expect(page.getByText("Run artifact đã import")).toBeVisible();
  await expect(page.getByRole("button", { name: "EN" })).toBeVisible();
  await page.getByRole("button", { name: "EN" }).click();
  await expect(page.getByRole("heading", { name: "Pipeline runs", exact: true })).toBeVisible();
  await page.goto("/runs/run-artifact");
  await expect(page.getByText("Read only").first()).toBeVisible();
  await expect(page.getByText("Validated artifacts, without worker telemetry")).toBeVisible();
  await expect(page.getByRole("button", { name: "Resume" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Rerun" })).toHaveCount(0);
  await expect(page.getByText("Imported Season Archive")).toBeVisible();
});

test("new-run wizard inspects source, keeps display title separate from slug, and sends no basic overrides", async ({ page }) => {
  const capture: Record<string, unknown> = {};
  await mockApi(page, "series", capture);
  await page.goto("/runs/new");
  await page.getByRole("button", { name: "Duyệt" }).first().click();
  await page.locator("#allowed-root").selectOption("workspace");
  await page.getByRole("button", { name: /Chọn$/ }).filter({ hasText: "Chọn" }).first().click();
  await page.getByRole("button", { name: "Duyệt" }).last().click();
  await page.locator("#allowed-root").selectOption("workspace");
  await page.getByRole("button", { name: "Chọn thư mục hiện tại" }).click();
  await expect(page.locator("#display-title")).toHaveValue("Solo Leveling | Season 1");
  await expect(page.locator("#run-name")).toHaveValue("solo-leveling-s01");
  await page.getByRole("button", { name: "Tiếp tục" }).click();
  await page.getByLabel("Preset production").selectOption("preset-token");
  await page.getByRole("button", { name: "Tiếp tục" }).click();
  await expect(page.getByText("ffmpeg")).toBeVisible();
  expect((capture.preflight as Record<string, unknown>).display_title).toBe("Solo Leveling | Season 1");
  expect((capture.preflight as Record<string, unknown>).run_name).toBe("solo-leveling-s01");
  expect((capture.preflight as Record<string, unknown>).overrides).toEqual({});
  await page.getByRole("button", { name: "Tạo dry-run" }).click();
  await expect(page.getByText("Theo tập")).toBeVisible();
  await expect(page.getByText("Chuỗi tổng hợp cuối")).toBeVisible();
  expect((capture.plan as Record<string, unknown>).display_title).toBe("Solo Leveling | Season 1");
});

test("managed season detail exposes log filters, reconnect, and explicit final/all rerun selector", async ({ page }) => {
  const capture: Record<string, unknown> = {};
  await mockApi(page, "series", capture);
  await page.goto("/runs/run-series");
  await page.getByRole("tab", { name: "Logs realtime" }).click();
  await expect(page.getByText("render complete")).toBeVisible();
  await expect(page.getByText("Đã hoàn tất")).toBeVisible();
  await expect(page.getByRole("button", { name: "Đang bám cuối" })).toBeVisible();
  await page.getByRole("button", { name: "Đang bám cuối" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Bám log mới nhất" })).toBeVisible();
  await page.getByRole("button", { name: "Kết nối lại" }).focus();
  await page.keyboard.press("Enter");
  const rerun = page.getByRole("combobox", { name: "Phạm vi chạy lại" });
  await expect(rerun).toHaveValue("final");
  await rerun.selectOption("all");
  await page.getByRole("button", { name: "Chạy lại" }).click();
  await expect(page.getByText("Force chạy lại toàn bộ mùa?")).toBeVisible();
  await expect(page.getByRole("button", { name: "Force chạy lại toàn mùa" })).toBeVisible();
});

test("mobile navigation stays usable and single rerun opens a stage selector", async ({ page }) => {
  const capture: Record<string, unknown> = {};
  await mockApi(page, "single", capture);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/runs");
  await expect(page.getByRole("link", { name: "Tạo lượt chạy" }).last()).toBeVisible();
  await page.getByRole("link", { name: "Tạo lượt chạy" }).last().click();
  await expect(page.getByRole("heading", { name: "Tạo lượt chạy recap" })).toBeVisible();
  await page.goto("/runs/run-single");
  const rerun = page.getByRole("combobox", { name: "Stage chạy lại" });
  await rerun.selectOption("review");
  await page.getByRole("button", { name: "Chạy lại" }).click();
  await expect(page.getByText("Chạy lại Review?")).toBeVisible();
  await expect(page.getByRole("dialog").getByText(/Review.*Tts.*Shots.*Match.*Render/)).toBeVisible();
});
