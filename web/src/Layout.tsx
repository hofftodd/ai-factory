// ai-swap's layout: a sidebar (projects and nodes), the page in the middle, and an
// activity pane on the right for whatever project is selected.
import { useMemo, useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { EventLog } from "./components";
import { useEventStream } from "./hooks";
import { Counts, PHASE_LABEL, ProgressBar, planned } from "./projectBits";
import type { Machine, Project, SystemInfo } from "./types";

const STALE_S = 10;

/** localStorage that never throws (private windows, blocked storage). */
export const store = {
  get(key: string, fallback: string): string {
    try {
      return localStorage.getItem(key) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key: string, value: string): void {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* not persisted */
    }
  },
};

/** One word and a dot colour for a project in the sidebar. */
export function projectState(p: Project): { dot: string; text: string } {
  const c = p.task_counts ?? {};
  if (p.paused) return { dot: "paused", text: "paused" };
  if ((p.pending_gates ?? 0) > 0) return { dot: "waiting", text: "needs you" };
  if ((c.failed ?? 0) + (c.blocked ?? 0) > 0 && !p.active_task) return { dot: "failed", text: "stuck" };
  if (p.active_task || p.phase === "decomposing" || p.phase === "environment") return { dot: "busy", text: PHASE_LABEL[p.phase] };
  if (p.phase === "done") return { dot: "ready", text: "done" };
  return { dot: "idle", text: PHASE_LABEL[p.phase] };
}

function Health({ sys }: { sys: SystemInfo }) {
  return (
    <div className="side-health">
      {["orchestrator", "supervisor"].map((c) => {
        const age = sys.heartbeats[c];
        const ok = age !== undefined && age < STALE_S;
        return (
          <span key={c} title={age === undefined ? "never seen" : `${age}s ago`}>
            <span className={`sdot ${ok ? "ready" : "failed"}`} />
            {c}
          </span>
        );
      })}
      <span>{sys.runtime}</span>
    </div>
  );
}

export function Sidebar({
  sys,
  projects,
  machines,
  selected,
  onNavigate,
}: {
  sys: SystemInfo | null;
  projects: Project[] | null;
  machines: Machine[] | null;
  selected: number | null;
  onNavigate: () => void;
}) {
  const [filter, setFilter] = useState("");
  const navigate = useNavigate();
  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return (projects ?? []).filter((p) => !q || p.name.toLowerCase().includes(q) || p.repo_url.toLowerCase().includes(q));
  }, [projects, filter]);

  return (
    <aside id="side" aria-label="Projects and nodes">
      <div className="side-head">
        <Link to="/" className="brand" onClick={onNavigate}>
          <img src="/favicon.svg" alt="" />
          ai-factory
        </Link>
      </div>
      {sys && <Health sys={sys} />}
      <div className="filter">
        <input
          type="search"
          placeholder="Filter projects"
          aria-label="Filter projects"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>

      <div className="side-title">
        Projects
        <button
          className="btn small"
          onClick={() => {
            navigate("/?new=1");
            onNavigate();
          }}
        >
          + New
        </button>
      </div>
      <nav className="side-list" aria-label="Projects">
        {projects === null && <div className="side-empty">Loading…</div>}
        {projects?.length === 0 && <div className="side-empty">No projects yet.</div>}
        {projects && projects.length > 0 && shown.length === 0 && <div className="side-empty">No match.</div>}
        {shown.map((p) => {
          const st = projectState(p);
          const { done, total } = planned(p);
          return (
            <NavLink
              key={p.id}
              to={`/projects/${p.id}`}
              className="side-row"
              aria-current={p.id === selected ? "page" : undefined}
              onClick={onNavigate}
              title={`${p.name}: ${st.text}`}
            >
              <span className={`sdot ${st.dot}`} aria-hidden="true" />
              <span className="side-name">{p.name}</span>
              {(p.pending_gates ?? 0) > 0 ? (
                <span className="pill warn" title="decisions waiting for you">
                  {p.pending_gates}
                </span>
              ) : total ? (
                <span className="side-meta">
                  {done}/{total}
                </span>
              ) : null}
              <span className="side-sub">{p.active_task ? `▶ ${p.active_task}` : st.text}</span>
            </NavLink>
          );
        })}
      </nav>

      <div className="side-title">
        Machines
        <NavLink to="/nodes" className="side-link" onClick={onNavigate}>
          Nodes
        </NavLink>
      </div>
      <div className="side-list machines" aria-label="Machines">
        {machines?.length === 0 && <div className="side-empty">No nodes yet.</div>}
        {machines?.map((m) => {
          const mem = m.budget_gb ? Math.min(1, m.used_gb / m.budget_gb) : 0;
          return (
            <div key={m.name} className="machine">
              <Link to="/nodes" className="side-row machine-head" onClick={onNavigate} title={m.reserved ? `reserved by ${m.reserved}` : m.name}>
                <svg className="srv" viewBox="0 0 16 16" aria-hidden="true">
                  <rect x="2" y="2.5" width="12" height="4.5" rx="1.2" fill="none" stroke="currentColor" strokeWidth="1.3" />
                  <rect x="2" y="9" width="12" height="4.5" rx="1.2" fill="none" stroke="currentColor" strokeWidth="1.3" />
                  <circle cx="4.8" cy="4.75" r=".9" fill="currentColor" />
                  <circle cx="4.8" cy="11.25" r=".9" fill="currentColor" />
                </svg>
                <span className="side-name">{m.name}</span>
                <span className="side-meta">
                  {m.reserved ? <span className="pill warn">reserved</span> : m.ai_swap && m.budget_gb ? `${m.used_gb.toFixed(0)} / ${m.budget_gb.toFixed(0)} GB` : ""}
                </span>
                {m.ai_swap && m.budget_gb > 0 && (
                  <span className="side-bar" aria-hidden="true">
                    <i style={{ width: `${mem * 100}%` }} />
                  </span>
                )}
              </Link>
              {m.nodes.map((n) => {
                const dot =
                  n.healthy === false ? "failed" : n.model_state === "ready" ? (n.active ? "busy" : "ready") : n.model_state === "starting" ? "busy" : "idle";
                const word = n.healthy === false ? "unreachable" : n.model_state === "ready" ? "loaded" : n.model_state === "starting" ? "loading" : m.ai_swap ? "not loaded" : "";
                return (
                  <Link key={n.id} to="/nodes" className="side-row model" onClick={onNavigate} title={`${n.name}${n.models[0] ? ` (${n.models[0]})` : ""}${word ? `: ${word}` : ""}`}>
                    <span className={`sdot ${dot}`} aria-hidden="true" />
                    <span className="side-name">{n.name}</span>
                    <span className="side-meta">
                      {n.active}/{n.capacity}
                      {n.waiting ? ` +${n.waiting}` : ""}
                    </span>
                  </Link>
                );
              })}
            </div>
          );
        })}
      </div>
    </aside>
  );
}

