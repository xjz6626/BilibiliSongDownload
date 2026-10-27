#!/usr/bin/env python3
"""Resolve yt-dlp Bilibili video-page failures to official festival URLs."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx


VIEW_API_URL = "https://api.bilibili.com/x/web-interface/view"
MAX_LOG_BYTES = 16 * 1024 * 1024
ERROR_MARKER = "ERROR:"
INITIAL_STATE_ERROR_RE = re.compile(
    r"ERROR:\s*\[BiliBili\]\s+(?P<short_id>[0-9A-Za-z]{10}):\s+"
    r"Unable to extract initial state\b"
)


def extract_failed_bvids(log_text: str) -> tuple[list[str], int]:
    """Return unique retryable BVIDs and the count of unrelated error lines."""
    failed_bvids: list[str] = []
    matched_error_lines = 0
    total_error_lines = 0

    for line in log_text.splitlines():
        if ERROR_MARKER not in line:
            continue
        total_error_lines += 1
        match = INITIAL_STATE_ERROR_RE.search(line)
        if not match:
            continue
        matched_error_lines += 1
        bvid = f"BV{match.group('short_id')}"
        if bvid not in failed_bvids:
            failed_bvids.append(bvid)

    return failed_bvids, total_error_lines - matched_error_lines


def festival_url_from_payload(payload: object, expected_bvid: str) -> str | None:
    """Validate and normalize the official festival URL returned by Bilibili."""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    raw_url = data.get("festival_jump_url")
    if not isinstance(raw_url, str) or not raw_url:
        return None

    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"bilibili.com", "www.bilibili.com"}
        or port not in {None, 80, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/festival/")
    ):
        return None

    query_bvids = parse_qs(parsed.query).get("bvid", [])
    if not any(value.casefold() == expected_bvid.casefold() for value in query_bvids):
        return None

    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def resolve_festival_retries(
    log_text: str,
    lookup: Callable[[str], object],
) -> tuple[list[str], list[str]]:
    """Resolve every known initial-state error; report anything left unresolved."""
    failed_bvids, unrelated_error_count = extract_failed_bvids(log_text)
    retry_urls: list[str] = []
    unresolved: list[str] = []

    if unrelated_error_count:
        unresolved.append(f"另有 {unrelated_error_count} 条非活动页下载错误")

    for bvid in failed_bvids:
        try:
            payload = lookup(bvid)
        except Exception as error:  # Network/API failures must remain retryable next run.
            unresolved.append(f"{bvid}: 元数据查询失败 ({error})")
            continue
        festival_url = festival_url_from_payload(payload, bvid)
        if festival_url:
            retry_urls.append(festival_url)
        else:
            unresolved.append(f"{bvid}: 未返回匹配的官方活动页")

    return retry_urls, unresolved


def read_log(path: str) -> str:
    size = os.path.getsize(path)
    if size > MAX_LOG_BYTES:
        raise ValueError(f"日志超过 {MAX_LOG_BYTES} 字节上限")
    with open(path, encoding="utf-8", errors="replace") as log_file:
        return log_file.read()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(f"用法: {os.path.basename(sys.argv[0])} YT_DLP_LOG", file=sys.stderr)
        return 2

    try:
        log_text = read_log(argv[0])
    except (OSError, ValueError) as error:
        print(f"❌ 无法读取下载日志: {error}", file=sys.stderr)
        return 1

    with httpx.Client(
        timeout=15,
        follow_redirects=True,
        trust_env=False,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
        },
    ) as api_client:

        def lookup(bvid: str) -> object:
            response = api_client.get(VIEW_API_URL, params={"bvid": bvid})
            response.raise_for_status()
            return response.json()

        retry_urls, unresolved = resolve_festival_retries(log_text, lookup)

    for retry_url in retry_urls:
        print(retry_url)
    for message in unresolved:
        print(f"⚠️ {message}", file=sys.stderr)
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
