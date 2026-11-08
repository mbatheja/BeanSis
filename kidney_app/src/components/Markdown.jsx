import React from "react";

// Small markdown renderer for answers and visit sheets. It builds React elements directly, so text from the
// model can never inject HTML. Supports: paragraphs, # / ## / ### headings, - and 1. lists, "[ ]" checkboxes,
// **bold**, _italic_, [text](url) links (http/https/tel/mailto/# only), --- rules, and citation markers [1], [2, 3].

const SAFE_URL = /^(https?:|tel:|mailto:|#)/i;

function inline(text, citePrefix, keyBase) {
  const out = [];
  // order matters: links before citation markers, since both use square brackets
  const re = /\[([^\]]+)\]\(([^)\s]+)\)|\[(\d+(?:\s*,\s*\d+)*)\]|\*\*([^*]+)\*\*|_([^_]+)_|\*([^*]+)\*/g;
  let last = 0;
  let m;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${keyBase}-${i++}`;
    if (m[1] !== undefined) {
      const href = m[2];
      out.push(SAFE_URL.test(href)
        ? <a key={k} href={href} target={href.startsWith("http") ? "_blank" : undefined} rel="noreferrer">{m[1]}</a>
        : m[1]);
    } else if (m[3] !== undefined) {
      const nums = m[3].split(",").map((n) => n.trim());
      out.push(
        <span key={k} className="cite-group">
          {nums.map((n) =>
            citePrefix
              ? <a key={n} className="cite" href={`#${citePrefix}-${n}`} aria-label={`Source ${n}`}>{n}</a>
              : <span key={n} className="cite">{n}</span>)}
        </span>);
    } else if (m[4] !== undefined) {
      out.push(<strong key={k}>{m[4]}</strong>);
    } else {
      out.push(<em key={k}>{m[5] ?? m[6]}</em>);
    }
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export default function Markdown({ text = "", citePrefix }) {
  const lines = String(text).replace(/\r/g, "").split("\n");
  const blocks = [];
  let para = [];
  let list = null; // { ordered, items: [] }

  const flushPara = () => {
    if (para.length) blocks.push({ type: "p", text: para.join(" ") });
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push({ type: "list", ...list });
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if (/^-{3,}$/.test(line.trim())) { flushPara(); flushList(); blocks.push({ type: "hr" }); continue; }
    if (h) { flushPara(); flushList(); blocks.push({ type: `h${h[1].length}`, text: h[2] }); continue; }
    if (ul || ol) {
      flushPara();
      const ordered = Boolean(ol);
      if (!list || list.ordered !== ordered) { flushList(); list = { ordered, items: [] }; }
      list.items.push((ul || ol)[1]);
      continue;
    }
    if (list) flushList();
    para.push(line.trim());
  }
  flushPara();
  flushList();

  return (
    <div className="md">
      {blocks.map((b, bi) => {
        if (b.type === "hr") return <hr key={bi} />;
        if (b.type === "p") return <p key={bi}>{inline(b.text, citePrefix, bi)}</p>;
        if (b.type === "list") {
          const Tag = b.ordered ? "ol" : "ul";
          return (
            <Tag key={bi}>
              {b.items.map((it, ii) => {
                const box = it.match(/^\[( |x)\]\s+(.*)$/i);
                return (
                  <li key={ii} className={box ? "check" : undefined}>
                    {box && <span className="box" aria-hidden="true" />}
                    {inline(box ? box[2] : it, citePrefix, `${bi}-${ii}`)}
                  </li>
                );
              })}
            </Tag>
          );
        }
        const H = b.type; // h1..h3; shift down one level so page headings stay unique
        const Tag = { h1: "h2", h2: "h3", h3: "h4" }[H];
        return <Tag key={bi}>{inline(b.text, citePrefix, bi)}</Tag>;
      })}
    </div>
  );
}