/** The right-hand pane: the selected project's progress and live activity, or everything's. */
export function ActivityPane({ project, onClose }: { project: Project | null; onClose: () => void }) {
  const { events, connected } = useEventStream(project ? { project_id: project.id } : {});
  // Without a project, only the headline events: phases, gates, stops, system notes.
  const shown = project ? events : events.filter((e) => e.kind !== "log" && e.kind !== "diff" && e.kind !== "test").slice(-300);
  return (
    <aside id="activity" aria-label="Activity">
      <div className="act-head">
        <h2>{project ? project.name : "Activity"}</h2>
        <span className={`sdot ${connected ? "ready" : "idle"}`} title={connected ? "live" : "reconnecting"} />
        <button className="btn small ghost close-pane" onClick={onClose} aria-label="Close activity">
          ✕
        </button>
      </div>
      {project ? (
        <div className="act-summary">
          <div className="act-line">
            <span className={`badge phase-${project.phase}`}>{PHASE_LABEL[project.phase]}</span>
            {project.paused && <span className="badge state-paused">paused</span>}
            {(project.pending_gates ?? 0) > 0 && (
              <Link to="/inbox" className="badge state-paused">
                {project.pending_gates} need you
              </Link>
            )}
          </div>
          {project.active_task && <div className="small">▶ {project.active_task}</div>}
          <ProgressBar p={project} />
          <Counts p={project} />
        </div>
      ) : (
        <p className="muted small act-summary">Phases, decisions and stops across every project. Pick a project for its full log.</p>
      )}
      <EventLog events={shown} empty={project ? "No activity yet." : "Nothing has happened yet."} />
    </aside>
  );
}
