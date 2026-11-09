import { useEffect, useState } from "react";
import { matchPath, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { control, getToken, setToken } from "./api";
import { ActionButton, StopButton } from "./components";
import { usePoll } from "./hooks";
import { ActivityPane, projectState, Sidebar, store } from "./Layout";
import { PHASE_LABEL } from "./projectBits";
import InboxPage from "./pages/InboxPage";
import NodesPage from "./pages/NodesPage";
import ProjectPage from "./pages/ProjectPage";
import ProjectsPage from "./pages/ProjectsPage";
import SettingsPage from "./pages/SettingsPage";
import TaskPage from "./pages/TaskPage";
import type { Machine, Project, SystemInfo, Task } from "./types";

function TokenPrompt({ onSaved }: { onSaved: () => void }) {
  const [value, setValue] = useState(getToken());
  return (
    <div className="card narrow">
      <h2>API token required</h2>
      <p className="muted">This server was started with AI_FACTORY_TOKEN. Enter it to continue.</p>
      <input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
      <button
        className="btn primary"
        onClick={() => {
          setToken(value);
          onSaved();
        }}
      >
        Save
      </button>
    </div>
  );
}

/** The project the current page is about: /projects/:id, or the project of /tasks/:id. */
function useSelectedProject(): number | null {
  const { pathname } = useLocation();
  const proj = matchPath("/projects/:id", pathname);
  const task = matchPath("/tasks/:id", pathname);
  const { data } = usePoll<Task>(task ? `/api/tasks/${task.params.id}` : null, 30000);
  if (proj) return Number(proj.params.id);
  if (task && data) return data.project_id;
  return null;
}

export default function App() {
  const { data: sys, error, reload } = usePoll<SystemInfo>("/api/system", 2000);
  const needsToken = !!error && error.includes("token");
  const { data: projects } = usePoll<Project[]>(needsToken ? null : "/api/projects", 3000);
  const { data: machines } = usePoll<Machine[]>(needsToken ? null : "/api/machines", 5000);
  const selectedId = useSelectedProject();
  const selected = projects?.find((p) => p.id === selectedId) ?? null;

  // The activity pane is on by default on wide screens, remembered per browser; on narrow
  // screens it (and the sidebar) open over the page.
  const [activity, setActivity] = useState(
    () => store.get("aifactory.activity", window.innerWidth > 1100 ? "1" : "0") === "1",
  );
  const [sideOpen, setSideOpen] = useState(false);
  const { pathname } = useLocation();
  useEffect(() => setSideOpen(false), [pathname]);
  const toggleActivity = () =>
    setActivity((a) => {
      store.set("aifactory.activity", a ? "0" : "1");
      return !a;
    });

  const st = selected ? projectState(selected) : null;
  return (
    <div
      className={`app${sys?.estop ? " estop-on" : ""}${activity ? "" : " no-activity"}${sideOpen ? " side-open" : ""}`}
    >
      <Sidebar
        sys={sys}
        projects={needsToken ? [] : projects}
        machines={machines}
        selected={selectedId}
        onNavigate={() => setSideOpen(false)}
      />
      <div className="main-col">
        <header className="topbar">
          <button className="btn small" id="sideToggle" aria-label="Open projects and nodes" onClick={() => setSideOpen(true)}>
            ☰
          </button>
          {selected && st && (
            <NavLink to={`/projects/${selected.id}`} className="chip" title={`${selected.name}: ${st.text}`}>
              <span className={`sdot ${st.dot}`} aria-hidden="true" />
              <span className="cname">{selected.name}</span>
              <span className="cstate">{PHASE_LABEL[selected.phase]}</span>
            </NavLink>
          )}
          <span className="spacer" />
          <nav aria-label="Pages">
            <NavLink to="/" end>
              Overview
            </NavLink>
            <NavLink to="/inbox">
              Needs you
              {!!sys?.pending_gates && <span className="count">{sys.pending_gates}</span>}
            </NavLink>
            <NavLink to="/nodes">Nodes</NavLink>
            <NavLink to="/settings">Settings</NavLink>
          </nav>
          <button
            className="btn small"
            id="activityToggle"
            aria-pressed={activity}
            aria-controls="activity"
            onClick={toggleActivity}
            title="Activity pane"
          >
            <span aria-hidden="true">ⓘ</span> <span className="lbl">Activity</span>
          </button>
          {sys?.estop ? (
            <ActionButton
              kind="primary"
              onClick={async () => {
                await control("global", "clear_estop");
                reload();
              }}
            >
              Clear E-stop
            </ActionButton>
          ) : (
            <StopButton
              size="big"
              label="E-STOP ALL"
              onStop={async () => {
                await control("global", "estop");
                reload();
              }}
            />
          )}
        </header>
        {sys?.estop && (
          <div className="estop-banner">EMERGENCY STOP ACTIVE — all agents killed, nothing will start until cleared.</div>
        )}
        <main>
          {needsToken ? (
            <TokenPrompt onSaved={reload} />
          ) : (
            <Routes>
              <Route path="/" element={<ProjectsPage />} />
              <Route path="/projects/:id" element={<ProjectPage />} />
              <Route path="/tasks/:id" element={<TaskPage />} />
              <Route path="/nodes" element={<NodesPage />} />
              <Route path="/inbox" element={<InboxPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<p>Not found.</p>} />
            </Routes>
          )}
          {error && !needsToken && <div className="error-note">Backend error: {error}</div>}
        </main>
      </div>
      {activity && !needsToken && <ActivityPane project={selected} onClose={toggleActivity} />}
      <div id="backdrop" onClick={() => (sideOpen ? setSideOpen(false) : toggleActivity())} />
    </div>
  );
}
