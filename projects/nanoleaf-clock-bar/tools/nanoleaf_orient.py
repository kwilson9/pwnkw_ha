#!/usr/bin/env python3
"""
Nanoleaf Panel Orientation Helper  (standalone — does NOT touch nanoleaf_clock.py)
==================================================================================
Run this on a machine ON your LAN (Mac terminal or HA Terminal add-on).

It will:
  1. DISCOVER every panel the device currently reports — including any new bar
     you just added — and print their IDs + (x, y) coordinates.
  2. WALK each panel one at a time, lighting it at 50% dim in a bright color
     while every other panel goes dark, so you can physically pinpoint which
     ID sits where in the new single-line layout.

As each panel lights, note its physical position (e.g. "1st from left").
At the end you'll have the left->right order to give me for the remap.

Usage:
    python3 nanoleaf_orient.py              # discover + walk all panels
    python3 nanoleaf_orient.py --discover   # just print the layout, no walk
    python3 nanoleaf_orient.py --hold 4     # 4 seconds per panel (default 3)
"""

import sys
import time
import json
import urllib.request
import urllib.error

NANOLEAF_IP    = "YOUR_NANOLEAF_IP"
NANOLEAF_TOKEN = "YOUR_NANOLEAF_TOKEN"
BASE           = f"http://{NANOLEAF_IP}:16021/api/v1/{NANOLEAF_TOKEN}"

# 50% dim, distinct highlight color (cyan reads cleanly on Lines)
HI = (0, 128, 128)     # green+blue at ~50%
OFF = (0, 0, 0)


def _put(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        print(f"  ! request failed: {e}")
        return None


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"! GET {path} failed: {e}")
        return None


def discover():
    layout = _get("/panelLayout/layout")
    if not layout:
        print("Could not read layout. Check IP/token and that you're on the LAN.")
        sys.exit(1)
    panels = layout.get("positionData", [])
    print(f"\nDevice reports {layout.get('numPanels', len(panels))} addressable units.\n")
    print(f"{'panelId':>10} {'x':>6} {'y':>6} {'orient':>7}")
    print("-" * 34)
    # Sort by x so the print roughly matches left->right physical order
    for p in sorted(panels, key=lambda d: (d.get('x', 0), d.get('y', 0))):
        print(f"{p['panelId']:>10} {p.get('x',0):>6} {p.get('y',0):>6} {p.get('o',0):>7}")
    print()
    return [p["panelId"] for p in sorted(panels, key=lambda d: (d.get('x',0), d.get('y',0)))]


def light_one(panel_id, all_ids):
    """Light a single panel at HI, all others OFF."""
    parts = []
    for pid in all_ids:
        r, g, b = HI if pid == panel_id else OFF
        parts.append(f"{pid} 1 {r} {g} {b} 0 1")
    anim = f"{len(parts)} " + " ".join(parts)
    payload = {"write": {"command": "display", "animType": "static",
                         "animData": anim, "loop": False, "palette": []}}
    return _put("/effects", payload)


def walk(all_ids, hold):
    print("Walking panels left->right (by x coordinate). Note each physical position.\n")
    for i, pid in enumerate(all_ids, 1):
        status = light_one(pid, all_ids)
        print(f"  [{i:>2}/{len(all_ids)}]  panel {pid}  lit (cyan, 50%)   http={status}")
        time.sleep(hold)
    # all off at the end
    light_one(None, all_ids)
    print("\nDone. All panels off. Tell me the left->right order you observed.")


# 7-stop ROYGBIV ramp (full brightness so it's unmistakable)
ROYGBIV = [
    (255,   0,   0),  # red
    (255, 110,   0),  # orange
    (255, 230,   0),  # yellow
    (  0, 200,   0),  # green
    (  0,  90, 255),  # blue
    ( 75,   0, 180),  # indigo
    (160,   0, 220),  # violet
]


