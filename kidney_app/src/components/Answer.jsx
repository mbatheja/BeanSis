import React, { useState } from "react";
import Markdown from "./Markdown";
import { api } from "../api/kidney";

const CHECK_LABELS = {
  safety_check: "Emergency check",
  citation_check: "Sources check",
  dosing_check: "Dosing check",
  tool_caveats: "Data caveats",
  self_correction: "Self-correction",
};
const STATUS_TONE = {
  passed: "ok", applied: "ok", not_needed: "ok", not_applicable: "quiet", revised: "ok",
  modified: "warn", warning: "warn", flagged: "warn", kept_original: "warn", triggered: "urgent",
};
const STATUS_TEXT = {
  passed: "Passed", applied: "Added", not_needed: "Not needed", not_applicable: "Not needed",
  revised: "Rewritten", modified: "Changed", warning: "Needs care", flagged: "Flagged",
  kept_original: "Partly fixed", triggered: "Urgent",
};

export function UrgentAnswer({ text }) {
  return (
    <div className="urgent" role="alert">
      <p className="urgent-text">{text}</p>
      <div className="urgent-actions">
        <a className="btn urgent-btn" href="tel:911">Call 911</a>
        <a className="btn urgent-btn secondary" href="tel:988">Call or text 988</a>
      </div>
    </div>
  );
}

function Checks({ guardrails = [] }) {
  const attention = guardrails.some((g) => ["warn", "urgent"].includes(STATUS_TONE[g.status]));
  const [open, setOpen] = useState(false);
  return (
    <div className="checks">
      <div className="checks-summary">
        <button
          type="button"
          className="checks-toggle"
          aria-label={open ? "Hide answer checks" : "Show answer checks"}
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <span className="checks-arrow" aria-hidden="true" />
        </button>
        <span className="checks-title">How this answer was checked</span>
        {attention && <span className="pill warn">Read this</span>}
      </div>
      {open && <ul>
        {guardrails.map((g) => (
          <li key={g.name}>
            <span className={`pill ${STATUS_TONE[g.status] || "quiet"}`}>{STATUS_TEXT[g.status] || g.status}</span>
            <span className="check-name">{CHECK_LABELS[g.name] || g.name}</span>
            <span className="check-detail">{g.detail}</span>
          </li>
        ))}
      </ul>}
    </div>
  );
}

export default function Answer({ msg, onOpenRelated }) {
  const [pinned, setPinned] = useState(false);
  const [pinError, setPinError] = useState(null);
  const prefix = `src-${msg.message_id || "x"}`;

  if (msg.urgent || msg.guardrails?.some((g) => g.name === "safety_check" && g.status === "triggered")) {
    return <UrgentAnswer text={msg.answer_display || msg.content} />;
  }

  const pin = async () => {
    setPinError(null);
    try {
      await api("POST", "/pins", { body: { message_id: msg.message_id } });
      setPinned(true);
    } catch (e) {
      setPinError(e.message);
    }
  };

  return (
    <article className="answer">
      {msg.related_history?.length > 0 && (
        <div className="related">
          <p>You looked into this before:</p>
          <ul>
            {msg.related_history.map((r) => (
              <li key={(r.pin_id || r.thread_id) + r.kind}>
                <button className="link" onClick={() => onOpenRelated?.(r)}>
                  {r.kind === "pinned" ? "Pinned: " : ""}{r.text}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <Markdown text={msg.answer_display || msg.content} citePrefix={prefix} />

      {msg.citations?.length > 0 && (
        <ol className="sources" aria-label="Sources">
          {msg.citations.map((c) => (
            <li key={c.n} id={`${prefix}-${c.n}`}>
              <span className="cite static" aria-hidden="true">{c.n}</span>
              <span>
                {c.url ? <a href={c.url} target="_blank" rel="noreferrer">{c.title}</a> : c.title}
                <span className="kind">{c.kind}</span>
              </span>
            </li>
          ))}
        </ol>
      )}

      {msg.notices?.map((n, i) => <p key={i} className="notice">{n}</p>)}

      {msg.guardrails?.length > 0 && <Checks guardrails={msg.guardrails} />}

      {msg.message_id && (
        <div className="answer-actions">
          <button className="btn quiet" onClick={pin} disabled={pinned}>
            {pinned ? "Pinned to your board" : "Pin to my board"}
          </button>
          {pinError && <span className="error-inline">{pinError}</span>}
        </div>
      )}
    </article>
  );
}
