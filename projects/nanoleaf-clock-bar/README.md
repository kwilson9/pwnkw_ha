# Nanoleaf Room Clock (Linear Bar)

> Part of **[pwnkw_ha](../../)** — Kenneth's collection of Home Assistant projects.
>
> **Status: v1.0** — running daily on real hardware. See [Known limitations](#known-limitations).

A **Nanoleaf Lines** bar turned into a *linear clock*: seven light bars, mounted in a
straight row, showing minutes-within-the-hour as they fill left to right. The seventh
bar shows the **outdoor temperature**, and its other half is held dark for a future notifier.

It also takes over the whole bar for a few special jobs — a **meeting countdown** that
drains the bar to empty exactly at meeting time, a **partner bedtime wind-down**, and a
**Pride rainbow**.

The countdown is the one built for a specific problem: **working from home.** If your
work calendar is the thing that governs your day, you want an ambient "the next meeting
is coming" signal in the rooms where you *don't* have a screen — readable from across
the room, at a glance, without picking up a phone or alt-tabbing to a calendar. The bar
goes orange at fifteen minutes out, marches down through the last fourteen, and is
completely dark at the moment the meeting starts. You learn to read it without thinking
about it.

> 🚿 **The origin story, which is also the actual design constraint:** this bar lives in
> a bathroom. The countdown exists because of one specific, recurring, entirely
> self-inflicted failure — you're in the shower, you have a 9:00, and you have no idea
> whether it's 8:47 or 8:58. Your phone is in another room. Your hands are full of
> shampoo. A watch is not an option and neither, frankly, is squinting at a phone
> screen through steam.
>
> So the wall tells you instead. **Orange means start wrapping up. The marching zone
> means you are committed. When the bar goes dark, you are not "about to be late" —
> you are late, and you will be joining with wet hair.** It is the only clock in this
> house that has ever successfully changed anyone's behavior.

Driven by a custom **AppDaemon** app that talks to the Nanoleaf over its **local REST
API** — not through a Home Assistant `light` entity, because per-zone animation frames
aren't expressible through the light domain.

| Behavior | What you see |
|---|---|
| Normal | Minutes fill the bar; completed minutes solid, future minutes dim. The active 5-minute zone is a lighter shade that **breathes** (fades to dark and back, 3s each way) while the room is occupied, and holds steady when it's empty |
| Color arc | Warm reds/ambers overnight → sky blue-white at solar noon → violet at dusk |
| Line 7 right | Outdoor temperature, as a stepped color scale |
| Line 7 left | Dark — reserved for a future notifier |
| Meeting countdown | Whole bar goes orange, then a marching zone, then drains to empty at meeting time — an ambient WFH warning that your next work-calendar event is imminent |
| Post-meeting | Solid pink, one line per minute, for six minutes |
| Partner wind-down | Signals the hour before a shift-derived bedtime |
| Pride | Full-bar ROYGBIV — a *fallback* when the clock is switched off, never an override |

## How it works

```
Home Assistant entities ──► AppDaemon app ──► Nanoleaf local REST API ──► the bar
  (sun, motion, temp,        (mode select +      (PUT /effects,
   calendars, toggles)        frame render)       animData frames)
```

The app polls once a minute (plus a 30s tick) and pushes a complete frame describing
all 14 zones. Mode selection is a strict priority ladder in `get_mode()`:

1. **`meeting_countdown`** — workday only, 07:30–17:00, qualifying calendar event ≤15 min out
2. **`post_meeting`** — the first 6 minutes after a meeting's start time
3. **`partner_winddown`** — the hour before a bedtime derived from the partner's shift
4. **`normal`** — the within-hour clock

## Hardware

- **Nanoleaf Lines**, model **NL59**, API port **16021**
- **7 Lines in a straight horizontal row**, left → right
- Each Line is **one bar with two addressable zones** — `a` (left half) and `b` (right
  half). This is native to Lines; it's what gives 14 segments from 7 bars.

Two things that will cost you an evening if you don't know them:

- The device's `panelLayout` reports **23 nodes, but only 14 light.** The other **9 are
  the hex connector joints** — they appear in the layout data and are not lightable.
  Line segments report orientation `0` or `180`; joints report `60/120/240/300`.
- **The bar is *vertical* in the device's coordinate space** even when mounted
  horizontally — every node shares an `x`, differing only by `y`. Sort by `y` and
  reverse it to get physical left→right. Don't try to cluster by `x`.

Panel IDs are per-device and **change when you replace a bar**, so yours will differ.
Discover your own with the tools in [`tools/`](https://github.com/kwilson9/pwnkw_ha/tree/main/projects/nanoleaf-clock-bar/tools) — see [Panel discovery](#panel-discovery).

## Requirements

- **Home Assistant** (built against 2026.8) with the **AppDaemon** add-on
  (built against AppDaemon 4.5.13 / Python 3.12)
- A **Nanoleaf Lines** device with a local **API auth token**
- The machine running AppDaemon must be **on the same LAN** as the device
- Home Assistant entities for: room motion/occupancy, outdoor temperature, `sun.sun`,
  a workday binary sensor, and (optionally) two calendars

## Install

1. **Get a Nanoleaf auth token** — hold the device's power button ~5–7s until the LED
   flashes, then within 30s:
   `curl -X POST http://<device-ip>:16021/api/v1/new`
2. **Copy the app** — put `nanoleaf_clock_bar.py` in your AppDaemon apps directory, and
   merge `apps.yaml.example` into that directory's `apps.yaml`.
   ⚠️ **Read [the app_dir section](#gotcha-1-appdaemon-reads-a-different-config-than-you-do) first** — "your apps directory" is the single most common place this goes wrong.
3. **Set your device details** — edit the constants at the top of `nanoleaf_clock_bar.py`:
   `NANOLEAF_IP` and `NANOLEAF_TOKEN`. (See `apps.yaml.example` for moving these into
   `!secret` instead, which is recommended.)
4. **Point it at your entities** — edit the entity constants (table below) to match your
   system. There is no autodiscovery; these are yours to set.
5. **Discover your panel IDs** and replace the `ARMS` map — see below.
6. **Create the two helpers**: `input_boolean.nano_clock_display` (master on/off) and
   `input_boolean.pride_mode`.
7. **Restart AppDaemon.** Watch its log for `NanoleafClock initialized`.

### Entities to configure

| Constant | Placeholder shipped | What it's for |
|---|---|---|
| `OCCUPANCY_SENSOR` | `binary_sensor.room_motion` | Brightness (occupied vs empty); gates the breathing active zone |
| `TEMP_SENSOR` | `sensor.outdoor_temperature` | Line 7b temperature gauge (°F) |
| `SUN_ENTITY` | `sun.sun` | Day/night brightness and the color arc (uses the `elevation` attribute) |
| `WORKDAY_SENSOR` | `binary_sensor.workday_sensor` | Gates the meeting countdown |
| `MY_CALENDAR` | `calendar.your_work_calendar` | Meeting countdown / post-meeting |
| `PARTNER_CALENDAR` | `calendar.partner_work_calendar` | Bedtime wind-down; shift time parsed from the event title as `HHMM / HHMM` |
| `CLOCK_ENABLED` | `input_boolean.nano_clock_display` | Master kill switch |
| `PRIDE_ENABLED` | `input_boolean.pride_mode` | Pride rainbow fallback |

Meeting titles containing any of `MEETING_EXCLUDE` (`lunch`, `dns`, `out`, `dr`,
`medical` — case-insensitive) are skipped by the countdown.

### Panel discovery

Run these from a machine **on the same LAN** (the device isn't reachable remotely):

```
python3 tools/nanoleaf_orient.py --discover     # dump panel IDs + x/y/orientation
python3 tools/nanoleaf_guided_walk.py           # lights each node; you type 1a, 7b, or x
python3 tools/nanoleaf_orient.py --rainbow      # ROYGBIV left→right sanity check
```

`nanoleaf_guided_walk.py` is the reliable one: it lights a single node at a time and you
tell it what you're looking at, which is how the verified map gets built. Put the result
in the `ARMS` structure near the top of the app. `MEETING_MARCH` and `EXTINGUISH_ORDER`
are **derived** from `ARMS`, so they follow automatically.

---

## Gotchas

These are the two things that actually cost real time. Both are written up because
they're non-obvious and generalize beyond this project.

### Gotcha 1: AppDaemon reads a different `/config` than you do

**Symptom:** you put your app in Home Assistant's `config/apps/` folder, AppDaemon
starts cleanly, and your app never loads. Or you set `app_dir` and *nothing changes*.

On the Home Assistant OS add-on, AppDaemon runs `appdaemon -c /config`, and the add-on
declares `map: [addon_config:rw, homeassistant_config:rw, ...]`. Supervisor mounts those
at different places, so **inside the AppDaemon container**:

| Path inside the container | What it actually is |
|---|---|
| `/config` | the **add-on's own** config dir — `/addon_configs/a0d7b954_appdaemon/` |
| `/homeassistant` | **Home Assistant's** config dir — what your file editor shows as `/config` |

So the two directories that look identically named are not the same place:

```
Studio Code Server shows          AppDaemon sees
  /config/apps/            ───►     /homeassistant/apps/
  /addon_configs/a0d7b954_appdaemon/apps/  ───►  /config/apps/
```

**Consequences:**

- By default (no `app_dir`), AppDaemon loads from **the add-on's own** `apps/` folder,
  not from Home Assistant's config directory.
- If you keep apps in HA's config dir, you must set **`app_dir: /homeassistant/apps`**.
- Setting `app_dir: /config/apps` is a **silent no-op** — it resolves right back to the
  add-on's own folder, which is where it was already looking. Nothing errors. Nothing
  changes. This is the trap.

**Confirm which one is live** — AppDaemon prints it on every start:

```
INFO AppDaemon: Configuration read from: /config/appdaemon.yaml
INFO AppDaemon: Using /homeassistant/apps as app_dir
```

A failed import helpfully prints its search path too, which is a second confirmation:

```
ModuleNotFoundError: No module named 'hello'
  Import paths:
    /homeassistant/apps
```

Reference: Supervisor defines `PATH_HOMEASSISTANT_CONFIG = /homeassistant` and
`PATH_PUBLIC_CONFIG = /config`.

**Related:** AppDaemon does **not** inherit `latitude` / `longitude` / `elevation` /
`time_zone` from Home Assistant — its docs mark all four **required**, and the add-on
ships with Amsterdam sample values. Set them, and note **`elevation` is in metres** in
both AppDaemon and Home Assistant regardless of your unit system (HA takes a bare
integer and hands it to `astral`, which documents metres). 250 ft is `76`, not `250`.

### Gotcha 2: multi-frame panels need `animType: "custom"` — `static` rejects them

**Any panel with more than one frame makes a `"static"` write fail with HTTP 400, and the
whole write is thrown away** — every other panel in it too. Send the same `animData` as
`"animType": "custom"` and it is accepted and plays. The app now picks `custom` whenever
a frame contains a loop (the breathing zone) and `static` otherwise.

Found the hard way (2026-09): v1.0 sent its night heartbeat — a 2-frame fade — as
`static`, so **every redraw at night with the room occupied was rejected** and the bar
sat on whatever it last showed. Nothing in Home Assistant shows it; only the app's
`Nanoleaf error: 400` warnings do. **Log the HTTP status of every push.**

Isolated by hand against firmware 12.4.1, same payload each time:

| `animType` | multi-frame panel | `loop` | result |
|---|---|---|---|
| static | yes | true / false | **400** |
| static | no | true / false | 204 |
| custom | yes | true / false | 204 |

**What this means for the older "loops freeze the bar" rule.** v1.0's notes said the device
silently ignores writes while a loop is running (`loop → loop`, `loop → static`). With
`custom` writes, **a loop replaced by another loop updated the bar correctly** (checked by
eye, and the app's own 5-minute redraws log no errors). The old freeze may have been this
400 all along — that is a hypothesis; it was not re-tested with `static` loops. The meeting
countdown still uses static writes only (`loop: false`), spaced ~0.7s apart —
`tools/nanoleaf_countdown_test.py` demonstrates that pattern.

---

## Files

```
nanoleaf_clock_bar.py       # the app — class NanoleafClock, module nanoleaf_clock_bar
apps.yaml.example           # AppDaemon app registration (+ !secret hardening notes)
appdaemon.example.yaml      # minimal appdaemon.yaml, with the app_dir trap documented
tools/
  nanoleaf_orient.py        # --discover panel IDs/geometry, --rainbow, --blocks
  nanoleaf_guided_walk.py   # interactive per-node identifier; builds the panel map
  nanoleaf_countdown_test.py# standalone countdown player; the static-writes pattern
```

## Known limitations

This is **v1.0** — honest about what isn't proven:

- **The meeting countdown has not had a clean end-to-end run on hardware.** The
  all-static rewrite is verified offline (march order, extinguish order, no loops), but
  the `−1:00 →` final sweep handoff has historically been the fragile spot.
- **Panel IDs are device-specific and change when a bar is replaced.** Replacing one
  Line changes only that Line's two IDs; diff a fresh `panelLayout` dump against your
  previous one to find them.
- **Brightness constants are tuned by editing the file.** No dashboard for it.
- **Some docstrings in the app are stale** relative to the tuned constants.
- **The published copy is sanitized, not a byte-mirror** of the running app —
  credentials and personal entity IDs are placeholders. It is a reference
  implementation, so expect to edit before it runs.

### Ideas for later

- Hour-of-day indicator (Line 7's original reserved purpose)
- A dashboard to tune brightness live instead of editing constants
- A general notification effect on Line 7a (now held dark for it)

## Credits & Attribution

- **Nanoleaf** local OpenAPI (`/api/v1/<token>/effects`) — Nanoleaf is a trademark of
  Nanoleaf; this project is not affiliated with or endorsed by them.
- **[AppDaemon](https://appdaemon.readthedocs.io/)** — sandboxed Python app runtime for HA.
- **[AppDaemon add-on](https://github.com/hassio-addons/addon-appdaemon)** by Franck Nijhof.
- **[astral](https://github.com/sffjunkie/astral)** — the sun math behind HA's `sun.sun`.
