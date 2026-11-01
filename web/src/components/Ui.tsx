import * as Dialog from "@radix-ui/react-dialog";
import { AlertTriangle, Inbox } from "lucide-react";
import { cloneElement, isValidElement } from "react";
import type { ButtonHTMLAttributes, PropsWithChildren, ReactElement, ReactNode } from "react";
import { useLocale, type MessageKey } from "../i18n";
import { statusTone, titleCase } from "../utils";
import styles from "./ui.module.css";

type ButtonVariant = "default" | "primary" | "danger" | "ghost";

const STATUS_MESSAGES: Record<string, MessageKey> = {
  pass: "status.pass", warn: "status.warn", warning: "status.warn", block: "status.block", blocked: "status.block",
  running: "status.running", queued: "status.queued", succeeded: "status.succeeded", failed: "status.failed",
  interrupted: "status.interrupted", cancelled: "status.cancelled", cancel_requested: "status.cancelRequested",
  skipped_valid: "status.skippedValid", pending: "status.pending", starting: "status.starting", unknown: "status.unknown",
  offline: "status.offline", checking: "status.checking", orphaned: "status.orphaned",
};

export function Button({ variant = "default", large, iconOnly, className, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; large?: boolean; iconOnly?: boolean }) {
  const classes = [styles.button, styles[variant], large && styles.large, iconOnly && styles.iconOnly, className].filter(Boolean).join(" ");
  return <button {...props} className={classes} />;
}

export function StatusPill({ status, label }: { status: string; label?: string }) {
  const { t } = useLocale();
  const tone = statusTone(status);
  const text = label ?? (STATUS_MESSAGES[status] ? t(STATUS_MESSAGES[status]) : titleCase(status));
  return <span className={`${styles.status} ${styles[`tone${tone[0]!.toUpperCase()}${tone.slice(1)}`]}`} role="status" aria-label={text}>{text}</span>;
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
  const { t } = useLocale();
  return <div className={styles.error} role="alert"><AlertTriangle size={18} /><span>{error instanceof Error ? error.message : t("common.error")}</span></div>;
}

export function Skeleton({ height = 100 }: { height?: number }) { return <div className={styles.skeleton} style={{ minHeight: height }} />; }

export function Field({ label, htmlFor, hint, error, children }: PropsWithChildren<{ label: string; htmlFor?: string; hint?: string; error?: string }>) {
  const hintId = htmlFor && hint ? `${htmlFor}-hint` : undefined;
  const errorId = htmlFor && error ? `${htmlFor}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  const isFormControl = isValidElement(children) && typeof children.type === "string" && ["input", "select", "textarea"].includes(children.type);
  const fieldChildren = describedBy && isFormControl
    ? cloneElement(children as ReactElement<Record<string, unknown>>, { "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })
    : children;
  const labelNode = htmlFor
    ? <label className={styles.label} htmlFor={htmlFor}>{label}</label>
    : <span className={styles.label}>{label}</span>;
  return <div className={styles.field}>{labelNode}{fieldChildren}{hint && <span id={hintId} className={styles.fieldHint}>{hint}</span>}{error && <span id={errorId} className={styles.fieldError} role="alert">{error}</span>}</div>;
}

export const inputClass = styles.input;
export const selectClass = styles.select;
export const textareaClass = styles.textarea;

export function ConfirmDialog({ open, title, detail, confirmLabel, danger, onConfirm, onOpenChange }: { open: boolean; title: string; detail: string; confirmLabel: string; danger?: boolean; onConfirm: () => void; onOpenChange: (open: boolean) => void }) {
  const { t } = useLocale();
  return <Dialog.Root open={open} onOpenChange={onOpenChange}><Dialog.Portal><Dialog.Overlay className={styles.dialogOverlay} /><Dialog.Content className={styles.dialogContent}><div className={styles.dialogHead}><Dialog.Title>{title}</Dialog.Title><Dialog.Description>{detail}</Dialog.Description></div><div className={styles.dialogActions}><Dialog.Close asChild><Button variant="ghost">{t("common.keepRunning")}</Button></Dialog.Close><Button variant={danger ? "danger" : "primary"} onClick={onConfirm}>{confirmLabel}</Button></div></Dialog.Content></Dialog.Portal></Dialog.Root>;
}
