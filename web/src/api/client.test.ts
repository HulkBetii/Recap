import { describe, expect, it } from "vitest";

import { artifactStageForPath } from "./client";

describe("artifactStageForPath", () => {
  it("maps enhanced edit-plan artifacts to postprocess", () => {
    expect(artifactStageForPath("series_recap/edit_plan.json")).toBe("postprocess");
    expect(artifactStageForPath("series_recap/edit_plan.meta.json")).toBe("postprocess");
    expect(artifactStageForPath("series_recap/edit_plan.qa.json")).toBe("postprocess");
    expect(artifactStageForPath("series_recap/audio_attribution.txt")).toBe("postprocess");
  });

  it("preserves legacy final-stage mappings", () => {
    expect(artifactStageForPath("series_recap/edl.json")).toBe("series_match");
    expect(artifactStageForPath("series_recap/render.meta.json")).toBe("render");
  });
});
