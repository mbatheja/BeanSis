// Mock @base44/sdk for local rendering tests. Responses mirror the real API's shapes (synthetic data only).
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const signedIn = !location.search.includes("signedout");

const ANSWER = {
  thread_id: "t1", message_id: "m1", urgent: false,
  answer: "x",
  answer_display:
    "Tacrolimus is listed on its FDA label with **tremor**, headache, and diarrhea among common side effects [1]. " +
    "Many transplant patients on Drugs.com describe hand tremor, often easing over the first months [2].\n\n" +
    "Some reviewers also mention hair loss. The FDA label doesn't list it, and a few studies discuss it in transplant recipients [3].\n\n" +
    "- Tell your transplant team about new or worsening tremor.\n- Don't change your dose on your own.",
  citations: [
    { n: 1, source_id: "LABEL:abc", title: "FDA label: tacrolimus", url: "https://dailymed.nlm.nih.gov/", kind: "FDA drug label" },
    { n: 2, source_id: "REVIEWS:tacrolimus", title: "Drugs.com patient reviews: tacrolimus (DrugRev dataset)", url: "https://doi.org/10.17632/64cc5w5dxy.1", kind: "Patient reviews (Drugs.com via DrugRev dataset, CC BY 4.0)" },
    { n: 3, source_id: "PMID:111", title: "Alopecia in solid organ transplant recipients on tacrolimus", url: "https://pubmed.ncbi.nlm.nih.gov/", kind: "Research article (PubMed)" },
  ],
  notices: [
    "Signals are compared across patient reviews, FDA adverse event reports, and the FDA label. None of these shows how often an effect happens.",
    "This is general health education, not medical advice for your situation.",
  ],
  guardrails: [
    { name: "safety_check", status: "passed", detail: "Checked for emergency or crisis language before answering." },
    { name: "citation_check", status: "passed", detail: "All 3 reference(s) matched sources retrieved for this answer." },
    { name: "dosing_check", status: "passed", detail: "No personal dosing instructions detected." },
    { name: "tool_caveats", status: "applied", detail: "drug_signal_profile" },
    { name: "self_correction", status: "revised", detail: "The first draft cited sources that weren't retrieved; it was rewritten using retrieved sources." },
  ],
  related_history: [{ kind: "pinned", tab: "medications", text: "Is tacrolimus safe during pregnancy?", pin_id: "p1", thread_id: "t0" }],
};

const SHEET = `# Visit prep: OB/GYN
_Prepared 2026-11-08 from my entries in the app over the last 30 days._

## About me
- Life stage: pregnant
- CKD stage: 3a
- Kidney transplant: Yes

## My medications (as I entered them)
- **Prograf** (tacrolimus): 1 mg capsule, 08:00, 20:00

Doses I marked: 12 taken, 1 skipped, 0 reminders not marked. _(Only what I marked in the app.)_

## Things to mention
- My ankles have been swelling at night for about two weeks.

## My questions
1. [ ] Does my kidney disease make this a high-risk pregnancy, and should I also see maternal-fetal medicine?
2. [ ] Should I take low-dose aspirin to lower my preeclampsia risk, and when would I start?
3. [ ] My ankles swell at night. Should we check anything? [1]

## Sources
1. KDIGO CKD guideline (Research / guideline)

---
This sheet lists your own entries and questions to ask. It isn't medical advice.`;

