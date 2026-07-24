import { useMutation, useQuery } from "@tanstack/react-query";
import { Folder, Play, ShieldCheck } from "lucide-react";
import { api } from "../api/client";
import { PageHeader } from "../components/AppShell";
import { Button, ErrorNotice, Section, Skeleton, StatusPill } from "../components/Ui";
import styles from "./pages.module.css";

export function SettingsPage() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const roots = useQuery({ queryKey: ["fs-roots"], queryFn: api.roots });
  const profile = useMutation({ mutationFn: api.testProfile });
  return <>
    <PageHeader eyebrow="Local runtime" title="Settings & health" subtitle="Security-sensitive values are read only. Secrets never leave the backend process." />
    <div className={styles.settingGrid}>
      <Section eyebrow="Environment" title="Runtime checks" action={<ShieldCheck size={17} />}>{health.isLoading ? <Skeleton height={260} /> : health.error ? <ErrorNotice error={health.error} /> : <div className={styles.checks}>{health.data?.checks?.map((check) => <div className={styles.check} key={check.key}><div><strong>{check.label}</strong><p>{check.detail ?? "Ready"}</p></div><StatusPill status={check.status} /></div>) ?? <div className={styles.check}><div><strong>FastAPI control plane</strong><p>127.0.0.1:8765 / same-origin only</p></div><StatusPill status={health.data?.status === "pass" ? "pass" : "warn"} /></div>}<div className={styles.check}><div><strong>ChatGPT persistent profile</strong><p>Active smoke test opens the composer but never sends a prompt.</p></div><div style={{ display: "flex", gap: 8, alignItems: "center" }}>{profile.data && <StatusPill status={profile.data.status} /> }<Button disabled={profile.isPending} onClick={() => profile.mutate()}><Play size={13} />Test</Button></div></div></div>}</Section>
      <Section eyebrow="Filesystem security" title="Allowed roots" action={<Folder size={17} />}>{roots.isLoading ? <Skeleton height={260} /> : roots.error ? <ErrorNotice error={roots.error} /> : <div className={styles.rootList}>{roots.data?.map((root) => <div className={styles.root} key={root.id}><div><strong>{root.label}</strong><span>{root.path ?? "Path hidden by backend"}</span></div><StatusPill status="pass" label="Allowlisted" /></div>)}</div>}</Section>
      <Section eyebrow="Policy" title="Execution guarantees"><div className={styles.checks}><Policy title="One active pipeline" detail="FIFO queue prevents duplicate GPU, media and profile work." /><Policy title="CLI boundary preserved" detail="The worker launches public commands with shell=False; JSON artifacts remain authoritative." /><Policy title="Local-only binding" detail="Production serves one origin on 127.0.0.1 without broad CORS." /><Policy title="No secret exposure" detail="Provider readiness is boolean; environment values and prompts never enter API responses." /></div></Section>
      <Section eyebrow="Application" title="Build information"><div className={styles.checks}><div className={styles.check}><div><strong>Recap Control Room</strong><p>React 19 / FastAPI / SQLite WAL / REST + SSE</p></div><StatusPill status="pass" label={health.data?.version ?? "V1"} /></div><div className={styles.check}><div><strong>Daily endpoint</strong><p>http://127.0.0.1:8765</p></div><StatusPill status={health.data?.status === "pass" ? "pass" : "warn"} label="Same origin" /></div></div></Section>
    </div>
  </>;
}

function Policy({ title, detail }: { title: string; detail: string }) { return <div className={styles.check}><div><strong>{title}</strong><p>{detail}</p></div><StatusPill status="pass" /></div>; }
