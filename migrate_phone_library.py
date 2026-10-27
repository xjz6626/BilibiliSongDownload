#!/usr/bin/env python3
"""Safely enrich an existing M4A library on an Android device.

The normal workflow is deliberately split in three:

1. ``catalog`` downloads *metadata and thumbnails only* for IDs in archive.txt.
2. ``plan`` reads the phone and writes a reviewable JSON/CSV plan.  It never
   writes to the phone.
3. ``apply`` requires the exact plan hash, keeps a local copy of every source
   file, verifies that the MP4 ``mdat`` payload is unchanged, and only then
   performs a same-directory replacement on the phone.

Instrumental-looking files are marked as deletion candidates.  They are never
deleted by ``plan`` or ``apply``.  The separate ``delete`` command needs both
the plan hash and ``--allow-delete`` before it will remove one.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from mutagen.mp4 import MP4

import prv

try:  # credits.py is optional so the migration tool can be developed alone.
    from credits import extract_credits as _extract_credits
except (ImportError, AttributeError):
    _extract_credits = None


PLAN_VERSION = 1
CATALOG_INDEX = "catalog-index.json"
DEFAULT_REMOTE_DIR = "/sdcard/_Library/Audio/Music/v+/"
MIGRATION_DIR = ".migration"
DEFAULT_CATALOG_DIR = f"{MIGRATION_DIR}/catalog"
DEFAULT_PLAN_FILE = f"{MIGRATION_DIR}/phone-library-plan.json"
DEFAULT_CSV_FILE = f"{MIGRATION_DIR}/phone-library-plan.csv"
DEFAULT_BACKUP_DIR = f"{MIGRATION_DIR}/backups"
DEFAULT_TITLE_CACHE = f"{MIGRATION_DIR}/title-cache.json"
READ_CHUNK_SIZE = 1024 * 1024
SOURCE_CID_TAG = getattr(prv, "SOURCE_CID_TAG", "----:com.apple.iTunes:BilibiliCID")

BVID_RE = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{8,})(?:_p(\d+))?", re.IGNORECASE)
PART_SUFFIX_RE = re.compile(r"^(BV[0-9A-Za-z]{8,})_p(\d+)$", re.IGNORECASE)
PART_HINT_RE = re.compile(r"(?:^|[\s._-])p(?:art)?\s*0*(\d+)(?:$|[\s._-])", re.IGNORECASE)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
INSTRUMENTAL_PATTERNS = (
    ("伴奏", re.compile(r"伴奏")),
    ("无人声", re.compile(r"无(?:人声|主唱|演唱)")),
    ("off vocal", re.compile(r"(?<![a-z])off[ ._-]*vocal(?![a-z])", re.IGNORECASE)),
    ("instrumental", re.compile(r"(?<![a-z])instrumental(?![a-z])", re.IGNORECASE)),
    ("inst", re.compile(r"(?<![a-z])inst(?:\.?[ _-]*only)?(?![a-z])", re.IGNORECASE)),
    ("karaoke", re.compile(r"(?<![a-z])karaoke(?![a-z])", re.IGNORECASE)),
    ("backing track", re.compile(r"(?<![a-z])backing[ _-]*track(?![a-z])", re.IGNORECASE)),
    ("纯音乐版", re.compile(r"纯音乐版")),
)

CREDIT_TAGS = {
    "lyricists": "----:com.apple.iTunes:BilibiliLyricists",
    "composers": "----:com.apple.iTunes:BilibiliComposers",
    "arrangers": "----:com.apple.iTunes:BilibiliArrangers",
    "tuners": "----:com.apple.iTunes:BilibiliTuners",
    "mixers": "----:com.apple.iTunes:BilibiliMixers",
    "illustrators": "----:com.apple.iTunes:BilibiliIllustrators",
    "video_creators": "----:com.apple.iTunes:BilibiliVideoCreators",
}

MIGRATION_MANAGED_TAGS = {
    "\xa9nam",
    "\xa9cmt",
    "\xa9ART",
    "\xa9alb",
    "aART",
    "\xa9grp",
    "\xa9day",
    "\xa9wrt",
    prv.STATE_TAG,
    prv.SONG_TYPE_TAG,
    prv.SOURCE_ID_TAG,
    prv.SOURCE_DESCRIPTION_TAG,
    prv.SOURCE_UPLOADER_TAG,
    prv.SOURCE_UPLOADER_ID_TAG,
    prv.SOURCE_PUBLISH_TIME_TAG,
    getattr(prv, "SOURCE_ITEM_ID_TAG", "----:com.apple.iTunes:BilibiliItemID"),
    getattr(prv, "SOURCE_CID_TAG", "----:com.apple.iTunes:BilibiliCID"),
    getattr(prv, "SOURCE_PAGE_TAG", "----:com.apple.iTunes:BilibiliPage"),
    getattr(prv, "SOURCE_PART_TITLE_TAG", "----:com.apple.iTunes:BilibiliPartTitle"),
    getattr(prv, "SOURCE_ORIGINAL_TITLE_TAG", "----:com.apple.iTunes:BilibiliOriginalTitle"),
    getattr(prv, "SOURCE_CREDITS_TAG", "----:com.apple.iTunes:BilibiliCredits"),
    *CREDIT_TAGS.values(),
    *getattr(getattr(prv, "credits", None), "MP4_FREEFORM_TAGS", {}).values(),
}


class MigrationError(RuntimeError):
    """A safety precondition failed; no later entries should be touched."""


def sha256_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(READ_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_region(handle, digest, size: int) -> None:
    remaining = size
    while remaining:
        chunk = handle.read(min(remaining, READ_CHUNK_SIZE))
        if not chunk:
            raise ValueError("M4A 的 mdat box 被截断")
        digest.update(chunk)
        remaining -= len(chunk)


def hash_audio_stream(path: os.PathLike[str] | str) -> str:
    """Hash every top-level MP4 ``mdat`` payload, excluding editable tags."""
    digest = hashlib.sha256()
    found = False
    file_size = os.path.getsize(path)
    with open(path, "rb") as handle:
        offset = 0
        while offset < file_size:
            header = handle.read(8)
            if len(header) != 8:
                raise ValueError("M4A 顶层 box 头被截断")
            size32, box_type = struct.unpack(">I4s", header)
            header_size = 8
            if size32 == 1:
                extended = handle.read(8)
                if len(extended) != 8:
                    raise ValueError("M4A 扩展 box 头被截断")
                box_size = struct.unpack(">Q", extended)[0]
                header_size = 16
            elif size32 == 0:
                box_size = file_size - offset
            else:
                box_size = size32
            if box_size < header_size or offset + box_size > file_size:
                raise ValueError("M4A 顶层 box 大小无效")
            payload_size = box_size - header_size
            if box_type == b"mdat":
                found = True
                _hash_region(handle, digest, payload_size)
            else:
                handle.seek(payload_size, os.SEEK_CUR)
            offset += box_size
    if not found:
        raise ValueError("M4A 中没有 mdat 音频数据")
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def compute_plan_hash(plan: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def seal_plan(plan: dict[str, Any]) -> dict[str, Any]:
    plan = dict(plan)
    plan.pop("plan_hash", None)
    plan["plan_hash"] = compute_plan_hash(plan)
    return plan


def validate_plan(plan: dict[str, Any], confirmed_hash: str | None = None) -> str:
    stored_hash = plan.get("plan_hash")
    actual_hash = compute_plan_hash(plan)
    if not isinstance(stored_hash, str) or stored_hash != actual_hash:
        raise MigrationError("plan 内容或 plan_hash 已改变；请重新生成并审阅计划")
    if confirmed_hash is not None and confirmed_hash != stored_hash:
        raise MigrationError("--confirm-hash 与计划哈希不一致")
    if plan.get("version") != PLAN_VERSION or not isinstance(plan.get("entries"), list):
        raise MigrationError("不支持或损坏的迁移计划")
    return stored_hash


def load_title_cache(path: os.PathLike[str] | str) -> dict[str, str]:
    cache_path = Path(path)
    if not cache_path.exists():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationError(f"无法读取标题缓存: {cache_path}") from error
    titles = raw.get("titles") if isinstance(raw, dict) else None
    if not isinstance(titles, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in titles.items()):
        raise MigrationError("标题缓存格式无效")
    return titles


def _save_title_cache(path: os.PathLike[str] | str, titles: dict[str, str]) -> None:
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{cache_path.name}.", dir=cache_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "titles": titles}, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, cache_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def clean_title_cached(
    source_title: str,
    description: str,
    cache: dict[str, str],
    cache_path: os.PathLike[str] | str,
) -> str:
    key = hashlib.sha256(canonical_json({
        "source_title": source_title,
        "description": description,
        "model": getattr(prv, "MODEL_NAME", ""),
    })).hexdigest()
    cached = cache.get(key)
    if cached:
        return prv.sanitize_title(cached)
    title = prv.clean_title_with_ai(source_title, description)
    cache[key] = title
    _save_title_cache(cache_path, cache)
    return title


def normalize_source_id(value: Any) -> tuple[str, int | None]:
    if not isinstance(value, str):
        return "", None
    match = BVID_RE.search(value.strip())
    if not match:
        return "", None
    return match.group(1), int(match.group(2)) if match.group(2) else None


def source_key(bvid: str, part: int | None) -> str:
    return f"{bvid}_p{part}" if part is not None else bvid


def catalog_identity(bvid: str, cid: str, part: int | None) -> str:
    """CID is stable for a Bilibili page; page number is display-only fallback."""
    if cid:
        return f"bvid:{bvid.casefold()}:cid:{cid}"
    return f"legacy:{source_key(bvid, part).casefold()}"


def parse_archive(path: os.PathLike[str] | str) -> list[dict[str, Any]]:
    """Read yt-dlp archive IDs, retaining part suffixes but deduplicating them."""
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            bvid, part = normalize_source_id(raw_line)
            identity = (bvid.casefold(), part)
            if not bvid or identity in seen:
                continue
            seen.add(identity)
            records.append({"bvid": bvid, "part": part, "source_key": source_key(bvid, part)})
    return records


def _catalog_output_template(catalog_dir: os.PathLike[str] | str) -> str:
    return os.fspath(Path(catalog_dir, "%(id)s.%(ext)s"))


def fetch_catalog(
    archive_path: os.PathLike[str] | str,
    catalog_dir: os.PathLike[str] | str,
    *,
    cookies_from_browser: str = "",
    refresh: bool = False,
    chunk_size: int = 50,
    workers: int = 4,
) -> None:
    """Ask yt-dlp for metadata/thumbnails only; audio formats are never selected."""
    archive = parse_archive(archive_path)
    base_ids: list[str] = []
    seen: set[str] = set()
    for item in archive:
        folded = item["bvid"].casefold()
        if folded not in seen:
            seen.add(folded)
            base_ids.append(item["bvid"])
    Path(catalog_dir).mkdir(parents=True, exist_ok=True)
    if not base_ids:
        raise MigrationError("archive.txt 中没有 BVID")
    if workers < 1 or workers > 8:
        raise MigrationError("catalog --workers 必须在 1 到 8 之间")

    def fetch_batch(offset: int) -> tuple[int, int]:
        command = [
            "yt-dlp",
            "--skip-download",
            "--write-info-json",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--yes-playlist",
            "--ignore-errors",
            "--no-warnings",
            "--quiet",
            "--output",
            _catalog_output_template(catalog_dir),
        ]
        if not refresh:
            command.append("--no-overwrites")
        if cookies_from_browser:
            command.extend(["--cookies-from-browser", cookies_from_browser])
        command.extend(f"https://www.bilibili.com/video/{bvid}" for bvid in base_ids[offset:offset + chunk_size])
        result = subprocess.run(command, check=False)
        return offset, result.returncode

    offsets = list(range(0, len(base_ids), chunk_size))
    failures: list[int] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(offsets))) as executor:
        futures = {executor.submit(fetch_batch, offset): offset for offset in offsets}
        for completed, future in enumerate(as_completed(futures), 1):
            offset, returncode = future.result()
            if returncode:
                failures.append(offset)
            print(f"catalog 元数据批次: {completed}/{len(offsets)}")
    records = load_catalog(catalog_dir, archive)
    if not records:
        raise MigrationError("catalog 未取得任何可用元数据")
    Path(catalog_dir, CATALOG_INDEX).write_text(
        json.dumps(
            {
                "version": 1,
                "archive_base_ids": len(base_ids),
                "records_loaded": len(records),
                "failed_batch_offsets": sorted(offset + 1 for offset in failures),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if failures:
        failed_text = ", ".join(str(offset + 1) for offset in sorted(failures))
        print(f"警告: 部分视频已失效或无法读取，已保留其余目录（批次起点: {failed_text}）")


def _metadata_cover(info_path: Path, raw: dict[str, Any]) -> Path | None:
    stem = info_path.name[:-len(".info.json")] if info_path.name.endswith(".info.json") else info_path.stem
    direct = [path for path in info_path.parent.glob(f"{stem}.*") if path.suffix.lower() in IMAGE_SUFFIXES]
    if direct:
        return sorted(direct)[0]
    bvid, part = normalize_source_id(str(raw.get("id") or ""))
    if bvid:
        patterns = [f"{source_key(bvid, part)}.*", f"{bvid}.*"]
        for pattern in patterns:
            matches = [path for path in info_path.parent.glob(pattern) if path.suffix.lower() in IMAGE_SUFFIXES]
            if matches:
                return sorted(matches)[0]
    return None


def _iter_metadata(raw: Any, info_path: Path) -> Iterable[tuple[dict[str, Any], Path]]:
    if not isinstance(raw, dict):
        return
    entries = raw.get("entries")
    if isinstance(entries, list) and entries:
        for entry in entries:
            entry_origin = info_path
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                candidate = info_path.with_name(f"{entry['id']}.info.json")
                if candidate.is_file():
                    entry_origin = candidate
            yield from _iter_metadata(entry, entry_origin)
        return
    yield raw, info_path


def _record_from_metadata(raw: dict[str, Any], info_path: Path) -> dict[str, Any] | None:
    identity_values = (
        raw.get("id"), raw.get("display_id"), raw.get("webpage_url"), raw.get("original_url")
    )
    bvid = ""
    suffix_part = None
    for value in identity_values:
        bvid, suffix_part = normalize_source_id(str(value or ""))
        if bvid:
            break
    if not bvid:
        return None
    raw_item_id = raw.get("id")
    source_item_id = str(raw_item_id).strip() if isinstance(raw_item_id, str) else source_key(bvid, suffix_part)
    item_metadata = (
        prv.extract_source_item_metadata(raw, source_item_id)
        if hasattr(prv, "extract_source_item_metadata")
        else {}
    )
    raw_part = item_metadata.get("page") or raw.get("playlist_index") or raw.get("page")
    part = suffix_part
    if part is None and isinstance(raw_part, (str, int)) and str(raw_part).isdigit() and int(raw_part) > 0:
        part = int(raw_part)
    playlist_count = raw.get("playlist_count") or raw.get("n_entries")
    if part == 1 and playlist_count in (None, 0, 1):
        part = None
    raw_cid = item_metadata.get("cid") or raw.get("cid")
    cid = str(raw_cid).strip() if isinstance(raw_cid, (str, int)) and not isinstance(raw_cid, bool) else ""
    cover_path = _metadata_cover(info_path, raw)
    attribution = prv.extract_attribution_metadata(raw)
    title = prv._normalize_source_text(raw.get("title") or raw.get("fulltitle"), prv.MAX_SOURCE_TITLE_CHARS)
    description = prv._normalize_source_text(raw.get("description"), prv.MAX_DESCRIPTION_CHARS)
    return {
        "source_key": catalog_identity(bvid, cid, part),
        "source_item_id": source_item_id,
        "bvid": bvid,
        "cid": cid,
        "part": part,
        "part_count": playlist_count if isinstance(playlist_count, int) else None,
        "title": title,
        "description": description,
        "description_truncated": bool(
            isinstance(raw.get("description"), str)
            and len(raw["description"]) > prv.MAX_DESCRIPTION_CHARS
        ),
        "uploader": attribution["uploader"],
        "uploader_id": attribution["uploader_id"],
        "upload_date": attribution["upload_date"],
        "publish_time": attribution["publish_time"],
        "duration": raw.get("duration") if isinstance(raw.get("duration"), (int, float)) else None,
        "cover_sha256": sha256_file(cover_path) if cover_path else "",
        "cover_path": os.fspath(cover_path.resolve()) if cover_path else "",
        "info_path": os.fspath(info_path.resolve()),
    }


def _record_score(record: dict[str, Any]) -> tuple[int, int, int]:
    return (bool(record.get("description")), bool(record.get("cover_sha256")), len(record.get("title", "")))


def load_catalog(
    catalog_dir: os.PathLike[str] | str,
    archive: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Load raw yt-dlp info JSON files, limited to archive base IDs when supplied."""
    allowed = {item["bvid"].casefold() for item in archive or []}
    by_key: dict[str, dict[str, Any]] = {}
    for info_path in sorted(Path(catalog_dir).rglob("*.info.json")):
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MigrationError(f"无法读取 catalog 元数据: {info_path}") from error
        for metadata, origin in _iter_metadata(raw, info_path):
            record = _record_from_metadata(metadata, origin)
            if record is None or (allowed and record["bvid"].casefold() not in allowed):
                continue
            folded_key = record["source_key"].casefold()
            old = by_key.get(folded_key)
            if old is None or _record_score(record) > _record_score(old):
                by_key[folded_key] = record
    return sorted(by_key.values(), key=lambda item: (item["bvid"].casefold(), item["part"] or 0))


