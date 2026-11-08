import React, { useEffect, useState } from "react";
import { api } from "../api/kidney";

export default function RideFinder() {
  const [answers, setAnswers] = useState({});
  const [step, setStep] = useState(null);
  const [zip, setZip] = useState("");
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);

  const advance = async (next) => {
    setError(null);
    try {
      const res = await api("POST", "/resources/ride", { body: { answers: next } });
      setAnswers(next);
      setStep(res);
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => { advance({}); }, []);

  const answer = (field, value) => advance({ ...answers, [field]: value });
  const restart = () => { setZip(""); setCopied(false); advance({}); };

  if (error) return <div className="panel"><p className="error" role="alert">{error}</p><button className="btn quiet" onClick={restart}>Try again</button></div>;
  if (!step) return <div className="panel"><p className="muted">Loading…</p></div>;

  if (!step.done) {
    const q = step.ask;
    return (
      <div className="panel ride">
        <h2>{q.question}</h2>
        {q.why && <p className="muted">{q.why}</p>}
        {q.options.length > 0 ? (
          <div className="choices">
            {q.options.map((o) => (
              <button key={o.value} className="choice" onClick={() => answer(q.field, o.value)}>{o.label}</button>
            ))}
          </div>
        ) : (
          <form className="inline-form" onSubmit={(e) => { e.preventDefault(); if (/^\d{5}$/.test(zip)) answer("zip", zip); }}>
            <label className="sr-only" htmlFor="ride-zip">ZIP code</label>
            <input id="ride-zip" inputMode="numeric" maxLength={5} value={zip} placeholder="ZIP code"
                   onChange={(e) => setZip(e.target.value.replace(/\D/g, ""))} />
            <button className="btn primary" disabled={!/^\d{5}$/.test(zip)}>Continue</button>
          </form>
        )}
        {Object.keys(answers).length > 0 && <button className="btn link" onClick={restart}>Start over</button>}
      </div>
    );
  }

  const c = step.card;
  const contact = c.contact || {};
  const copy = async () => {
    try { await navigator.clipboard.writeText(c.call_script); setCopied(true); } catch { /* clipboard blocked */ }
  };

  return (
    <div className="panel ride-card">
      <h2>{c.mode_label}</h2>

      {c.contact && (
        <div className="contact">
          {contact.tel ? (
            <>
              <a className="btn primary call" href={contact.tel}>Call {contact.label}</a>
              <p className="number">{contact.phone}</p>
              {contact.tty && <p className="muted">TTY {contact.tty}</p>}
              {contact.last_verified && <p className="fine">Number checked {contact.last_verified}</p>}
            </>
          ) : (
            <>
              <p className="contact-label">{contact.label}</p>
              {contact.how && <p>{contact.how}</p>}
              {contact.email && <p><a href={`mailto:${contact.email}`}>{contact.email}</a></p>}
              {contact.url && <p><a href={contact.url} target="_blank" rel="noreferrer">Open the official page</a></p>}
            </>
          )}
          {contact.other_phones?.length > 0 && (
            <div className="other-lines">
              {contact.other_phones.map((o) => (
                <a key={o.label} className="btn quiet" href={o.tel}>{o.label}: {o.phone}</a>
              ))}
            </div>
          )}
          {contact.alternate && (
            <div className="alternate">
              <p>{contact.alternate.how}</p>
              <a className="btn quiet" href={contact.alternate.tel}>Call {contact.alternate.label}</a>
              <p className="fine">{contact.alternate.hours} TTY {contact.alternate.tty}.</p>
            </div>
          )}
        </div>
      )}

      {c.call_script && (
        <section>
          <h3>What to say</h3>
          <blockquote className="script">{c.call_script}</blockquote>
          <button className="btn quiet" onClick={copy}>{copied ? "Copied" : "Copy what to say"}</button>
        </section>
      )}

      {c.requirements?.length > 0 && (
        <section>
          <h3>Before you call</h3>
          <ul className="checklist">{c.requirements.map((r) => <li key={r}>{r}</li>)}</ul>
        </section>
      )}

      {c.notes?.length > 0 && <section>{c.notes.map((n) => <p key={n}>{n}</p>)}</section>}

      <section>
        <h3>If that doesn’t work</h3>
        <ul className="fallbacks">
          {c.fallbacks.map((f) => (
            <li key={f.name}>
              <strong>{f.name}.</strong> {f.detail}{" "}
              {f.phone && <a href={`tel:${f.phone}`}>Call {f.phone}</a>}
              {f.url && !f.phone && <a href={f.url} target="_blank" rel="noreferrer">Website</a>}
            </li>
          ))}
        </ul>
      </section>

      <p className="fine">{c.disclaimer}</p>
      <p className="fine">Sources: {c.sources.map((s, i) => (
        <span key={s.source_id}>{i > 0 && ", "}<a href={s.url} target="_blank" rel="noreferrer">{s.title}</a></span>
      ))}</p>
      <button className="btn link" onClick={restart}>Start over</button>
    </div>
  );
}
