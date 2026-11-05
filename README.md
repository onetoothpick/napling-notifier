# Napling Notifier

A small, self-hosted bot that posts to a Discord channel when:

- a **Bluesky** account makes a new post (replies, reposts and quote posts are ignored)
- a **YouTube** channel uploads a video, goes live, or (optionally) schedules, reschedules or cancels a stream

Announced streams are kept up to date: if the title changes (for example mid-stream) the existing message is updated in place, and when the stream ends the "live now" message is marked as ended.

Each platform can ping its own Discord role, so members only get notified about what they opted into.

<!-- Add a screenshot of an example Discord notification here -->

It's a single Python file with one dependency (`httpx`). It uses a plain Discord webhook, so there's no Discord bot to create, and state lives in a small SQLite file.

> **Scope:** it watches exactly **one Bluesky account and one YouTube channel**.

## Quick start (Docker Compose)

```bash
git clone <this-repo-url>
cd <repo-folder>
cp .env.example .env      # then edit .env (see "Getting your credentials" below)
docker compose up -d
docker compose logs -f
```

You should see `Watching Bluesky: ...` and/or `Watching YouTube: ...`, followed by a "seeded N existing ..." line. **Nothing is posted on first start.** Existing posts and videos are recorded as already seen, so you don't get a flood of old notifications. To test, make a new post or wait for the next upload.

Your state lives in a Docker volume, so it survives restarts and updates. To update later: `git pull && docker compose up -d --build`.

## Configuration

Everything is configured with environment variables (see `.env.example`). A source is enabled only if its variables are set, so you can run Bluesky only, YouTube only, or both.

| Variable                          | Required    | Description                                                                                                                                                    |
| --------------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DISCORD_WEBHOOK_URL`             | Yes         | Webhook URL for the channel to post in                                                                                                                         |
| `BSKY_ACTOR`                      | For Bluesky | Handle (e.g. `someone.bsky.social`) or DID of the account to watch                                                                                             |
| `BSKY_ROLE_ID`                    | No          | Role to ping for Bluesky posts                                                                                                                                 |
| `BSKY_POLL_SECONDS`               | No          | Default `60`                                                                                                                                                   |
| `YOUTUBE_API_KEY`                 | For YouTube | YouTube Data API v3 key                                                                                                                                        |
| `YT_CHANNEL_ID`                   | For YouTube | Channel ID, starting with `UC`                                                                                                                                 |
| `YT_ROLE_ID`                      | No          | Role to ping for all YouTube notifications                                                                                                                     |
| `YT_POLL_SECONDS`                 | No          | Default `120`                                                                                                                                                  |
| `YT_NOTIFY_SCHEDULED`             | No          | `true` to also announce scheduled streams and handle reschedules and cancellations. Default `false`                                                            |
| `YT_INCLUDE_MEMBERS`              | No          | `true` to also watch the channel's members-only live streams (one extra API call per poll). Default `false`. See [Members-only streams](#members-only-streams) |
| `YT_RESCHEDULE_THRESHOLD_MINUTES` | No          | Reschedules smaller than this only edit the original message; larger ones also post a new pinging message. Default `60`                                        |
| `DB_PATH`                         | No          | SQLite file location. Default `state.db` (the Docker image uses `/data/state.db`)                                                                              |
| `LOG_LEVEL`                       | No          | Default `INFO`                                                                                                                                                 |
| `HEARTBEAT_MINUTES`               | No          | How often each source logs a "healthy" line (see [Logs](#logs)). `0` disables. Default `60`                                                                    |

## Getting your credentials

**Discord webhook URL:** channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. Treat it like a password: anyone with it can post to your channel.

**Discord role IDs (optional):** enable Developer Mode (User Settings → Advanced), then Server Settings → Roles → right-click a role → Copy Role ID. Use the bare number (like `123456789012345678`), not the role name or a `<@&...>` mention. Leave a role ID blank for no ping.

**Bluesky:** just the account's handle (without the `@`). No API key is needed.

**YouTube channel ID:** open the channel's page → About → Share channel → Copy channel ID. It starts with `UC`. (A handle like `@name` won't work here.)

**YouTube API key** (free within the default quota):

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. Enable **YouTube Data API v3** (APIs & Services → Library).
3. Create an API key (APIs & Services → Credentials) and restrict it to the YouTube Data API v3.

The default daily quota is 10,000 units. At the default polling interval the bot uses roughly 700 units a day when nothing is happening and up to about 1,400 while a stream is scheduled or live. Enabling members-only streams adds up to about 700 more.

## Other ways to run it

**Plain Docker:**

```bash
docker build -t discord-notifier .
docker run -d --name discord-notifier --restart unless-stopped \
  --env-file .env -v notifier-data:/data discord-notifier
