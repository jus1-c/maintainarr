# maintainarr

Container that protects qBittorrent seeding files when Sonarr/Radarr accidentally copied media instead of hardlinking it, cleans up orphaned download files, and optionally unmonitors Sonarr/Radarr items that have no files to prevent re-download loops.

Default behavior is safe: `dry_run` is `true`, so the first runs only report what would be repaired or deleted.

Supports an optional HTTP server with Jellyfin webhook integration so cleanup runs immediately when you delete media, rather than waiting for the periodic interval.

## What It Does

- Reads qBittorrent torrent files under `/data/downloads`.
- Reads Radarr movie files and Sonarr episode files via their APIs.
- Detects media copies that match a download file by size and content hash but are not hardlinks.
- Replaces the media copy with an atomic hardlink when `dry_run=false`.
- Protects torrents with unrepaired duplicate copies from deletion.
- Cleans torrents whose files no longer have hardlinks when enabled.
- Removes orphaned download files not tracked by any active torrent (`cleanup_orphaned_files`).
- Protects hardlinked and media-tracked files from false-positive orphaned deletion.
- Optionally unmonitors Sonarr/Radarr items that have no files to stop re-download loops (`unmonitor_on_cleanup`).
- Optional HTTP server with `/run`, `/webhook/jellyfin`, and `/healthz` endpoints.
- Writes JSON reports to `/config/reports`.

## Schedule

The interval is configured in `/config/config.json`:

```json
"run_interval_seconds": 21600
```

`21600` seconds is 6 hours. When HTTP mode is enabled, the periodic interval acts as a fallback.

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
| `http_enabled` | `false` | Start an HTTP server for `/run` and `/webhook/jellyfin` |
| `http_port` | `9898` | Port for the HTTP server |
| `jellyfin.trigger_deleted_task_enabled` | `false` | Periodically trigger Jellyfin's queued `WebhookItemDeleted` task |

Set `unmonitor_on_cleanup=true` if you delete files from Jellyfin/Sonarr/Radarr UI and want the maintainer to stop re-downloads automatically.

## Event-Driven Mode (Jellyfin Webhook)

When you delete media in Jellyfin, maintainarr can react immediately instead of waiting for the periodic interval.

1. Enable the HTTP server in config:
```json
{
  "http_enabled": true,
  "http_port": 9898
}
```

2. In Jellyfin, install the **Webhook** plugin from the official catalog.

3. Configure a webhook in Jellyfin:
    - **Webhook URL**: `http://maintainarr:9898/webhook/jellyfin`
    - **Notification Type**: Item Deleted
    - Check **Send All Properties**

Jellyfin's Webhook plugin queues deleted-item notifications and flushes them through the scheduled task named `Webhook Item Deleted Notifier`. Some Jellyfin versions expose only coarse UI intervals, which can delay deletes. To force that queue to flush quickly, enable:

```json
{
  "jellyfin": {
    "host": "jellyfin",
    "port": 8096,
    "api_key": "YOUR_JELLYFIN_API_KEY",
    "trigger_deleted_task_enabled": true,
    "trigger_deleted_task_interval_seconds": 30
  }
}
```

When you delete a movie or episode from Jellyfin, the webhook fires. maintainarr uses the Servarr parse API to find matching items and deletes/unmonitors them if `dry_run=false`, or logs the action if `dry_run=true`.

Manual run via HTTP:
```bash
curl -X POST http://localhost:9898/run
```

## HTTP Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Health check |
| `POST` | `/run` | Trigger a full maintenance run |
| `POST` | `/webhook/jellyfin` | Receive Jellyfin webhook notifications |

## Report Keys (Orphaned Files)

| Key | Description |
|---|---|
| `orphaned_files_found` | Total orphaned candidates scanned |
| `orphaned_files_would_delete` | Would delete (dry-run) |
| `orphaned_files_deleted` | Actually deleted |
| `orphaned_bytes_freed` | Bytes freed |
| `orphaned_hardlinked_protected` | Protected because file still has hardlinks |
| `orphaned_media_protected` | Protected because file is tracked by Sonarr/Radarr |

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
    ports:
      - "9898:9898"  # only needed if http_enabled=true
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
