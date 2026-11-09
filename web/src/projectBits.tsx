// Project bits shared by the projects page, the sidebar and the activity pane.
import type { Project } from "./types";

export const PHASE_LABEL: Record<Project["phase"], string> = {
  specify: "specifying",
  decomposing: "planning",
  plan_review: "plan review",
  environment: "preparing environment",
  execute: "executing",
  done: "done",
};

export function planned(p: Project) {
  const c = p.task_counts ?? {};
  const total = Object.entries(c).reduce((n, [k, v]) => (k === "discarded" || k === "split" ? n : n + (v ?? 0)), 0);
  return { done: c.done ?? 0, total };
}

export function Counts({ p }: { p: Project }) {
  const c = p.task_counts ?? {};
  const active = (c.running ?? 0) + (c.validating ?? 0);
  return (
    <div className="counts">
      <span>{c.done ?? 0} done</span>
      <span className={active ? "hot" : ""}>{active} running</span>
      <span>{c.queued ?? 0} queued</span>
      {(c.failed ?? 0) > 0 && <span className="warn">{c.failed} failed</span>}
      {(c.stopped_by_user ?? 0) > 0 && <span className="warn">{c.stopped_by_user} stopped</span>}
      {(c.blocked ?? 0) > 0 && <span className="warn">{c.blocked} blocked</span>}
    </div>
  );
}

export function ProgressBar({ p }: { p: Project }) {
  const { done, total } = planned(p);
  if (!total) return null;
  return (
    <div title={`${done} of ${total} tasks done`}>
      <div className="progress">
        <div className="progress-fill" style={{ width: `${(done / total) * 100}%` }} />
      </div>
      <div className="small muted">
        {done} of {total} tasks done
        {p.coverage != null && ` · ${Math.round(p.coverage)}% test coverage`}
        {p.spend_usd > 0 && ` · $${p.spend_usd.toFixed(2)} spent`}
      </div>
    </div>
  );
}
