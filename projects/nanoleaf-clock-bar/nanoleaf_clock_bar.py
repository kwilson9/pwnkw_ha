"""
Nanoleaf Room Clock — AppDaemon App  (LINEAR BAR VERSION)
=============================================================
Single horizontal bar using Nanoleaf Lines D486.

This is the bar remap of the original star/clock app. The clock logic is
IDENTICAL — same modes, colors, occupancy, breathe — but the six original
Lines now render as one straight bar filling LEFT -> RIGHT instead of
radiating clockwise. The former clockwise progression (00->02->04->06->08->10,
inner half then outer half) maps directly onto left->right fill order.

A second physical bar was added (11 extra addressable units). During normal
clock operation those are held DARK (reserved for a future hour-of-day
indicator).

PRIDE MODE:
- input_boolean.pride_mode paints ALL 23 units ROYGBIV, red(left)->violet(right).
- It is a FALLBACK, not an override: it only displays when the clock helper
  (input_boolean.nano_clock_display) is OFF and pride_mode is ON.
  Clock on  => clock always wins.
  Clock off + pride on => rainbow.
  Both off  => bar dark.

Modes (priority order):
1. Meeting countdown          — workday + calendar.your_work_calendar, 15 min out
2. Eóin bedtime wind-down     — calendar.partner_work_calendar, 1 hr before shift_start - 10h
3. Normal within-hour clock   — each Line = 10 min, first/second unit = 5 min halves

Color logic:
- Warm/cool arc peaking warm at 2 AM, coolest at solar noon
- Night mode (10 PM–4 AM): warm reds/ambers
- Day/evening: sky blues/whites cooling toward noon, warming back at dusk

Occupancy: brightness only, 10-min dim delay after sensor clears.
"""

import appdaemon.plugins.hass.hassapi as hass
import requests
import datetime
import math
import re

NANOLEAF_IP    = "YOUR_NANOLEAF_IP"
NANOLEAF_TOKEN = "YOUR_NANOLEAF_TOKEN"
NANOLEAF_URL   = f"http://{NANOLEAF_IP}:16021/api/v1/{NANOLEAF_TOKEN}/effects"

OCCUPANCY_SENSOR = "binary_sensor.room_motion"
SUN_ENTITY       = "sun.sun"
WORKDAY_SENSOR   = "binary_sensor.workday_sensor"
MY_CALENDAR      = "calendar.your_work_calendar"
PARTNER_CALENDAR    = "calendar.partner_work_calendar"
CLOCK_ENABLED    = "input_boolean.nano_clock_display"
PRIDE_ENABLED    = "input_boolean.pride_mode"
TEMP_SENSOR      = "sensor.outdoor_temperature"  # outdoor temp (°F), shown on Line 7

# Keywords to exclude from meeting countdown (case-insensitive)
MEETING_EXCLUDE  = ["lunch", "dns", "out", "dr", "medical"]

# Master brightness multipliers (applied to all computed RGB).
# Driven by sun state (sun.sun) + occupancy.
DAY_OCC     = 0.70   # daytime, someone in the room
DAY_EMPTY   = 0.45   # daytime, empty
NIGHT_OCC   = 0.35   # after sunset, someone in the room
NIGHT_EMPTY = 0.08   # after sunset, empty

# Occupied (any hour): the active 5-min zone breathes -- fades to DARK and back
# as a 2-frame device loop (sent as animType "custom"). transTime is 100 ms units.
BREATHE_TRANS = 30   # 3.0 s down to dark, 3.0 s back up

# ─── VERIFIED physical layout (guided walk) ──────────────────────────────
# 7 physical Lines, left -> right. Each Line = 2 lit units: a (left) + b (right).
#   Lines 1..6 = the clock, :00 at Line 1 (leftmost) ... :50 at Line 6
#   Line 7 = reserved, held DARK (far right)
# "inner" = a (first 5-min half), "outer" = b (second 5-min half); fills left->right.
ARMS = [
    {"name": "00", "inner": 13966, "outer": 51150},  # Line 1  :00–:09 (leftmost)
    {"name": "10", "inner": 53491, "outer": 16434},  # Line 2  :10–:19
    {"name": "20", "inner": 23652, "outer": 52389},  # Line 3  :20–:29
    {"name": "30", "inner": 14609, "outer": 51281},  # Line 4  :30–:39 (bar replaced 2026-08-17; was 48801/11872)
    {"name": "40", "inner": 48260, "outer": 11333},  # Line 5  :40–:49
    {"name": "50", "inner": 50579, "outer": 13523},  # Line 6  :50–:59
]

