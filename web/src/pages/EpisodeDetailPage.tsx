import * as Tabs from "@radix-ui/react-tabs";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { PageHeader } from "../components/AppShell";
import { ArtifactBrowser } from "../components/DataViews";
import { Button, EmptyState, ErrorNotice, Section, Skeleton, StatusPill } from "../components/Ui";
import { formatDuration } from "../utils";
import styles from "./pages.module.css";

export function EpisodeDetailPage() {
  const { runId = "", episodeKey = "" } = useParams();
  const detail = useQuery({ queryKey: ["run", runId, "episode", episodeKey], queryFn: () => api.episode(runId, episodeKey) });
  const artifacts = useQuery({ queryKey: ["run", runId, "artifacts"], queryFn: () => api.artifacts(runId) });
  const episodeArtifacts = artifacts.data?.filter((item) => item.episode_key === episodeKey) ?? [];
  if (detail.isLoading) return <><PageHeader eyebrow="Episode QA" title="Loading episode" subtitle="Reading artifact contracts." /><Skeleton height={300} /></>;
  if (detail.error || !detail.data) return <><PageHeader eyebrow="Episode QA" title="Episode unavailable" subtitle="The requested episode could not be indexed." /><ErrorNotice error={detail.error} /></>;
  const episode = detail.data;

  return <>
    <PageHeader eyebrow={`Episode ${episode.number ?? episode.key}`} title={episode.title ?? episode.key} subtitle={`${formatDuration(episode.duration_s)} source duration / artifact contracts are read only.`} actions={<Link to={`/runs/${runId}`}><Button variant="ghost"><ArrowLeft size={14} />Back to run</Button></Link>} />
    <div className={styles.healthStrip}><EpisodeStat label="Episode QA" value={episode.status} status={episode.status} /><EpisodeStat label="Translation" value={episode.translation_ratio == null ? "-" : `${Math.round(episode.translation_ratio * 100)}%`} status={(episode.translation_ratio ?? 1) >= .95 ? "pass" : "block"} /><EpisodeStat label="Timecodes" value={episode.approximate_timecodes ? "Approximate" : "Strict"} status={episode.approximate_timecodes ? "block" : "pass"} /><EpisodeStat label="Stages valid" value={`${episode.stages.filter((stage) => ["succeeded", "skipped_valid"].includes(stage.status)).length}/${episode.stages.length}`} status={episode.stages.some((stage) => stage.status === "failed") ? "block" : "pass"} /></div>
    <Tabs.Root defaultValue="transcript"><Tabs.List className={styles.tabsList}><Tabs.Trigger className={styles.tab} value="transcript">Transcript</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="review">Review beats</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="tts">TTS timing</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="shots">Shots</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="artifacts">Artifacts</Tabs.Trigger></Tabs.List>
      <Tabs.Content className={styles.tabPanel} value="transcript"><RawSection title="Film map" eyebrow="GD1 contract" value={episode.film_map} /></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="review"><RawSection title="Narration and source spans" eyebrow="GD2 contract" value={episode.review_beats} /></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="tts"><RawSection title="Voiceover timing" eyebrow="GD3 contract" value={episode.timings} /></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="shots"><RawSection title="Shot library" eyebrow="GD4 contract" value={episode.shots} /></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="artifacts"><Section eyebrow="Episode files" title="Artifact browser">{episodeArtifacts.length ? <ArtifactBrowser runId={runId} artifacts={episodeArtifacts} /> : <EmptyState title="No episode artifacts" detail="No indexed artifact is associated with this episode key." />}</Section></Tabs.Content>
    </Tabs.Root>
  </>;
}

function EpisodeStat({ label, value, status }: { label: string; value: string; status: string }) { return <div className={styles.healthCell}><div><p>{label}</p><strong>{value}</strong></div><StatusPill status={status} /></div>; }

function RawSection({ title, eyebrow, value }: { title: string; eyebrow: string; value: unknown }) {
  return <Section eyebrow={eyebrow} title={title}>{Array.isArray(value) && value.length ? <div className={styles.rawGrid}>{chunk(value, 2).map((group, index) => <article className={styles.rawCard} key={index}><div className={styles.rawHead}>Records {index * 25 + 1}-{index * 25 + group.length}</div><div className={styles.rawBody}><pre>{JSON.stringify(group, null, 2)}</pre></div></article>)}</div> : <EmptyState title="Artifact not available" detail="This stage has not produced an indexed contract yet." />}</Section>;
}

function chunk<T>(values: T[], columns: number): T[][] {
  const batch = Math.max(1, Math.ceil(values.length / columns));
  return Array.from({ length: Math.ceil(values.length / batch) }, (_, index) => values.slice(index * batch, (index + 1) * batch));
}
