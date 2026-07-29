import * as Dialog from "@radix-ui/react-dialog";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, ChevronRight, FileVideo, Folder, Layers3, Play, RefreshCw, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { PageHeader } from "../components/AppShell";
import { Button, ErrorNotice, Field, StatusPill, inputClass, selectClass } from "../components/Ui";
import { useLocale } from "../i18n";
import type { FsEntry, FsListing, FsRoot, PlanResponse, PreflightResponse, Preset, RunKind, SeriesSourceInspection, SingleSourceInspection } from "../types";
import { formatBytes } from "../utils";
import page from "./pages.module.css";
import ui from "../components/ui.module.css";

const STEPS = [
  { key: "source", label: "wizard.source.label", hint: "wizard.source.hint" },
  { key: "preset", label: "wizard.preset.label", hint: "wizard.preset.hint" },
  { key: "preflight", label: "wizard.preflight.label", hint: "wizard.preflight.hint" },
  { key: "review", label: "wizard.review.label", hint: "wizard.review.hint" },
] as const;
type OverrideKey = "logLevel" | "providerMode" | "voiceId" | "vieneuStyle" | "targetRatio" | "arcSize" | "seriesMin" | "seriesMax" | "seriesCap" | "ttsSpeed" | "ttsConcurrency" | "renderCrf" | "renderPreset" | "renderConcurrency";
type OverrideValues = Record<OverrideKey, string | number>;
type DirtyOverrides = Partial<Record<OverrideKey, true>>;

export interface WizardState {
  kind: RunKind;
  displayTitle: string;
  runName: string;
  displayTitleTouched: boolean;
  runNameTouched: boolean;
  sourceToken: string;
  sourceName: string;
  presetToken: string;
  outputParentToken: string;
  outputParentName: string;
  episodes: string;
  overrides: OverrideValues;
  dirty: DirtyOverrides;
  /** Tracks fields auto-filled when switching to the local VieNeu provider. */
  derivedVieNeuFields?: Partial<Record<"voiceId" | "vieneuStyle", true>>;
}

export const defaultOverrides: OverrideValues = {
  logLevel: "INFO", providerMode: "auto", voiceId: "", vieneuStyle: "doc_truyen", targetRatio: "auto", arcSize: 3,
  seriesMin: 35, seriesMax: 45, seriesCap: 50, ttsSpeed: 1,
  ttsConcurrency: 3, renderCrf: 20, renderPreset: "medium", renderConcurrency: 4,
};

const initialState: WizardState = {
  kind: "series", displayTitle: "", runName: "", displayTitleTouched: false, runNameTouched: false,
  sourceToken: "", sourceName: "", presetToken: "", outputParentToken: "", outputParentName: "", episodes: "1-12",
  overrides: { ...defaultOverrides }, dirty: {}, derivedVieNeuFields: {},
};

function isValidRunName(value: string): boolean {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(value)) return false;
  const stem = value.split(".", 1)[0]!.toUpperCase();
  return !new Set(["CON", "PRN", "AUX", "NUL", ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}`), ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}`)]).has(stem);
}

export function safeConfigValid(state: WizardState): boolean {
  const values = state.overrides;
  return ["auto", "ai33", "genmax", "openai", "vieneu"].includes(String(values.providerMode))
    && (values.providerMode !== "vieneu" || String(values.voiceId).trim().length > 0)
    && ["tu_nhien", "tin_tuc", "doc_truyen"].includes(String(values.vieneuStyle))
    && Number(values.ttsSpeed) >= .8 && Number(values.ttsSpeed) <= 1.2
    && Number(values.ttsConcurrency) >= 1 && Number(values.ttsConcurrency) <= 8
    && Number(values.renderCrf) >= 16 && Number(values.renderCrf) <= 28
    && Number(values.renderConcurrency) >= 1 && Number(values.renderConcurrency) <= 8
    && (state.kind === "single" || (Number(values.seriesMin) <= Number(values.seriesMax) && Number(values.seriesMax) <= Number(values.seriesCap) && Number(values.arcSize) >= 1 && Number(values.arcSize) <= 6));
}

function secondsToMinutes(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.round(value / 60) : fallback;
}

