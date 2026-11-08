import React from "react";

const DESCRIPTIONS = {
  symptoms: "Understand symptoms, pregnancy, and everyday kidney care.",
  medications: "Explore medicine questions and safety information.",
  resources: "Find support, coverage, rides, and research studies.",
  doctors: "Look for kidney and pregnancy care specialists.",
  you: "Manage your profile, medicines, and personal tools.",
};

export default function Home({ user, tabs, onNavigate }) {
  const name = user?.username || user?.full_name || user?.name || "there";
  return (
    <section className="home-page">
      <p className="eyebrow">Bean Sis</p>
      <h1>Welcome to Bean Sis, {name}</h1>
      <h2>How can we help you today?</h2>
      <div className="home-grid">
        {tabs.map((tab) => (
          <button key={tab.id} className="home-tile" onClick={() => onNavigate(tab.id)}>
            <span className="home-tile-title">{tab.label}</span>
            <span>{DESCRIPTIONS[tab.id]}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
