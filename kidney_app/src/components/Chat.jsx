import React, { useEffect, useRef, useState } from "react";
import Answer from "./Answer";
import RideFinder from "./RideFinder";
import { api } from "../api/kidney";

const STARTERS = {
  symptoms: [
    "I’m on dialysis and my periods have changed or stopped. Could dialysis or kidney disease be related?",
    "I have kidney disease and am thinking about pregnancy. What should I discuss with my care team first?",
    "I’m pregnant and have kidney disease. What symptoms need urgent attention, and how is my pregnancy monitored?",
    "Could kidney disease or menopause be related to changes in my periods or other symptoms?",
  ],
  medications: [
    "What side effects do women report with tacrolimus?",
    "Is lisinopril safe during pregnancy?",
    "Can I take ibuprofen for headaches?",
  ],
  resources: [
    "Are there programs that help pay for dialysis costs?",
    "Find kidney studies near me that don’t exclude pregnancy",
    "Where can I talk to someone who has been through dialysis?",
  ],
  doctors: [
    "Find a nephrologist near me who takes my insurance",
    "I need a high-risk pregnancy doctor",
    "Find an OB-GYN near me",
  ],
};

export default function Chat({ tab, intro, onNavigate }) {
  const [threads, setThreads] = useState([]);
  const [threadId, setThreadId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);
  const [retention, setRetention] = useState(null);
  const [showRide, setShowRide] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const endRef = useRef(null);

  const loadThreads = () => api("GET", "/threads", { query: { tab } }).then(setThreads).catch(() => {});

  useEffect(() => {
    setThreadId(null);
    setMessages([]);
    setError(null);
    setShowRide(false);
    loadThreads();
    api("GET", "/retention").then((r) => setRetention(r?.description)).catch(() => {});
  }, [tab]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [messages, sending]);

  const openThread = async (id) => {
    setError(null);
    setThreadId(id);
    setShowRide(false);
    try {
      setMessages(await api("GET", `/threads/${id}/messages`));
    } catch (e) {
      setError(e.message);
    }
  };

  const newChat = () => { setThreadId(null); setMessages([]); setError(null); };

  const send = async (text) => {
    const q = (text ?? input).trim();
    if (!q || sending) return;
    setInput("");
    setError(null);
    setMessages((m) => [...m, { role: "user", content: q }]);
    setSending(true);
    try {
      const res = await api("POST", `/chat/${tab}`, { body: { message: q, thread_id: threadId } });
      setThreadId(res.thread_id);
      setMessages((m) => [...m, { role: "assistant", ...res, content: res.answer }]);
      loadThreads();
    } catch (e) {
      setError(e.message);
      setMessages((m) => m.slice(0, -1));
      setInput(q);
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  };

  const openRelated = (r) => {
    if (r.kind === "pinned") onNavigate?.("you");
    else if (r.tab === tab) openThread(r.thread_id);
    else onNavigate?.(r.tab);
  };

  return (
    <div className={`chat ${historyOpen ? "history-open" : ""}`}>
      <div className="chat-toolbar">
        <button className="btn quiet" onClick={() => setHistoryOpen((open) => !open)} aria-expanded={historyOpen} aria-controls={`history-${tab}`}>
          {historyOpen ? "Hide history" : "Show history"}
        </button>
        <button className="btn primary" onClick={newChat}>New question</button>
      </div>

      <aside id={`history-${tab}`} className="threads" aria-label="Message history" hidden={!historyOpen}>
        <h2 className="threads-title">Message history</h2>
        {threads.length > 0 ? <ul>
          {threads.map((t) => (
            <li key={t.thread_id}>
              <button className={`thread ${t.thread_id === threadId ? "active" : ""}`} onClick={() => openThread(t.thread_id)}>
                {t.title || "Untitled chat"}
              </button>
            </li>
          ))}
        </ul> : <p className="fine">Your past questions will appear here.</p>}
      </aside>

      <section className="conversation">
        {tab === "resources" && (
          <div className="tool-bar">
            <button className="btn quiet" onClick={() => setShowRide((v) => !v)} aria-expanded={showRide}>
              {showRide ? "Close ride finder" : "Get a ride to an appointment"}
            </button>
          </div>
        )}
        {tab === "doctors" && (
          <div className="tool-bar">
            <button className="btn quiet" onClick={() => onNavigate?.("profile")}>Set my insurance and ZIP code</button>
          </div>
        )}

        {showRide ? <RideFinder /> : (
          <>
            {messages.length === 0 && (
              <div className="empty">
                <p className="intro">{intro}</p>
                <p className="muted">Try one of these:</p>
                <div className="starters">
                  {(STARTERS[tab] || []).map((s) => (
                    <button key={s} className="starter" onClick={() => send(s)}>{s}</button>
                  ))}
                </div>
              </div>
            )}

            <div className="messages" aria-live="polite">
              {messages.map((m, i) =>
                m.role === "user"
                  ? <p key={i} className="question">{m.content}</p>
                  : <Answer key={i} msg={m} onOpenRelated={openRelated} />)}
              {sending && <p className="thinking">Looking that up and checking sources. This can take up to 30 seconds.</p>}
              <div ref={endRef} />
            </div>

            {error && <p className="error" role="alert">{error}</p>}

            <div className="composer">
              <label className="sr-only" htmlFor={`ask-${tab}`}>Your question</label>
              <textarea
                id={`ask-${tab}`}
                rows={2}
                value={input}
                placeholder="Ask a question"
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                disabled={sending}
              />
              <button className="btn primary" onClick={() => send()} disabled={sending || !input.trim()}>Ask</button>
            </div>
            {retention && <p className="fine">{retention}</p>}
          </>
        )}
      </section>
    </div>
  );
}