export function presetOverrides(preset: Preset | undefined, kind: RunKind): OverrideValues {
  const summary = preset?.summary ?? {};
  const readPath = (key: string): unknown => key.split(".").reduce<unknown>((current, part) => current && typeof current === "object" ? (current as Record<string, unknown>)[part] : undefined, summary);
  const read = (keys: string[], fallback: unknown): unknown => {
    for (const key of keys) { const value = readPath(key); if (value !== undefined) return value; }
    return fallback;
  };
  const result: OverrideValues = {
    ...defaultOverrides,
    logLevel: String(read(["orchestrator.log_level", "log_level"], defaultOverrides.logLevel)),
    providerMode: String(read(["tts.provider_mode", "tts_provider_mode"], defaultOverrides.providerMode)),
    voiceId: String(read(["tts.voice_id", "tts.voice", "voice_id"], defaultOverrides.voiceId)),
    vieneuStyle: String(read(["tts.vieneu_style"], defaultOverrides.vieneuStyle)),
    targetRatio: String(read(["review.target_ratio", "target_ratio"], defaultOverrides.targetRatio)),
    arcSize: Number(read(["series.arc_size", "series_recap.arc_size", "arc_size"], defaultOverrides.arcSize)),
    seriesMin: secondsToMinutes(read(["series.minimum_s", "series.target_total_min_s", "series_recap.target_total_min_s", "target_total_min_s"], 2100), defaultOverrides.seriesMin as number),
    seriesMax: secondsToMinutes(read(["series.maximum_s", "series.target_total_max_s", "series_recap.target_total_max_s", "target_total_max_s"], 2700), defaultOverrides.seriesMax as number),
    seriesCap: secondsToMinutes(read(["series.planning_reference_s", "series.hard_cap_s", "series.target_total_hard_cap_s", "series_recap.target_total_hard_cap_s", "target_total_hard_cap_s"], 3000), defaultOverrides.seriesCap as number),
    ttsSpeed: Number(read(["tts.speed", "speed"], defaultOverrides.ttsSpeed)),
    ttsConcurrency: Number(read(["tts.concurrency", "concurrency"], defaultOverrides.ttsConcurrency)),
    renderCrf: Number(read(["render.crf", "crf"], defaultOverrides.renderCrf)),
    renderPreset: String(read(["render.preset", "preset"], defaultOverrides.renderPreset)),
    renderConcurrency: Number(read(["render.concurrency"], defaultOverrides.renderConcurrency)),
  };
  return kind === "single" ? { ...result, arcSize: defaultOverrides.arcSize } : result;
}

export function buildPayload(state: WizardState): Record<string, unknown> {
  const values = state.overrides;
  const dirty = state.dirty;
  const overrides: Record<string, Record<string, unknown>> = {};
  const set = (section: string, key: string, value: unknown, dirtyKey: OverrideKey) => { if (dirty[dirtyKey]) overrides[section] = { ...(overrides[section] ?? {}), [key]: value }; };
  set("orchestrator", "log_level", values.logLevel, "logLevel");
  set("tts", "provider_mode", values.providerMode, "providerMode");
  set("tts", "voice_id", String(values.voiceId).trim(), "voiceId");
  set("tts", "vieneu_style", values.vieneuStyle, "vieneuStyle");
  set("tts", "speed", Number(values.ttsSpeed), "ttsSpeed");
  set("tts", "concurrency", Number(values.ttsConcurrency), "ttsConcurrency");
  set("render", "crf", Number(values.renderCrf), "renderCrf");
  set("render", "preset", values.renderPreset, "renderPreset");
  set("render", "concurrency", Number(values.renderConcurrency), "renderConcurrency");
  if (state.kind === "single") set("review", "target_ratio", values.targetRatio, "targetRatio");
  if (state.kind === "series") {
    set("series_recap", "target_total_min_s", Number(values.seriesMin) * 60, "seriesMin");
    set("series_recap", "target_total_max_s", Number(values.seriesMax) * 60, "seriesMax");
    set("series_recap", "target_total_hard_cap_s", Number(values.seriesCap) * 60, "seriesCap");
    set("series_recap", "arc_size", Number(values.arcSize), "arcSize");
  }
  return {
    kind: state.kind,
    display_title: state.displayTitle.trim(),
    run_name: state.runName.trim(),
    run_parent_token: state.outputParentToken,
    config_token: state.presetToken,
    ...(state.kind === "series" ? { manifest_token: state.sourceToken, episodes: state.episodes } : { source_token: state.sourceToken }),
    overrides,
  };
}