ALL_PANELS = [z for arm in ARMS for z in [arm["inner"], arm["outer"]]]

# Line 7 (far right) — split into two special-use zones:
HEARTBEAT_PANEL = 23507   # 7a: held DARK -- reserved for a future notifier (was a night heartbeat)
TEMP_PANEL      = 51986   # 7b: outdoor temp gauge
RESERVED_BAR    = [HEARTBEAT_PANEL, TEMP_PANEL]  # both Line-7 zones (used for off/pride)

# ── Meeting countdown zone orders (whole 14-zone bar is the canvas) ──
def _z(line, ab):  # line 1-7, ab 0=a/left 1=b/right
    return ARMS[line - 1]["inner" if ab == 0 else "outer"] if line <= 6 else \
           (HEARTBEAT_PANEL if ab == 0 else TEMP_PANEL)

# Marching blink order, -14 .. -1 (right -> left): 7b,7a,6b,6a,...,1b,1a
MEETING_MARCH = [
    _z(7,1), _z(7,0), _z(6,1), _z(6,0), _z(5,1), _z(5,0), _z(4,1),
    _z(4,0), _z(3,1), _z(3,0), _z(2,1), _z(2,0), _z(1,1), _z(1,0),
]
# Final-minute extinguish: alternate ends inward to center
EXTINGUISH_ORDER = [
    _z(1,0), _z(7,1), _z(1,1), _z(7,0), _z(2,0), _z(6,1), _z(2,1),
    _z(6,0), _z(3,0), _z(5,1), _z(3,1), _z(5,0), _z(4,0), _z(4,1),
]

# Every lit unit on the bar, in physical LEFT -> RIGHT order (Line 1 .. Line 7).
# Used for the full-bar rainbow (pride) mode.
BAR_LEFT_TO_RIGHT = [
    13966, 51150,          # Line 1 (new bar)
    53491, 16434,          # Line 2
    23652, 52389,          # Line 3
    14609, 51281,          # Line 4  (bar replaced 2026-08-17; was 48801/11872)
    48260, 11333,          # Line 5
    50579, 13523,          # Line 6
    23507, 51986,          # Line 7
]

# ─── Outdoor temp gauge (Line 7) ─────────────────────────────────────────
# Stepped color scale, °F. Each entry: (upper_bound_inclusive, (R,G,B)).
# A temp is colored by the FIRST band whose upper bound it falls at/under.
# Below 10°F clamps to white; above 110°F clamps to super-dark-red.
TEMP_BANDS = [
    ( 20, (255, 255, 255)),  # 10–20  white
    ( 32, (140, 200, 255)),  # 20–32  light blue
    ( 40, (  0,  80, 255)),  # 32–40  blue
    ( 55, (  0, 200, 180)),  # 40–55  yellowish blue (teal)
    ( 60, (220, 220,  40)),  # 55–60  warmer yellow
    ( 75, (255, 200,   0)),  # 60–75  yellow
    ( 90, (255, 120,   0)),  # 75–90  orange
    (105, (230,  20,   0)),  # 90–105 red
    (110, (110,   0,   0)),  # 105–110 super dark red
]
TEMP_MIN_COLOR = (255, 255, 255)   # below 10°F
TEMP_MAX_COLOR = (110,   0,   0)   # above 110°F

# 7-stop ROYGBIV ramp (full brightness) — red = left end, violet = right end
ROYGBIV = [
    (255,   0,   0),  # red
    (255, 110,   0),  # orange
    (255, 230,   0),  # yellow
    (  0, 200,   0),  # green
    (  0,  90, 255),  # blue
    ( 75,   0, 180),  # indigo
    (160,   0, 220),  # violet
]


