from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_CONFIG_PATH = "/config/config.json"


def create_default_config(path: Path) -> None:
    template_path = Path(__file__).resolve().parents[1] / "config.example.json"
    if not template_path.exists():
        raise FileNotFoundError(f"Default config template not found: {template_path}")

    with open(template_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Created default config at {path}", flush=True)


@dataclass(frozen=True)
class FileEntry:
    path: Path
    size: int
    dev: int
    ino: int
    nlink: int
    source: str
    title: str
    torrent_hash: str | None = None
    torrent_name: str | None = None


class ApiClient:
    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"X-Api-Key": api_key})

    def get(self, path: str, **params: Any) -> Any:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, data: dict[str, Any]) -> requests.Response:
        response = self.session.post(f"{self.base_url}{path}", data=data, timeout=60)
        response.raise_for_status()
        return response

    def put(self, path: str, json_data: dict[str, Any]) -> requests.Response:
        response = self.session.put(f"{self.base_url}{path}", json=json_data, timeout=60)
        response.raise_for_status()
        return response


class QBittorrentClient(ApiClient):
    def __init__(self, cfg: dict[str, Any]) -> None:
        base_url = f"http://{cfg.get('host', 'qbittorrent')}:{cfg.get('port', 8080)}/api/v2"
        super().__init__(base_url)
        self.username = cfg.get("username") or ""
        self.password = cfg.get("password") or ""
        self._ready = False

    def login(self) -> bool:
        if self._ready:
            return True

        if self.username or self.password:
            response = self.session.post(
                f"{self.base_url}/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=30,
            )
            if response.status_code == 200 and response.text == "Ok.":
                self._ready = True
                logging.info("qBittorrent login succeeded")
                return True

        response = self.session.get(f"{self.base_url}/app/version", timeout=30)
        if response.status_code == 200:
            self._ready = True
            logging.info("qBittorrent API is reachable without login")
            return True

        logging.error("qBittorrent login failed")
        return False

    def torrents(self) -> list[dict[str, Any]]:
        if not self.login():
            return []
        return self.get("/torrents/info")

    def torrent_files(self, torrent_hash: str) -> list[dict[str, Any]]:
        if not self.login():
            return []
        return self.get("/torrents/files", hash=torrent_hash)

    def delete_torrent(self, torrent_hash: str) -> None:
        if not self.login():
            raise RuntimeError("qBittorrent is not reachable")
        self.post("/torrents/delete", {"hashes": torrent_hash, "deleteFiles": "false"})


class ArrClient(ApiClient):
    def delete(self, path: str) -> requests.Response:
        response = self.session.delete(f"{self.base_url}{path}", timeout=60)
        response.raise_for_status()
        return response


class JellyfinClient(ApiClient):
    def post_json(self, path: str) -> requests.Response:
        response = self.session.post(f"{self.base_url}{path}", timeout=60)
        response.raise_for_status()
        return response


def load_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        create_default_config(config_path)

    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    template_path = Path(__file__).resolve().parents[1] / "config.example.json"
    if template_path.exists():
        with open(template_path, "r", encoding="utf-8") as f:
            template = json.load(f)
        def merge_missing(dst: dict[str, Any], src: dict[str, Any]) -> int:
            added = 0
            for key, value in src.items():
                if key not in dst:
                    dst[key] = value
                    added += 1
                elif isinstance(dst[key], dict) and isinstance(value, dict):
                    added += merge_missing(dst[key], value)
            return added

        added = merge_missing(cfg, template)
        if added:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print(f"Merged {added} new default keys into {path}", flush=True)

    return cfg


def read_api_key(value: str | None, file_path: str | None) -> str:
    if value:
        return value
    if not file_path:
        return ""
    root = ET.parse(file_path).getroot()
    for child in root:
        if child.tag == "ApiKey":
            return child.text or ""
    return ""