```

**Coolify (or similar platforms):** create an application from this repo using the Dockerfile build pack, expose no port, disable the HTTP healthcheck, add the variables from `.env.example`, and add a **persistent storage** mount at `/data`. Without that mount, state is lost on every redeploy and you'll get duplicate notifications.

**Directly with Python** (tested on Python 3.12):

```bash
pip install -r requirements.txt
set -a; source .env; set +a      # the script reads environment variables, not the .env file itself
python main.py
```

State is saved to `state.db` in the current directory unless you set `DB_PATH`.

## Members-only streams

Members-only content isn't in a channel's public uploads playlist, so by default the bot never sees it. Setting `YT_INCLUDE_MEMBERS=true` makes it also read the channel's members-only live streams playlist, which gets the same scheduled, live, rescheduled, retitled, cancelled and ended handling as public streams, labeled "🔒 Members-only".

This relies on an **undocumented** YouTube playlist, so treat it as best-effort:

- It's off by default. Think about whether you want members-only streams announced in a channel that non-members can read. Titles and links are exposed, though only members can open them.
- If the playlist can't be read (for example the channel has no memberships), the bot logs it and retries once an hour. It never affects normal notifications.
- Turning it on later doesn't announce the existing backlog.
- Only streams are covered, not regular members-only videos. A members-only stream that starts and ends between two polls isn't announced.
- Members-only messages ping the same `YT_ROLE_ID` as everything else.

## How it works

- **Bluesky:** polls the public `getAuthorFeed` endpoint and posts a link for each new top-level post, so Discord builds its own embed (preserving links and images).
- **YouTube:** polls the channel's uploads playlist (and optionally its members-only streams playlist) and classifies videos with `videos.list` (upload, upcoming or live). With `YT_NOTIFY_SCHEDULED=true`, a rescheduled stream quietly edits the original announcement (and posts a new pinging message if the shift is large), and a cancelled stream is marked cancelled. If a stream's title changes after it's announced (scheduled or live), the original message is quietly edited to match, without pinging anyone. When a live stream ends, its "live now" message is quietly edited to say the stream ended (with how long it ran, when YouTube provides that), so it never claims a finished stream is live. Changes must hold for two consecutive polls before the bot acts, so a creator fiddling with a schedule or title doesn't cause noise.
- **Sends are committed after success**, so a Discord outage causes a retry, not a lost notification.
- **Role pings are locked down:** the ping is in the message text and only the configured role is allowed to be mentioned, so a title containing `@everyone` can't ping anyone.
- Each source runs independently; one failing never blocks the other, and repeated failures back off up to 15 minutes.

## Logs

Normal operation is quiet. On startup you'll see which sources are being watched, and on the first run a one-time "seeded N existing ..." line per source. After that, a line appears only when something happens (a notification sent, a message edited, a failure). Request URLs are never logged, and the API key and webhook URL are scrubbed from log output.

Once an hour (`HEARTBEAT_MINUTES`), each source logs a heartbeat so you can tell it's alive:

```
INFO youtube: healthy, 30 successful polls in the last 60 min
```

The heartbeat is only logged while the source is working. Failures are logged as errors with exponential back-off (up to 15 minutes between attempts), followed by a "recovered" line once it works again. Seeing "seeded" lines again after a redeploy means your state isn't being persisted.

## Troubleshooting

| Symptom                                  | Likely cause                                                                                                                      |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Duplicate notifications after a redeploy | State isn't persisted (no volume mounted at `/data`)                                                                              |
| Role isn't pinged                        | Wrong role ID, or the role isn't mentionable (try enabling "Allow anyone to @mention this role")                                  |
| `403` from YouTube in the logs           | API key missing or restricted incorrectly, API not enabled, or daily quota exhausted (resets at midnight Pacific)                 |
| Members-only streams aren't announced    | `YT_INCLUDE_MEMBERS` must be `true`. Check the logs for "members-only playlist unavailable", which means the API couldn't read it |
| Nothing happens on first start           | Expected. Existing content is recorded silently                                                                                   |

## Limitations

- Notification latency is bounded by the poll interval, and by how quickly YouTube adds videos to the uploads playlist.
- YouTube Shorts notify like normal videos.
- Title updates only apply to streams the bot announced. Thumbnail-only changes aren't detected (the thumbnail's URL doesn't change, and Discord caches embed images by URL); the thumbnail refreshes when the title changes. Normal upload messages are never edited.
- Only streams the bot announced as live are marked ended. One that was already live when the bot first started, or that was deleted rather than ended, keeps its original message.
- Logs hide your API key and webhook URL, but check anything you paste into a public issue anyway.
- Bluesky multi-post threads: follow-up posts are self-replies and are excluded by design.
- A premiere is treated like a scheduled stream, so its messages use the word "stream".

This is a personal project. Issues and pull requests are welcome, but there are no guarantees on response time.

## Development

Formatting uses [Black](https://black.readthedocs.io/) (settings in `pyproject.toml`).

## License

[MIT](LICENSE)
