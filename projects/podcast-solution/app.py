"""Podcast Solution: turn podcast RSS feeds into Plex albums that Sonos can play.

Two kinds of feed:

  Latest only  (news, hourly, daily shows). On each scheduled check it picks the
               episodes the rules ask for and, if they changed, swaps them in under
               fixed names (<slug>-1.mp3, <slug>-2.mp3 ...). Back episodes are never kept.

  Episodic     (a show you are working through). New episodes are added to the album,
               oldest unplayed first. Plex reports what you played (about 90 % through
               counts); played episodes move to "<name> - Played", newest-played first,
               and are deleted after N days. Mark played / unplayed on the page.

Files you delete yourself (in Plex or on disk) stay deleted until you press Restore.
Analyze reads a feed and suggests settings. Artwork is embedded and saved as cover.*.

Standard library plus mutagen. Config and state are JSON files in /data.
"""

import hashlib
import html
import json
import logging
import os
import re
import shutil
import signal
import statistics
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, deque
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import mutagen
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

DATA = os.environ.get("DATA_DIR", "/data")
MEDIA = os.environ.get("MEDIA_DIR", "/media")
MEDIA_HOST_PATH = os.environ.get("MEDIA_HOST_PATH", MEDIA)  # same folder as the NAS names it
PORT = int(os.environ.get("PORT", "8095"))
PLEX_URL = os.environ.get("PLEX_URL", "")  # e.g. http://192.168.1.10:32400; can also be set on Config
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
VERSION = "1.0.0"
UA = "Mozilla/5.0 (podcast-solution; personal use)"
PLAYED_POLL_MIN = 15

FEEDS_FILE = os.path.join(DATA, "feeds.json")
STATE_FILE = os.path.join(DATA, "state.json")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
ART_DIR = os.path.join(DATA, "art")

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
AUDIO_EXT = ("mp3", "m4a", "aac", "ogg", "opus")

DEFAULTS = {
    "kind": "latest", "take": "per_show", "count": 1, "order": "newest",
    "skip_types": [], "skip_titles": "", "art_url": "", "art_source": "show", "stamp": "none", "tidy": True, "sched": "manual", "tags": [],
    "start_n": 1, "max_unplayed": 20, "played_days": 30,
    "days": [0, 1, 2, 3, 4], "start": "04:00", "end": "09:00", "every": 30, "enabled": True,
}

urllib.request.HTTPRedirectHandler.max_redirections = 25  # podcast tracking chains are long

LOG = deque(maxlen=120)


class _MemLog(logging.Handler):
    def emit(self, record):
        LOG.appendleft(self.format(record))


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%m-%d %H:%M:%S")
_h = _MemLog()
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_h)
log = logging.getLogger("podcast")

_file_lock = threading.RLock()
_feed_locks = {}


def feed_lock(feed_id):
    with _file_lock:
        return _feed_locks.setdefault(feed_id, threading.Lock())


# ---------------------------------------------------------------- storage ---

def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:  # corrupt file: keep it for inspection, start clean
        log.error("could not read %s (%s); moved aside", path, e)
        shutil.move(path, path + ".bad")
        return default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def norm_feed(f):
    d = dict(DEFAULTS)
    d.update({k: v for k, v in f.items() if k != "mode"})
    if "take" not in f and f.get("mode"):  # settings written by version 1
        d["take"] = f["mode"]
    return d


def feeds():
    with _file_lock:
        return [norm_feed(f) for f in _load(FEEDS_FILE, [])]


def save_feeds(items):
    with _file_lock:
        _save(FEEDS_FILE, items)


def state():
    with _file_lock:
        return _load(STATE_FILE, {})


def feed_state(feed_id):
    return state().get(feed_id, {})


def update_state(feed_id, **kw):
    with _file_lock:
        s = _load(STATE_FILE, {})
        s.setdefault(feed_id, {}).update(kw)
        _save(STATE_FILE, s)


def replace_state(feed_id, new):
    with _file_lock:
        s = _load(STATE_FILE, {})
        s[feed_id] = new
        _save(STATE_FILE, s)


def settings():
    with _file_lock:
        return _load(SETTINGS_FILE, {"plex_url": "", "plex_token": ""})


def save_settings(obj):
    with _file_lock:
        _save(SETTINGS_FILE, obj)


# ------------------------------------------------------------------ feeds ---

def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "feed"


def folder_name(name):
    return re.sub(r"[^\w .,'()&-]+", "", name).strip() or "Feed"


def played_folder(folder):
    return f"{folder} - Played"


def http_get(url, timeout=60, extra=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(extra or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def _secs(d):
    d = (d or "").strip()
    if not d:
        return 0
    try:
        parts = [float(x) for x in d.split(":")]
    except ValueError:
        return 0
    n = 0
    for p in parts:
        n = n * 60 + p
    return int(n)


def parse_feed(xml_bytes):
    """Channel info and episodes. The show name is itunes:author or the channel title:
    never dc:creator, which many sites fill with the reporter's name."""
    root = ET.fromstring(xml_bytes)
    ch = root.find("channel")
    if ch is None:
        raise RuntimeError("not an RSS feed (no <channel>)")
    title = html.unescape((ch.findtext("title") or "Podcast").strip())
    img = ch.find(ITUNES + "image")
    image = img.get("href") if img is not None else ""
    if not image and ch.find("image") is not None:
        image = (ch.find("image").findtext("url") or "").strip()
    channel = {"title": title, "image": image, "type": (ch.findtext(ITUNES + "type") or "").strip().lower(),
               "author": html.unescape((ch.findtext(ITUNES + "author") or "").strip())}
    out = []
    for pos, it in enumerate(root.iter("item")):
        enc = it.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        # The show is the feed's own title. Per-episode author fields hold hosts or reporters
        # (a host's name in itunes:author, a reporter's in dc:creator), not shows. Only a real
        # multi-show playlist names a different show per episode (Listen Notes: "Podcast: <a>Show</a>").
        m = re.search(r"Podcast</strong>:\s*<a[^>]*>([^<]+)</a>", it.findtext("description") or "")
        show = m.group(1).strip() if m else title
        own = show == title  # a single-show feed: no need to repeat the show in each title
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:
            pub = None
        ii = it.find(ITUNES + "image")
        out.append({
            "pos": pos,
            "title": html.unescape((it.findtext("title") or "Episode").strip()),
            "show": html.unescape(show),
            "own": own,
            "guid": (it.findtext("guid") or enc.get("url")).strip(),
            "url": enc.get("url"),
            "type": enc.get("type") or "",
            "pub": pub.isoformat() if pub else "",
            "etype": (it.findtext(ITUNES + "episodeType") or "full").strip().lower(),
            "dur": _secs(it.findtext(ITUNES + "duration")),
            "image": ii.get("href") if ii is not None else "",
        })
    return channel, out


SEEN_DAYS = 45


def read_feed(url, feed_id=None):
    """Read and parse a feed. With feed_id, also remember every episode seen, so feeds that
    keep only their newest episode still build up a posting history to learn from."""
    with http_get(url) as r:
        channel, items = parse_feed(r.read())
    if feed_id:
        record_seen(feed_id, items)
    return channel, items


def record_seen(feed_id, items):
    with _file_lock:
        s = _load(STATE_FILE, {})
        fs = s.setdefault(feed_id, {})
        seen = fs.get("seen") or {}
        now = datetime.now(TZ)
        for e in items:
            if e["pub"] and e["guid"] not in seen:
                seen[e["guid"]] = {"pub": e["pub"], "title": e["title"][:120], "etype": e["etype"],
                                   "first": now.isoformat()}
        cutoff = (now - timedelta(days=SEEN_DAYS)).isoformat()
        seen = {k: v for k, v in seen.items() if v.get("first", "") >= cutoff or v["pub"] >= cutoff}
        if len(seen) > 2000:  # keep the newest
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1]["pub"])[-2000:])
        fs["seen"] = seen
        _save(STATE_FILE, s)


def with_seen(items, fs):
    """The feed's items plus remembered ones it no longer lists (for learning only)."""
    have = {e["guid"] for e in items}
    extra = [{"guid": g, "pub": v["pub"], "title": v.get("title", ""), "etype": v.get("etype", "full"),
              "show": "", "own": True, "url": "", "type": "", "pos": 10 ** 6, "dur": 0, "image": ""}
             for g, v in (fs.get("seen") or {}).items() if g not in have]
    return items + extra


def skip_list(feed):
    return [s.strip().lower() for s in (feed.get("skip_titles") or "").split(",") if s.strip()]


def apply_skips(items, feed):
    words, types = skip_list(feed), set(feed.get("skip_types") or [])
    return [e for e in items
            if e["etype"] not in types and not any(w in e["title"].lower() for w in words)]


def local_date(e):
    return datetime.fromisoformat(e["pub"]).astimezone(TZ).date() if e["pub"] else None


def pick_latest(items, feed):
    if not items:
        return []
    by_new = sorted(items, key=lambda e: e["pub"], reverse=True)
    take = feed.get("take")
    if take == "newest_n":
        chosen = by_new[: max(1, int(feed.get("count") or 1))]
    elif take == "latest_day":
        day = local_date(by_new[0])
        chosen = [e for e in by_new if local_date(e) == day]
    else:  # newest episode of each show
        seen, chosen = set(), []
        for e in by_new:
            if e["show"] not in seen:
                seen.add(e["show"])
                chosen.append(e)
    order = feed.get("order")
    if order == "feed":
        chosen.sort(key=lambda e: e["pos"])
    elif order == "oldest":
        chosen.sort(key=lambda e: e["pub"])
    return chosen


# --------------------------------------------------------------- download ---

def ext_for(ep, final_url, ctype):
    for source in (urllib.parse.urlparse(final_url).path, urllib.parse.urlparse(ep["url"]).path):
        m = re.search(r"\.(mp3|m4a|aac|mp4|ogg|opus)$", source.lower())
        if m:
            return "m4a" if m.group(1) == "mp4" else m.group(1)
    ctype = (ctype or ep.get("type") or "").lower()
    if "mp4" in ctype or "m4a" in ctype or "aac" in ctype:
        return "m4a"
    return "mp3"


def download_part(ep, d, base):
    """Download to a hidden .part file in d. Returns (part_path, final_name)."""
    with http_get(ep["url"], timeout=300) as r:
        ext = ext_for(ep, r.geturl(), r.headers.get("Content-Type"))
        part = os.path.join(d, f".{base}.{ext}.part")
        with open(part, "wb") as out:
            shutil.copyfileobj(r, out, 1024 * 256)
    return part, f"{base}.{ext}"


def clear_parts(*dirs):
    for d in dirs:
        if os.path.isdir(d):
            for x in os.listdir(d):
                if x.endswith(".part"):
                    os.remove(os.path.join(d, x))


# ---------------------------------------------------------------- artwork ---

_art_mem = {}


def fetch_art(url):
    """(bytes, mime) for an image URL, cached on disk. None if it isn't an image."""
    if not url:
        return None
    if url in _art_mem:
        return _art_mem[url]
    os.makedirs(ART_DIR, exist_ok=True)
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")):
        p = os.path.join(ART_DIR, f"{key}.{ext}")
        if os.path.exists(p):
            with open(p, "rb") as f:
                _art_mem[url] = (f.read(), mime)
                return _art_mem[url]
    try:
        with http_get(url, timeout=30) as r:
            data = r.read(6 * 1024 * 1024 + 1)
    except Exception as e:
        log.info("artwork %s not fetched: %s", url[:80], e)
        return None
    if len(data) > 6 * 1024 * 1024:
        log.info("artwork %s is over 6 MB; skipped", url[:80])
        return None
    if data[:3] == b"\xff\xd8\xff":
        ext, mime = "jpg", "image/jpeg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext, mime = "png", "image/png"
    else:
        log.info("artwork %s is not JPEG or PNG; skipped", url[:80])
        return None
    with open(os.path.join(ART_DIR, f"{key}.{ext}"), "wb") as f:
        f.write(data)
    _art_mem[url] = (data, mime)
    return _art_mem[url]


def apple_art(title):
    """Artwork from Apple's podcast directory, matched by show title."""
    try:
        q = urllib.parse.urlencode({"media": "podcast", "entity": "podcast", "limit": 5, "term": title})
        with http_get("https://itunes.apple.com/search?" + q, timeout=20) as r:
            res = json.load(r).get("results", [])
    except Exception:
        return ""
    for x in res:
        if (x.get("collectionName") or "").strip().lower() == title.strip().lower():
            return x.get("artworkUrl600") or ""
    return ""


def art_choice(feed, channel, ep, fs):
    """Where this episode's artwork comes from: (url, source label). ('', '') = keep the file's own."""
    if feed.get("art_url"):
        return feed["art_url"], "your link"
    if feed.get("art_source") == "episode" and ep.get("image"):
        return ep["image"], "episode image"
    if channel.get("image"):
        return channel["image"], "feed image"
    if "apple_art" not in fs:
        fs["apple_art"] = apple_art(channel.get("title", ""))
    if fs.get("apple_art"):
        return fs["apple_art"], "Apple directory"
    return "", ""


def write_cover(d, art):
    if not art:
        return
    data, mime = art
    name = "cover.png" if mime == "image/png" else "cover.jpg"
    for other in ("cover.jpg", "cover.png"):
        if other != name and os.path.exists(os.path.join(d, other)):
            os.remove(os.path.join(d, other))
    p = os.path.join(d, name)
    if os.path.exists(p):
        with open(p, "rb") as f:
            if f.read() == data:
                return
    with open(p + ".tmp", "wb") as f:
        f.write(data)
    os.replace(p + ".tmp", p)


STAMPS = {"none": "None", "time": "Time (11/13 11:01 AM)", "daydate": "Day and date (Fri 11/13)",
          "daydatetime": "Day, date and time (Fri 11/13 11:01 AM)", "date": "Date (2026-11-13)"}


def stamp_text(kind, pub):
    if not pub or kind not in STAMPS or kind == "none":
        return ""
    d = datetime.fromisoformat(pub).astimezone(TZ)
    t = d.strftime("%-I:%M %p")
    return {"time": f"{d.month}/{d.day} {t}", "daydate": f"{d:%a} {d.month}/{d.day}",
            "daydatetime": f"{d:%a} {d.month}/{d.day} {t}", "date": d.strftime("%Y-%m-%d")}[kind]


TITLE_VERSION = 2  # bump when the title rules change, so existing files get re-tagged

