> Superseded: the frontend is now built as code in `src/` (see FRONTEND.md). Kept as a description.

# Frontend spec for Base44 (paste into the Base44 builder)

Build a mobile-first web app for women living with kidney disease (including pregnant, postpartum, and
planning pregnancy). Users must log in. ALL data comes from one backend function:

```js
const res = await base44.functions.invoke("kidney-api", { method, path, query, body });
const data = res.data;   // JSON from the API
```
Never send user_id; the function adds it. In paths, write `{me}` for the current user (e.g. `/you/{me}`).
Do NOT store health information in Base44 entities; it lives in the backend.

## Layout
Bottom navigation with 5 tabs: **Symptoms**, **Medications**, **Resources**, **Find a doctor**, **You**.
Show a one-line banner on first launch: "General health education, not medical advice. In an emergency
call 911." Footer link: Privacy & data.

## Chat tabs (Symptoms, Medications, Resources, Find a doctor)
- Thread list: `GET /threads` with `query: {tab}`; open one: `GET /threads/{thread_id}/messages`.
- Send: `POST /chat/{tab}` body `{message, thread_id}` (thread_id null for a new chat; keep the returned one).
  Show a "Looking that up…" state; replies can take 10-30 s.
- Render each assistant reply from the response:
  - `answer_display` as the message text (markdown). Markers like [1] link to the citation list.
  - `citations`: numbered list under the answer: `n. title (kind)`, title links to `url`.
  - `notices`: small grey caption lines under the citations.
  - `guardrails`: collapsible "How this answer was checked" panel listing name, status, detail.
    Show it EXPANDED with an amber badge if `citation_check` is `warning` or `modified`, or
    `self_correction` is `kept_original`.
  - If `urgent` is true: show the answer in a red full-width card with call buttons (911, 988) and no
    citations panel.
  - If `related_history` is non-empty, show above the answer: "You looked into this before" with links
    (pinned ones first).
  - A **Pin** button on every assistant reply: `POST /pins` body `{message_id}`.
- In the Medications tab, anything citing patient reviews must show the credit:
  "Patient reviews: Drugs.com via DrugRev dataset (CC BY 4.0)".
- Under the input: "Chats you don't pin are deleted after 30 days without use."

## Resources tab: "Get a ride" (California)
A button above the chat opens a step-by-step card flow. Call `POST /resources/ride` with
`body: {answers}` (everything answered so far; the server fills ZIP from the profile if it has one).
- If the response has `done: false`: show `ask.question`; if `ask.options` is non-empty, show them as large
  tap buttons (send the option's `value`), otherwise a text input (ZIP). Add the answer to `answers` with key
  `ask.field` and call again. One question per screen.
- If `done: true`, show `card`:
  - Heading from `card.mode_label`.
  - `card.contact`: if `tel` is present, a big **Call** button (`href = tel`) labeled with `contact.label`
    and the number, plus "Number checked {last_verified}". If no `tel`, show `contact.how` and a link to
    `contact.url`; if `contact.email`, a mail link. NEVER show a number that isn't in `tel`.
  - `contact.other_phones` (e.g. Spanish, Chinese dialects, toll free): a smaller call button for each.
    Show `contact.tty` as text ("TTY: ...").
  - `contact.alternate` (shown when she isn't sure of her plan): a second call button for Medi-Cal Health
    Care Options with its `how`, `hours`, and TTY.
  - `card.call_script` in a quoted box with a **Copy** button ("What to say").
  - `card.requirements` as a checklist; `card.notes` as plain lines.
  - "If that doesn't work": `card.fallbacks` (name, detail, link or 211 call button).
  - `card.disclaimer` in small text, and `card.sources` as links.
- A "Start over" link resets `answers`.

## Find a doctor
Same chat, plus a "My insurance & ZIP" button opening the profile form. Each provider result already
includes its directory date; keep the reply's notice about confirming with the insurer visible.

## You tab (3 sections, load with `GET /you/{me}`)
1. **My board**: `board.pinned_answers` (render `snapshot.answer_display` + `snapshot.citations`) and
   `board.saved_resources`. Unpin: `DELETE /pins/{pin_id}`.
2. **My medications**
   - Today: `todays_doses` (time, name, dose_text, status). Buttons Taken / Skip:
     `POST /you/meds/{med_id}/dose` body `{scheduled_for, status}`.
   - List + Add form (name, dose exactly as on the prescription, times as HH:MM, optional weekdays,
     reminders on/off): `POST /you/meds`. Edit: `PATCH /you/meds/{med_id}`. Remove: `DELETE /you/meds/{med_id}`.
     Helper text under dose: "Enter it exactly as your prescription says. The app never changes doses."
   - "Check interactions" button: `GET /you/{me}/interactions` (optional `query: {drug}`); show flagged
     pairs with the quoted label sentence and its source, and ALWAYS show the returned `notices`.
3. **Nutrition**
   - Log a meal (date, meal type, free text): `POST /you/meals`. Show `totals`, list `unmatched` foods,
     show `notice`.
   - Week view: `GET /you/{me}/nutrition` with `query: {day_from, day_to}`: daily sodium, potassium,
     phosphorus, protein, fluids; compare to targets only if `targets_set`.
   - NO calories, weight, or scores anywhere.
4. **Prepare for a visit** button: pick PCP / OB-GYN / Nephrology / Maternal-fetal medicine
   (`GET /you/visit-types`), optional "What do you want to bring up?" text, then
   `POST /you/visit-prep` body `{visit_type, concerns, save}`. Render `markdown` as a printable page with
   Print and "Save to board" (re-call with `save: true`).

## Settings / Profile
Form for ZIP, state, insurance plan, CKD stage, life stage (pregnant / postpartum / planning / not
pregnant), dialysis, transplant, timezone, and optional dietitian targets:
`GET /profile/{me}`, `PUT /profile/{me}`.
**Delete all my data**: confirm dialog, then `DELETE /users/{me}/data`.
Privacy page text: what's stored (profile, chats 30 days, pinned items until removed, medication list
until removed, meal and dose logs 90 days), that data isn't sold or shared, and how to delete it.

## Errors
Show the API's `error` field when present. 429 means "Too many requests, wait a minute". 504 means it
took too long, offer Retry.
