# qBittorrent Hardlink Maintainer

Small container that protects qBittorrent seeding files when Sonarr/Radarr accidentally copied media instead of hardlinking it.

Default behavior is safe: `dry_run` is `true`, so the first runs only report what would be repaired or deleted.

## What It Does

- Reads qBittorrent torrent files under `/data/downloads`.
- Reads Radarr movie files and Sonarr episode files via their APIs.
- Detects media copies that match a download file by size and content hash but are not hardlinks.
- Replaces the media copy with an atomic hardlink when `dry_run=false`.
- Protects torrents with unrepaired duplicate copies from deletion.
- Cleans torrents whose files no longer have hardlinks when enabled.
- Writes JSON reports to `/config/reports`.

## Schedule

The interval is configured in `/config/config.json`:

```json
"run_interval_seconds": 21600
```

`21600` seconds is 6 hours.

## First Run

Keep `dry_run=true` and inspect reports before enabling real repair/cleanup.
