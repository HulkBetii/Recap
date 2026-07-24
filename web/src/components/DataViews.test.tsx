import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderUi } from "../test/render";
import { EdlTimeline, LogViewer, QaPanel } from "./DataViews";

describe("quality views", () => {
  it("keeps over-cap duration non-blocking while showing planning references", () => {
    renderUi(<QaPanel qa={{ status: "warn", metrics: [{ key: "render", label: "Render format", status: "pass", value: "1920x1080" }], durations: { composer_estimate_s: 2650, tts_s: 3358, edl_s: 3359, render_s: 3359, minimum_s: 2100, target_max_s: 2700, hard_cap_s: 3000 } }} />);
    expect(screen.getByText("44:10")).toBeInTheDocument();
    expect(screen.getAllByText("55:59")).toHaveLength(2);
    expect(screen.getByText("50:00")).toBeInTheDocument();
    expect(screen.getByText("Required minimum")).toBeInTheDocument();
    expect(screen.getByText("Planning reference")).toBeInTheDocument();
    expect(screen.getByText("Render actual").parentElement).toHaveAttribute("data-duration-tone", "neutral");
    expect(screen.getByText("1920x1080")).toBeInTheDocument();
  });

  it("blocks only when rendered duration is below the required minimum", () => {
    renderUi(<QaPanel qa={{ status: "block", metrics: [], durations: { render_s: 1800, minimum_s: 2100, hard_cap_s: 3000 } }} />);
    expect(screen.getByText("Render actual").parentElement).toHaveAttribute("data-duration-tone", "block");
    expect(screen.getByText("30:00")).toBeInTheDocument();
    expect(screen.getByText("35:00")).toBeInTheDocument();
  });

  it("shows placement source details on the read-only timeline", () => {
    renderUi(<EdlTimeline placements={[{ id: "p1", episode_key: "s01e01", tl_start: 0, tl_end: 4, src: "s01e01/source.mp4", src_in: 10, src_out: 14 }, { id: "p2", episode_key: "s01e02", tl_start: 4, tl_end: 8, src: "s01e02/source.mp4" }]} />);
    fireEvent.click(screen.getByLabelText("Placement p2"));
    expect(screen.getByText("s01e02")).toBeInTheDocument();
    expect(screen.getByText("source.mp4")).toBeInTheDocument();
  });

  it("expands persisted log batches into readable lines", () => {
    renderUi(<LogViewer connected events={[{ id: 42, type: "log", timestamp: "2026-07-24T10:00:00Z", level: "info", message: "", payload: { source: "series_recap.log", lines: ["composer started", "arc 1 complete"] } }]} />);
    expect(screen.getByText("composer started")).toBeInTheDocument();
    expect(screen.getByText("arc 1 complete")).toBeInTheDocument();
    expect(screen.getAllByText("series_recap.log")).toHaveLength(2);
  });
});
