import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { JobEvent } from "../types";
import { normalizeEvent } from "./client";

export function useJobEvents(jobId: string | undefined): { events: JobEvent[]; connected: boolean } {
  const [stream, setStream] = useState<{ jobId?: string; events: JobEvent[] }>({ events: [] });
  const [connected, setConnected] = useState(false);
  const latestId = useRef(0);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!jobId) return undefined;
    latestId.current = 0;
    const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events/stream`);
    const consume = (event: MessageEvent<string>) => {
      try {
        const parsed = normalizeEvent(JSON.parse(event.data) as Parameters<typeof normalizeEvent>[0]);
        const id = Number(event.lastEventId || parsed.id);
        if (id <= latestId.current) return;
        latestId.current = id;
        setStream((current) => {
          const currentEvents = current.jobId === jobId ? current.events : [];
          return { jobId, events: [...currentEvents, { ...parsed, id }] };
        });
        if (parsed.type === "job_state" || parsed.type === "stage_state") {
          void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
          void queryClient.invalidateQueries({ queryKey: ["runs"] });
        }
        if (parsed.type === "artifact" || parsed.type === "qa") {
          void queryClient.invalidateQueries({ queryKey: ["run"] });
        }
      } catch {
        // Ignore malformed transport events; the persisted events endpoint remains authoritative.
      }
    };
    ["job_state", "stage_state", "log", "artifact", "qa", "heartbeat"].forEach((name) => source.addEventListener(name, consume));
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    return () => source.close();
  }, [jobId, queryClient]);

  return { events: stream.jobId === jobId ? stream.events : [], connected };
}
