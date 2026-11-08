# Talking to route-book

Two Siri Shortcuts. No app to install, no speech-to-text to set up, no text-to-speech
either — **Siri does both**, and your phone already has it.

## How it works

```
You speak
   ↓
Siri dictates the text            ← Dictate Text action, on-device or Apple's
   ↓
POST to /ask on your Mac          ← Get Contents of URL
   ↓
route-book parses and answers     ← the rules first, the model only if needed
   ↓
Siri reads the reply aloud        ← Speak Text
```

The reply comes back **in the HTTP response**, not as a message to read later. That
matters: there is no polling, no queue, and nothing to check. You speak, you hear the
answer.

**Nothing is installed for speech.** Dictation and speech are Siri's, and the
renderer in this plugin returns *text* — deliberately. If it synthesised audio it
would need a voice, a format, and a codec on the host, and it would sound like a
robot reading a database.

---

## Shortcut 1 — "Ask my schedule"

For questions and for bookings.

Siri: *"Ask my schedule"* → dictate → hear the answer.

**Build it:**

1. Shortcuts app → **+** → name it **Ask my schedule**.
2. Add **Dictate Text**.
   - Language: your own. Stop listening: **After Pause**.
3. Add **Get Contents of URL**.
   - URL: `http://YOUR-MAC:8585/ask`
   - Method: **POST**
   - Request Body: **JSON**
   - Add two fields:
     - `text` (Text) → the **Dictated Text** variable
     - `spoken` (Boolean) → **on**
   - Headers: add one —
     - Key `Authorization`, Value `Bearer YOUR-TOKEN`
4. Add **Get Dictionary Value**.
   - Get **Value** for key `reply` in **Contents of URL**.
5. Add **Speak Text**, using the **Dictionary Value** from step 4.
6. **Say "Hey Siri, Ask my schedule"** and try it.

Try saying:

- *"Bala, next Thursday, 3 hrs"*
- *"Recommend the best day for Bala in two weeks"*
- *"What does my week look like"*
- *"Cancel Bala Construction"*

### What you will hear

Your booking gets **read back before it is written**:

> *"That is Bala Construction, next Thursday, for 3 hours. Should I book it?"*

Say **"yes"** and it books. Say nothing, or anything else, and nothing is written.

**That read-back is the whole point of voice mode.** Dictation mishears — "Thursday"
and "Tuesday" are one syllable apart on a bad connection — and a misheard date
creates a real appointment in a real customer's week. You are listening, not reading,
so hearing it back is your only chance to catch it. Confirmations expire after three
minutes, because a "yes" ten minutes later is one nobody remembers giving.

---

## Shortcut 2 — "Note for the vault"

For thoughts you want to keep, rather than questions you want answered. This one uses
`/note`, which files text into `Inbox/` for the vault processor.

**Build it:**

1. **+** → name it **Note for the vault**.
2. **Dictate Text**.
3. **Get Contents of URL**.
   - URL: `http://YOUR-MAC:8585/note`
   - Method: **POST**, Request Body **JSON**
   - Field `text` → the **Dictated Text** variable
   - Header `Authorization`: `Bearer YOUR-TOKEN`
4. Add **Show Notification** (optional) so you know it landed.

No `Speak Text` — this one is a filing action, and a robot saying "saved" at you gets
tiresome by the third day.

---

## Or skip the Shortcuts entirely

If you only want reminders and one-off questions, plain Siri already reaches Hermes
through the same endpoint. Shortcuts are worth the ten minutes when you want the
*scheduling* phrases to work hands-free — that is when you are standing in a van with
a phone in one hand.

---

## The endpoint

| | |
|---|---|
| URL | `http://YOUR-MAC:8585/ask` |
| Method | POST |
| Body | `{"text": "...", "spoken": true}` |
| Header | `Authorization: Bearer <token>` |
| Response | `{"status": "ok", "reply": "..."}` |

- `spoken` is optional. Without it the reply is written for a screen and can be
  longer.
- The **token is the security control**, and it is required. The endpoint refuses to
  start on a reachable interface without one. The token lives beside the server
  script, or in `VAULT_INGEST_TOKEN`.
- **Any interface works** — home LAN, Tailscale, or a tunnel. The service binds
  `0.0.0.0` on purpose. Binding it to one network only moves the problem and secures
  nothing.

### Troubleshooting

| What you get | What happened |
|---|---|
| *"That did not work"* in your ear | The agent replied, but not as a booking. Ask again with the client's name. |
| Nothing at all | The Shortcut could not reach the Mac. Check the URL and that the server is running. |
| A 401 in a notification | Wrong or missing token. Check the `Authorization` header. |
| It answers a different question | Dictation garbled the sentence. The read-back is there to catch exactly this before anything is written. |

---

## Why the renderer returns text

Worth writing down, because it is a design choice someone will otherwise "fix".

The spoken renderer produces **text**, and Siri speaks it. The alternatives were to
synthesise audio on the host or to send structured data to the phone.

- **Host-side audio** means picking a voice, a sample rate, and a codec — and then
  maintaining them. It also breaks the `Speak Text` action, which is free, uses the
  voice already configured on the device, and respects the user's own settings for
  speed and accent.
- **Structured data to the phone** means the Shortcut has to know how to say a date.
  That logic belongs in one place, not in a Shortcut that is edited by hand.

So the host renders the *words*, and the device owns the *sound*.
