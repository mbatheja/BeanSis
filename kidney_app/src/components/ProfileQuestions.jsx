import React from "react";

const STAGES = ["1", "2", "3a", "3b", "4", "5", "Not sure"];
const DISEASE_TYPES = [
  ["ckd", "Chronic kidney disease (CKD)"], ["aki", "Acute kidney injury"],
  ["glomerular", "Glomerular disease"], ["polycystic", "Polycystic kidney disease"],
  ["other", "Other"], ["unknown", "Not sure"],
];
const LANGUAGES = ["English", "Spanish", "Chinese", "Tagalog", "Vietnamese", "Hindi", "Arabic", "Korean", "Russian", "Other"];

export default function ProfileQuestions({ p, set, plans, required = false }) {
  const knownPlan = plans.some((plan) => plan.plan_id === p.plan_id);
  const planChoice = knownPlan ? p.plan_id : (p.plan_id === "none" || p.plan_id === "unknown" ? p.plan_id : (p.plan_id ? "other" : ""));
  const otherPlan = p.insurance_other || (!knownPlan && p.plan_id && !["none", "unknown", "other"].includes(p.plan_id) ? p.plan_id : "");
  const transplantStatus = p.transplant_status || (p.transplant ? "had_transplant" : "");
  const dialysisValue = p.dialysis === true ? "yes" : p.dialysis === false ? "no" : "";
  // A transplant waitlist is for chronic kidney failure; acute kidney injury is not a useful
  // disease-type choice in that context. Keep the other underlying diagnoses available.
  const diseaseTypes = transplantStatus === "waitlist"
    ? DISEASE_TYPES.filter(([value]) => value !== "aki")
    : DISEASE_TYPES;

  return (
    <>
      <label>Name<input autoComplete="name" value={p.name || ""} onChange={(e) => set("name", e.target.value)} required={required} /></label>
      <label>Age (select your date of birth)<input type="date" max={new Date().toISOString().slice(0, 10)} value={p.birth_date || ""} onChange={(e) => set("birth_date", e.target.value)} required={required} /></label>
      <label>ZIP code<input inputMode="numeric" autoComplete="postal-code" maxLength={5} pattern="[0-9]{5}" value={p.zip || ""} onChange={(e) => set("zip", e.target.value.replace(/\D/g, ""))} required={required} /></label>
      <label>Insurance plan
        <select value={planChoice} onChange={(e) => {
          set("plan_id", e.target.value);
          if (e.target.value !== "other") set("insurance_other", "");
        }} required={required}>
          <option value="">Choose your plan</option>
          {plans.map((plan) => <option key={plan.plan_id} value={plan.plan_id}>{plan.insurer} {plan.plan_name}</option>)}
          <option value="none">No insurance</option><option value="unknown">Not sure</option><option value="other">Other / not listed</option>
        </select>
      </label>
      {planChoice === "other" && <label>Insurance plan name<input value={otherPlan} onChange={(e) => set("insurance_other", e.target.value)} required={required} /></label>}
      <label>Kidney transplant status<select value={transplantStatus} onChange={(e) => {
        const nextStatus = e.target.value;
        set("transplant_status", nextStatus); set("transplant", nextStatus === "had_transplant");
        if (nextStatus === "waitlist" && p.kidney_disease_type === "aki") set("kidney_disease_type", "");
      }} required={required}>
        <option value="">Choose one</option><option value="none">No transplant</option>
        <option value="had_transplant">I’ve had a transplant</option><option value="waitlist">I’m on the transplant list</option>
        <option value="unknown">Not sure</option>
      </select></label>
      {transplantStatus && <>
        <label>Kidney disease type<select value={p.kidney_disease_type || ""} onChange={(e) => {
          const type = e.target.value;
          set("kidney_disease_type", type);
          if (type !== "ckd") set("ckd_stage", "");
        }} required={required}>
          <option value="">Choose one</option>{diseaseTypes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        {p.kidney_disease_type === "ckd" && <label>Chronic kidney disease stage<select value={p.ckd_stage || ""} onChange={(e) => set("ckd_stage", e.target.value)} required={required}>
          <option value="">Choose one</option>{STAGES.map((stage) => <option key={stage} value={stage}>{stage}</option>)}
        </select></label>}
      </>}
      <label>Are you on dialysis?<select value={dialysisValue} onChange={(e) => set("dialysis", e.target.value === "" ? null : e.target.value === "yes")} required={required}>
        <option value="">Choose one</option><option value="yes">Yes</option><option value="no">No</option>
      </select></label>
      <label>Pregnancy goals<select value={p.life_stage || ""} onChange={(e) => set("life_stage", e.target.value)} required={required}>
        <option value="">Choose one</option><option value="pregnant">Already pregnant</option>
        <option value="planning_pregnancy">Planning to get pregnant</option><option value="not_pregnant">No plans yet</option>
        <option value="postpartum">Recently gave birth</option>
      </select></label>
      <label>Primary language<select value={p.primary_language || ""} onChange={(e) => set("primary_language", e.target.value)} required={required}>
        <option value="">Choose one</option>{LANGUAGES.map((language) => <option key={language} value={language}>{language}</option>)}
      </select></label>
      <p className="fine">Your language answer helps us understand your preference. The app will continue to display in English.</p>
    </>
  );
}