def read_jellyfin_api_key(cfg: dict[str, Any]) -> str:
    api_key = read_api_key(cfg.get("api_key"), cfg.get("api_key_file"))
    if api_key:
        return api_key

    db_path = cfg.get("db_path") or ""
    if not db_path:
        return ""

    key_name = cfg.get("api_key_name") or "Maintainarr"
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            row = con.execute("select AccessToken from ApiKeys where Name = ? limit 1", (key_name,)).fetchone()
            if not row:
                row = con.execute("select AccessToken from ApiKeys order by Id limit 1").fetchone()
            return (row[0] if row else "") or ""
    except Exception as exc:
        logging.warning("Failed to read Jellyfin API key database: %s", exc)
        return ""


def servarr_client(cfg: dict[str, Any]) -> ArrClient:
    base_url = f"http://{cfg.get('host')}:{cfg.get('port')}"
    base_path = (cfg.get("base_url") or "").strip("/")
    if base_path:
        base_url = f"{base_url}/{base_path}"
    api_key = read_api_key(cfg.get("api_key"), cfg.get("api_key_file"))
    return ArrClient(f"{base_url}/api/v3", api_key)


def jellyfin_client(cfg: dict[str, Any]) -> JellyfinClient:
    base_url = f"http://{cfg.get('host', 'jellyfin')}:{cfg.get('port', 8096)}"
    base_path = (cfg.get("base_url") or "").strip("/")
    if base_path:
        base_url = f"{base_url}/{base_path}"
    api_key = read_api_key(cfg.get("api_key"), cfg.get("api_key_file"))
    client = JellyfinClient(base_url, None)
    if api_key:
        client.session.headers.update({"X-Emby-Token": api_key})
    return client


