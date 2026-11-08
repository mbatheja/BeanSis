import React, { useEffect, useState } from "react";
import { api } from "../api/kidney";
import { base44 } from "../api/base44Client";
import ProfileQuestions from "./ProfileQuestions";
import { CA_INSURANCE_PLANS } from "../data/insurancePlans";

const TARGETS = [["sodium_mg", "Sodium (mg)"], ["potassium_mg", "Potassium (mg)"], ["phosphorus_mg", "Phosphorus (mg)"],
                 ["protein_g", "Protein (g)"], ["fluids_ml", "Fluids (ml)"]];

export function Profile() {
  const [p, setP] = useState(null);
  const [status, setStatus] = useState(null);

  useEffect(() => {
    api("GET", "/profile/{me}").then((d) => setP({
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, state: "CA", nutrition_targets: {}, ...d,
    })).catch((e) => setStatus(e.message));
  }, []);

  if (!p) return <section className="panel"><p className="muted">{status || "Loading…"}</p></section>;
  const set = (k, v) => setP((previous) => ({ ...previous, [k]: v }));
  const setTarget = (k, v) => {
    const t = { ...(p.nutrition_targets || {}) };
    if (v === "") delete t[k]; else t[k] = Number(v);
    set("nutrition_targets", t);
  };

  const save = async (e) => {
    e.preventDefault();
    setStatus(null);
    const body = { ...p, plan_id: p.plan_id === "other" ? null : p.plan_id, dialysis: !!p.dialysis,
      transplant: p.transplant_status ? p.transplant_status === "had_transplant" : !!p.transplant };
    if (!body.nutrition_targets || !Object.keys(body.nutrition_targets).length) delete body.nutrition_targets;
    try { await api("PUT", "/profile/{me}", { body }); setStatus("Saved."); }
    catch (err) { setStatus(err.message); }
  };

  const erase = async () => {
    if (!window.confirm("Delete all your chats, pinned answers, medications, meal logs, and profile? This can’t be undone.")) return;
    try { await api("DELETE", "/users/{me}/data"); base44.auth.logout("/"); }
    catch (err) { setStatus(err.message); }
  };

  return (
    <section className="panel">
      <h2>My profile</h2>
      <p className="muted">This helps the app tailor answers and find doctors and rides near you. You can update these details any time.</p>
      <form className="stack" onSubmit={save}>
        <ProfileQuestions p={p} set={set} plans={CA_INSURANCE_PLANS} />

        <fieldset>
          <legend>Daily targets from your dietitian (optional)</legend>
          <p className="fine">Only add numbers your care team gave you. The app never sets targets.</p>
          <div className="targets">{TARGETS.map(([k, label]) => (
            <label key={k}>{label}<input type="number" min="0" value={p.nutrition_targets?.[k] ?? ""} onChange={(e) => setTarget(k, e.target.value)} /></label>
          ))}</div>
        </fieldset>
        <button className="btn primary">Save profile</button>
        {status && <p className="muted" role="status">{status}</p>}
      </form>

      <div className="account">
        <button className="btn quiet" onClick={() => base44.auth.logout("/")}>Sign out</button>
        <button className="btn danger" onClick={erase}>Delete all my data</button>
      </div>
    </section>
  );
}

export function Privacy() {
  return (
    <section className="panel prose">
      <h2>Privacy and your data</h2>
      <p>This app is a prototype for health education. It doesn’t replace your care team, and it can’t diagnose or treat anything. In an emergency, call 911.</p>
      <h3>What we keep, and for how long</h3>
      <ul>
        <li>Chats: deleted 30 days after you last use them.</li>
        <li>Answers you pin to your board: kept until you unpin them.</li>
        <li>Your medication list: kept until you remove a medication.</li>
        <li>Meal logs and doses you mark: deleted after 90 days.</li>
        <li>Your profile: kept until you change it or delete your data.</li>
      </ul>
      <h3>What we don’t do</h3>
      <p>We don’t sell your information or share it with advertisers. Reminder emails say only the time, never the names of your medicines.</p>
      <h3>Deleting your data</h3>
      <p>Go to your profile and choose "Delete all my data". Everything listed above is erased right away.</p>
      <h3>Sources</h3>
      <p>Answers draw on FDA drug labels and adverse event reports, PubMed research, clinical guidelines, ClinicalTrials.gov, the national provider registry, Medi-Cal and Medicare pages, and patient reviews from Drugs.com (DrugRev dataset, CC BY 4.0).</p>
    </section>
  );
}