export function NewRunPage() {
  const navigate = useNavigate();
  const { t } = useLocale();
  const [step, setStep] = useState(0);
  const [state, setState] = useState(initialState);
  const [browser, setBrowser] = useState<"source" | "runDir" | null>(null);
  const [inspection, setInspection] = useState<SingleSourceInspection | SeriesSourceInspection>();
  const [preflight, setPreflight] = useState<PreflightResponse>();
  const [plan, setPlan] = useState<PlanResponse>();
  const presets = useQuery({ queryKey: ["presets"], queryFn: api.presets });
  const inspectMutation = useMutation<SingleSourceInspection | SeriesSourceInspection, Error, { kind: RunKind; token: string; episodes: string }>({ mutationFn: async (input) => input.kind === "series" ? api.inspectSeries(input.token, input.episodes) : api.inspectSingle(input.token), onSuccess: (data) => { setInspection(data); setState((current) => ({ ...current, displayTitle: current.displayTitleTouched ? current.displayTitle : data.display_title, runName: current.runNameTouched ? current.runName : data.suggested_run_name })); } });
  const preflightMutation = useMutation({ mutationFn: (input: WizardState) => api.preflight(buildPayload(input)), onSuccess: setPreflight });
  const planMutation = useMutation({ mutationFn: (input: WizardState) => api.plan(input.kind, buildPayload(input)), onSuccess: setPlan });
  const profile = useMutation({ mutationFn: api.testProfile });
  const createMutation = useMutation({ mutationFn: () => api.createJob(plan!.plan_id), onSuccess: (job) => navigate(`/runs/${job.id}`) });
  const availablePresets = presets.data?.filter((preset) => preset.kind === state.kind || preset.kind === "both") ?? [];
  const selectedPreset = availablePresets.find((preset) => preset.token === state.presetToken);
  const validRunName = isValidRunName(state.runName);
  const sourceReady = Boolean(state.sourceToken && state.outputParentToken && state.displayTitle.trim() && validRunName && inspection && (inspection.kind !== "single" || inspection.media_valid !== false) && !inspectMutation.isPending);
  const canAdvance = step === 0 ? sourceReady : step === 1 ? Boolean(state.presetToken && safeConfigValid(state)) : step === 2 ? Boolean(preflight?.can_start) : Boolean(plan?.can_start);
  const disabledReason = step === 0 ? (sourceReady ? "" : t("wizard.disabled.source")) : step === 1 ? (canAdvance ? "" : t("wizard.disabled.preset")) : step === 2 ? (canAdvance ? "" : t("wizard.disabled.preflight")) : (canAdvance ? "" : t("wizard.disabled.review"));

  const updateState = (updater: (current: WizardState) => WizardState) => { setState(updater); setPreflight(undefined); setPlan(undefined); };
  const selectSource = (entry: FsEntry) => {
    updateState((current) => ({ ...current, sourceToken: entry.token, sourceName: entry.name, displayTitle: "", runName: "", displayTitleTouched: false, runNameTouched: false }));
    setInspection(undefined);
    inspectMutation.mutate({ kind: state.kind, token: entry.token, episodes: state.episodes });
    setBrowser(null);
  };
  const selectOutput = (entry: FsEntry) => { updateState((current) => ({ ...current, outputParentToken: entry.token, outputParentName: entry.name })); setBrowser(null); };
  const selectPreset = (token: string) => { const preset = availablePresets.find((item) => item.token === token); updateState((current) => ({ ...current, presetToken: token, overrides: presetOverrides(preset, current.kind), dirty: {}, derivedVieNeuFields: {} })); };
  const selectKind = (kind: RunKind) => { updateState((current) => ({ ...current, kind, sourceToken: "", sourceName: "", presetToken: "", displayTitle: "", runName: "", displayTitleTouched: false, runNameTouched: false, episodes: kind === "series" ? "1-12" : current.episodes, overrides: { ...defaultOverrides }, dirty: {}, derivedVieNeuFields: {} })); setInspection(undefined); };
  const setOverride = (key: OverrideKey, value: string | number) => updateState((current) => {
    const overrides = { ...current.overrides, [key]: value };
    const dirty = { ...current.dirty, [key]: true };
    const derivedVieNeuFields = { ...(current.derivedVieNeuFields ?? {}) };
    const presetValues = presetOverrides(selectedPreset, current.kind);
    if (key === "providerMode" && value === "vieneu") {
      const presetVoice = String(presetValues.voiceId).trim();
      const presetStyle = String(presetValues.vieneuStyle);
      overrides.voiceId = presetVoice || "Ngọc Linh";
      overrides.vieneuStyle = ["tu_nhien", "tin_tuc", "doc_truyen"].includes(presetStyle) ? presetStyle : "doc_truyen";
      dirty.voiceId = true;
      dirty.vieneuStyle = true;
      derivedVieNeuFields.voiceId = true;
      derivedVieNeuFields.vieneuStyle = true;
    } else if (key === "providerMode" && value !== "vieneu" && current.overrides.providerMode === "vieneu") {
      if (derivedVieNeuFields.voiceId) {
        overrides.voiceId = presetValues.voiceId;
        delete dirty.voiceId;
        delete derivedVieNeuFields.voiceId;
      }
      if (derivedVieNeuFields.vieneuStyle) {
        overrides.vieneuStyle = presetValues.vieneuStyle;
        delete dirty.vieneuStyle;
        delete derivedVieNeuFields.vieneuStyle;
      }
    } else if (key === "voiceId" || key === "vieneuStyle") {
      delete derivedVieNeuFields[key];
    }
    return { ...current, overrides, dirty, derivedVieNeuFields };
  });
  const refreshInspection = () => { if (state.sourceToken) inspectMutation.mutate({ kind: state.kind, token: state.sourceToken, episodes: state.episodes }); };
  const goNext = async () => {
    if (step === 1) { await preflightMutation.mutateAsync(state); setStep(2); return; }
    if (step === 2) { await planMutation.mutateAsync(state); setStep(3); return; }
    setStep((current) => Math.min(current + 1, STEPS.length - 1));
  };
  const error = inspectMutation.error ?? preflightMutation.error ?? planMutation.error ?? createMutation.error;
  return <>
    <PageHeader eyebrow={t("wizard.eyebrow")} title={t("wizard.title")} subtitle={t("wizard.subtitle")} />
    <div className={page.wizard}>
      <aside className={page.steps} aria-label="Wizard progress"><ol className={page.stepList}>{STEPS.map((item, index) => <li key={item.key} className={`${page.step} ${index === step ? page.stepActive : ""} ${index < step ? page.stepDone : ""}`} aria-current={index === step ? "step" : undefined}><span className={page.stepNumber}>{index < step ? <Check size={13} /> : index + 1}</span><div><strong>{t(item.label as never)}</strong><small>{t(item.hint as never)}</small></div></li>)}</ol></aside>
      <div className={page.wizardContent}>
        <header className={page.wizardHead}><h2>{t(STEPS[step]!.label as never)}</h2><p>{t(STEPS[step]!.hint as never)}</p></header>
        <div className={page.wizardBody}>
          {step === 0 && <SourceStep state={state} updateState={updateState} onKind={selectKind} onBrowse={setBrowser} inspection={inspection} inspecting={inspectMutation.isPending} onRefresh={refreshInspection} />}
          {step === 1 && <PresetStep state={state} presets={availablePresets} loading={presets.isLoading} selectedPreset={selectedPreset} onPreset={selectPreset} onOverride={setOverride} onReset={() => updateState((current) => ({ ...current, overrides: presetOverrides(selectedPreset, current.kind), dirty: {}, derivedVieNeuFields: {} }))} />}
          {step === 2 && <PreflightStep data={preflight} loading={preflightMutation.isPending} profile={profile} onRerun={() => void preflightMutation.mutateAsync(state)} />}
          {step === 3 && <PlanStep data={plan} loading={planMutation.isPending} />}
          {error && <div style={{ marginTop: 14 }}><ErrorNotice error={error} /></div>}
        </div>
        <footer className={page.wizardFoot}><div>{step > 0 && <Button variant="ghost" disabled={createMutation.isPending} onClick={() => setStep((current) => Math.max(0, current - 1))}><ArrowLeft size={14} />{t("wizard.back")}</Button>}<span className={page.disabledReason} aria-live="polite">{disabledReason}</span></div>{step < 3 ? <Button variant="primary" disabled={!canAdvance || preflightMutation.isPending || planMutation.isPending} onClick={() => void goNext()}>{preflightMutation.isPending || planMutation.isPending ? <RefreshCw className="spin" size={14} /> : null}{step === 2 ? t("wizard.buildReview") : t("wizard.continue")}<ArrowRight size={14} /></Button> : <Button variant="primary" large disabled={!plan?.can_start || createMutation.isPending} onClick={() => createMutation.mutate()}><Play size={15} />{createMutation.isPending ? t("wizard.queuing") : t("wizard.start")}</Button>}</footer>
      </div>
    </div>
    <FileBrowser key={browser ?? "closed"} open={browser !== null} mode={browser ?? "source"} kind={state.kind} onClose={() => setBrowser(null)} onSelect={browser === "source" ? selectSource : selectOutput} />
  </>;
}

