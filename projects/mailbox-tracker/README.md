# Mailbox Item Tracker

> Part of **[pwnkw_ha](../../)** — Kenneth's collection of Home Assistant projects.

> ### ⚠ Breaking change, September 2026 — if you installed this before now, your counter is probably dead
>
> Earlier versions deduped on the IMAP **`uid`**. **That was never sound**, and the failure is
> **silent and permanent** — the count simply stops rising and nothing reports an error.
> See [Why the UID dedup was wrong](#why-the-uid-dedup-was-wrong).
> **[Upgrading](#upgrading-from-the-uid-version) takes one file, one helper and a restart.**

Know how many pieces of mail are waiting in your remote / CMRA mailbox **before** you drive over. This turns your mailbox provider's "*N* new mail item(s)" notification emails into a live count on your dashboard — and clears it automatically when you show up to collect.

| Control | What it does |
|---|---|
| **Items waiting** | Live count of uncollected items, updated the moment a notice email arrives |
| **Arrive at the mailbox** | Presence zone auto-resets the count to 0 (you collected everything) |
| **Reset button** | Manual "set to 0" backstop |

## Why it's built this way

The naive version — a scheduled script that tallies the emails — **lags**. Mail arrives at 11am, you grab it at 2pm, and the next morning's run happily re-adds it, so the dashboard sends you on a wasted trip.

This design has **no script and no polling in the counting path**. Home Assistant's IMAP integration fires an event the instant a notice email lands; an automation reads the item count out of the email and adds it. The count is a *real-time function of email arrivals*, so there's nothing running "later" to get it wrong. Presence at the mailbox zeroes it. That's the whole system.

```
Provider "new item" email ─► your inbox ─► HA IMAP ─(imap_content event)─► +N ─┐
                                                 parse count, dedup by Date hdr  ▼
                                                                    ┌─────────────────────────┐
   You arrive at the mailbox ─► zone enter (+1-min dwell) ─► reset ─► │ input_number.           │
                                                                    │   mailbox_items_waiting │  ─► dashboard
                                                                    └─────────────────────────┘
```

## How it works

- **Arrival** — Your provider (e.g. **Anytime Mailbox**) emails a "*N* new mail item(s)…" notice. A HA **IMAP** integration watches your inbox for those and fires an `imap_content` event. An automation:
  - **parses `N`** from the email body (`regex_findall('(\d+)\s+new mail item')`),
  - **adds it** to `input_number.mailbox_items_waiting`,
  - **dedups on the email's `date` header**, held as a rolling list of the last 20 notices in `input_text.mailbox_processed_notices`. The question asked is *"have I already seen this exact notice?"* — restarts and re-syncs can't double-count, and the automation never has to write back to your mailbox.
  - A **list**, not a high-water mark, so dedup needs no assumption about ordering. A high-water mark ("is it newer than the last one?") silently drops a **provider backfill** — which is real: providers go dark for days and then send a batch of catch-up notices.
- **Pickup** — a **zone** at your mailbox's address plus the `person` entities of everyone who collects (list them all in the trigger). Enter the zone and stay **≥1 minute** (a **dwell filter**, so a drive-by doesn't count), and the count resets to 0 and stamps `input_datetime.last_mailbox_pickup`. Assumes you clear the whole box on a visit.
- **Backstop** — an `input_button` to zero it by hand if presence ever misses.

No cloud webhook, no external server, no LLM — just an IMAP event and a zone.

## Why the UID dedup was wrong

Every version of this project before September 2026 deduped like this:

```jinja
{{ (trigger.event.data.uid | int(0)) > (states('input_number.mailbox_last_uid') | int(0)) }}
```

The README claimed *"IMAP UIDs only increase, so this is idempotent."* **That claim was false**, and
it is contradicted by the integration's own maintainer. On the pull request that added the field
([home-assistant/core#114432](https://github.com/home-assistant/core/pull/114432)), code owner
**jbouwh** wrote:

> "Note that `uid` changes when an email is removed from the mailbox, So it is not unique to the message."

It was merged anyway — the author needed it to delete mail, which is a legitimate use. But the
[integration docs](https://www.home-assistant.io/integrations/imap/) describe it only as *"Latest
`uid` of the message"*, with **no warning**, which is how this project came to lean on it.

**What it looks like in the wild.** One message was observed reporting **uid 51, 52, 53, 54 across
four events in four seconds** — identical `date`, `subject` and body each time — while the stored
high-water mark sat at **92**. Every notice arrived numbered *below* the mark and was discarded.
That had been happening for **eight days**, and would have continued forever:

- no error, no log line, no repair item
- the dashboard shows a plausible number — usually `0`, i.e. "no mail waiting"
- the failure mode is **you stop being told about mail**, which is exactly what you installed this for

**The lesson worth stealing even if you never use this project:** ask what a field's *source* says it
guarantees, not what its name implies. `uid` sounds like a unique id. The maintainer had already said
in review that it isn't.

## Upgrading from the UID version

1. Replace `packages/mailbox_tracker.yaml` with this version.
2. Restart Home Assistant. The new `input_text.mailbox_processed_notices` helper is created by the
   package — nothing to add by hand.
3. **Set `input_number.mailbox_items_waiting` to your real current count.** Whatever it reads now is
   almost certainly wrong, and the arrival path will pick up from there.
4. `input_number.mailbox_last_uid` is no longer referenced. **Delete it whenever you like**, or leave
   it — nothing reads it.

Do **not** simply reset the old UID mark to 0 and keep the old design. It will work briefly and then
freeze again the next time the number moves the wrong way.

## Requirements

- Home Assistant (built against 2026.8).
- Your mailbox notifications delivered to an **IMAP-accessible inbox** (built against **Fastmail**; any IMAP host works).
- A provider that emails item notifications (built against **Anytime Mailbox**; adjust the search + regex for others).
- For the auto-reset only: a **person** entity with device tracking and a **zone** at your mailbox's address. (The manual button works without them.)

## Install

1. **IMAP integration** (HA UI — *not* YAML): Settings ▸ Devices & Services ▸ **Add Integration ▸ IMAP**.
   - **Server / port:** your IMAP host, e.g. `imap.fastmail.com` : `993`. Use an **app password**, not your login password.
   - **Folder:** `INBOX`.
   - **Search:** `SUBJECT "New Mail" FROM "anytimemailbox"` — matches only the notices. **Adjust to your provider's sender/subject.**
   - **Include "Body text"** so the event carries the body to parse.
   - *Tip:* don't filter on `UnSeen` if something marks these read quickly (you'd match nothing) — filter by sender/subject as above; the dedup list handles the rest.
2. **Package:** copy `packages/mailbox_tracker.yaml` into your HA `packages/` folder and add the include from [`configuration.example.yaml`](configuration.example.yaml).
3. **Edit two references** in the package (and leave the two `OPTIONAL` steps disabled unless you have read their comments — they move mail in your account): list everyone who collects the mail under the pickup trigger (`person.you`, `person.partner`, …), and ensure a `zone.mailbox` exists at your mailbox's address (or rename the zone reference).
4. **Restart** Home Assistant.
5. **Dashboard:** add the card from [`dashboard/mailbox_card.yaml`](dashboard/mailbox_card.yaml).
6. **Seed** the current real count via the button/helper — you're live.

## Verify without waiting for mail

From **Developer Tools ▸ Events**, fire event type `imap_content` with:

```yaml
subject: "New Mail"
date: "2030-01-01 00:00:00+00:00"
text: "1 new mail item(s) have been added to your mailbox."
```

`mailbox_items_waiting` should jump by 1, and `mailbox_processed_notices` should gain `1893456000`.
**Re-fire the exact same payload → no change.** That is the dedup proving itself, and it is the test
that matters. Change the `date` and it counts again. Clear the helper and set the count back when done.

> **A synthetic test can pass while every real email fails.** This project has been bitten twice.
> Once by an inverted `initial` clause that only real payloads exposed, and once by the `uid` dedup
> below — which passed every hand-test, worked for weeks, and then silently stopped counting.
> **If you can, confirm against one real notice before trusting it.**

## Files

```
packages/mailbox_tracker.yaml   # helpers (count, processed-notice list, pickup time, reset button) + 1 automation
dashboard/mailbox_card.yaml     # glanceable entities card
configuration.example.yaml      # the packages: include line
```

## Security

No credentials are committed. The IMAP app password is entered in the HA UI and stored in the config entry — not in this repo and not in `secrets.yaml`. `.storage/` is git-ignored.

## Credits & Attribution

- **Home Assistant IMAP integration** — the `imap_content` event this is built on:
  [docs](https://www.home-assistant.io/integrations/imap) ·
  [source](https://github.com/home-assistant/core/tree/dev/homeassistant/components/imap).
- **Anytime Mailbox** / **Fastmail** — the mailbox provider and IMAP host this was built against.

Trademarks (Anytime Mailbox, Fastmail) belong to their respective owners; this project is not affiliated with or endorsed by any of them.

---

*Built with Claude Code. Never drive to an empty mailbox again. 📬*
