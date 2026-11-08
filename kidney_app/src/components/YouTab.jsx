import React, { useEffect, useState } from "react";
import Markdown from "./Markdown";
import { api } from "../api/kidney";

const SECTIONS = [["board", "My board"], ["meds", "Medications"], ["food", "Nutrition"], ["visit", "Visit prep"]];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const NUTRIENTS = [["sodium_mg", "Sodium", "mg"], ["potassium_mg", "Potassium", "mg"], ["phosphorus_mg", "Phosphorus", "mg"],
                   ["protein_g", "Protein", "g"], ["fluids_ml", "Fluids", "ml"]];
const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in local time
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return d.toLocaleDateString("en-CA"); };

export default function YouTab() {
  const [section, setSection] = useState("board");
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const reload = () => api("GET", "/you/{me}").then(setData).catch((e) => setError(e.message));
  useEffect(() => { reload(); }, []);

  return (
    <div className="you">
      <nav className="segments" aria-label="You sections">
        {SECTIONS.map(([id, label]) => (
          <button key={id} className={section === id ? "active" : ""} aria-current={section === id ? "page" : undefined}
                  onClick={() => setSection(id)}>{label}</button>
        ))}
      </nav>
      {error && <p className="error" role="alert">{error}</p>}
      {!data && !error && <p className="muted">Loading…</p>}
      {data && section === "board" && <Board board={data.board} retention={data.retention} onChange={reload} />}
      {data && section === "meds" && <Meds data={data} onChange={reload} />}
      {data && section === "food" && <Nutrition onChange={reload} />}
      {data && section === "visit" && <VisitPrep />}
    </div>
  );
}

function Board({ board, retention, onChange }) {
  const unpin = async (id) => { await api("DELETE", `/pins/${id}`).catch(() => {}); onChange(); };
  const pins = board?.pinned_answers || [];
  return (
    <section className="panel">
      <h2>My board</h2>
      <p className="muted">{retention}</p>
      {pins.length === 0 && board?.saved_resources?.length === 0 && (
        <p className="empty-note">Nothing pinned yet. Use "Pin to my board" under any answer to keep it here.</p>
      )}
      {pins.map((p) => (
        <article key={p.pin_id} className="pin">
          <h3>{p.title}</h3>
          {p.note && <p className="muted">Note: {p.note}</p>}
          <details>
            <summary>Show the answer</summary>
            <Markdown text={p.snapshot?.answer_display} citePrefix={`pin-${p.pin_id}`} />
            {p.snapshot?.citations?.length > 0 && (
              <ol className="sources">
                {p.snapshot.citations.map((c) => (
                  <li key={c.n} id={`pin-${p.pin_id}-${c.n}`}>
                    <span className="cite static" aria-hidden="true">{c.n}</span>
                    <span>{c.url ? <a href={c.url} target="_blank" rel="noreferrer">{c.title}</a> : c.title}
                      <span className="kind">{c.kind}</span></span>
                  </li>
                ))}
              </ol>
            )}
          </details>
          <p className="fine">Pinned {p.created_at?.slice(0, 10)}</p>
          <button className="btn link" onClick={() => unpin(p.pin_id)}>Unpin</button>
        </article>
      ))}
      {board?.saved_resources?.length > 0 && (
        <>
          <h3>Saved resources</h3>
          <ul>{board.saved_resources.map((r) => (
            <li key={r.saved_id}>{r.url ? <a href={r.url} target="_blank" rel="noreferrer">{r.title}</a> : r.title}</li>
          ))}</ul>
        </>
      )}
    </section>
  );
}

