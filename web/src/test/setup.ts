import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

class EventSourceStub {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  addEventListener() { /* transport behavior is covered by browser tests */ }
  close() { /* no-op */ }
}

Object.defineProperty(globalThis, "EventSource", { value: EventSourceStub, configurable: true });