_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = rf"\b(?P<mon>{_MON})\s+(?P<day>\d{{1,2}})(?!\d)(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{{4}}))?"
_TIME = r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap])\.?\s?m\.?"
_DT = re.compile(rf"(?:{_DATE})?(?:\s*(?:,|at|@|-)?\s*)?(?:\b{_TIME})?", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"], 1)}
_SEP = " \t:|-–—,·"


def strip_show(title, names):
    """Drop a leading show name followed by a separator ('City Newscast: …', 'The Show | …')."""
    for n in sorted({x.strip() for x in names if x and x.strip()}, key=len, reverse=True):
        if title.lower().startswith(n.lower()):
            rest = title[len(n):].lstrip(" ")
            if rest[:1] and rest[:1] in ":|-–—":
                return rest.lstrip(_SEP) or title
    return title


def title_datetime(title, pub):
    """(datetime the title names, title without that date/time) or (None, title)."""
    for m in _DT.finditer(title):
        if not (m.group("mon") or m.group("h")) or not m.group(0).strip():
            continue
        base = datetime.fromisoformat(pub).astimezone(TZ) if pub else datetime.now(TZ)
        d = base
        try:
            if m.group("mon"):
                d = d.replace(month=_MONTHS[m.group("mon").lower()[:3]], day=int(m.group("day")),
                              year=int(m.group("year") or base.year))
            if m.group("h"):
                h = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "p" else 0)
                d = d.replace(hour=h, minute=int(m.group("m") or 0))
        except ValueError:
            continue
        rest = (title[:m.start()].rstrip(_SEP) + " " + title[m.end():].lstrip(_SEP)).strip(_SEP + " ")
        # Only a title that IS a date/time ("November 13 at 12:01pm") or names a clock time is
        # treated as the air time; a date inside a story's title is part of the story.
        if rest and not m.group("h"):
            continue
        return d, rest
    return None, title


def display_title(feed, ep, album):
    """'<stamp>| <title>'. Tidy (default on): drop a leading show name, and when a stamp is on,
    take the date/time from the title itself and don't repeat it."""
    feed = feed or {}
    kind, pub, title = feed.get("stamp", "none"), ep.get("pub"), ep["title"]
    show, album_base = ep.get("show", ""), album.removesuffix(" - Played")
    single = ep.get("own", True) or show == album_base
    if feed.get("tidy", True):
        title = strip_show(title, [show, album_base, feed.get("name", "")])
        if kind != "none":
            when_, rest = title_datetime(title, pub)
            if when_ is not None:
                pub, title = when_.isoformat(), rest
    base = title if single else f"{show}: {title}"
    s = stamp_text(kind, pub)
    if s and base:
        return f"{s}| {base}"
    return s or base or ep["title"]


def tag(path, album, ep, track, total, art, feed=None):
    f = mutagen.File(path, easy=True)
    if f is None:
        raise RuntimeError("not an audio file mutagen can read")
    if f.tags is None:
        f.add_tags()
    for k in list(f.tags.keys()):
        del f.tags[k]
    f["title"] = display_title(feed, ep, album)
    f["artist"] = ep["show"]
    f["albumartist"] = album.removesuffix(" - Played")
    f["album"] = album
    f["tracknumber"] = f"{track}/{total}"
    f["genre"] = "Podcast"
    if ep.get("pub"):
        f["date"] = ep["pub"][:10]
    f.save()
    kind = type(f).__name__  # EasyMP3 / EasyMP4; the file may still be named .part
    if art:
        data, mime = art
        if kind == "EasyMP3":
            try:
                t = ID3(path)
            except ID3NoHeaderError:
                t = ID3()
            t.delall("APIC")
            t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            t.save(path, v2_version=3)
        elif kind == "EasyMP4":
            m = MP4(path)
            m["covr"] = [MP4Cover(data, MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG)]
            m.save()


def retag_number(path, album, track, total, title=None, artist=None):
    f = mutagen.File(path, easy=True)
    if f is None:
        return
    if f.tags is None:
        f.add_tags()
    if title:
        f["title"] = title
    if artist:
        f["artist"] = artist
    f["album"] = album
    f["albumartist"] = album.removesuffix(" - Played")
    f["tracknumber"] = f"{track}/{total}"
    f.save()


# ------------------------------------------------------------------- plex ---

def _share_path(p):
    """Synology shows one share under several roots; reduce each to /share/<share>/..."""
    p = (p or "").rstrip("/")
    m = re.match(r"^/volume\d+/@appdata/ContainerManager/all_shares/(.+)$", p)
    return "/share/" + m.group(1) if m else re.sub(r"^/volume\d+/", "/share/", p)


def _plex(s, path):
    base = (s.get("plex_url") or PLEX_URL).rstrip("/")
    if not base:
        raise RuntimeError("no Plex address: set it on Config (or PLEX_URL in docker-compose.yml)")
    req = urllib.request.Request(base + path, headers={"X-Plex-Token": s["plex_token"], "User-Agent": UA,
                                                       "Accept": "application/xml"})
    return urllib.request.urlopen(req, timeout=30).read()


def plex_section(s):
    """The library whose folder holds MEDIA_HOST_PATH, and that folder as Plex names it."""
    if s.get("plex_section") and s.get("plex_root"):
        return str(s["plex_section"]), s.get("plex_section_name", "")
    want = _share_path(MEDIA_HOST_PATH)
    root = ET.fromstring(_plex(s, "/library/sections"))
    for d in root.findall("Directory"):
        for loc in d.findall("Location"):
            raw = (loc.get("path") or "").rstrip("/")
            have = _share_path(raw)
            if raw and (want == have or want.startswith(have + "/")):
                s["plex_section"], s["plex_section_name"] = d.get("key"), d.get("title", "")
                s["plex_root"] = raw + want[len(have):]
                save_settings(s)
                return s["plex_section"], s["plex_section_name"]
    raise RuntimeError(f"no Plex library has a folder containing {MEDIA_HOST_PATH}")


def plex_test():
    s = settings()
    if not s.get("plex_token"):
        return "Plex nudge off (no token)"
    try:
        key, title = plex_section(s)
        return f"Plex OK: library {key} \"{title}\" at {settings().get('plex_root')}"
    except Exception as e:
        return f"Plex check failed: {e}"


def plex_nudge(*folders):
    s = settings()
    if not s.get("plex_token"):
        return "Plex nudge off"
    try:
        key, _ = plex_section(s)
        root = settings().get("plex_root") or MEDIA_HOST_PATH
        for folder in folders:
            path = os.path.join(root, folder)
            _plex(s, f"/library/sections/{urllib.parse.quote(key)}/refresh?"
                  + urllib.parse.urlencode({"path": path}, quote_via=urllib.parse.quote))
        return "Plex rescan requested"
    except Exception as e:
        return f"Plex rescan failed: {e}"


