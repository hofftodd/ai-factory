import { useState } from "react";
import { del, post, put } from "../api";
import { ActionButton, ErrorNote, Field } from "../components";
import { usePoll } from "../hooks";
import type { GitHubTokenStatus } from "../types";

// Fine-grained tokens can be limited to the repositories ai-factory works on; the classic
// link pre-fills the one scope it needs.
const NEW_FINE_GRAINED = "https://github.com/settings/personal-access-tokens/new";
const NEW_CLASSIC = "https://github.com/settings/tokens/new?scopes=repo&description=ai-factory";

export default function SettingsPage() {
  const { data: gh, error, reload } = usePoll<GitHubTokenStatus>("/api/settings/github", 10000);
  const [token, setTokenValue] = useState("");
  const [formError, setFormError] = useState("");
  const [check, setCheck] = useState<GitHubTokenStatus | null>(null);

  return (
    <div>
      <div className="page-head">
        <h1>Settings</h1>
      </div>
      <ErrorNote error={error} />
      <form
        className="card form"
        onSubmit={async (e) => {
          e.preventDefault();
          setFormError("");
          setCheck(null);
          try {
            await put("/api/settings/github", { token });
            setTokenValue("");
            setCheck(await post<GitHubTokenStatus>("/api/settings/github/check"));
            reload();
          } catch (err) {
            setFormError((err as Error).message);
          }
        }}
      >
        <h2>GitHub token</h2>
        <p className="muted">
          Used to clone private repositories, push the milestone branch, open the pull request and create repositories
          for new projects.
          It is never passed to executors or written to <code>.git/config</code>.
        </p>
        <p className="small">
          <a href={NEW_FINE_GRAINED} target="_blank" rel="noreferrer">
            Create a fine-grained token on GitHub
          </a>{" "}
          with <b>Contents</b> and <b>Pull requests</b> set to read and write for your repositories, or a{" "}
          <a href={NEW_CLASSIC} target="_blank" rel="noreferrer">
            classic token
          </a>{" "}
          with the <code>repo</code> scope.
        </p>
        <p className="small muted">
          To create repositories for new projects from ai-factory, a fine-grained token also needs{" "}
          <b>Administration</b> (read and write) and access to <b>all repositories</b>; a classic token's{" "}
          <code>repo</code> scope already covers it.
        </p>
        <div className="small">
          {!gh ? (
            <span className="muted">loading…</span>
          ) : gh.configured ? (
            <>
              Token ending in <code>{gh.hint}</code>, set{" "}
              {gh.source === "dashboard" ? "here" : <>by <code>AI_FACTORY_GITHUB_TOKEN</code></>}
            </>
          ) : (
            <span className="muted">No token configured</span>
          )}
        </div>
        {check && (
          <div className={`dot ${check.ok ? "ok" : "bad"}`}>
            {check.ok ? `Works: signed in as ${check.login}${check.scopes ? ` (scopes: ${check.scopes})` : ""}` : check.error}
          </div>
        )}
        <Field
          label={gh?.configured ? "Replace token" : "Token"}
          hint={gh?.source === "environment" ? "A token saved here takes precedence over AI_FACTORY_GITHUB_TOKEN" : undefined}
        >
          <input
            type="password"
            className="mono"
            autoComplete="off"
            value={token}
            placeholder="github_pat_… or ghp_…"
            onChange={(e) => setTokenValue(e.target.value)}
          />
        </Field>
        <ErrorNote error={formError} />
        <div className="actions">
          <button className="btn primary" disabled={!token.trim()}>
            Save
          </button>
          {gh?.configured && (
            <ActionButton onClick={async () => setCheck(await post<GitHubTokenStatus>("/api/settings/github/check"))}>
              Test
            </ActionButton>
          )}
          {gh?.source === "dashboard" && (
            <ActionButton
              kind="danger"
              onClick={async () => {
                if (!confirm("Remove the saved GitHub token?")) return;
                await del("/api/settings/github");
                setCheck(null);
                reload();
              }}
            >
              Remove
            </ActionButton>
          )}
        </div>
      </form>
    </div>
  );
}
