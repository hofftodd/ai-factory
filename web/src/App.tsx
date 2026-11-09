import { useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { control, getToken, setToken } from "./api";
import { ActionButton, StopButton } from "./components";
import { usePoll } from "./hooks";
import InboxPage from "./pages/InboxPage";
import NodesPage from "./pages/NodesPage";
import ProjectPage from "./pages/ProjectPage";
import ProjectsPage from "./pages/ProjectsPage";
import SettingsPage from "./pages/SettingsPage";
import TaskPage from "./pages/TaskPage";
import type { SystemInfo } from "./types";

const STALE_S = 10;

function Health({ sys }: { sys: SystemInfo }) {
  return (
    <div className="health">
      {["orchestrator", "supervisor"].map((c) => {
        const age = sys.heartbeats[c];
        const ok = age !== undefined && age < STALE_S;
        return (
          <span key={c} className={`dot ${ok ? "ok" : "bad"}`} title={age === undefined ? "never seen" : `${age}s ago`}>
            {c}
          </span>
        );
      })}
      <span className="muted">runtime: {sys.runtime}</span>
    </div>
  );
}

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

export default function App() {
  const { data: sys, error, reload } = usePoll<SystemInfo>("/api/system", 2000);
  const needsToken = error && error.includes("token");

  return (
    <div className={`app ${sys?.estop ? "estop-on" : ""}`}>
      <header className="topbar">
        <NavLink to="/" className="brand" end>
          <img src="/favicon.svg" alt="" />
          ai-factory
        </NavLink>
        <nav>
          <NavLink to="/" end>
            Projects
          </NavLink>
          <NavLink to="/inbox">
            Needs you
            {!!sys?.pending_gates && <span className="count">{sys.pending_gates}</span>}
          </NavLink>
          <NavLink to="/nodes">Nodes</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        <div className="spacer" />
        {sys && <Health sys={sys} />}
        {sys?.estop ? (
          <ActionButton kind="primary" onClick={async () => { await control("global", "clear_estop"); reload(); }}>
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
        <div className="estop-banner">
          EMERGENCY STOP ACTIVE — all agents killed, nothing will start until cleared.
        </div>
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
  );
}
