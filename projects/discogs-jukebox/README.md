# Discogs → Sonos Jukebox

> Part of **[pwnkw_ha](../../)** — Kenneth's collection of Home Assistant projects.

Turn a random record from your **Discogs** collection into a one-tap jukebox on **Sonos**,
using **Apple Music** — no Spotify, no Music Assistant, no paid APIs.

A dashboard tile shows a random album from your collection (cover art, label, year). Then:

| Gesture / control | What it does |
|---|---|
| **Tap the tile** | Play the **album** on the Living Room Sonos |
| **Double-tap the tile** | **Shuffle** to a new random record |
| **Hold the tile** | Open the album on **Discogs** |
| **"Play Artist" button** | Play a **shuffled mix of the artist's top 10 songs** |

## How it works

Home Assistant's Sonos integration can play an **Apple Music album/song URL** directly on a
speaker via Sonos share-links (as long as Apple Music is linked in your Sonos app). The only
missing piece is turning `"Artist - Album"` text into a playable Apple Music URL — and Apple
provides a **free, no-auth [iTunes Search API](https://performance-partners.apple.com/search-api)**
that does exactly that.

```
Discogs random record  ──►  iTunes Search API  ──►  Apple Music URL  ──►  Sonos (share-link)
   (sensor)                  (REST sensor)          (collectionViewUrl)   (media_player.play_media)
```

- `sensor.discogs_random_record` — from the built-in Discogs integration; state is `Artist - Album`.
- **REST sensors** resolve that to Apple Music URLs (album, and the artist's top songs).
- **Scripts** force-refresh the resolver, then call `media_player.play_media`.
- A **`custom:button-card`** tile ties it together with tap / double-tap / hold gestures.

> **Why not YouTube Music?** Sonos share-links support Apple Music, Spotify, Tidal, Deezer,
> and Amazon — but **not** YouTube Music, and there's no free YTM search API. Handing Sonos a
> YouTube Music (or Apple Music *artist-page*) URL returns a 500. YTM on Sonos would require
> Music Assistant.

## Requirements

- Home Assistant (built with 2026.8).
- The **Discogs** integration configured (provides `sensor.discogs_random_record`).
- **Sonos** speakers with **Apple Music** added as a service in the Sonos app.
- **[custom:button-card](https://github.com/custom-cards/button-card)** (via HACS) for the dashboard tile.

## Install

1. **Secret:** add your Discogs user token to `secrets.yaml` (see `secrets.yaml.example`):
   ```yaml
   discogs_token: YOUR_DISCOGS_USER_TOKEN
   ```
2. **Includes:** ensure `configuration.yaml` has (see `configuration.example.yaml`):
   ```yaml
   sensor: !include_dir_merge_list sensors/
   rest:   !include_dir_merge_list rest/
   ```
3. **Copy files** into your HA config:
   - `sensors/discogs.yaml`
   - `rest/discogs_apple_music.yaml`, `rest/discogs_artist_songs.yaml`
   - the three scripts in `scripts.yaml` (merge into your existing `scripts.yaml`, or add via the UI)
4. **Set your speaker.** The scripts target `media_player.livingroom_sonos` — change that
   entity to your speaker in all three scripts.
5. **Restart** Home Assistant (platform sensors need a restart; `rest.reload` won't add new ones).
6. **Dashboard:** add the two cards from `dashboard/` (Manual card → paste YAML).

## Files

```
sensors/discogs.yaml              # Discogs integration (token via !secret)
rest/discogs_apple_music.yaml     # album  → Apple Music URL   (sensor.discogs_album_on_apple_music)
rest/discogs_artist_songs.yaml    # artist → top-10 song URLs  (sensor.discogs_artist_songs_apple_music)
scripts.yaml                      # play album / play artist mix / shuffle
dashboard/discogs_tile.yaml       # the album tile (tap/double-tap/hold)
dashboard/play_artist_button.yaml # "Play <Artist> Mix in LR" button
configuration.example.yaml        # the include lines to add
secrets.yaml.example              # the secret you need to set
```

## Security

No credentials are committed. `secrets.yaml` and `.storage/` are git-ignored, and the Discogs
token is referenced via `!secret discogs_token` rather than inlined.

## Credits & Attribution

This project is glue — it stands on these:

- **Discogs** — album metadata and cover art come from the [Discogs](https://www.discogs.com)
  database via the [Discogs API](https://www.discogs.com/developers). *Data provided by Discogs.*
- **Home Assistant Discogs integration** — provides `sensor.discogs_random_record`
  ([docs](https://www.home-assistant.io/integrations/discogs) ·
  [source](https://github.com/home-assistant/core/tree/dev/homeassistant/components/discogs),
  code owner [@thibmaek](https://github.com/thibmaek)).
- **discogs-client** — the official Python Discogs API client the integration depends on:
  [github.com/discogs/discogs_client](https://github.com/discogs/discogs_client).
- **Apple iTunes Search API** — resolves album/artist names to Apple Music URLs (free, no auth):
  [documentation](https://performance-partners.apple.com/search-api).
- **custom:button-card** — the dashboard tile:
  [github.com/custom-cards/button-card](https://github.com/custom-cards/button-card).

Trademarks (Discogs, Apple Music, Sonos) belong to their respective owners; this project is not
affiliated with or endorsed by any of them.

---

*Built in a single evening with Claude Code. Tap a record, hear it play. 🎶*
