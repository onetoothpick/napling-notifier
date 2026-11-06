#!/usr/bin/env python3
"""
Discord notifier for one Bluesky account and one YouTube channel.

- Bluesky: new top-level posts only (no replies, reposts, or quote posts)
- YouTube: new uploads, scheduled streams (optional), streams going live, and
  reschedules, cancellations, title changes and endings of announced streams
  (optionally including members-only streams)
- Pings an optional role per source via a plain Discord webhook

Each source is enabled only if its env vars are set. See .env.example.
"""
import logging
import os
import random
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")

BSKY_ACTOR = os.environ.get("BSKY_ACTOR")  # handle or DID
BSKY_ROLE_ID = os.environ.get("BSKY_ROLE_ID") or None
BSKY_POLL = int(os.environ.get("BSKY_POLL_SECONDS") or "60")

YT_KEY = os.environ.get("YOUTUBE_API_KEY")
YT_CHANNEL_ID = os.environ.get("YT_CHANNEL_ID")  # starts with UC
YT_ROLE_ID = os.environ.get("YT_ROLE_ID") or None
YT_POLL = int(os.environ.get("YT_POLL_SECONDS") or "120")
YT_NOTIFY_SCHEDULED = os.environ.get("YT_NOTIFY_SCHEDULED", "false").lower() in ("1", "true", "yes")
# Also watch the channel's members-only live streams (needs one extra API call per poll)
YT_INCLUDE_MEMBERS = os.environ.get("YT_INCLUDE_MEMBERS", "false").lower() in ("1", "true", "yes")
# Reschedules smaller than this edit the original message quietly; larger ones
# also post a new (pinging) message.
YT_RESCHEDULE_THRESHOLD = int(os.environ.get("YT_RESCHEDULE_THRESHOLD_MINUTES") or "60") * 60

DB_PATH = os.environ.get("DB_PATH", "state.db")
# Log one "still running" line per source this often (0 disables)
HEARTBEAT = int(os.environ.get("HEARTBEAT_MINUTES") or "60") * 60

BSKY_API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
YT_API = "https://www.googleapis.com/youtube/v3"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("notifier")


def redact(text):
    """Strip secrets from text before logging. HTTP error messages include the
    full request URL, which contains the YouTube API key or the webhook token."""
    text = str(text)
    for secret in (YT_KEY, WEBHOOK):
        if secret:
            text = text.replace(secret, "***")
    return text


class RedactFilter(logging.Filter):
    """Safety net: scrub secrets from every log record, including ones emitted by libraries."""

    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(RedactFilter())

# httpx logs every request URL at INFO, and our URLs carry the API key / webhook token.
# We log meaningful events ourselves, so keep the libraries quiet.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


http = httpx.Client(timeout=20, headers={"User-Agent": "discord-notifier/1.0"})

