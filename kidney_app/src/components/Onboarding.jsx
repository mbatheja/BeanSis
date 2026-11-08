import React, { useState } from "react";
import { api } from "../api/kidney";
import ProfileQuestions from "./ProfileQuestions";
import { CA_INSURANCE_PLANS } from "../data/insurancePlans";

export default function Onboarding({ onComplete }) {
  const [p, setP] = useState({ timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, state: "CA" });
  const [status, setStatus] = useState(null);

  const set = (key, value) => setP((previous) => ({ ...previous, [key]: value }));
  const save = async (e) => {
    e.preventDefault();
    setStatus(null);
    const body = { ...p, plan_id: p.plan_id === "other" ? null : p.plan_id,
      onboarding_complete: true, dialysis: !!p.dialysis, transplant: !!p.transplant };
    try {
      const saved = await api("PUT", "/profile/{me}", { body });
      onComplete({ ...p, ...saved, name: p.name, onboarding_complete: true });
    } catch (error) {
      setStatus(error.message);
    }
  };

  return (
    <main className="onboarding page">
      <section className="panel onboarding-panel">
        <p className="eyebrow">Bean Sis · Getting started</p>
        <h1>Tell us a little about you</h1>
        <p className="muted">These answers help personalize kidney health information and local resources. You can update them later in your profile.</p>
        <form className="stack" onSubmit={save}>
          <ProfileQuestions p={p} set={set} plans={CA_INSURANCE_PLANS} required />
          <button className="btn primary large">Save and continue</button>
          {status && <p className="error" role="alert">{status}</p>}
        </form>
      </section>
    </main>
  );
}