class NanoleafClock(hass.Hass):

    def initialize(self):
        self.occupied        = False
        self.occupancy_timer = None
        self.last_state_key  = None   # tracks last rendered state to avoid redundant pushes
        self.pulse_toggle    = False  # legacy (unused by new countdown)
        self.sweep_timer     = None   # fast tick during the final-minute extinguish sweep

        # Fire once per minute (on the minute) — handles arm transitions and mode changes
        self.run_minutely(self.update_clock, datetime.time(0, 0, 2))

        # Also fire every 30 seconds to catch the inner→outer half transition
        self.run_every(self.update_clock, "now", 30)

        # Event-driven: occupancy, sun, calendar, kill switch
        self.listen_state(self.on_occupancy, OCCUPANCY_SENSOR)
        self.listen_state(self.on_state_change, SUN_ENTITY)
        self.listen_state(self.on_state_change, MY_CALENDAR)
        self.listen_state(self.on_state_change, PARTNER_CALENDAR)
        self.listen_state(self.on_clock_toggle, CLOCK_ENABLED)
        self.listen_state(self.on_pride_toggle, PRIDE_ENABLED)
        self.listen_state(self.on_state_change, TEMP_SENSOR)

        # Set initial occupancy state
        self.occupied = self.get_state(OCCUPANCY_SENSOR) == "on"

        self.log("NanoleafClock initialized")

    # ─── State listeners ───────────────────────────────────────────────────

    def on_occupancy(self, entity, attribute, old, new, kwargs):
        if new == "on":
            self.occupied = True
            if self.occupancy_timer:
                self.cancel_timer(self.occupancy_timer)
                self.occupancy_timer = None
            self.update_clock({})
        else:
            # Start 10-minute delay before dimming
            if self.occupancy_timer:
                self.cancel_timer(self.occupancy_timer)
            self.occupancy_timer = self.run_in(self.dim_after_delay, 600)

    def dim_after_delay(self, kwargs):
        self.occupied = False
        self.occupancy_timer = None
        self.update_clock({})

    def on_state_change(self, entity, attribute, old, new, kwargs):
        self.update_clock({})

    def on_clock_toggle(self, entity, attribute, old, new, kwargs):
        self.last_state_key = None  # force a fresh push
        if new == "off":
            # Clock off → hand the bar to pride if it's on, else go dark.
            if self.get_state(PRIDE_ENABLED) == "on":
                self.update_clock({})   # will render pride (clock is off)
            else:
                self.send_all_off()
        else:
            # Clock on → manual reset: resume from the REAL sensor state, not
            # stale memory. Cancel any pending dim timer so it starts clean.
            if self.occupancy_timer:
                self.cancel_timer(self.occupancy_timer)
                self.occupancy_timer = None
            self.occupied = self.get_state(OCCUPANCY_SENSOR) == "on"
            # Clock on → clock wins over pride.
            self.update_clock({})

    def on_pride_toggle(self, entity, attribute, old, new, kwargs):
        self.last_state_key = None
        # Pride only matters when the clock is off.
        if self.get_state(CLOCK_ENABLED) == "on":
            return  # clock has priority; ignore pride changes
        if new == "on":
            self.update_clock({})       # render pride
        else:
            self.send_all_off()         # pride off + clock off → dark

    # ─── Main update ───────────────────────────────────────────────────────

    def update_clock(self, kwargs):
        # Clock has priority. Pride is a fallback that ONLY runs when the clock
        # helper is off AND the pride helper is on.
        if self.get_state(CLOCK_ENABLED) != "on":
            if self.get_state(PRIDE_ENABLED) == "on":
                sun_up = self.get_state(SUN_ENTITY) == "above_horizon"
                state_key = ("pride", sun_up, self.occupied)
                if state_key != self.last_state_key:
                    self.last_state_key = state_key
                    self.render_pride()
            return

        now  = datetime.datetime.now()
        mode = self.get_mode(now)

        # Meeting countdown changes sub-minute (marching blink steps + the final
        # 60s sweep), so bypass dedup and render every tick while it's active.
        if mode != "meeting_countdown":
            self._cancel_sweep_timer()
        else:
            self.last_state_key = None
            self.render(now, mode)
            return

        # Build a state key — only push to Nanoleaf if something meaningful changed
        hour      = now.hour
        minute    = now.minute
        arm       = minute // 10
        half      = 0 if (minute % 10) < 5 else 1
        sun_up    = self.get_state(SUN_ENTITY) == "above_horizon"
        temp_band = self.temp_color(self._read_temp())  # include so Line 7 repaints on change
        state_key = (mode, hour, arm, half, self.occupied, sun_up, temp_band)

        if state_key == self.last_state_key:
            return  # nothing changed, skip the API call

        self.last_state_key = state_key
        self.render(now, mode)

    def get_mode(self, now):
        """Determine current display mode."""
        hour   = now.hour
        minute = now.minute

        # Meeting countdown — workday only, 7:30 AM–5 PM
        if (self.get_state(WORKDAY_SENSOR) == "on"
                and 7 <= hour < 17
                and not (hour == 7 and minute < 30)):
            mins_to_meeting = self.mins_to_next_meeting(now)
            if mins_to_meeting is not None and mins_to_meeting <= 15:
                return "meeting_countdown"

        # Post-meeting pink (first 6 minutes after meeting start)
        mins_since_meeting = self.mins_since_last_meeting_start(now)
        if mins_since_meeting is not None and 0 <= mins_since_meeting < 6:
            return "post_meeting"

        # Eóin bedtime wind-down — 1 hour before shift_start - 10h
        if 17 <= hour or hour == 0:
            partner_wind_down = self.partner_wind_down_active(now)
            if partner_wind_down:
                return "partner_winddown"

        return "normal"

    # ─── Render ────────────────────────────────────────────────────────────

    def render_pride(self):
        """Static full-bar ROYGBIV across EVERY unit, red=left -> violet=right.
        Dimmed by sun + occupancy like the clock. Covers the new added bar too."""
        ids = BAR_LEFT_TO_RIGHT
        n   = len(ids)
        bright = self.brightness_factor(self.occupied)
        parts = []
        for i, pid in enumerate(ids):
            frac = i / (n - 1) if n > 1 else 0.0
            r, g, b = self.rainbow_color(frac)
            r = max(0, min(255, int(r * bright)))
            g = max(0, min(255, int(g * bright)))
            b = max(0, min(255, int(b * bright)))
            parts.append(f"{pid} 1 {r} {g} {b} 0 1")
        anim_data = f"{len(parts)} " + " ".join(parts)
        self.send_effect_raw(anim_data, loop=False)

    def rainbow_color(self, frac):
        """frac in [0,1] -> RGB along the ROYGBIV ramp."""
        if frac <= 0:
            return ROYGBIV[0]
        if frac >= 1:
            return ROYGBIV[-1]
        span = frac * (len(ROYGBIV) - 1)
        i = int(span)
        t = span - i
        c1, c2 = ROYGBIV[i], ROYGBIV[i + 1]
        return tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))

    def render(self, now, mode):
        hour   = now.hour
        minute = now.minute
        second = now.second

        sun_elevation = self.get_state(SUN_ENTITY, attribute="elevation") or 0

        if mode == "normal":
            self.render_normal(hour, minute, second, sun_elevation)

        elif mode == "meeting_countdown":
            self.render_meeting_countdown(now)

        elif mode == "post_meeting":
            self.render_post_meeting(now)

        elif mode == "partner_winddown":
            self.render_partner_winddown(now)

    def render_normal(self, hour, minute, second, sun_elevation):
        """Within-hour clock. Each line = 10 min, zone a = first 5, zone b = second 5.

        Zone states:
        - Completed minutes: solid clock color.
        - Active (counting) zone: a LIGHTER SHADE of the solid color. Occupied =
          it BREATHES (fades to dark and back, BREATHE_TRANS each way, device
          loop). Unoccupied = dimmer half shade, steady.
        - Future minutes: dim.
        Line 7: 7a held dark, 7b temp gauge.
        """
        solid_rgb = self.get_color(hour, sun_elevation)
        off_rgb   = self.dim_rgb(solid_rgb, 0.04)
        light_rgb = self.lighten_rgb(solid_rgb, 0.55)   # active = lighter shade
        half_rgb  = self.dim_rgb(solid_rgb, 0.5)        # unoccupied active

        active_arm = minute // 10
        arm_minute = minute % 10

        def static_frame(pid, rgb, occ):
            bright = self.brightness_factor(occ)
            r, g, b = [max(0, min(255, int(c * bright))) for c in rgb]
            return f"{pid} 1 {r} {g} {b} 0 1"

        def active_frame(pid):
            # Currently-counting zone. Occupied = lighter shade breathing to dark
            # (2-frame loop). Unoccupied = dim half shade, steady.
            if not self.occupied:
                return static_frame(pid, half_rgb, False)
            bright = self.brightness_factor(True)
            r, g, b = [max(0, min(255, int(c * bright))) for c in light_rgb]
            return f"{pid} 2 {r} {g} {b} 0 {BREATHE_TRANS} 0 0 0 0 {BREATHE_TRANS}"

        parts = []

        for i, arm in enumerate(ARMS):
            if i < active_arm:
                # Completed — solid
                parts.append(static_frame(arm["inner"], solid_rgb, self.occupied))
                parts.append(static_frame(arm["outer"], solid_rgb, self.occupied))
            elif i == active_arm:
                if arm_minute < 5:
                    # First half active (counting), second half off
                    parts.append(active_frame(arm["inner"]))
                    parts.append(static_frame(arm["outer"], off_rgb, self.occupied))
                else:
                    # First half solid (done), second half active (counting)
                    parts.append(static_frame(arm["inner"], solid_rgb, self.occupied))
                    parts.append(active_frame(arm["outer"]))
            else:
                # Future — dim
                parts.append(static_frame(arm["inner"], off_rgb, self.occupied))
                parts.append(static_frame(arm["outer"], off_rgb, self.occupied))

        # Line 7: 7a dark + 7b temp.
        line7_parts, _ = self.line7_parts(solid_rgb, hour, special=False)
        parts += line7_parts

        anim_data = f"{len(parts)} " + " ".join(parts)
        # The breathing active zone is the only loop in a normal frame.
        self.send_effect_raw(anim_data, loop=self.occupied)

    def render_meeting_countdown(self, now):
        """15-minute pre-meeting countdown over the WHOLE 14-zone bar.

        Timeline (mins-to-meeting = m):
          15 >= m > 14  : all 14 solid orange, held ("-15").
          14 >= m > 1   : one MARCHING zone blinks (native 1s on / 1s off loop).
                          March order MEETING_MARCH = 7b,7a,6b,...,1b,1a.
                          Step index = 14 - ceil(m) ... zones BEHIND the marcher
                          are dark (elapsed), zones AHEAD are solid orange.
          1  >= m > 0   : final 60s — re-light all 14, then EXTINGUISH alternate
                          ends -> center, one segment every ~4.3s.
        Line 7 is part of the canvas here (no temp/heartbeat during a meeting).
        """
        m = self.mins_to_next_meeting(now)
        if m is None:
            return
        orange = self.meeting_orange_rgb()
        secs = m * 60.0

        # ---- Final minute: extinguish sweep (driven by a fast timer) ----
        if secs <= 60:
            self._ensure_sweep_timer()
            self._render_sweep(secs, orange)
            return
        else:
            self._cancel_sweep_timer()

        # ---- -15 hold: all 14 zones solid orange (whole bar is canvas) ----
        if m > 14:
            bright = self.brightness_factor(True)
            r, g, b = [int(c * bright) for c in orange]
            parts = [f"{pid} 1 {r} {g} {b} 0 1" for pid in BAR_LEFT_TO_RIGHT]
            self.send_effect_raw(f"{len(parts)} " + " ".join(parts), loop=False)
            return

        # ---- Marching position, -14 .. -1 (ALL STATIC, no device loops) ----
        # step 0 at m in (13,14], step 1 in (12,13], ... step 13 at m in (0,1].
        # The current zone shows a LIGHTER shade; zones behind are dark, ahead solid.
        step = 14 - int(math.ceil(m))      # 0..13
        step = max(0, min(13, step))

        bright = self.brightness_factor(True)   # meeting ignores occupancy → full
        r, g, b = [int(c * bright) for c in orange]
        lr, lg, lb = [int(c * bright) for c in self.lighten_rgb(orange, 0.55)]

        parts = []
        for idx, pid in enumerate(MEETING_MARCH):
            if idx < step:
                parts.append(f"{pid} 1 0 0 0 0 1")                 # behind → dark
            elif idx == step:
                parts.append(f"{pid} 1 {lr} {lg} {lb} 0 1")        # current → lighter
            else:
                parts.append(f"{pid} 1 {r} {g} {b} 0 1")           # ahead → solid
        anim_data = f"{len(parts)} " + " ".join(parts)
        self.send_effect_raw(anim_data, loop=False)   # STATIC — reliable

    # ─── Final-minute extinguish sweep ───────────────────────────────────────

    def _ensure_sweep_timer(self):
        """Arm a ~4s fast tick for the final-minute sweep (idempotent)."""
        if getattr(self, "sweep_timer", None) is None:
            self.sweep_timer = self.run_every(self.update_clock, "now+4", 4)

    def _cancel_sweep_timer(self):
        if getattr(self, "sweep_timer", None) is not None:
            self.cancel_timer(self.sweep_timer)
            self.sweep_timer = None

    def _render_sweep(self, secs_remaining, orange):
        """secs_remaining in [0,60]. Re-light all 14, then extinguish alternate
        ends -> center. 14 segments / 60s => ~4.29s each. All STATIC writes."""
        bright = self.brightness_factor(True)
        r, g, b = [int(c * bright) for c in orange]
        n = len(EXTINGUISH_ORDER)                 # 14
        elapsed = 60.0 - secs_remaining           # 0..60
        gone = int(elapsed // (60.0 / n))         # how many extinguished so far
        gone = max(0, min(n, gone))
        dead = set(EXTINGUISH_ORDER[:gone])

        parts = []
        for pid in BAR_LEFT_TO_RIGHT:
            if pid in dead:
                parts.append(f"{pid} 1 0 0 0 0 1")
            else:
                parts.append(f"{pid} 1 {r} {g} {b} 0 1")
        anim_data = f"{len(parts)} " + " ".join(parts)
        self.send_effect_raw(anim_data, loop=False)   # STATIC — no loop anywhere

    def render_post_meeting(self, now):
        """Solid pink, cumulative clockwise from top, 1 arm per minute past meeting start."""
        mins_past = self.mins_since_last_meeting_start(now)
        if mins_past is None:
            return

        pink = (255, 80, 160)
        off  = (0, 0, 0)
        panels = {}
        for i, arm in enumerate(ARMS):
            if i < mins_past:
                panels[arm["inner"]] = pink
                panels[arm["outer"]] = pink
            else:
                panels[arm["inner"]] = off
                panels[arm["outer"]] = off

        self.send_panels(panels, self.occupied)

    def render_partner_winddown(self, now):
        """1-hour wind-down before Eóin's bedtime. Arms drop clockwise. Lavender → deep indigo."""
        bedtime_dt = self.partner_bedtime(now)
        if bedtime_dt is None:
            return

        wind_start = bedtime_dt - datetime.timedelta(hours=1)
        secs_elapsed = (now - wind_start).total_seconds()
        secs_total   = 3600.0
        progress     = max(0.0, min(1.0, secs_elapsed / secs_total))

        # Color: warm lavender (0%) → deep indigo (100%)
        r = int(180 - 150 * progress)   # 180 → 30
        g = int(100 - 80  * progress)   # 100 → 20
        b = int(220 - 40  * progress)   # 220 → 180
        solid_rgb = (r, g, b)
        off_rgb   = self.dim_rgb(solid_rgb, 0.04)

        # Arms drop clockwise — each arm = 10 minutes of the hour
        active_arm = int(secs_elapsed // 600)  # 0–5
        arm_minute = int((secs_elapsed % 600) // 60)  # 0–9 within arm

        panels = {}
        for i, arm in enumerate(ARMS):
            if i < active_arm:
                # Dropped
                panels[arm["inner"]] = off_rgb
                panels[arm["outer"]] = off_rgb
            elif i == active_arm:
                # Draining
                if arm_minute < 5:
                    panels[arm["inner"]] = solid_rgb
                    panels[arm["outer"]] = solid_rgb
                else:
                    panels[arm["inner"]] = solid_rgb
                    panels[arm["outer"]] = off_rgb
            else:
                panels[arm["inner"]] = solid_rgb
                panels[arm["outer"]] = solid_rgb

        self.send_panels(panels, self.occupied)

    # ─── Color logic ───────────────────────────────────────────────────────

    def get_color(self, hour, sun_elevation):
        """
        Warm/cool arc: hottest (amber/red) at 2 AM, coolest (blue-white) at solar noon.
        Uses a sine curve peaking warm at 2 AM (hour=2).
        """
        # Night mode: 10 PM – 4 AM → warm reds/ambers
        if hour >= 22 or hour < 4:
            # 2 AM = hottest. Normalize within night window.
            # Map 22→0→4 onto 0→1→0 warmth curve peaking at 2
            if hour >= 22:
                h = hour - 22  # 0–1 (10 PM, 11 PM)
            else:
                h = hour + 2   # 0=midnight→2, 2=2AM→4, 4=4AM→6
            warmth = math.sin(math.pi * h / 6)  # peaks at h=3 (2 AM)
            warmth = max(0.0, min(1.0, warmth))

            r = int(200 + 55 * warmth)    # 200–255
            g = int(40  + 60 * warmth)    # 40–100
            b = int(0)
            return (r, g, b)

        # Day/evening: use sun elevation
        # elevation < 0 = below horizon, max ~60° at noon in Seattle
        elev = float(sun_elevation) if sun_elevation else 0.0
        elev_norm = max(0.0, min(1.0, elev / 60.0))  # 0 at horizon, 1 at noon

        if elev_norm > 0:
            # Above horizon: warm amber at sunrise/sunset → cool blue-white at noon
            r = int(255 - 100 * elev_norm)   # 255→155
            g = int(180 + 60  * elev_norm)   # 180→240
            b = int(80  + 175 * elev_norm)   # 80→255
        else:
            # Below horizon, before 10 PM: cool violet/blue dusk
            if hour >= 17:
                # Evening dusk: violet
                r, g, b = 100, 80, 200
            else:
                # Pre-dawn: deep blue
                r, g, b = 40, 40, 160

        return (r, g, b)

    def meeting_orange_rgb(self):
        # Deep orange-red: near-zero green so it can't be confused with the
        # soft daytime amber→blue-white clock palette.
        return (255, 60, 0)

    def dim_rgb(self, rgb, factor):
        return tuple(int(c * factor) for c in rgb)

    def lighten_rgb(self, rgb, amount):
        """Blend toward white by `amount` (0=same, 1=white). Lighter shade."""
        return tuple(int(c + (255 - c) * amount) for c in rgb)

    def contrast_rgb(self, rgb):
        """Opposite/contrast color (simple RGB inversion)."""
        return tuple(255 - c for c in rgb)

    def reserved_off_parts(self):
        """Frame strings that force BOTH Line-7 zones OFF."""
        return [f"{pid} 1 0 0 0 0 1" for pid in RESERVED_BAR]

    def _read_temp(self):
        """Outdoor temp as float, or -999 sentinel if unavailable."""
        try:
            return float(self.get_state(TEMP_SENSOR))
        except (TypeError, ValueError):
            return -999.0

    def temp_color(self, temp_f):
        """Map an outdoor temp (°F) to a stepped color. Clamps out of range."""
        if temp_f <= 10:
            return TEMP_MIN_COLOR
        if temp_f > 110:
            return TEMP_MAX_COLOR
        for upper, color in TEMP_BANDS:
            if temp_f <= upper:
                return color
        return TEMP_MAX_COLOR

    def temp_7b_part(self):
        """Frame string for 7b = outdoor temp gauge. OFF if sensor unavailable."""
        temp_f = self._read_temp()
        if temp_f <= -900:
            return f"{TEMP_PANEL} 1 0 0 0 0 1"
        rgb = self.temp_color(temp_f)
        bright = self.brightness_factor(self.occupied)
        r, g, b = [max(0, min(255, int(c * bright))) for c in rgb]
        return f"{TEMP_PANEL} 1 {r} {g} {b} 0 1"

    def heartbeat_7a_part(self, clock_rgb, hour, special=False):
        """Frame string for 7a. Held DARK -- reserved for a future notifier.
        (The night heartbeat that lived here moved to the active zone as the
        breathing effect in render_normal.) Returns (frame_string, loops_bool)."""
        return f"{HEARTBEAT_PANEL} 1 0 0 0 0 1", False

    def line7_parts(self, clock_rgb, hour, special=False):
        """Build both Line-7 zones: 7a heartbeat + 7b temp.
        Returns (list_of_frame_strings, any_loop_bool)."""
        hb_frame, hb_loops = self.heartbeat_7a_part(clock_rgb, hour, special)
        return [hb_frame, self.temp_7b_part()], hb_loops

    def brightness_factor(self, occupied):
        """Master brightness multiplier, by sun state + occupancy.
            Day  (sun up):   occupied 50%, unoccupied 25%
            Night(sun down): occupied  6%, unoccupied  2%
        """
        sun_up = self.get_state(SUN_ENTITY) == "above_horizon"
        if sun_up:
            return DAY_OCC if occupied else DAY_EMPTY
        return NIGHT_OCC if occupied else NIGHT_EMPTY

    # ─── Send to Nanoleaf ──────────────────────────────────────────────────

    def send_effect_raw(self, anim_data, loop=False):
        """Send pre-built animData string directly — bypasses occupancy multiplier."""
        payload = {
            "write": {
                "command":  "display",
                # Multi-frame panels are REJECTED (HTTP 400) under "static"; they
                # need "custom". loop is True exactly when a frame has one.
                "animType": "custom" if loop else "static",
                "animData": anim_data,
                "loop":     loop,
                "palette":  [],
            }
        }
        try:
            r = requests.put(NANOLEAF_URL, json=payload, timeout=5)
            if r.status_code != 204:
                self.log(f"Nanoleaf error: {r.status_code} {r.text}", level="WARNING")
        except Exception as e:
            self.log(f"Nanoleaf request failed: {e}", level="ERROR")

    def send_all(self, rgb, occupied):
        panels = {}
        for arm in ARMS:
            panels[arm["inner"]] = rgb
            panels[arm["outer"]] = rgb
        self.send_panels(panels, occupied)

    def send_all_off(self):
        # Clear EVERY unit on the bar (clock + new bar) so pride/rainbow can't linger.
        panels = {pid: (0, 0, 0) for pid in BAR_LEFT_TO_RIGHT}
        self.send_panels(panels, occupied=True)  # bypass brightness multiplier

    def send_panels(self, panels, occupied, special=True):
        bright = self.brightness_factor(occupied)
        parts  = []
        for pid, rgb in panels.items():
            r, g, b = [int(c * bright) for c in rgb]
            r = max(0, min(255, r))
            g = max(0, min(255, g))
            b = max(0, min(255, b))
            parts.append(f"{pid} 1 {r} {g} {b} 0 1")

        # Line 7 = heartbeat (7a) + temp (7b), unless caller explicitly set those
        # panels (e.g. send_all_off passes them as black).
        loop = False
        if not any(pid in panels for pid in RESERVED_BAR):
            hour = datetime.datetime.now().hour
            line7_parts, loop = self.line7_parts((255, 255, 255), hour, special=special)
            parts += line7_parts

        anim_data = f"{len(parts)} " + " ".join(parts)
        payload = {
            "write": {
                "command":  "display",
                # Multi-frame panels are REJECTED (HTTP 400) under "static"; they
                # need "custom". loop is True exactly when a frame has one.
                "animType": "custom" if loop else "static",
                "animData": anim_data,
                "loop":     loop,
                "palette":  [],
            }
        }
        try:
            r = requests.put(NANOLEAF_URL, json=payload, timeout=5)
            if r.status_code != 204:
                self.log(f"Nanoleaf error: {r.status_code} {r.text}", level="WARNING")
        except Exception as e:
            self.log(f"Nanoleaf request failed: {e}", level="ERROR")

    # ─── Calendar helpers ──────────────────────────────────────────────────

    def mins_to_next_meeting(self, now):
        """Returns minutes to next qualifying meeting, or None."""
        try:
            start_s = self.get_state(MY_CALENDAR, attribute="start_time")
            title   = self.get_state(MY_CALENDAR, attribute="message") or ""
            if not start_s:
                return None

            # Filter excluded keywords
            title_lower = title.lower()
            for kw in MEETING_EXCLUDE:
                if kw in title_lower:
                    return None

            start_dt = datetime.datetime.fromisoformat(start_s)
            if start_dt.tzinfo:
                start_dt = start_dt.astimezone().replace(tzinfo=None)

            delta_mins = (start_dt - now).total_seconds() / 60
            if 0 <= delta_mins <= 15:
                return delta_mins

        except Exception as e:
            self.log(f"Calendar error: {e}", level="WARNING")
        return None

    def mins_since_last_meeting_start(self, now):
        """Returns minutes since the last meeting started (for post-meeting pink), or None."""
        try:
            start_s = self.get_state(MY_CALENDAR, attribute="start_time")
            title   = self.get_state(MY_CALENDAR, attribute="message") or ""
            if not start_s:
                return None

            title_lower = title.lower()
            for kw in MEETING_EXCLUDE:
                if kw in title_lower:
                    return None

            start_dt = datetime.datetime.fromisoformat(start_s)
            if start_dt.tzinfo:
                start_dt = start_dt.astimezone().replace(tzinfo=None)

            delta_mins = (now - start_dt).total_seconds() / 60
            if 0 <= delta_mins < 6:
                return int(delta_mins)

        except Exception as e:
            self.log(f"Calendar error: {e}", level="WARNING")
        return None

    def partner_bedtime(self, now):
        """Returns Eóin's bedtime datetime (shift_start - 10h), or None."""
        try:
            start_s = self.get_state(PARTNER_CALENDAR, attribute="start_time")
            title   = self.get_state(PARTNER_CALENDAR, attribute="message") or ""
            if not start_s:
                return None

            start_dt = datetime.datetime.fromisoformat(start_s)
            if start_dt.tzinfo:
                start_dt = start_dt.astimezone().replace(tzinfo=None)

            # Try to parse HHMM / HHMM from title
            match = re.findall(r'(\d{4})\s*/\s*(\d{4})', title)
            if match:
                sh = int(match[0][0][:2])
                sm = int(match[0][0][2:])
                base_date = start_dt.date()
                start_dt  = datetime.datetime(base_date.year, base_date.month,
                                              base_date.day, sh, sm)

            bedtime = start_dt - datetime.timedelta(hours=10)
            return bedtime

        except Exception as e:
            self.log(f"Partner calendar error: {e}", level="WARNING")
        return None

    def partner_wind_down_active(self, now):
        """Returns True if we're in the 1-hour wind-down before Eóin's bedtime."""
        bedtime = self.partner_bedtime(now)
        if bedtime is None:
            return False

        # Only active if bedtime falls between 18:00 and 00:00
        if not (18 <= bedtime.hour < 24):
            return False

        wind_start = bedtime - datetime.timedelta(hours=1)
        return wind_start <= now < bedtime
