# Run everything on your computer (no Base44, no tunnel, no VM)

Two PowerShell windows, both opened in the folder that contains `app\` and `package.json`
(`C:\Users\...\kidney_app\kidney_app`). You need Python 3.10+ and Node.js 20+.

## One-time setup
```powershell
pip install -r requirements.txt     # includes tzdata, which Windows needs for reminders and profile saving
npm install
copy .env.example .env              # then edit .env: set NEBIUS_API_KEY (and FDC_API_KEY if you have one)
python tests\test_api_smoke.py      # should print four "ok" lines
```
If the smoke test fails, fix that first. It checks the API loads and the You tab works, with no network.
`BASE44_API_KEY` in `.env` is not used for a local run.

## Run it
**Window 1, the API:**
```powershell
powershell -ExecutionPolicy Bypass -File .\run_local.ps1 -Local
```
Wait for `Uvicorn running on http://127.0.0.1:8000`. Open `http://127.0.0.1:8000/healthz`: it should show `{"ok":true}`.

**Window 2, the screens:**
```powershell
npm run dev:local
```
Open the address it prints (normally `http://localhost:5173`). An amber banner says "Local test mode".
Local mode skips Base44 sign-in and uses one test user (`local-demo-user`). Your data is stored in `data\app.db`.
To start fresh, stop the API and delete that file. Stop either window with Ctrl+C.

## What works before you've built the reference database
`data\reference.duckdb` holds your library, trials, providers, reviews, and ZIP-county table. Without it:

| Screen | What happens |
|---|---|
| You tab: board, medications, today's doses, profile, delete my data | Works fully |
| You tab: nutrition | Works with internet (USDA lookup; set `FDC_API_KEY`, the default demo key runs out fast) |
| You tab: interaction check | Works with internet (FDA labels, RxNorm) |
| Medications tab: side effects, labels, pregnancy safety | Works with internet (FDA label and adverse event reports); the patient-review comparison is skipped |
| Symptoms tab | Can use live PubMed research, but your library isn't loaded, so expect a "Needs care" badge on some answers |
| Resources tab: chat | Library search is empty until the database is built |
| Resources tab: ride finder | Shows the ZIP question, then an error: it needs the ZIP-county table |
| Find a doctor tab | Empty: it needs the providers table |

To see everything, build the database first (README, "Build the reference DB").

## If something goes wrong
- **"Something went wrong" on the screen:** look at Window 1. The Python error is printed there. Send it to me.
- **A time-zone warning at API start:** `pip install tzdata`.
- **Screens load but every action fails right away:** Window 1 isn't running, or isn't on port 8000.
- **`ModuleNotFoundError: app`:** you're in the wrong folder; it must contain the `app` folder.
- **First answer takes up to 30 seconds:** normal; it's calling the model and several data sources.
- **Rate limit message:** 12 questions a minute per user, by design.

## What a local run does not test
Base44 sign-in, the `kidney-api` proxy function, the reminder email relay, and your Base44 secrets. Those only
run after `npx base44 deploy` (see FRONTEND.md).
