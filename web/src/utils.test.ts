import { describe, expect, it } from "vitest";
import { formatBytes, formatDuration, statusTone, titleCase } from "./utils";

describe("display utilities", () => {
  it("formats media duration and sizes", () => {
    expect(formatDuration(3359)).toBe("55:59");
    expect(formatDuration(3661)).toBe("1:01:01");
    expect(formatBytes(1_572_864)).toBe("1.5 MB");
  });

  it("maps operational states to visual tones", () => {
    expect(statusTone("running")).toBe("active");
    expect(statusTone("succeeded")).toBe("pass");
    expect(statusTone("blocked")).toBe("block");
    expect(titleCase("series_composer")).toBe("Series Composer");
  });
});