const ROUTES = [
  ["GET", /^\/threads$/, () => [{ thread_id: "t0", tab: "medications", title: "Is tacrolimus safe during pregnancy?" }]],
  ["GET", /^\/threads\/.+\/messages$/, () => []],
  ["GET", /^\/retention$/, () => ({ description: "Chats are deleted after 30 days without use. Pin an answer to keep it on your board." })],
  ["POST", /^\/chat\//, async () => { await delay(300); return ANSWER; }],
  ["POST", /^\/pins$/, () => ({ pin_id: "p2" })],
  ["GET", /^\/you\/\{me\}$/, () => ({
    board: { pinned_answers: [{ pin_id: "p1", title: "Is tacrolimus safe during pregnancy?", created_at: "2026-11-02T10:00:00",
             snapshot: { answer_display: "Pregnancy after transplant is possible with planning [1].", citations: [ANSWER.citations[0]] } }],
             saved_resources: [] },
    medications: [{ med_id: "a", name: "Prograf", ingredient: "tacrolimus", dose_text: "1 mg capsule", times: ["08:00", "20:00"], days: null, reminders: true },
                  { med_id: "b", name: "Zestril", ingredient: "lisinopril", dose_text: "10 mg tablet", times: ["08:00"], days: [0, 2, 4], reminders: false }],
    todays_doses: [{ time: "08:00", med_id: "a", name: "Prograf", dose_text: "1 mg capsule", status: "taken", scheduled_for: "2026-11-08T08:00" },
                   { time: "08:00", med_id: "b", name: "Zestril", dose_text: "10 mg tablet", status: "due", scheduled_for: "2026-11-08T08:00" },
                   { time: "20:00", med_id: "a", name: "Prograf", dose_text: "1 mg capsule", status: "upcoming", scheduled_for: "2026-11-08T20:00" }],
    nutrition_today: { days: [] },
    retention: "Chats are deleted after 30 days without use; meal and dose logs after 90 days. Pinned answers and your medication list stay until you remove them.",
  })],
  ["GET", /interactions$/, () => ({ medications_checked: ["tacrolimus", "fluconazole"], flagged_pairs: 1,
    pairs: [{ drugs: ["tacrolimus", "fluconazole"], flagged: true, faers_reported_together_women: 42,
              label_flags: [{ label_of: "tacrolimus", quote: "Azole antifungal agents such as fluconazole increase tacrolimus concentrations; monitor levels.", source_id: "LABEL:x" }] }],
    not_checked: [], notices: ["This check only finds interactions that FDA labels name directly. No flag does not mean no interaction."] })],
  ["GET", /nutrition$/, () => ({ days: [{ day: "2026-11-07", totals: { sodium_mg: 1840, potassium_mg: 2150, phosphorus_mg: 820, protein_g: 61, fluids_ml: 1250 }, vs_targets: { sodium_mg: { target: 2000 } } }], targets_set: true })],
  ["GET", /visit-types$/, () => ({ pcp: "Primary care", obgyn: "OB/GYN", nephrology: "Nephrology", maternal_fetal_medicine: "Maternal-fetal medicine" })],
  ["POST", /visit-prep$/, () => ({ markdown: SHEET, pin_id: "p3" })],
  ["POST", /^\/resources\/ride$/, (q, body) => {
    const a = body.answers || {};
    if (!a.zip) return { done: false, ask: { field: "zip", question: "What's your ZIP code?", options: [] } };
    if (!a.coverage) return { done: false, ask: { field: "coverage", question: "Do you have Medi-Cal, Medicare, both, or neither?",
      options: [{ value: "medi_cal", label: "Medi-Cal" }, { value: "medicare", label: "Medicare" }, { value: "both", label: "Both Medi-Cal and Medicare" }, { value: "neither", label: "Neither / not sure" }] } };
    return { done: true, card: { mode_label: "Non-Medical Transportation (car or public transit)",
      contact: { label: "Kaiser Permanente member services", phone: "1-855-839-7613", tel: "tel:+18558397613", tty: "711", last_verified: "2026-11-07",
                 other_phones: [{ label: "Spanish", phone: "1-800-788-0616", tel: "tel:+18007880616" }, { label: "Chinese dialects", phone: "1-800-757-7585", tel: "tel:+18007577585" }] },
      call_script: "Hi, I'm a Medi-Cal member and I need a ride to dialysis (regular sessions). I don't have another way to get there. My dialysis is on a regular schedule. Can we set up rides for every session? How far ahead do I need to book, and what information do you need from me?",
      requirements: ["Tell them you have no other way to get to your appointment.", "Dialysis is regular, so ask if they can set up standing rides for every session.", "Ask how far ahead you need to book, and have your Medi-Cal ID card ready."],
      notes: ["Medi-Cal doesn't pay you back for driving yourself, but your plan may reimburse a friend or family member who drives you. Ask first."],
      fallbacks: [{ name: "ADA paratransit", detail: "Shared-ride service from your local transit agency." }, { name: "211", detail: "Call or text 211 for local ride programs.", phone: "211" }],
      disclaimer: "Rules and phone numbers can change. If something doesn't match, ask your plan.",
      sources: [{ source_id: "DOC:medi-cal-transport", title: "DHCS: Medi-Cal Transportation Services", url: "https://www.dhcs.ca.gov/" }] } };
  }],
  ["GET", /^\/profile\//, () => ({ zip: "92701", ckd_stage: "3a", life_stage: "pregnant", transplant: true })],
  ["GET", /^\/plans$/, () => [{ plan_id: "caloptima", insurer: "CalOptima", plan_name: "Health" }]],
];

export function createClient() {
  return {
    auth: {
      isAuthenticated: async () => signedIn,
      me: async () => ({ id: "u1", email: "demo@example.com", full_name: "Demo" }),
      redirectToLogin: () => {}, logout: () => {},
    },
    functions: {
      invoke: async (name, { method, path, query, body }) => {
        const r = ROUTES.find(([m, re]) => m === method && re.test(path));
        if (!r) throw Object.assign(new Error("no mock"), { status: 404 });
        return { data: await r[2](query, body || {}) };
      },
    },
  };
}
