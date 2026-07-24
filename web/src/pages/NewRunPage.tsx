import * as Dialog from "@radix-ui/react-dialog";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, ChevronRight, FileVideo, Folder, Layers3, Play, RefreshCw, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { PageHeader } from "../components/AppShell";
import { Button, ErrorNotice, Field, StatusPill, inputClass, selectClass } from "../components/Ui";
import type { FsEntry, FsRoot, PlanResponse, PreflightResponse, RunKind } from "../types";
import { formatBytes } from "../utils";
import page from "./pages.module.css";
import ui from "../components/ui.module.css";

const STEPS = [
  { label: "Source", hint: "Mode and local media" },
  { label: "Preset", hint: "Safe production controls" },
  { label: "Preflight", hint: "Runtime readiness" },
  { label: "Dry run", hint: "Verify DAG and launch" },
];

interface WizardState {
  kind: RunKind;
  title: string;
  sourceToken: string;
  sourceName: string;
  presetId: string;
  outputParentToken: string;
  outputParentName: string;
  runName: string;
  runDirToken: string;
  runDirName: string;
  episodes: string;
  targetRatio: string;
  logLevel: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  voiceId: string;
  ttsSpeed: number;
  ttsConcurrency: number;
  renderCrf: number;
  renderPreset: "slow" | "medium" | "fast";
  renderConcurrency: number;
  seriesMin: number;
  seriesMax: number;
  seriesCap: number;
  arcSize: number;
}

const initialState: WizardState = {
  kind: "series", title: "", sourceToken: "", sourceName: "", presetId: "", outputParentToken: "", outputParentName: "", runName: "", runDirToken: "", runDirName: "", episodes: "1-12", targetRatio: "auto", logLevel: "INFO", voiceId: "", ttsSpeed: 1, ttsConcurrency: 3, renderCrf: 20, renderPreset: "medium", renderConcurrency: 4, seriesMin: 2100, seriesMax: 2700, seriesCap: 3000, arcSize: 3,
};

