# Mailbox Item Tracker

> Part of **[pwnkw_ha](../../)** — Kenneth's collection of Home Assistant projects.

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
                                                    parse count, dedup by UID   ▼
                                                                    ┌─────────────────────────┐
   You arrive at the mailbox ─► zone enter (+1-min dwell) ─► reset ─► │ input_number.           │
                                                                    │   mailbox_items_waiting │  ─► dashboard
                                                                    └─────────────────────────┘
```

## How it works

- **Arrival** — Your provider (e.g. **Anytime Mailbox**) emails a "*N* new mail item(s)…" notice. A HA **IMAP** integration watches your inbox for those and fires an `imap_content` event. An automation:
  - **parses `N`** from the email body (`regex_findall('(\d+)\s+new mail item')`),
  - **adds it** to `input_number.mailbox_items_waiting`,
  - **dedups** with a UID high-water mark (`input_number.mailbox_last_uid`). IMAP UIDs only increase, so "count only if UID > last seen" is idempotent — restarts and re-syncs can't double-count, and the automation never has to write back to your mailbox.
- **Pickup** — a **zone** at your mailbox's address plus your `person` entity. Enter the zone and stay **≥1 minute** (a **dwell filter**, so a drive-by doesn't count), and the count resets to 0 and stamps `input_datetime.last_mailbox_pickup`. Assumes you clear the whole box on a visit.
- **Backstop** — an `input_button` to zero it by hand if presence ever misses.

No cloud webhook, no external server, no LLM — just an IMAP event and a zone.

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
   - *Tip:* don't filter on `UnSeen` if something marks these read quickly (you'd match nothing) — filter by sender/subject as above; the UID dedup handles the rest.
2. **Package:** copy `packages/mailbox_tracker.yaml` into your HA `packages/` folder and add the include from [`configuration.example.yaml`](configuration.example.yaml).
3. **Edit two references** in the package: `person.you` → your person entity, and ensure a `zone.mailbox` exists at your mailbox's address (or rename the zone reference).
4. **Restart** Home Assistant.
5. **Dashboard:** add the card from [`dashboard/mailbox_card.yaml`](dashboard/mailbox_card.yaml).
6. **Seed** the current real count via the button/helper — you're live.

## Verify without waiting for mail

From **Developer Tools ▸ Events**, fire event type `imap_content` with:

```yaml
subject: "New Mail"
uid: 999999
initial: false
text: "1 new mail item(s) have been added to your mailbox."
```

`mailbox_items_waiting` should jump by 1 and `mailbox_last_uid` to 999999. Re-fire with the same or a lower UID → no change (dedup proven). Set both back to 0 when done.

## Files

```
packages/mailbox_tracker.yaml   # helpers (count, UID mark, pickup time, reset button) + 3 automations
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
