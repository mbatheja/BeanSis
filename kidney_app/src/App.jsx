import React, { useEffect, useState } from "react";
import { base44, LOCAL_MODE } from "./api/base44Client";
import Chat from "./components/Chat";
import YouTab from "./components/YouTab";
import { Profile, Privacy } from "./components/Profile";
import Login from "./components/Login";
import Onboarding from "./components/Onboarding";
import Home from "./components/Home";

export const APP_NAME = "Bean Sis";

const TABS = [
  { id: "symptoms", label: "Symptoms", intro: "Ask about symptoms, pregnancy, and everyday care with kidney disease. Answers come with their sources." },
  { id: "medications", label: "Medications", intro: "Ask about side effects, pregnancy and breastfeeding safety, or how a medicine fits with your others." },
  { id: "resources", label: "Resources", intro: "Find help paying for care, support groups, research studies, and rides to appointments." },
  { id: "doctors", label: "Find a doctor", intro: "Find nephrologists, OB-GYNs, and high-risk pregnancy specialists near you who take your insurance." },
  { id: "you", label: "You", intro: "" },
];
const PAGES = new Set([...TABS.map((t) => t.id), "profile", "privacy", "home", "welcome", "onboarding"]);

function useRoute() {
  const read = () => { const r = window.location.hash.replace(/^#\/?/, ""); return PAGES.has(r) ? r : "home"; };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [route, (r) => { window.location.hash = `/${r}`; }];
}

function Welcome() {
  return (
    <main className="welcome">
      <h1>{APP_NAME}</h1>
      <p className="lede">Answers about kidney disease, pregnancy, and medications, each one showing where it came from. Plus medication reminders, a meal log, and help getting to appointments in California.</p>
      <button className="btn primary large" onClick={() => base44.auth.redirectToLogin(window.location.href)}>Sign in or create an account</button>
      <p className="fine">Health education, not medical advice. In an emergency, call 911.</p>
    </main>
  );
}

export default function App() {
  const [user, setUser] = useState(undefined); // undefined = checking, null = signed out
  const [route, go] = useRoute();

  useEffect(() => {
    base44.auth.isAuthenticated()
      .then((ok) => (ok ? base44.auth.me() : null))
      .then((u) => {
        setUser(u || null);
        if (u?.is_new && route !== "onboarding") go("welcome");
        else if (!u && LOCAL_MODE) go("home");
      })
      .catch(() => { setUser(null); if (LOCAL_MODE) go("home"); });
  }, []);

  if (user === undefined) return <main className="welcome"><p className="muted">Loading…</p></main>;
  if (!user) return LOCAL_MODE ? <Login onLogin={(u) => { setUser(u); go(u.is_new ? "welcome" : "home"); }} /> : <Welcome />;
  if (LOCAL_MODE && user.is_new && !user.onboarding_complete) {
    if (route === "onboarding") return <Onboarding onComplete={(profile) => {
      setUser({ ...user, ...profile, is_new: false, onboarding_complete: true }); go("home");
    }} />;
    return <main className="welcome landing-welcome">
      <p className="eyebrow">Bean Sis</p>
      <h1>Welcome to Bean Sis, {user.username}</h1>
      <p className="lede">A kidney health support space built around your questions and goals.</p>
      <button className="btn primary large" onClick={() => go("onboarding")}>Set up my profile</button>
    </main>;
  }

  const tab = TABS.find((t) => t.id === route);
  return (
    <div className="shell">
      <a className="skip" href="#main">Skip to content</a>
      <header className="topbar">
        <a className="brand" href="#/home">{APP_NAME}</a>
        <nav className="top-links" aria-label="Account">
          <a href="#/privacy" aria-current={route === "privacy" ? "page" : undefined}>Privacy</a>
          <a href="#/profile" aria-current={route === "profile" ? "page" : undefined}>Profile</a>
        </nav>
      </header>

      <nav className="tabs" aria-label="Main">
        {TABS.map((t) => (
          <a key={t.id} href={`#/${t.id}`} className={route === t.id ? "active" : ""} aria-current={route === t.id ? "page" : undefined}>
            {t.label}
          </a>
        ))}
      </nav>

      <main id="main" className="main">
        {LOCAL_MODE && <p className="banner local">Local test mode: no sign-in, talking to the API on this computer.</p>}
        <p className="banner">Health education, not medical advice. In an emergency, call 911.</p>
        {route === "home" && <Home user={user} tabs={TABS} onNavigate={go} />}
        {route === "you" && <YouTab />}
        {route === "profile" && <Profile />}
        {route === "privacy" && <Privacy />}
        {tab && route !== "you" && <Chat key={route} tab={route} intro={tab.intro} onNavigate={go} />}
      </main>
    </div>
  );
}