function SourceStep({ state, updateState, onKind, onBrowse, inspection, inspecting, onRefresh }: { state: WizardState; updateState: (updater: (current: WizardState) => WizardState) => void; onKind: (kind: RunKind) => void; onBrowse: (mode: "source" | "runDir") => void; inspection?: SingleSourceInspection | SeriesSourceInspection; inspecting: boolean; onRefresh: () => void }) {
  const { t } = useLocale();
  const runNameError = state.runName && !isValidRunName(state.runName) ? t("source.runNameError") : undefined;
  return <>
    <div className={page.choiceGrid}><button type="button" className={`${page.choice} ${state.kind === "single" ? page.choiceSelected : ""}`} aria-pressed={state.kind === "single"} onClick={() => onKind("single")}><FileVideo size={22} /><strong>{t("source.single")}</strong><span>{t("source.singleDetail")}</span></button><button type="button" className={`${page.choice} ${state.kind === "series" ? page.choiceSelected : ""}`} aria-pressed={state.kind === "series"} onClick={() => onKind("series")}><Layers3 size={22} /><strong>{t("source.series")}</strong><span>{t("source.seriesDetail")}</span></button></div>
    <div className={page.formGrid}><Field htmlFor="display-title" label={t("source.displayTitle")} hint={t("source.displayTitleHint")}><input id="display-title" className={inputClass} value={state.displayTitle} placeholder={t("source.displayTitlePlaceholder")} onChange={(event) => updateState((current) => ({ ...current, displayTitle: event.target.value, displayTitleTouched: true }))} /></Field>{state.kind === "series" && <Field htmlFor="episodes" label={t("source.episodes")} hint={t("source.episodesHint")}><input id="episodes" className={inputClass} value={state.episodes} onChange={(event) => updateState((current) => ({ ...current, episodes: event.target.value }))} onBlur={onRefresh} /></Field>}<div className={page.formFull}><Field htmlFor="source-path" label={state.kind === "series" ? t("source.manifest") : t("source.media")} hint={t("source.opaqueHint")}><div className={page.pathRow}><input id="source-path" className={inputClass} aria-describedby="source-path-hint" readOnly value={state.sourceName} placeholder={state.kind === "series" ? t("source.selectManifest") : t("source.selectVideo")} /><Button type="button" onClick={() => onBrowse("source")}><Search size={14} />{t("source.browse")}</Button></div></Field></div><Field htmlFor="output-parent" label={t("source.outputParent")} hint={t("source.outputHint")}><div className={page.pathRow}><input id="output-parent" className={inputClass} aria-describedby="output-parent-hint" readOnly value={state.outputParentName} placeholder={t("source.selectOutput")} /><Button type="button" onClick={() => onBrowse("runDir")}><Folder size={14} />{t("source.browse")}</Button></div></Field><Field htmlFor="run-name" label={t("source.runName")} hint={t("source.runNameHint")} error={runNameError}><input id="run-name" className={inputClass} value={state.runName} placeholder="solo-leveling-s01" aria-invalid={Boolean(runNameError)} onChange={(event) => updateState((current) => ({ ...current, runName: event.target.value, runNameTouched: true }))} /></Field></div>
    {inspection && <InspectionPreview inspection={inspection} onRefresh={onRefresh} />}{inspecting && <div className={page.inlineNotice}><RefreshCw size={14} className="spin" />{t("source.inspecting")}</div>}
  </>;
}