function Meds({ data, onChange }) {
  const [form, setForm] = useState({ name: "", dose_text: "", times: ["08:00"], days: [], reminders: true });
  const [msg, setMsg] = useState(null);
  const [inter, setInter] = useState(null);
  const [extra, setExtra] = useState("");
  const [checking, setChecking] = useState(false);

  const mark = async (d, status) => {
    await api("POST", `/you/meds/${d.med_id}/dose`, { body: { scheduled_for: d.scheduled_for, status } }).catch(() => {});
    onChange();
  };
  const add = async (e) => {
    e.preventDefault();
    setMsg(null);
    try {
      const r = await api("POST", "/you/meds", { body: { ...form, days: form.days.length ? form.days : null } });
      setMsg(r.recognized ? `Added. Recognized as ${r.ingredient}.`
                          : "Added. We couldn’t match this name to a known drug, so interaction checks will skip it.");
      setForm({ name: "", dose_text: "", times: ["08:00"], days: [], reminders: true });
      onChange();
    } catch (err) { setMsg(err.message); }
  };
  const remove = async (id) => { await api("DELETE", `/you/meds/${id}`).catch(() => {}); onChange(); };
  const check = async () => {
    setChecking(true);
    try { setInter(await api("GET", "/you/{me}/interactions", { query: extra ? { drug: extra } : {} })); }
    catch (err) { setInter({ error: err.message }); }
    finally { setChecking(false); }
  };
  const toggleDay = (i) => setForm((f) => ({ ...f, days: f.days.includes(i) ? f.days.filter((d) => d !== i) : [...f.days, i] }));

  return (
    <>
      <section className="panel">
        <h2>Today</h2>
        {data.todays_doses.length === 0 && <p className="empty-note">No doses scheduled today. Add your medications below.</p>}
        <ul className="doses">
          {data.todays_doses.map((d) => (
            <li key={d.med_id + d.time} className={`dose ${d.status}`}>
              <span className="time">{d.time}</span>
              <span className="what"><strong>{d.name}</strong> {d.dose_text}</span>
              {d.status === "taken" || d.status === "skipped"
                ? <span className="state">{d.status === "taken" ? "Taken" : "Skipped"}</span>
                : <span className="dose-actions">
                    <button className="btn small primary" onClick={() => mark(d, "taken")}>Taken</button>
                    <button className="btn small quiet" onClick={() => mark(d, "skipped")}>Skip</button>
                  </span>}
            </li>
          ))}
        </ul>
      </section>

      <section className="panel">
        <h2>My medications</h2>
        <ul className="med-list">
          {data.medications.map((m) => (
            <li key={m.med_id}>
              <span><strong>{m.name}</strong>{m.ingredient && m.ingredient !== m.name.toLowerCase() && <span className="muted"> ({m.ingredient})</span>}
                {" "}{m.dose_text}, {m.times.join(", ")}{m.days ? ` on ${m.days.map((d) => WEEKDAYS[d]).join(", ")}` : ""}
                {!m.reminders && <span className="muted"> (no reminders)</span>}</span>
              <button className="btn link" onClick={() => remove(m.med_id)}>Remove</button>
            </li>
          ))}
        </ul>

        <form className="stack" onSubmit={add}>
          <h3>Add a medication</h3>
          <label>Name<input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="For example, Prograf" /></label>
          <label>Dose, exactly as your prescription says
            <input required value={form.dose_text} onChange={(e) => setForm({ ...form, dose_text: e.target.value })} placeholder="For example, 1 mg capsule" /></label>
          <p className="fine">The app stores your dose as you enter it. It never suggests or changes doses.</p>
          <fieldset>
            <legend>Times</legend>
            {form.times.map((t, i) => (
              <span key={i} className="time-row">
                <input type="time" value={t} aria-label={`Time ${i + 1}`}
                       onChange={(e) => setForm({ ...form, times: form.times.map((x, j) => (j === i ? e.target.value : x)) })} />
                {form.times.length > 1 && <button type="button" className="btn link" onClick={() => setForm({ ...form, times: form.times.filter((_, j) => j !== i) })}>Remove</button>}
              </span>
            ))}
            <button type="button" className="btn link" onClick={() => setForm({ ...form, times: [...form.times, "20:00"] })}>Add another time</button>
          </fieldset>
          <fieldset>
            <legend>Days (leave blank for every day)</legend>
            <div className="days">{WEEKDAYS.map((d, i) => (
              <label key={d} className="day"><input type="checkbox" checked={form.days.includes(i)} onChange={() => toggleDay(i)} />{d}</label>
            ))}</div>
          </fieldset>
          <label className="row"><input type="checkbox" checked={form.reminders} onChange={(e) => setForm({ ...form, reminders: e.target.checked })} /> Email me reminders</label>
          <button className="btn primary">Add medication</button>
          {msg && <p className="muted" role="status">{msg}</p>}
        </form>
      </section>

      <section className="panel">
        <h2>Check for interactions</h2>
        <p className="muted">Checks your list against FDA drug labels. Add a medicine you’re thinking about, or leave it blank to check your list.</p>
        <div className="inline-form">
          <label className="sr-only" htmlFor="extra-drug">Another medicine (optional)</label>
          <input id="extra-drug" value={extra} onChange={(e) => setExtra(e.target.value)} placeholder="Another medicine (optional)" />
          <button className="btn primary" onClick={check} disabled={checking}>{checking ? "Checking…" : "Check"}</button>
        </div>
        {inter?.error && <p className="error">{inter.error}</p>}
        {inter && !inter.error && (
          <div className="inter">
            {inter.flagged_pairs === 0
              ? <p>No interactions named in the FDA labels for {inter.medications_checked.join(", ")}.</p>
              : inter.pairs.filter((p) => p.flagged).map((p) => (
                  <div key={p.drugs.join("+")} className="flag">
                    <h3>{p.drugs[0]} and {p.drugs[1]}</h3>
                    {p.label_flags.slice(0, 2).map((f, i) => (
                      <blockquote key={i}>"{f.quote}" <span className="fine">FDA label for {f.label_of}</span></blockquote>
                    ))}
                    {p.faers_reported_together_women > 0 && (
                      <p className="fine">Reported together in {p.faers_reported_together_women.toLocaleString()} FDA adverse event reports for women. This doesn’t show the drugs interact.</p>
                    )}
                  </div>
                ))}
            {inter.not_checked?.length > 0 && <p className="fine">Not checked: {inter.not_checked.join(", ")}.</p>}
            {inter.notices?.map((n) => <p key={n} className="notice">{n}</p>)}
          </div>
        )}
      </section>
    </>
  );
}

