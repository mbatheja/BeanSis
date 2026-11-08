// Base44 backend function: the ONLY way the frontend reaches the Nebius API.
//
// Frontend usage:
//   const res = await base44.functions.invoke("kidney-api", {
//     method: "POST", path: "/chat/medications", body: { message: "...", thread_id: null }
//   });
//   const data = res.data;
//
// Security:
//   - requires a logged-in Base44 user (base44.auth.me)
//   - user_id is ALWAYS set here from that user; any user_id the browser sends is overwritten
//   - only routes in ROUTES are allowed; "{me}" in a route is replaced with the user's id
//   - adds the shared secret the API requires; the browser never sees it
//
// Secrets (Base44 app settings): KIDNEY_API_URL (e.g. https://api.example.com), KIDNEY_API_SECRET
import { createClientFromRequest } from "npm:@base44/sdk";

type Route = { method: string; pattern: RegExp };
const ID = "[A-Za-z0-9_-]{1,64}";
const ROUTES: Route[] = [
  { method: "GET", pattern: /^\/tabs$/ },
  { method: "POST", pattern: /^\/chat\/(symptoms|medications|resources|doctors)$/ },
  { method: "GET", pattern: /^\/threads$/ },
  { method: "GET", pattern: new RegExp(`^/threads/${ID}/messages$`) },
  { method: "DELETE", pattern: new RegExp(`^/threads/${ID}$`) },
  { method: "GET", pattern: /^\/profile\/\{me\}$/ },
  { method: "PUT", pattern: /^\/profile\/\{me\}$/ },
  { method: "POST", pattern: /^\/pins$/ },
  { method: "DELETE", pattern: new RegExp(`^/pins/${ID}$`) },
  { method: "GET", pattern: /^\/board$/ },
  { method: "POST", pattern: /^\/saved$/ },
  { method: "GET", pattern: /^\/saved$/ },
  { method: "GET", pattern: /^\/recall$/ },
  { method: "GET", pattern: /^\/retention$/ },
  { method: "DELETE", pattern: /^\/users\/\{me\}\/data$/ },
  { method: "GET", pattern: /^\/you\/\{me\}$/ },
  { method: "GET", pattern: /^\/you\/visit-types$/ },
  { method: "POST", pattern: /^\/you\/meds$/ },
  { method: "PATCH", pattern: new RegExp(`^/you/meds/${ID}$`) },
  { method: "DELETE", pattern: new RegExp(`^/you/meds/${ID}$`) },
  { method: "POST", pattern: new RegExp(`^/you/meds/${ID}/dose$`) },
  { method: "GET", pattern: /^\/you\/\{me\}\/interactions$/ },
  { method: "POST", pattern: /^\/you\/meals$/ },
  { method: "DELETE", pattern: new RegExp(`^/you/meals/${ID}$`) },
  { method: "GET", pattern: /^\/you\/\{me\}\/nutrition$/ },
  { method: "POST", pattern: /^\/you\/visit-prep$/ },
  { method: "POST", pattern: /^\/resources\/ride$/ },
];

const TIMEOUT_MS = 120_000; // chat with tool calls + a revision can take a while

Deno.serve(async (req) => {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: "Please log in." }, { status: 401 });

    const { method = "GET", path = "", query = {}, body = undefined } = await req.json();
    const m = String(method).toUpperCase();
    if (typeof path !== "string" || path.includes("..") || path.includes("//")) {
      return Response.json({ error: "Bad path" }, { status: 400 });
    }
    const route = ROUTES.find((r) => r.method === m && r.pattern.test(path));
    if (!route) return Response.json({ error: "Route not allowed" }, { status: 403 });

    const uid = String(user.id);
    const realPath = path.replaceAll("{me}", encodeURIComponent(uid));
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query ?? {})) {
      if (k !== "user_id" && v !== undefined && v !== null) qs.set(k, String(v));
    }
    qs.set("user_id", uid); // always ours, never the browser's

    let payload: string | undefined;
    if (m !== "GET" && m !== "DELETE") {
      payload = JSON.stringify({ ...(body ?? {}), user_id: uid });
    }

    const base = (Deno.env.get("KIDNEY_API_URL") ?? "").replace(/\/$/, "");
    const secret = Deno.env.get("KIDNEY_API_SECRET") ?? "";
    if (!base || !secret) return Response.json({ error: "Server not configured" }, { status: 500 });

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    const upstream = await fetch(`${base}${realPath}?${qs.toString()}`, {
      method: m,
      headers: { "content-type": "application/json", "x-app-secret": secret, "x-user-id": uid },
      body: payload,
      signal: ctrl.signal,
    }).finally(() => clearTimeout(timer));

    const text = await upstream.text();
    let data: unknown;
    try { data = JSON.parse(text); } catch { data = { error: "Unexpected response from server" }; }
    return Response.json(data, { status: upstream.status });
  } catch (err) {
    const aborted = err instanceof DOMException && err.name === "AbortError";
    // Never echo upstream errors or user content into logs here: they can contain health data.
    return Response.json(
      { error: aborted ? "That took too long. Please try again." : "Something went wrong." },
      { status: aborted ? 504 : 500 },
    );
  }
});
