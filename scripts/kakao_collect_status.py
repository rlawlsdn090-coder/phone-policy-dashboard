#!/usr/bin/env python3
"""Report whether KakaoTalk image collection can run safely.

This helper does not read chat text. It only checks:
- whether the Mac console is locked,
- whether today's KakaoTalk downloads/inbox files exist,
- whether KakaoTalk's local media cache has recent encrypted image candidates.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CACHE_ROOT = Path(
    "/Users/jinwoo/Library/Containers/com.kakao.KakaoTalkMac/Data/Library/"
    "Application Support/com.kakao.KakaoTalkMac/"
    "a2180d30873c9a880661aeb33e8fa3120c6ba29e"
)
DEFAULT_WORKDIR = Path("/Users/jinwoo/Documents/Codex/2026-05-30/new-chat/policy-tracker")


def is_console_locked() -> bool | None:
    try:
        proc = subprocess.run(
            ["ioreg", "-n", "Root", "-d1"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    for line in proc.stdout.splitlines():
        if "IOConsoleLocked" in line:
            if "= Yes" in line:
                return True
            if "= No" in line:
                return False
    return None


def file_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def list_today_files(pattern: str) -> list[dict[str, Any]]:
    return [file_info(p) for p in sorted(Path("/").glob(pattern.lstrip("/")))]


def cache_summary(cache_root: Path, since_days: int) -> dict[str, Any]:
    if not cache_root.exists():
        return {"exists": False, "directories": []}

    now = datetime.now().timestamp()
    cutoff = now - since_days * 24 * 60 * 60
    directories = []
    for directory in sorted(p for p in cache_root.iterdir() if p.is_dir()):
        media = [p for p in directory.iterdir() if p.is_file() and p.suffix in {".img", ".thm"}]
        recent_img = [p for p in media if p.suffix == ".img" and p.stat().st_mtime >= cutoff]
        recent_thm = [p for p in media if p.suffix == ".thm" and p.stat().st_mtime >= cutoff]
        if not media and directory.name != "commonResource":
            continue
        latest = max((p.stat().st_mtime for p in media), default=None)
        directories.append(
            {
                "name": directory.name,
                "media_count": len(media),
                "recent_img_count": len(recent_img),
                "recent_thm_count": len(recent_thm),
                "latest_modified_at": (
                    datetime.fromtimestamp(latest).isoformat(timespec="seconds") if latest else None
                ),
                "recent_img_examples": [file_info(p) for p in sorted(recent_img, key=lambda x: x.stat().st_mtime)[-5:]],
            }
        )
    directories.sort(key=lambda item: (item["recent_img_count"], item["media_count"]), reverse=True)
    return {"exists": True, "directories": directories}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--since-days", type=int, default=3)
    args = parser.parse_args()

    date_compact = args.date.replace("-", "")
    inbox_dir = args.workdir / "inbox" / "kakao" / args.date
    downloads = list_today_files(f"/Users/jinwoo/Downloads/KakaoTalk_Photo_{args.date}*")
    inbox_files = [file_info(p) for p in sorted(inbox_dir.glob("*")) if p.is_file()]
    cache = cache_summary(args.cache_root, args.since_days)

    recent_cache_candidates = sum(d["recent_img_count"] for d in cache.get("directories", []))
    result = {
        "date": args.date,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "console_locked": is_console_locked(),
        "downloads_count": len(downloads),
        "downloads": downloads,
        "inbox_count": len(inbox_files),
        "inbox_files": inbox_files,
        "cache_recent_img_count": recent_cache_candidates,
        "cache": cache,
        "suggested_status": "ok_to_try_ui_collection",
    }

    if result["console_locked"] is True:
        result["suggested_status"] = "mac_locked_do_not_claim_no_change"
    elif downloads:
        result["suggested_status"] = "downloaded_images_ready_to_import"
    elif inbox_files:
        result["suggested_status"] = "inbox_images_ready_or_already_imported"
    elif recent_cache_candidates:
        result["suggested_status"] = "encrypted_cache_candidates_need_original_download"

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
