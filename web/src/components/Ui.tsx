import * as Dialog from "@radix-ui/react-dialog";
import { AlertTriangle, Inbox } from "lucide-react";
import type { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from "react";
import { statusTone, titleCase } from "../utils";
import styles from "./ui.module.css";

type ButtonVariant = "default" | "primary" | "danger" | "ghost";

export function Button({ variant = "default", large, iconOnly, className, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; large?: boolean; iconOnly?: boolean }) {
  const classes = [styles.button, styles[variant], large && styles.large, iconOnly && styles.iconOnly, className].filter(Boolean).join(" ");
  return <button {...props} className={classes} />;
}

export function StatusPill({ status, label }: { status: string; label?: string }) {
  const tone = statusTone(status);
  return <span className={`${styles.status} ${styles[`tone${tone[0]!.toUpperCase()}${tone.slice(1)}`]}`}>{label ?? titleCase(status)}</span>;
}

export function Section({ title, eyebrow, action, children, className }: PropsWithChildren<{ title: string; eyebrow?: string; action?: ReactNode; className?: string }>) {
  return (
    <section className={`${styles.section} ${className ?? ""}`}>
      <header className={styles.sectionHead}>
        <div>{eyebrow && <p className={styles.sectionEyebrow}>{eyebrow}</p>}<h2 className={styles.sectionTitle}>{title}</h2></div>
        {action}
      </header>
      <div className={styles.sectionBody}>{children}</div>
    </section>
  );
}

export function EmptyState({ title, detail, action }: { title: string; detail: string; action?: ReactNode }) {
  return <div className={styles.empty}><div><span className={styles.emptyIcon}><Inbox size={21} /></span><h3>{title}</h3><p>{detail}</p>{action && <div style={{ marginTop: 18 }}>{action}</div>}</div></div>;
}

export function ErrorNotice({ error }: { error: unknown }) {
  return <div className={styles.error} role="alert"><AlertTriangle size={18} /><span>{error instanceof Error ? error.message : "The control room could not load this data."}</span></div>;
}

export function Skeleton({ height = 100 }: { height?: number }) { return <div className={styles.skeleton} style={{ minHeight: height }} />; }

export function Field({ label, hint, error, children }: PropsWithChildren<{ label: string; hint?: string; error?: string }>) {
  return <div className={styles.field}><span className={styles.label}>{label}</span>{children}{hint && <span className={styles.fieldHint}>{hint}</span>}{error && <span className={styles.fieldError}>{error}</span>}</div>;
}

export const inputClass = styles.input;
export const selectClass = styles.select;
export const textareaClass = styles.textarea;

export function ConfirmDialog({ open, title, detail, confirmLabel, danger, onConfirm, onOpenChange }: { open: boolean; title: string; detail: string; confirmLabel: string; danger?: boolean; onConfirm: () => void; onOpenChange: (open: boolean) => void }) {
  return <Dialog.Root open={open} onOpenChange={onOpenChange}><Dialog.Portal><Dialog.Overlay className={styles.dialogOverlay} /><Dialog.Content className={styles.dialogContent}><div className={styles.dialogHead}><Dialog.Title>{title}</Dialog.Title><Dialog.Description>{detail}</Dialog.Description></div><div className={styles.dialogActions}><Dialog.Close asChild><Button variant="ghost">Keep running</Button></Dialog.Close><Button variant={danger ? "danger" : "primary"} onClick={onConfirm}>{confirmLabel}</Button></div></Dialog.Content></Dialog.Portal></Dialog.Root>;
}