function InspectionPreview({ inspection, onRefresh }: { inspection: SingleSourceInspection | SeriesSourceInspection; onRefresh: () => void }) {
  const { t } = useLocale();
  const series = inspection.kind === "series" ? inspection : undefined;
  const duplicateCount = series?.duplicate_source_count ?? series?.duplicate_source_episode_keys?.length ?? series?.duplicate_sources?.length ?? 0;
  const arcs = series?.arc_preview?.map((arc) => typeof arc === "string" ? arc : arc.title ?? arc.arc ?? arc.episode_keys?.join(", ") ?? "Arc") ?? series?.arcs ?? [];
  return <section className={page.inspection} aria-label={t("source.preview")}><div className={page.inspectionHead}><div><p>{t("source.preview")}</p><strong>{inspection.display_title}</strong></div><Button type="button" variant="ghost" onClick={onRefresh}><RefreshCw size={13} />{t("source.refreshPreview")}</Button></div><div className={page.inspectionStats}><span><b>{t("source.duration")}</b>{inspection.kind === "single" ? formatDuration(inspection.duration_s) : formatDuration(inspection.total_duration_s)}</span>{inspection.kind === "single" && <span><b>{inspection.media_valid === false ? t("source.mediaInvalid") : t("source.mediaValid")}</b><StatusPill status={inspection.media_valid === false ? "block" : "pass"} /></span>}{series && <><span><b>{t("source.episodeCount")}</b>{series.episodes.length}</span><span><b>{t("source.available")}</b>{series.episodes.filter((episode) => episode.source_available).length}</span><span><b>{t("source.missing")}</b>{series.missing_source_count ?? series.missing_sources?.length ?? 0}</span><span><b>{t("source.duplicates")}</b>{duplicateCount}</span></>}</div>{arcs.length > 0 && <div className={page.arcList}><b>{t("source.arcs")}</b>{arcs.map((arc) => <span key={arc}>{arc}</span>)}</div>}</section>;
}

function formatDuration(value: number | null | undefined): string { if (typeof value !== "number" || !Number.isFinite(value)) return "--:--"; const minutes = Math.floor(value / 60); return `${minutes}:${String(Math.floor(value % 60)).padStart(2, "0")}`; }

function summaryRows(summary: Record<string, unknown>): [string, string][] {
  const rows: [string, string][] = [];
  const visit = (value: unknown, prefix: string) => {
    if (rows.length >= 10) return;
    if (value && typeof value === "object" && !Array.isArray(value)) Object.entries(value as Record<string, unknown>).forEach(([key, nested]) => visit(nested, prefix ? `${prefix}.${key}` : key));
    else if (value !== undefined && value !== null) rows.push([prefix, String(value)]);
  };
  visit(summary, "");
  return rows;
}

function summaryValue(summary: Record<string, unknown> | undefined, path: string): unknown {
  return path.split(".").reduce<unknown>((current, part) => current && typeof current === "object" ? (current as Record<string, unknown>)[part] : undefined, summary);
}

