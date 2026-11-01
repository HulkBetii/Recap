import { useMutation, useQuery } from "@tanstack/react-query";
import { Folder, Play, ShieldCheck } from "lucide-react";
import { api } from "../api/client";
import { PageHeader } from "../components/AppShell";
import { Button, ErrorNotice, Section, Skeleton, StatusPill } from "../components/Ui";
import { useLocale } from "../i18n";
import styles from "./pages.module.css";

export function SettingsPage() {
  const { t } = useLocale();
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const roots = useQuery({ queryKey: ["fs-roots"], queryFn: api.roots });
  const profile = useMutation({ mutationFn: api.testProfile });
  return <>
    <PageHeader eyebrow={t("settings.eyebrow")} title={t("settings.title")} subtitle={t("settings.subtitle")} />
    <div className={styles.settingGrid}>
      <Section eyebrow={t("settings.environment")} title={t("settings.runtimeChecks")} action={<ShieldCheck size={17} />}>{health.isLoading ? <Skeleton height={260} /> : health.error ? <ErrorNotice error={health.error} /> : <div className={styles.checks}>{health.data?.checks?.map((check) => <div className={styles.check} key={check.key}><div><strong>{check.label}</strong><p>{check.detail ?? t("settings.ready")}</p></div><StatusPill status={check.status} /></div>) ?? <div className={styles.check}><div><strong>{t("settings.controlPlane")}</strong><p>{t("settings.sameOriginOnly", { host: window.location.host })}</p></div><StatusPill status={health.data?.status === "pass" ? "pass" : "warn"} /></div>}<div className={styles.check}><div><strong>{t("settings.chatgptProfile")}</strong><p>{t("settings.profileSmoke")}</p></div><div style={{ display: "flex", gap: 8, alignItems: "center" }}>{profile.data && <StatusPill status={profile.data.status} /> }<Button disabled={profile.isPending} onClick={() => profile.mutate()}><Play size={13} />{t("settings.test")}</Button></div></div></div>}</Section>
      <Section eyebrow={t("settings.filesystemSecurity")} title={t("settings.allowedRoots")} action={<Folder size={17} />}>{roots.isLoading ? <Skeleton height={260} /> : roots.error ? <ErrorNotice error={roots.error} /> : <div className={styles.rootList}>{roots.data?.map((root) => <div className={styles.root} key={root.id}><div><strong>{root.label}</strong><span>{root.path ?? t("settings.pathHidden")}</span></div><StatusPill status="pass" label={t("settings.allowlisted")} /></div>)}</div>}</Section>
      <Section eyebrow={t("settings.policy")} title={t("settings.guarantees")}><div className={styles.checks}><Policy title={t("settings.oneActive")} detail={t("settings.oneActiveDetail")} /><Policy title={t("settings.cliBoundary")} detail={t("settings.cliBoundaryDetail")} /><Policy title={t("settings.localOnly")} detail={t("settings.localOnlyDetail")} /><Policy title={t("settings.noSecret")} detail={t("settings.noSecretDetail")} /></div></Section>
      <Section eyebrow={t("settings.application")} title={t("settings.buildInfo")}><div className={styles.checks}><div className={styles.check}><div><strong>Recap Control Room</strong><p>React 19 / FastAPI / SQLite WAL / REST + SSE</p></div><StatusPill status="pass" label={health.data?.version ?? "V1"} /></div><div className={styles.check}><div><strong>{t("settings.dailyEndpoint")}</strong><p>{window.location.origin}</p></div><StatusPill status={health.data?.status === "pass" ? "pass" : "warn"} label={t("settings.sameOrigin")} /></div></div></Section>
    </div>
  </>;
}

function Policy({ title, detail }: { title: string; detail: string }) { return <div className={styles.check}><div><strong>{title}</strong><p>{detail}</p></div><StatusPill status="pass" /></div>; }