def _decode_freeform(tags: Any, key: str) -> str:
    values = tags.get(key) if tags else None
    if not values:
        return ""
    raw = values[0]
    if isinstance(raw, str):
        return raw
    try:
        return bytes(raw).decode("utf-8")
    except (TypeError, UnicodeDecodeError):
        return ""


def inspect_m4a(local_path: os.PathLike[str] | str, remote_path: str = "") -> dict[str, Any]:
    audio = MP4(local_path)
    tags = audio.tags or {}
    cover = tags.get("covr") or []
    state_text = _decode_freeform(tags, prv.STATE_TAG)
    try:
        state = json.loads(state_text) if state_text else None
    except json.JSONDecodeError:
        state = None
    filename = posixpath.basename(remote_path) if remote_path else os.path.basename(local_path)
    source_id = _decode_freeform(tags, prv.SOURCE_ID_TAG)
    if not source_id and isinstance(state, dict):
        source_id = state.get("source_id") if isinstance(state.get("source_id"), str) else ""
    return {
        "remote_path": remote_path,
        "filename": filename,
        "size": os.path.getsize(local_path),
        "file_sha256": sha256_file(local_path),
        "audio_stream_sha256": hash_audio_stream(local_path),
        "cover_sha256": hashlib.sha256(bytes(cover[0])).hexdigest() if cover else "",
        "source_id": source_id,
        "source_cid": _decode_freeform(tags, SOURCE_CID_TAG),
        "title": str((tags.get("\xa9nam") or [Path(filename).stem])[0]),
        "artists": [str(value) for value in (tags.get("\xa9ART") or [])],
        "album": str((tags.get("\xa9alb") or [""])[0]),
        "comment": str((tags.get("\xa9cmt") or [""])[0]),
        "duration": round(float(audio.info.length), 3) if audio.info else None,
        "has_processing_state": isinstance(state, dict),
    }


