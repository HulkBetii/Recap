import * as Tabs from "@radix-ui/react-tabs";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { PageHeader } from "../components/AppShell";
import { ArtifactBrowser } from "../components/DataViews";
import { EmptyState, ErrorNotice, Section, Skeleton, StatusPill } from "../components/Ui";
import { useLocale } from "../i18n";
import { formatDuration } from "../utils";
import styles from "./pages.module.css";

export function EpisodeDetailPage() {
  const { t } = useLocale();
  const { runId = "", episodeKey = "" } = useParams();
  const detail = useQuery({ queryKey: ["run", runId, "episode", episodeKey], queryFn: () => api.episode(runId, episodeKey) });
  const artifacts = useQuery({ queryKey: ["run", runId, "artifacts"], queryFn: () => api.artifacts(runId) });
  const episodeArtifacts = artifacts.data?.filter((item) => item.episode_key === episodeKey) ?? [];
  if (detail.isLoading) return <><PageHeader eyebrow={t("episode.qa")} title={t("episode.loading")} subtitle={t("episode.loadingDetail")} /><Skeleton height={300} /></>;
  if (detail.error || !detail.data) return <><PageHeader eyebrow={t("episode.qa")} title={t("episode.unavailable")} subtitle={t("episode.unavailableDetail")} /><ErrorNotice error={detail.error} /></>;
  const episode = detail.data;

  return <>
    <PageHeader eyebrow={`${t("episode.label")} ${episode.number ?? episode.key}`} title={episode.title ?? episode.key} subtitle={t("episode.readOnlyDetail", { duration: formatDuration(episode.duration_s) })} actions={<Link className={styles.actionLink} to={`/runs/${runId}`}><ArrowLeft size={14} />{t("episode.back")}</Link>} />
    <div className={styles.healthStrip}><EpisodeStat label={t("episode.qa")} value={episode.status} status={episode.status} /><EpisodeStat label={t("episode.translation")} value={episode.translation_ratio == null ? "-" : `${Math.round(episode.translation_ratio * 100)}%`} status={(episode.translation_ratio ?? 1) >= .95 ? "pass" : "block"} /><EpisodeStat label={t("episode.timecodes")} value={episode.approximate_timecodes ? t("episode.approximate") : t("episode.strict")} status={episode.approximate_timecodes ? "block" : "pass"} /><EpisodeStat label={t("episode.validStages")} value={`${episode.stages.filter((stage) => ["succeeded", "skipped_valid"].includes(stage.status)).length}/${episode.stages.length}`} status={episode.stages.some((stage) => stage.status === "failed") ? "block" : "pass"} /></div>
    <Tabs.Root defaultValue="transcript"><Tabs.List className={styles.tabsList}><Tabs.Trigger className={styles.tab} value="transcript">{t("episode.transcript")}</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="review">{t("episode.review")}</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="tts">{t("episode.tts")}</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="shots">{t("episode.shots")}</Tabs.Trigger><Tabs.Trigger className={styles.tab} value="artifacts">{t("episode.artifacts")}</Tabs.Trigger></Tabs.List>
      <Tabs.Content className={styles.tabPanel} value="transcript"><RawSection title={t("episode.filmMap")} eyebrow="GD1 contract" value={episode.film_map} /></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="review"><RawSection title={t("episode.narration")} eyebrow="GD2 contract" value={episode.review_beats} /></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="tts"><RawSection title={t("episode.voiceover")} eyebrow="GD3 contract" value={episode.timings} /></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="shots"><RawSection title={t("episode.shotLibrary")} eyebrow="GD4 contract" value={episode.shots} /></Tabs.Content>
      <Tabs.Content className={styles.tabPanel} value="artifacts"><Section eyebrow={t("episode.files")} title={t("episode.artifactBrowser")}>{episodeArtifacts.length ? <ArtifactBrowser runId={runId} artifacts={episodeArtifacts} /> : <EmptyState title={t("episode.noArtifacts")} detail={t("episode.noArtifactsDetail")} />}</Section></Tabs.Content>
    </Tabs.Root>
  </>;
}

function EpisodeStat({ label, value, status }: { label: string; value: string; status: string }) { return <div className={styles.healthCell}><div><p>{label}</p><strong>{value}</strong></div><StatusPill status={status} /></div>; }

function RawSection({ title, eyebrow, value }: { title: string; eyebrow: string; value: unknown }) {
  const { t } = useLocale();
  return <Section eyebrow={eyebrow} title={title}>{Array.isArray(value) && value.length ? <div className={styles.rawGrid}>{chunk(value, 2).map((group, index) => <article className={styles.rawCard} key={index}><div className={styles.rawHead}>{t("episode.records", { from: index * 25 + 1, to: index * 25 + group.length })}</div><div className={styles.rawBody}><pre>{JSON.stringify(group, null, 2)}</pre></div></article>)}</div> : <EmptyState title={t("episode.artifactUnavailable")} detail={t("episode.artifactUnavailableDetail")} />}</Section>;
}

function chunk<T>(values: T[], columns: number): T[][] {
  const batch = Math.max(1, Math.ceil(values.length / columns));
  return Array.from({ length: Math.ceil(values.length / batch) }, (_, index) => values.slice(index * batch, (index + 1) * batch));
}
