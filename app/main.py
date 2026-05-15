from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
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


def load_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        create_default_config(config_path)

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def servarr_client(cfg: dict[str, Any]) -> ApiClient:
    base_url = f"http://{cfg.get('host')}:{cfg.get('port')}"
    base_path = (cfg.get("base_url") or "").strip("/")
    if base_path:
        base_url = f"{base_url}/{base_path}"
    api_key = read_api_key(cfg.get("api_key"), cfg.get("api_key_file"))
    return ApiClient(f"{base_url}/api/v3", api_key)


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


def cleanup_torrents(qbt: QBittorrentClient, cfg: dict[str, Any], protected_hashes: set[str], report: dict[str, Any]) -> None:
    if not cfg.get("cleanup_torrents", True):
        return
    dry_run = bool(cfg.get("dry_run", True))
    download_root = Path(cfg.get("download_root", "/data/downloads"))
    skip_incomplete = bool(cfg.get("skip_incomplete_torrents", True))

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
        paths = []
        has_hardlink = False
        save_path = Path(torrent.get("save_path") or str(download_root))
        for file_info in files:
            path = save_path / (file_info.get("name") or "")
            stat = stat_file(path)
            if not stat:
                continue
            _, _, _, nlink = stat
            paths.append(path)
            if nlink > 1:
                has_hardlink = True
        if has_hardlink:
            continue

        report["deleted_torrents"].append({"name": torrent.get("name"), "hash": torrent_hash, "dry_run": dry_run})
        if dry_run:
            report["torrents_would_delete"] += 1
            logging.info("[DRY-RUN] Would delete torrent with no hardlinks: %s", torrent.get("name"))
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
        "repairs": [],
        "deleted_torrents": [],
        "errors": [],
    }

    qbt = QBittorrentClient(cfg.get("qbittorrent") or {})
    downloads, _ = build_download_entries(qbt, cfg)
    media = build_media_entries(cfg)
    report["download_files_scanned"] = len(downloads)
    report["media_files_scanned"] = len(media)

    logging.info("Scanned %s download files and %s media files", len(downloads), len(media))
    protected_hashes = repair_duplicates(downloads, media, cfg, report)
    cleanup_torrents(qbt, cfg, protected_hashes, report)

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
        if first_run and not cfg.get("run_on_start", True):
            logging.info("Initial run disabled; waiting for the first interval")
        else:
            try:
                run_once(args.config)
            except Exception:
                logging.exception("Maintainer run failed")
        first_run = False

        interval = int(load_config(args.config).get("run_interval_seconds", 21600))
        logging.info("Sleeping for %s seconds", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
