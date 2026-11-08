# Frontend: build and deploy with the Base44 CLI

The frontend is a React (Vite) app in `src/`, deployed to Base44 hosting together with the two backend functions
in `base44/functions/` (`kidney-api` proxy, `reminder-relay`). Users sign in on Base44's hosted login page.
`base44/FRONTEND_SPEC.md` is superseded by this code; keep it only as a description.

## One-time setup (PowerShell, in the folder that contains package.json and app\)
Needs Node.js 20.19 or newer.
```powershell
npm install
$env:BASE44_API_KEY = "b44k_..."          # your workspace key; the CLI reads it from the environment
npx base44 whoami                          # should print your account, with no browser login
npx base44 link --create --name "Kidney Women's Health"    # creates the Base44 app, writes base44\.app.jsonc
npx base44 auth password-login enable      # email + password sign-in (Google is on by default)
npx base44 secrets set KIDNEY_API_URL=https://<your-tunnel-or-domain> KIDNEY_API_SECRET=<APP_SHARED_SECRET> RELAY_SECRET=<REMINDER_WEBHOOK_SECRET>
```
- `KIDNEY_API_SECRET` must equal `APP_SHARED_SECRET` in the Python API's `.env`; `RELAY_SECRET` must equal
  `REMINDER_WEBHOOK_SECRET`.
- With the laptop tunnel, the address changes whenever cloudflared restarts: run `secrets set KIDNEY_API_URL=...` again.

## Deploy (every time)
```powershell
npx base44 deploy --build -y    # builds with the app id injected, then uploads the site and both functions
npx base44 site open
```
Always build through the CLI (`npx base44 build` or `deploy --build`). A bare `npm run build` leaves the app id out
and every call from the deployed site fails.

## After the first deploy
- Find the `reminder-relay` function's URL in the Base44 dashboard (or `npx base44 functions list`) and set it as
  `REMINDER_WEBHOOK_URL` in the Python API's `.env`, then restart the API.
- Check the relay's user lookup (`entities.User.get`) by adding a medication with a reminder two minutes out.
- `npx base44 logs --level error` shows function errors (the proxy never logs health data).

## Keys
`BASE44_API_KEY` is only for the CLI on your machine. Never put it in `src/` or anything starting with `VITE_`
(Vite ships those to the browser), and keep `.env` out of git (it's in `.gitignore`).

## What's where
- `src/App.jsx`: sign-in gate, navigation (bottom bar on phones, side rail on desktop), routes (`#/symptoms` ...)
- `src/components/Chat.jsx`: the four question tabs; `Answer.jsx`: cited answer, sources, checks panel, pin
- `src/components/RideFinder.jsx`, `YouTab.jsx` (board, medications, nutrition, visit prep), `Profile.jsx`
- `src/components/Markdown.jsx`: renders answers as React elements, never raw HTML
- `src/api/kidney.js`: every call goes through the `kidney-api` function with `{me}` for the current user
- `tests/frontend/mock-sdk.js`: a fake Base44 SDK with realistic responses, for rendering the UI without a backend
