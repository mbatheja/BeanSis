import React from "react";

const DESCRIPTIONS = {
  symptoms: "Understand symptoms, pregnancy, and everyday kidney care.",
  medications: "Explore medicine questions and safety information.",
  resources: "Find support, coverage, rides, and research studies.",
  doctors: "Look for kidney and pregnancy care specialists.",
  you: "Manage your profile, medicines, and personal tools.",
};

const ICONS = {
  symptoms: <><path d="M3 12h4l2.2-6 4.1 12 2.2-6H21" /></>,
  medications: <><path d="M7.2 19.2a5 5 0 0 1 0-7.1l4.9-4.9a5 5 0 1 1 7.1 7.1l-4.9 4.9a5 5 0 0 1-7.1 0Z" /><path d="m9.3 10.8 7.1 7.1" /></>,
  resources: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v17H6.5A2.5 2.5 0 0 0 4 22Z" /><path d="M4 5.5v14A2.5 2.5 0 0 1 6.5 17H20M8 7h8M8 10h8" /></>,
  doctors: <><path d="M6 3v5a4 4 0 0 0 8 0V3M4 3h4M12 3h4" /><path d="M10 12v2a5 5 0 0 0 10 0v-1" /><circle cx="20" cy="11" r="1.5" /></>,
  you: <><circle cx="12" cy="8" r="3.5" /><path d="M5 21a7 7 0 0 1 14 0" /></>,
};

function TileIcon({ id }) {
  return <svg className="home-tile-icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{ICONS[id] || ICONS.you}</svg>;
}

export default function Home({ user, tabs, onNavigate }) {
  const name = user?.username || user?.full_name || user?.name || "there";
  return (
    <section className="home-page">
      <h1>Welcome to Bean Sis, {name}</h1>
      <p className="home-notice">Health education, not medical advice. In an emergency, call 911.</p>
      <h2>How can we help you today?</h2>
      <div className="home-grid">
        {tabs.map((tab) => (
          <button key={tab.id} className={`home-tile home-tile-${tab.id}`} onClick={() => onNavigate(tab.id)}>
            <span className="home-tile-heading">
              <span className="home-tile-icon"><TileIcon id={tab.id} /></span>
              <span className="home-tile-title">{tab.label}</span>
            </span>
            <span>{DESCRIPTIONS[tab.id]}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
