#!/usr/bin/env python3
"""
Nanoleaf GUIDED walk — pin down the real 7 Lines (14 lights) among 23 nodes.
===========================================================================
The device exposes 23 addressable nodes (some are connector joints). You have
7 physical Lines. This lights ONE node at a time, top -> bottom, and after each
you say which physical Line lit and which end.

For each node you'll be asked:
    line number (1-7), or 'x' if NOTHING visibly lit (it's a joint/dead node),
    then end: t = top/upper, b = bottom/lower  (skip if you typed x)

At the end it prints, per Line, the top & bottom panel IDs — ready to paste.

Run on your LAN:
    python3 nanoleaf_guided_walk.py
    python3 nanoleaf_guided_walk.py --hold 2     # seconds lit before prompt
"""

import sys
import time
import json
import urllib.request
import urllib.error

NANOLEAF_IP    = "YOUR_NANOLEAF_IP"
NANOLEAF_TOKEN = "YOUR_NANOLEAF_TOKEN"
BASE           = f"http://{NANOLEAF_IP}:16021/api/v1/{NANOLEAF_TOKEN}"

HI  = (0, 200, 200)   # bright cyan
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
        print(f"  ! request failed: {e}"); return None


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"! GET {path} failed: {e}"); return None


def light_one(panel_id, all_ids):
    parts = []
    for pid in all_ids:
        r, g, b = HI if pid == panel_id else OFF
        parts.append(f"{pid} 1 {r} {g} {b} 0 1")
    anim = f"{len(parts)} " + " ".join(parts)
    return _put("/effects", {"write": {"command": "display", "animType": "static",
                                       "animData": anim, "loop": False, "palette": []}})


def main():
    hold = 1.5
    args = sys.argv[1:]
    if "--hold" in args:
        try: hold = float(args[args.index("--hold") + 1])
        except (IndexError, ValueError): pass

    layout = _get("/panelLayout/layout")
    if not layout:
        print("Could not read layout. Check LAN/IP/token."); sys.exit(1)
    pts = layout.get("positionData", [])
    # top -> bottom by y (smaller y = top)
    pts = sorted(pts, key=lambda d: (d.get("y", 0), d.get("x", 0)))
    ids = [p["panelId"] for p in pts]

    print(f"\n{len(ids)} nodes. Lighting one at a time, LEFT -> RIGHT.")
    print("Type in one shot: line number 1-7 + a/b, e.g.  7b  (line 7, right unit).")
    print("a = left unit of the pair, b = right unit. Type x if nothing lights.\n")

    # line_no -> {'t': id, 'b': id}
    lines = {}
    for idx, pid in enumerate(ids, 1):
        light_one(pid, ids)
        time.sleep(hold)
        while True:
            ans = input(f"  [{idx:2}/{len(ids)}] node {pid}: e.g. 7b  or  x : ").strip().lower()
            if ans == "x":
                print("      -> joint/dead, skipped")
                break
            # accept "7b" / "1a" in one shot
            if len(ans) == 2 and ans[0] in "1234567" and ans[1] in "ab":
                ln = int(ans[0]); end = ans[1]
                lines.setdefault(ln, {})[end] = pid
                print(f"      -> line {ln}{end} = {pid}")
                break
            print("      (type like 7b or 1a, or x)")

    light_one(None, ids)
    print("\n================  RESULT  ================")
    print("Paste this back to me:\n")
    for ln in sorted(lines):
        d = lines[ln]
        print(f"  Line {ln}: a(left)={d.get('a','?')}  b(right)={d.get('b','?')}")
    print("\nAll nodes off.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
