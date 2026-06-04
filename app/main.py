#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import base64
import hashlib
import hmac
import html
import json
import os
import posixpath
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests
from fastapi import FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "gateway.db"
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY") or secrets.token_urlsafe(48)
ADMIN_INIT_USER = os.getenv("ADMIN_INIT_USER", "admin")
ADMIN_INIT_PASSWORD = os.getenv("ADMIN_INIT_PASSWORD", "change-me-please")
STRM_OUTPUT_DIR = os.getenv("STRM_OUTPUT_DIR", "/strm-output")
CONFIG_CACHE_SECONDS = 3

VIDEO_EXTS = {
    ".3gp", ".avi", ".divx", ".flv", ".iso", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}
SUBTITLE_EXTS = {".ass", ".srt", ".ssa", ".sub", ".vtt"}
PROBE_UA_WORDS = ("lavf", "ffmpeg", "ffprobe", "embyserver")

def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "")
    if not value:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


DEFAULT_CONFIG: dict[str, Any] = {
    "gateway_public_base_url": os.getenv("GATEWAY_PUBLIC_BASE_URL", "http://localhost:8097"),
    "emby_base_url": os.getenv("EMBY_BASE_URL", "http://host.docker.internal:8096"),
    "emby_api_key": os.getenv("EMBY_API_KEY", ""),
    "openlist_base_url": os.getenv("OPENLIST_BASE_URL", "http://host.docker.internal:5244"),
    "openlist_token": os.getenv("OPENLIST_TOKEN", ""),
    "media_sources": [],
    "strm_output_dir": STRM_OUTPUT_DIR,
    "static_token": os.getenv("STATIC_TOKEN", secrets.token_urlsafe(24)),
    "webhook_sync_token": os.getenv("WEBHOOK_SYNC_TOKEN", ""),
    "security_mode": os.getenv("SECURITY_MODE", "compatible"),
    "play_url_ttl_seconds": env_int("PLAY_URL_TTL_SECONDS", 600),
    "sync_mode": os.getenv("SYNC_MODE", "manual"),
    "sync_interval_seconds": env_int("SYNC_INTERVAL_SECONDS", 21600),
    "openlist_list_refresh": env_bool("OPENLIST_LIST_REFRESH", False),
    "delete_missing_after_count": env_int("DELETE_MISSING_AFTER_COUNT", 3),
    "probe_mode": os.getenv("PROBE_MODE", "none"),
    "probe_minimal_bytes": env_int("PROBE_MINIMAL_BYTES", 65536),
    "external_player_enabled": env_bool("EXTERNAL_PLAYER_ENABLED", True),
    "external_players": ["pot", "vlc", "mpv", "iina", "nplayer", "mx", "infuse"],
}
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)

_config_cache: tuple[float, dict[str, Any]] = (0, {})
sync_lock = threading.Lock()
last_sync: dict[str, Any] = {"status": "not_started"}

app = FastAPI(title="Emby 115 Gateway", version="0.3.0")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.mount("/admin/static", StaticFiles(directory=str(APP_DIR / "static")), name="admin-static")


