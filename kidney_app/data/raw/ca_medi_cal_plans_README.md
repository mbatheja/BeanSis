# ca_medi_cal_plans.csv: fill in from the official DHCS plan directory

Source: https://www.dhcs.ca.gov/individuals/medi-cal-managed-care-health-plan-directory/

One row per plan per county. Columns:
- `county`: county name without "County" (e.g. `Los Angeles`)
- `plan_name`: as printed on members' Medi-Cal ID cards
- `plan_type`: `managed_care` or `pace` (PACE programs are for people 55+)
- `member_services_phone`: from the DHCS directory
- `other_phones`: extra language lines as `Label: number; Label: number` (e.g. `Spanish: 1-800-...`)
- `ride_line_phone`: the plan's transportation line, ONLY if the plan publishes a separate one (leave blank otherwise)
- `tty`, `url`: optional
- `last_verified`: YYYY-MM-DD when a teammate checked the number against the official source.
  **Rows without it get no call button** (the app links the directory instead).
- `source`: where you verified it

Start with the counties you'll demo, then expand. Re-verify before any public use.
