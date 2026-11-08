import plansCsv from "../../data/raw/plans.csv?raw";

const CACHE_KEY = "beansis.ca-insurance-plans.v2";

function parseCsv(csv) {
  const records = [];
  let row = [];
  let cell = "";
  let quoted = false;

  for (let i = 0; i < csv.length; i += 1) {
    const char = csv[i];
    if (quoted) {
      if (char === '"' && csv[i + 1] === '"') { cell += '"'; i += 1; }
      else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { row.push(cell); cell = ""; }
    else if (char === "\n") { row.push(cell.replace(/\r$/, "")); records.push(row); row = []; cell = ""; }
    else cell += char;
  }
  if (cell || row.length) { row.push(cell.replace(/\r$/, "")); records.push(row); }

  const [headers = [], ...data] = records;
  const cleanHeaders = headers.map((header, i) => (i === 0 ? header.replace(/^\uFEFF/, "") : header).trim());
  return data.map((values) => Object.fromEntries(cleanHeaders.map((header, i) => [header, values[i] || ""])))
    .filter((plan) => plan.plan_id && plan.insurer && plan.plan_name && plan.state === "CA");
}

function loadPlans() {
  try {
    const saved = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
    if (Array.isArray(saved) && saved.length) return saved;
  } catch { /* Ignore a stale or malformed cache and rebuild it from the bundled CSV. */ }

  const plans = parseCsv(plansCsv);
  try { localStorage.setItem(CACHE_KEY, JSON.stringify(plans)); } catch { /* The bundled CSV remains available if storage is disabled. */ }
  return plans;
}

// Source of truth is the repo's plans.csv. Bundle it at build time and keep a browser cache so the
// insurance picker never needs a live API request just to render its options.
export const CA_INSURANCE_PLANS = loadPlans();
