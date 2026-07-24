import { useQuery } from "@tanstack/react-query";
import { Activity, Clapperboard, Gauge, Plus, Settings } from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "../api/client";
import styles from "./shell.module.css";
import ui from "./ui.module.css";

const navItems = [
  { to: "/runs", label: "Runs", icon: Clapperboard, end: true },
  { to: "/runs/new", label: "New Run", icon: Plus },
  { to: "/settings", label: "Settings", icon: Settings },
];

function Navigation({ mobile = false }: { mobile?: boolean }) {
  return <nav className={mobile ? styles.mobileNav : styles.nav}>{!mobile && <p className={styles.navLabel}>Workspace</p>}{navItems.map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => `${styles.navLink} ${isActive ? styles.navActive : ""}`}><Icon size={16} /><span>{label}</span></NavLink>)}</nav>;
}

export function AppShell() {
  const location = useLocation();
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 10_000 });
  const active = health.data?.active_job_id;
  const crumbs = location.pathname.split("/").filter(Boolean);

  return <div className={styles.shell}>
    <aside className={styles.sidebar}>
      <div className={styles.brand}><span className={styles.mark}><Gauge size={17} /></span><div className={styles.brandText}><strong>Recap</strong><span>Local control room</span></div></div>
      <Navigation />
      <div className={styles.sidebarFoot}><div className={styles.runtime}><span className={styles.runtimeDot} />{health.isError ? "Runtime unavailable" : active ? "Pipeline active" : "Runtime ready"}</div></div>
    </aside>
    <div className={styles.content}>
      <header className={styles.topbar}><div className={styles.crumbs}><Activity size={13} /><span>{crumbs.join(" / ") || "runs"}</span></div><div className={styles.topActions}>{typeof health.data?.queue_depth === "number" && <span className={`${ui.status} ${health.data.queue_depth ? ui.toneWarn : ui.tonePass}`}>{health.data.queue_depth} queued</span>}<span className={styles.clock}>127.0.0.1:8765</span></div></header>
      <main className={styles.main}><Outlet /></main>
    </div>
    <Navigation mobile />
  </div>;
}

export function PageHeader({ eyebrow, title, subtitle, actions }: { eyebrow: string; title: string; subtitle: string; actions?: React.ReactNode }) {
  return <header className={styles.pageHead}><div><p className={styles.eyebrow}>{eyebrow}</p><h1 className={styles.pageTitle}>{title}</h1><p className={styles.pageSubtitle}>{subtitle}</p></div>{actions}</header>;
}