# --------------------------------------------------------------------------
# State (SQLite, shared across threads behind a lock)
# --------------------------------------------------------------------------
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_lock = threading.Lock()
with _lock:
    _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS seen (
            source TEXT NOT NULL, item_id TEXT NOT NULL,
            PRIMARY KEY (source, item_id));
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS yt_tracked (
            video_id TEXT PRIMARY KEY, state TEXT NOT NULL,
            scheduled_at TEXT, pending_at TEXT, message_id TEXT,
            title TEXT, missing_polls INTEGER DEFAULT 0, pending_title TEXT,
            members INTEGER DEFAULT 0);
        """
    )
    # Upgrade databases created by earlier versions of this script
    _cols = {r[1] for r in _db.execute("PRAGMA table_info(yt_tracked)")}
    for _col, _type in (
        ("scheduled_at", "TEXT"),
        ("pending_at", "TEXT"),
        ("message_id", "TEXT"),
        ("title", "TEXT"),
        ("missing_polls", "INTEGER DEFAULT 0"),
        ("pending_title", "TEXT"),
        ("members", "INTEGER DEFAULT 0"),
    ):
        if _col not in _cols:
            _db.execute(f"ALTER TABLE yt_tracked ADD COLUMN {_col} {_type}")
    _db.commit()


def db(sql, args=()):
    with _lock:
        cur = _db.execute(sql, args)
        rows = cur.fetchall()
        _db.commit()
        return rows


def is_seen(source, item_id):
    return bool(db("SELECT 1 FROM seen WHERE source=? AND item_id=?", (source, item_id)))


def mark_seen(source, item_id):
    db("INSERT OR IGNORE INTO seen VALUES (?, ?)", (source, item_id))


def meta_get(key):
    rows = db("SELECT value FROM meta WHERE key=?", (key,))
    return rows[0][0] if rows else None


def meta_set(key, value="1"):
    db("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------
def _discord_request(method, url, payload, params=None):
    """Send a webhook request, sleeping and retrying on Discord 429s."""
    for _ in range(5):
        r = http.request(method, url, json=payload, params=params)
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2.0
            log.warning("Discord rate limited, sleeping %.1fs", wait)
            time.sleep(wait + 0.5)
            continue
        return r
    raise RuntimeError("Discord rate limit: gave up after 5 attempts")


def _content(role_id, text):
    return f"<@&{role_id}> {text}".strip() if role_id else text


def send_discord(embed=None, role_id=None, text=""):
    """Post a message and return its Discord message ID. The role ping goes in
    `content` (pings in embeds don't notify), and allowed_mentions whitelists
    only that role so nothing in a post/video title (@everyone, other roles)
    can ping anyone. A bare URL in `content` is unfurled by Discord itself."""
    payload = {
        "allowed_mentions": {"parse": [], "roles": [role_id] if role_id else []},
    }
    if embed:
        payload["embeds"] = [embed]
    content = _content(role_id, text)
    if content:
        payload["content"] = content

    r = _discord_request("POST", WEBHOOK, payload, params={"wait": "true"})
    r.raise_for_status()
    return r.json().get("id")


def edit_discord(message_id, embed, role_id, text):
    """Edit a message this webhook previously posted. Nothing is whitelisted
    in allowed_mentions, so an edit never pings. Returns False if the message
    no longer exists (e.g. someone deleted it)."""
    payload = {
        "content": _content(role_id, text),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    r = _discord_request("PATCH", f"{WEBHOOK}/messages/{message_id}", payload)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


# --------------------------------------------------------------------------
# Bluesky
# --------------------------------------------------------------------------
def bsky_poll():
    r = http.get(
        BSKY_API,
        params={"actor": BSKY_ACTOR, "filter": "posts_no_replies", "limit": 30},
    )
    r.raise_for_status()

    posts = []
    for item in r.json().get("feed", []):
        # Skip reposts. (Pinned posts also carry a `reason`, but of a
        # different type, so only skip actual reposts.)
        reason = item.get("reason") or {}
        if reason.get("$type", "").endswith("reasonRepost"):
            continue
        post = item["post"]
        record = post.get("record", {})
        if record.get("reply"):
            continue
        # Skip quote posts (record and recordWithMedia embeds)
        if (record.get("embed") or {}).get("$type", "").startswith("app.bsky.embed.record"):
            continue
        posts.append(post)

    posts.reverse()  # oldest first

    if not meta_get("bsky_seeded"):
        for p in posts:
            mark_seen("bsky", p["uri"])
        meta_set("bsky_seeded")
        log.info("Bluesky: seeded %d existing posts (no notifications sent)", len(posts))
        return

    for post in posts:
        uri = post["uri"]
        if is_seen("bsky", uri):
            continue
        author = post["author"]
        name = author.get("displayName") or author["handle"]
        url = f"https://bsky.app/profile/{author['handle']}/post/{uri.rsplit('/', 1)[-1]}"

        # Plain link in the message body: Discord unfurls it into its own
        # embed, which keeps links, images and formatting from the post.
        send_discord(None, BSKY_ROLE_ID, f"{name} just posted on Bluesky!\n{url}")
        mark_seen("bsky", uri)  # commit only after a successful send
        log.info("Bluesky: notified %s", url)


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------
def yt_get(path, **params):
    r = http.get(f"{YT_API}/{path}", params={**params, "key": YT_KEY})
    r.raise_for_status()
    return r.json()


def _parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _discord_time(s):
    return f"<t:{int(_parse_ts(s).timestamp())}:F>"  # renders in each viewer's timezone


def _scheduled_start(video):
    return (video.get("liveStreamingDetails") or {}).get("scheduledStartTime")


def _stream_duration(video):
    """'2h 14m' / '45m' from the stream's actual start and end, or None if unknown."""
    det = video.get("liveStreamingDetails") or {}
    try:
        secs = (_parse_ts(det["actualEndTime"]) - _parse_ts(det["actualStartTime"])).total_seconds()
    except (KeyError, ValueError):
        return None
    hours, minutes = divmod(int(secs) // 60, 60)
    if not hours and not minutes:
        return None
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _is_members(video_id):
    rows = db("SELECT members FROM yt_tracked WHERE video_id=?", (video_id,))
    return bool(rows and rows[0][0])


def yt_message(video, kind, old_start=None, members=None):
    """Build (embed, text) for kind: video | live | ended | scheduled | rescheduled.
    `members` marks it members-only; if None, it's looked up from the tracked row."""
    if members is None:
        members = _is_members(video["id"])
    sn = video["snippet"]
    thumbs = sn.get("thumbnails", {})
    thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
    channel = sn.get("channelTitle", "The channel")
    start = _scheduled_start(video)

    labels = {
        "video": "New video",
        "live": "🔴 Live now",
        "scheduled": "📅 Stream scheduled",
        "rescheduled": "🕒 Stream rescheduled",
        "ended": "⚫ Stream ended",
    }
    if kind == "video":
        text = f"{channel} uploaded a new video!"
    elif kind == "live":
        text = f"{channel} is live now!"
    elif kind == "ended":
        text = f"{channel} was live."
    elif kind == "scheduled":
        text = f"{channel} scheduled a stream" + (f" for {_discord_time(start)}." if start else ".")
    else:
        text = f"{channel} rescheduled their stream to {_discord_time(start)}"
        text += f" (was {_discord_time(old_start)})." if old_start else "."

    embed = {
        "author": {"name": channel},
        "title": sn["title"][:256],
        "url": f"https://www.youtube.com/watch?v={video['id']}",
        "description": labels[kind],
        "color": 0x808080 if kind == "ended" else 0xFF0000,
    }
    if kind == "ended" and _stream_duration(video):
        embed["description"] += f" · streamed for {_stream_duration(video)}"
    if thumb:
        embed["image"] = {"url": thumb}
    if members:
        text = f"🔒 Members Only: {text}"
    return embed, text


def yt_notify(video, kind, old_start=None, members=None):
    """Send a YouTube notification (pinging YT_ROLE_ID) and return its message ID."""
    embed, text = yt_message(video, kind, old_start, members)
    msg_id = send_discord(embed, YT_ROLE_ID, text)
    log.info("YouTube: notified (%s) %s", kind, embed["url"])
    return msg_id


def yt_check_reschedule(video, row):
    """Compare an upcoming stream's current start time with the one we announced.

    - The new time must hold for two consecutive polls before we act, so a
      creator fiddling with the time several times doesn't cause noise.
    - Always: quietly edit the original announcement so it never shows a stale
      time (edits don't ping).
    - If the shift is at least YT_RESCHEDULE_THRESHOLD, also post a new
      pinging "rescheduled" message.

    Returns True if it acted (so the caller can skip other edits this poll).
    """
    vid, _state, announced, pending, msg_id = row[:5]
    observed = _scheduled_start(video)

    if not observed:
        return False
    if announced is None:  # tracked before reschedule support existed: just baseline it
        db("UPDATE yt_tracked SET scheduled_at=? WHERE video_id=?", (observed, vid))
        return False
    if observed == announced:
        if pending:  # time went back to what we announced
            db("UPDATE yt_tracked SET pending_at=NULL WHERE video_id=?", (vid,))
        return False
    if observed != pending:  # first sighting of the new time: wait one more poll
        db("UPDATE yt_tracked SET pending_at=? WHERE video_id=?", (observed, vid))
        return False

    # The new time has held steady: act on it
    if YT_NOTIFY_SCHEDULED:
        if msg_id:
            embed, text = yt_message(video, "scheduled")
            if not edit_discord(msg_id, embed, YT_ROLE_ID, text + " *(rescheduled)*"):
                log.warning("YouTube: original message for %s is gone, can't edit", vid)
        shift = abs((_parse_ts(observed) - _parse_ts(announced)).total_seconds())
        if shift >= YT_RESCHEDULE_THRESHOLD:
            yt_notify(video, "rescheduled", old_start=announced)
        else:
            log.info("YouTube: small reschedule for %s, edited quietly", vid)

    # The edit above rebuilt the whole embed, so the shown title is current too
    db(
        "UPDATE yt_tracked SET scheduled_at=?, pending_at=NULL, title=?, pending_title=NULL"
        " WHERE video_id=?",
        (observed, video["snippet"].get("title"), vid),
    )
    return True


def yt_check_title(video, row, kind):
    """Keep an announcement's title (and thumbnail) current when the creator changes it.

    kind is "live" (the "live now" message) or "scheduled" (the announcement of
    an upcoming stream). `title` in the database is the title currently shown in
    our Discord message. A new title must hold for two consecutive polls before
    we act. We then rebuild the whole embed and edit the message in place; edits
    never ping, so nobody is re-notified.

    Thumbnail-only changes aren't detected (the thumbnail URL doesn't change,
    and Discord caches embed images by URL); a thumbnail refreshes whenever the
    title changes.
    """
    vid = row[0]
    msg_id, shown, pending = row[4], row[5], row[7]
    observed = video["snippet"].get("title")

    if shown is None:  # row from an older version: just baseline it
        db("UPDATE yt_tracked SET title=? WHERE video_id=?", (observed, vid))
        return
    if observed == shown:
        if pending:  # title went back to what we showed
            db("UPDATE yt_tracked SET pending_title=NULL WHERE video_id=?", (vid,))
        return
    if observed != pending:  # first sighting of the new title: wait one more poll
        db("UPDATE yt_tracked SET pending_title=? WHERE video_id=?", (observed, vid))
        return

    # The new title has held steady: act on it
    if msg_id and (kind == "live" or YT_NOTIFY_SCHEDULED):
        embed, text = yt_message(video, kind)
        if edit_discord(msg_id, embed, YT_ROLE_ID, text):
            log.info("YouTube: updated %s message for %s (title changed)", kind, vid)
        else:
            log.warning("YouTube: message for %s is gone, can't edit", vid)
    db("UPDATE yt_tracked SET title=?, pending_title=NULL WHERE video_id=?", (observed, vid))


def yt_handle_ended(video, row):
    """A live stream we announced has ended: quietly edit the "live now" message
    so it no longer claims the stream is live. No new message, no ping."""
    vid, msg_id = row[0], row[4]
    if not msg_id:  # not announced by us (e.g. already live when the bot first started)
        return
    embed, text = yt_message(video, "ended")
    if edit_discord(msg_id, embed, None, text):
        log.info("YouTube: marked stream %s as ended", vid)
    else:
        log.warning("YouTube: message for %s is gone, can't mark it ended", vid)


def yt_handle_cancelled(row):
    """A scheduled stream we announced has disappeared (deleted or made private).

    - Quietly edit the original announcement: struck through, marked cancelled.
    - Post a short follow-up message that does NOT ping the role. Cancellation
      isn't urgent enough to buzz everyone's phone, but people who planned
      around the stream should still see it.
    """
    vid, _state, announced, _pending, msg_id, title, _missing = row[:7]
    channel = meta_get("yt_channel_name") or "The channel"
    when = f" for {_discord_time(announced)}" if announced else ""

    embed = {
        "author": {"name": channel},
        "title": (title or "Scheduled stream")[:256],
        "description": "❌ Stream cancelled",
        "color": 0x808080,
    }
    text = f"~~{channel} scheduled a stream{when}.~~ **Cancelled.**"
    if row[8]:
        text = "🔒 Members Only: " + text
    if not edit_discord(msg_id, embed, None, text):
        log.warning("YouTube: original message for %s is gone, can't edit", vid)

    what = f"scheduled stream **{title[:100]}**" if title else "scheduled stream"
    send_discord(None, None, f"❌ {channel}'s {what} was cancelled.")
    log.info("YouTube: notified (cancelled) %s", vid)


QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}
_members_retry_at = 0.0  # monotonic time before which we don't ask for the members playlist


def _error_reason(response):
    try:
        return response.json()["error"]["errors"][0]["reason"]
    except Exception:
        return None


def yt_members_ids():
    """Video IDs from the channel's members-only live streams playlist ("UUMV...").

    This is an undocumented playlist, so it must never break the main poll:
    returns None if it can't be read right now. A "not found"/"forbidden" answer
    (e.g. the channel has no memberships) is retried only once an hour, so it
    doesn't waste a quota unit on every poll.
    """
    global _members_retry_at
    if time.monotonic() < _members_retry_at:
        return None
    try:
        data = yt_get(
            "playlistItems",
            part="contentDetails",
            playlistId="UUMV" + YT_CHANNEL_ID[2:],
            maxResults=10,
        )
    except httpx.HTTPStatusError as e:
        status, reason = e.response.status_code, _error_reason(e.response)
        if status in (403, 404) and reason not in QUOTA_REASONS:
            _members_retry_at = time.monotonic() + 3600
            log.info(
                "YouTube: members-only playlist unavailable (HTTP %s, %s); checking again in an hour",
                status,
                reason,
            )
        else:
            log.warning("YouTube: members-only playlist check failed: %s", redact(e))
        return None
    except Exception as e:
        log.warning("YouTube: members-only playlist check failed: %s", redact(e))
        return None
    return [i["contentDetails"]["videoId"] for i in data.get("items", [])]


def yt_poll():
    uploads = "UU" + YT_CHANNEL_ID[2:]
    pl = yt_get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=10)
    playlist_ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]

    # Optional members-only streams. Seeded separately from the main playlist, so
    # turning the feature on later doesn't announce the existing backlog.
    member_ids = yt_members_ids() if YT_INCLUDE_MEMBERS else None
    members_seeding = member_ids is not None and not meta_get("yt_members_seeded")
    members_set = set(member_ids or [])

    seeding = not meta_get("yt_seeded")
    seed_ids = set(playlist_ids) if seeding else set()
    if members_seeding:
        seed_ids |= members_set

    candidates = list(dict.fromkeys(playlist_ids + (member_ids or [])))
    new_ids = [v for v in candidates if not is_seen("yt", v)]
    tracked = {
        r[0]: r
        for r in db(
            "SELECT video_id, state, scheduled_at, pending_at, message_id, title, missing_polls,"
            " pending_title, members"
            " FROM yt_tracked"
        )
    }

    # One videos.list call (1 quota unit) covers new IDs and tracked IDs
    ids = list(dict.fromkeys(new_ids + list(tracked)))
    videos = {}
    if ids:
        data = yt_get("videos", part="snippet,liveStreamingDetails", id=",".join(ids))
        videos = {v["id"]: v for v in data.get("items", [])}

    # Remember the channel's display name (used when a video is gone and can't be looked up)
    if videos:
        name = next(iter(videos.values()))["snippet"].get("channelTitle")
        if name and name != meta_get("yt_channel_name"):
            meta_set("yt_channel_name", name)

    # Tracked videos that vanished (deleted/private). For an announced upcoming
    # stream that means "cancelled", but require it to stay missing for two
    # consecutive polls so an API hiccup can't trigger a false cancellation.
    for vid, row in tracked.items():
        if vid in videos:
            continue
        announced = YT_NOTIFY_SCHEDULED and row[1] == "upcoming" and row[4]
        if not announced:
            db("DELETE FROM yt_tracked WHERE video_id=?", (vid,))
        elif (row[6] or 0) + 1 >= 2:
            yt_handle_cancelled(row)
            db("DELETE FROM yt_tracked WHERE video_id=?", (vid,))
        else:
            db("UPDATE yt_tracked SET missing_polls=missing_polls+1 WHERE video_id=?", (vid,))

    # Newly discovered videos, oldest first
    for vid in reversed(new_ids):
        v = videos.get(vid)
        if not v:
            continue
        quiet = vid in seed_ids  # first-run backlog: record, don't announce
        members = vid in members_set
        state = v["snippet"].get("liveBroadcastContent", "none")
        if state == "none":
            # A members-only stream that already ended isn't worth announcing
            if not quiet and not members:
                yt_notify(v, "video", members=False)
        elif state == "live":
            msg_id = None
            if not quiet:
                msg_id = yt_notify(v, "live", members=members)
            db(
                "INSERT OR REPLACE INTO yt_tracked (video_id, state, message_id, title, members)"
                " VALUES (?, 'live', ?, ?, ?)",
                (vid, msg_id, v["snippet"].get("title"), int(members)),
            )
        elif state == "upcoming":
            msg_id = None
            if not quiet and YT_NOTIFY_SCHEDULED:
                msg_id = yt_notify(v, "scheduled", members=members)
            db(
                "INSERT OR REPLACE INTO yt_tracked"
                " (video_id, state, scheduled_at, message_id, title, members)"
                " VALUES (?, 'upcoming', ?, ?, ?, ?)",
                (vid, _scheduled_start(v), msg_id, v["snippet"].get("title"), int(members)),
            )
        mark_seen("yt", vid)

    # Previously tracked upcoming/live videos: look for state changes
    for vid, row in tracked.items():
        v = videos.get(vid)
        if not v:
            continue
        old = row[1]
        state = v["snippet"].get("liveBroadcastContent", "none")
        if state == "live" and old != "live":
            msg_id = yt_notify(v, "live")
            db(
                "UPDATE yt_tracked SET state='live', message_id=?, title=?, pending_at=NULL,"
                " pending_title=NULL, missing_polls=0 WHERE video_id=?",
                (msg_id, v["snippet"].get("title"), vid),
            )
        elif state == "live":
            yt_check_title(v, row, "live")
        elif state == "none":
            if old == "live":
                yt_handle_ended(v, row)
            db("DELETE FROM yt_tracked WHERE video_id=?", (vid,))  # ended
        elif state == "upcoming":
            start = _scheduled_start(v)
            if start and _parse_ts(start) < datetime.now(timezone.utc) - timedelta(hours=48):
                db("DELETE FROM yt_tracked WHERE video_id=?", (vid,))  # stale/never started
            else:
                if row[6]:
                    db("UPDATE yt_tracked SET missing_polls=0 WHERE video_id=?", (vid,))
                if not yt_check_reschedule(v, row):
                    yt_check_title(v, row, "scheduled")

    if seeding:
        meta_set("yt_seeded")
        log.info("YouTube: seeded %d existing videos (no notifications sent)", len(playlist_ids))
    if members_seeding:
        meta_set("yt_members_seeded")
        log.info("YouTube: seeded %d members-only streams (no notifications sent)", len(member_ids))


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def run_forever(name, fn, interval):
    fails = 0
    polls = 0  # successful polls since the last heartbeat
    last_beat = time.monotonic()
    while True:
        try:
            fn()
            if fails:
                log.info("%s: recovered after %d failure(s)", name, fails)
            fails = 0
            polls += 1
            # Heartbeat: only logged while healthy; failures are already logged as errors
            elapsed = time.monotonic() - last_beat
            if HEARTBEAT and elapsed >= HEARTBEAT:
                log.info("%s: healthy, %d successful polls in the last %d min", name, polls, round(elapsed / 60))
                polls = 0
                last_beat = time.monotonic()
        except Exception as e:  # one source failing never affects the other
            fails += 1
            log.error("%s poll failed (%d in a row): %s", name, fails, redact(e))
        delay = interval if not fails else min(interval * 2 ** fails, 900)
        time.sleep(delay * random.uniform(0.9, 1.1))


def main():
    if not WEBHOOK:
        sys.exit("DISCORD_WEBHOOK_URL is required")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # clean docker stop

    started = 0
    if BSKY_ACTOR:
        threading.Thread(target=run_forever, args=("bluesky", bsky_poll, BSKY_POLL), daemon=True).start()
        log.info("Watching Bluesky: %s (every %ss)", BSKY_ACTOR, BSKY_POLL)
        started += 1
    if YT_KEY and YT_CHANNEL_ID:
        if not YT_CHANNEL_ID.startswith("UC"):
            sys.exit("YT_CHANNEL_ID must be a channel ID starting with 'UC'")
        threading.Thread(target=run_forever, args=("youtube", yt_poll, YT_POLL), daemon=True).start()
        log.info("Watching YouTube: %s (every %ss)", YT_CHANNEL_ID, YT_POLL)
        started += 1
    if not started:
        sys.exit("Nothing to watch: set BSKY_ACTOR and/or YOUTUBE_API_KEY + YT_CHANNEL_ID")

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()