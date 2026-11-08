import { base44, LOCAL_MODE, getLocalUserId } from "./base44Client";

// Every call goes through the "kidney-api" Base44 function, which checks sign-in, sets user_id itself,
// allows only listed routes, and adds the server secret. Write "{me}" in paths for the current user.
export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

const MESSAGES = {
  401: "Your session ended. Sign in again to continue.",
  403: "That action isn't available.",
  404: "That item no longer exists.",
  429: "Too many requests in a short time. Wait a minute, then try again.",
  504: "That took too long to answer. Try again.",
};

// Local test mode: do what the Base44 proxy does (fill in {me}, set user_id ourselves), then call the Python API
// through the Vite dev server's /api proxy (see vite.config.js).
async function localCall(method, path, { query, body }) {
  const userId = getLocalUserId();
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query || {})) if (k !== "user_id" && v != null) qs.set(k, String(v));
  qs.set("user_id", userId);
  const hasBody = method !== "GET" && method !== "DELETE";
  const res = await fetch(`/api${path.replaceAll("{me}", encodeURIComponent(userId))}?${qs}`, {
    method,
    headers: { "content-type": "application/json", "x-user-id": userId },
    body: hasBody ? JSON.stringify({ ...(body || {}), user_id: userId }) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error("request failed"), { status: res.status, data });
  return data;
}

export async function api(method, path, { query, body } = {}) {
  try {
    if (LOCAL_MODE) return await localCall(method, path, { query, body });
    const res = await base44.functions.invoke("kidney-api", { method, path, query: query || {}, body });
    return res?.data;
  } catch (e) {
    const status = e?.status ?? e?.response?.status;
    const d = e?.data ?? e?.response?.data ?? {};
    const serverMsg = d.error || (typeof d.detail === "string" ? d.detail : null);
    const msg = status === 422 ? serverMsg : MESSAGES[status] || serverMsg;
    throw new ApiError(msg || "Something went wrong. Try again.", status);
  }
}
