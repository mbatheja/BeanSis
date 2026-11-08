// Base44 backend function: receives medication reminders from the Nebius API and emails the user.
// Called server-to-server (no logged-in user), so it checks a shared secret and uses the service role.
//
// Secrets (Base44 app settings): RELAY_SECRET  (same value as REMINDER_WEBHOOK_SECRET on the API)
// API side: REMINDER_WEBHOOK_URL = https://<your-base44-app-domain>/functions/reminder-relay
//
// The message is deliberately generic ("Time for your 8:00 AM medications"): no drug names in email
// subjects or previews, which show up on lock screens.
import { createClientFromRequest } from "npm:@base44/sdk";

Deno.serve(async (req) => {
  const secret = Deno.env.get("RELAY_SECRET") ?? "";
  if (!secret || req.headers.get("x-relay-secret") !== secret) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const { user_id, message } = await req.json();
    if (!user_id || !message) return Response.json({ error: "bad request" }, { status: 400 });

    const base44 = createClientFromRequest(req);
    // Look up the user's email. Verify this entity call against your app; if your app stores users
    // differently, adjust here. SendEmail only delivers to registered users of the app.
    const user = await base44.asServiceRole.entities.User.get(String(user_id));
    if (!user?.email) return Response.json({ error: "user not found" }, { status: 404 });

    await base44.asServiceRole.integrations.Core.SendEmail({
      to: user.email,
      subject: "Medication reminder",
      body: `${message}\n\nYou're getting this because you turned on reminders in the app. ` +
        "You can turn them off for any medication in the You tab.",
    });
    return Response.json({ ok: true });
  } catch (_err) {
    return Response.json({ error: "relay failed" }, { status: 500 });
  }
});