def now() -> int:
    return int(time.time())


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(text: str) -> str:
    return b64(hmac.new(APP_SECRET_KEY.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).digest())


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute(
            """
            create table if not exists config (
                key text primary key,
                value text not null
            )
            """
        )
        con.execute(
            """
            create table if not exists users (
                username text primary key,
                password_hash text not null,
                must_change_password integer not null default 1,
                created_at integer not null
            )
            """
        )
        con.execute(
            """
            create table if not exists media (
                id text primary key,
                source_key text not null default '',
                openlist_path text not null unique,
                strm_path_host text not null,
                strm_path_emby text not null,
                name text not null,
                size integer,
                modified text,
                last_seen integer not null,
                missing_count integer not null default 0
            )
            """
        )
        con.execute(
            """
            create table if not exists dir_state (
                path text primary key,
                fingerprint text not null,
                last_seen integer not null
            )
            """
        )
        try:
            con.execute("alter table media add column source_key text not null default ''")
        except sqlite3.OperationalError:
            pass
        con.execute(
            """
            create table if not exists direct_cache (
                id text primary key,
                url text not null,
                expires_at integer not null
            )
            """
        )
        con.execute(
            """
            create table if not exists sync_log (
                id integer primary key autoincrement,
                started_at integer not null,
                finished_at integer,
                status text not null,
                path text,
                mode text,
                summary text,
                error text
            )
            """
        )
        row = con.execute("select username from users limit 1").fetchone()
        if not row:
            con.execute(
                "insert into users (username, password_hash, must_change_password, created_at) values (?, ?, 1, ?)",
                (ADMIN_INIT_USER, hash_password(ADMIN_INIT_PASSWORD), now()),
            )
        for key, value in DEFAULT_CONFIG.items():
            exists = con.execute("select 1 from config where key = ?", (key,)).fetchone()
            if not exists:
                con.execute("insert into config (key, value) values (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
        row = con.execute("select value from config where key = 'media_sources'").fetchone()
        if row:
            try:
                stored_sources = json.loads(row["value"])
            except json.JSONDecodeError:
                stored_sources = []
            if (
                isinstance(stored_sources, list)
                and len(stored_sources) == 1
                and isinstance(stored_sources[0], dict)
                and not stored_sources[0].get("openlist_path")
                and str(stored_sources[0].get("name") or "").startswith("默认")
            ):
                con.execute("replace into config (key, value) values ('media_sources', ?)", (json.dumps([], ensure_ascii=False),))


@app.on_event("startup")
def startup() -> None:
    init_db()
    threading.Thread(target=sync_loop, daemon=True).start()


def get_config(force: bool = False) -> dict[str, Any]:
    global _config_cache
    cached_at, cached = _config_cache
    if not force and cached and time.time() - cached_at < CONFIG_CACHE_SECONDS:
        return dict(cached)
    config = dict(DEFAULT_CONFIG)
    with db() as con:
        for row in con.execute("select key, value from config"):
            try:
                config[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                config[row["key"]] = row["value"]
    _config_cache = (time.time(), dict(config))
    return config


def save_config(values: dict[str, Any]) -> None:
    global _config_cache
    with db() as con:
        for key, value in values.items():
            if key not in DEFAULT_CONFIG:
                continue
            con.execute(
                "replace into config (key, value) values (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
    _config_cache = (0, {})


def auth_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get("egw_session", "")
    if not token or "." not in token:
        return None
    payload_b64, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(sign(payload_b64), signature):
        return None
    try:
        payload = json.loads(unb64(payload_b64))
    except Exception:
        return None
    if payload.get("exp", 0) < now():
        return None
    username = payload.get("sub")
    with db() as con:
        row = con.execute("select username, must_change_password from users where username = ?", (username,)).fetchone()
    return dict(row) if row else None


def require_user(request: Request) -> dict[str, Any]:
    user = auth_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def require_page_user(request: Request) -> dict[str, Any] | RedirectResponse:
    user = auth_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    return user


def make_session(username: str) -> str:
    payload = b64(json.dumps({"sub": username, "exp": now() + 86400 * 7}, separators=(",", ":")).encode("utf-8"))
    return payload + "." + sign(payload)


def openlist_headers(config: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = str(config.get("openlist_token") or "")
    if token:
        headers["Authorization"] = token
    return headers


def openlist_post(config: dict[str, Any], api_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = str(config["openlist_base_url"]).rstrip("/")
    resp = requests.post(base + api_path, headers=openlist_headers(config), data=json.dumps(payload), timeout=30)
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"OpenList returned non-JSON response: HTTP {resp.status_code}") from exc
    if resp.status_code >= 400 or data.get("code") != 200:
        raise RuntimeError(f"OpenList API error: HTTP {resp.status_code}, {data.get('code')} {data.get('message')}")
    return data.get("data") or {}


def list_openlist_dir(config: dict[str, Any], path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        data = openlist_post(
            config,
            "/api/fs/list",
            {
                "path": path,
                "password": "",
                "page": page,
                "per_page": 200,
                "refresh": bool(config.get("openlist_list_refresh")),
            },
        )
        content = data.get("content") or []
        items.extend(content)
        total = data.get("total")
        if not content or (isinstance(total, int) and len(items) >= total):
            return items
        page += 1


def get_openlist_download_url(config: dict[str, Any], openlist_path: str) -> str:
    data = openlist_post(config, "/api/fs/get", {"path": openlist_path, "password": ""})
    sign_value = data.get("sign")
    if not sign_value:
        raise RuntimeError(f"No OpenList sign returned for {openlist_path}")
    base = str(config["openlist_base_url"]).rstrip("/")
    return f"{base}/d{quote(openlist_path)}?sign={quote(sign_value)}"


def media_id_for(openlist_path: str) -> str:
    return hashlib.sha256(openlist_path.encode("utf-8")).hexdigest()[:24]


def infer_media_type(item: dict[str, Any]) -> str:
    media_type = str(item.get("media_type") or item.get("type") or "").strip().lower()
    if media_type in ("tv", "movie", "raw"):
        return media_type
    text = " ".join(
        str(item.get(key) or "")
        for key in ("name", "openlist_path", "output_subdir", "emby_strm_dir")
    )
    if "电视剧" in text:
        return "tv"
    if "电影" in text:
        return "movie"
    return "raw"


def media_sources(config: dict[str, Any]) -> list[dict[str, str]]:
    raw_sources = config.get("media_sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raw_sources = []
    sources: list[dict[str, Any]] = []
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"媒体源{index + 1}").strip()
        openlist_path = normalize_openlist_path(str(item.get("openlist_path") or ""))
        output_subdir = normalize_relative_path(str(item.get("output_subdir") or name))
        emby_strm_dir = str(item.get("emby_strm_dir") or "").strip().rstrip("/")
        enabled = item.get("enabled", True)
        enabled = enabled if isinstance(enabled, bool) else str(enabled).lower() in ("1", "true", "yes", "on", "启用")
        media_type = infer_media_type(item)
        tv_normalize = item.get("tv_normalize", media_type == "tv")
        tv_normalize = tv_normalize if isinstance(tv_normalize, bool) else str(tv_normalize).lower() in ("1", "true", "yes", "on", "启用")
        try:
            tv_series_depth = max(int(item.get("tv_series_depth") or 1), 1)
        except (TypeError, ValueError):
            tv_series_depth = 1
        try:
            tv_default_season = max(int(item.get("tv_default_season") or 1), 1)
        except (TypeError, ValueError):
            tv_default_season = 1
        tv_unmatched = str(item.get("tv_unmatched") or "original").strip().lower()
        if tv_unmatched not in ("original", "unmatched", "skip"):
            tv_unmatched = "original"
        source_key = hashlib.sha1(f"{name}|{openlist_path}|{output_subdir}|{emby_strm_dir}".encode("utf-8")).hexdigest()[:12]
        sources.append({
            "key": source_key,
            "name": name,
            "openlist_path": openlist_path,
            "output_subdir": output_subdir,
            "emby_strm_dir": emby_strm_dir,
            "enabled": enabled,
            "media_type": media_type,
            "tv_normalize": tv_normalize,
            "tv_series_depth": tv_series_depth,
            "tv_default_season": tv_default_season,
            "tv_unmatched": tv_unmatched,
        })
    return sources


def enabled_media_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [source for source in media_sources(config) if source.get("enabled", True)]


def normalize_relative_path(path: str) -> str:
    parts = [part for part in path.replace("\\", "/").split("/") if part and part not in (".", "..")]
    return "/".join(parts)


def normalize_openlist_path(path: str) -> str:
    rel = normalize_relative_path(path)
    return "/" + rel if rel else ""


def source_root(config: dict[str, Any], source: dict[str, str]) -> str:
    return normalize_openlist_path(str(source.get("openlist_path") or ""))


def path_under(path: str, root: str) -> bool:
    path = normalize_openlist_path(path)
    root = normalize_openlist_path(root).rstrip("/")
    return path == root or path.startswith(root + "/")


def validate_source(source: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    name = source.get("name") or "未命名媒体源"
    openlist_path = normalize_openlist_path(str(source.get("openlist_path") or ""))
    depth = len([part for part in openlist_path.split("/") if part])
    if not openlist_path:
        errors.append(f"{name}: OpenList 完整目录不能为空")
    elif depth < 4:
        errors.append(f"{name}: OpenList 目录过于宽泛，至少需要 4 级路径，例如 /Cloud/115/Media/Movies")
    if not source.get("output_subdir"):
        errors.append(f"{name}: STRM 输出子目录不能为空")
    if not source.get("emby_strm_dir"):
        errors.append(f"{name}: Emby 内 STRM 目录不能为空")
    return errors


def config_errors(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key, label in (
        ("gateway_public_base_url", "网关访问地址"),
        ("emby_base_url", "Emby 内部地址"),
        ("emby_api_key", "Emby API Key"),
        ("openlist_base_url", "OpenList 内部地址"),
        ("openlist_token", "OpenList Token"),
        ("strm_output_dir", "STRM 容器输出根目录"),
    ):
        if not str(config.get(key) or "").strip():
            errors.append(f"{label} 未配置")
    sources = enabled_media_sources(config)
    if not sources:
        errors.append("至少需要一个启用的媒体源")
    for source in sources:
        errors.extend(validate_source(source))
    try:
        output_dir = Path(str(config.get("strm_output_dir") or STRM_OUTPUT_DIR))
        output_dir.mkdir(parents=True, exist_ok=True)
        test_file = output_dir / ".emby115-write-test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except OSError as exc:
        errors.append(f"STRM 输出目录不可写: {exc}")
    return errors


def is_config_complete(config: dict[str, Any]) -> bool:
    return not config_errors(config)


def safe_rel_path(config: dict[str, Any], openlist_path: str, source: dict[str, str] | None = None) -> Path:
    root = source_root(config, source) if source else ""
    rel = openlist_path[len(root):].lstrip("/")
    parts = [part for part in rel.split("/") if part and part not in (".", "..")]
    return Path(*parts)


EPISODE_PATTERNS = (
    re.compile(r"(?i)(?<![A-Za-z0-9])S(?P<season>\d{1,2})\s*[-_. ]?\s*E(?P<episode>\d{1,4})(?![A-Za-z0-9])"),
    re.compile(r"(?i)(?<![A-Za-z0-9])(?P<season>\d{1,2})x(?P<episode>\d{1,4})(?![A-Za-z0-9])"),
    re.compile(r"(?i)(?<![A-Za-z0-9])EP?(?P<episode>\d{1,4})(?![A-Za-z0-9])"),
    re.compile(r"第\s*(?P<episode>\d{1,4})\s*[集话話]"),
)


def parse_episode_info(filename: str, default_season: int) -> tuple[int, int] | None:
    stem = Path(filename).stem
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        season = int(match.groupdict().get("season") or default_season)
        episode = int(match.group("episode"))
        if season > 0 and episode > 0:
            return season, episode
    return None


def tv_normalized_rel_path(config: dict[str, Any], openlist_path: str, source: dict[str, Any]) -> Path | None:
    original_rel = safe_rel_path(config, openlist_path, source)
    parts = list(original_rel.parts)
    if not parts:
        return None
    try:
        series_depth = max(int(source.get("tv_series_depth") or 1), 1)
    except (TypeError, ValueError):
        series_depth = 1
    if len(parts) <= series_depth:
        return None
    series_parts = parts[:series_depth]
    filename = parts[-1]
    try:
        default_season = max(int(source.get("tv_default_season") or 1), 1)
    except (TypeError, ValueError):
        default_season = 1
    episode = parse_episode_info(filename, default_season)
    if not episode:
        unmatched = str(source.get("tv_unmatched") or "original")
        if unmatched == "skip":
            return None
        if unmatched == "unmatched":
            return Path(*series_parts) / "_Unmatched" / filename
        return original_rel
    season, _ = episode
    return Path(*series_parts) / f"Season {season:02d}" / filename


def output_rel_path(config: dict[str, Any], openlist_path: str, source: dict[str, Any], target_suffix: str | None = None) -> Path | None:
    rel: Path | None
    if source.get("media_type") == "tv" and source.get("tv_normalize", True):
        rel = tv_normalized_rel_path(config, openlist_path, source)
    else:
        rel = safe_rel_path(config, openlist_path, source)
    if rel is None:
        return None
    if target_suffix:
        rel = rel.with_suffix(target_suffix)
    return fit_path_for_fs(rel)


def truncate_utf8(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    result = ""
    used = 0
    for char in text:
        char_len = len(char.encode("utf-8"))
        if used + char_len > max_bytes:
            break
        result += char
        used += char_len
    return result.rstrip(" ._-") or "file"


def safe_fs_name(name: str, max_bytes: int = 180) -> str:
    clean = "".join("_" if char in '<>:"/\\|?*\0' else char for char in name).strip()
    clean = clean or "file"
    if len(clean.encode("utf-8")) <= max_bytes:
        return clean
    suffix = "".join(Path(clean).suffixes[-1:])
    stem = clean[: -len(suffix)] if suffix else clean
    digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:10]
    year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", stem)
    year = year_match.group(1) if year_match else ""
    title = stem[:year_match.end()] if year_match else re.split(r"\b(?:2160p|1080p|720p|WEB|BluRay|REMUX|HEVC|H\.?265|H\.?264)\b", stem, 1, flags=re.IGNORECASE)[0]
    title = title.strip(" ._-") or stem
    year_part = f"-{year}" if year and year not in title else ""
    reserved = len((year_part + "-" + digest + suffix).encode("utf-8"))
    prefix = truncate_utf8(title, max(max_bytes - reserved, 24))
    return f"{prefix}{year_part}-{digest}{suffix}"


def fit_path_for_fs(path: Path) -> Path:
    return Path(*(safe_fs_name(part) for part in path.parts))


def strm_url(config: dict[str, Any], media_id: str, openlist_path: str) -> str:
    source_name = quote(posixpath.basename(openlist_path))
    return f"{str(config['gateway_public_base_url']).rstrip('/')}/strm/{quote(media_id)}/{source_name}?token={quote(str(config['static_token']))}"


def write_strm(config: dict[str, Any], media_id: str, openlist_path: str, source: dict[str, str]) -> tuple[str, str]:
    emby_rel = output_rel_path(config, openlist_path, source, ".strm")
    if emby_rel is None:
        raise RuntimeError(f"无法识别电视剧集数，已按配置跳过: {openlist_path}")
    rel = emby_rel
    output_subdir = normalize_relative_path(source.get("output_subdir", ""))
    if output_subdir:
        rel = Path(output_subdir) / rel
    host_root = Path(str(config.get("strm_output_dir") or STRM_OUTPUT_DIR))
    emby_root = str(source.get("emby_strm_dir") or "").rstrip("/")
    host_path = host_root / rel
    emby_path = posixpath.join(emby_root, emby_rel.as_posix()) if emby_root else rel.as_posix()
    host_path.parent.mkdir(parents=True, exist_ok=True)
    content = strm_url(config, media_id, openlist_path) + "\n"
    if not host_path.exists() or host_path.read_text(encoding="utf-8") != content:
        host_path.write_text(content, encoding="utf-8")
    return str(host_path), emby_path


def rewrite_strm_urls(source_key: str | None = None) -> dict[str, Any]:
    config = get_config(force=True)
    started = now()
    stats: dict[str, Any] = {
        "status": "running",
        "mode": "rewrite_strm_url",
        "path": "all media" if not source_key else f"source:{source_key}",
        "checked": 0,
        "rewritten": 0,
        "unchanged": 0,
        "missing_files": 0,
        "errors": 0,
        "synced_at": started,
    }
    with db() as con:
        cur = con.execute(
            "insert into sync_log (started_at, status, path, mode) values (?, 'running', ?, 'rewrite_strm_url')",
            (started, stats["path"]),
        )
        log_id = cur.lastrowid
    try:
        with sync_lock:
            with db() as con:
                if source_key:
                    rows = con.execute(
                        "select id, openlist_path, strm_path_host from media where source_key = ? order by name",
                        (source_key,),
                    ).fetchall()
                else:
                    rows = con.execute(
                        "select id, openlist_path, strm_path_host from media order by name",
                    ).fetchall()
            output_root = Path(str(config.get("strm_output_dir") or STRM_OUTPUT_DIR)).resolve()
            for row in rows:
                stats["checked"] += 1
                try:
                    path = Path(row["strm_path_host"])
                    resolved = path.resolve()
                    if not resolved.is_relative_to(output_root) or not path.exists() or not path.is_file():
                        stats["missing_files"] += 1
                        continue
                    content = strm_url(config, row["id"], row["openlist_path"]) + "\n"
                    if path.read_text(encoding="utf-8") == content:
                        stats["unchanged"] += 1
                        continue
                    path.write_text(content, encoding="utf-8")
                    stats["rewritten"] += 1
                except OSError:
                    stats["errors"] += 1
        stats.update({"status": "ok", "finished_at": now()})
        with db() as con:
            con.execute(
                "update sync_log set finished_at = ?, status = 'ok', summary = ? where id = ?",
                (now(), json.dumps(stats, ensure_ascii=False), log_id),
            )
        last_sync.clear()
        last_sync.update(stats)
        return stats
    except Exception as exc:
        stats.update({"status": "failed", "error": str(exc), "finished_at": now()})
        with db() as con:
            con.execute("update sync_log set finished_at = ?, status = 'failed', error = ? where id = ?", (now(), str(exc), log_id))
        last_sync.clear()
        last_sync.update(stats)
        raise


def upsert_media(config: dict[str, Any], item: dict[str, Any], openlist_path: str, source: dict[str, str], seen_at: int) -> bool:
    if output_rel_path(config, openlist_path, source, ".strm") is None:
        return False
    media_id = media_id_for(openlist_path)
    host_path, emby_path = write_strm(config, media_id, openlist_path, source)
    with db() as con:
        old = con.execute("select strm_path_host from media where id = ?", (media_id,)).fetchone()
        if old and old["strm_path_host"] and old["strm_path_host"] != host_path:
            try:
                old_path = Path(old["strm_path_host"])
                output_root = Path(str(config.get("strm_output_dir") or STRM_OUTPUT_DIR)).resolve()
                if old_path.resolve().is_relative_to(output_root) and old_path.exists() and old_path.is_file():
                    old_path.unlink()
            except OSError:
                pass
        con.execute(
            """
            insert into media (id, source_key, openlist_path, strm_path_host, strm_path_emby, name, size, modified, last_seen, missing_count)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            on conflict(id) do update set
                source_key=excluded.source_key,
                openlist_path=excluded.openlist_path,
                strm_path_host=excluded.strm_path_host,
                strm_path_emby=excluded.strm_path_emby,
                name=excluded.name,
                size=excluded.size,
                modified=excluded.modified,
                last_seen=excluded.last_seen,
                missing_count=0
            """,
            (
                media_id,
                source.get("key", ""),
                openlist_path,
                host_path,
                emby_path,
                item.get("name") or posixpath.basename(openlist_path),
                item.get("size"),
                item.get("modified"),
                seen_at,
            ),
        )
    return True


def dir_fingerprint(items: list[dict[str, Any]]) -> str:
    parts = []
    for item in sorted(items, key=lambda value: str(value.get("name") or "")):
        parts.append("|".join([
            str(item.get("name") or ""),
            "d" if item.get("is_dir") else "f",
            str(item.get("size") or ""),
            str(item.get("modified") or ""),
        ]))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def dir_changed(config: dict[str, Any], path: str, fingerprint: str, seen_at: int, force: bool) -> bool:
    mode = str(config.get("sync_mode") or "full").lower()
    if force or mode not in ("smart", "incremental"):
        with db() as con:
            con.execute("replace into dir_state (path, fingerprint, last_seen) values (?, ?, ?)", (path, fingerprint, seen_at))
        return True
    with db() as con:
        row = con.execute("select fingerprint from dir_state where path = ?", (path,)).fetchone()
        changed = not row or row["fingerprint"] != fingerprint
        con.execute("replace into dir_state (path, fingerprint, last_seen) values (?, ?, ?)", (path, fingerprint, seen_at))
    return changed


def mark_seen_under_path(path: str, seen_at: int) -> int:
    prefix = path.rstrip("/") + "/"
    with db() as con:
        con.execute(
            "update media set last_seen = ?, missing_count = 0 where openlist_path = ? or openlist_path like ?",
            (seen_at, path, prefix + "%"),
        )
        return con.total_changes


def sync_subtitle(config: dict[str, Any], item: dict[str, Any], openlist_path: str, source: dict[str, str], stats: dict[str, Any]) -> None:
    rel = output_rel_path(config, openlist_path, source)
    if rel is None:
        stats["tv_unmatched"] = stats.get("tv_unmatched", 0) + 1
        return
    output_subdir = normalize_relative_path(source.get("output_subdir", ""))
    if output_subdir:
        rel = Path(output_subdir) / rel
    subtitle_path = Path(str(config.get("strm_output_dir") or STRM_OUTPUT_DIR)) / rel
    expected_size = item.get("size")
    if subtitle_path.exists() and isinstance(expected_size, int) and subtitle_path.stat().st_size == expected_size:
        return
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    direct_url = get_openlist_download_url(config, openlist_path)
    with requests.get(direct_url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        tmp_path = subtitle_path.with_suffix(subtitle_path.suffix + ".tmp")
        with tmp_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)
        tmp_path.replace(subtitle_path)
    stats["subtitles_synced"] += 1


def scan_dir(config: dict[str, Any], path: str, source: dict[str, str], seen_at: int, stats: dict[str, Any], force: bool) -> None:
    items = list_openlist_dir(config, path)
    stats["listed_dirs"] += 1
    fingerprint = dir_fingerprint(items)
    if not dir_changed(config, path, fingerprint, seen_at, force):
        stats["skipped_dirs"] += 1
        stats["kept_by_dir_cache"] += mark_seen_under_path(path, seen_at)
        return
    for item in items:
        name = item.get("name")
        if not name:
            continue
        child_path = posixpath.join(path, name)
        if item.get("is_dir"):
            stats["dirs"] += 1
            scan_dir(config, child_path, source, seen_at, stats, force)
            continue
        ext = Path(name).suffix.lower()
        if ext in VIDEO_EXTS:
            stats["videos"] += 1
            if source.get("media_type") == "tv" and source.get("tv_normalize", True):
                if parse_episode_info(name, int(source.get("tv_default_season") or 1)):
                    stats["tv_normalized"] += 1
                else:
                    stats["tv_unmatched"] += 1
            if not upsert_media(config, item, child_path, source, seen_at):
                stats["videos_skipped"] += 1
        elif ext in SUBTITLE_EXTS:
            stats["subtitles"] += 1
            sync_subtitle(config, item, child_path, source, stats)


def cleanup_missing(config: dict[str, Any], seen_at: int, cleanup: bool) -> tuple[int, int]:
    if not cleanup:
        return 0, 0
    removed = 0
    with db() as con:
        con.execute("update media set missing_count = missing_count + 1 where last_seen < ?", (seen_at,))
        marked = con.total_changes
        rows = con.execute(
            "select id, strm_path_host from media where last_seen < ? and missing_count >= ?",
            (seen_at, max(int(config.get("delete_missing_after_count") or 3), 1)),
        ).fetchall()
        for row in rows:
            try:
                path = Path(row["strm_path_host"])
                if path.exists() and path.is_file():
                    path.unlink()
            except OSError:
                pass
            con.execute("delete from media where id = ?", (row["id"],))
            con.execute("delete from direct_cache where id = ?", (row["id"],))
            removed += 1
    return removed, marked


def cleanup_orphan_strm(config: dict[str, Any], source: dict[str, Any]) -> int:
    output_subdir = normalize_relative_path(str(source.get("output_subdir") or ""))
    if not output_subdir:
        return 0
    root = (Path(str(config.get("strm_output_dir") or STRM_OUTPUT_DIR)) / output_subdir).resolve()
    if not root.exists() or not root.is_dir():
        return 0
    with db() as con:
        rows = con.execute("select strm_path_host from media where source_key = ?", (source.get("key", ""),)).fetchall()
    keep = {str(Path(row["strm_path_host"]).resolve()) for row in rows}
    removed = 0
    for path in root.rglob("*.strm"):
        try:
            resolved = str(path.resolve())
            if resolved in keep:
                continue
            if path.resolve().is_relative_to(root):
                path.unlink()
                removed += 1
        except OSError:
            pass
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            if path.resolve().is_relative_to(root):
                path.rmdir()
        except OSError:
            pass
    return removed


def sync_once(path: str | None = None, force: bool = False, cleanup: bool | None = None, source_key: str | None = None) -> dict[str, Any]:
    config = get_config(force=True)
    started = now()
    sources = enabled_media_sources(config)
    selected_sources = [item for item in sources if not source_key or item["key"] == source_key]
    if not selected_sources:
        raise RuntimeError(f"media source not found: {source_key}")
    sync_path = (path or "").rstrip("/")
    if sync_path:
        sync_path = normalize_openlist_path(sync_path)
    cleanup_enabled = cleanup if cleanup is not None else not sync_path and not source_key
    stats: dict[str, Any] = {
        "status": "running",
        "mode": config.get("sync_mode"),
        "path": sync_path or "all sources",
        "force": force,
        "sources": 0,
        "dirs": 0,
        "listed_dirs": 0,
        "skipped_dirs": 0,
        "kept_by_dir_cache": 0,
        "videos": 0,
        "subtitles": 0,
        "subtitles_synced": 0,
        "videos_skipped": 0,
        "tv_normalized": 0,
        "tv_unmatched": 0,
        "orphan_strm_removed": 0,
        "synced_at": started,
    }
    with db() as con:
        cur = con.execute(
            "insert into sync_log (started_at, status, path, mode) values (?, 'running', ?, ?)",
            (started, sync_path or "all sources", str(config.get("sync_mode"))),
        )
        log_id = cur.lastrowid
    try:
        with sync_lock:
            for source in selected_sources:
                source_path = sync_path or source_root(config, source)
                if not path_under(source_path, source_root(config, source)):
                    raise RuntimeError(f"sync path must be under media source root: {source_path}")
                stats["sources"] += 1
                scan_dir(config, source_path, source, started, stats, force)
                if force:
                    stats["orphan_strm_removed"] += cleanup_orphan_strm(config, source)
            removed, missing_marked = cleanup_missing(config, started, cleanup_enabled)
        stats.update({"status": "ok", "removed": removed, "missing_marked": missing_marked, "finished_at": now()})
        with db() as con:
            con.execute(
                "update sync_log set finished_at = ?, status = 'ok', summary = ? where id = ?",
                (now(), json.dumps(stats, ensure_ascii=False), log_id),
            )
        last_sync.clear()
        last_sync.update(stats)
        return stats
    except Exception as exc:
        stats.update({"status": "failed", "error": str(exc), "finished_at": now()})
        with db() as con:
            con.execute("update sync_log set finished_at = ?, status = 'failed', error = ? where id = ?", (now(), str(exc), log_id))
        last_sync.clear()
        last_sync.update(stats)
        raise


def sync_loop() -> None:
    while True:
        try:
            config = get_config(force=True)
            mode = str(config.get("sync_mode") or "full").lower()
            configured = is_config_complete(config)
            if mode in ("webhook", "webhook_only", "manual") or not configured:
                last_sync.update({"status": "idle", "mode": mode, "synced_at": now()})
            else:
                print(f"sync ok: {sync_once()}", flush=True)
            interval = max(int(config.get("sync_interval_seconds") or 21600), 60)
        except Exception as exc:
            last_sync.update({"status": "failed", "error": str(exc), "synced_at": now()})
            interval = 300
            print(f"sync failed: {exc}", flush=True)
        time.sleep(interval)


def media_by_id(media_id: str) -> tuple[str, str]:
    with db() as con:
        row = con.execute("select openlist_path, name from media where id = ?", (media_id,)).fetchone()
    if not row:
        raise KeyError(media_id)
    return row["openlist_path"], row["name"]


def media_id_by_strm_path(config: dict[str, Any], path: str) -> str | None:
    candidates = [path]
    output_root = Path(str(config.get("strm_output_dir") or STRM_OUTPUT_DIR))
    for source in media_sources(config):
        emby_root = str(source.get("emby_strm_dir") or "").rstrip("/")
        output_subdir = normalize_relative_path(str(source.get("output_subdir") or ""))
        if emby_root and path.startswith(emby_root + "/"):
            rel = path[len(emby_root):].lstrip("/")
            candidate = output_root / Path(output_subdir) / Path(rel) if output_subdir else output_root / Path(rel)
            candidates.append(str(candidate))
    with db() as con:
        for candidate in candidates:
            row = con.execute(
                "select id from media where strm_path_emby = ? or strm_path_host = ?",
                (candidate, candidate),
            ).fetchone()
            if row:
                return row["id"]
    return None


def media_id_from_strm_url(path: str) -> str | None:
    match = __import__("re").search(r"/strm/([A-Za-z0-9_-]+)/", path)
    if match:
        return match.group(1)
    query = parse_qs(urlparse(path).query)
    values = query.get("id")
    return values[0] if values and values[0] else None


def token_from_uri(config: dict[str, Any], uri: str) -> str:
    query = parse_qs(urlparse(uri).query)
    for key in ("api_key", "X-Emby-Token", "x-emby-token"):
        values = query.get(key)
        if values and values[0]:
            return values[0]
    return str(config.get("emby_api_key") or "")


def emby_media_source_path(config: dict[str, Any], item_id: str, original_uri: str = "") -> str | None:
    token = token_from_uri(config, original_uri)
    if not token:
        return None
    base = str(config["emby_base_url"]).rstrip("/")
    params = {
        "api_key": token,
        "StartTimeTicks": 0,
        "IsPlayback": "false",
        "AutoOpenLiveStream": "false",
        "MaxStreamingBitrate": 200000000,
        "reqformat": "json",
    }
    for url in (f"{base}/Items/{item_id}/PlaybackInfo", f"{base}/emby/Items/{item_id}/PlaybackInfo"):
        try:
            resp = requests.post(url, params=params, json={}, timeout=20)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            for source in data.get("MediaSources") or []:
                path = source.get("Path")
                if path:
                    return path
        except Exception as exc:
            print(f"emby playbackinfo lookup failed for {item_id}: {exc}", flush=True)
    return None


def media_id_by_item_id(config: dict[str, Any], item_id: str, original_uri: str = "") -> str | None:
    source_path = emby_media_source_path(config, item_id, original_uri)
    if source_path:
        return media_id_from_strm_url(source_path) or media_id_by_strm_path(config, source_path)
    return None


def gateway_openlist_redirect_url(config: dict[str, Any], media_id: str) -> str:
    openlist_path, name = media_by_id(media_id)
    return f"{str(config['gateway_public_base_url']).rstrip('/')}/openlist-redirect/{quote(media_id)}/{quote(posixpath.basename(openlist_path) or name)}?token={quote(str(config['static_token']))}"


def play_signature(media_id: str, exp: int) -> str:
    return sign(f"play:{media_id}:{exp}")


def gateway_play_url(config: dict[str, Any], media_id: str) -> str:
    openlist_path, name = media_by_id(media_id)
    exp = now() + max(int(config.get("play_url_ttl_seconds") or 600), 60)
    return f"{str(config['gateway_public_base_url']).rstrip('/')}/play/{quote(media_id)}/{quote(posixpath.basename(openlist_path) or name)}?exp={exp}&sig={quote(play_signature(media_id, exp))}"


def item_id_from_uri(uri: str) -> str | None:
    match = __import__("re").search(r"/(?:emby/)?videos/(\d+)/original", uri, __import__("re").IGNORECASE)
    return match.group(1) if match else None


def is_probe_request(request: Request) -> bool:
    ua = request.headers.get("user-agent", "").lower()
    return any(word in ua for word in PROBE_UA_WORDS) or bool(request.headers.get("x-emby-token") and request.headers.get("range"))


def blocked_probe_response(request: Request) -> Response:
    headers = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes"}
    if request.method.upper() == "HEAD":
        headers["Content-Length"] = "0"
        return Response(status_code=200, headers=headers)
    return Response(status_code=204, headers=headers)


def openlist_client_redirect(media_id: str, token: str, request: Request) -> RedirectResponse:
    config = get_config()
    if token != str(config.get("static_token")):
        raise HTTPException(status_code=403, detail="invalid token")
    openlist_path, _ = media_by_id(media_id)
    openlist_url = get_openlist_download_url(config, openlist_path)
    headers = {}
    for key in ("user-agent", "range", "accept"):
        value = request.headers.get(key)
        if value:
            headers[key.title()] = value
    resp = requests.get(openlist_url, headers=headers, allow_redirects=False, stream=True, timeout=30)
    try:
        if 300 <= resp.status_code < 400 and resp.headers.get("location"):
            return RedirectResponse(urljoin(openlist_url, resp.headers["location"]), status_code=302)
        if resp.status_code < 400:
            return RedirectResponse(openlist_url, status_code=302)
        body = resp.content[:500].decode("utf-8", "replace")
        raise HTTPException(status_code=502, detail=f"OpenList redirect failed: HTTP {resp.status_code}, {body}")
    finally:
        resp.close()


def html_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="/admin/static/admin.css">
</head>
<body>{body}</body>
</html>"""
    )


@app.get("/admin/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return html_page(
        "登录",
        """
        <main class="login">
          <form method="post" action="/admin/login" class="panel">
            <h1>Emby 115 Gateway</h1>
            <label>账号<input name="username" value="admin" autocomplete="username"></label>
            <label>密码<input name="password" type="password" autocomplete="current-password"></label>
            <button type="submit">登录</button>
            <p>默认账号：admin，默认密码：change-me-please。首次部署后请立即修改。</p>
          </form>
        </main>
        """,
    )


@app.post("/admin/login")
def login(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    with db() as con:
        row = con.execute("select username, password_hash from users where username = ?", (username,)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return RedirectResponse("/admin/login", status_code=303)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("egw_session", make_session(username), httponly=True, samesite="lax", max_age=86400 * 7, secure=SESSION_COOKIE_SECURE)
    return resp


@app.get("/admin/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("egw_session")
    return resp


def nav() -> str:
    return """<nav><a href="/admin">概览</a><a href="/admin/connections">连接配置</a><a href="/admin/sources">媒体源</a><a href="/admin/security">安全</a><a href="/admin/sync">同步</a><a href="/admin/password">密码</a><a href="/admin/logout">退出</a></nav>"""


@app.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request) -> Response:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    config = get_config(force=True)
    with db() as con:
        media_count = con.execute("select count(*) n from media").fetchone()["n"]
        logs = con.execute("select * from sync_log order by id desc limit 8").fetchall()
    configured = is_config_complete(config)
    rows = "".join(
        f"<tr><td>{r['id']}</td><td>{r['status']}</td><td>{r['path'] or ''}</td><td>{r['started_at']}</td><td>{r['finished_at'] or ''}</td><td>{(r['error'] or '')[:80]}</td></tr>"
        for r in logs
    )
    return html_page(
        "管理后台",
        f"""
        {nav()}
        <main>
          <h1>概览</h1>
          <section class="grid">
            <div class="card"><b>媒体索引</b><span>{media_count}</span></div>
            <div class="card"><b>配置状态</b><span>{"已配置" if configured else "待配置"}</span></div>
            <div class="card"><b>同步模式</b><span>{config.get('sync_mode')}</span></div>
            <div class="card"><b>探测模式</b><span>{config.get('probe_mode')}</span></div>
            <div class="card"><b>最近同步</b><span>{last_sync.get('status')}</span></div>
          </section>
          <h2>最近同步日志</h2>
          <table><thead><tr><th>ID</th><th>状态</th><th>路径</th><th>开始</th><th>结束</th><th>错误</th></tr></thead><tbody>{rows}</tbody></table>
        </main>
        """,
    )


def render_messages(messages: list[str], kind: str = "notice") -> str:
    if not messages:
        return ""
    items = "".join(f"<li>{html.escape(message)}</li>" for message in messages)
    return f"<div class='{kind}'><ul>{items}</ul></div>"


def json_ok(message: str, **extra: Any) -> dict[str, Any]:
    data = {"ok": True, "message": message}
    data.update(extra)
    return data


def json_error(message: str, **extra: Any) -> dict[str, Any]:
    data = {"ok": False, "message": message}
    data.update(extra)
    return data


def test_emby_connection(base: str, api_key: str) -> dict[str, Any]:
    try:
        if not base or not api_key:
            raise RuntimeError("Emby 地址和 API Key 都不能为空")
        resp = requests.get(f"{base.rstrip('/')}/System/Info", params={"api_key": api_key}, timeout=10)
        resp.raise_for_status()
        info = resp.json()
        return json_ok(f"Emby 连接成功：{info.get('ServerName') or info.get('Id') or 'OK'}")
    except Exception as exc:
        return json_error(f"Emby 连接失败：{exc}")


def test_openlist_connection(base: str, token: str) -> dict[str, Any]:
    try:
        if not base or not token:
            raise RuntimeError("OpenList 地址和 Token 都不能为空")
        resp = requests.get(base.rstrip("/") + "/api/me", headers={"Authorization": token}, timeout=10)
        resp.raise_for_status()
        return json_ok("OpenList 连接成功：Token 可用")
    except Exception as exc:
        return json_error(f"OpenList 连接失败：{exc}")


@app.get("/admin/config")
def legacy_config_page() -> RedirectResponse:
    return RedirectResponse("/admin/connections", status_code=303)


@app.get("/admin/connections", response_class=HTMLResponse)
def connections_page(request: Request, message: str = "") -> Response:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    c = get_config(force=True)
    def val(key: str) -> str:
        return html.escape(str(c.get(key, "")))
    return html_page(
        "连接配置",
        f"""
        {nav()}
        <main>
          <h1>连接配置</h1>
          {render_messages([message] if message else [])}
          <form method="post" action="/admin/connections" class="form">
            <section class="form-section">
              <h2>网关</h2>
              <label>网关访问地址<input name="gateway_public_base_url" value="{val('gateway_public_base_url')}"></label>
              <label>STRM 容器输出根目录<input name="strm_output_dir" value="{val('strm_output_dir')}"></label>
            </section>
            <section class="form-section">
              <h2>Emby</h2>
              <label>Emby 内部地址<input name="emby_base_url" value="{val('emby_base_url')}"></label>
              <label>Emby API Key<input name="emby_api_key" value="{val('emby_api_key')}"></label>
            </section>
            <section class="form-section">
              <h2>OpenList</h2>
              <label>OpenList 内部地址<input name="openlist_base_url" value="{val('openlist_base_url')}"></label>
              <label>OpenList Token<input name="openlist_token" value="{val('openlist_token')}"></label>
            </section>
            <div class="actions">
              <button>保存连接配置</button>
              <button type="button" data-test="emby">测试 Emby</button>
              <button type="button" data-test="openlist">测试 OpenList</button>
            </div>
            <div id="connection-test-result" class="inline-result" hidden></div>
          </form>
        </main>
        <script>
        (function () {{
          const form = document.querySelector('form[action="/admin/connections"]');
          const result = document.getElementById('connection-test-result');
          async function runTest(kind) {{
            result.hidden = false;
            result.className = 'inline-result';
            result.textContent = '测试中...';
            const data = new FormData(form);
            const resp = await fetch('/admin/api/test/' + kind, {{ method: 'POST', body: data }});
            const payload = await resp.json();
            result.className = 'inline-result ' + (payload.ok ? 'ok' : 'fail');
            result.textContent = payload.message || (payload.ok ? '测试成功' : '测试失败');
          }}
          form.querySelector('[data-test="emby"]').addEventListener('click', () => runTest('emby'));
          form.querySelector('[data-test="openlist"]').addEventListener('click', () => runTest('openlist'));
        }})();
        </script>
        """,
    )


@app.post("/admin/connections")
async def save_connections_page(request: Request) -> RedirectResponse:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    values = {
        "gateway_public_base_url": str(form.get("gateway_public_base_url", "")).strip(),
        "strm_output_dir": str(form.get("strm_output_dir", "")).strip() or STRM_OUTPUT_DIR,
        "emby_base_url": str(form.get("emby_base_url", "")).strip(),
        "emby_api_key": str(form.get("emby_api_key", "")).strip(),
        "openlist_base_url": str(form.get("openlist_base_url", "")).strip(),
        "openlist_token": str(form.get("openlist_token", "")).strip(),
    }
    save_config(values)
    return RedirectResponse("/admin/connections?message=连接配置已保存", status_code=303)


@app.post("/admin/test/emby")
async def test_emby_page(request: Request) -> Response:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    base = str(form.get("emby_base_url", "")).strip().rstrip("/")
    api_key = str(form.get("emby_api_key", "")).strip()
    messages = [test_emby_connection(base, api_key)["message"]]
    return html_page("测试 Emby", f"{nav()}<main><h1>测试 Emby</h1>{render_messages(messages)}<p><a href='/admin/connections'>返回连接配置</a></p></main>")


@app.post("/admin/api/test/emby")
async def api_test_emby(request: Request) -> dict[str, Any]:
    require_user(request)
    form = await request.form()
    return test_emby_connection(str(form.get("emby_base_url", "")).strip(), str(form.get("emby_api_key", "")).strip())


@app.post("/admin/test/openlist")
async def test_openlist_page(request: Request) -> Response:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    messages = [test_openlist_connection(str(form.get("openlist_base_url", "")).strip(), str(form.get("openlist_token", "")).strip())["message"]]
    return html_page("测试 OpenList", f"{nav()}<main><h1>测试 OpenList</h1>{render_messages(messages)}<p><a href='/admin/connections'>返回连接配置</a></p></main>")


@app.post("/admin/api/test/openlist")
async def api_test_openlist(request: Request) -> dict[str, Any]:
    require_user(request)
    form = await request.form()
    return test_openlist_connection(str(form.get("openlist_base_url", "")).strip(), str(form.get("openlist_token", "")).strip())


def parse_source_payload(raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return [], ["媒体源数据格式错误，请刷新页面后重试"]
    if not isinstance(items, list):
        return [], ["媒体源数据格式错误，请刷新页面后重试"]
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        openlist_path = str(item.get("openlist_path") or "").strip()
        output_subdir = str(item.get("output_subdir") or "").strip()
        emby_strm_dir = str(item.get("emby_strm_dir") or "").strip()
        enabled = bool(item.get("enabled", True))
        media_type = infer_media_type(item)
        tv_normalize = item.get("tv_normalize", media_type == "tv")
        tv_normalize = tv_normalize if isinstance(tv_normalize, bool) else str(tv_normalize).lower() in ("1", "true", "yes", "on", "启用")
        try:
            tv_series_depth = max(int(item.get("tv_series_depth") or 1), 1)
        except (TypeError, ValueError):
            tv_series_depth = 1
        try:
            tv_default_season = max(int(item.get("tv_default_season") or 1), 1)
        except (TypeError, ValueError):
            tv_default_season = 1
        tv_unmatched = str(item.get("tv_unmatched") or "original").strip().lower()
        if tv_unmatched not in ("original", "unmatched", "skip"):
            tv_unmatched = "original"
        if not any((name, openlist_path, output_subdir, emby_strm_dir)):
            continue
        source = {
            "name": name or f"媒体源{index + 1}",
            "openlist_path": normalize_openlist_path(openlist_path),
            "output_subdir": normalize_relative_path(output_subdir),
            "emby_strm_dir": emby_strm_dir.rstrip("/"),
            "enabled": enabled,
            "media_type": media_type,
            "tv_normalize": tv_normalize,
            "tv_series_depth": tv_series_depth,
            "tv_default_season": tv_default_season,
            "tv_unmatched": tv_unmatched,
        }
        errors.extend(validate_source(source))
        sources.append(source)
    return sources, errors


@app.get("/admin/sources", response_class=HTMLResponse)
def sources_page(request: Request, error: str = "", message: str = "") -> Response:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    c = get_config(force=True)
    sources = media_sources(c)
    source_data = json.dumps(sources, ensure_ascii=False).replace("</", "<\\/")
    return html_page(
        "媒体源",
        f"""
        {nav()}
        <main>
          <h1>媒体源</h1>
          {render_messages([error] if error else [], "error")}
          {render_messages([message] if message else [])}
          <form method="post" action="/admin/sources" class="form">
            <input type="hidden" name="sources_json" id="sources-json">
            <section class="form-section">
              <div class="section-head">
                <h2>媒体源列表</h2>
                <div class="actions">
                  <button type="button" id="add-source-row">添加媒体源</button>
                </div>
              </div>
              <table class="source-list">
                <thead><tr><th>状态</th><th>类型</th><th>名称</th><th>OpenList 完整目录</th><th>STRM 输出</th><th>Emby 内 STRM 目录</th><th>操作</th></tr></thead>
                <tbody id="source-rows"></tbody>
              </table>
              <div class="pager"><button type="button" class="secondary" id="prev-page">上一页</button><span id="page-info"></span><button type="button" class="secondary" id="next-page">下一页</button></div>
              <p>OpenList 完整目录不能为空，也不能过于宽泛；建议至少类似 /Cloud/115/Media/Movies。空白行会被忽略。</p>
            </section>
          </form>
        </main>
        <div class="modal-backdrop" id="source-modal" hidden>
          <div class="modal">
            <h2 id="source-modal-title">添加媒体源</h2>
            <label>名称<input id="modal-name" placeholder="电影"></label>
            <label>OpenList 完整目录<input id="modal-openlist-path" placeholder="/Cloud/115/Media/Movies"></label>
            <label>STRM 输出子目录<input id="modal-output-subdir" placeholder="Movies"></label>
            <label>Emby 内 STRM 目录<input id="modal-emby-strm-dir" placeholder="/media/Strm115/Movies"></label>
            <label>媒体类型<select id="modal-media-type">
              <option value="raw">原样同步</option>
              <option value="movie">电影</option>
              <option value="tv">电视剧</option>
            </select></label>
            <div class="tv-options" id="tv-options">
              <label class="inline-check"><input type="checkbox" id="modal-tv-normalize"> 电视剧目录归一化</label>
              <label>剧名目录层级<input id="modal-tv-series-depth" type="number" min="1" value="1"></label>
              <label>默认季号<input id="modal-tv-default-season" type="number" min="1" value="1"></label>
              <label>无法识别集数时<select id="modal-tv-unmatched">
                <option value="original">原样输出</option>
                <option value="unmatched">放入 _Unmatched</option>
                <option value="skip">跳过并记录</option>
              </select></label>
            </div>
            <label class="inline-check"><input type="checkbox" id="modal-enabled" checked> 启用</label>
            <div class="actions">
              <button type="button" id="modal-save">保存</button>
              <button type="button" class="secondary" id="modal-cancel">取消</button>
            </div>
          </div>
        </div>
        <script id="source-data" type="application/json">{source_data}</script>
        <script>
        (function () {{
          const pageSize = 10;
          let sources = JSON.parse(document.getElementById('source-data').textContent || '[]');
          let page = 1;
          let editingIndex = -1;
          const tbody = document.getElementById('source-rows');
          const modal = document.getElementById('source-modal');
          const fields = {{
            name: document.getElementById('modal-name'),
            openlist_path: document.getElementById('modal-openlist-path'),
            output_subdir: document.getElementById('modal-output-subdir'),
            emby_strm_dir: document.getElementById('modal-emby-strm-dir'),
            media_type: document.getElementById('modal-media-type'),
            tv_normalize: document.getElementById('modal-tv-normalize'),
            tv_series_depth: document.getElementById('modal-tv-series-depth'),
            tv_default_season: document.getElementById('modal-tv-default-season'),
            tv_unmatched: document.getElementById('modal-tv-unmatched'),
            enabled: document.getElementById('modal-enabled')
          }};
          function escapeHtml(text) {{
            return String(text || '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
          }}
          function render() {{
            const totalPages = Math.max(Math.ceil(sources.length / pageSize), 1);
            page = Math.min(Math.max(page, 1), totalPages);
            const start = (page - 1) * pageSize;
            const pageItems = sources.slice(start, start + pageSize);
            tbody.innerHTML = pageItems.map((source, offset) => {{
              const index = start + offset;
              return `<tr>
                <td><button type="button" class="${{source.enabled ? 'secondary' : 'danger'}}" data-toggle="${{index}}">${{source.enabled ? '启用' : '停用'}}</button></td>
                <td>${{source.media_type === 'tv' ? '电视剧' : source.media_type === 'movie' ? '电影' : '原样'}}</td>
                <td>${{escapeHtml(source.name)}}</td>
                <td>${{escapeHtml(source.openlist_path)}}</td>
                <td>/strm-output/${{escapeHtml(source.output_subdir)}}</td>
                <td>${{escapeHtml(source.emby_strm_dir)}}</td>
                <td><button type="button" class="secondary" data-edit="${{index}}">编辑</button> <button type="button" class="danger" data-delete="${{index}}">删除</button></td>
              </tr>`;
            }}).join('') || '<tr><td colspan="7">还没有媒体源，请点击“添加媒体源”。</td></tr>';
            document.getElementById('page-info').textContent = `第 ${{page}} / ${{totalPages}} 页，共 ${{sources.length}} 条`;
            document.getElementById('sources-json').value = JSON.stringify(sources);
          }}
          async function saveSources(nextSources) {{
            const resp = await fetch('/admin/api/sources', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ sources: nextSources }})
            }});
            const payload = await resp.json();
            if (!payload.ok) {{
              alert(payload.message || '保存失败');
              return false;
            }}
            sources = payload.sources || nextSources;
            render();
            return true;
          }}
          function openModal(index) {{
            editingIndex = index;
            const source = index >= 0 ? sources[index] : {{name:'', openlist_path:'', output_subdir:'', emby_strm_dir:'', media_type:'raw', tv_normalize:false, tv_series_depth:1, tv_default_season:1, tv_unmatched:'original', enabled:true}};
            document.getElementById('source-modal-title').textContent = index >= 0 ? '编辑媒体源' : '添加媒体源';
            fields.name.value = source.name || '';
            fields.openlist_path.value = source.openlist_path || '';
            fields.output_subdir.value = source.output_subdir || '';
            fields.emby_strm_dir.value = source.emby_strm_dir || '';
            fields.media_type.value = source.media_type || 'raw';
            fields.tv_normalize.checked = source.tv_normalize !== false && fields.media_type.value === 'tv';
            fields.tv_series_depth.value = source.tv_series_depth || 1;
            fields.tv_default_season.value = source.tv_default_season || 1;
            fields.tv_unmatched.value = source.tv_unmatched || 'original';
            fields.enabled.checked = source.enabled !== false;
            document.getElementById('tv-options').hidden = fields.media_type.value !== 'tv';
            modal.hidden = false;
          }}
          function closeModal() {{ modal.hidden = true; }}
          fields.media_type.addEventListener('change', () => {{
            const isTv = fields.media_type.value === 'tv';
            document.getElementById('tv-options').hidden = !isTv;
            if (isTv) fields.tv_normalize.checked = true;
          }});
          document.getElementById('add-source-row').addEventListener('click', () => openModal(-1));
          document.getElementById('modal-cancel').addEventListener('click', closeModal);
          document.getElementById('modal-save').addEventListener('click', () => {{
            const source = {{
              name: fields.name.value.trim(),
              openlist_path: fields.openlist_path.value.trim(),
              output_subdir: fields.output_subdir.value.trim(),
              emby_strm_dir: fields.emby_strm_dir.value.trim(),
              media_type: fields.media_type.value,
              tv_normalize: fields.media_type.value === 'tv' && fields.tv_normalize.checked,
              tv_series_depth: Number(fields.tv_series_depth.value || 1),
              tv_default_season: Number(fields.tv_default_season.value || 1),
              tv_unmatched: fields.tv_unmatched.value,
              enabled: fields.enabled.checked
            }};
            const nextSources = sources.slice();
            if (editingIndex >= 0) nextSources[editingIndex] = source;
            else {{ nextSources.push(source); page = Math.ceil(nextSources.length / pageSize); }}
            saveSources(nextSources).then((ok) => {{ if (ok) closeModal(); }});
          }});
          document.getElementById('prev-page').addEventListener('click', () => {{ page--; render(); }});
          document.getElementById('next-page').addEventListener('click', () => {{ page++; render(); }});
          tbody.addEventListener('click', (event) => {{
            const edit = event.target.closest('[data-edit]');
            const remove = event.target.closest('[data-delete]');
            const toggle = event.target.closest('[data-toggle]');
            if (edit) openModal(Number(edit.dataset.edit));
            if (remove) {{
              if (!confirm('确认删除这个媒体源？')) return;
              const nextSources = sources.slice();
              nextSources.splice(Number(remove.dataset.delete), 1);
              saveSources(nextSources);
            }}
            if (toggle) {{
              const i = Number(toggle.dataset.toggle);
              const nextSources = sources.slice();
              nextSources[i] = Object.assign({{}}, nextSources[i], {{ enabled: !nextSources[i].enabled }});
              saveSources(nextSources);
            }}
          }});
          document.querySelector('form[action="/admin/sources"]').addEventListener('submit', (event) => event.preventDefault());
          render();
        }})();
        </script>
        """,
    )


@app.post("/admin/sources")
async def save_sources_page(request: Request) -> Response:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    sources, errors = parse_source_payload(str(form.get("sources_json", "[]")))
    if errors:
        return sources_page(request, error="；".join(errors))
    save_config({"media_sources": sources})
    return RedirectResponse("/admin/sources?message=媒体源已保存", status_code=303)


@app.post("/admin/api/sources")
async def api_save_sources(request: Request) -> dict[str, Any]:
    require_user(request)
    try:
        payload = await request.json()
    except Exception:
        return json_error("请求数据格式错误")
    sources, errors = parse_source_payload(json.dumps(payload.get("sources", []), ensure_ascii=False))
    if errors:
        return json_error("；".join(errors))
    save_config({"media_sources": sources})
    return json_ok("媒体源已保存", sources=media_sources(get_config(force=True)))


@app.get("/admin/security", response_class=HTMLResponse)
def security_page(request: Request, message: str = "") -> Response:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    c = get_config(force=True)
    mode = str(c.get("security_mode") or "compatible")
    def selected(value: str) -> str:
        return " selected" if mode == value else ""
    return html_page(
        "安全设置",
        f"""
        {nav()}
        <main>
          <h1>安全设置</h1>
          {render_messages([message] if message else [])}
          <form method="post" action="/admin/security" class="form">
            <label>播放安全模式
              <select name="security_mode">
                <option value="strict"{selected('strict')}>strict - 仅允许 Emby Web 登录态换短效播放地址</option>
                <option value="compatible"{selected('compatible')}>compatible - STRM 库 token 可换短效地址，兼容客户端</option>
                <option value="private"{selected('private')}>private - 内网/VPN 简化模式</option>
              </select>
            </label>
            <div class="notice warning">
              <strong>安全边界说明</strong>
              <p>后台管理端口默认面向本地、内网或 VPN 使用。项目只提供基础登录、密码哈希和签名 Cookie，不提供公网管理后台所需的完整安全体系。请通过防火墙、反向代理认证、IP 白名单或 VPN 自行控制访问范围。</p>
              <ul>
                <li><strong>strict</strong>：最保守。STRM 静态入口不可直接播放，只能通过 Emby Web 按钮换取短效播放地址。适合公网网关或多用户环境，但对 Infuse/VLC 等直接读取 STRM 的客户端兼容性较差。</li>
                <li><strong>compatible</strong>：兼容性与安全折中。STRM 文件里的静态 token 可换取短效播放地址。适合内网/VPN 或可信用户环境；如果 STRM 内容或 token 泄露，攻击者可能在 token 轮换前持续换取播放地址。</li>
                <li><strong>private</strong>：最宽松。STRM 静态 token 直接跳转 OpenList/115 直链。仅建议纯内网/VPN 自用，不建议公网或多人共享环境使用。</li>
              </ul>
              <p>无论哪种模式，只要攻击者能访问网关并拿到有效 token，都可能消耗直链获取次数或触发网盘风控；安全模式不能替代网络访问控制。</p>
            </div>
            <label>短效播放地址有效期，秒<input name="play_url_ttl_seconds" value="{html.escape(str(c.get('play_url_ttl_seconds', 600)))}"></label>
            <label>播放/STRM Token<input name="static_token" value="{html.escape(str(c.get('static_token', '')))}"></label>
            <label>Webhook Token，留空复用播放 Token<input name="webhook_sync_token" value="{html.escape(str(c.get('webhook_sync_token', '')))}"></label>
            <label>播放器列表，逗号分隔<input name="external_players" value="{html.escape(','.join(c.get('external_players') or []))}"></label>
            <button>保存安全设置</button>
          </form>
        </main>
        """,
    )


@app.post("/admin/security")
async def save_security_page(request: Request) -> RedirectResponse:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    mode = str(form.get("security_mode", "compatible")).strip()
    if mode not in ("strict", "compatible", "private"):
        mode = "compatible"
    save_config({
        "security_mode": mode,
        "play_url_ttl_seconds": max(int(str(form.get("play_url_ttl_seconds", "600")) or 600), 60),
        "static_token": str(form.get("static_token", "")).strip() or secrets.token_urlsafe(24),
        "webhook_sync_token": str(form.get("webhook_sync_token", "")).strip(),
        "external_players": [part.strip() for part in str(form.get("external_players", "")).split(",") if part.strip()],
    })
    return RedirectResponse("/admin/security?message=安全设置已保存", status_code=303)


@app.get("/admin/password", response_class=HTMLResponse)
def password_page(request: Request) -> Response:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return html_page("修改密码", f"{nav()}<main><h1>修改密码</h1><form method='post' action='/admin/password' class='form'><label>新密码<input name='password' type='password'></label><button>保存</button></form></main>")


@app.post("/admin/password")
def password_save(request: Request, password: str = Form(...)) -> RedirectResponse:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if len(password) < 8:
        return RedirectResponse("/admin/password", status_code=303)
    with db() as con:
        con.execute("update users set password_hash = ?, must_change_password = 0 where username = ?", (hash_password(password), user["username"]))
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/sync", response_class=HTMLResponse)
def sync_page(request: Request) -> Response:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    config = get_config()
    token = config.get("static_token", "")
    options = "<option value=''>全部媒体源</option>" + "".join(
        f"<option value='{source['key']}'>{source['name']} - {source_root(config, source)}</option>"
        for source in enabled_media_sources(config)
    )
    errors = config_errors(config)
    mode = str(config.get("sync_mode") or "manual")
    def selected(value: str) -> str:
        return " selected" if mode == value else ""
    return html_page(
        "同步",
        f"""
        {nav()}
        <main>
          <h1>同步</h1>
          {render_messages(errors, "error") if errors else render_messages(["配置完整，可以启用自动同步"])}
          <form method="post" action="/admin/sync/settings" class="form">
            <section class="form-section">
              <h2>同步规则</h2>
              <label>同步模式
                <select name="sync_mode">
                  <option value="manual"{selected('manual')}>manual - 只手动同步</option>
                  <option value="smart"{selected('smart')}>smart - 定时同步并跳过未变化目录</option>
                  <option value="full"{selected('full')}>full - 定时全量扫描</option>
                  <option value="webhook"{selected('webhook')}>webhook - 仅接受外部事件触发</option>
                </select>
              </label>
              <label>同步间隔，秒<input name="sync_interval_seconds" value="{html.escape(str(config.get('sync_interval_seconds', 21600)))}"></label>
              <label>连续缺失多少次后删除 STRM<input name="delete_missing_after_count" value="{html.escape(str(config.get('delete_missing_after_count', 3)))}"></label>
              <label>OpenList 列表 refresh
                <select name="openlist_list_refresh">
                  <option value="false"{'' if config.get('openlist_list_refresh') else ' selected'}>false - 使用 OpenList 缓存</option>
                  <option value="true"{' selected' if config.get('openlist_list_refresh') else ''}>true - 强制刷新 OpenList 列表</option>
                </select>
              </label>
              <label>探测模式
                <select name="probe_mode">
                  <option value="none"{' selected' if config.get('probe_mode') == 'none' else ''}>none - 阻断 Emby 探测访问</option>
                  <option value="minimal"{' selected' if config.get('probe_mode') == 'minimal' else ''}>minimal - 最小探测</option>
                  <option value="range"{' selected' if config.get('probe_mode') == 'range' else ''}>range - 允许 Range 探测</option>
                </select>
              </label>
            </section>
            <button>保存同步规则</button>
          </form>
          <form method="post" action="/admin/sync/run" class="form">
            <section class="form-section">
              <h2>手动同步</h2>
            <label>媒体源<select name="source_key">{options}</select></label>
            <label>OpenList 完整路径，留空则同步所选媒体源根目录<input name="path" placeholder="/Cloud/115/Media/Movies/子目录"></label>
            <label><input type="checkbox" name="force" value="true"> 强制扫描，不使用目录指纹缓存</label>
            </section>
            <button>开始同步</button>
          </form>
          <form method="post" action="/admin/sync/rewrite-strm" class="form">
            <section class="form-section">
              <h2>重写 STRM URL</h2>
              <p class="muted">仅刷新现有 STRM 文件中的网关地址和播放 Token，不扫描 OpenList，不获取 115 直链，不增删媒体记录。</p>
              <label>媒体源<select name="source_key">{options}</select></label>
            </section>
            <button class="secondary">重写 STRM URL</button>
          </form>
          <p>API 示例：<code>/admin-api/sync?token={token}&path=/Cloud/115/Media/Movies</code></p>
        </main>
        """,
    )


@app.post("/admin/sync/settings")
async def sync_settings_save(request: Request) -> RedirectResponse:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    mode = str(form.get("sync_mode", "manual")).strip()
    if mode not in ("manual", "smart", "full", "webhook"):
        mode = "manual"
    save_config({
        "sync_mode": mode,
        "sync_interval_seconds": max(int(str(form.get("sync_interval_seconds", "21600")) or 21600), 60),
        "delete_missing_after_count": max(int(str(form.get("delete_missing_after_count", "3")) or 3), 1),
        "openlist_list_refresh": str(form.get("openlist_list_refresh", "false")).lower() == "true",
        "probe_mode": str(form.get("probe_mode", "none")).strip() or "none",
    })
    return RedirectResponse("/admin/sync", status_code=303)


@app.post("/admin/sync/run")
def sync_run(request: Request, path: str = Form(""), source_key: str = Form(""), force: str | None = Form(None)) -> RedirectResponse:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    threading.Thread(target=lambda: sync_once(path=path or None, source_key=source_key or None, force=bool(force)), daemon=True).start()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/sync/rewrite-strm")
def sync_rewrite_strm(request: Request, source_key: str = Form("")) -> RedirectResponse:
    user = require_page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    threading.Thread(target=lambda: rewrite_strm_urls(source_key=source_key or None), daemon=True).start()
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/api/status")
def api_status(request: Request) -> dict[str, Any]:
    require_user(request)
    config = get_config(force=True)
    with db() as con:
        media_count = con.execute("select count(*) n from media").fetchone()["n"]
    return {"config": {k: ("***" if "key" in k or "token" in k else v) for k, v in config.items()}, "media_count": media_count, "last_sync": last_sync}


@app.post("/admin-api/sync")
def admin_sync(token: str = Query(...), path: str | None = Query(None), source_key: str | None = Query(None), force: bool = Query(False), cleanup: bool | None = Query(None)) -> dict[str, Any]:
    config = get_config()
    if token != str(config.get("static_token")):
        raise HTTPException(status_code=403, detail="invalid token")
    return sync_once(path=path, source_key=source_key, force=force, cleanup=cleanup)


@app.post("/admin-api/webhook")
def admin_webhook(token: str = Query(...), path: str | None = Query(None), source_key: str | None = Query(None), force: bool = Query(False)) -> dict[str, Any]:
    config = get_config()
    expected = str(config.get("webhook_sync_token") or config.get("static_token"))
    if token != expected:
        raise HTTPException(status_code=403, detail="invalid token")
    return sync_once(path=path, source_key=source_key, force=force, cleanup=False)


@app.get("/admin-api/health")
def health() -> dict[str, Any]:
    config = get_config(force=True)
    errors = config_errors(config)
    return {
        "ok": True,
        "configured": not errors,
        "config_errors": errors,
        "security_mode": config.get("security_mode"),
        "sync_mode": config.get("sync_mode"),
        "probe_mode": config.get("probe_mode"),
        "strm_output_dir": config.get("strm_output_dir"),
        "media_sources": media_sources(config),
        "last_sync": last_sync,
    }


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/external-url/{item_id}")
def external_url(item_id: str) -> dict[str, Any]:
    config = get_config()
    media_id = media_id_by_item_id(config, item_id)
    if not media_id:
        raise HTTPException(status_code=404, detail="item is not mapped to a 115 strm media")
    openlist_path, name = media_by_id(media_id)
    return {"item_id": item_id, "media_id": media_id, "name": name, "url": gateway_play_url(config, media_id), "openlist_path": openlist_path}


@app.get("/check")
def check(x_original_uri: str = Header("")) -> Response:
    item_id = item_id_from_uri(x_original_uri)
    if not item_id:
        return Response(status_code=204)
    config = get_config()
    media_id = media_id_by_item_id(config, item_id, x_original_uri)
    if not media_id:
        return Response(status_code=204)
    return Response(status_code=401, headers={"X-Redirect-Url": gateway_openlist_redirect_url(config, media_id)})


def strm_response(media_id: str, token: str, request: Request) -> Response:
    config = get_config()
    if token != str(config.get("static_token")):
        raise HTTPException(status_code=403, detail="invalid token")
    if is_probe_request(request):
        mode = str(config.get("probe_mode") or "none").lower()
        if mode in ("none", "off", "false", "0"):
            return blocked_probe_response(request)
    security_mode = str(config.get("security_mode") or "compatible").lower()
    if security_mode == "strict":
        raise HTTPException(status_code=403, detail="strm static entry is disabled in strict mode")
    if security_mode == "private":
        return openlist_client_redirect(media_id, token, request)
    return RedirectResponse(gateway_play_url(config, media_id), status_code=302)


@app.get("/strm/{media_id}/{name}")
def strm_file(request: Request, media_id: str, name: str, token: str = Query(...)) -> Response:
    return strm_response(media_id, token, request)


@app.head("/strm/{media_id}/{name}")
def strm_file_head(request: Request, media_id: str, name: str, token: str = Query(...)) -> Response:
    return strm_response(media_id, token, request)


@app.get("/openlist-redirect/{media_id}/{name}")
def openlist_redirect(request: Request, media_id: str, name: str, token: str = Query(...)) -> RedirectResponse:
    return openlist_client_redirect(media_id, token, request)


@app.head("/openlist-redirect/{media_id}/{name}")
def openlist_redirect_head(request: Request, media_id: str, name: str, token: str = Query(...)) -> RedirectResponse:
    return openlist_client_redirect(media_id, token, request)


def play_response(media_id: str, exp: int, sig: str, request: Request) -> RedirectResponse:
    if exp < now():
        raise HTTPException(status_code=403, detail="play url expired")
    if not hmac.compare_digest(sig, play_signature(media_id, exp)):
        raise HTTPException(status_code=403, detail="invalid play signature")
    config = get_config()
    return openlist_client_redirect(media_id, str(config.get("static_token")), request)


@app.get("/play/{media_id}/{name}")
def play(request: Request, media_id: str, name: str, exp: int = Query(...), sig: str = Query(...)) -> RedirectResponse:
    return play_response(media_id, exp, sig, request)


@app.head("/play/{media_id}/{name}")
def play_head(request: Request, media_id: str, name: str, exp: int = Query(...), sig: str = Query(...)) -> RedirectResponse:
    return play_response(media_id, exp, sig, request)


@app.exception_handler(HTTPException)
def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