def _lerp(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def rainbow_color(frac):
    """frac in [0,1] -> color along the ROYGBIV ramp."""
    if frac <= 0:
        return ROYGBIV[0]
    if frac >= 1:
        return ROYGBIV[-1]
    span = frac * (len(ROYGBIV) - 1)
    i = int(span)
    return _lerp(ROYGBIV[i], ROYGBIV[i + 1], span - i)


def rainbow(all_ids):
    """Paint the bar ROYGBIV left->right. all_ids must be in LEFT->RIGHT order.
    Red = left end, violet = right end."""
    n = len(all_ids)
    parts = []
    for i, pid in enumerate(all_ids):
        frac = i / (n - 1) if n > 1 else 0
        r, g, b = rainbow_color(frac)
        parts.append(f"{pid} 1 {r} {g} {b} 0 1")
    anim = f"{len(parts)} " + " ".join(parts)
    payload = {"write": {"command": "display", "animType": "static",
                         "animData": anim, "loop": False, "palette": []}}
    status = _put("/effects", payload)
    print(f"Painted {n} units ROYGBIV left->right (red=left, violet=right). http={status}")
    print("Check the bar: red should be on your LEFT, violet on your RIGHT.")


# 7 discrete block colors, left -> right. Rightmost = white (the new bar).
BLOCK_COLORS = [
    (255,   0,   0),  # 1 red
    (255, 110,   0),  # 2 orange
    (255, 230,   0),  # 3 yellow
    (  0, 200,   0),  # 4 green
    (  0,  90, 255),  # 5 blue
    (160,   0, 220),  # 6 violet
    (255, 255, 255),  # 7 white  (new bar, rightmost)
]


def pairs_by_geometry(n_blocks=7):
    """The bar is VERTICAL (all x equal, differ by y). Split the y-sorted nodes
    into n_blocks EQUAL contiguous blocks, top->bottom. Returns list of blocks."""
    layout = _get("/panelLayout/layout")
    if not layout:
        print("Could not read layout."); sys.exit(1)
    pts = layout.get("positionData", [])
    pts = sorted(pts, key=lambda d: (d.get("y", 0), d.get("x", 0)))  # top->bottom
    ids = [p["panelId"] for p in pts]
    n = len(ids)
    # divide into n_blocks as evenly as possible
    blocks, start = [], 0
    for i in range(n_blocks):
        size = (n - start) // (n_blocks - i)
        blocks.append(ids[start:start + size])
        start += size
    print(f"\nSplit {n} nodes into {n_blocks} blocks, TOP -> BOTTOM:")
    for i, b in enumerate(blocks):
        print(f"  block {i+1}: {b}")
    return blocks


def color_blocks(pairs):
    """Paint each pair ONE solid color (left unit bright, right unit 50%).
    Colors left->right: red, orange, yellow, green, blue, violet, white."""
    parts = []
    for i, pr in enumerate(pairs):
        color = BLOCK_COLORS[i] if i < len(BLOCK_COLORS) else (0, 0, 0)
        for j, pid in enumerate(pr):
            mult = 1.0 if j == 0 else 0.5   # left bright, right 50%
            r, g, b = (int(c * mult) for c in color)
            parts.append(f"{pid} 1 {r} {g} {b} 0 1")
    anim = f"{len(parts)} " + " ".join(parts)
    payload = {"write": {"command": "display", "animType": "static",
                         "animData": anim, "loop": False, "palette": []}}
    status = _put("/effects", payload)
    print(f"\nPainted {len(pairs)} color blocks left->right "
          f"(R O Y G B V White). Left unit bright, right unit 50%. http={status}")


if __name__ == "__main__":
    hold = 3.0
    discover_only = False
    args = sys.argv[1:]
    if "--discover" in args:
        discover_only = True
    if "--hold" in args:
        try:
            hold = float(args[args.index("--hold") + 1])
        except (IndexError, ValueError):
            pass

    ids = discover()  # x-sorted order (device reports this as physically RIGHT->LEFT)
    if discover_only:
        sys.exit(0)

    # left->right order = reverse of the x-sorted/walk order
    left_to_right = list(reversed(ids))

    if "--rainbow" in args:
        rainbow(left_to_right)
        sys.exit(0)

    if "--blocks" in args:
        blocks = pairs_by_geometry(7)        # 7 blocks, TOP -> BOTTOM
        color_blocks(blocks)
        sys.exit(0)

    input(f"Press Enter to walk all {len(ids)} panels at {hold}s each "
          f"(Ctrl-C to abort)... ")
    try:
        walk(ids, hold)
    except KeyboardInterrupt:
        light_one(None, ids)
        print("\nAborted, panels off.")