function Nutrition() {
  const [form, setForm] = useState({ day: today(), meal_type: "lunch", description: "" });
  const [result, setResult] = useState(null);
  const [week, setWeek] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const loadWeek = () => api("GET", "/you/{me}/nutrition", { query: { day_from: daysAgo(6), day_to: today() } }).then(setWeek).catch(() => {});
  useEffect(() => { loadWeek(); }, []);

  const log = async (e) => {
    e.preventDefault();
    setSaving(true); setError(null);
    try {
      setResult(await api("POST", "/you/meals", { body: form }));
      setForm({ ...form, description: "" });
      loadWeek();
    } catch (err) { setError(err.message); }
    finally { setSaving(false); }
  };

  return (
    <>
      <section className="panel">
        <h2>Log a meal</h2>
        <form className="stack" onSubmit={log}>
          <div className="row-2">
            <label>Date<input type="date" value={form.day} onChange={(e) => setForm({ ...form, day: e.target.value })} /></label>
            <label>Meal<select value={form.meal_type} onChange={(e) => setForm({ ...form, meal_type: e.target.value })}>
              {["breakfast", "lunch", "dinner", "snack"].map((m) => <option key={m} value={m}>{m[0].toUpperCase() + m.slice(1)}</option>)}
            </select></label>
          </div>
          <label>What did you eat and drink?
            <textarea rows={3} required value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
                      placeholder="For example, turkey sandwich, an apple, and two cups of coffee" /></label>
          <button className="btn primary" disabled={saving}>{saving ? "Adding…" : "Add meal"}</button>
        </form>
        {error && <p className="error">{error}</p>}
        {result && (
          <div className="meal-result">
            <Totals totals={result.totals} />
            {result.unmatched?.length > 0 && <p className="fine">Couldn’t find nutrition data for: {result.unmatched.join(", ")}.</p>}
            <p className="notice">{result.notice}</p>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>This week</h2>
        {!week?.days?.length && <p className="empty-note">No meals logged this week.</p>}
        {week?.days?.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th scope="col">Day</th>{NUTRIENTS.map(([k, label, unit]) => <th key={k} scope="col">{label} ({unit})</th>)}</tr></thead>
              <tbody>{week.days.map((d) => (
                <tr key={d.day}><th scope="row">{d.day.slice(5)}</th>
                  {NUTRIENTS.map(([k]) => (
                    <td key={k}>{Math.round(d.totals[k])}{d.vs_targets[k] && <span className="fine"> / {d.vs_targets[k].target}</span>}</td>
                  ))}</tr>
              ))}</tbody>
            </table>
          </div>
        )}
        {week && !week.targets_set && <p className="fine">{week.targets_note} You can add them in your profile.</p>}
      </section>
    </>
  );
}

function Totals({ totals }) {
  return (
    <dl className="totals">
      {NUTRIENTS.map(([k, label, unit]) => (
        <div key={k}><dt>{label}</dt><dd>{Math.round(totals[k] || 0)} {unit}</dd></div>
      ))}
    </dl>
  );
}

function VisitPrep() {
  const [types, setTypes] = useState({});
  const [form, setForm] = useState({ visit_type: "nephrology", concerns: "", save: true });
  const [sheet, setSheet] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => { api("GET", "/you/visit-types").then(setTypes).catch(() => {}); }, []);

  const make = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try { setSheet(await api("POST", "/you/visit-prep", { body: form })); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };

  return (
    <section className="panel">
      <h2>Prepare for a visit</h2>
      {!sheet && (
        <form className="stack" onSubmit={make}>
          <label>Who are you seeing?
            <select value={form.visit_type} onChange={(e) => setForm({ ...form, visit_type: e.target.value })}>
              {Object.entries(types).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select></label>
          <label>Anything you want to bring up? (optional)
            <textarea rows={3} value={form.concerns} onChange={(e) => setForm({ ...form, concerns: e.target.value })}
                      placeholder="For example, my ankles have been swelling at night" /></label>
          <label className="row"><input type="checkbox" checked={form.save} onChange={(e) => setForm({ ...form, save: e.target.checked })} /> Save the sheet to my board</label>
          <button className="btn primary" disabled={busy}>{busy ? "Making your sheet…" : "Make my visit sheet"}</button>
          {error && <p className="error">{error}</p>}
        </form>
      )}
      {sheet && (
        <div className="sheet">
          <div className="sheet-actions no-print">
            <button className="btn primary" onClick={() => window.print()}>Print</button>
            <button className="btn quiet" onClick={() => setSheet(null)}>Make another</button>
            {sheet.pin_id && <span className="fine">Saved to your board.</span>}
          </div>
          <div className="printable"><Markdown text={sheet.markdown} /></div>
        </div>
      )}
    </section>
  );
}