class ADBClient:
    def __init__(self, serial: str = ""):
        self.serial = serial or self._select_device()

    @staticmethod
    def _select_device() -> str:
        result = subprocess.run(["adb", "devices"], check=True, stdout=subprocess.PIPE, text=True)
        devices = [
            line.split()[0]
            for line in result.stdout.splitlines()[1:]
            if len(line.split()) >= 2 and line.split()[1] == "device"
        ]
        if len(devices) != 1:
            raise MigrationError("请连接且只连接一台手机，或使用 --adb-serial 指定设备")
        return devices[0]

    @property
    def base(self) -> list[str]:
        return ["adb", "-s", self.serial]

    def _shell(self, script: str, *, text: bool = True) -> subprocess.CompletedProcess:
        # adb transports a command string rather than an argv array.  Quote the
        # complete inner script so ``sh -c`` receives it as exactly one value.
        # Use the shell protocol rather than ``exec-out``: on some devices
        # exec-out returns host status 0 even when the remote command failed,
        # which would turn ``test -e`` into an unsafe false positive.
        remote_command = f"sh -c {shlex.quote(script)}"
        return subprocess.run(
            [*self.base, "shell", remote_command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
        )

    def list_files(self, remote_dir: str) -> list[str]:
        directory = remote_dir.rstrip("/") or "/"
        quoted = shlex.quote(directory)
        script = (
            f'[ -d {quoted} ] || exit 69; for f in {quoted}/*.m4a {quoted}/*.M4A; do '
            '[ -f "$f" ] && printf "%s\\0" "$f"; done; exit 0'
        )
        result = self._shell(script, text=False)
        if result.returncode:
            raise MigrationError(f"无法扫描手机目录: {result.stderr.decode(errors='replace').strip()}")
        return sorted(value.decode("utf-8") for value in result.stdout.split(b"\0") if value)

    def pull(self, remote_path: str, local_path: os.PathLike[str] | str) -> None:
        result = subprocess.run(
            [*self.base, "pull", remote_path, os.fspath(local_path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode:
            raise MigrationError(f"ADB pull 失败: {remote_path}: {result.stdout.strip()}")

    def push(self, local_path: os.PathLike[str] | str, remote_path: str) -> None:
        result = subprocess.run(
            [*self.base, "push", os.fspath(local_path), remote_path],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode:
            raise MigrationError(f"ADB push 失败: {remote_path}: {result.stdout.strip()}")

    def exists(self, remote_path: str) -> bool:
        return self._shell(f"test -e {shlex.quote(remote_path)}").returncode == 0

    def remote_sha256(self, remote_path: str) -> str:
        result = self._shell(f"sha256sum {shlex.quote(remote_path)}")
        if result.returncode:
            raise MigrationError(f"无法计算手机文件 SHA-256: {remote_path}")
        match = re.match(r"([0-9a-fA-F]{64})\s", result.stdout)
        if not match:
            raise MigrationError("手机上的 sha256sum 输出无法识别")
        return match.group(1).lower()

    def atomic_replace(
        self,
        source_path: str,
        temp_path: str,
        target_path: str,
        source_sha256: str,
        temp_sha256: str,
    ) -> None:
        source_q, temp_q, target_q = map(shlex.quote, (source_path, temp_path, target_path))
        common = (
            f'[ -f {source_q} ] || exit 70; '
            f'[ "$(sha256sum {source_q} | cut -d" " -f1)" = {shlex.quote(source_sha256)} ] || exit 71; '
            f'[ "$(sha256sum {temp_q} | cut -d" " -f1)" = {shlex.quote(temp_sha256)} ] || exit 72; '
        )
        if source_path == target_path:
            script = common + f"mv {temp_q} {source_q}"
        else:
            script = common + f"[ ! -e {target_q} ] || exit 73; mv {temp_q} {target_q} && rm {source_q}"
        result = self._shell(script)
        if result.returncode:
            raise MigrationError(f"手机端原子替换失败（退出码 {result.returncode}）: {source_path}")

    def delete_verified(self, remote_path: str, expected_sha256: str) -> None:
        path_q = shlex.quote(remote_path)
        script = (
            f'[ -f {path_q} ] || exit 70; '
            f'[ "$(sha256sum {path_q} | cut -d" " -f1)" = {shlex.quote(expected_sha256)} ] || exit 71; '
            f"rm {path_q}"
        )
        result = self._shell(script)
        if result.returncode:
            raise MigrationError(f"手机端校验删除失败（退出码 {result.returncode}）: {remote_path}")


def scan_phone(adb: ADBClient, remote_dir: str) -> list[dict[str, Any]]:
    """Pull current phone files one at a time for read-only inspection."""
    tracks: list[dict[str, Any]] = []
    remote_files = adb.list_files(remote_dir)
    with tempfile.TemporaryDirectory(prefix="bilibili-phone-scan-") as directory:
        for index, remote_path in enumerate(remote_files, 1):
            if index == 1 or index % 50 == 0 or index == len(remote_files):
                print(
                    f"只读扫描 {index}/{len(remote_files)}: {posixpath.basename(remote_path)}",
                    flush=True,
                )
            local_path = Path(directory, f"{index:06d}.m4a")
            try:
                adb.pull(remote_path, local_path)
                tracks.append(inspect_m4a(local_path, remote_path))
            except Exception as error:
                tracks.append({
                    "remote_path": remote_path,
                    "filename": posixpath.basename(remote_path),
                    "scan_error": str(error),
                })
            finally:
                # 曲库可能有数十 GB；摘要完成后立即释放单首临时副本，
                # 避免只读扫描自身占满 PC 空间。
                try:
                    local_path.unlink(missing_ok=True)
                except OSError:
                    pass
    return tracks


def instrumental_evidence(*texts: str) -> list[str]:
    normalized = "\n".join(unicodedata.normalize("NFKC", text or "") for text in texts)
    return [label for label, pattern in INSTRUMENTAL_PATTERNS if pattern.search(normalized)]


def _catalog_indexes(records: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], list], dict[str, list], dict[str, list]]:
    exact: dict[tuple[str, str], list] = defaultdict(list)
    base: dict[str, list] = defaultdict(list)
    covers: dict[str, list] = defaultdict(list)
    for record in records:
        if record.get("cid"):
            exact[(record["bvid"].casefold(), str(record["cid"]))].append(record)
        base[record["bvid"].casefold()].append(record)
        if record.get("cover_sha256"):
            covers[record["cover_sha256"]].append(record)
    return exact, base, covers


def duration_check(track: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    phone_duration = track.get("duration")
    catalog_duration = record.get("duration")
    if not isinstance(phone_duration, (int, float)) or not isinstance(catalog_duration, (int, float)):
        return {"status": "missing", "phone_seconds": phone_duration, "catalog_seconds": catalog_duration}
    tolerance = max(5.0, float(catalog_duration) * 0.03)
    delta = abs(float(phone_duration) - float(catalog_duration))
    return {
        "status": "compatible" if delta <= tolerance else "mismatch",
        "phone_seconds": float(phone_duration),
        "catalog_seconds": float(catalog_duration),
        "delta_seconds": round(delta, 3),
        "tolerance_seconds": round(tolerance, 3),
    }


def _normalized_title_identity(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", value)


def zero_duration_title_identity(track: dict[str, Any], record: dict[str, Any]) -> bool:
    """Confirm old fragmented M4A files whose Mutagen length is reported as 0.

    Cover SHA alone is never enough.  This fallback additionally requires a
    reasonably specific normalized phone title to occur verbatim in the unique
    catalog title (or vice versa), while real non-zero duration conflicts remain
    review-only.
    """

    duration = track.get("duration")
    if not isinstance(duration, (int, float)) or duration > 0:
        return False
    phone_title = _normalized_title_identity(track.get("title"))
    catalog_title = _normalized_title_identity(record.get("title"))
    if min(len(phone_title), len(catalog_title)) < 4:
        return False
    return phone_title in catalog_title or catalog_title in phone_title


def match_track(track: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    exact, base, covers = _catalog_indexes(records)
    bvid, _display_part = normalize_source_id(track.get("source_id"))
    cid = str(track.get("source_cid") or "").strip()
    if bvid:
        base_candidates = base.get(bvid.casefold(), [])
        candidates = exact.get((bvid.casefold(), cid), []) if cid else []
        if cid:
            if len(candidates) == 1:
                return {
                    "status": "matched",
                    "confidence": "high",
                    "method": "embedded_bvid_cid",
                    "record": candidates[0],
                }
            # 手机里已有 CID 时，它比只有 BV 的候选更具体。不得因
            # catalog 缺页或更新不完整，把冲突 CID 降级成 BV 自动命中。
            return {
                "status": "review",
                "confidence": "conflict" if base_candidates else "insufficient",
                "method": "embedded_bvid_cid_not_in_catalog",
                "candidates": base_candidates,
            }
        if len(base_candidates) == 1:
            only = base_candidates[0]
            known_multipart = bool((only.get("part_count") or 0) > 1 or only.get("part") is not None)
            if not known_multipart:
                return {"status": "matched", "confidence": "high", "method": "embedded_bvid", "record": only}
            return {
                "status": "review", "confidence": "insufficient", "method": "embedded_bvid_multipart_missing_cid",
                "candidates": base_candidates,
            }
        if base_candidates:
            return {
                "status": "review", "confidence": "ambiguous", "method": "embedded_bvid_missing_or_unknown_cid",
                "candidates": base_candidates,
            }
    cover_hash = track.get("cover_sha256") or ""
    cover_candidates = covers.get(cover_hash, []) if cover_hash else []
    if len(cover_candidates) == 1:
        only = cover_candidates[0]
        known_multipart = bool((only.get("part_count") or 0) > 1 or only.get("part") is not None)
        if known_multipart:
            return {
                "status": "review",
                "confidence": "insufficient",
                "method": "cover_sha256_multipart_catalog_incomplete",
                "candidates": cover_candidates,
            }
        check = duration_check(track, only)
        if check["status"] == "compatible":
            return {
                "status": "matched", "confidence": "high", "method": "cover_sha256_duration",
                "duration_check": check, "record": only,
            }
        if zero_duration_title_identity(track, only):
            return {
                "status": "matched",
                "confidence": "high",
                "method": "cover_sha256_zero_duration_title",
                "duration_check": check,
                "record": only,
            }
        return {
            "status": "review", "confidence": "insufficient", "method": f"cover_sha256_duration_{check['status']}",
            "duration_check": check, "candidates": cover_candidates,
        }
    if cover_candidates:
        return {
            "status": "review", "confidence": "ambiguous", "method": "cover_sha256_multiple_candidates",
            "candidates": cover_candidates,
        }
    return {"status": "unmatched", "confidence": "none", "method": "none", "candidates": []}


def _safe_current_title(track: dict[str, Any], is_cover: bool) -> str:
    title = str(track.get("title") or Path(track["filename"]).stem)
    if is_cover and title.endswith(f" [{prv.COVER_LABEL}]"):
        title = title[: -(len(prv.COVER_LABEL) + 3)]
    return prv.sanitize_title(title)


def _credits(record: dict[str, Any]) -> dict[str, Any]:
    if _extract_credits is None:
        return {"extraction_error": "credits.extract_credits unavailable"}
    try:
        result = _extract_credits(
            record.get("title", ""),
            record.get("description", ""),
            uploader=record.get("uploader", ""),
            uploader_id=record.get("uploader_id", ""),
        )
    except Exception as error:
        return {"extraction_error": str(error)}
    return result if isinstance(result, dict) else {}


def proposed_metadata(
    track: dict[str, Any],
    record: dict[str, Any],
    *,
    use_llm: bool = False,
    rename_files: bool = False,
    title_cache: dict[str, str] | None = None,
    title_cache_path: os.PathLike[str] | str = DEFAULT_TITLE_CACHE,
) -> dict[str, Any]:
    source_title = record.get("title") or track.get("comment") or track.get("title") or ""
    description = record.get("description") or ""
    artists = prv.extract_artists_from_sources(source_title, description) or track.get("artists") or []
    is_cover = prv.detect_cover_from_sources(source_title, description)
    title = (
        clean_title_cached(source_title, description, title_cache if title_cache is not None else {}, title_cache_path)
        if use_llm
        else _safe_current_title(track, is_cover)
    )
    preferred = prv.build_output_filename(artists, title, ".m4a", is_cover)
    target_filename = preferred if rename_files else track["filename"]
    return {
        "source_id": record.get("source_item_id") or record["bvid"],
        "source_item_id": record.get("source_item_id") or record["bvid"],
        "source_key": record["source_key"],
        "bvid": record["bvid"],
        "source_cid": record.get("cid", ""),
        "page": str(record.get("part") or ""),
        "part_title": record.get("title", "") if record.get("part") is not None else "",
        "source_title": source_title,
        "description": description,
        "uploader": record.get("uploader", ""),
        "uploader_id": record.get("uploader_id", ""),
        "upload_date": record.get("upload_date", ""),
        "publish_time": record.get("publish_time", ""),
        "title": title,
        "artists": artists,
        "is_cover": is_cover,
        "target_filename": target_filename,
        "preferred_filename": preferred,
        "credits": _credits(record),
        "description_truncated": bool(record.get("description_truncated")),
        "title_source": "llm" if use_llm else "existing_phone_tag",
    }


def build_plan(
    tracks: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    remote_dir: str,
    adb_serial: str,
    archive_sha256: str,
    use_llm: bool = False,
    rename_files: bool = False,
    title_cache: dict[str, str] | None = None,
    title_cache_path: os.PathLike[str] | str = DEFAULT_TITLE_CACHE,
    max_updates: int | None = None,
) -> dict[str, Any]:
    """Build a deterministic action list from phone-present files only."""
    if max_updates is not None and max_updates < 1:
        raise MigrationError("plan --pilot 必须大于 0")
    entries: list[dict[str, Any]] = []
    planned_updates = 0
    for track in tracks:
        entry: dict[str, Any] = {"phone": track}
        if track.get("scan_error"):
            entry.update({"action": "review", "match": {"status": "scan_error", "confidence": "none", "method": "scan_error"}})
            entries.append(entry)
            continue
        match = match_track(track, records)
        candidate_records = [match["record"]] if "record" in match else match.get("candidates", [])
        phone_evidence = instrumental_evidence(track.get("filename", ""), track.get("title", ""))
        matched_catalog_evidence = (
            instrumental_evidence(match["record"].get("title", "")) if match.get("status") == "matched" else []
        )
        entry["match"] = {
            key: value for key, value in match.items() if key not in {"record", "candidates"}
        }
        if "record" in match:
            entry["catalog"] = match["record"]
            eligible_for_update = bool(
                match.get("status") == "matched"
                and not (phone_evidence or matched_catalog_evidence)
                and (max_updates is None or planned_updates < max_updates)
            )
            entry["proposed"] = proposed_metadata(
                track,
                match["record"],
                use_llm=use_llm and eligible_for_update,
                rename_files=rename_files,
                title_cache=title_cache,
                title_cache_path=title_cache_path,
            )
        elif candidate_records:
            entry["candidates"] = candidate_records
        entry["instrumental_evidence"] = {
            "phone": phone_evidence,
            "matched_catalog": matched_catalog_evidence,
        }
        credits_failed = bool(entry.get("proposed", {}).get("credits", {}).get("extraction_error"))
        if credits_failed:
            entry["action"] = "review"
            entry["review_reason"] = "credit_extraction_error"
        elif phone_evidence or matched_catalog_evidence:
            entry["action"] = "delete_candidate"
        elif match["status"] == "matched":
            if max_updates is not None and planned_updates >= max_updates:
                entry["action"] = "review"
                entry["review_reason"] = "pilot_limit"
            else:
                entry["action"] = "update"
                planned_updates += 1
        else:
            entry["action"] = "review"
        entries.append(entry)

    counts = Counter(entry["action"] for entry in entries)
    summary = {
        "phone_files": len(tracks),
        "catalog_records": len(records),
        "update": counts["update"],
        "review": counts["review"],
        "delete_candidates": counts["delete_candidate"],
        "entries_sha256": hashlib.sha256(canonical_json(entries)).hexdigest(),
    }
    plan = {
        "version": PLAN_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "safety": {
            "phone_writes_during_plan": False,
            "missing_archive_items_restored": False,
            "deletions_require_allow_delete": True,
            "metadata_updates_require_plan_hash": True,
            "rename_files": rename_files,
            "title_cleaning_with_llm": use_llm,
            "title_cache": os.fspath(title_cache_path) if use_llm else "",
            "pilot_limit": max_updates,
        },
        "device": {"adb_serial": adb_serial, "remote_dir": remote_dir.rstrip("/") + "/"},
        "archive_sha256": archive_sha256,
        "summary": summary,
        "entries": entries,
    }
    return seal_plan(plan)


def write_plan(plan: dict[str, Any], json_path: os.PathLike[str] | str, csv_path: os.PathLike[str] | str) -> None:
    json_file = Path(json_path)
    csv_file = Path(csv_path)
    json_file.parent.mkdir(parents=True, exist_ok=True)
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    json_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = [
        "action", "filename", "match_status", "match_method", "source_id", "source_title",
        "target_filename", "artists", "uploader", "publish_time", "instrumental_evidence",
    ]
    with open(csv_file, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for entry in plan["entries"]:
            phone, proposed = entry.get("phone", {}), entry.get("proposed", {})
            writer.writerow({
                "action": entry.get("action", ""),
                "filename": phone.get("filename", ""),
                "match_status": entry.get("match", {}).get("status", ""),
                "match_method": entry.get("match", {}).get("method", ""),
                "source_id": proposed.get("source_id", ""),
                "source_title": proposed.get("source_title", ""),
                "target_filename": proposed.get("target_filename", ""),
                "artists": " / ".join(proposed.get("artists", [])),
                "uploader": proposed.get("uploader", ""),
                "publish_time": proposed.get("publish_time", ""),
                "instrumental_evidence": " / ".join(
                    entry.get("instrumental_evidence", {}).get("phone", [])
                    + entry.get("instrumental_evidence", {}).get("matched_catalog", [])
                ),
            })


def _validate_entry_paths(plan: dict[str, Any], actions: set[str]) -> list[dict[str, Any]]:
    remote_dir = plan["device"]["remote_dir"].rstrip("/")
    actionable = [entry for entry in plan["entries"] if entry.get("action") in actions]
    targets: set[str] = set()
    for entry in actionable:
        phone = entry.get("phone") or {}
        filename = phone.get("filename")
        remote_path = phone.get("remote_path")
        if not isinstance(filename, str) or posixpath.basename(filename) != filename:
            raise MigrationError("plan 含不安全的手机文件名")
        if remote_path != posixpath.join(remote_dir, filename):
            raise MigrationError(f"plan 手机路径不属于目标目录: {remote_path}")
        if entry["action"] == "update":
            target = entry.get("proposed", {}).get("target_filename")
            if not isinstance(target, str) or posixpath.basename(target) != target or not target.lower().endswith(".m4a"):
                raise MigrationError("plan 含不安全的目标文件名")
            folded = target.casefold()
            if folded in targets:
                raise MigrationError(f"plan 中多个条目指向同一目标: {target}")
            targets.add(folded)
    return actionable


def _preflight(
    plan: dict[str, Any],
    adb: ADBClient,
    actions: set[str],
    *,
    allow_rename: bool = False,
) -> list[dict[str, Any]]:
    actionable = _validate_entry_paths(plan, actions)
    current = set(adb.list_files(plan["device"]["remote_dir"]))
    for entry in actionable:
        source = entry["phone"]["remote_path"]
        if source not in current:
            raise MigrationError(f"手机文件在制定计划后消失，停止: {source}")
        if entry["action"] == "update":
            if entry["proposed"]["target_filename"] != entry["phone"]["filename"] and not allow_rename:
                raise MigrationError("计划包含重命名；需另外显式传入 --allow-rename")
            target = posixpath.join(
                plan["device"]["remote_dir"].rstrip("/"), entry["proposed"]["target_filename"]
            )
            if target != source and target in current:
                raise MigrationError(f"目标文件已存在，尚未修改任何文件: {target}")
    return actionable


def _backup_source(adb: ADBClient, entry: dict[str, Any], backup_dir: Path) -> Path:
    source = entry["phone"]["remote_path"]
    expected = entry["phone"].get("file_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise MigrationError(f"plan 缺少源文件 SHA-256: {source}")
    backup = backup_dir / entry["phone"]["filename"]
    if backup.exists():
        if sha256_file(backup) != expected:
            raise MigrationError(f"已有备份与计划不一致，拒绝覆盖: {backup}")
        return backup
    partial = backup_dir / f".partial-{expected}"
    if partial.exists():
        partial.unlink()  # Only this tool's incomplete local pull is removed.
    adb.pull(source, partial)
    if sha256_file(partial) != expected:
        partial.unlink()
        raise MigrationError(f"手机文件在制定计划后已变化: {source}")
    os.replace(partial, backup)
    return backup


def _write_credit_tags(audio: MP4, values: dict[str, Any]) -> None:
    for name, tag in CREDIT_TAGS.items():
        raw_values = values.get(name, []) if isinstance(values, dict) else []
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        cleaned = [str(value).strip() for value in raw_values if str(value).strip()]
        if cleaned:
            audio[tag] = [json.dumps(cleaned, ensure_ascii=False).encode("utf-8")]
        else:
            audio.pop(tag, None)


def _snapshot_unmanaged_tags(audio: MP4) -> dict[str, list[Any]]:
    """Capture tags this migration does not own, including cover and lyrics."""
    snapshot: dict[str, list[Any]] = {}
    for tag_name, values in (audio.tags or {}).items():
        if tag_name in MIGRATION_MANAGED_TAGS:
            continue
        snapshot[tag_name] = [
            bytes(value) if isinstance(value, (bytes, bytearray)) else value
            for value in values
        ]
    return snapshot


def enrich_local_copy(source: Path, target: Path, entry: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Write tags on a copy and prove the raw audio payload did not change."""
    proposed = entry["proposed"]
    original_stream_hash = hash_audio_stream(source)
    expected_stream_hash = entry["phone"].get("audio_stream_sha256")
    if expected_stream_hash != original_stream_hash:
        raise MigrationError("本地备份的音频流与计划不一致")
    unmanaged_tags = _snapshot_unmanaged_tags(MP4(source))
    shutil.copy2(source, target)
    credits_value = proposed.get("credits", {})
    credits_json = canonical_json(credits_value).decode("utf-8")
    state = {
        "version": prv.STATE_VERSION,
        "source_name": entry["phone"]["filename"],
        "source_id": proposed["source_id"],
        "final_name": proposed["target_filename"],
        "title": proposed["title"],
        "artists": proposed["artists"],
        "is_cover": bool(proposed["is_cover"]),
        "cover_embedded": bool(entry["phone"].get("cover_sha256")),
        "description_sha256": hashlib.sha256(proposed["description"].encode("utf-8")).hexdigest()
        if proposed["description"] else "",
        "uploader": proposed.get("uploader", ""),
        "uploader_id": proposed.get("uploader_id", ""),
        "upload_date": proposed.get("upload_date", ""),
        "publish_time": proposed.get("publish_time", ""),
    }
    if prv.STATE_VERSION >= 3:
        credits_schema = credits_value.get("schema_version", 1) if isinstance(credits_value, dict) else 1
        state.update({
            "source_item_id": proposed.get("source_item_id", proposed["source_id"]),
            "source_key": proposed.get("source_key", ""),
            "bvid": proposed.get("bvid", ""),
            "cid": proposed.get("source_cid", ""),
            "page": proposed.get("page", ""),
            "part_title": proposed.get("part_title", ""),
            "source_title_sha256": hashlib.sha256(proposed["source_title"].encode("utf-8")).hexdigest(),
            "credits_sha256": hashlib.sha256(credits_json.encode("utf-8")).hexdigest(),
            "credits_schema": credits_schema,
            "credit_extractor_version": f"credits.py:{credits_schema}",
            "description_truncated": bool(proposed.get("description_truncated")),
        })
    audio = MP4(target)
    metadata_args = (
        audio,
        state,
        proposed["source_title"],
        proposed["description"],
        proposed["artists"],
        proposed["title"],
        None,
    )
    if prv.STATE_VERSION >= 3:
        prv._write_metadata(*metadata_args, credits_value)
    else:
        prv._write_metadata(*metadata_args)
        if proposed.get("source_cid"):
            audio[SOURCE_CID_TAG] = [str(proposed["source_cid"]).encode("utf-8")]
        _write_credit_tags(audio, credits_value)
    audio.save()
    modified_stream_hash = hash_audio_stream(target)
    if modified_stream_hash != original_stream_hash:
        raise MigrationError("写标签后音频流发生变化，拒绝推送")
    verified = MP4(target)
    if prv._state_from_audio(verified) != state or not prv._metadata_matches_state(verified, state):
        raise MigrationError("写标签后的完整状态校验失败")
    if _snapshot_unmanaged_tags(verified) != unmanaged_tags:
        raise MigrationError("写标签后封面、歌词或其他非受管标签发生变化")
    return original_stream_hash, sha256_file(target), state


def apply_plan(
    plan: dict[str, Any],
    *,
    confirmed_hash: str,
    backup_dir: os.PathLike[str] | str,
    allow_rename: bool = False,
    max_updates: int | None = None,
    adb: ADBClient | None = None,
) -> dict[str, int]:
    """Apply a sealed plan.  Every destructive step is hash-guarded."""
    plan_hash = validate_plan(plan, confirmed_hash)
    serial = plan.get("device", {}).get("adb_serial", "")
    adb = adb or ADBClient(serial)
    if adb.serial != serial:
        raise MigrationError("当前 ADB 设备与制定计划时的设备不一致")
    actionable = _preflight(plan, adb, {"update"}, allow_rename=allow_rename)
    if max_updates is not None:
        if max_updates < 1:
            raise MigrationError("--pilot 必须大于 0")
        actionable = actionable[:max_updates]
    backups = Path(backup_dir, plan_hash[:16])
    backups.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    with tempfile.TemporaryDirectory(prefix="bilibili-phone-apply-") as working:
        for index, entry in enumerate(actionable, 1):
            backup = _backup_source(adb, entry, backups)
            source_remote = entry["phone"]["remote_path"]
            modified = Path(working, f"{index:06d}.m4a")
            stream_hash, modified_sha, expected_state = enrich_local_copy(backup, modified, entry)
            remote_dir = plan["device"]["remote_dir"].rstrip("/")
            # Some Android external-storage implementations acknowledge an ADB
            # push to a dot-prefixed M4A and then immediately hide/drop it.
            # A same-directory ``.part`` name remains rename-compatible while
            # also preventing MediaStore from indexing it as another song.
            temp_remote = posixpath.join(
                remote_dir,
                f"bilibili-migrate-{plan_hash[:12]}-{index:06d}.part",
            )
            if adb.exists(temp_remote):
                if adb.remote_sha256(temp_remote) != modified_sha:
                    raise MigrationError(f"手机端存在不同内容的迁移临时文件: {temp_remote}")
            else:
                adb.push(modified, temp_remote)
            if adb.remote_sha256(temp_remote) != modified_sha:
                raise MigrationError(f"手机端临时文件校验失败: {temp_remote}")
            target_remote = posixpath.join(remote_dir, entry["proposed"]["target_filename"])
            adb.atomic_replace(
                source_remote,
                temp_remote,
                target_remote,
                entry["phone"]["file_sha256"],
                modified_sha,
            )
            if adb.remote_sha256(target_remote) != modified_sha:
                raise MigrationError(f"替换后的手机文件校验失败；原文件备份位于 {backup}")
            verified_pull = Path(working, f"{index:06d}.verified.m4a")
            adb.pull(target_remote, verified_pull)
            if sha256_file(verified_pull) != modified_sha:
                raise MigrationError(f"回读后的整文件 SHA-256 不一致；原文件备份位于 {backup}")
            if hash_audio_stream(verified_pull) != stream_hash:
                raise MigrationError(f"回读后的音频流 SHA-256 不一致；原文件备份位于 {backup}")
            verified_audio = MP4(verified_pull)
            if (
                prv._state_from_audio(verified_audio) != expected_state
                or not prv._metadata_matches_state(verified_audio, expected_state)
            ):
                raise MigrationError(f"回读后的受管标签/state 校验失败；原文件备份位于 {backup}")
            counts["updated"] += 1
            print(
                f"手机补全 {index}/{len(actionable)}: {entry['phone']['filename']}",
                flush=True,
            )
            # Large batches can exceed a tmpfs quota if every modified and
            # verified copy survives until the outer TemporaryDirectory exits.
            # The persistent original backup is already outside this directory.
            modified.unlink(missing_ok=True)
            verified_pull.unlink(missing_ok=True)
    return dict(counts)


def delete_candidates(
    plan: dict[str, Any],
    *,
    confirmed_hash: str,
    backup_dir: os.PathLike[str] | str,
    allow_delete: bool,
    confirm_files: Iterable[str] = (),
    adb: ADBClient | None = None,
) -> dict[str, int]:
    """Delete only reviewed instrumental candidates; never enrich in this run."""
    if not allow_delete:
        raise MigrationError("删除候选必须显式传入 --allow-delete")
    plan_hash = validate_plan(plan, confirmed_hash)
    selected_names = set(confirm_files)
    if not selected_names:
        return {"deleted": 0}
    candidate_entries = {
        entry.get("phone", {}).get("filename"): entry
        for entry in plan["entries"]
        if entry.get("action") == "delete_candidate"
    }
    unknown = selected_names - set(candidate_entries)
    if unknown:
        raise MigrationError(f"--confirm-file 不是计划中的删除候选: {sorted(unknown)!r}")
    selected_plan = dict(plan)
    selected_plan["entries"] = [candidate_entries[name] for name in sorted(selected_names)]
    serial = plan.get("device", {}).get("adb_serial", "")
    adb = adb or ADBClient(serial)
    if adb.serial != serial:
        raise MigrationError("当前 ADB 设备与制定计划时的设备不一致")
    candidates = _preflight(selected_plan, adb, {"delete_candidate"})
    backups = Path(backup_dir, plan_hash[:16])
    backups.mkdir(parents=True, exist_ok=True)
    deleted = 0
    for entry in candidates:
        backup = _backup_source(adb, entry, backups)
        source_remote = entry["phone"]["remote_path"]
        if adb.remote_sha256(source_remote) != entry["phone"]["file_sha256"]:
            raise MigrationError(f"删除前手机文件已变化: {source_remote}")
        adb.delete_verified(source_remote, entry["phone"]["file_sha256"])
        if adb.exists(source_remote):
            raise MigrationError(f"删除后文件仍存在；本地备份位于 {backup}")
        deleted += 1
    return {"deleted": deleted}


def _load_plan(path: os.PathLike[str] | str) -> dict[str, Any]:
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationError(f"无法读取迁移计划: {path}") from error
    if not isinstance(plan, dict):
        raise MigrationError("迁移计划必须是 JSON 对象")
    return plan


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bilibili 手机旧曲库安全补全工具（默认不写手机）")
    subparsers = parser.add_subparsers(dest="command")

    catalog = subparsers.add_parser("catalog", help="只下载 archive 对应元数据与封面，不下载音频")
    catalog.add_argument("--archive", default="archive.txt")
    catalog.add_argument("--catalog-dir", default=DEFAULT_CATALOG_DIR)
    catalog.add_argument("--cookies-from-browser", default="")
    catalog.add_argument("--refresh", action="store_true")
    catalog.add_argument("--workers", type=int, default=4, help="并行元数据批次（1-8，默认 4）")

    plan = subparsers.add_parser("plan", help="只读扫描手机并生成 JSON/CSV 审阅计划")
    plan.add_argument("--archive", default="archive.txt")
    plan.add_argument("--catalog-dir", default=DEFAULT_CATALOG_DIR)
    plan.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    plan.add_argument("--adb-serial", default="")
    plan.add_argument("--json", default=DEFAULT_PLAN_FILE)
    plan.add_argument("--csv", default=DEFAULT_CSV_FILE)
    plan.add_argument("--use-llm", action="store_true", help="显式调用现有标题清洗模型")
    plan.add_argument("--title-cache", default=DEFAULT_TITLE_CACHE, help="模型结果即时落盘，支持中断续跑")
    plan.add_argument("--rename-files", action="store_true", help="计划重命名；默认保持所有手机路径")
    plan.add_argument(
        "--pilot",
        type=int,
        default=None,
        metavar="N",
        help="计划中只放行前 N 首高置信更新，其余转审阅",
    )

    apply = subparsers.add_parser("apply", help="确认计划哈希后备份并更新手机")
    apply.add_argument("--plan", default=DEFAULT_PLAN_FILE)
    apply.add_argument("--confirm-hash", required=True)
    apply.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    apply.add_argument("--allow-rename", action="store_true", help="另行允许计划中的改名；默认仅补标签")
    apply.add_argument("--pilot", type=int, default=None, metavar="N", help="本次最多更新 N 首；之后重新扫描再继续")

    delete = subparsers.add_parser("delete", help="独立删除伴奏候选，不在本次补标签")
    delete.add_argument("--plan", default=DEFAULT_PLAN_FILE)
    delete.add_argument("--confirm-hash", required=True)
    delete.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    delete.add_argument("--allow-delete", action="store_true", required=True)
    delete.add_argument(
        "--confirm-file",
        action="append",
        default=[],
        metavar="文件名.m4a",
        help="逐首确认；可重复。省略时删除 0 首",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        print("\n未指定子命令：没有读取或修改手机。")
        return 0
    try:
        if args.command == "catalog":
            fetch_catalog(
                args.archive,
                args.catalog_dir,
                cookies_from_browser=args.cookies_from_browser,
                refresh=args.refresh,
                workers=args.workers,
            )
            print(f"catalog 已保存到 {args.catalog_dir}（未下载音频）")
            return 0
        if args.command == "plan":
            archive = parse_archive(args.archive)
            records = load_catalog(args.catalog_dir, archive)
            if not records:
                raise MigrationError("catalog 中没有 archive 对应记录；请先运行 catalog 子命令")
            adb = ADBClient(args.adb_serial)
            tracks = scan_phone(adb, args.remote_dir)
            title_cache = load_title_cache(args.title_cache) if args.use_llm else None
            migration_plan = build_plan(
                tracks,
                records,
                remote_dir=args.remote_dir,
                adb_serial=adb.serial,
                archive_sha256=sha256_file(args.archive),
                use_llm=args.use_llm,
                rename_files=args.rename_files,
                title_cache=title_cache,
                title_cache_path=args.title_cache,
                max_updates=args.pilot,
            )
            write_plan(migration_plan, args.json, args.csv)
            print(json.dumps(migration_plan["summary"], ensure_ascii=False, indent=2))
            print(f"plan_hash: {migration_plan['plan_hash']}")
            print("只读扫描完成；手机未被修改。请先审阅 JSON/CSV。")
            return 0
        migration_plan = _load_plan(args.plan)
        if args.command == "apply":
            result = apply_plan(
                migration_plan,
                confirmed_hash=args.confirm_hash,
                backup_dir=args.backup_dir,
                allow_rename=args.allow_rename,
                max_updates=args.pilot,
            )
        else:
            result = delete_candidates(
                migration_plan,
                confirmed_hash=args.confirm_hash,
                backup_dir=args.backup_dir,
                allow_delete=args.allow_delete,
                confirm_files=args.confirm_file,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MigrationError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
