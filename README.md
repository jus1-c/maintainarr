# maintainarr

Container that protects qBittorrent seeding files when Sonarr/Radarr accidentally copied media instead of hardlinking it, cleans up orphaned download files, and optionally unmonitors Sonarr/Radarr items that have no files to prevent re-download loops.

Default behavior is safe: `dry_run` is `true`, so the first runs only report what would be repaired or deleted.

## What It Does

- Reads qBittorrent torrent files under `/data/downloads`.
- Reads Radarr movie files and Sonarr episode files via their APIs.
- Detects media copies that match a download file by size and content hash but are not hardlinks.
- Replaces the media copy with an atomic hardlink when `dry_run=false`.
- Protects torrents with unrepaired duplicate copies from deletion.
- Cleans torrents whose files no longer have hardlinks when enabled.
- Removes orphaned download files not tracked by any active torrent (`cleanup_orphaned_files`).
- Optionally unmonitors Sonarr/Radarr items that have no files to stop re-download loops (`unmonitor_on_cleanup`).
- Writes JSON reports to `/config/reports`.

## Schedule

The interval is configured in `/config/config.json`:

```json
"run_interval_seconds": 21600
```

`21600` seconds is 6 hours.

## First Run

If `/config/config.json` does not exist, the container creates it from the bundled safe defaults.

Keep `dry_run=true` and inspect reports before enabling real repair/cleanup.

## Key Config Flags

| Flag | Default | Description |
|---|---|---|
| `dry_run` | `true` | When true, only reports what would be done |
| `repair_duplicate_copies` | `true` | Replace media copies with atomic hardlinks |
| `cleanup_torrents` | `true` | Remove torrents whose files have no hardlinks |
| `cleanup_orphaned_files` | `true` | Delete download files not tracked by any active torrent |
| `unmonitor_on_cleanup` | `false` | Unmonitor Sonarr/Radarr items with no files before deleting torrent |

Set `unmonitor_on_cleanup=true` if you delete files from Jellyfin/Sonarr/Radarr UI and want the maintainer to stop re-downloads automatically.

## Minimal Compose

```yaml
services:
  maintainarr:
    image: local/maintainarr:latest
    build:
      context: https://github.com/jus1-c/maintainarr.git
      dockerfile: Dockerfile
    container_name: maintainarr
    restart: unless-stopped
    volumes:
      - ${DATA_DIR}:/data
      - ${SERVICES_DIR}/maintainarr:/config
      - ${SERVICES_DIR}/radarr/config.xml:/servarr/radarr-config.xml:ro
      - ${SERVICES_DIR}/sonarr/config.xml:/servarr/sonarr-config.xml:ro
    depends_on:
      - qbittorrent
      - radarr
      - sonarr
```