function displaySummaryValue(value: unknown, fallback: string): string {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

function PresetStep({ state, presets, loading, selectedPreset, onPreset, onOverride, onReset }: { state: WizardState; presets: Preset[]; loading: boolean; selectedPreset?: Preset; onPreset: (token: string) => void; onOverride: (key: OverrideKey, value: string | number) => void; onReset: () => void }) {
  const { t } = useLocale();
  const dirtyCount = Object.keys(state.dirty).length;
  const configuredProvider = String(state.overrides.providerMode);
  const hasPresetOnlyProvider = ["ai33", "genmax", "openai"].includes(configuredProvider);
  const presetUsesVieNeu = summaryValue(selectedPreset?.summary, "tts.provider_mode") === "vieneu";
  return <div className={page.formGrid}>
    <div className={page.formFull}>
      <Field htmlFor="production-preset" label={t("preset.production")}>
        <select id="production-preset" className={selectClass} disabled={loading} value={state.presetToken} onChange={(event) => onPreset(event.target.value)}>
          <option value="">{loading ? t("preset.loading") : t("preset.select")}</option>
          {presets.map((preset) => <option key={preset.id} value={preset.token}>{preset.name}</option>)}
        </select>
      </Field>
      {selectedPreset && <div className={page.presetSummary}>
        <strong>{selectedPreset.name}</strong>
        <span>{selectedPreset.description ?? t("preset.basic")}</span>
        {selectedPreset.summary && <div className={page.ttsSummary} aria-label={t("preset.ttsSummary")}>
          <span><b>{t("preset.ttsProvider")}</b>{displaySummaryValue(summaryValue(selectedPreset.summary, "tts.provider_mode"), t("preset.notConfigured"))}</span>
          <span><b>{t("preset.ttsVoice")}</b>{displaySummaryValue(summaryValue(selectedPreset.summary, "tts.voice_id"), t("preset.notConfigured"))}</span>
          <span><b>{t("preset.ttsStyle")}</b>{displaySummaryValue(summaryValue(selectedPreset.summary, "tts.vieneu_style"), t("preset.notConfigured"))}</span>
          <span><b>{t("preset.ttsSpeedSummary")}</b>{displaySummaryValue(summaryValue(selectedPreset.summary, "tts.speed"), t("preset.notConfigured"))}</span>
          <span><b>{t("preset.lexicon")}</b>{summaryValue(selectedPreset.summary, "tts.pronunciation_lexicon_configured") === true ? t("preset.configured") : t("preset.notConfigured")}</span>
        </div>}
        {selectedPreset.summary && summaryRows(selectedPreset.summary).map(([key, value]) => <small key={key}>{key.replaceAll("_", " ")}: {value}</small>)}
      </div>}
    </div>
    <div className={page.formFull}>
      <details className={page.advanced}>
        <summary><span><strong>{t("preset.advanced")}</strong><small>{t("preset.advancedHint")}</small></span>{dirtyCount > 0 && <StatusPill status="warn" label={`${dirtyCount} ${t("preset.changed")}`} />}</summary>
        <div className={page.advancedBody}>
          <div className={page.advancedActions}><span>{t("preset.basic")}</span><Button type="button" variant="ghost" disabled={dirtyCount === 0} onClick={onReset}>{t("preset.reset")}</Button></div>
          <div className={page.formGrid}>
            <Field htmlFor="log-level" label={t("preset.logLevel")}><select id="log-level" className={selectClass} value={String(state.overrides.logLevel)} onChange={(event) => onOverride("logLevel", event.target.value)}><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select></Field>
            <Field htmlFor="tts-provider" label={t("preset.providerMode")} hint={presetUsesVieNeu ? t("preset.vieneuProviderLocked") : t("preset.providerHint")}><select id="tts-provider" className={selectClass} value={configuredProvider} onChange={(event) => onOverride("providerMode", event.target.value)}>{!presetUsesVieNeu && <option value="auto">Auto (AI33 → Genmax → OpenAI)</option>}<option value="vieneu">VieNeu local (ONNX)</option>{hasPresetOnlyProvider && <option value={configuredProvider} disabled>{configuredProvider.toUpperCase()} ({t("preset.presetOnly")})</option>}</select></Field>
            <Field htmlFor="voice-id" label={t("preset.voiceId")} hint={state.overrides.providerMode === "vieneu" ? t("preset.vieneuVoiceHint") : t("preset.voiceHint")} error={state.overrides.providerMode === "vieneu" && !String(state.overrides.voiceId).trim() ? t("preset.voiceRequired") : undefined}><input id="voice-id" className={inputClass} value={String(state.overrides.voiceId)} aria-invalid={state.overrides.providerMode === "vieneu" && !String(state.overrides.voiceId).trim()} onChange={(event) => onOverride("voiceId", event.target.value)} /></Field>
            {state.overrides.providerMode === "vieneu" && <Field htmlFor="vieneu-style" label={t("preset.vieneuStyle")}><select id="vieneu-style" className={selectClass} value={String(state.overrides.vieneuStyle)} onChange={(event) => onOverride("vieneuStyle", event.target.value)}><option value="doc_truyen">{t("preset.vieneuStory")}</option><option value="tu_nhien">{t("preset.vieneuNatural")}</option><option value="tin_tuc">{t("preset.vieneuNews")}</option></select></Field>}
            {state.kind === "single" ? <Field htmlFor="target-ratio" label={t("preset.targetRatio")}><select id="target-ratio" className={selectClass} value={String(state.overrides.targetRatio)} onChange={(event) => onOverride("targetRatio", event.target.value)}><option value="auto">Auto</option><option value="0.25">25%</option><option value="0.33">33%</option><option value="0.4">40%</option></select></Field> : <><Field htmlFor="arc-size" label={t("preset.arcSize")}><input id="arc-size" className={inputClass} type="number" min={1} max={6} value={Number(state.overrides.arcSize)} onChange={(event) => onOverride("arcSize", Number(event.target.value))} /></Field><div className={page.formFull}><div className={page.rangeRow}><Field htmlFor="series-min" label={t("preset.minimum")}><input id="series-min" className={inputClass} type="number" min={1} value={Number(state.overrides.seriesMin)} onChange={(event) => onOverride("seriesMin", Number(event.target.value))} /></Field><Field htmlFor="series-max" label={t("preset.maximum")}><input id="series-max" className={inputClass} type="number" min={1} value={Number(state.overrides.seriesMax)} onChange={(event) => onOverride("seriesMax", Number(event.target.value))} /></Field><Field htmlFor="series-cap" label={t("preset.cap")}><input id="series-cap" className={inputClass} type="number" min={1} value={Number(state.overrides.seriesCap)} onChange={(event) => onOverride("seriesCap", Number(event.target.value))} /></Field></div></div></>}
            <Field htmlFor="tts-speed" label={t("preset.ttsSpeed")}><input id="tts-speed" className={inputClass} type="number" min={.8} max={1.2} step={.05} value={Number(state.overrides.ttsSpeed)} onChange={(event) => onOverride("ttsSpeed", Number(event.target.value))} /></Field>
            <Field htmlFor="tts-concurrency" label={t("preset.ttsConcurrency")}><input id="tts-concurrency" className={inputClass} type="number" min={1} max={8} value={Number(state.overrides.ttsConcurrency)} onChange={(event) => onOverride("ttsConcurrency", Number(event.target.value))} /></Field>
            <Field htmlFor="render-crf" label={t("preset.renderCrf")}><input id="render-crf" className={inputClass} type="number" min={16} max={28} value={Number(state.overrides.renderCrf)} onChange={(event) => onOverride("renderCrf", Number(event.target.value))} /></Field>
            <Field htmlFor="render-preset" label={t("preset.renderPreset")}><select id="render-preset" className={selectClass} value={String(state.overrides.renderPreset)} onChange={(event) => onOverride("renderPreset", event.target.value)}><option>slow</option><option>medium</option><option>fast</option></select></Field>
            <Field htmlFor="render-concurrency" label={t("preset.renderConcurrency")}><input id="render-concurrency" className={inputClass} type="number" min={1} max={8} value={Number(state.overrides.renderConcurrency)} onChange={(event) => onOverride("renderConcurrency", Number(event.target.value))} /></Field>
            {!safeConfigValid(state) && <div className={page.formFull}><ErrorNotice error={new Error(t("preset.invalid"))} /></div>}
          </div>
        </div>
      </details>
    </div>
  </div>;
}

function PreflightStep({ data, loading, profile, onRerun }: { data?: PreflightResponse; loading: boolean; profile: ReturnType<typeof useMutation<{ status: "pass" | "warn" | "block"; detail?: string }, Error, void>>; onRerun: () => void }) {
  const { t } = useLocale();
  if (loading || !data) return <div className={page.checks}>{Array.from({ length: 6 }, (_, index) => <div className={page.check} key={index}><div><strong>{t("preflight.running")}</strong><p>{t("preflight.runningDetail")}</p></div><StatusPill status="running" /></div>)}</div>;
  return <><div className={page.checkActions}><Button type="button" variant="ghost" onClick={onRerun} disabled={loading}><RefreshCw size={14} />{t("preflight.rerun")}</Button></div><div className={page.checks}>{data.checks.map((check) => { const details = check.details ?? {}; const downloadWarning = details.first_run_may_download_model === true; const lexiconName = typeof details.file_name === "string" ? details.file_name : undefined; return <div className={page.check} key={check.key}><div><strong>{check.label}</strong><p>{check.detail ?? t("preflight.noIssues")}</p>{downloadWarning && <small className={page.checkDetail}>{t("preflight.modelDownload")}</small>}{lexiconName && <small className={page.checkDetail}>{t("preflight.lexiconFile", { file: lexiconName })}</small>}{check.status === "block" && <small className={page.remediation}><b>{t("preflight.remediation")}:</b> {remediation(check.key, t)}</small>}</div><StatusPill status={check.status} /></div>; })}</div><div className={page.profileAction}><Button type="button" disabled={profile.isPending} onClick={() => profile.mutate()}>{profile.isPending ? <RefreshCw size={14} className="spin" /> : <Play size={14} />}{t("preflight.profile")}</Button>{profile.data && <StatusPill status={profile.data.status} label={profile.data.detail} />}</div></>;
}

function remediation(key: string, t: ReturnType<typeof useLocale>["t"]): string { const lower = key.toLowerCase(); if (lower.includes("source") || lower.includes("manifest")) return t("preflight.fix.source"); if (lower.includes("profile") || lower.includes("chatgpt")) return t("preflight.fix.profile"); if (lower.includes("ffmpeg") || lower.includes("ffprobe")) return t("preflight.fix.ffmpeg"); if (lower.includes("cuda") || lower.includes("whisper")) return t("preflight.fix.cuda"); if (lower.includes("tts") || lower.includes("provider")) return t("preflight.fix.provider"); return t("preflight.fix.generic"); }

function PlanStep({ data, loading }: { data?: PlanResponse; loading: boolean }) {
  const { t } = useLocale();
  if (loading || !data) return <p>{t("review.building")}</p>;
  const command = data.command_preview.join(" ");
  const episodeNodes = data.stages.filter((stage) => stage.episode_key);
  const finalNodes = data.stages.filter((stage) => !stage.episode_key);
  const episodeGroups = new Map<string, typeof episodeNodes>();
  episodeNodes.forEach((stage) => episodeGroups.set(stage.episode_key!, [...(episodeGroups.get(stage.episode_key!) ?? []), stage]));
  return <><StatusPill status={data.can_start ? "pass" : "block"} label={data.can_start ? t("review.ready") : t("review.blocked")} /><div className={page.dagGroup}><div><p>{t(data.kind === "series" ? "review.episodeWork" : "review.singleWork")}</p>{episodeGroups.size > 0 ? <div className={page.episodePlan}>{Array.from(episodeGroups).map(([episodeKey, stages]) => <div className={page.episodePlanRow} key={episodeKey}><strong>{episodeKey}</strong><div>{stages.map((stage) => <span key={stage.key} className={page.dagStage}>{stage.label.replace(`${episodeKey} `, "")}</span>)}</div></div>)}</div> : <div className={page.dag}>{finalNodes.map((stage, index, all) => <span key={stage.key} style={{ display: "contents" }}><span className={page.dagStage}>{stage.label}</span>{index < all.length - 1 && <ChevronRight className={page.dagArrow} size={13} />}</span>)}</div>}</div>{episodeNodes.length > 0 && <div><p>{t("review.finalWork")}</p><div className={page.dag}>{finalNodes.map((stage, index) => <span key={stage.key} className={page.dagStage}>{stage.label}{index < finalNodes.length - 1 ? "  →" : ""}</span>)}</div></div>}</div><Field label={t("review.command")}><pre className={page.command}>{command}</pre></Field>{data.output_names && data.output_names.length > 0 && <Field label={t("review.outputs")}><ul className={page.outputList}>{data.output_names.map((output) => <li key={output}>{output}</li>)}</ul></Field>}{data.dry_run_summary && <details style={{ marginTop: 14 }}><summary>{t("review.output")}</summary><pre className={page.command}>{data.dry_run_summary}</pre></details>}{data.warnings.length > 0 && <div className={page.checks} style={{ marginTop: 14 }}>{data.warnings.map((warning) => <div className={page.check} key={warning}><p>{warning}</p><StatusPill status="warn" /></div>)}</div>}</>;
}

function FileBrowser({ open, mode, kind, onClose, onSelect }: { open: boolean; mode: "source" | "runDir"; kind: RunKind; onClose: () => void; onSelect: (entry: FsEntry) => void }) {
  const { t } = useLocale();
  const roots = useQuery({ queryKey: ["fs-roots"], queryFn: api.roots, enabled: open });
  const [root, setRoot] = useState<FsRoot>();
  const [stack, setStack] = useState<{ token?: string; name: string }[]>([]);
  const currentToken = stack.at(-1)?.token;
  const listing = useQuery<FsListing>({ queryKey: ["fs-listing", root?.id, currentToken], queryFn: () => api.listing(root!.id, currentToken), enabled: open && Boolean(root), retry: 1 });
  const entries = useMemo(() => listing.data?.entries.filter((entry) => mode === "runDir" ? entry.kind === "directory" : entry.kind === "directory" || (kind === "series" ? /\.ya?ml$/i.test(entry.suffix ?? entry.extension ?? entry.name) : /\.(mp4|mkv|mov|webm)$/i.test(entry.suffix ?? entry.extension ?? entry.name))) ?? [], [listing.data, kind, mode]);
  const currentName = listing.data?.current.name ?? stack.at(-1)?.name ?? root?.label;
  const choose = (entry: FsEntry) => { if (entry.kind === "directory") { setStack((value) => [...value, { token: entry.token, name: entry.name }]); return; } onSelect(entry); };
  const chooseCurrent = () => { if (root && listing.data) onSelect({ token: listing.data.current.token || root.token, name: currentName ?? root.label, kind: "directory" }); };
  return <Dialog.Root open={open} onOpenChange={(next) => { if (!next) onClose(); }}><Dialog.Portal><Dialog.Overlay className={ui.dialogOverlay} /><Dialog.Content className={ui.dialogContent}><div className={ui.dialogHead}><Dialog.Title>{mode === "source" ? t("picker.sourceTitle") : t("picker.outputTitle")}</Dialog.Title><Dialog.Description>{t("picker.description")}</Dialog.Description></div><div className={ui.dialogBody}><Field htmlFor="allowed-root" label={t("picker.root")}><select id="allowed-root" className={selectClass} value={root?.id ?? ""} onChange={(event) => { const next = roots.data?.find((item) => item.id === event.target.value); setRoot(next); setStack([]); }}><option value="">{t("picker.selectRoot")}</option>{roots.data?.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></Field>{root && <div className={page.breadcrumbs} aria-label="Breadcrumb"><button type="button" onClick={() => setStack([])}>{root.label}</button>{stack.map((item, index) => <span key={item.token ?? item.name}><ChevronRight size={12} /><button type="button" onClick={() => setStack((value) => value.slice(0, index + 1))}>{item.name}</button></span>)}</div>}{root && mode === "runDir" && <Button type="button" variant="ghost" onClick={chooseCurrent} style={{ marginTop: 10 }}><Check size={13} />{t("picker.selectCurrent")}</Button>}<div className={page.checks} style={{ marginTop: 12, maxHeight: 330, overflow: "auto" }}>{listing.isLoading && <p>{t("picker.loading")}</p>}{listing.isError && <div className={page.pickerState}><p>{t("picker.error")}</p><Button type="button" variant="ghost" onClick={() => void listing.refetch()}><RefreshCw size={13} />{t("picker.retry")}</Button></div>}{!listing.isLoading && !listing.isError && root && entries.length === 0 && <p className={page.pickerState}>{t("picker.empty")}</p>}{entries.map((entry) => <div key={entry.token} className={page.check}><div><strong>{entry.name}</strong><p>{entry.kind === "directory" ? t("picker.directory") : `${entry.suffix ?? entry.extension ?? t("picker.file")} / ${formatBytes(entry.size)}`}</p></div><div className={page.entryActions}>{entry.kind === "directory" ? <Button type="button" variant="ghost" onClick={() => choose(entry)}><ChevronRight size={14} />{t("picker.open")}</Button> : <Button type="button" onClick={() => onSelect(entry)}><Check size={14} />{t("picker.select")}</Button>}</div></div>)}</div></div><div className={ui.dialogActions}><Dialog.Close asChild><Button type="button" variant="ghost">{t("picker.cancel")}</Button></Dialog.Close></div></Dialog.Content></Dialog.Portal></Dialog.Root>;
}
