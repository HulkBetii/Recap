import { useQuery } from "@tanstack/react-query";
import { Activity, Clapperboard, Gauge, Plus, Settings } from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "../api/client";
import { useLocale } from "../i18n";
import styles from "./shell.module.css";
import ui from "./ui.module.css";

function Navigation({ mobile = false }: { mobile?: boolean }) {
  const { t } = useLocale();
  const navItems = [
    { to: "/runs", label: t("nav.runs"), icon: Clapperboard, end: true },
    { to: "/runs/new", label: t("nav.newRun"), icon: Plus },
    { to: "/settings", label: t("nav.settings"), icon: Settings },
  ];
  return <nav aria-label={t("nav.workspace")} className={mobile ? styles.mobileNav : styles.nav}>{!mobile && <p className={styles.navLabel}>{t("nav.workspace")}</p>}{navItems.map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => `${styles.navLink} ${isActive ? styles.navActive : ""}`}><Icon size={16} /><span>{label}</span></NavLink>)}</nav>;
}

export function AppShell() {
  const location = useLocation();
  const { locale, setLocale, t } = useLocale();
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 10_000 });
  const active = health.data?.active_job_id;
  const crumbs = location.pathname.split("/").filter(Boolean);

  return <div className={styles.shell}>
    <aside className={styles.sidebar}>
      <div className={styles.brand}><span className={styles.mark}><Gauge size={17} /></span><div className={styles.brandText}><strong>Recap</strong><span>Local control room</span></div></div>
      <Navigation />
      <div className={styles.sidebarFoot}><div className={styles.runtime}><span className={styles.runtimeDot} />{health.isError ? t("runtime.unavailable") : active ? t("runtime.active") : t("runtime.ready")}</div></div>
    </aside>
    <div className={styles.content}>
      <header className={styles.topbar}><div className={styles.crumbs}><Activity size={13} /><span>{crumbs.join(" / ") || "runs"}</span></div><div className={styles.topActions}>{typeof health.data?.queue_depth === "number" && <span className={`${ui.status} ${health.data.queue_depth ? ui.toneWarn : ui.tonePass}`}>{health.data.queue_depth} {t("runtime.queued")}</span>}<div className={styles.localeToggle} role="group" aria-label={t("locale.label")}><button type="button" aria-pressed={locale === "vi"} onClick={() => setLocale("vi")}>VI</button><button type="button" aria-pressed={locale === "en"} onClick={() => setLocale("en")}>EN</button></div><span className={styles.clock}>{window.location.host}</span></div></header>
      <main className={styles.main}><Outlet /></main>
    </div>
    <Navigation mobile />
  </div>;
}

export function PageHeader({ eyebrow, title, subtitle, actions }: { eyebrow: string; title: string; subtitle: string; actions?: React.ReactNode }) {
  return <header className={styles.pageHead}><div><p className={styles.eyebrow}>{eyebrow}</p><h1 className={styles.pageTitle}>{title}</h1><p className={styles.pageSubtitle}>{subtitle}</p></div>{actions}</header>;
}