export function NewRunPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [state, setState] = useState(initialState);
  const [browser, setBrowser] = useState<"source" | "runDir" | null>(null);
  const [preflight, setPreflight] = useState<PreflightResponse>();
  const [plan, setPlan] = useState<PlanResponse>();
  const presets = useQuery({ queryKey: ["presets"], queryFn: api.presets });
  const profile = useMutation({ mutationFn: api.testProfile });
  const preflightMutation = useMutation({ mutationFn: (input: WizardState) => api.preflight(buildPayload(input)), onSuccess: setPreflight });
  const planMutation = useMutation({ mutationFn: (input: WizardState) => api.plan(input.kind, buildPayload(input)), onSuccess: setPlan });
  const createMutation = useMutation({ mutationFn: () => api.createJob(plan!.plan_id), onSuccess: (job) => navigate(`/runs/${job.run_id ?? job.id}`) });
  const availablePresets = presets.data?.filter((preset) => !preset.kind || preset.kind === "both" || preset.kind === state.kind) ?? [];
  const validRunName = /^[A-Za-z0-9][A-Za-z0-9._-]{1,79}$/.test(state.runName);
  const canAdvance = step === 0 ? Boolean(state.sourceToken && state.outputParentToken && state.title.trim() && validRunName) : step === 1 ? Boolean(state.presetId && safeConfigValid(state)) : step === 2 ? Boolean(preflight?.can_start) : Boolean(plan?.can_start);

  const resolveRunDirectory = async (): Promise<WizardState> => {
    if (state.runDirToken) return state;
    const child = await api.childToken(state.outputParentToken, state.runName);
    const next = { ...state, runDirToken: child.token, runDirName: child.name };
    setState(next);
    return next;
  };

  const goNext = async () => {
    if (step === 1) { const next = await resolveRunDirectory(); await preflightMutation.mutateAsync(next); setStep(2); return; }
    if (step === 2) { const next = await resolveRunDirectory(); await planMutation.mutateAsync(next); setStep(3); return; }
    setStep((current) => Math.min(current + 1, STEPS.length - 1));
  };

  return <>
    <PageHeader eyebrow="New operation" title="Configure a run" subtitle="The wizard validates local resources and shows the exact CLI plan before anything enters the queue." />
    <div className={page.wizard}>
      <aside className={page.steps}>{STEPS.map((item, index) => <div key={item.label} className={`${page.step} ${index === step ? page.stepActive : ""} ${index < step ? page.stepDone : ""}`}><span className={page.stepNumber}>{index < step ? <Check size={13} /> : index + 1}</span><div><strong>{item.label}</strong><small>{item.hint}</small></div></div>)}</aside>
      <div className={page.wizardContent}>
        <header className={page.wizardHead}><h2>{STEPS[step]!.label}</h2><p>{STEPS[step]!.hint}</p></header>
        <div className={page.wizardBody}>
          {step === 0 && <SourceStep state={state} setState={setState} onBrowse={setBrowser} />}
          {step === 1 && <PresetStep state={state} setState={setState} presets={availablePresets} loading={presets.isLoading} />}
          {step === 2 && <PreflightStep data={preflight} loading={preflightMutation.isPending} profile={profile} />}
          {step === 3 && <PlanStep data={plan} loading={planMutation.isPending} />}
          {(preflightMutation.error || planMutation.error || createMutation.error) && <div style={{ marginTop: 14 }}><ErrorNotice error={preflightMutation.error ?? planMutation.error ?? createMutation.error} /></div>}
        </div>
        <footer className={page.wizardFoot}><Button variant="ghost" disabled={step === 0 || createMutation.isPending} onClick={() => setStep((current) => Math.max(0, current - 1))}><ArrowLeft size={14} />Back</Button>{step < 3 ? <Button variant="primary" disabled={!canAdvance || preflightMutation.isPending || planMutation.isPending} onClick={() => void goNext()}>{preflightMutation.isPending || planMutation.isPending ? <RefreshCw className="spin" size={14} /> : null}{step === 2 ? "Build dry run" : "Continue"}<ArrowRight size={14} /></Button> : <Button variant="primary" large disabled={!plan?.can_start || createMutation.isPending} onClick={() => createMutation.mutate()}><Play size={15} />{createMutation.isPending ? "Queuing..." : "Start run"}</Button>}</footer>
      </div>
    </div>
    <FileBrowser open={browser !== null} mode={browser ?? "source"} kind={state.kind} onClose={() => setBrowser(null)} onSelect={(entry) => { if (browser === "source") setState((current) => ({ ...current, sourceToken: entry.token, sourceName: entry.name })); else setState((current) => ({ ...current, outputParentToken: entry.token, outputParentName: entry.name, runDirToken: "", runDirName: "" })); setBrowser(null); }} />
  </>;
}

function buildPayload(state: WizardState): Record<string, unknown> {
  return {
    kind: state.kind, run_dir_token: state.runDirToken, config_token: state.presetId,
    ...(state.kind === "series" ? { manifest_token: state.sourceToken, episodes: state.episodes, overrides: { orchestrator: { log_level: state.logLevel }, series_recap: { target_total_min_s: state.seriesMin, target_total_max_s: state.seriesMax, target_total_hard_cap_s: state.seriesCap, arc_size: state.arcSize }, tts: { ...(state.voiceId.trim() ? { voice_id: state.voiceId.trim() } : {}), speed: state.ttsSpeed, concurrency: state.ttsConcurrency }, render: { crf: state.renderCrf, preset: state.renderPreset, concurrency: state.renderConcurrency } } } : { source_token: state.sourceToken, overrides: { orchestrator: { log_level: state.logLevel }, review: { target_ratio: state.targetRatio }, tts: { ...(state.voiceId.trim() ? { voice_id: state.voiceId.trim() } : {}), speed: state.ttsSpeed, concurrency: state.ttsConcurrency }, render: { crf: state.renderCrf, preset: state.renderPreset, concurrency: state.renderConcurrency } } }),
  };
}

