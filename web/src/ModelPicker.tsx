// "Which model does this role use": one menu of models, grouped by the machine serving them.
// Each choice is a node and a model together. A node behind ai-swap stands for one model;
// any other node (Claude, a plain server) offers each of its models.
import { usePoll } from "./hooks";
import type { Machine } from "./types";

export type ModelChoice = { node_id: string; model: string };

const keyOf = (nodeId: string | number, model: string) => `${nodeId}|${model}`;

export function ModelPicker({
  value,
  onChange,
  ariaLabel,
}: {
  value: ModelChoice;
  onChange: (c: ModelChoice) => void;
  ariaLabel?: string;
}) {
  const { data: machines } = usePoll<Machine[]>("/api/machines", 15000);
  const groups = (machines ?? [])
    .map((m) => ({
      machine: m.name,
      items: m.nodes.flatMap((n) =>
        m.ai_swap || n.models.length === 0
          ? [{ node: n, model: n.model }]
          : n.models.map((model) => ({ node: n, model })),
      ),
    }))
    .filter((g) => g.items.length);
  const current = value.node_id ? keyOf(value.node_id, value.model) : "";
  const known = groups.some((g) => g.items.some((i) => keyOf(i.node.id, i.model) === current));

  return (
    <select
      aria-label={ariaLabel}
      value={current}
      onChange={(e) => {
        const v = e.target.value;
        if (!v) return onChange({ node_id: "", model: "" });
        const [nodeId, ...rest] = v.split("|");
        onChange({ node_id: nodeId, model: rest.join("|") });
      }}
    >
      <option value="">— none —</option>
      {current && !known && (
        // A binding made before (or a model no machine lists right now): keep it selectable.
        <option value={current}>{value.model || "(no model)"}</option>
      )}
      {groups.map((g) => (
        <optgroup key={g.machine} label={g.machine}>
          {g.items.map((i) => {
            const label = i.model || i.node.name;
            const named = i.model && !i.model.toLowerCase().includes(i.node.name.toLowerCase());
            return (
              <option key={keyOf(i.node.id, i.model)} value={keyOf(i.node.id, i.model)}>
                {label}
                {named ? ` · ${i.node.name}` : ""}
                {i.node.provider === "anthropic" ? " (Claude)" : ""}
              </option>
            );
          })}
        </optgroup>
      ))}
    </select>
  );
}

/** Further models a coding agent may use, after the first: work goes to the first with a
 * free slot, an already loaded model before one that must load. */
export function MoreModels({
  label,
  value,
  onChange,
}: {
  label: string;
  value: ModelChoice[];
  onChange: (v: ModelChoice[]) => void;
}) {
  return (
    <div className="more-models">
      {value.map((c, i) => (
        <div className="inline" key={i}>
          <span className="muted small then">then</span>
          <ModelPicker
            ariaLabel={`${label} choice ${i + 2}`}
            value={c}
            onChange={(n) => onChange(value.map((x, j) => (j === i ? n : x)))}
          />
          <button
            type="button"
            className="btn small ghost"
            aria-label={`Remove ${label} choice ${i + 2}`}
            onClick={() => onChange(value.filter((_, j) => j !== i))}
          >
            ✕
          </button>
        </div>
      ))}
      <button type="button" className="btn small" onClick={() => onChange([...value, { node_id: "", model: "" }])}>
        + Another model
      </button>
    </div>
  );
}
