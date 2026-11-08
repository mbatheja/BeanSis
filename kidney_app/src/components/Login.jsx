import React, { useState } from "react";
import { localAuth } from "../api/base44Client";

export default function Login({ onLogin }) {
  const [mode, setMode] = useState("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const register = mode === "register";

  const submit = async (e) => {
    e.preventDefault(); setError(null); setBusy(true);
    try {
      const user = await localAuth(register ? "register" : "login", { username, password });
      onLogin(user);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };

  return (
    <main className="auth-screen">
      <section className="panel auth-panel">
        <p className="eyebrow">Bean Sis</p>
        <h1>{register ? "Create your account" : "Welcome back"}</h1>
        <p className="muted">Sign in to continue to your kidney health support space.</p>
        <div className="auth-switch" role="group" aria-label="Account action">
          <button className={!register ? "active" : ""} onClick={() => { setMode("login"); setError(null); }}>Sign in</button>
          <button className={register ? "active" : ""} onClick={() => { setMode("register"); setError(null); }}>Create account</button>
        </div>
        <form className="stack" onSubmit={submit}>
          <label>Username<input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required maxLength={64} /></label>
          <label>Password<input type="password" autoComplete={register ? "new-password" : "current-password"} value={password} onChange={(e) => setPassword(e.target.value)} required maxLength={128} /></label>
          <button className="btn primary large" disabled={busy}>{busy ? "Please wait…" : register ? "Create account" : "Sign in"}</button>
          {error && <p className="error" role="alert">{error}</p>}
        </form>
        <p className="fine">This is a local prototype login for testing Bean Sis.</p>
      </section>
    </main>
  );
}