function safeConfigValid(state: WizardState): boolean {
  return state.ttsSpeed >= .8 && state.ttsSpeed <= 1.2 && state.ttsConcurrency >= 1 && state.ttsConcurrency <= 8 && state.renderCrf >= 16 && state.renderCrf <= 28 && state.renderConcurrency >= 1 && state.renderConcurrency <= 8 && (state.kind === "single" || (state.seriesMin <= state.seriesMax && state.seriesMax <= state.seriesCap && state.arcSize >= 1 && state.arcSize <= 6));
}

function SourceStep({ state, setState, onBrowse }: { state: WizardState; setState: React.Dispatch<React.SetStateAction<WizardState>>; onBrowse: (mode: "source" | "runDir") => void }) {
  const runNameError = state.runName && !/^[A-Za-z0-9][A-Za-z0-9._-]{1,79}$/.test(state.runName) ? "Use 2-80 letters, digits, dots, underscores or hyphens; start with a letter or digit." : undefined;
  return <><div className={page.choiceGrid}><button className={`${page.choice} ${state.kind === "single" ? page.choiceSelected : ""}`} onClick={() => setState((current) => ({ ...current, kind: "single", sourceToken: "", sourceName: "", presetId: "" }))}><FileVideo size={22} /><strong>Single video</strong><span>Run the six-stage recap DAG for one local source.</span></button><button className={`${page.choice} ${state.kind === "series" ? page.choiceSelected : ""}`} onClick={() => setState((current) => ({ ...current, kind: "series", sourceToken: "", sourceName: "", presetId: "" }))}><Layers3 size={22} /><strong>Season recap</strong><span>Process selected episodes, compose the season, then render.</span></button></div><div className={page.formGrid}><Field label="Run title"><input className={inputClass} value={state.title} placeholder="Solo Leveling Season 1" onChange={(event) => setState((current) => ({ ...current, title: event.target.value }))} /></Field>{state.kind === "series" && <Field label="Episodes" hint="Ranges such as 1-12 or 1,3,5-8"><input className={inputClass} value={state.episodes} onChange={(event) => setState((current) => ({ ...current, episodes: event.target.value }))} /></Field>}<div className={page.formFull}><Field label={state.kind === "series" ? "Series manifest" : "Source video"} hint="Local paths stay behind opaque filesystem tokens."><div className={page.pathRow}><input className={inputClass} readOnly value={state.sourceName} placeholder={state.kind === "series" ? "Select series_manifest.yaml" : "Select source video"} /><Button onClick={() => onBrowse("source")}><Search size={14} />Browse</Button></div></Field></div><Field label="Output parent" hint="The backend derives a token for the new child directory."><div className={page.pathRow}><input className={inputClass} readOnly value={state.outputParentName} placeholder="Select output parent" /><Button onClick={() => onBrowse("runDir")}><Folder size={14} />Browse</Button></div></Field><Field label="Run directory name" error={runNameError}><input className={inputClass} value={state.runName} placeholder="solo-leveling-s01" onChange={(event) => setState((current) => ({ ...current, runName: event.target.value, runDirToken: "", runDirName: "" }))} /></Field></div></>;
}

