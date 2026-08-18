#!/usr/bin/env python3
"""
Nanoleaf Pre-Meeting Countdown — TEST PLAYER (standalone, all-static)
====================================================================
60-second compression of the meeting countdown. NO blinks, NO loops —
every frame is a plain static write, spaced out so the device doesn't drop
writes. Prints the HTTP status of every push so we can see if the device is
actually accepting them.

Run on your LAN:
    python3 nanoleaf_countdown_test.py
    python3 nanoleaf_countdown_test.py --gap 0.8   # seconds between frames
    python3 nanoleaf_countdown_test.py --pink      # end with pink fill

Sequence (compressed into ~60s):
  1. All 14 zones solid orange (hold).
  2. Drain RIGHT->LEFT: drop one zone at a time (7b,7a,6b,...,1a) until empty.
  3. Re-light all 14, then EXTINGUISH alternate-ends-to-center.
"""

import sys
import time
import json
import urllib.request
import urllib.error

NANOLEAF_IP    = "YOUR_NANOLEAF_IP"
NANOLEAF_TOKEN = "YOUR_NANOLEAF_TOKEN"
BASE           = f"http://{NANOLEAF_IP}:16021/api/v1/{NANOLEAF_TOKEN}"

ORANGE = (255, 60, 0)
PINK   = (255, 80, 160)
OFF    = (0, 0, 0)

LINES = [
    [13966, 51150],  # Line 1
    [53491, 16434],  # Line 2
    [23652, 52389],  # Line 3
    [48801, 11872],  # Line 4
    [48260, 11333],  # Line 5
    [50579, 13523],  # Line 6
    [23507, 51986],  # Line 7
]
ZONES_LR = [pid for ln in LINES for pid in ln]  # 1a,1b,...,7a,7b

def zid(line, ab):
    return LINES[line - 1][0 if ab == "a" else 1]

# Drain order: right -> left, b then a within each line
DRAIN_ORDER = [
    zid(7,"b"), zid(7,"a"), zid(6,"b"), zid(6,"a"), zid(5,"b"), zid(5,"a"),
    zid(4,"b"), zid(4,"a"), zid(3,"b"), zid(3,"a"), zid(2,"b"), zid(2,"a"),
    zid(1,"b"), zid(1,"a"),
]
# Extinguish: alternate ends inward to center
EXTINGUISH_ORDER = [
    zid(1,"a"), zid(7,"b"), zid(1,"b"), zid(7,"a"), zid(2,"a"), zid(6,"b"),
    zid(2,"b"), zid(6,"a"), zid(3,"a"), zid(5,"b"), zid(3,"b"), zid(5,"a"),
    zid(4,"a"), zid(4,"b"),
]


def push(colors, label=""):
    """Static write only. colors: dict pid->(r,g,b); missing => off."""
    parts = []
    for pid in ZONES_LR:
        r, g, b = colors.get(pid, OFF)
        parts.append(f"{pid} 1 {r} {g} {b} 0 1")
    anim = f"{len(parts)} " + " ".join(parts)
    payload = {"write": {"command": "display", "animType": "static",
                         "animData": anim, "loop": False, "palette": []}}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + "/effects", data=data, method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        status = f"ERR {e}"
    print(f"  push {label:24} http={status}")
    return status


def all_color(c):
    return {pid: c for pid in ZONES_LR}


def main():
    gap = 0.7
    end_pink = "--pink" in sys.argv
    if "--gap" in sys.argv:
        try: gap = float(sys.argv[sys.argv.index("--gap") + 1])
        except (IndexError, ValueError): pass

    print(f"60s countdown test (all static, gap={gap}s). Ctrl-C to stop.\n")

    # 1. Solid hold
    print("HOLD: all 14 solid orange")
    push(all_color(ORANGE), "all-solid")
    time.sleep(3)

    # 2. Drain right->left
    print("\nDRAIN right->left:")
    lit = set(ZONES_LR)
    for pid in DRAIN_ORDER:
        lit.discard(pid)
        push({p: ORANGE for p in lit}, f"drop {pid}")
        time.sleep(gap)

    # 3. Re-light, then extinguish alternate ends -> center
    print("\nRE-LIGHT all 14:")
    push(all_color(ORANGE), "relight")
    time.sleep(1.5)
    print("\nEXTINGUISH alternate ends -> center:")
    remaining = set(ZONES_LR)
    for pid in EXTINGUISH_ORDER:
        remaining.discard(pid)
        push({p: ORANGE for p in remaining}, f"out {pid}")
        time.sleep(gap)

    # End
    if end_pink:
        print("\nPINK post-meeting fill:")
        for n in range(1, 7):
            colors = {}
            for i, ln in enumerate(LINES[:6]):
                c = PINK if i < n else OFF
                colors[ln[0]] = c; colors[ln[1]] = c
            push(colors, f"pink {n}/6")
            time.sleep(0.8)
        time.sleep(1)
    push(all_color(OFF), "off")
    print("\nDone. Bar off.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        push(all_color(OFF), "abort-off")
        print("\nAborted, bar off.")