def setup_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def stat_file(path: Path) -> tuple[int, int, int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        return None
    return st.st_size, st.st_dev, st.st_ino, st.st_nlink


def is_video(path: Path, extensions: set[str]) -> bool:
    return path.suffix.lower() in extensions


def path_from_data(path: str, data_root: Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return data_root / p


def build_download_entries(qbt: QBittorrentClient, cfg: dict[str, Any]) -> tuple[list[FileEntry], dict[str, dict[str, Any]]]:
    data_root = Path(cfg.get("data_root", "/data"))
    download_root = Path(cfg.get("download_root", "/data/downloads"))
    extensions = {x.lower() for x in cfg.get("video_extensions", [])}
    min_size = int(cfg.get("min_video_size_mb", 10)) * 1024 * 1024
    skip_incomplete = bool(cfg.get("skip_incomplete_torrents", True))
    entries: list[FileEntry] = []
    torrents_by_hash: dict[str, dict[str, Any]] = {}

    for torrent in qbt.torrents():
        torrent_hash = torrent.get("hash")
        if not torrent_hash:
            continue
        torrents_by_hash[torrent_hash] = torrent
        if skip_incomplete and float(torrent.get("progress") or 0) < 1.0:
            continue

        save_path = path_from_data(torrent.get("save_path") or str(download_root), data_root)
        for file_info in qbt.torrent_files(torrent_hash):
            rel_name = file_info.get("name") or ""
            path = save_path / rel_name
            if not is_video(path, extensions):
                continue
            stat = stat_file(path)
            if not stat:
                continue
            size, dev, ino, nlink = stat
            if size < min_size:
                continue
            entries.append(
                FileEntry(
                    path=path,
                    size=size,
                    dev=dev,
                    ino=ino,
                    nlink=nlink,
                    source="qbittorrent",
                    title=rel_name,
                    torrent_hash=torrent_hash,
                    torrent_name=torrent.get("name") or "",
                )
            )
    return entries, torrents_by_hash


def build_media_entries(cfg: dict[str, Any]) -> list[FileEntry]:
    extensions = {x.lower() for x in cfg.get("video_extensions", [])}
    min_size = int(cfg.get("min_video_size_mb", 10)) * 1024 * 1024
    entries: list[FileEntry] = []

    radarr_cfg = cfg.get("radarr") or {}
    if radarr_cfg.get("enabled", True):
        client = servarr_client(radarr_cfg)
        for movie in client.get("/movie"):
            movie_file = movie.get("movieFile") or {}
            path_value = movie_file.get("path")
            if not path_value:
                continue
            path = Path(path_value)
            if not is_video(path, extensions):
                continue
            stat = stat_file(path)
            if not stat:
                continue
            size, dev, ino, nlink = stat
            if size >= min_size:
                entries.append(FileEntry(path, size, dev, ino, nlink, "radarr", movie.get("title") or path.name))

    sonarr_cfg = cfg.get("sonarr") or {}
    if sonarr_cfg.get("enabled", True):
        client = servarr_client(sonarr_cfg)
        for series in client.get("/series"):
            for episode_file in client.get("/episodefile", seriesId=series.get("id")):
                path_value = episode_file.get("path")
                if not path_value:
                    continue
                path = Path(path_value)
                if not is_video(path, extensions):
                    continue
                stat = stat_file(path)
                if not stat:
                    continue
                size, dev, ino, nlink = stat
                if size >= min_size:
                    entries.append(FileEntry(path, size, dev, ino, nlink, "sonarr", series.get("title") or path.name))

    return entries


def chunk_hash(path: Path, offset: int, size: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(offset)
        h.update(f.read(size))
    return h.hexdigest()


def partial_hash(path: Path, size: int, chunk_size: int) -> str:
    offsets = {0}
    if size > chunk_size:
        offsets.add(max(0, (size // 2) - (chunk_size // 2)))
        offsets.add(max(0, size - chunk_size))
    h = hashlib.sha256()
    for offset in sorted(offsets):
        h.update(chunk_hash(path, offset, min(chunk_size, size - offset)).encode("ascii"))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def same_content(a: Path, b: Path, size: int, cfg: dict[str, Any]) -> bool:
    chunk_size = int(cfg.get("partial_hash_bytes", 4 * 1024 * 1024))
    if partial_hash(a, size, chunk_size) != partial_hash(b, size, chunk_size):
        return False
    if bool(cfg.get("full_hash_before_repair", True)):
        return full_hash(a) == full_hash(b)
    return True


def replace_copy_with_hardlink(download_path: Path, media_path: Path, dry_run: bool) -> None:
    tmp_path = media_path.with_name(f".hardlink-repair.{media_path.name}.tmp")
    if dry_run:
        logging.info("[DRY-RUN] Would replace media copy with hardlink: %s -> %s", media_path, download_path)
        return
    try:
        tmp_path.unlink()
    except FileNotFoundError:
        pass
    os.link(download_path, tmp_path)
    download_stat = download_path.stat()
    tmp_stat = tmp_path.stat()
    if (download_stat.st_dev, download_stat.st_ino) != (tmp_stat.st_dev, tmp_stat.st_ino):
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"temporary hardlink verification failed for {media_path}")
    os.replace(tmp_path, media_path)
    media_stat = media_path.stat()
    if (download_stat.st_dev, download_stat.st_ino) != (media_stat.st_dev, media_stat.st_ino):
        raise RuntimeError(f"media hardlink verification failed for {media_path}")


def repair_duplicates(downloads: list[FileEntry], media: list[FileEntry], cfg: dict[str, Any], report: dict[str, Any]) -> set[str]:
    protected_hashes: set[str] = set()
    by_size: dict[int, list[FileEntry]] = {}
    for entry in downloads:
        by_size.setdefault(entry.size, []).append(entry)

    dry_run = bool(cfg.get("dry_run", True))
    if not cfg.get("repair_duplicate_copies", True):
        return protected_hashes

    for media_entry in media:
        candidates = [d for d in by_size.get(media_entry.size, []) if d.dev == media_entry.dev and d.ino != media_entry.ino]
        if not candidates:
            continue
        report["duplicates_found"] += 1
        content_matches = []
        for candidate in candidates:
            try:
                if same_content(media_entry.path, candidate.path, media_entry.size, cfg):
                    content_matches.append(candidate)
            except OSError as exc:
                report["errors"].append(str(exc))

        if len(content_matches) != 1:
            for candidate in candidates:
                if candidate.torrent_hash:
                    protected_hashes.add(candidate.torrent_hash)
            report["duplicates_skipped"] += 1
            logging.warning("Skipping ambiguous duplicate copy: %s (%s matches)", media_entry.path, len(content_matches))
            continue

        match = content_matches[0]
        try:
            replace_copy_with_hardlink(match.path, media_entry.path, dry_run)
            if match.torrent_hash:
                protected_hashes.add(match.torrent_hash)
            if dry_run:
                report["duplicates_would_repair"] += 1
                logging.info("[DRY-RUN] Would repair duplicate copy: %s", media_entry.path)
            else:
                report["duplicates_repaired"] += 1
                logging.info("Repaired duplicate copy: %s", media_entry.path)
            report["repairs"].append({"media": str(media_entry.path), "download": str(match.path), "dry_run": dry_run})
        except OSError as exc:
            if match.torrent_hash:
                protected_hashes.add(match.torrent_hash)
            report["duplicates_skipped"] += 1
            report["errors"].append(str(exc))
            logging.error("Failed to repair duplicate copy %s: %s", media_entry.path, exc)

    return protected_hashes


def remove_empty_parents(path: Path, stop_at: Path) -> None:
    current = path.parent
    stop_at = stop_at.resolve()
    while current.exists() and current.resolve() != stop_at:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _collect_candidates(qbt: QBittorrentClient, cfg: dict[str, Any], protected_hashes: set[str], report: dict[str, Any]) -> list[dict[str, Any]]:
    if not cfg.get("cleanup_torrents", True):
        return []
    download_root = Path(cfg.get("download_root", "/data/downloads"))
    skip_incomplete = bool(cfg.get("skip_incomplete_torrents", True))
    candidates: list[dict[str, Any]] = []

    for torrent in qbt.torrents():
        torrent_hash = torrent.get("hash")
        if not torrent_hash:
            continue
        if torrent_hash in protected_hashes and cfg.get("skip_if_repair_failed", True):
            report["protected_torrents"] += 1
            logging.info("Skipping protected torrent: %s", torrent.get("name"))
            continue
        if skip_incomplete and float(torrent.get("progress") or 0) < 1.0:
            continue
        files = qbt.torrent_files(torrent_hash)
        paths: list[Path] = []
        file_names: list[str] = []
        has_hardlink = False
        save_path = Path(torrent.get("save_path") or str(download_root))
        for file_info in files:
            name = file_info.get("name") or ""
            path = save_path / name
            stat = stat_file(path)
            if not stat:
                continue
            _, _, _, nlink = stat
            paths.append(path)
            file_names.append(name)
            if nlink > 1:
                has_hardlink = True
        if has_hardlink:
            continue
        candidates.append({"hash": torrent_hash, "name": torrent.get("name"), "paths": paths, "file_names": file_names})
    return candidates


def _unmonitor_orphaned_items(candidates: list[dict[str, Any]], cfg: dict[str, Any], report: dict[str, Any]) -> None:
    if not cfg.get("unmonitor_on_cleanup", False):
        return
    dry_run = bool(cfg.get("dry_run", True))

    sonarr_cfg = cfg.get("sonarr") or {}
    if sonarr_cfg.get("enabled", True):
        try:
            sonarr = servarr_client(sonarr_cfg)
        except Exception:
            sonarr = None
    else:
        sonarr = None

    radarr_cfg = cfg.get("radarr") or {}
    if radarr_cfg.get("enabled", True):
        try:
            radarr = servarr_client(radarr_cfg)
        except Exception:
            radarr = None
    else:
        radarr = None

    for candidate in candidates:
        name = candidate.get("name") or ""
        if sonarr:
            _unmonitor_sonarr(sonarr, name, dry_run, report)
        if radarr:
            _unmonitor_radarr(radarr, name, dry_run, report)


def _unmonitor_sonarr(sonarr: ApiClient, name: str, dry_run: bool, report: dict[str, Any]) -> None:
    try:
        parsed = sonarr.get("/parse", title=name)
    except Exception:
        return
    episodes = parsed.get("episodes") or []
    if not episodes:
        return
    series_title = (parsed.get("series") or {}).get("title") or "unknown"
    for ep in episodes:
        ep_id = ep.get("id")
        if not ep_id:
            continue
        try:
            detail = sonarr.get(f"/episode/{ep_id}")
        except Exception:
            continue
        if not detail.get("monitored"):
            continue
        if detail.get("hasFile"):
            continue
        info = {
            "id": ep_id,
            "series": series_title,
            "season": detail.get("seasonNumber"),
            "episode": detail.get("episodeNumber"),
            "title": detail.get("title"),
        }
        if dry_run:
            report["unmonitored_episodes"].append(info)
            report["unmonitored_would_apply"] = True
            logging.info("[DRY-RUN] Would unmonitor episode: %s S%02dE%02d", series_title, info["season"] or 0, info["episode"] or 0)
        else:
            try:
                sonarr.put("/episode/monitor", {"episodeIds": [ep_id], "monitored": False})
                report["unmonitored_episodes"].append(info)
                report["unmonitored_applied"] += 1
                logging.info("Unmonitored episode: %s S%02dE%02d", series_title, info["season"] or 0, info["episode"] or 0)
            except Exception as exc:
                logging.warning("Failed to unmonitor episode %s S%02dE%02d: %s", series_title, info["season"] or 0, info["episode"] or 0, exc)


def _unmonitor_radarr(radarr: ApiClient, name: str, dry_run: bool, report: dict[str, Any]) -> None:
    try:
        parsed = radarr.get("/parse", title=name)
    except Exception:
        return
    movie = parsed.get("movie")
    if not movie:
        return
    movie_id = movie.get("id")
    movie_title = movie.get("title") or "unknown"
    if not movie_id:
        return
    try:
        detail = radarr.get(f"/movie/{movie_id}")
    except Exception:
        return
    if not detail.get("monitored"):
        return
    if detail.get("hasFile"):
        return
    info = {"id": movie_id, "title": movie_title}
    if dry_run:
        report["unmonitored_movies"].append(info)
        report["unmonitored_would_apply"] = True
        logging.info("[DRY-RUN] Would unmonitor movie: %s", movie_title)
    else:
        try:
            radarr.put("/movie/monitor", {"movieIds": [movie_id], "monitored": False})
            report["unmonitored_movies"].append(info)
            report["unmonitored_applied"] += 1
            logging.info("Unmonitored movie: %s", movie_title)
        except Exception as exc:
            logging.warning("Failed to unmonitor movie %s: %s", movie_title, exc)


def _cleanup_orphaned_files(cfg: dict[str, Any], report: dict[str, Any], active_paths: set[Path], media: list[FileEntry]) -> None:
    if not cfg.get("cleanup_orphaned_files", True):
        return
    dry_run = bool(cfg.get("dry_run", True))
    download_root = Path(cfg.get("download_root", "/data/downloads"))
    extensions = {x.lower() for x in cfg.get("video_extensions", [])}
    min_size = int(cfg.get("min_video_size_mb", 10)) * 1024 * 1024
    media_inodes: set[tuple[int, int]] = {(m.dev, m.ino) for m in media}
    active_resolved: set[Path] = {p.resolve() for p in active_paths}

    count = 0
    bytes_freed = 0
    for entry in download_root.rglob("*"):
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in extensions:
            continue
        try:
            st = entry.stat()
            size = st.st_size
        except OSError:
            continue
        if size < min_size:
            continue
        if entry.resolve() in active_resolved:
            continue
        nlink = st.st_nlink
        dev = st.st_dev
        ino = st.st_ino
        count += 1
        bytes_freed += size

        protected = False
        if nlink > 1:
            protected = True
            report["orphaned_hardlinked_protected"] += 1
            logging.info("[PROTECTED] Orphaned candidate with hardlinks: %s (nlink=%s)", entry, nlink)
        elif (dev, ino) in media_inodes:
            protected = True
            report["orphaned_media_protected"] += 1
            logging.info("[PROTECTED] Orphaned candidate tracked by Sonarr/Radarr: %s", entry)

        if protected:
            continue

        if dry_run:
            report["orphaned_files_would_delete"] += 1
            logging.info("[DRY-RUN] Would delete orphaned: %s", entry)
        else:
            try:
                entry.unlink()
                report["orphaned_files_deleted"] += 1
                logging.info("Deleted orphaned: %s", entry)
                if cfg.get("delete_empty_dirs", True):
                    remove_empty_parents(entry, download_root)
            except OSError as exc:
                report["errors"].append(str(exc))
                logging.error("Failed to delete orphaned %s: %s", entry, exc)

    report["orphaned_files_found"] = count
    report["orphaned_bytes_freed"] = bytes_freed


def cleanup_torrents(qbt: QBittorrentClient, cfg: dict[str, Any], protected_hashes: set[str], report: dict[str, Any]) -> None:
    candidates = _collect_candidates(qbt, cfg, protected_hashes, report)
    if not candidates:
        return
    _unmonitor_orphaned_items(candidates, cfg, report)
    dry_run = bool(cfg.get("dry_run", True))
    download_root = Path(cfg.get("download_root", "/data/downloads"))

    for candidate in candidates:
        torrent_hash = candidate["hash"]
        torrent_name = candidate["name"]
        paths = candidate["paths"]
        report["deleted_torrents"].append({"name": torrent_name, "hash": torrent_hash, "dry_run": dry_run})
        if dry_run:
            report["torrents_would_delete"] += 1
            logging.info("[DRY-RUN] Would delete torrent with no hardlinks: %s", torrent_name)
            continue
        report["torrents_deleted"] += 1
        qbt.delete_torrent(torrent_hash)
        for path in paths:
            try:
                path.unlink()
                if cfg.get("delete_empty_dirs", True):
                    remove_empty_parents(path, download_root)
            except FileNotFoundError:
                pass
            except OSError as exc:
                report["errors"].append(str(exc))
                logging.error("Failed to delete %s: %s", path, exc)


def write_report(cfg: dict[str, Any], report: dict[str, Any]) -> Path:
    reports_dir = Path(cfg.get("reports_dir", "/config/reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{report['started_at'].replace(':', '-')}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return path


_run_lock = threading.Lock()
_last_triggered: float = 0.0


class _MaintainarrHandler(http.server.BaseHTTPRequestHandler):
    """Thin request handler that dispatches to module-level callables."""

    config_path: str = DEFAULT_CONFIG_PATH

    def log_message(self, fmt: str, *args: object) -> None:
        logging.info(fmt, *args)

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/run":
            self._trigger_full_run()
        elif parsed.path == "/webhook/jellyfin":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            self._handle_jellyfin_webhook(body)
        else:
            self._json(404, {"error": "not found"})

    def _trigger_full_run(self) -> None:
        with _run_lock:
            try:
                cfg = load_config(self.config_path)
                report = run_once(self.config_path)
                self._json(200, {"status": "completed", "report": report})
            except Exception as exc:
                logging.exception("Triggered run failed")
                self._json(500, {"status": "error", "error": str(exc)})

    def _handle_jellyfin_webhook(self, body: bytes) -> None:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._json(400, {"status": "error", "error": "invalid json"})
            return

        event = payload.get("Event") or payload.get("NotificationType", "")
        item_type = payload.get("ItemType", "")
        item_name = payload.get("Name", "")
        item_path = payload.get("Path", "")
        logging.info("Jellyfin webhook: %s %s %s %s", event, item_type, item_name, item_path)

        if event not in ("ItemRemoved", "MediaDeleted", "ItemDeleted"):
            self._json(200, {"status": "ignored", "event": event})
            return

        self._handle_media_deleted(item_name, item_path, payload)
        self._json(200, {"status": "processed"})

    def _handle_media_deleted(self, item_name: str, item_path: str, payload: dict[str, Any]) -> None:
        if not item_name:
            return
        cfg = load_config(self.config_path)
        dry_run = bool(cfg.get("dry_run", True))

        sonarr_title = item_name
        if payload.get("ItemType") == "Episode" and payload.get("SeriesName"):
            season = payload.get("SeasonNumber")
            episode = payload.get("EpisodeNumber")
            if season is not None and episode is not None:
                sonarr_title = f"{payload['SeriesName']} S{int(season):02d}E{int(episode):02d}"

        sonarr_cfg = cfg.get("sonarr") or {}
        if sonarr_cfg.get("enabled", True):
            try:
                sonarr = servarr_client(sonarr_cfg)
                parsed = sonarr.get("/parse", title=sonarr_title)
                episodes = parsed.get("episodes") or []
                series_title = (parsed.get("series") or {}).get("title") or "?"
                for ep in episodes:
                    ep_id = ep.get("id")
                    if not ep_id:
                        continue
                    detail = sonarr.get(f"/episode/{ep_id}")
                    if dry_run:
                        logging.info("[DRY-RUN] Webhook would unmonitor: %s S%02dE%02d",
                                     series_title, detail.get("seasonNumber") or 0, detail.get("episodeNumber") or 0)
                    else:
                        sonarr.put("/episode/monitor", {"episodeIds": [ep_id], "monitored": False})
                        logging.info("Webhook unmonitored: %s S%02dE%02d",
                                     series_title, detail.get("seasonNumber") or 0, detail.get("episodeNumber") or 0)
                    if detail.get("episodeFileId"):
                        if dry_run:
                            logging.info("[DRY-RUN] Webhook would delete episode file: %s S%02dE%02d",
                                         series_title, detail.get("seasonNumber") or 0, detail.get("episodeNumber") or 0)
                        else:
                            try:
                                sonarr.delete(f"/episodefile/{detail['episodeFileId']}")
                                logging.info("Webhook deleted episode file: %s S%02dE%02d",
                                             series_title, detail.get("seasonNumber") or 0, detail.get("episodeNumber") or 0)
                            except Exception as exc:
                                logging.warning("Failed to delete episode file: %s", exc)
            except Exception:
                pass

        radarr_cfg = cfg.get("radarr") or {}
        if radarr_cfg.get("enabled", True):
            try:
                radarr = servarr_client(radarr_cfg)
                parsed = radarr.get("/parse", title=item_name)
                movie = parsed.get("movie")
                if movie and movie.get("id"):
                    mid = movie["id"]
                    detail = radarr.get(f"/movie/{mid}")
                    if dry_run:
                        logging.info("[DRY-RUN] Webhook would delete movie: %s", movie.get("title", "?"))
                    else:
                        radarr.delete(f"/movie/{mid}?deleteFiles=true")
                        logging.info("Webhook deleted movie: %s", movie.get("title", "?"))
            except Exception:
                pass


def _http_worker(cfg: dict[str, Any], handler_class: type[http.server.BaseHTTPRequestHandler]) -> None:
    if not cfg.get("http_enabled", False):
        return
    port = int(cfg.get("http_port", 9898))
    server = http.server.HTTPServer(("0.0.0.0", port), handler_class)
    logging.info("HTTP server listening on port %s", port)
    server.serve_forever()


def _periodic_worker(config_path: str) -> None:
    while True:
        interval = int(load_config(config_path).get("run_interval_seconds", 21600))
        with _run_lock:
            try:
                logging.info("Running periodic maintenance")
                run_once(config_path)
            except Exception:
                logging.exception("Periodic run failed")
        logging.info("Sleeping for %s seconds", interval)
        time.sleep(interval)


def _jellyfin_deleted_task_worker(config_path: str) -> None:
    task_id: str | None = None
    while True:
        cfg = load_config(config_path)
        jellyfin_cfg = cfg.get("jellyfin") or {}
        interval = max(10, int(jellyfin_cfg.get("trigger_deleted_task_interval_seconds", 60)))

        if not jellyfin_cfg.get("trigger_deleted_task_enabled", False):
            time.sleep(interval)
            continue

        try:
            client = jellyfin_client(jellyfin_cfg)
            task_state = "Idle"
            for task in client.get("/ScheduledTasks"):
                if task.get("Key") == "WebhookItemDeleted":
                    task_id = task.get("Id")
                    task_state = task.get("State") or "Idle"
                    break
            if task_id and task_state == "Idle":
                client.post_json(f"/ScheduledTasks/Running/{task_id}")
                logging.debug("Triggered Jellyfin WebhookItemDeleted task")
            elif task_id:
                logging.debug("Jellyfin WebhookItemDeleted task is already %s", task_state)
            else:
                logging.warning("Jellyfin WebhookItemDeleted task not found")
        except Exception as exc:
            logging.warning("Failed to trigger Jellyfin WebhookItemDeleted task: %s", exc)

        time.sleep(interval)


def run_once(config_path: str) -> dict[str, Any]:
    cfg = load_config(config_path)
    setup_logging(cfg.get("log_level", "INFO"))
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(cfg.get("dry_run", True)),
        "duplicates_found": 0,
        "duplicates_would_repair": 0,
        "duplicates_repaired": 0,
        "duplicates_skipped": 0,
        "protected_torrents": 0,
        "torrents_would_delete": 0,
        "torrents_deleted": 0,
        "orphaned_files_found": 0,
        "orphaned_files_would_delete": 0,
        "orphaned_files_deleted": 0,
        "orphaned_bytes_freed": 0,
        "orphaned_hardlinked_protected": 0,
        "orphaned_media_protected": 0,
        "unmonitored_episodes": [],
        "unmonitored_movies": [],
        "unmonitored_applied": 0,
        "unmonitored_would_apply": False,
        "repairs": [],
        "deleted_torrents": [],
        "errors": [],
    }

    qbt = QBittorrentClient(cfg.get("qbittorrent") or {})
    downloads, torrents_by_hash = build_download_entries(qbt, cfg)
    media = build_media_entries(cfg)
    report["download_files_scanned"] = len(downloads)
    report["media_files_scanned"] = len(media)

    logging.info("Scanned %s download files and %s media files", len(downloads), len(media))

    active = {entry.path.resolve().absolute() for entry in downloads}

    protected_hashes = repair_duplicates(downloads, media, cfg, report)
    cleanup_torrents(qbt, cfg, protected_hashes, report)
    _cleanup_orphaned_files(cfg, report, active, media)

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report_path = write_report(cfg, report)
    logging.info("Report written to %s", report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        run_once(args.config)
        return

    first_run = True
    while True:
        cfg = load_config(args.config)
        setup_logging(cfg.get("log_level", "INFO"))
        http_enabled = bool(cfg.get("http_enabled", False))

        if first_run:
            if http_enabled:
                _MaintainarrHandler.config_path = args.config
                threading.Thread(target=_http_worker, args=(cfg, _MaintainarrHandler), daemon=True).start()
            threading.Thread(target=_jellyfin_deleted_task_worker, args=(args.config,), daemon=True).start()
            threading.Thread(target=_periodic_worker, args=(args.config,), daemon=True).start()
            if not cfg.get("run_on_start", True):
                first_run = False
                time.sleep(1)
                continue

        try:
            with _run_lock:
                logging.info("Running periodic maintenance")
                run_once(args.config)
        except Exception:
            logging.exception("Maintainer run failed")
        first_run = False

        interval = int(load_config(args.config).get("run_interval_seconds", 21600))
        logging.info("Sleeping for %s seconds", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