function PresetStep({ state, setState, presets, loading }: { state: WizardState; setState: React.Dispatch<React.SetStateAction<WizardState>>; presets: { id: string; token: string; name: string; description?: string }[]; loading: boolean }) {
  return <div className={page.formGrid}><div className={page.formFull}><Field label="Production preset"><select className={selectClass} disabled={loading} value={state.presetId} onChange={(event) => setState((current) => ({ ...current, presetId: event.target.value }))}><option value="">{loading ? "Loading presets..." : "Select preset"}</option>{presets.map((preset) => <option key={preset.id} value={preset.token}>{preset.name}</option>)}</select></Field></div><Field label="Log level"><select className={selectClass} value={state.logLevel} onChange={(event) => setState((current) => ({ ...current, logLevel: event.target.value as WizardState["logLevel"] }))}><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select></Field><Field label="TTS voice ID" hint="Leave blank to keep the preset voice."><input className={inputClass} value={state.voiceId} onChange={(event) => setState((current) => ({ ...current, voiceId: event.target.value }))} /></Field>{state.kind === "single" ? <Field label="Review target ratio"><select className={selectClass} value={state.targetRatio} onChange={(event) => setState((current) => ({ ...current, targetRatio: event.target.value }))}><option value="auto">Auto</option><option value="0.25">25%</option><option value="0.33">33%</option><option value="0.4">40%</option></select></Field> : <><Field label="Arc size"><input className={inputClass} type="number" min={1} max={6} value={state.arcSize} onChange={(event) => setState((current) => ({ ...current, arcSize: Number(event.target.value) }))} /></Field><div className={page.formFull}><div className={page.rangeRow}><Field label="Required minimum (s)"><input className={inputClass} type="number" value={state.seriesMin} onChange={(event) => setState((current) => ({ ...current, seriesMin: Number(event.target.value) }))} /></Field><Field label="Preferred maximum (s)"><input className={inputClass} type="number" value={state.seriesMax} onChange={(event) => setState((current) => ({ ...current, seriesMax: Number(event.target.value) }))} /></Field><Field label="Advisory cap (s)"><input className={inputClass} type="number" value={state.seriesCap} onChange={(event) => setState((current) => ({ ...current, seriesCap: Number(event.target.value) }))} /></Field></div></div></>}<Field label="TTS speed"><input className={inputClass} type="number" min={.8} max={1.2} step={.05} value={state.ttsSpeed} onChange={(event) => setState((current) => ({ ...current, ttsSpeed: Number(event.target.value) }))} /></Field><Field label="TTS concurrency"><input className={inputClass} type="number" min={1} max={8} value={state.ttsConcurrency} onChange={(event) => setState((current) => ({ ...current, ttsConcurrency: Number(event.target.value) }))} /></Field><Field label="Render CRF"><input className={inputClass} type="number" min={16} max={28} value={state.renderCrf} onChange={(event) => setState((current) => ({ ...current, renderCrf: Number(event.target.value) }))} /></Field><Field label="Render preset"><select className={selectClass} value={state.renderPreset} onChange={(event) => setState((current) => ({ ...current, renderPreset: event.target.value as WizardState["renderPreset"] }))}><option>slow</option><option>medium</option><option>fast</option></select></Field><Field label="Render concurrency"><input className={inputClass} type="number" min={1} max={8} value={state.renderConcurrency} onChange={(event) => setState((current) => ({ ...current, renderConcurrency: Number(event.target.value) }))} /></Field>{!safeConfigValid(state) && <div className={page.formFull}><ErrorNotice error={new Error("Safe overrides are outside the allowed range or series durations are inconsistent.")} /></div>}</div>;
}

function PreflightStep({ data, loading, profile }: { data?: PreflightResponse; loading: boolean; profile: ReturnType<typeof useMutation<{ status: "pass" | "warn" | "block"; detail?: string }, Error, void>> }) {
  if (loading || !data) return <div className={page.checks}>{Array.from({ length: 6 }, (_, index) => <div className={page.check} key={index}><div><strong>Checking local runtime...</strong><p>Inspecting dependency and resource readiness.</p></div><StatusPill status="running" /></div>)}</div>;
  return <><div className={page.checks}>{data.checks.map((check) => <div className={page.check} key={check.key}><div><strong>{check.label}</strong><p>{check.detail ?? "No issues detected."}</p></div><StatusPill status={check.status} /></div>)}</div><div style={{ marginTop: 14, display: "flex", alignItems: "center", gap: 10 }}><Button disabled={profile.isPending} onClick={() => profile.mutate()}>{profile.isPending ? <RefreshCw size={14} /> : <Play size={14} />}Test ChatGPT profile</Button>{profile.data && <StatusPill status={profile.data.status} label={profile.data.detail ?? profile.data.status} />}</div></>;
}