def plex_tracks():
    """{share-path of file: {rk, vc, lv, off, dur}} for every track in the library, or None."""
    s = settings()
    if not s.get("plex_token"):
        return None
    key, _ = plex_section(s)
    root = ET.fromstring(_plex(s, f"/library/sections/{urllib.parse.quote(key)}/all?type=10"))
    out = {}
    for t in root.findall("Track"):
        info = {"rk": t.get("ratingKey"), "vc": int(t.get("viewCount") or 0), "lv": int(t.get("lastViewedAt") or 0),
                "off": int(t.get("viewOffset") or 0) // 1000, "dur": int(t.get("duration") or 0) // 1000}
        for part in t.iter("Part"):
            if part.get("file"):
                out[_share_path(part.get("file"))] = info
    return out


def plex_plays():
    """{share-path of file: (viewCount, lastViewedAt)}; kept for the episodic mover."""
    t = plex_tracks()
    return None if t is None else {k: (v["vc"], v["lv"]) for k, v in t.items()}


def plex_reset(rk):
    """Mark a Plex track unplayed and clear its resume point (a Latest only file got new audio)."""
    s = settings()
    if not (s.get("plex_token") and rk):
        return
    q = lambda **kw: urllib.parse.urlencode(dict(identifier="com.plexapp.plugins.library", **kw))
    for path in (f"/:/unscrobble?{q(key=rk)}", f"/:/progress?{q(key=rk, time=0, state='stopped')}"):
        try:
            _plex(s, path)
        except Exception as e:
            log.info("Plex reset of %s failed: %s", rk, e)


HISTORY_FILE = os.path.join(DATA, "plays.json")


def history():
    with _file_lock:
        return _load(HISTORY_FILE, [])


def add_history(events):
    if not events:
        return
    with _file_lock:
        h = _load(HISTORY_FILE, [])
        seen = {(x["feed"], x["title"], x["what"], x["at"][:16]) for x in h}
        fresh = [e for e in events if (e["feed"], e["title"], e["what"], e["at"][:16]) not in seen]
        h = (sorted(fresh, key=lambda e: e["at"], reverse=True) + h)[:300]
        _save(HISTORY_FILE, h)


def managed_files(feed, fs):
    """[(folder, file, title, stamp-free label)] this feed currently has on disk."""
    out = []
    if feed["kind"] == "episodic":
        for e in (fs.get("eps") or {}).values():
            if e["status"] in ("unplayed", "played") and e.get("file"):
                fold = played_folder(feed["folder"]) if e["status"] == "played" else feed["folder"]
                out.append((fold, e["file"], e.get("title", "")))
    else:
        eps = fs.get("episodes") or []
        for i, x in enumerate(fs.get("files") or []):
            out.append((feed["folder"], x, eps[i]["title"] if i < len(eps) else x))
    return out


_last_plex_sync = {"at": "", "result": ""}
_sync_lock = threading.Lock()


def sync_plex():
    """Read play state from Plex for every feed: record it, log new listening, and move
    played episodic episodes to their Played album. One run at a time."""
    with _sync_lock:
        return _sync_plex()


def _sync_plex():
    try:
        tracks = plex_tracks()
    except Exception as e:
        _last_plex_sync.update(at=now_iso(), result=f"failed: {e}")
        log.error("Plex play check failed: %s", e)
        return _last_plex_sync["result"]
    if tracks is None:
        _last_plex_sync.update(at=now_iso(), result="off (no token)")
        return _last_plex_sync["result"]
    events, seen = [], 0
    for feed in feeds():
        lock = feed_lock(feed["id"])
        if not lock.acquire(timeout=10):
            continue
        try:
            fs = feed_state(feed["id"])
            old = fs.get("plays") or {}
            updated_ts = datetime.fromisoformat(fs["updated"]).timestamp() if fs.get("updated") else 0
            new = {}
            for fold, name, title in managed_files(feed, fs):
                info = tracks.get(plex_file_key(fold, name))
                if not info:
                    continue
                info = dict(info)
                if feed["kind"] == "latest" and info["lv"] and info["lv"] < updated_ts:
                    info.update(vc=0, lv=0, off=0)  # that play was of the audio before the last swap
                new[name] = info
                seen += 1
                prev = old.get(name, {})
                if info["lv"] and info["lv"] > prev.get("lv", 0):
                    events.append({"at": datetime.fromtimestamp(info["lv"], TZ).isoformat(), "feed": feed["name"],
                                   "title": title, "what": "finished", "dur": info["dur"]})
                elif info["off"] and abs(info["off"] - prev.get("off", 0)) >= 60:
                    events.append({"at": now_iso(), "feed": feed["name"], "title": title, "what": "listening",
                                   "off": info["off"], "dur": info["dur"]})
            update_state(feed["id"], plays=new, plays_at=now_iso())
        finally:
            lock.release()
        if feed["kind"] == "episodic":
            lock.acquire()
            try:
                sync_played(feed, {k: (v["vc"], v["lv"]) for k, v in tracks.items()})
            except Exception as e:
                log.error("%s: played move failed: %s", feed["name"], e)
            finally:
                lock.release()
    add_history(events)
    _last_plex_sync.update(at=now_iso(), result=f"OK: {seen} podcast tracks, {len(events)} new plays")
    return _last_plex_sync["result"]


def plex_file_key(folder, name):
    root = settings().get("plex_root") or MEDIA_HOST_PATH
    return _share_path(os.path.join(root, folder, name))


# ------------------------------------------------------------ latest only ---

def check_latest(feed, reason, restore=False):
    fid, name, folder = feed["id"], feed["name"], feed["folder"]
    d = os.path.join(MEDIA, folder)
    os.makedirs(d, exist_ok=True)
    base = slug(folder)
    fs = feed_state(fid)
    channel, items = read_feed(feed["url"], feed["id"])
    chosen = pick_latest(apply_skips(items, feed), feed)
    if not chosen:
        raise RuntimeError("no episodes left after the skip rules")
    guids = [e["guid"] for e in chosen]
    have = fs.get("files") or []
    sig = f"{feed['stamp']}|{feed.get('tidy', True)}|{TITLE_VERSION}|{name}"
    if not restore and fs.get("guids") == guids:
        gone = [x for x in have if not os.path.exists(os.path.join(d, x))]
        retagged = 0
        if fs.get("title_sig") != sig:  # title settings changed: re-tag in place, no download
            for i, (ep, x) in enumerate(zip(chosen, have), 1):
                p = os.path.join(d, x)
                if os.path.exists(p):
                    retag_number(p, name, i, len(chosen), display_title(feed, ep, name), ep["show"])
                    retagged += 1
            if retagged:
                plex_nudge(folder)
        msg = f"no change ({len(guids)} episodes" + (f", {len(gone)} deleted by you; Restore brings them back"
                                                      if gone else "") + (f", {retagged} re-titled" if retagged else "") + ")"
        upd = {"title_sig": sig}
        if retagged:
            upd["episodes"] = [{"show": e["show"], "title": display_title(feed, e, name), "pub": e["pub"][:10],
                                "pub_full": e["pub"]} for e in chosen]
        update_state(fid, last_result=msg, last_error="", **upd)
        log.info("%s: %s [%s]", name, msg, reason)
        return msg

    parts, cover = [], None
    for i, ep in enumerate(chosen, 1):
        part, final = download_part(ep, d, f"{base}-{i}")
        url, src = art_choice(feed, channel, ep, fs)
        art = fetch_art(url)
        tag(part, name, ep, i, len(chosen), art, feed)
        cover = cover or (art if url and feed.get("art_source") != "episode" else None)
        parts.append((part, final))
    new_files = []
    for part, final in parts:  # every download succeeded: swap in
        os.replace(part, os.path.join(d, final))
        new_files.append(final)
    pat = re.compile(rf"^{re.escape(base)}-\d+\.({'|'.join(AUDIO_EXT)})$")
    for x in os.listdir(d):
        if pat.match(x) and x not in new_files:
            os.remove(os.path.join(d, x))
    write_cover(d, cover or fetch_art(art_choice(feed, channel, {}, fs)[0]))
    nudge = plex_nudge(folder)
    for x in new_files:  # same Plex track, new audio: clear the old play count and resume point
        plex_reset((fs.get("plays") or {}).get(x, {}).get("rk"))
    msg = f"{'restored' if restore else 'updated'}: {len(chosen)} episode{'s' * (len(chosen) != 1)} ({nudge})"
    update_state(fid, guids=guids, files=new_files, last_result=msg, last_error="", updated=now_iso(),
                 stamp=feed["stamp"], plays={}, title_sig=sig,
                 apple_art=fs.get("apple_art", ""),
                 episodes=[{"show": e["show"], "title": display_title(feed, e, name), "pub": e["pub"][:10],
                            "pub_full": e["pub"]} for e in chosen])
    log.info("%s: %s [%s]", name, msg, reason)
    return msg


# --------------------------------------------------------------- episodic ---

def sid(guid):
    return hashlib.sha1(guid.encode()).hexdigest()[:10]


def now_iso():
    return datetime.now(TZ).isoformat()


def ep_base(e):
    return f"{(e.get('pub') or '0000-00-00')[:10]}-{slug(e['title'])[:50]}"


def renumber(feed, eps):
    """Track numbers: unplayed oldest first; played most-recently-played first."""
    folder, name = feed["folder"], feed["name"]
    groups = [
        (folder, name, sorted([e for e in eps.values() if e["status"] == "unplayed"], key=lambda e: e["pub"])),
        (played_folder(folder), f"{name} - Played",
         sorted([e for e in eps.values() if e["status"] == "played"], key=lambda e: e.get("played_at", ""),
                reverse=True)),
    ]
    changed = set()
    for fold, album, lst in groups:
        for i, e in enumerate(lst, 1):
            p = os.path.join(MEDIA, fold, e.get("file", ""))
            title = display_title(feed, e, album)
            want = f"{i}/{len(lst)}|{album}|{title}|{e.get('show', '')}"
            if e.get("file") and os.path.exists(p) and e.get("tagged") != want:
                retag_number(p, album, i, len(lst), title, e.get("show"))
                e["tagged"] = want
                changed.add(fold)
    return changed


def move_ep(feed, e, to_status):
    src_fold = played_folder(feed["folder"]) if e["status"] == "played" else feed["folder"]
    dst_fold = played_folder(feed["folder"]) if to_status == "played" else feed["folder"]
    os.makedirs(os.path.join(MEDIA, dst_fold), exist_ok=True)
    src = os.path.join(MEDIA, src_fold, e.get("file", ""))
    if e.get("file") and os.path.exists(src) and src_fold != dst_fold:
        os.replace(src, os.path.join(MEDIA, dst_fold, e["file"]))
    e["status"] = to_status
    e.pop("tagged", None)
    return {src_fold, dst_fold}


def fetch_ep(feed, channel, e, fs, fold):
    d = os.path.join(MEDIA, fold)
    os.makedirs(d, exist_ok=True)
    part, final = download_part(e, d, ep_base(e))
    url, _ = art_choice(feed, channel, e, fs)
    tag(part, feed["name"] + (" - Played" if fold != feed["folder"] else ""), e, 1, 1, fetch_art(url), feed)
    os.replace(part, os.path.join(d, final))
    e["file"] = final
    e.pop("tagged", None)


def check_episodic(feed, reason, restore=False):
    fid, name, folder = feed["id"], feed["name"], feed["folder"]
    pfold = played_folder(folder)
    d, pd = os.path.join(MEDIA, folder), os.path.join(MEDIA, pfold)
    os.makedirs(d, exist_ok=True)
    fs = feed_state(fid)
    eps = fs.get("eps") or {}
    channel, items = read_feed(feed["url"], feed["id"])
    by_guid = {it["guid"]: it for it in items}
    for e in eps.values():  # keep each episode's show in step with the current rules (re-tagged below)
        it = by_guid.get(e.get("guid"))
        if it and e.get("status") in ("unplayed", "played"):
            e["show"], e["own"] = it["show"], it["own"]
    items = sorted(apply_skips(items, feed), key=lambda e: e["pub"])
    touched, notes = set(), []

    newest = {e["guid"] for e in items[-max(1, int(feed.get("start_n") or 1)):]}
    if not eps:  # first run: only the newest N; everything older is history
        for e in items:
            if e["guid"] not in newest:
                eps[sid(e["guid"])] = {"guid": e["guid"], "status": "skipped", "pub": e["pub"]}
    else:  # "start with the newest N" always holds: raising N brings set-aside ones in
        for g in newest:
            if eps.get(sid(g), {}).get("status") == "skipped":
                del eps[sid(g)]  # becomes new below; played or deleted ones are left alone

    # files you deleted: remember, don't re-download
    for e in eps.values():
        if e["status"] in ("unplayed", "played") and e.get("file"):
            fold = pfold if e["status"] == "played" else folder
            if not os.path.exists(os.path.join(MEDIA, fold, e["file"])):
                if restore:
                    fetch_ep(feed, channel, e, fs, fold)
                    touched.add(fold)
                    notes.append(f"restored {e['title'][:40]}")
                else:
                    e["status"] = "deleted" if e["status"] == "unplayed" else "deleted_played"
    if restore:
        for e in eps.values():
            if e["status"] in ("deleted", "deleted_played"):
                back = "unplayed" if e["status"] == "deleted" else "played"
                fetch_ep(feed, channel, e, fs, folder if back == "unplayed" else pfold)
                e["status"] = back
                touched.add(folder if back == "unplayed" else pfold)
                notes.append(f"restored {e['title'][:40]}")

    # new episodes, oldest first, up to the backlog cap
    unplayed = sum(1 for e in eps.values() if e["status"] == "unplayed")
    cap = max(1, int(feed.get("max_unplayed") or 20))
    added = 0
    for it in items:
        k = sid(it["guid"])
        if k in eps:
            continue
        if unplayed >= cap:
            notes.append(f"backlog full ({cap} unplayed); newer episodes wait")
            break
        e = {"guid": it["guid"], "title": it["title"], "show": it["show"], "own": it["own"], "pub": it["pub"], "url": it["url"],
             "type": it["type"], "image": it["image"], "status": "unplayed", "added": now_iso()}
        fetch_ep(feed, channel, e, fs, folder)
        eps[k] = e
        unplayed += 1
        added += 1
        touched.add(folder)

    # played episodes past their keep time
    days = int(feed.get("played_days") or 0)
    if days > 0:
        cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
        for e in eps.values():
            if e["status"] in ("played", "deleted_played") and e.get("played_at", "") < cutoff:
                p = os.path.join(pd, e.get("file", ""))
                if e.get("file") and os.path.exists(p):
                    os.remove(p)
                e["status"] = "expired"
                touched.add(pfold)

    touched |= renumber(feed, eps)
    url, _ = art_choice(feed, channel, {}, fs)
    art = fetch_art(url)
    write_cover(d, art)
    if os.path.isdir(pd):
        write_cover(pd, art)
    nudge = plex_nudge(*sorted(touched)) if touched else ""
    unp = sum(1 for e in eps.values() if e["status"] == "unplayed")
    msg = (f"{'restored; ' if restore else ''}{added} new, {unp} unplayed"
           + (f" ({'; '.join(notes[:3])})" if notes else "") + (f" ({nudge})" if nudge else ""))
    update_state(fid, eps=eps, apple_art=fs.get("apple_art", ""), last_result=msg, last_error="",
                 **({"updated": now_iso()} if touched else {}))
    log.info("%s: %s [%s]", name, msg, reason)
    return msg


def sync_played(feed, plays=None):
    """Move episodes Plex says were played to the Played album."""
    plays = plex_plays() if plays is None else plays
    if plays is None:
        return
    fid, folder = feed["id"], feed["folder"]
    fs = feed_state(fid)
    eps = fs.get("eps") or {}
    touched = set()
    for e in eps.values():
        if e["status"] != "unplayed" or not e.get("file"):
            continue
        vc, lv = plays.get(plex_file_key(folder, e["file"]), (0, 0))
        since = e.get("unplayed_at", 0)  # a Mark unplayed resets the count
        if vc >= 1 and lv > since:
            touched |= move_ep(feed, e, "played")
            e["played_at"] = datetime.fromtimestamp(lv, TZ).isoformat()
            log.info("%s: played \"%s\"", feed["name"], e["title"][:60])
    if touched:
        touched |= renumber(feed, eps)
        update_state(fid, eps=eps, updated=now_iso())
        plex_nudge(*sorted(touched))


def mark(feed, key, played):
    lock = feed_lock(feed["id"])
    with lock:
        fs = feed_state(feed["id"])
        eps = fs.get("eps") or {}
        e = eps.get(key)
        if not e or e["status"] not in ("unplayed", "played"):
            return
        touched = move_ep(feed, e, "played" if played else "unplayed")
        if played:
            e["played_at"] = now_iso()
        else:
            e["unplayed_at"] = int(time.time())
            e.pop("played_at", None)
        touched |= renumber(feed, eps)
        update_state(feed["id"], eps=eps, updated=now_iso())
    plex_nudge(*sorted(touched))
    log.info("%s: marked %s \"%s\"", feed["name"], "played" if played else "unplayed", e["title"][:60])


# ------------------------------------------------------------------ check ---

def run_check(feed_id, reason="schedule", restore=False):
    lock = feed_lock(feed_id)
    if not lock.acquire(blocking=False):
        return "already running"
    try:
        feed = next((f for f in feeds() if f["id"] == feed_id), None)
        if feed is None:
            return "no such feed"
        update_state(feed_id, last_check=now_iso(), running=True)
        relearn(feed)
        try:
            if feed["kind"] == "episodic":
                return check_episodic(feed, reason, restore)
            return check_latest(feed, reason, restore)
        except Exception as e:
            clear_parts(os.path.join(MEDIA, feed["folder"]), os.path.join(MEDIA, played_folder(feed["folder"])))
            update_state(feed_id, last_error=f"{type(e).__name__}: {e}")
            log.error("%s: FAILED %s: %s [%s] (existing files kept)", feed["name"], type(e).__name__, e, reason)
            return f"failed: {e}"
        finally:
            update_state(feed_id, running=False)
    finally:
        lock.release()


def rename_feed(old, new):
    """Move a renamed feed's folders and files so nothing is stranded; re-tag on the next check."""
    if old["folder"] == new["folder"]:
        return
    pairs = [(old["folder"], new["folder"]), (played_folder(old["folder"]), played_folder(new["folder"]))]
    for a, b in pairs:  # refuse before touching anything
        src, dst = os.path.join(MEDIA, a), os.path.join(MEDIA, b)
        if os.path.isdir(src) and os.path.isdir(dst) and os.listdir(dst):
            raise ValueError(f"A folder called “{b}” already exists and isn't empty. "
                             "Choose another name, or delete that folder on Config first.")
    with feed_lock(old["id"]):
        for a, b in pairs:
            src, dst = os.path.join(MEDIA, a), os.path.join(MEDIA, b)
            if os.path.isdir(src):
                if os.path.isdir(dst):
                    os.rmdir(dst)  # empty
                os.rename(src, dst)
        fs = feed_state(old["id"])
        upd = {"title_sig": ""}  # forces an in-place re-tag (new album) on the next check
        if old.get("kind") != "episodic" and fs.get("files"):
            ob, nb, d, files = slug(old["folder"]), slug(new["folder"]), os.path.join(MEDIA, new["folder"]), []
            for x in fs["files"]:
                y = nb + x[len(ob):] if x.startswith(ob + "-") else x
                if y != x and os.path.exists(os.path.join(d, x)):
                    os.rename(os.path.join(d, x), os.path.join(d, y))
                files.append(y)
            plays = {(nb + k[len(ob):] if k.startswith(ob + "-") else k): v for k, v in (fs.get("plays") or {}).items()}
            upd.update(files=files, plays=plays)
        if fs.get("eps"):
            for e in fs["eps"].values():
                e.pop("tagged", None)
            upd["eps"] = fs["eps"]
        update_state(old["id"], **upd)
    plex_nudge(old["folder"], played_folder(old["folder"]), new["folder"], played_folder(new["folder"]))
    log.info("renamed feed %s -> %s (folders moved)", old["name"], new["name"])


def stray_folders():
    """Folders in the save location that no current feed owns: [(name, audio files, bytes)]."""
    owned = {x for f in feeds() for x in (f["folder"], played_folder(f["folder"]))}
    out = []
    if os.path.isdir(MEDIA):
        for n in sorted(os.listdir(MEDIA)):
            p = os.path.join(MEDIA, n)
            if os.path.isdir(p) and not n.startswith((".", "@")) and n not in owned:
                files = [os.path.join(r, x) for r, _, xs in os.walk(p) for x in xs]
                out.append((n, sum(1 for x in files if x.lower().endswith(tuple("." + e for e in AUDIO_EXT))),
                            sum(os.path.getsize(x) for x in files if os.path.exists(x))))
    return out


def start_over(feed):
    """Forget this feed's history and files; the next check starts as if newly added."""
    lock = feed_lock(feed["id"])
    with lock:
        for fold in (feed["folder"], played_folder(feed["folder"])):
            shutil.rmtree(os.path.join(MEDIA, fold), ignore_errors=True)
        fs = feed_state(feed["id"])
        replace_state(feed["id"], {"apple_art": fs.get("apple_art", "")})
    log.info("%s: started over", feed["name"])
    run_check(feed["id"], "start over")


# ------------------------------------------------------------- analyzer ---

def analyze(url):
    """Read a feed and suggest settings. Returns (suggested feed dict, report rows, warnings, preview)."""
    channel, items = read_feed(url)
    now = datetime.now(TZ)
    sug = dict(DEFAULTS, name=channel["title"], url=url)
    rows, warn = [], []
    dated = [e for e in items if e["pub"]]
    pubs = [datetime.fromisoformat(e["pub"]).astimezone(TZ) for e in dated]
    rows.append(("Episodes", f"{len(items)} with audio"
                 + (f", {min(pubs).date()} to {max(pubs).date()}" if pubs else "")))
    if len(dated) < 8:
        warn.append(f"Only {len(dated)} dated episodes: these suggestions are a weak guess.")

    # bonus / trailer episodes and repeating title prefixes
    recent = sorted(dated, key=lambda e: e["pub"], reverse=True)[:120]
    etypes = Counter(e["etype"] for e in recent)
    sug["skip_types"] = [t for t in ("bonus", "trailer") if etypes.get(t)]
    durs = [e["dur"] for e in recent if e["dur"]]
    med = statistics.median(durs) if durs else 0
    prefixes = Counter(e["title"].split(":")[0].strip() for e in recent
                       if ":" in e["title"] and 2 <= len(e["title"].split(":")[0].strip()) <= 30)
    by_type, by_len = [], []
    for pre, n in prefixes.items():
        group = [e for e in recent if e["title"].startswith(pre + ":")]
        if n < 2 or n > 0.4 * len(recent):
            continue
        gd = [e["dur"] for e in group if e["dur"]]
        if any(e["etype"] != "full" for e in group):
            by_type.append(pre + ":")
        elif med and gd and abs(statistics.median(gd) - med) > 0.3 * med:
            by_len.append(pre + ":")
    # a different length only means "extra" for a regular show; serials vary by nature
    serial_like = channel["type"] == "serial"
    dated_sorted = sorted(datetime.fromisoformat(e["pub"]) for e in recent)
    if len(dated_sorted) > 2:
        g = [(b - a).total_seconds() / 86400 for a, b in zip(dated_sorted, dated_sorted[1:])]
        serial_like = serial_like or statistics.median(g) >= 2.5
    skip_words = by_type + ([] if serial_like else by_len)
    sug["skip_titles"] = ", ".join(skip_words)
    if sug["skip_types"] or skip_words:
        why = []
        if sug["skip_types"]:
            why.append(f"{sum(etypes[t] for t in sug['skip_types'])} labelled "
                       + "/".join(sug["skip_types"]))
        if skip_words:
            why.append("titles starting " + ", ".join(f'"{w}"' for w in skip_words)
                       + " are labelled bonus or run a different length")
        rows.append(("Extras", "; ".join(why) + ". Suggest skipping them."))
    else:
        rows.append(("Extras", "none found"))
    main = apply_skips(recent, sug)
    md = [e["dur"] for e in main if e["dur"]]
    if md:
        rows.append(("Length", f"typically {round(statistics.median(md) / 60)} min"))

    # rhythm
    mp = [datetime.fromisoformat(e["pub"]).astimezone(TZ) for e in main]
    last30 = [p for p in mp if p > now - timedelta(days=30)]
    per_day = Counter(p.date() for p in last30)
    ppd = statistics.median(per_day.values()) if per_day else 0
    last8w = [p for p in mp if p > now - timedelta(days=56)]
    wk = Counter(p.weekday() for p in last8w)
    days = [i for i in range(7) if wk.get(i, 0) >= 3] or sorted(wk) or list(range(7))
    gaps = [(a - b).total_seconds() / 86400 for a, b in zip(mp, mp[1:])]
    gap = statistics.median(gaps) if gaps else 0
    serial = channel["type"] == "serial"
    if not last30:
        rhythm = f"nothing in the last 30 days (typical gap {gap:.0f} days)"
    elif ppd >= 2:
        rhythm = f"about {ppd:g} a day"
    else:
        rhythm = f"about 1 a day on {', '.join(DAYS[i] for i in days)}" if gap < 2 else f"every {gap:.0f} days or so"
    rows.append(("Rhythm", rhythm + (" · the feed says it is a serial" if serial else "")))

    # kind and what to take
    newsy = sum(1 for e in main[:20] if re.search(r"\b(news|newscast|update|briefing|headlines|"
                                                   r"\d{1,2}:\d{2}\s*[ap]m|\d{1,2}\s*[ap]m)\b", e["title"], re.I))
    if serial or (gap >= 2.5 and newsy < 5 and ppd < 2):
        sug["kind"], why = "episodic", "irregular or serial: you'd want every episode, in order"
        sug["order"] = "oldest"
    else:
        sug["kind"], why = "latest", "daily or news-like: you'd want only the newest"
    if sug["kind"] == "latest":
        if ppd >= 4 or newsy >= 5:
            sug["take"], sug["count"] = "newest_n", 1
            why += "; takes the single newest"
        elif ppd >= 2:
            sug["take"] = "latest_day"
            why += f"; several pieces a day, so takes the whole latest day (track count will vary)"
        else:
            sug["take"], sug["count"] = "newest_n", 1
    rows.append(("Kind", ("Episodic" if sug["kind"] == "episodic" else "Latest only") + f": {why}"))
    if sug["kind"] == "episodic" and by_len and not serial_like:  # length says nothing for a series
        sug["skip_titles"] = ", ".join(by_type)
        rows = [(a, b) for a, b in rows if a != "Extras"]
        rows.insert(1, ("Extras", (f"{sum(etypes[t] for t in sug['skip_types'])} labelled "
                                   + "/".join(sug["skip_types"]) + ". Suggest skipping them.")
                        if sug["skip_types"] else "none labelled"))
        main = apply_skips(recent, sug)

    # schedule from posting times
    hours = [p.hour * 60 + p.minute for p in (last30 or mp[:30])]
    span_days = (max(pubs) - min(pubs)).days if pubs else 0
    if hours:
        if (ppd >= 4 or newsy >= 5) and span_days < 7:
            sug["start"], sug["end"], sug["every"], sug["days"] = "05:00", "23:00", 60, list(range(7))
            sched = "posts many times a day; the feed only keeps a few hours, so check hourly 5:00–23:00 every day"
        elif ppd >= 4 or newsy >= 5:
            sug["start"] = f"{max(0, min(hours) // 60):02d}:00"
            sug["end"] = f"{min(23, max(hours) // 60 + 1):02d}:00"
            sug["every"] = 60
            sug["days"] = days
            sched = f"posts between {min(hours)//60}:00 and {max(hours)//60 + 1}:00, so check hourly"
        elif sug["kind"] == "episodic":
            sug["start"], sug["end"], sug["every"], sug["days"] = "06:00", "22:00", 240, list(range(7))
            sched = "irregular, so check every 4 hours"
        else:
            typ = int(statistics.median(hours))
            st = ((typ + 15 + 29) // 30) * 30
            st = min(st, 22 * 60)
            en = min(st + 240, 23 * 60 + 30)
            sug["start"], sug["end"], sug["every"], sug["days"] = (f"{st//60:02d}:{st%60:02d}",
                                                                   f"{en//60:02d}:{en%60:02d}", 30, days)
            t = datetime(2000, 1, 1, typ // 60, typ % 60).strftime("%-I:%M %p")
            sched = f"usually posts around {t} your time, so check from {sug['start']} to {sug['end']}"
        rows.append(("Schedule", sched))

    # schedule: Auto when there's enough recent history to learn from
    learned = learn_schedule(items, sug)
    if learned.get("learning"):
        sug["sched"] = "auto"
        rows = [(a, b) for a, b in rows if a != "Schedule"]
        rows.append(("Schedule", "Auto, learning: this feed keeps only its newest episode, so the app checks every "
                                 f"15 min and learns its rhythm by watching it (needs {LEARN_NEED} posts; for an hourly "
                                 "newscast, about half a day). Then it switches to the learned schedule."))
        warn[:] = [x for x in warn if "weak guess" not in x]
    elif learned["basis"] >= 4:
        sug["sched"] = "auto"
        rows = [(a, b) for a, b in rows if a != "Schedule"]
        rows.append(("Schedule", f"Auto: {schedule_text(dict(sug, **learned))} ({learned['why']}; learned from "
                                 f"{learned['basis']} recent episodes, refreshed daily, plus a noon check)"))
    else:
        sug["sched"] = "manual"
        warn.append("Too little recent history to learn a schedule; the manual one above is a guess.")

    # title stamp: skip it when titles already carry a date or time
    dated_titles = sum(1 for e in main[:20] if re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b|\b\d{1,2}/\d{1,2}\b|"
        r"\b\d{1,2}:\d{2}\s*[ap]\.?m\b|\b\d{4}-\d{2}-\d{2}\b", e["title"], re.I))
    if sug["kind"] == "episodic":
        sug["stamp"], why = "date", "episodic: the date keeps the order clear"
    elif ppd >= 2 or newsy >= 5:
        sug["stamp"], why = "time", "several a day"
    else:
        sug["stamp"], why = "daydate", "one a day"
    if main and dated_titles >= max(2, len(main[:20]) // 2):
        why += "; the titles already include a date or time (choose None to leave them as they are)"
    ex = stamp_text(sug["stamp"], main[0]["pub"]) if main else ""
    rows.append(("Title", f"{STAMPS[sug['stamp']].split(' (')[0]}: {why}"
                 + (f' · e.g. "{ex}| {main[0]["title"][:40]}"' if ex else "")))

    # artwork
    if channel["image"]:
        rows.append(("Artwork", "the feed has a show image"))
    else:
        a = apple_art(channel["title"])
        rows.append(("Artwork", "found in Apple's directory" if a else "none found: paste an image link below"))
        if not a:
            warn.append("No artwork in the feed or Apple's directory.")
    if any(e["image"] for e in main[:10]) and channel["image"]:
        rows.append(("", "episodes also have their own images (option: use episode images)"))

    # audio
    if main:
        try:
            with http_get(main[0]["url"], timeout=30, extra={"Range": "bytes=0-1023"}) as r:
                host = urllib.parse.urlparse(r.geturl()).netloc
                rows.append(("Audio", f"{r.headers.get('Content-Type', '?')}, downloads OK (ends at {host})"))
        except Exception as e:
            warn.append(f"A test download of the newest episode failed ({e}). It may be a passing limit; "
                        "the first check will tell.")

    if sug["kind"] == "episodic":
        preview = main[: max(1, sug["start_n"])][::-1]
    else:
        preview = pick_latest(main, sug)
    return sug, rows, warn, preview


# -------------------------------------------------------------- schedule ---

def _hm(s):
    h, m = (s or "00:00").split(":")
    return int(h), int(m)


def drop_hours(feed):
    sh, _ = _hm(feed.get("start"))
    eh, _ = _hm(feed.get("end") or feed.get("start"))
    return range(sh, eh + 1)


def burst_times(feed):
    """Minutes of the day to check a drop-timed feed: each hour's drop + each burst offset."""
    d = int(feed["drop"])
    return sorted(t for h in drop_hours(feed) for off in feed["burst"] if (t := h * 60 + d + int(off)) < 24 * 60)


def drop_before(feed, slot):
    """The drop time (datetime) a burst slot belongs to."""
    m = slot.hour * 60 + slot.minute
    drops = [h * 60 + int(feed["drop"]) for h in drop_hours(feed) if h * 60 + int(feed["drop"]) <= m]
    t = max(drops) if drops else m
    return slot.replace(hour=t // 60, minute=t % 60)


def latest_slot(feed, now):
    """Most recent scheduled time at or before now (today only), or None."""
    if now.weekday() not in feed.get("days", []):
        return None
    if feed.get("drop") is not None and feed.get("burst"):
        m = now.hour * 60 + now.minute
        past = [t for t in burst_times(feed) if t <= m]
        return now.replace(hour=past[-1] // 60, minute=past[-1] % 60, second=0, microsecond=0) if past else None
    sh, sm = _hm(feed.get("start"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if now < start:
        return None
    every = int(feed.get("every") or 0)
    if every <= 0:
        return start
    eh, em = _hm(feed.get("end") or feed.get("start"))
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    last = start + timedelta(minutes=every * ((min(now, end) - start) // timedelta(minutes=every)))
    return last if last <= end else None


AUTO_FALLBACK = {"days": list(range(7)), "start": "06:00", "end": "22:00", "every": 240}
AUTO_SAFETY = "12:00"  # Auto feeds also check once a day at noon, whatever was learned


def _fmt_hm(m):
    m = max(0, min(23 * 60 + 59, int(m)))
    return f"{m // 60:02d}:{m % 60:02d}"


LEARN_NEED = 8  # episodes a short feed must be seen posting before its rhythm is trusted
LEARNING = {"days": list(range(7)), "start": "05:00", "end": "23:00", "every": 15}


def learn_schedule(items, feed, fs=None):
    """Learn days, window and interval from the last 30 days of posting (the feed's items plus
    episodes remembered from earlier checks). Returns days/start/end/every plus 'basis' (how many
    episodes it learned from) and 'why'. A feed that keeps only its newest episode or two is
    watched closely ('learning') until enough posts have been seen."""
    now = datetime.now(TZ)
    listed = len([e for e in items if e["pub"]])
    main = apply_skips([e for e in with_seen(items, fs or {}) if e["pub"]], feed)
    pubs = sorted(datetime.fromisoformat(e["pub"]).astimezone(TZ) for e in main)
    last30 = [p for p in pubs if p > now - timedelta(days=30)]
    if listed <= 2 and len(last30) < LEARN_NEED:
        return dict(LEARNING, basis=len(last30), learned=now.isoformat(), learning=True,
                    why=f"this feed keeps only its newest episode, so the app is watching it to learn its rhythm "
                        f"(seen {len(last30)} of {LEARN_NEED} needed)")
    if len(last30) < 4:  # slow show: look back 90 days for which weekdays it posts on
        last90 = [p for p in pubs if p > now - timedelta(days=90)]
        if len(last90) >= 3:
            wk = Counter(p.weekday() for p in last90)
            days = sorted(d for d, n in wk.items() if n >= max(1, len(last90) // 4)) or sorted(wk)
            return dict(days=days, start="06:00", end="22:00", every=240, basis=len(last90), learned=now.isoformat(),
                        why=f"posts every week or two (on {', '.join(DAYS[d] for d in days)}), so every 4 hours on those days")
        return dict(AUTO_FALLBACK, basis=len(last30), learned=now.isoformat(),
                    why=f"only {len(last90)} episodes in 90 days, so every 4 hours every day")
    per_day = statistics.median(Counter(p.date() for p in last30).values())
    span = (last30[-1] - last30[0]).days
    mins = sorted(p.hour * 60 + p.minute for p in last30)
    q = lambda f_: mins[min(len(mins) - 1, int(f_ * (len(mins) - 1)))]
    last8w = [p for p in pubs if p > now - timedelta(days=56)]
    wk = Counter(p.weekday() for p in last8w)
    weeks = max(1, min(8, (now - last8w[0]).days // 7 + 1)) if last8w else 1
    days = [d for d in range(7) if wk.get(d, 0) >= max(1, round(weeks * 0.4))] or sorted(wk)
    if per_day >= 4:  # many a day: hourly across the hours it posts
        if span < 7:
            start, end = 5 * 60, 23 * 60
            why = "posts many times a day; the feed keeps only a few hours, so hourly 05:00–23:00"
        else:
            start, end = q(0.05) // 60 * 60, min(23 * 60, (q(0.95) // 60 + 1) * 60)
            why = "posts many times a day, so hourly through its posting hours"
        out = dict(days=days if span >= 7 else list(range(7)), start=_fmt_hm(start), end=_fmt_hm(end), every=60,
                   basis=len(last30), learned=now.isoformat(), why=why)
        # A steady drop minute (e.g. ~:06:40 every hour) means check right after it, not on the hour.
        secs = [p.minute * 60 + p.second for p in last30[-48:]]
        if any(x < 300 for x in secs) and any(x > 3300 for x in secs):  # on-the-hour drops straddle :00
            secs = [x - 3600 if x > 1800 else x for x in secs]
        mid = statistics.median(secs)
        if len(secs) >= 4 and sum(1 for s_ in secs if abs(s_ - mid) <= 240) >= 0.7 * len(secs):
            drop = int(mid // 60)  # may be -1 (:59 of the hour before)
            late = max(s_ for s_ in secs if abs(s_ - mid) <= 240)
            first = max(drop + 1, -(-late // 60))  # just after the latest normal drop
            out.update(drop=drop, burst=sorted({first - drop, first - drop + 2, first - drop + 5, first - drop + 10}),
                       why=f"posts every hour at about :{drop % 60:02d}, so checks at :{first % 60:02d} and again a few minutes "
                           "later if it's late, stopping once it's in")
        return out
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(last30, last30[1:])]
    if statistics.median(gaps) >= 2.5:  # weekly-ish or irregular: a few checks on its days
        return dict(days=days, start="06:00", end="22:00", every=240, basis=len(last30), learned=now.isoformat(),
                    why="posts every few days, so every 4 hours on the days it posts")
    start = max(0, q(0.1) - 15)
    end = min(23 * 60 + 30, (q(0.9) + 180 + 14) // 15 * 15)
    return dict(days=days, start=_fmt_hm(start // 15 * 15), end=_fmt_hm(end), every=30, basis=len(last30),
                learned=now.isoformat(), why="usually posts between "
                + datetime(2000, 1, 1, q(0.1) // 60, q(0.1) % 60).strftime("%-I:%M %p") + " and "
                + datetime(2000, 1, 1, q(0.9) // 60, q(0.9) % 60).strftime("%-I:%M %p"))


def effective(feed, fs=None):
    """The schedule actually used: the learned one for Auto feeds, else the feed's own."""
    if feed.get("sched") != "auto":
        return feed
    a = (fs if fs is not None else feed_state(feed["id"])).get("auto") or dict(AUTO_FALLBACK, basis=0, why="not learned yet")
    return dict(feed, **{k: a[k] for k in ("days", "start", "end", "every")}, drop=a.get("drop"), burst=a.get("burst"))


def relearn(feed, force=False):
    """Re-learn an Auto feed's schedule if it's over a day old (or forced)."""
    if feed.get("sched") != "auto":
        return
    a = feed_state(feed["id"]).get("auto") or {}
    fresh = a.get("learned") and datetime.fromisoformat(a["learned"]) > datetime.now(TZ) - timedelta(hours=24)
    if not force and fresh and not a.get("learning"):  # while learning, re-learn on every check
        return
    try:
        _, items = read_feed(feed["url"], feed["id"])
        new = learn_schedule(items, feed, feed_state(feed["id"]))
        update_state(feed["id"], auto=new)
        if ((a.get("days"), a.get("start"), a.get("end"), a.get("every"), a.get("drop"), a.get("burst"))
                != (new["days"], new["start"], new["end"], new["every"], new.get("drop"), new.get("burst"))):
            log.info("%s: schedule learned: %s (%s)", feed["name"], schedule_text(dict(feed, **new)), new["why"])
    except Exception as e:
        log.info("%s: schedule not re-learned: %s", feed["name"], e)


def scheduler():
    last_poll = 0.0
    while True:
        try:
            now = datetime.now(TZ)
            st = state()
            items = feeds()
            for f in items:
                if not f.get("enabled", True):
                    continue
                fs = st.get(f["id"], {})
                eff = effective(f, fs)
                slot = latest_slot(eff, now)
                if slot is not None and eff.get("drop") is not None and eff.get("burst") and fs.get("updated"):
                    if datetime.fromisoformat(fs["updated"]) >= drop_before(eff, slot):
                        slot = None  # this hour's episode is already in; skip the rest of the burst
                if f.get("sched") == "auto":
                    sh, sm = _hm(AUTO_SAFETY)
                    noon = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
                    if now >= noon and (slot is None or noon > slot):
                        slot = noon
                if slot is None:
                    continue
                lc = fs.get("last_check")
                if not lc or datetime.fromisoformat(lc) < slot:
                    threading.Thread(target=run_check, args=(f["id"], "schedule"), daemon=True).start()
            if time.time() - last_poll >= PLAYED_POLL_MIN * 60:
                last_poll = time.time()
                threading.Thread(target=sync_plex, daemon=True).start()
        except Exception as e:
            log.error("scheduler: %s", e)
        time.sleep(30)


# ------------------------------------------------------------------- page ---

THEMES = {
    "studio": {"label": "Studio",
               "light": {"bg": "#EDF0F3", "surface": "#FFFFFF", "ink": "#17202B", "muted": "#5B6776", "line": "#D3DAE2",
                         "accent": "#E3A012", "accent_ink": "#17202B", "ok": "#1D7A52", "bad": "#B5372F"},
               "dark": {"bg": "#0F1318", "surface": "#161C23", "ink": "#E5EAF0", "muted": "#8D99A7", "line": "#26303B",
                        "accent": "#F0B23A", "accent_ink": "#0F1318", "ok": "#4BC08A", "bad": "#EE766B"}},
    "marine": {"label": "Marine",
               "light": {"bg": "#E8F1F1", "surface": "#FAFEFE", "ink": "#0E292D", "muted": "#4D696C", "line": "#CCDEDE",
                         "accent": "#0D7776", "accent_ink": "#FFFFFF", "ok": "#1D7A52", "bad": "#B5372F"},
               "dark": {"bg": "#0A181A", "surface": "#0F2326", "ink": "#DCEFEE", "muted": "#84A4A4", "line": "#1D383B",
                        "accent": "#3BC0BC", "accent_ink": "#05201F", "ok": "#4BC08A", "bad": "#EE766B"}},
    "plum": {"label": "Plum",
             "light": {"bg": "#F2EEF5", "surface": "#FFFFFF", "ink": "#23162D", "muted": "#695B75", "line": "#DDD3E6",
                       "accent": "#7A3EA7", "accent_ink": "#FFFFFF", "ok": "#1D7A52", "bad": "#B5372F"},
             "dark": {"bg": "#140E19", "surface": "#1D1524", "ink": "#EDE5F3", "muted": "#A092AD", "line": "#32273C",
                      "accent": "#B88ADF", "accent_ink": "#140E19", "ok": "#4BC08A", "bad": "#EE766B"}},
    "mono": {"label": "Mono",
             "light": {"bg": "#F1F1F1", "surface": "#FFFFFF", "ink": "#111111", "muted": "#626262", "line": "#D9D9D9",
                       "accent": "#111111", "accent_ink": "#FFFFFF", "ok": "#2B7A4B", "bad": "#B5372F"},
             "dark": {"bg": "#0D0D0D", "surface": "#171717", "ink": "#EDEDED", "muted": "#979797", "line": "#2A2A2A",
                      "accent": "#EDEDED", "accent_ink": "#111111", "ok": "#5CC08A", "bad": "#EE766B"}},
}

HELP = {
    "add": "Paste a podcast's RSS link. Analyze reads the feed and suggests how to handle the show; you can change anything before adding.",
    "feeds": "Each feed becomes an album in Plex. Favorite the album in Sonos once; the app keeps it current. The strip shows today's checks across 24 hours: shaded is the check window, ticks are checks, the dot is when the newest episode was posted, the line is now.",
    "played": "What Plex has recorded you playing from these albums, newest first. Read every 15 minutes, or press Check Plex now on Config.",
    "kind": "Latest only keeps just the newest episode and replaces it when a new one arrives. Episodic keeps every new episode until you've played it.",
    "take": "For Latest only: the newest episode of each show in the feed, the newest N, or every episode from the most recent day (for shows posted in segments).",
    "episodic": "Starts with the newest episodes only. When Plex sees you've played one (about 90% through), it moves to the '<name> - Played' album and is deleted after the days you set. A cap stops a show you've stopped listening to from filling the NAS.",
    "skip": "Leave out episodes the feed labels bonus or trailer, or whose titles contain any of these words. Separate several with commas.",
    "title": "Stamp adds the date or time to the front of each title, as '<stamp>| <title>', so Sonos shows which day it's from. Tidy titles drops a leading show name (Sonos shows the show underneath) and, with a stamp on, uses the date or time written in the title instead of repeating it.",
    "artwork": "Embedded in every file and saved as the album cover. The app uses your link first, then the feed's image, then Apple's podcast directory.",
    "schedule": "Auto learns when the show usually posts (days and times, last 30 days, refreshed daily) and checks around then, plus once at noon every day as a safety net. A show that drops at the same minute every hour (a newscast) is checked a minute after the drop, then a few more times that hour only if it's late. Manual uses your days and times; Every 0 means once, at the start time.",
    "save": "Set in the container's docker-compose.yml. Each feed gets its own folder here.",
    "plex": "The token lets the app ask Plex to rescan a feed's folder after changes, read what you've played, and reset a replaced episode to unplayed.",
    "appearance": "Pick a theme, and optionally your own accent colour. Auto follows your device's light or dark setting.",
    "log": "The last 120 things the app did. Newest first.",
    "tags": "Your own labels for organising this list, separated by commas (for example news, local). They stay in Podcast Solution; nothing is sent to Plex. Filter by them above the feed list.",
    "strays": "Folders in the save location that no feed owns, for example left by removing a feed but keeping its files. Renaming a feed moves its folders, so it no longer leaves strays. Deleting here removes the folder and its files for good, and Plex is asked to rescan.",
}

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@500;600&'
         'family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap">')

CSS = """
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "IBM Plex Sans",system-ui,-apple-system,sans-serif}
main{max-width:920px;margin:0 auto;padding:22px 16px 72px}
a{color:inherit;text-decoration-color:var(--line);text-underline-offset:3px}a:hover{text-decoration-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:10px;font:600 24px/1 "IBM Plex Sans Condensed",sans-serif;letter-spacing:-.01em;text-decoration:none}
.lamp{width:12px;height:12px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 22%,transparent)}
nav{display:flex;gap:4px}nav a{text-decoration:none;padding:6px 12px;border-radius:7px;color:var(--muted);font-weight:500}
nav a[aria-current=page]{color:var(--ink);background:var(--surface);box-shadow:inset 0 0 0 1px var(--line)}
h1{font:600 30px/1.15 "IBM Plex Sans Condensed",sans-serif;margin:0 0 4px;letter-spacing:-.01em}
h2{font:600 18px/1.3 "IBM Plex Sans Condensed",sans-serif;margin:0;display:flex;align-items:center;gap:6px}
.sec{margin:30px 0 0}.sechead{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.mute{color:var(--muted);font-size:13px}.ok{color:var(--ok)}.bad{color:var(--bad)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px}
.addbar{display:flex;gap:8px}.addbar input{flex:1;font-size:16px;padding:11px 12px}
input[type=text],input[type=url],input[type=time],input[type=number],input[type=password],select{width:100%;font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
input[type=color]{width:44px;height:34px;padding:2px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
button{font:500 14px/1 "IBM Plex Sans",sans-serif;border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:8px;padding:8px 12px;cursor:pointer}
button:hover{border-color:var(--muted)}button.go{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
button.quiet{border-color:transparent;background:transparent;color:var(--muted);padding:6px 8px}button.danger{color:var(--bad)}
button.small{font-size:12px;padding:4px 8px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;padding:2px;background:var(--surface)}
.seg button{border:0;background:transparent;padding:6px 10px;border-radius:7px;color:var(--muted)}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--bg)}
#feeds{border-top:1px solid var(--line)}
details.feed{border-bottom:1px solid var(--line)}
details.feed>summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:14px minmax(0,1fr) 250px 18px;gap:14px;align-items:center;padding:14px 4px}
details.feed>summary::-webkit-details-marker{display:none}
details.feed>summary:hover{background:color-mix(in srgb,var(--surface) 70%,transparent)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--ok)}.dot.err{background:var(--bad)}.dot.off{background:var(--line)}
.dot.run{background:var(--accent);animation:pulse 1s infinite alternate}@keyframes pulse{to{opacity:.35}}
.fname{font:600 18px/1.2 "IBM Plex Sans Condensed",sans-serif}.fmeta{color:var(--muted);font-size:13px;margin-top:2px}
.kind{display:inline-block;font-size:12px;font-weight:500;padding:1px 7px;border-radius:5px;background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--ink);margin-left:6px;vertical-align:2px}
details.tgroup{border-bottom:1px solid var(--line)}
details.tgroup>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;padding:14px 4px 10px;font:600 16px/1.2 "IBM Plex Sans Condensed",sans-serif}
details.tgroup>summary::-webkit-details-marker{display:none}
details.tgroup>summary .chev{margin-left:auto}
.gcount{font:500 12px/1 "IBM Plex Sans",sans-serif;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 8px}
details.tgroup details.feed{border-bottom:0;border-top:1px solid var(--line);padding-left:10px}
.tag{display:inline-block;font-size:12px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--muted);margin-left:6px;vertical-align:2px}
.tagbar{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}.tagbar button{border-radius:999px;padding:5px 11px;font-size:13px;color:var(--muted)}
.tagbar button[aria-pressed=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.chev{width:9px;height:9px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .15s}
details[open]>summary .chev{transform:rotate(45deg)}
.strip{display:block;width:100%;height:30px}.strip .win{fill:var(--accent);opacity:.2}.strip .tick{stroke:var(--accent);stroke-width:1.5}
.strip .grid{stroke:var(--line)}.strip .now{stroke:var(--ink);stroke-width:1.5}.strip .pub{fill:var(--ink)}.strip text{fill:var(--muted);font:9px "IBM Plex Sans",sans-serif}
.strip.idle{opacity:.45}
.fbody{padding:2px 4px 18px 32px}
.actions{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 12px}
.status{font-size:13px;margin:0 0 10px}
.eps{list-style:none;margin:0;padding:0}.eps li{display:grid;grid-template-columns:minmax(0,1fr) 170px auto;gap:10px;align-items:center;padding:7px 0;border-top:1px solid var(--line)}
.eps .t{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.eps .when{color:var(--muted);font-size:12px}
.pl{font-size:12px;color:var(--muted)}.pl.done{color:var(--ok)}
.bar{height:5px;border-radius:3px;background:var(--line);overflow:hidden;margin-top:3px}.bar i{display:block;height:100%;background:var(--accent)}
h3{font:600 14px/1.3 "IBM Plex Sans",sans-serif;margin:14px 0 4px;color:var(--muted)}
details.sub>summary{cursor:pointer;color:var(--muted);font-size:14px;margin:10px 0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:2px 14px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
.checks{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:4px}.checks label{display:inline-flex;align-items:center;gap:5px;margin:0;color:var(--ink);font-size:14px}
fieldset{border:0;border-top:1px solid var(--line);margin:16px 0 0;padding:6px 0 0}
legend{font:600 14px/1.3 "IBM Plex Sans",sans-serif;padding:0 8px 0 0;display:flex;align-items:center;gap:4px}
.help{position:relative;display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;border:1px solid var(--line);color:var(--muted);font:600 11px/1 "IBM Plex Sans",sans-serif;cursor:help;flex:none}
.help .tip{position:absolute;left:-10px;top:calc(100% + 8px);width:290px;max-width:80vw;background:var(--ink);color:var(--bg);padding:9px 11px;border-radius:8px;font:400 13px/1.45 "IBM Plex Sans",sans-serif;opacity:0;visibility:hidden;transition:opacity .12s;z-index:20;text-align:left}
.help:hover .tip,.help:focus .tip{opacity:1;visibility:visible}
table.kv{border-collapse:collapse;width:100%}table.kv td{padding:7px 10px 7px 0;border-top:1px solid var(--line);vertical-align:top;font-size:14px}
table.kv td:first-child{color:var(--muted);width:130px}code{font-size:13px;overflow-wrap:anywhere}
.warn{color:var(--bad);font-size:14px;margin-top:8px}
.hist{list-style:none;margin:0;padding:0}.hist li{display:grid;grid-template-columns:120px minmax(0,1fr);gap:12px;padding:6px 0;border-top:1px solid var(--line);font-size:14px}
.hist .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.swatches{display:flex;gap:10px;flex-wrap:wrap}.swatch{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:10px;padding:8px 12px;cursor:pointer;margin:0;color:var(--ink);font-size:14px}
.swatch input{margin:0}.chip{display:inline-flex;width:34px;height:18px;border-radius:5px;overflow:hidden;border:1px solid var(--line)}.chip i{flex:1}
pre{white-space:pre-wrap;font-size:12px;color:var(--muted);margin:0;max-height:420px;overflow:auto}
.empty{padding:22px 4px;color:var(--muted)}
@media (max-width:700px){details.feed>summary{grid-template-columns:14px minmax(0,1fr) 18px}details.feed>summary .stripcell{grid-column:2/3;grid-row:2}
.fbody{padding-left:4px}.eps li{grid-template-columns:minmax(0,1fr) auto}.eps li .pcol{grid-column:1/-1}.hist li{grid-template-columns:90px minmax(0,1fr)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

JS = """(function(){
[].forEach.call(document.querySelectorAll('select[data-kind]'),function(sel){var form=sel.form;
function show(){[].forEach.call(form.querySelectorAll('[data-for]'),function(el){el.hidden=el.dataset.for!==sel.value})}
sel.addEventListener('change',show);show()});
[].forEach.call(document.querySelectorAll('select[data-schedsel]'),function(sel){var form=sel.form;
function show(){[].forEach.call(form.querySelectorAll('[data-sched]'),function(el){el.hidden=el.dataset.sched!==sel.value})}
sel.addEventListener('change',show);show()});
})();
(function(){
function g(k){try{return localStorage.getItem(k)}catch(e){return null}}function s(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function arr(k){try{return JSON.parse(g(k)||'[]')}catch(e){return[]}}
var L=document.getElementById('feeds');if(!L)return;
var orig=[].slice.call(L.querySelectorAll('details.feed'));
var label={};[].forEach.call(document.querySelectorAll('[data-tagf]'),function(b){if(b.dataset.tagf)label[b.dataset.tagf]=b.textContent});
var cur='name',tag='';
function cmp(k){return function(x,y){if(k==='often')return(+y.dataset.often)-(+x.dataset.often)||x.dataset.name.localeCompare(y.dataset.name);
if(k==='updated')return(y.dataset.updated||'').localeCompare(x.dataset.updated||'');return x.dataset.name.localeCompare(y.dataset.name)}}
function has(n,t){return(n.dataset.tags||'').split('|').indexOf(t)>=0}
function reset(){orig.forEach(function(n){L.appendChild(n)});[].forEach.call(L.querySelectorAll('.tgroup,details.feed[data-clone]'),function(n){n.remove()})}
function render(){reset();
if(cur!=='tag'){orig.slice().sort(cmp(cur)).forEach(function(n){n.hidden=!!tag&&!has(n,tag);L.appendChild(n)});return}
var groups={},used={};orig.slice().sort(cmp('name')).forEach(function(n){var ts=(n.dataset.tags||'').split('|').filter(Boolean);if(!ts.length)ts=[''];
ts.forEach(function(t){(groups[t]=groups[t]||[]).push(n)})});
var keys=Object.keys(groups).sort(function(a,b){return a===''?1:b===''?-1:a.localeCompare(b)}),closed=arr('ps-gclosed');
keys.forEach(function(t){if(tag&&t!==tag)return;var d=document.createElement('details');d.className='tgroup';d.open=closed.indexOf(t)<0;
var sm=document.createElement('summary');sm.innerHTML='<span class="gname"></span><span class="gcount"></span><span class="chev" aria-hidden="true"></span>';
sm.querySelector('.gname').textContent=t?(label[t]||t):'Untagged';sm.querySelector('.gcount').textContent=groups[t].length;d.appendChild(sm);
groups[t].forEach(function(n){var node=n;if(used[n.id]){node=n.cloneNode(true);node.setAttribute('data-clone','1');node.removeAttribute('id')}else{used[n.id]=1}
node.hidden=false;d.appendChild(node)});
d.addEventListener('toggle',function(){var c=arr('ps-gclosed').filter(function(x){return x!==t});if(!d.open)c.push(t);s('ps-gclosed',JSON.stringify(c))});
L.appendChild(d)});
orig.forEach(function(n){if(!used[n.id]){n.hidden=true;L.appendChild(n)}})}
function press(sel,attr,v){[].forEach.call(document.querySelectorAll(sel),function(b){b.setAttribute('aria-pressed',String(b.dataset[attr]===v))})}
function setSort(k){cur=k;s('ps-sort',k);press('[data-sort]','sort',k);render()}
function setTag(t){tag=t;s('ps-tag',t);press('[data-tagf]','tagf',t);render()}
[].forEach.call(document.querySelectorAll('[data-sort]'),function(b){b.addEventListener('click',function(){setSort(b.dataset.sort)})});
var tb=document.querySelectorAll('[data-tagf]');[].forEach.call(tb,function(b){b.addEventListener('click',function(){setTag(b.dataset.tagf)})});
var t0=g('ps-tag')||'';tag=[].some.call(tb,function(b){return b.dataset.tagf===t0})?t0:'';press('[data-tagf]','tagf',tag);
var k0=g('ps-sort')||'name';cur=document.querySelector('[data-sort="'+k0+'"]')?k0:'name';press('[data-sort]','sort',cur);render();
var open=arr('ps-open');
orig.forEach(function(d){if(open.indexOf(d.id)>=0)d.open=true;
d.addEventListener('toggle',function(){var o=arr('ps-open').filter(function(i){return i!==d.id});if(d.open)o.push(d.id);s('ps-open',JSON.stringify(o))})});
})();"""


def esc(x):
    return html.escape(str(x if x is not None else ""), quote=True)


def helptag(key):
    return (f'<span class="help" tabindex="0" role="button" aria-label="Help">?'
            f'<span class="tip" role="tooltip">{esc(HELP[key])}</span></span>')


def _ink_for(hexc):
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#111111" if lum > 0.55 else "#FFFFFF"


def theme_css():
    t = settings().get("theme") or {}
    p = THEMES.get(t.get("preset"), THEMES["studio"])
    light, dark = dict(p["light"]), dict(p["dark"])
    acc = t.get("accent") or ""
    if re.fullmatch(r"#[0-9a-fA-F]{6}", acc):
        for d in (light, dark):
            d["accent"], d["accent_ink"] = acc, _ink_for(acc)
    v = lambda d: ";".join(f"--{k.replace('_', '-')}:{val}" for k, val in d.items())
    mode = t.get("mode", "auto")
    if mode == "light":
        return f":root{{{v(light)};color-scheme:light}}"
    if mode == "dark":
        return f":root{{{v(dark)};color-scheme:dark}}"
    return f":root{{{v(light)};color-scheme:light dark}}@media (prefers-color-scheme:dark){{:root{{{v(dark)}}}}}"


def shell(title, body, current="feeds", refresh=False):
    nav = (f'<nav><a href="/"{" aria-current=page" if current == "feeds" else ""}>Feeds</a>'
           f'<a href="/config"{" aria-current=page" if current == "config" else ""}>Config</a></nav>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{'<meta http-equiv="refresh" content="4">' if refresh else ''}<title>{esc(title)}</title>{FONTS}<style>{theme_css()}{CSS}</style></head>
<body><main><header class="top"><a class="brand" href="/"><span class="lamp" aria-hidden="true"></span>Podcast Solution</a>{nav}</header>
{body}</main><script>{JS}</script></body></html>"""


def when(iso):
    if not iso:
        return "never"
    d = datetime.fromisoformat(iso).astimezone(TZ)
    today = datetime.now(TZ).date()
    if d.date() == today:
        return d.strftime("today %-I:%M %p")
    if d.date() == today - timedelta(days=1):
        return d.strftime("yesterday %-I:%M %p")
    return d.strftime("%a %b %-d, %-I:%M %p")


def when_ts(ts):
    return when(datetime.fromtimestamp(ts, TZ).isoformat()) if ts else "never"


def days_text(days):
    days = sorted(days or [])
    if days == list(range(7)):
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "Mon–Fri"
    if days == [5, 6]:
        return "weekends"
    return ", ".join(DAYS[d] for d in days) or "no days"


def schedule_text(f):
    if f.get("drop") is not None and f.get("burst"):
        first = int(f["drop"]) + int(f["burst"][0])
        return (f"{days_text(f.get('days'))}, {f.get('start')}–{f.get('end')} hourly at :{first % 60:02d} "
                f"(drops ~:{int(f['drop']) % 60:02d})")
    if int(f.get("every") or 0) > 0:
        e = int(f["every"])
        often = "hourly" if e == 60 else (f"every {e // 60} h" if e % 60 == 0 else f"every {e} min")
        return f"{days_text(f.get('days'))}, {f.get('start')}–{f.get('end')} {often}"
    return f"{days_text(f.get('days'))} at {f.get('start')}"


def slots(f, all_burst=False):
    """Check times (minutes of day). Drop-timed feeds: the first check after each drop,
    or every burst check with all_burst."""
    if f.get("drop") is not None and f.get("burst"):
        if all_burst:
            return burst_times(f)
        return [t for h in drop_hours(f) if (t := h * 60 + int(f["drop"]) + int(f["burst"][0])) < 24 * 60]
    sh, sm = _hm(f.get("start"))
    s0 = sh * 60 + sm
    every = int(f.get("every") or 0)
    if every <= 0:
        return [s0]
    eh, em = _hm(f.get("end") or f.get("start"))
    return list(range(s0, eh * 60 + em + 1, every)) or [s0]


def checks_per_week(f):
    return len(f.get("days") or []) * len(slots(f)) if f.get("enabled", True) else 0


def newest_pub(f, fs):
    if f["kind"] == "episodic":
        pubs = [e["pub"] for e in (fs.get("eps") or {}).values() if e.get("status") in ("unplayed", "played")]
    else:
        pubs = [e.get("pub_full") or e.get("pub") for e in fs.get("episodes") or []]
    pubs = [p for p in pubs if p and len(p) > 10]
    return max(pubs) if pubs else ""


def strip_svg(f, fs):
    now = datetime.now(TZ)
    W, H = 240, 30
    x = lambda m: round(m / 1440 * W, 1)
    today = now.weekday() in (f.get("days") or [])
    parts = [f'<line class="grid" x1="0" y1="14" x2="{W}" y2="14"/>']
    parts += [f'<line class="grid" x1="{x(h * 60)}" y1="6" x2="{x(h * 60)}" y2="22"/>' for h in (6, 12, 18)]
    sl = slots(f)
    if len(sl) > 1:
        parts.append(f'<rect class="win" x="{x(sl[0])}" y="6" width="{max(2, x(sl[-1]) - x(sl[0]))}" height="16" rx="2"/>')
    parts += [f'<line class="tick" x1="{x(m)}" y1="8" x2="{x(m)}" y2="20"/>' for m in sl]
    if f.get("sched") == "auto":  # the daily noon safety check
        sh, sm = _hm(AUTO_SAFETY)
        parts.append(f'<line class="tick" stroke-dasharray="2 2" x1="{x(sh * 60 + sm)}" y1="8" x2="{x(sh * 60 + sm)}" y2="20"/>')
    p = newest_pub(f, fs)
    if p:
        d = datetime.fromisoformat(p).astimezone(TZ)
        parts.append(f'<circle class="pub" cx="{x(d.hour * 60 + d.minute)}" cy="14" r="3"/>')
    parts.append(f'<line class="now" x1="{x(now.hour * 60 + now.minute)}" y1="3" x2="{x(now.hour * 60 + now.minute)}" y2="25"/>')
    parts += [f'<text x="{x(h * 60)}" y="30" text-anchor="middle">{lbl}</text>'
              for h, lbl in ((6, "6a"), (12, "12p"), (18, "6p"))]
    label = f"Checks {schedule_text(f)}" + ("" if today else "; none today")
    return (f'<svg class="strip{"" if today and f.get("enabled", True) else " idle"}" viewBox="0 0 {W} {H}" '
            f'role="img" aria-label="{esc(label)}">{"".join(parts)}</svg>')


def play_cell(info):
    if not info:
        return '<span class="pl">Not in Plex yet</span>'
    if info.get("vc", 0) >= 1 and info.get("lv"):
        return f'<span class="pl done">Played {esc(when_ts(info["lv"]))}</span>'
    if info.get("off"):
        dur = max(1, info.get("dur") or 1)
        pct = min(100, round(info["off"] / dur * 100))
        return (f'<span class="pl">{info["off"] // 60} of {dur // 60} min</span>'
                f'<div class="bar" aria-hidden="true"><i style="width:{pct}%"></i></div>')
    return '<span class="pl">Not played</span>'


def take_text(f):
    if f["kind"] == "episodic":
        kept = f"played kept {f['played_days']} days" if f["played_days"] else "played kept"
        return f"Started with the newest {f['start_n']}. Keeps up to {f['max_unplayed']} unplayed, {kept}"
    n = int(f["count"])
    t = {"per_show": "The newest episode of each show", "latest_day": "Every episode from the latest day",
         "newest_n": "The newest episode" if n == 1 else f"The newest {n} episodes"}
    out = [t.get(f["take"], f["take"])]
    skips = []
    if f.get("skip_types"):
        skips.append(" and ".join(f["skip_types"]) + " episodes")
    words = [w.strip() for w in (f.get("skip_titles") or "").split(",") if w.strip()]
    if words:
        skips.append("titles with " + ", ".join(f"“{w}”" for w in words))
    if skips:
        out.append("Skips " + " and ".join(skips))
    if f.get("stamp", "none") != "none":
        out.append(STAMPS[f["stamp"]].split(" (")[0] + " at the front of titles")
    return ". ".join(out)


def feed_form(f=None, action=None, button=None):
    f = norm_feed(f or {"name": "", "url": ""})
    action = action or (f"/feeds/{f['id']}" if f.get("id") else "/feeds")
    sel = lambda a, b: " selected" if a == b else ""
    chk = lambda c: " checked" if c else ""
    days = "".join(f'<label><input type="checkbox" name="day" value="{i}"{chk(i in f["days"])}>{d}</label>'
                   for i, d in enumerate(DAYS))
    types = "".join(f'<label><input type="checkbox" name="skip_type" value="{t}"{chk(t in f["skip_types"])}>labelled {t}</label>'
                    for t in ("bonus", "trailer"))
    note = ("Changing the name makes a new Plex album; your Sonos favorite stays on the old one."
            if f.get("id") else "Leave blank to use the show's own title.")
    stamps = "".join(f'<option value="{k}"{sel(f["stamp"], k)}>{esc(v)}</option>' for k, v in STAMPS.items())
    return f"""<form method="post" action="{action}">
<div class="grid"><div><label for="n-{esc(f.get('id', 'new'))}">Name in Plex</label><input id="n-{esc(f.get('id', 'new'))}" type="text" name="name" value="{esc(f.get('name'))}"><div class="mute">{note}</div></div>
<div><label>RSS link</label><input type="url" name="url" required value="{esc(f.get('url'))}"></div>
<div><label>Tags {helptag('tags')}</label><input type="text" name="tags" value="{esc(', '.join(f.get('tags') or []))}" placeholder="news, local"></div></div>
<fieldset><legend>Kind {helptag('kind')}</legend><div class="grid">
<div><label>Keep</label><select name="kind" data-kind><option value="latest"{sel(f['kind'], 'latest')}>Latest only</option>
<option value="episodic"{sel(f['kind'], 'episodic')}>Episodic</option></select></div>
<div data-for="latest"><label>Takes {helptag('take')}</label><select name="take">
<option value="per_show"{sel(f['take'], 'per_show')}>Newest of each show</option>
<option value="newest_n"{sel(f['take'], 'newest_n')}>Newest N</option>
<option value="latest_day"{sel(f['take'], 'latest_day')}>Whole latest day</option></select></div>
<div data-for="latest"><label>N (for "Newest N")</label><input type="number" min="1" max="20" name="count" value="{esc(f['count'])}"></div>
<div data-for="latest"><label>Order</label><select name="order"><option value="newest"{sel(f['order'], 'newest')}>Newest first</option>
<option value="oldest"{sel(f['order'], 'oldest')}>Oldest first</option><option value="feed"{sel(f['order'], 'feed')}>As in the feed</option></select></div></div></fieldset>
<fieldset data-for="episodic"><legend>Episodic {helptag('episodic')}</legend><div class="grid">
<div><label>Start with the newest</label><input type="number" min="1" max="50" name="start_n" value="{esc(f['start_n'])}"></div>
<div><label>Most unplayed to keep</label><input type="number" min="1" max="200" name="max_unplayed" value="{esc(f['max_unplayed'])}"></div>
<div><label>Keep played for days (0 = always)</label><input type="number" min="0" max="3650" name="played_days" value="{esc(f['played_days'])}"></div></div></fieldset>
<fieldset><legend>Skip {helptag('skip')}</legend><div class="checks">{types}</div>
<label>Titles containing</label><input type="text" name="skip_titles" value="{esc(f['skip_titles'])}" placeholder="Bonus:"></fieldset>
<fieldset><legend>Title {helptag('title')}</legend><label>Stamp</label><select name="stamp">{stamps}</select>
<div class="checks" style="margin-top:10px"><label><input type="checkbox" name="tidy" value="1"{chk(f.get('tidy', True))}> Tidy titles</label></div></fieldset>
<fieldset><legend>Artwork {helptag('artwork')}</legend><div class="grid"><div><label>Use</label><select name="art_source">
<option value="show"{sel(f['art_source'], 'show')}>The show image on every episode</option>
<option value="episode"{sel(f['art_source'], 'episode')}>Each episode's own image</option></select></div>
<div><label>Or this image link</label><input type="url" name="art_url" value="{esc(f['art_url'])}"></div></div></fieldset>
<fieldset><legend>Schedule {helptag('schedule')}</legend>
<label>When to check</label><select name="sched" data-schedsel><option value="auto"{sel(f.get('sched'), 'auto')}>Auto: follow the show</option>
<option value="manual"{sel(f.get('sched'), 'manual')}>Manual: my days and times</option></select>
<div data-sched="auto" class="mute" style="margin-top:6px">Learned each day from when the show posted over the last 30 days, plus a check at noon every day.</div>
<div data-sched="manual"><div class="checks" style="margin-top:10px">{days}</div><div class="grid">
<div><label>Start</label><input type="time" name="start" value="{esc(f['start'])}"></div>
<div><label>End</label><input type="time" name="end" value="{esc(f.get('end', ''))}"></div>
<div><label>Every (minutes)</label><input type="number" min="0" max="720" name="every" value="{esc(f['every'])}"></div></div></div>
<div class="checks" style="margin-top:10px"><label><input type="checkbox" name="enabled" value="1"{chk(f.get('enabled', True))}> Checks on</label></div></fieldset>
<p><button class="go" type="submit">{button or ("Save changes" if f.get("id") else "Add feed")}</button></p></form>"""


def ep_list_latest(f, fs):
    plays = fs.get("plays") or {}
    files = fs.get("files") or []
    rows = []
    for i, e in enumerate(fs.get("episodes") or []):
        info = plays.get(files[i]) if i < len(files) else None
        rows.append(f'<li><span class="t">{esc(e["title"])}</span><span class="pcol">{play_cell(info)}</span>'
                    f'<span class="when">{esc((e.get("pub_full") or e.get("pub") or "")[:10])}</span></li>')
    return f'<ul class="eps">{"".join(rows)}</ul>' if rows else '<div class="mute">No episodes yet.</div>'


def ep_list_episodic(f, fs):
    eps, plays = fs.get("eps") or {}, fs.get("plays") or {}
    up = sorted([(k, e) for k, e in eps.items() if e["status"] == "unplayed"], key=lambda x: x[1]["pub"])
    pl = sorted([(k, e) for k, e in eps.items() if e["status"] == "played"],
                key=lambda x: x[1].get("played_at", ""), reverse=True)
    other = Counter(e["status"] for e in eps.values() if e["status"] not in ("unplayed", "played"))

    def row(k, e, label, to, cell):
        return (f'<li><span class="t">{esc(display_title(f, e, f["name"]))}</span><span class="pcol">{cell}</span>'
                f'<form method="post" action="/feeds/{f["id"]}/ep/{k}/{to}"><button class="small">{label}</button></form></li>')

    out = f"<h3>Up next ({len(up)})</h3>"
    out += (f'<ul class="eps">{"".join(row(k, e, "Mark played", "played", play_cell(plays.get(e.get("file")))) for k, e in up)}</ul>'
            if up else '<div class="mute">You\'re caught up.</div>')
    if pl:
        out += f"<h3>Played ({len(pl)})</h3>"
        out += ('<ul class="eps">' + "".join(
            row(k, e, "Mark unplayed", "unplayed", f'<span class="pl done">Played {esc(when(e.get("played_at")))}</span>')
            for k, e in pl[:20]) + "</ul>")
    if other:
        names = {"skipped": "older, not downloaded", "deleted": "deleted by you", "deleted_played": "played, deleted by you",
                 "expired": "played and cleared"}
        out += '<div class="mute" style="margin-top:8px">' + "; ".join(
            f"{n} {names.get(s_, s_)}" for s_, n in other.items()) + "</div>"
    return out


def feed_row(f, fs):
    running, err = fs.get("running"), fs.get("last_error")
    dot = "run" if running else ("err" if err else ("off" if not f.get("enabled", True) else ""))
    kind = "Episodic" if f["kind"] == "episodic" else "Latest only"
    status = (f'<p class="status bad">Last check failed: {esc(err)}</p>' if err
              else f'<p class="status ok">{esc(fs.get("last_result", "Not checked yet"))}</p>')
    eps = ep_list_episodic(f, fs) if f["kind"] == "episodic" else ep_list_latest(f, fs)
    name_js = esc(f["name"]).replace("'", "\\'")
    eff = effective(f, fs)
    sched = schedule_text(eff)
    learned = ""
    if f.get("sched") == "auto":
        a = fs.get("auto") or {}
        sched = ("Auto: learning, every 15 min 05:00–23:00 (seen "
                 f"{a.get('basis', 0)} of {LEARN_NEED})") if a.get("learning") else "Auto: " + sched
        learned = (f'<div class="mute" style="margin-bottom:6px">Schedule learned {esc(when(a.get("learned")))} from '
                   f'{a.get("basis", 0)} episodes in the last 30 days: {esc(a.get("why", ""))}. Plus a safety check every day at noon.</div>'
                   if a.get("learned") else '<div class="mute" style="margin-bottom:6px">Auto schedule not learned yet; it will be on the next check.</div>')
    tags = f.get("tags") or []
    chips = "".join(f'<span class="tag">{esc(t)}</span>' for t in tags)
    return f"""<details class="feed" id="f-{f['id']}" data-name="{esc(f['name'].lower())}" data-often="{checks_per_week(eff)}" data-updated="{esc(fs.get('updated', ''))}" data-tags="{esc('|'.join(t.lower() for t in tags))}">
<summary><span class="dot {dot}" aria-label="{'checking' if running else ('error' if err else 'ok')}"></span>
<span><span class="fname">{esc(f['name'])}</span><span class="kind">{kind}</span>{chips}
<div class="fmeta">{esc(sched)}{'' if f.get('enabled', True) else ', checks off'}. Checked {esc(when(fs.get('last_check')))}.</div></span>
<span class="stripcell">{strip_svg(eff, fs)}</span><span class="chev" aria-hidden="true"></span></summary>
<div class="fbody">{status}
<div class="actions">
<form method="post" action="/feeds/{f['id']}/check"><button class="go" title="Look for new episodes now">{'Checking…' if running else 'Check now'}</button></form>
<form method="post" action="/feeds/{f['id']}/restore"><button title="Download again anything you deleted">Restore</button></form>
<form method="get" action="/analyze"><input type="hidden" name="url" value="{esc(f['url'])}"><input type="hidden" name="id" value="{f['id']}"><button title="Read the feed again and suggest settings">Re-analyze</button></form></div>
<div class="mute" style="margin-bottom:6px">{esc(take_text(f))}. Folder <code>{esc(f['folder'])}</code>. Last change {esc(when(fs.get('updated')))}.</div>
{learned}{eps}
<details class="sub"><summary>Edit settings</summary>{feed_form(f)}
<div class="actions" style="margin-top:4px">
<form method="post" action="/feeds/{f['id']}/startover" onsubmit="return confirm('Start {name_js} over? Its files and history are cleared and it begins again as if newly added.')"><button class="danger" title="Clear files and history, then begin again">Start over</button></form>
<form method="post" action="/feeds/{f['id']}/delete" onsubmit="return confirm('Remove {name_js}? Its files stay in Plex unless you tick the box.')">
<label style="display:inline-flex;gap:5px;margin:0 8px 0 0;align-items:center"><input type="checkbox" name="files" value="1"> and its files</label><button class="danger">Remove feed</button></form></div></details>
</div></details>"""


def page():
    st, items = state(), feeds()
    rows = "".join(feed_row(f, st.get(f["id"], {})) for f in items)
    running = any(st.get(f["id"], {}).get("running") for f in items)
    hist = history()[:12]
    hrows = "".join(
        f'<li><span class="mute">{esc(when(h["at"]))}</span><span class="t">{esc(h["title"])} '
        f'<span class="mute">{esc(h["feed"])}, '
        f'{"finished" if h["what"] == "finished" else f"at {h.get("off", 0) // 60} of {h.get("dur", 0) // 60} min"}</span></span></li>'
        for h in hist)
    plex_on = bool(settings().get("plex_token"))
    all_tags = sorted({t for f in items for t in (f.get("tags") or [])}, key=str.lower)
    tagbar = ('<div class="tagbar" role="group" aria-label="Filter by tag"><button type="button" data-tagf="" aria-pressed="true">All</button>'
              + "".join(f'<button type="button" data-tagf="{esc(t.lower())}" aria-pressed="false">{esc(t)}</button>' for t in all_tags)
              + "</div>") if all_tags else ""
    body = f"""<section class="panel" aria-labelledby="addh"><div class="sechead"><h2 id="addh">Add a feed {helptag('add')}</h2></div>
<form method="get" action="/analyze" class="addbar"><input type="url" name="url" required placeholder="Paste an RSS link" aria-label="RSS link"><button class="go">Analyze</button></form>
<details class="sub"><summary>Add without analyzing</summary>{feed_form()}</details></section>
<section class="sec" aria-labelledby="feedsh"><div class="sechead"><h2 id="feedsh">Feeds {helptag('feeds')}</h2>
<div class="seg" role="group" aria-label="Sort feeds"><button type="button" data-sort="name">Name</button><button type="button" data-sort="often">How often</button><button type="button" data-sort="updated">Last updated</button><button type="button" data-sort="tag">Tag</button></div></div>
{tagbar}
<div id="feeds">{rows or '<div class="empty">No feeds yet. Paste an RSS link above to add your first.</div>'}</div></section>
<section class="sec" aria-labelledby="playh"><div class="sechead"><h2 id="playh">Recently played {helptag('played')}</h2></div>
{f'<ul class="hist">{hrows}</ul>' if hrows else ('<div class="empty">Nothing played yet.</div>' if plex_on else '<div class="empty">Add your Plex token on <a href="/config">Config</a> to see what you play.</div>')}</section>"""
    return shell("Podcast Solution", body, "feeds", refresh=running)


def config_page():
    s = settings()
    t = s.get("theme") or {}
    preset, mode, acc = t.get("preset", "studio"), t.get("mode", "auto"), t.get("accent", "")
    sw = "".join(
        f'<label class="swatch"><input type="radio" name="preset" value="{k}"{" checked" if k == preset else ""}>'
        f'<span class="chip" aria-hidden="true"><i style="background:{v["light"]["bg"]}"></i><i style="background:{v["light"]["accent"]}"></i>'
        f'<i style="background:{v["dark"]["bg"]}"></i><i style="background:{v["dark"]["accent"]}"></i></span>{esc(v["label"])}</label>'
        for k, v in THEMES.items())
    sel = lambda a, b: " selected" if a == b else ""
    plex_line = (f"Library {esc(s.get('plex_section_name'))} ({esc(s.get('plex_section'))}); Plex sees the folder as <code>{esc(s.get('plex_root'))}</code>"
                 if s.get("plex_section") else "Not found yet.")
    status = s.get("plex_status", "")
    last = _last_plex_sync
    size = lambda b: f"{b / 1e6:.0f} MB" if b >= 1e6 else f"{b / 1e3:.0f} KB"
    strays = stray_folders()
    srows = "".join(
        f'<tr><td><code>{esc(n)}</code></td><td>{c} episode{"s" * (c != 1)}, {size(b)}</td><td style="text-align:right">'
        f'<form method="post" action="/folders/delete" onsubmit="return confirm(&quot;Delete this folder and everything in it? This cannot be undone.&quot;)">'
        f'<input type="hidden" name="folder" value="{esc(n)}"><button class="danger small">Delete</button></form></td></tr>'
        for n, c, b in strays)
    body = f"""<h1>Config</h1>
<section class="sec"><div class="sechead"><h2>Save location {helptag('save')}</h2></div><div class="panel"><table class="kv">
<tr><td>On the NAS</td><td><code>{esc(MEDIA_HOST_PATH)}</code></td></tr>
<tr><td>Inside the app</td><td><code>{esc(MEDIA)}</code></td></tr>
<tr><td>Settings and history</td><td><code>{esc(DATA)}</code></td></tr></table></div></section>
<section class="sec" id="strays"><div class="sechead"><h2>Stray folders {helptag('strays')}</h2></div><div class="panel">
{f'<table class="kv">{srows}</table>' if srows else '<div class="mute">None. Every folder belongs to a feed.</div>'}</div></section>
<section class="sec"><div class="sechead"><h2>Plex {helptag('plex')}</h2>
<form method="post" action="/plex/sync"><button title="Read play counts and resume points from Plex now">Check Plex now</button></form></div><div class="panel">
<table class="kv"><tr><td>Token</td><td>{'Set' if s.get('plex_token') else 'Not set'}</td></tr>
<tr><td>Library</td><td>{plex_line}</td></tr>
<tr><td>Connection</td><td class="{'bad' if 'failed' in status else ('ok' if status.startswith('Plex OK') else 'mute')}">{esc(status or 'Not tested')}</td></tr>
<tr><td>Plays last read</td><td>{esc(when(last['at']) if last['at'] else 'not yet')}{(': ' + esc(last['result'])) if last['result'] else ''}</td></tr></table>
<form method="post" action="/settings"><div class="grid"><div><label>Plex token (blank keeps the current one)</label><input type="password" name="plex_token" autocomplete="off"></div>
<div><label>Plex address{(' (blank = ' + esc(PLEX_URL) + ')') if PLEX_URL else ', for example http://192.168.1.10:32400'}</label><input type="text" name="plex_url" value="{esc(s.get('plex_url'))}"></div></div>
<div class="checks" style="margin-top:10px"><label><input type="checkbox" name="clear_token" value="1"> Clear the token</label></div>
<p><button class="go">Save and test</button></p></form></div></section>
<section class="sec"><div class="sechead"><h2>Appearance {helptag('appearance')}</h2></div><div class="panel">
<form method="post" action="/settings/theme"><div class="swatches">{sw}</div>
<div class="grid" style="margin-top:6px"><div><label>Light or dark</label><select name="mode"><option value="auto"{sel(mode, 'auto')}>Auto (follow device)</option>
<option value="light"{sel(mode, 'light')}>Light</option><option value="dark"{sel(mode, 'dark')}>Dark</option></select></div>
<div><label>Accent colour</label><div style="display:flex;gap:10px;align-items:center"><input type="color" name="accent" value="{esc(acc or THEMES.get(preset, THEMES['studio'])['light']['accent'])}">
<label style="margin:0;display:inline-flex;gap:5px;align-items:center;color:var(--ink)"><input type="checkbox" name="custom_accent" value="1"{' checked' if acc else ''}> Use my colour</label></div></div></div>
<p><button class="go">Save appearance</button></p></form></div></section>
<section class="sec"><div class="sechead"><h2>Log {helptag('log')}</h2></div><div class="panel"><pre>{esc(chr(10).join(LOG)) or 'Empty.'}</pre></div></section>
<p class="mute" style="margin-top:24px">Podcast Solution {VERSION}</p>"""
    return shell("Config · Podcast Solution", body, "config")


def analyze_page(url, feed_id=None):
    try:
        sug, rows, warn, preview = analyze(url)
    except Exception as e:
        return shell("Analyze", f"<h1>Couldn't read that feed</h1><p class='bad'>{esc(e)}</p><p>Check the link is an RSS feed, then <a href='/'>try again</a>.</p>")
    existing = next((f for f in feeds() if f["id"] == feed_id), None) if feed_id else None
    if existing:  # keep identity, take the suggestions
        sug = dict(existing, **{k: v for k, v in sug.items() if k not in ("name", "url", "id", "folder", "tags")})
    rep = "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td></tr>" for a, b in rows)
    w = "".join(f"<p class='warn'>{esc(x)}</p>" for x in warn)
    prev = "".join(f'<li><span class="t">{esc(display_title(sug, e, sug["name"]))}</span><span class="pcol mute">'
                   f'{str(round(e["dur"] / 60)) + " min" if e["dur"] else ""}</span><span class="when">{esc(e["pub"][:10])}</span></li>'
                   for e in preview)
    action = f"/feeds/{existing['id']}" if existing else "/feeds"
    return shell("Analyze · Podcast Solution", f"""<p class="mute"><a href="/">Feeds</a></p><h1>{esc(sug['name'])}</h1>
<p class="mute"><code>{esc(url)}</code></p>
<section class="sec"><div class="sechead"><h2>What's in the feed</h2></div><div class="panel"><table class="kv">{rep}</table>{w}</div></section>
<section class="sec"><div class="sechead"><h2>The first check would take</h2></div><ul class="eps">{prev or '<li class="mute">Nothing, with these settings.</li>'}</ul></section>
<section class="sec"><div class="sechead"><h2>{'New settings for ' + esc(existing['name']) if existing else 'Suggested settings'}</h2></div>
<div class="panel">{feed_form(sug, action, 'Save changes' if existing else 'Add feed')}</div></section>""")


def form_to_feed(form, existing=None):
    g = lambda k, d="": (form.get(k, [d])[0] or d).strip()
    f = dict(existing or {"id": uuid.uuid4().hex[:10]})
    url = g("url")
    if not url.startswith(("http://", "https://")):
        raise ValueError("Enter the feed's full link, starting with http:// or https://.")
    name = g("name")
    if not name:  # blank: use the show's own title
        try:
            name = read_feed(url)[0]["title"]
        except Exception as e:
            raise ValueError(f"Couldn't read the feed to get its title ({e}). Type a name instead.")
    if not existing or existing["name"] != name:
        f["folder"] = folder_name(name)
    f["name"], f["url"] = name, url
    f["kind"] = g("kind", "latest") if g("kind") in ("latest", "episodic") else "latest"
    f["take"] = g("take", "per_show")
    f["count"] = max(1, min(20, int(g("count", "1") or 1)))
    f["order"] = g("order", "newest")
    f["start_n"] = max(1, min(50, int(g("start_n", "1") or 1)))
    f["max_unplayed"] = max(1, min(200, int(g("max_unplayed", "20") or 20)))
    f["played_days"] = max(0, int(g("played_days", "30") or 0))
    f["skip_types"] = [t for t in form.get("skip_type", []) if t in ("bonus", "trailer")]
    f["skip_titles"] = g("skip_titles")
    f["stamp"] = g("stamp", "none") if g("stamp") in STAMPS else "none"
    f["tidy"] = "tidy" in form
    f["art_source"] = "episode" if g("art_source") == "episode" else "show"
    f["art_url"] = g("art_url")
    f["days"] = sorted(int(d) for d in form.get("day", []) if d.isdigit() and int(d) < 7)
    f["start"] = g("start", "04:00")
    f["end"] = g("end") or f["start"]
    f["every"] = max(0, int(g("every", "0") or 0))
    f["sched"] = "auto" if g("sched") == "auto" else "manual"
    seen, tags = set(), []
    for t in re.split(r"[,;]", form.get("tags", [""])[0]):
        t = re.sub(r"\s+", " ", t).strip()[:30]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    f["tags"] = tags[:12]
    f["enabled"] = "enabled" in form
    f.pop("mode", None)
    return f


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _go(self, to="/"):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            return self._send(200, page())
        if u.path == "/config":
            return self._send(200, config_page())
        if u.path == "/analyze":
            return self._send(200, analyze_page(q.get("url", [""])[0].strip(), q.get("id", [None])[0]))
        if u.path == "/api/status":
            st = state()
            out = []
            for f in feeds():
                fs = {k: v for k, v in st.get(f["id"], {}).items() if k not in ("guids", "eps", "plays")}
                if f["kind"] == "episodic":
                    eps = st.get(f["id"], {}).get("eps") or {}
                    fs["counts"] = dict(Counter(e["status"] for e in eps.values()))
                out.append({"name": f["name"], "folder": f["folder"], "kind": f["kind"], **fs})
            return self._send(200, json.dumps(out, indent=2), "application/json")
        if u.path == "/api/plays":
            return self._send(200, json.dumps(history()[:100], indent=2), "application/json")
        self._send(404, "Not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
        parts = self.path.strip("/").split("/")
        bg = lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True).start()
        back = lambda fid: self._go(f"/#f-{fid}")
        try:
            if parts == ["feeds"]:
                items = [dict(x) for x in _load(FEEDS_FILE, [])]
                new = form_to_feed(form)
                if any(x.get("folder") == new["folder"] for x in items):
                    raise ValueError(f"There's already a feed called {new['name']}. Choose another name.")
                items.append(new)
                save_feeds(items)
                log.info("added feed %s", new["name"])
                bg(run_check, new["id"], "added")
                time.sleep(0.5)
                return back(new["id"])
            fid = parts[1] if len(parts) >= 2 and parts[0] == "feeds" else None
            feed = next((f for f in feeds() if f["id"] == fid), None) if fid else None
            if fid and feed is None:
                raise ValueError("That feed no longer exists.")
            if len(parts) == 2 and feed:
                items = [dict(x) for x in _load(FEEDS_FILE, [])]
                renamed = False
                for i, x in enumerate(items):
                    if x["id"] == fid:
                        old = norm_feed(x)
                        new = form_to_feed(form, old)
                        if new["folder"] != old["folder"]:
                            if any(y["id"] != fid and y.get("folder") == new["folder"] for y in items):
                                raise ValueError(f"There's already a feed called {new['name']}. Choose another name.")
                            rename_feed(old, new)  # moves folders first; raises before saving if it can't
                            renamed = True
                        items[i] = new
                save_feeds(items)
                saved = next(f for f in feeds() if f["id"] == fid)
                if saved.get("sched") == "auto":
                    bg(relearn, saved, True)
                    time.sleep(1.5)
                if renamed:
                    bg(run_check, fid, "renamed")  # re-tags the moved files with the new album name
                    time.sleep(0.5)
                return back(fid)
            if len(parts) == 3 and feed and parts[2] == "check":
                bg(run_check, fid, "check now")
                time.sleep(0.5)
                return back(fid)
            if len(parts) == 3 and feed and parts[2] == "restore":
                bg(run_check, fid, "restore", True)
                time.sleep(0.5)
                return back(fid)
            if len(parts) == 3 and feed and parts[2] == "startover":
                bg(start_over, feed)
                time.sleep(0.5)
                return back(fid)
            if len(parts) == 5 and feed and parts[2] == "ep" and parts[4] in ("played", "unplayed"):
                mark(feed, parts[3], parts[4] == "played")
                return back(fid)
            if len(parts) == 3 and feed and parts[2] == "delete":
                save_feeds([x for x in _load(FEEDS_FILE, []) if x["id"] != fid])
                if "files" in form:
                    for fold in (feed["folder"], played_folder(feed["folder"])):
                        shutil.rmtree(os.path.join(MEDIA, fold), ignore_errors=True)
                    plex_nudge(feed["folder"])
                log.info("removed feed %s%s", feed["name"], " and its files" if "files" in form else "")
                return self._go("/")
            if parts == ["api", "check"]:  # for HA later: POST /api/check  name=<feed name>
                name = form.get("name", [""])[0]
                f = next((x for x in feeds() if x["name"].lower() == name.lower()), None)
                if not f:
                    return self._send(404, json.dumps({"error": "no such feed"}), "application/json")
                bg(run_check, f["id"], "api")
                return self._send(202, json.dumps({"started": f["name"]}), "application/json")
            if parts == ["folders", "delete"]:
                name = form.get("folder", [""])[0]
                if name not in {s_[0] for s_ in stray_folders()}:
                    raise ValueError("That folder belongs to a feed or no longer exists, so it wasn't deleted.")
                shutil.rmtree(os.path.join(MEDIA, name))
                plex_nudge(name)
                log.info("deleted stray folder %s", name)
                return self._go("/config#strays")
            if parts == ["plex", "sync"]:
                sync_plex()
                return self._go("/config")
            if parts == ["settings", "theme"]:
                s = settings()
                preset = form.get("preset", ["studio"])[0]
                acc = form.get("accent", [""])[0].strip()
                s["theme"] = {"preset": preset if preset in THEMES else "studio",
                              "mode": form.get("mode", ["auto"])[0] if form.get("mode", ["auto"])[0] in ("auto", "light", "dark") else "auto",
                              "accent": acc if "custom_accent" in form and re.fullmatch(r"#[0-9a-fA-F]{6}", acc) else ""}
                save_settings(s)
                return self._go("/config")
            if parts == ["settings"]:
                s = settings()
                s["plex_url"] = form.get("plex_url", [""])[0].strip()
                if form.get("plex_token", [""])[0].strip():
                    s["plex_token"] = form["plex_token"][0].strip()
                if "clear_token" in form:
                    s["plex_token"] = ""
                s["plex_section"] = s["plex_section_name"] = s["plex_root"] = ""
                save_settings(s)
                result = plex_test()
                s = settings()
                s["plex_status"] = result
                save_settings(s)
                log.info("%s", result)
                return self._go("/config")
        except ValueError as e:
            return self._send(400, shell("Problem", f"<h1>That didn't work</h1><p class='bad'>{esc(e)}</p><p><a href='/'>Back to feeds</a></p>"))
        self._send(404, "Not found", "text/plain")


def _stop(signum, _frame):
    # Docker sends SIGTERM on stop. As PID 1 Python would otherwise ignore it and be
    # force-killed after 10 s (exit 137). A .part file mid-download is harmless:
    # the next check deletes it and finished files are never touched.
    log.info("stopping (signal %d)", signum)
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    os.makedirs(DATA, exist_ok=True)
    for f in feeds():  # clear stale "running" flags from a restart
        update_state(f["id"], running=False)
    threading.Thread(target=scheduler, daemon=True).start()
    log.info("Podcast Solution %s listening on :%d, media %s", VERSION, PORT, MEDIA_HOST_PATH)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
