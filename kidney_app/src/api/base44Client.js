import { createClient } from "@base44/sdk";

// Local test mode (`npm run dev:local`): no Base44 sign-in, and calls go straight to the Python API on this
// computer. It only exists in that dev mode; production builds (`npx base44 deploy --build`) never use it.
export const LOCAL_MODE = import.meta.env.MODE === "localapi";
const LOCAL_SESSION_KEY = "beansis.local.session";

export function getLocalUserId() {
  try { return JSON.parse(localStorage.getItem(LOCAL_SESSION_KEY) || "null")?.id || "local-demo-user"; }
  catch { return "local-demo-user"; }
}

export async function localAuth(action, credentials) {
  const res = await fetch(`/api/auth/${action}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(credentials),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Could not sign in. Please try again.");
  localStorage.setItem(LOCAL_SESSION_KEY, JSON.stringify(data));
  return data;
}

function clearLocalSession() {
  localStorage.removeItem(LOCAL_SESSION_KEY);
}

const localStub = {
  auth: {
    isAuthenticated: async () => Boolean(JSON.parse(localStorage.getItem(LOCAL_SESSION_KEY) || "null")?.id),
    me: async () => {
      const id = getLocalUserId();
      const res = await fetch(`/api/auth/me?user_id=${encodeURIComponent(id)}`);
      if (!res.ok) { clearLocalSession(); return null; }
      const user = await res.json();
      localStorage.setItem(LOCAL_SESSION_KEY, JSON.stringify(user));
      return user;
    },
    redirectToLogin: () => {},
    logout: () => { clearLocalSession(); window.location.hash = "/"; window.location.reload(); },
  },
};

// VITE_BASE44_APP_ID is injected by `npx base44 build` / `npx base44 dev`. Don't run a bare `npm run build`.
export const base44 = LOCAL_MODE ? localStub : createClient({ appId: import.meta.env.VITE_BASE44_APP_ID });