function PlanStep({ data, loading }: { data?: PlanResponse; loading: boolean }) {
  if (loading || !data) return <p>Building dry-run plan...</p>;
  const command = Array.isArray(data.command) ? data.command.join(" ") : data.command;
  return <><StatusPill status={data.can_start ? "pass" : "block"} label={data.can_start ? "Ready to enqueue" : "Plan blocked"} /><div className={page.dag}>{data.stages.map((stage, index) => <span key={`${stage.key}-${stage.episode_key ?? "final"}`} style={{ display: "contents" }}><span className={page.dagStage}>{stage.episode_key ? `${stage.episode_key} / ` : ""}{stage.label}</span>{index < data.stages.length - 1 && <ChevronRight className={page.dagArrow} size={13} />}</span>)}</div><pre className={page.command}>{command}</pre>{data.dry_run_output && <details style={{ marginTop: 14 }}><summary>Dry-run output</summary><pre className={page.command}>{data.dry_run_output}</pre></details>}{data.warnings.length > 0 && <div className={page.checks} style={{ marginTop: 14 }}>{data.warnings.map((warning) => <div className={page.check} key={warning}><p>{warning}</p><StatusPill status="warn" /></div>)}</div>}</>;
}

function FileBrowser({ open, mode, kind, onClose, onSelect }: { open: boolean; mode: "source" | "runDir"; kind: RunKind; onClose: () => void; onSelect: (entry: FsEntry) => void }) {
  const roots = useQuery({ queryKey: ["fs-roots"], queryFn: api.roots, enabled: open });
  const [root, setRoot] = useState<string>();
  const [stack, setStack] = useState<{ token?: string; name: string }[]>([]);
  const current = stack.at(-1)?.token;
  const entries = useQuery({ queryKey: ["fs-entries", root, current], queryFn: () => api.entries(root!, current), enabled: open && Boolean(root) });
  const allowed = useMemo(() => entries.data?.filter((entry) => mode === "runDir" ? entry.kind === "directory" : entry.kind === "directory" || (kind === "series" ? /\.ya?ml$/i.test(entry.name) : /\.(mp4|mkv|mov|webm)$/i.test(entry.name))) ?? [], [entries.data, kind, mode]);
  const choose = (entry: FsEntry) => { if (entry.kind === "directory" && mode === "source") { setStack((value) => [...value, { token: entry.token, name: entry.name }]); return; } onSelect(entry); };

  return <Dialog.Root open={open} onOpenChange={(next) => { if (!next) onClose(); }}><Dialog.Portal><Dialog.Overlay className={ui.dialogOverlay} /><Dialog.Content className={ui.dialogContent}><div className={ui.dialogHead}><Dialog.Title>Choose local {mode === "source" ? "source" : "run directory"}</Dialog.Title><Dialog.Description>Only allowlisted roots are exposed. Paths never travel to the browser.</Dialog.Description></div><div className={ui.dialogBody}><Field label="Allowed root"><select className={selectClass} value={root ?? ""} onChange={(event) => { setRoot(event.target.value); setStack([]); }}><option value="">Select root</option>{roots.data?.map((item: FsRoot) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></Field>{stack.length > 0 && <Button variant="ghost" style={{ margin: "12px 0 7px" }} onClick={() => setStack((value) => value.slice(0, -1))}><ArrowLeft size={13} />Up</Button>}<div className={page.checks} style={{ marginTop: 12, maxHeight: 330, overflow: "auto" }}>{entries.isLoading && <p>Loading...</p>}{allowed.map((entry) => <button key={entry.token} className={page.check} style={{ color: "inherit", cursor: "pointer", textAlign: "left" }} onClick={() => choose(entry)}><div><strong>{entry.name}</strong><p>{entry.kind === "directory" ? "Directory" : `${entry.extension ?? "file"} / ${formatBytes(entry.size)}`}</p></div>{entry.kind === "directory" && mode === "source" ? <ChevronRight size={15} /> : <Check size={15} />}</button>)}</div></div><div className={ui.dialogActions}><Dialog.Close asChild><Button variant="ghost">Cancel</Button></Dialog.Close></div></Dialog.Content></Dialog.Portal></Dialog.Root>;
}
