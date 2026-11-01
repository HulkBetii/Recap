import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { renderUi } from "../test/render";
import { buildPayload, defaultOverrides, NewRunPage, presetOverrides, safeConfigValid, type WizardState } from "./NewRunPage";

describe("NewRunPage", () => {
  it("sends only dirty overrides and converts season minutes to seconds", () => {
    const state: WizardState = {
      kind: "series", displayTitle: "Season one", runName: "season-one", displayTitleTouched: true, runNameTouched: true,
      sourceToken: "manifest", sourceName: "series_manifest.yaml", presetToken: "preset", outputParentToken: "runs", outputParentName: "runs",
      episodes: "1-12", overrides: { ...defaultOverrides, seriesMin: 36 }, dirty: { seriesMin: true },
    };
    expect(buildPayload(state)).toMatchObject({ display_title: "Season one", run_name: "season-one", run_parent_token: "runs", overrides: { series_recap: { target_total_min_s: 2160 } } });
    expect(buildPayload(state)).not.toHaveProperty("overrides.render");
  });

  it("supports an explicit local VieNeu override without changing the preset by default", () => {
    const state: WizardState = {
      kind: "single", displayTitle: "Episode", runName: "episode", displayTitleTouched: true, runNameTouched: true,
      sourceToken: "source", sourceName: "episode.mp4", presetToken: "preset", outputParentToken: "runs", outputParentName: "runs",
      episodes: "1", overrides: { ...defaultOverrides, providerMode: "vieneu", voiceId: "Ngọc Linh", vieneuStyle: "doc_truyen" },
      dirty: { providerMode: true, voiceId: true, vieneuStyle: true },
    };
    expect(buildPayload(state).overrides).toMatchObject({ tts: { provider_mode: "vieneu", voice_id: "Ngọc Linh", vieneu_style: "doc_truyen" } });
  });

  it("hydrates the configured VieNeu voice and blocks an empty local voice", () => {
    const preset = { id: "vieneu", name: "VieNeu", path: "vieneu", token: "preset", kind: "series" as const, summary: { tts: { provider_mode: "vieneu", voice_id: "Ngọc Linh", vieneu_style: "doc_truyen", speed: 0.9, pronunciation_lexicon_configured: true } } };
    const overrides = presetOverrides(preset, "series");
    expect(overrides.voiceId).toBe("Ngọc Linh");
    expect(overrides.vieneuStyle).toBe("doc_truyen");
    expect(safeConfigValid({ kind: "series", displayTitle: "Season", runName: "season", displayTitleTouched: true, runNameTouched: true, sourceToken: "manifest", sourceName: "manifest.yaml", presetToken: "preset", outputParentToken: "runs", outputParentName: "runs", episodes: "1-12", overrides: { ...overrides, voiceId: "" }, dirty: {} })).toBe(false);
  });

  it("does not keep auto-filled VieNeu voice dirty when switching back to auto", () => {
    const vieneuState: WizardState = {
      kind: "single", displayTitle: "Episode", runName: "episode", displayTitleTouched: true, runNameTouched: true,
      sourceToken: "source", sourceName: "episode.mp4", presetToken: "preset", outputParentToken: "runs", outputParentName: "runs", episodes: "1",
      overrides: { ...defaultOverrides, providerMode: "vieneu", voiceId: "Ngọc Linh", vieneuStyle: "doc_truyen" },
      dirty: { providerMode: true, voiceId: true, vieneuStyle: true }, derivedVieNeuFields: { voiceId: true, vieneuStyle: true },
    };
    const switched: WizardState = { ...vieneuState, overrides: { ...vieneuState.overrides, providerMode: "auto" }, dirty: { providerMode: true } };
    expect(buildPayload(switched).overrides).toEqual({ tts: { provider_mode: "auto" } });
  });

  it("inspects a manifest, previews source health and reruns blocked preflight", async () => {
    vi.spyOn(api, "presets").mockResolvedValue([{ id: "anime", name: "Anime practical", path: "anime", token: "preset-token", kind: "series", description: "Safe defaults", summary: { series: { target_total_min_s: 2100 } } }]);
    vi.spyOn(api, "roots").mockResolvedValue([{ id: "runs", label: "Runs", token: "runs-token" }]);
    vi.spyOn(api, "listing").mockImplementation(async (_root, token) => ({ current: { root_id: "runs", token: token ?? "runs-token", name: token ? "Sources" : "Runs", at_root: !token }, entries: token ? [{ token: "manifest-token", name: "series_manifest.yaml", kind: "file", suffix: ".yaml" }] : [{ token: "source-dir", name: "Sources", kind: "directory" }] }));
    vi.spyOn(api, "inspectSeries").mockResolvedValue({ kind: "series", manifest_token: "manifest-token", manifest_name: "series_manifest.yaml", series_id: "solo-leveling", display_title: "Solo Leveling - Season 1", suggested_run_name: "solo-leveling-s01", episodes: [{ episode_key: "e01", episode_number: 1, source_available: true }], missing_source_count: 0, duplicate_source_count: 0, total_duration_s: 1440 });
    const preflight = vi.spyOn(api, "preflight").mockResolvedValue({ can_start: false, checks: [{ key: "ffmpeg", label: "ffmpeg", status: "block", detail: "Missing" }] });
    renderUi(<NewRunPage />, "/runs/new");
    const user = userEvent.setup();

    const browse = screen.getAllByRole("button", { name: "Duyệt" });
    await user.click(browse[0]!);
    await user.selectOptions(await screen.findByLabelText("Vùng được phép"), "runs");
    await user.click(await screen.findByRole("button", { name: /Mở/i }));
    await user.click(await screen.findByRole("button", { name: /^Chọn$/i }));
    expect(await screen.findByDisplayValue("Solo Leveling - Season 1")).toBeInTheDocument();
    expect(screen.getByDisplayValue("solo-leveling-s01")).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "Duyệt" })[1]!);
    await user.selectOptions(await screen.findByLabelText("Vùng được phép"), "runs");
    await user.click(await screen.findByRole("button", { name: /Chọn thư mục hiện tại/i }));
    await user.click(screen.getByRole("button", { name: /Tiếp tục/i }));
    fireEvent.change(screen.getByLabelText("Preset production"), { target: { value: "preset-token" } });
    fireEvent.click(screen.getByRole("button", { name: /Tiếp tục/i }));
    await waitFor(() => expect(preflight).toHaveBeenCalled());
    expect(preflight.mock.calls[0]![0]).toMatchObject({ display_title: "Solo Leveling - Season 1", run_parent_token: "runs-token", run_name: "solo-leveling-s01" });
    expect(await screen.findByText(/Cách xử lý/i)).toBeInTheDocument();
    preflight.mockResolvedValueOnce({ can_start: true, checks: [{ key: "ffmpeg", label: "ffmpeg", status: "pass", detail: "Ready" }] });
    fireEvent.click(screen.getByRole("button", { name: /Chạy lại preflight/i }));
    expect(await screen.findByText("Ready", { exact: true })).toBeInTheDocument();
  });
});
