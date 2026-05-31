#!/usr/bin/env python3
import argparse
import csv
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
IMAGE_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "policy.sqlite"
DASHBOARD_PATH = ROOT / "dashboard" / "index.html"
REVIEW_PATH = ROOT / "dashboard" / "review.html"
CANDIDATE_REVIEW_PATH = ROOT / "dashboard" / "candidate_review.html"
PRIORITY_REVIEW_PATH = ROOT / "dashboard" / "priority_review.html"
EXPORT_DIR = DATA_DIR / "exports"

CAFE_ID = "30984571"
MENU_ID = "10"
KST = timezone(timedelta(hours=9))

TARGET_CARRIERS = {
    "umobile": ["유모비", "유모바일", "U+U MOBILE", "UMOBILE"],
    "hello": ["헬로", "Hello"],
    "ktm": ["KTM", "kt M mobile", "M mobile"],
    "skylife": ["스카이", "skylife"],
    "sk7": ["SK7", "SK 7", "7mobile"],
}

EXCLUDE_NAMES = ["이야기"]


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    with connect() as db:
        db.executescript(
            """
            create table if not exists posts (
                article_id integer primary key,
                subject text not null,
                write_ts integer not null,
                write_date text not null,
                summary text,
                url text not null,
                raw_json text,
                collected_at text not null
            );

            create table if not exists images (
                id integer primary key autoincrement,
                article_id integer not null,
                carrier text,
                original_name text,
                url text not null unique,
                local_path text,
                ocr_text_path text,
                ocr_status text default 'pending',
                extract_status text default 'pending',
                created_at text not null,
                foreign key(article_id) references posts(article_id)
            );

            create table if not exists policy_values (
                id integer primary key autoincrement,
                article_id integer not null,
                carrier text not null,
                plan_bucket text not null,
                plan_name text,
                promo_price integer,
                data_amount text,
                qos text,
                new_amount integer,
                mnp_amount integer,
                foreign_new_amount integer,
                foreign_mnp_amount integer,
                conditions text,
                status text not null default 'needs_review',
                source_image_id integer,
                created_at text not null,
                unique(article_id, carrier, plan_bucket, plan_name),
                foreign key(article_id) references posts(article_id),
                foreign key(source_image_id) references images(id)
            );

            create table if not exists run_log (
                id integer primary key autoincrement,
                command text not null,
                details text,
                created_at text not null
            );

            create table if not exists manual_values (
                id integer primary key autoincrement,
                article_id integer not null,
                carrier text not null,
                plan_bucket text not null,
                plan_name text,
                new_amount integer,
                mnp_amount integer,
                memo text,
                updated_at text not null,
                unique(article_id, carrier, plan_bucket),
                foreign key(article_id) references posts(article_id)
            );
            """
        )
        for statement in [
            "alter table images add column extract_status text default 'pending'",
        ]:
            try:
                db.execute(statement)
            except sqlite3.OperationalError:
                pass


def request_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://cafe.naver.com/gray9de5o",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_posts(page, per_page=100):
    url = (
        "https://apis.naver.com/cafe-web/cafe-boardlist-api/v1/"
        f"cafes/{CAFE_ID}/menus/{MENU_ID}/articles?"
        f"page={page}&perPage={per_page}&query=&sortBy=TIME"
    )
    data = request_json(url)
    return data.get("result", {}).get("articleList", [])


def article_detail(article_id):
    url = f"https://apis.naver.com/cafe-web/cafe-articleapi/v3/cafes/{CAFE_ID}/articles/{article_id}"
    return request_json(url)


def get_article_payload(detail):
    return detail.get("result", {}).get("article") or detail.get("article") or {}


def ts_to_date(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=KST).strftime("%Y-%m-%d %H:%M:%S")


def parse_since(value):
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=KST)


def is_policy_subject(subject):
    return "케이엘모바일_부성정책" in subject and "정책" in subject


def extract_image_urls(content):
    urls = re.findall(r'<img[^>]+src="([^"]+)"', content or "")
    result = []
    for url in urls:
        url = html.unescape(url)
        if "cafeptthumb" not in url:
            continue
        if any(x in urllib.parse.unquote(url) for x in EXCLUDE_NAMES):
            continue
        result.append(url)
    return result


def guess_name(url):
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    return name or "image.png"


def guess_carrier(name, url):
    probe = (name + " " + urllib.parse.unquote(url)).lower()
    for carrier, aliases in TARGET_CARRIERS.items():
        for alias in aliases:
            if alias.lower() in probe:
                return carrier
    return None


def normalize_image_url(url):
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    query["type"] = ["w1600"]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        normalize_image_url(url),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://cafe.naver.com/gray9de5o",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        dest.write_bytes(resp.read())


def collect(args):
    init_db()
    since = parse_since(args.since)
    found = 0
    saved = 0
    page = 1
    stop = False

    with connect() as db:
        while not stop:
            items = list_posts(page, args.per_page)
            if not items:
                break
            for wrapped in items:
                item = wrapped.get("item", {})
                subject = item.get("subject", "")
                ts_ms = item.get("writeDateTimestamp")
                if not ts_ms:
                    continue
                written = datetime.fromtimestamp(ts_ms / 1000, tz=KST)
                if written < since:
                    stop = True
                    continue
                if not is_policy_subject(subject):
                    continue

                found += 1
                article_id = item["articleId"]
                detail = article_detail(article_id)
                article = get_article_payload(detail)
                content = article.get("contentHtml") or article.get("content") or ""
                summary = item.get("summary", "")
                url = f"https://cafe.naver.com/ArticleRead.nhn?clubid={CAFE_ID}&articleid={article_id}&menuid={MENU_ID}"
                now = datetime.now(KST).isoformat()

                db.execute(
                    """
                    insert into posts(article_id, subject, write_ts, write_date, summary, url, raw_json, collected_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(article_id) do update set
                      subject=excluded.subject,
                      write_ts=excluded.write_ts,
                      write_date=excluded.write_date,
                      summary=excluded.summary,
                      raw_json=excluded.raw_json,
                      collected_at=excluded.collected_at
                    """,
                    (
                        article_id,
                        subject,
                        ts_ms,
                        ts_to_date(ts_ms),
                        summary,
                        url,
                        json.dumps(detail, ensure_ascii=False),
                        now,
                    ),
                )

                for img_url in extract_image_urls(content):
                    name = guess_name(img_url)
                    carrier = guess_carrier(name, img_url)
                    if not carrier:
                        continue
                    ext = Path(name).suffix or ".png"
                    local = IMAGE_DIR / str(article_id) / f"{carrier}{ext}"
                    if args.download and (args.force_download or not local.exists()):
                        download(img_url, local)
                    db.execute(
                        """
                        insert into images(article_id, carrier, original_name, url, local_path, created_at)
                        values (?, ?, ?, ?, ?, ?)
                        on conflict(url) do update set
                          carrier=excluded.carrier,
                          original_name=excluded.original_name,
                          local_path=excluded.local_path
                        """,
                        (article_id, carrier, name, normalize_image_url(img_url), str(local), now),
                    )
                saved += 1
                time.sleep(args.sleep)
            db.commit()
            page += 1

        db.execute(
            "insert into run_log(command, details, created_at) values (?, ?, ?)",
            ("collect", json.dumps({"found": found, "saved": saved, "since": args.since}, ensure_ascii=False), datetime.now(KST).isoformat()),
        )
    print(f"collected posts={saved}, matched={found}, db={DB_PATH}")


def run_ocr(args):
    if not shutil.which("tesseract"):
        raise SystemExit("tesseract is not installed")
    init_db()
    with connect() as db:
        rows = db.execute(
            """
            select id, local_path from images
            where local_path is not null
              and local_path != ''
              and (ocr_status is null or ocr_status in ('pending', 'failed'))
            order by article_id desc, carrier
            limit ?
            """,
            (args.limit,),
        ).fetchall()
        done = 0
        for image_id, local_path in rows:
            src = Path(local_path)
            if not src.exists():
                db.execute("update images set ocr_status='missing_file' where id=?", (image_id,))
                continue
            out_base = src.with_suffix("")
            txt_path = str(out_base) + ".ocr.txt"
            cmd = ["tesseract", str(src), str(out_base) + ".ocr", "-l", "kor+eng", "--psm", "6"]
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                db.execute(
                    "update images set ocr_text_path=?, ocr_status='done' where id=?",
                    (txt_path, image_id),
                )
                done += 1
            except subprocess.CalledProcessError:
                db.execute("update images set ocr_status='failed' where id=?", (image_id,))
        db.commit()
    print(f"ocr done={done}")


def parse_amounts(text):
    raw = re.sub(r"[^0-9]", "", text or "")
    if len(raw) < 5:
        return []
    out = []
    if len(raw) in (10, 12):
        size = len(raw) // 2
        parts = [raw[:size], raw[size:]]
    elif len(raw) in (15, 18, 20, 24):
        size = 5 if len(raw) in (15, 20) else 6
        parts = [raw[i : i + size] for i in range(0, len(raw), size)]
    else:
        parts = [raw]
    for part in parts:
        try:
            value = int(part)
        except ValueError:
            continue
        if 10000 <= value <= 300000:
            out.append(value)
    return out


def parse_money_tokens(text):
    values = []
    for token in re.findall(r"\d{1,3}[,.]\d{3}|\b\d{5,6}\b", text or ""):
        try:
            value = int(re.sub(r"[^0-9]", "", token))
        except ValueError:
            continue
        if 10000 <= value <= 300000:
            values.append(value)
    return values


def run_tsv(image_path, timeout=25, psm="6"):
    cmd = ["tesseract", str(image_path), "stdout", "-l", "kor+eng", "--psm", psm, "tsv"]
    proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    lines = proc.stdout.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) != len(header):
            continue
        row = dict(zip(header, cols))
        text = row.get("text", "").strip()
        if not text:
            continue
        try:
            row["left_i"] = int(float(row["left"]))
            row["top_i"] = int(float(row["top"]))
            row["conf_f"] = float(row["conf"])
        except Exception:
            continue
        rows.append(row)
    return rows


def candidate_from_values(bucket, text, values):
    if len(values) >= 4:
        values = values[-4:]
    if len(values) < 2:
        return None
    new_amount, mnp_amount = values[0], values[1]
    if mnp_amount < new_amount:
        return None
    if new_amount < 20000 or mnp_amount < 20000:
        return None
    if new_amount % 500 != 0 or mnp_amount % 500 != 0:
        return None
    return {
        "bucket": bucket,
        "text": text[:180],
        "new_amount": new_amount,
        "mnp_amount": mnp_amount,
        "status": "needs_review",
    }


def run_text_lines(image_path, psm="4", timeout=25):
    cmd = ["tesseract", str(image_path), "stdout", "-l", "kor+eng", "--psm", psm]
    proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def row_key(word):
    return (word.get("block_num"), word.get("par_num"), word.get("line_num"))


def detect_bucket(text):
    normalized = text.upper().replace(" ", "")
    if "7GB" in normalized or "7G" in normalized or "768" in normalized:
        return "7GB"
    if "11GB" in normalized or "11G" in normalized or "1168" in normalized or "11C" in normalized:
        return "11GB"
    return None


def extract_candidates_from_image(image_path):
    words = run_tsv(image_path, timeout=15, psm="6")
    grouped = {}
    for word in words:
        grouped.setdefault(row_key(word), []).append(word)
    candidates = []
    for group_words in grouped.values():
        group_words.sort(key=lambda w: w["left_i"])
        text = " ".join(w["text"] for w in group_words)
        bucket = detect_bucket(text)
        if not bucket:
            continue
        amounts = []
        for word in group_words:
            if word["left_i"] < 520:
                continue
            for value in parse_amounts(word["text"]):
                amounts.append((word["left_i"], value))
        amounts = sorted(amounts, key=lambda x: x[0])
        dedup = []
        for x, value in amounts:
            if not dedup or dedup[-1][1] != value or abs(dedup[-1][0] - x) > 12:
                dedup.append((x, value))
        values = [value for _x, value in dedup]
        # The policy amount columns are the four rightmost money values:
        # Korean new/MNP, foreign new/MNP. Earlier values are usually promo prices.
        candidate = candidate_from_values(bucket, text, values)
        if candidate:
            candidates.append(candidate)
    if candidates:
        return candidates

    words = run_tsv(image_path, timeout=10, psm="4")
    bands = {}
    for word in words:
        y = word["top_i"] + int(float(word.get("height", 0) or 0) / 2)
        key = round(y / 18) * 18
        bands.setdefault(key, []).append(word)
    for group_words in bands.values():
        group_words.sort(key=lambda w: w["left_i"])
        text = " ".join(w["text"] for w in group_words)
        bucket = detect_bucket(text)
        if not bucket:
            continue
        amounts = []
        for word in group_words:
            if word["left_i"] < 600:
                continue
            for value in parse_money_tokens(word["text"]):
                amounts.append((word["left_i"], value))
        amounts = sorted(amounts, key=lambda x: x[0])
        values = [value for _x, value in amounts]
        candidate = candidate_from_values(bucket, text, values)
        if candidate:
            candidates.append(candidate)
    if candidates:
        return candidates

    for line in run_text_lines(image_path, psm="4", timeout=10):
        bucket = detect_bucket(line)
        if not bucket:
            continue
        values = parse_money_tokens(line)
        candidate = candidate_from_values(bucket, line, values)
        if candidate:
            candidates.append(candidate)
    return candidates


def extract_values(args):
    init_db()
    with connect() as db:
        missing_filter = ""
        if args.missing_only:
            missing_filter = """
              and coalesce(images.extract_status, 'pending') not in ('done', 'failed', 'timeout', 'missing_file')
              and not exists (select 1 from policy_values v where v.source_image_id = images.id)
              and not exists (select 1 from manual_values m where m.article_id = images.article_id and m.carrier = images.carrier)
            """
        order = "article_id asc, carrier" if getattr(args, "oldest_first", False) else "article_id desc, carrier"
        rows = db.execute(
            f"""
            select id, article_id, carrier, local_path
            from images
            where local_path is not null and local_path != ''
              {missing_filter}
            order by {order}
            limit ?
            """,
            (args.limit,),
        ).fetchall()
        inserted = 0
        now = datetime.now(KST).isoformat()
        total = len(rows)
        for index, (image_id, article_id, carrier, local_path) in enumerate(rows, start=1):
            if getattr(args, "verbose", False):
                print(f"extract {index}/{total} article={article_id} carrier={carrier}", flush=True)
            path = Path(local_path)
            if not path.exists():
                db.execute("update images set extract_status='missing_file' where id=?", (image_id,))
                db.commit()
                continue
            try:
                candidates = extract_candidates_from_image(path)
            except subprocess.TimeoutExpired:
                db.execute("update images set extract_status='timeout' where id=?", (image_id,))
                db.commit()
                print(f"extract timeout image_id={image_id}", file=sys.stderr)
                continue
            except Exception as exc:
                print(f"extract failed image_id={image_id}: {exc}", file=sys.stderr)
                db.execute("update images set extract_status='failed' where id=?", (image_id,))
                db.commit()
                continue
            selected = {}
            for cand in candidates:
                current = selected.get(cand["bucket"])
                if (
                    current is None
                    or (cand["mnp_amount"], cand["new_amount"]) > (current["mnp_amount"], current["new_amount"])
                ):
                    selected[cand["bucket"]] = cand
            for bucket, cand in selected.items():
                db.execute(
                    """
                    insert into policy_values(
                      article_id, carrier, plan_bucket, plan_name,
                      new_amount, mnp_amount, conditions, status, source_image_id, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(article_id, carrier, plan_bucket, plan_name) do update set
                      new_amount=excluded.new_amount,
                      mnp_amount=excluded.mnp_amount,
                      conditions=excluded.conditions,
                      status=excluded.status,
                      source_image_id=excluded.source_image_id
                    """,
                    (
                        article_id,
                        carrier,
                        bucket,
                        "auto_candidate",
                        cand["new_amount"],
                        cand["mnp_amount"],
                        cand["text"],
                        cand["status"],
                        image_id,
                        now,
                    ),
                )
                inserted += 1
            db.execute("update images set extract_status='done' where id=?", (image_id,))
            db.commit()
    print(f"extracted candidate rows={inserted}")


def money(value):
    if value is None:
        return ""
    return f"{value:,}"


def parse_int(value):
    value = re.sub(r"[^0-9]", "", str(value or ""))
    return int(value) if value else None


def upsert_manual_value(db, row, now):
    article_id = int(row["article_id"])
    carrier = row["carrier"]
    plan_bucket = row["plan_bucket"]
    new_amount = parse_int(row.get("new_amount"))
    mnp_amount = parse_int(row.get("mnp_amount"))
    if new_amount is None and mnp_amount is None:
        return 0
    db.execute(
        """
        insert into manual_values(article_id, carrier, plan_bucket, plan_name, new_amount, mnp_amount, memo, updated_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(article_id, carrier, plan_bucket) do update set
          plan_name=excluded.plan_name,
          new_amount=excluded.new_amount,
          mnp_amount=excluded.mnp_amount,
          memo=excluded.memo,
          updated_at=excluded.updated_at
        """,
        (
            article_id,
            carrier,
            plan_bucket,
            row.get("plan_name") or "manual",
            new_amount,
            mnp_amount,
            row.get("memo") or "",
            now,
        ),
    )
    has_policy_row = db.execute(
        """
        select 1
        from policy_values
        where article_id=? and carrier=? and plan_bucket=?
        limit 1
        """,
        (article_id, carrier, plan_bucket),
    ).fetchone()
    if not has_policy_row:
        image_row = db.execute(
            """
            select id
            from images
            where article_id=? and carrier=?
            order by id
            limit 1
            """,
            (article_id, carrier),
        ).fetchone()
        source_image_id = image_row[0] if image_row else None
        db.execute(
            """
            insert into policy_values(
              article_id, carrier, plan_bucket, plan_name,
              new_amount, mnp_amount, conditions, status, source_image_id, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_id,
                carrier,
                plan_bucket,
                "manual_seed",
                new_amount,
                mnp_amount,
                row.get("memo") or "manual value",
                "manual_fixed",
                source_image_id,
                now,
            ),
        )
    return 1


def review_priority(new_amount, mnp_amount, source_text):
    text = source_text or ""
    tokens = parse_money_tokens(text)
    if new_amount is None or mnp_amount is None:
        return "missing"
    if len(tokens) < 4:
        return "high"
    if new_amount == mnp_amount and mnp_amount <= 40000:
        return "high"
    if mnp_amount >= 200000 or new_amount >= 120000:
        return "medium"
    return "normal"


def generate_dashboard(_args):
    init_db()
    with connect() as db:
        posts = db.execute(
            "select article_id, subject, write_date from posts order by write_ts desc"
        ).fetchall()
        values = db.execute(
            """
            select p.write_date, p.article_id, p.subject, v.carrier, v.plan_bucket,
                   coalesce(m.plan_name, v.plan_name) plan_name,
                   coalesce(m.new_amount, v.new_amount) new_amount,
                   coalesce(m.mnp_amount, v.mnp_amount) mnp_amount,
                   case when m.id is not null then 'manual_fixed' else v.status end status
            from policy_values v
            join posts p on p.article_id = v.article_id
            left join manual_values m
              on m.article_id = v.article_id
             and m.carrier = v.carrier
             and m.plan_bucket = v.plan_bucket
            order by p.write_ts, v.carrier, v.plan_bucket
            """
        ).fetchall()
        images = db.execute(
            """
            select p.write_date, p.article_id, p.subject, i.carrier, i.local_path, i.ocr_status
            from images i join posts p on p.article_id=i.article_id
            order by p.write_ts desc, i.carrier
            limit 50
            """
        ).fetchall()
        coverage = db.execute(
            """
            select substr(p.write_date, 1, 7) month,
                   count(distinct p.article_id) posts,
                   count(distinct i.id) images,
                   count(v.id) value_rows
            from posts p
            left join images i on i.article_id = p.article_id
            left join policy_values v on v.source_image_id = i.id
            group by month
            order by month
            """
        ).fetchall()
        monthly_values = db.execute(
            """
            with effective as (
              select p.write_date, v.carrier, v.plan_bucket,
                     coalesce(m.new_amount, v.new_amount) new_amount,
                     coalesce(m.mnp_amount, v.mnp_amount) mnp_amount
              from policy_values v
              join posts p on p.article_id = v.article_id
              left join manual_values m
                on m.article_id = v.article_id
               and m.carrier = v.carrier
               and m.plan_bucket = v.plan_bucket
            )
            select substr(write_date, 1, 7) month, carrier, plan_bucket,
                   round(avg(new_amount), 1) avg_new,
                   round(avg(mnp_amount), 1) avg_mnp
            from effective
            group by month, carrier, plan_bucket
            order by month, carrier, plan_bucket
            """
        ).fetchall()

    payload = [
        {
            "write_date": r[0],
            "date": r[0][:10],
            "article_id": r[1],
            "subject": r[2],
            "carrier": r[3],
            "bucket": r[4],
            "plan": r[5],
            "new": r[6],
            "mnp": r[7],
            "status": r[8],
        }
        for r in values
    ]
    monthly_payload = [
        {
            "month": r[0],
            "carrier": r[1],
            "bucket": r[2],
            "avg_new": r[3],
            "avg_mnp": r[4],
        }
        for r in monthly_values
    ]
    posts_payload = [
        {
            "article_id": aid,
            "subject": subject,
            "write_date": write_date,
            "date": write_date[:10],
        }
        for aid, subject, write_date in posts
    ]
    images_payload = [
        {
            "write_date": write_date,
            "date": write_date[:10],
            "article_id": article_id,
            "subject": subject,
            "carrier": carrier,
            "file": Path(local_path or "").name,
            "ocr_status": ocr_status,
        }
        for write_date, article_id, subject, carrier, local_path, ocr_status in images
    ]
    coverage_payload = [
        {
            "month": month,
            "posts": post_count,
            "images": image_count,
            "values": value_count,
        }
        for month, post_count, image_count, value_count in coverage
    ]
    meta_payload = {
        "posts": len(posts),
        "values": len(values),
        "images": sum(row["images"] for row in coverage_payload),
        "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }
    DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    dashboard_html = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>알뜰폰 정책 인사이트 대시보드</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #08090a;
      --panel: #101114;
      --panel-2: #15171b;
      --panel-3: #1b1e24;
      --line: rgba(255,255,255,.08);
      --line-strong: rgba(255,255,255,.14);
      --text: #f4f6f8;
      --muted: #a5acb8;
      --faint: #69707d;
      --accent: #7170ff;
      --accent-2: #38bdf8;
      --good: #30d158;
      --warn: #ffb020;
      --bad: #ff5f57;
      --radius: 8px;
      --shadow: 0 20px 70px rgba(0,0,0,.36);
    }
    * { box-sizing: border-box; }
    html { background: var(--bg); }
    body {
      margin: 0;
      min-width: 320px;
      color: var(--text);
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background:
        linear-gradient(180deg, rgba(113,112,255,.12), transparent 280px),
        radial-gradient(circle at top right, rgba(56,189,248,.12), transparent 360px),
        var(--bg);
    }
    button, input, select {
      font: inherit;
    }
    button {
      appearance: none;
      border: 1px solid var(--line);
      color: var(--muted);
      background: rgba(255,255,255,.035);
      border-radius: 7px;
      min-height: 34px;
      padding: 0 10px;
      cursor: pointer;
      transition: background .16s ease, border-color .16s ease, color .16s ease;
    }
    button:hover {
      color: var(--text);
      border-color: var(--line-strong);
      background: rgba(255,255,255,.07);
    }
    button.active {
      color: #fff;
      border-color: rgba(113,112,255,.75);
      background: rgba(113,112,255,.22);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.03);
    }
    a {
      color: var(--accent-2);
      text-decoration: none;
    }
    .shell {
      width: min(1680px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 22px 0 32px;
    }
    .topbar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: start;
      padding: 16px 0 18px;
    }
    .eyebrow {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .dot {
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--good);
      box-shadow: 0 0 16px rgba(48,209,88,.7);
    }
    h1 {
      margin: 8px 0 0;
      font-size: clamp(24px, 4vw, 46px);
      line-height: 1.04;
      font-weight: 650;
      letter-spacing: 0;
    }
    .subcopy {
      max-width: 860px;
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.55;
    }
    .quick-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .primary-action {
      color: #fff;
      border-color: rgba(56,189,248,.55);
      background: rgba(56,189,248,.16);
    }
    .toolbar {
      position: sticky;
      top: 0;
      z-index: 10;
      display: grid;
      gap: 12px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: rgba(12,13,16,.88);
      backdrop-filter: blur(18px);
      box-shadow: var(--shadow);
    }
    .toolbar-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .toolbar-label {
      width: 74px;
      color: var(--faint);
      font-size: 12px;
      font-weight: 650;
    }
    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 16px 0 12px;
    }
    .kpi, .panel, .table-panel {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.02));
      box-shadow: 0 1px 0 rgba(255,255,255,.03) inset;
    }
    .kpi {
      min-height: 112px;
      padding: 16px;
    }
    .kpi-label {
      color: var(--faint);
      font-size: 12px;
      font-weight: 650;
    }
    .kpi-value {
      margin-top: 12px;
      font-size: clamp(22px, 3vw, 34px);
      line-height: 1;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }
    .kpi-note {
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.48fr) minmax(360px, .82fr);
      gap: 12px;
      margin-top: 12px;
    }
    .panel {
      min-width: 0;
      padding: 16px;
    }
    .panel-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .panel-title {
      margin: 0;
      font-size: 16px;
      font-weight: 680;
      line-height: 1.25;
    }
    .panel-caption {
      margin-top: 5px;
      color: var(--faint);
      font-size: 12px;
      line-height: 1.45;
    }
    .chart-wrap {
      position: relative;
      height: 420px;
      border: 1px solid rgba(255,255,255,.06);
      border-radius: var(--radius);
      background: #0b0c0f;
      overflow: hidden;
    }
    canvas {
      width: 100%;
      height: 100%;
      display: block;
    }
    .tooltip {
      position: absolute;
      display: none;
      pointer-events: none;
      max-width: min(320px, calc(100% - 24px));
      padding: 10px 12px;
      border: 1px solid var(--line-strong);
      border-radius: var(--radius);
      background: rgba(9,10,12,.94);
      box-shadow: 0 20px 60px rgba(0,0,0,.42);
      color: var(--text);
      font-size: 12px;
      line-height: 1.45;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 12px;
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .swatch {
      width: 9px;
      height: 9px;
      border-radius: 2px;
      background: currentColor;
    }
    .matrix {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
      margin-top: 12px;
    }
    .carrier-card {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: rgba(255,255,255,.025);
      min-width: 0;
      padding: 12px;
    }
    .carrier-name {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 10px;
    }
    .carrier-mark {
      width: 9px;
      height: 9px;
      border-radius: 2px;
      background: var(--accent);
    }
    .amount-row {
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr);
      gap: 8px;
      align-items: center;
      padding: 8px 0;
      border-top: 1px solid rgba(255,255,255,.06);
    }
    .bucket {
      color: var(--faint);
      font-size: 12px;
      font-weight: 700;
    }
    .amount {
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      font-size: 14px;
      font-weight: 680;
    }
    .delta {
      margin-top: 4px;
      font-size: 11px;
      color: var(--faint);
      font-variant-numeric: tabular-nums;
    }
    .delta.up { color: var(--good); }
    .delta.down { color: var(--bad); }
    .up { color: var(--good); }
    .down { color: var(--bad); }
    .side-list {
      display: grid;
      gap: 8px;
    }
    .change-item, .post-item {
      border: 1px solid rgba(255,255,255,.06);
      border-radius: var(--radius);
      background: rgba(255,255,255,.025);
      padding: 11px;
    }
    .item-top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: baseline;
      color: var(--muted);
      font-size: 12px;
    }
    .item-main {
      margin-top: 6px;
      font-size: 13px;
      line-height: 1.4;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 0 7px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
      white-space: nowrap;
    }
    .pill.manual { color: var(--good); border-color: rgba(48,209,88,.26); background: rgba(48,209,88,.08); }
    .pill.review { color: var(--warn); border-color: rgba(255,176,32,.3); background: rgba(255,176,32,.08); }
    .table-panel {
      margin-top: 12px;
      overflow: hidden;
    }
    .table-scroll {
      overflow: auto;
      max-height: 520px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 920px;
      font-size: 12px;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid rgba(255,255,255,.06);
      text-align: left;
      vertical-align: middle;
      white-space: nowrap;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 2;
      color: var(--faint);
      background: #101114;
      font-size: 11px;
      font-weight: 700;
    }
    td {
      color: var(--muted);
    }
    td.strong {
      color: var(--text);
      font-weight: 650;
      font-variant-numeric: tabular-nums;
    }
    .subject-cell {
      max-width: 420px;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .footer-note {
      margin: 18px 0 0;
      color: var(--faint);
      font-size: 12px;
      line-height: 1.5;
    }
    @media (max-width: 1180px) {
      .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; }
      .matrix { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 720px) {
      .shell { width: min(100vw - 20px, 1680px); padding-top: 10px; }
      .topbar { grid-template-columns: 1fr; }
      .quick-actions { justify-content: flex-start; }
      .toolbar-label { width: 100%; }
      .kpis, .matrix { grid-template-columns: 1fr; }
      .chart-wrap { height: 340px; }
      h1 { font-size: 28px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <div class="eyebrow"><span class="dot"></span><span>Open Design · Linear-App inspired dashboard</span><span id="generatedAt"></span></div>
        <h1>알뜰폰 정책 인사이트</h1>
        <p class="subcopy">유모비, 헬로, KTM, 스카이, SK7의 7GB/11GB 정책 금액을 신규 / 번호이동 기준으로 추적합니다. 자동 후보값은 판독 상태를 함께 봐야 합니다.</p>
      </div>
      <div class="quick-actions">
        <a href="priority_review.html"><button type="button">우선 검수</button></a>
        <a href="review.html"><button type="button">이미지 검토</button></a>
        <a href="../data/exports/policy_values_daily.csv"><button class="primary-action" type="button">CSV 열기</button></a>
      </div>
    </header>

    <section class="toolbar" aria-label="대시보드 필터">
      <div class="toolbar-row" id="carrierControls"><span class="toolbar-label">통신사</span></div>
      <div class="toolbar-row" id="viewControls"><span class="toolbar-label">보기</span></div>
      <div class="toolbar-row" id="statusControls"><span class="toolbar-label">판독</span></div>
    </section>

    <section class="kpis" id="kpis"></section>

    <section class="layout">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title" id="chartTitle">정책금 추이</h2>
            <div class="panel-caption" id="chartCaption"></div>
          </div>
          <span class="pill" id="activeState"></span>
        </div>
        <div class="chart-wrap">
          <canvas id="trendChart"></canvas>
          <div class="tooltip" id="chartTooltip"></div>
        </div>
        <div class="legend" id="legend"></div>
      </div>

      <aside class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">최근 변동 포착</h2>
            <div class="panel-caption">선택한 필터 안에서 직전 차수 대비 변화가 큰 항목입니다.</div>
          </div>
        </div>
        <div class="side-list" id="changesList"></div>
      </aside>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2 class="panel-title">현재 정책 스냅샷</h2>
          <div class="panel-caption">각 통신사 최신값입니다. 표기는 항상 신규 / 번호이동입니다.</div>
        </div>
        <span class="pill" id="latestPost"></span>
      </div>
      <div class="matrix" id="matrix"></div>
    </section>

    <section class="layout">
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">월별 커버리지</h2>
            <div class="panel-caption">게시글, 이미지, 추출값 누적 상태입니다.</div>
          </div>
        </div>
        <div class="chart-wrap" style="height:260px">
          <canvas id="coverageChart"></canvas>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">최근 정책글</h2>
            <div class="panel-caption">수집 기준 최신 게시글 순서입니다.</div>
          </div>
        </div>
        <div class="side-list" id="postsList"></div>
      </div>
    </section>

    <section class="table-panel">
      <div class="panel-head" style="padding:16px 16px 0">
        <div>
          <h2 class="panel-title">선택 항목 상세</h2>
          <div class="panel-caption">필터에 맞는 원천 후보값입니다. needs_review는 원본 이미지 대조 전 후보입니다.</div>
        </div>
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>날짜</th>
              <th>차수</th>
              <th>통신사</th>
              <th>요금제</th>
              <th>신규</th>
              <th>번호이동</th>
              <th>상태</th>
              <th>제목</th>
            </tr>
          </thead>
          <tbody id="detailRows"></tbody>
        </table>
      </div>
    </section>

    <p class="footer-note">금액은 원 단위입니다. 자동 OCR 후보값은 검수 화면에서 이미지 표와 대조한 뒤 확정값으로 보는 것이 안전합니다.</p>
  </main>
  <script>
    const rows = __ROWS_JSON__;
    const monthlyRows = __MONTHLY_JSON__;
    const posts = __POSTS_JSON__;
    const images = __IMAGES_JSON__;
    const coverageRows = __COVERAGE_JSON__;
    const meta = __META_JSON__;

    const carrierLabels = {
      all: "전체",
      umobile: "유모비",
      hello: "헬로",
      ktm: "KTM",
      skylife: "스카이",
      sk7: "SK7"
    };
    const carrierOrder = ["umobile", "hello", "ktm", "skylife", "sk7"];
    const carrierColors = {
      umobile: "#d946ef",
      hello: "#f43f5e",
      ktm: "#ef4444",
      skylife: "#38bdf8",
      sk7: "#22c55e"
    };
    const state = {
      carrier: "all",
      bucket: "7GB",
      metric: "mnp",
      cadence: "daily",
      status: "all"
    };
    let trendPoints = [];

    const fmtMoney = (value) => value == null ? "-" : Number(value).toLocaleString("ko-KR");
    const fmtAmount = (row) => row ? fmtMoney(row.new) + " / " + fmtMoney(row.mnp) : "-";
    const metricLabel = () => state.metric === "mnp" ? "번호이동" : "신규";
    const statusLabel = (status) => status === "manual_fixed" ? "확정" : "후보";
    const statusClass = (status) => status === "manual_fixed" ? "manual" : "review";
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));

    function latestRows() {
      const byKey = new Map();
      rows.forEach((row) => {
        const key = row.carrier + "|" + row.bucket;
        const current = byKey.get(key);
        if (!current || row.write_date > current.write_date) byKey.set(key, row);
      });
      return byKey;
    }

    function previousFor(row) {
      const candidates = rows
        .filter((item) => item.carrier === row.carrier && item.bucket === row.bucket && item.write_date < row.write_date)
        .sort((a, b) => b.write_date.localeCompare(a.write_date));
      return candidates[0] || null;
    }

    function deltaClass(delta) {
      if (delta > 0) return "up";
      if (delta < 0) return "down";
      return "";
    }

    function deltaText(row, field) {
      const prev = row ? previousFor(row) : null;
      if (!row || !prev) return "이전값 없음";
      const delta = row[field] - prev[field];
      if (!delta) return "변동 없음";
      const sign = delta > 0 ? "+" : "";
      return fmtMoney(prev[field]) + " → " + fmtMoney(row[field]) + " (" + sign + fmtMoney(delta) + ")";
    }

    function filteredRows() {
      return rows.filter((row) => {
        if (state.carrier !== "all" && row.carrier !== state.carrier) return false;
        if (row.bucket !== state.bucket) return false;
        if (state.status !== "all" && row.status !== state.status) return false;
        return true;
      });
    }

    function buildControls() {
      const carrierWrap = document.getElementById("carrierControls");
      carrierWrap.innerHTML = '<span class="toolbar-label">통신사</span>';
      ["all", ...carrierOrder].forEach((carrier) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = carrierLabels[carrier];
        button.dataset.key = carrier;
        button.className = state.carrier === carrier ? "active" : "";
        button.onclick = () => { state.carrier = carrier; render(); };
        carrierWrap.appendChild(button);
      });

      const viewWrap = document.getElementById("viewControls");
      viewWrap.innerHTML = '<span class="toolbar-label">보기</span>';
      [
        ["bucket", "7GB", "7GB"],
        ["bucket", "11GB", "11GB"],
        ["metric", "new", "신규"],
        ["metric", "mnp", "번호이동"],
        ["cadence", "daily", "일별"],
        ["cadence", "monthly", "월별"]
      ].forEach(([field, value, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.className = state[field] === value ? "active" : "";
        button.onclick = () => { state[field] = value; render(); };
        viewWrap.appendChild(button);
      });

      const statusWrap = document.getElementById("statusControls");
      statusWrap.innerHTML = '<span class="toolbar-label">판독</span>';
      [
        ["all", "전체"],
        ["manual_fixed", "확정값"],
        ["needs_review", "후보값"]
      ].forEach(([value, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.className = state.status === value ? "active" : "";
        button.onclick = () => { state.status = value; render(); };
        statusWrap.appendChild(button);
      });
    }

    function buildTrendData() {
      if (state.cadence === "monthly") {
        const labels = [...new Set(monthlyRows.map((row) => row.month))];
        const carriers = state.carrier === "all" ? carrierOrder : [state.carrier];
        const field = state.metric === "mnp" ? "avg_mnp" : "avg_new";
        const datasets = carriers.map((carrier) => ({
          carrier,
          label: carrierLabels[carrier],
          color: carrierColors[carrier],
          points: labels.map((label) => {
            const row = monthlyRows.find((item) => item.month === label && item.carrier === carrier && item.bucket === state.bucket);
            return { label, value: row ? row[field] : null, raw: row };
          })
        }));
        return { labels, datasets };
      }

      const source = rows.filter((row) => row.bucket === state.bucket && (state.status === "all" || row.status === state.status));
      const postKeys = [...new Map(source.map((row) => [String(row.article_id), row])).values()]
        .sort((a, b) => a.write_date.localeCompare(b.write_date))
        .map((row) => ({ id: row.article_id, label: row.date.slice(5) + " #" + row.article_id }));
      const carriers = state.carrier === "all" ? carrierOrder : [state.carrier];
      const datasets = carriers.map((carrier) => ({
        carrier,
        label: carrierLabels[carrier],
        color: carrierColors[carrier],
        points: postKeys.map((post) => {
          const row = source.find((item) => item.article_id === post.id && item.carrier === carrier);
          return { label: post.label, value: row ? row[state.metric] : null, raw: row };
        })
      }));
      return { labels: postKeys.map((post) => post.label), datasets };
    }

    function drawLineChart(canvas, data, options = {}) {
      const ctx = canvas.getContext("2d");
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * ratio));
      canvas.height = Math.max(1, Math.floor(rect.height * ratio));
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      const width = rect.width;
      const height = rect.height;
      ctx.clearRect(0, 0, width, height);
      const pad = { top: 26, right: 24, bottom: 44, left: 62 };
      const values = data.datasets.flatMap((set) => set.points.map((point) => point.value)).filter((value) => value != null);
      if (!values.length) {
        ctx.fillStyle = "#69707d";
        ctx.font = "13px system-ui";
        ctx.fillText("표시할 데이터가 없습니다.", 24, 36);
        trendPoints = [];
        return;
      }
      const min = Math.max(0, Math.floor(Math.min(...values) / 10000) * 10000 - 10000);
      const max = Math.ceil(Math.max(...values) / 10000) * 10000 + 10000;
      const span = Math.max(1, max - min);
      const plotW = Math.max(1, width - pad.left - pad.right);
      const plotH = Math.max(1, height - pad.top - pad.bottom);
      const xFor = (index) => pad.left + (data.labels.length <= 1 ? plotW / 2 : (index / (data.labels.length - 1)) * plotW);
      const yFor = (value) => pad.top + (1 - (value - min) / span) * plotH;

      ctx.strokeStyle = "rgba(255,255,255,.07)";
      ctx.lineWidth = 1;
      ctx.fillStyle = "#69707d";
      ctx.font = "11px system-ui";
      for (let i = 0; i <= 4; i += 1) {
        const y = pad.top + (i / 4) * plotH;
        const value = max - (i / 4) * span;
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
        ctx.fillText(Math.round(value / 1000) + "k", 12, y + 4);
      }
      const labelStep = Math.max(1, Math.ceil(data.labels.length / 9));
      data.labels.forEach((label, index) => {
        if (index % labelStep !== 0 && index !== data.labels.length - 1) return;
        const x = xFor(index);
        ctx.save();
        ctx.translate(x, height - 17);
        ctx.rotate(-0.35);
        ctx.fillStyle = "#69707d";
        ctx.fillText(label, 0, 0);
        ctx.restore();
      });

      trendPoints = [];
      data.datasets.forEach((set) => {
        ctx.strokeStyle = set.color;
        ctx.lineWidth = options.thick ? 2.4 : 2;
        ctx.beginPath();
        let started = false;
        set.points.forEach((point, index) => {
          if (point.value == null) {
            started = false;
            return;
          }
          const x = xFor(index);
          const y = yFor(point.value);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else {
            ctx.lineTo(x, y);
          }
          trendPoints.push({ x, y, point, set });
        });
        ctx.stroke();
        set.points.forEach((point, index) => {
          if (point.value == null) return;
          const x = xFor(index);
          const y = yFor(point.value);
          ctx.fillStyle = set.color;
          ctx.beginPath();
          ctx.arc(x, y, 2.7, 0, Math.PI * 2);
          ctx.fill();
        });
      });
    }

    function drawBars(canvas, data) {
      const ctx = canvas.getContext("2d");
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * ratio));
      canvas.height = Math.max(1, Math.floor(rect.height * ratio));
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      const width = rect.width;
      const height = rect.height;
      ctx.clearRect(0, 0, width, height);
      const pad = { top: 18, right: 16, bottom: 34, left: 42 };
      const max = Math.max(...data.map((row) => row.images), 1);
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const step = plotW / Math.max(data.length, 1);
      ctx.fillStyle = "#69707d";
      ctx.font = "11px system-ui";
      data.forEach((row, index) => {
        const barW = Math.max(6, step * .55);
        const x = pad.left + index * step + (step - barW) / 2;
        const h = (row.images / max) * plotH;
        const y = pad.top + plotH - h;
        ctx.fillStyle = "rgba(56,189,248,.78)";
        ctx.fillRect(x, y, barW, h);
        if (index % 2 === 0 || data.length < 10) {
          ctx.fillStyle = "#69707d";
          ctx.fillText(row.month.slice(2), x - 3, height - 12);
        }
      });
    }

    function renderKpis() {
      const latest = posts[0];
      const needsReview = rows.filter((row) => row.status === "needs_review").length;
      const manual = rows.filter((row) => row.status === "manual_fixed").length;
      const selected = filteredRows();
      const latestMap = latestRows();
      const selectedLatest = carrierOrder
        .map((carrier) => latestMap.get(carrier + "|" + state.bucket))
        .filter(Boolean);
      const maxMnp = selectedLatest.length ? Math.max(...selectedLatest.map((row) => row.mnp || 0)) : 0;
      const kpis = [
        ["최신 정책글", latest ? latest.date : "-", latest ? "#" + latest.article_id : "-"],
        ["추적 후보값", meta.values.toLocaleString("ko-KR"), "확정 " + manual.toLocaleString("ko-KR") + " · 후보 " + needsReview.toLocaleString("ko-KR")],
        ["선택 데이터", selected.length.toLocaleString("ko-KR"), carrierLabels[state.carrier] + " · " + state.bucket + " · " + metricLabel()],
        ["최신 최대 번호이동", fmtMoney(maxMnp), state.bucket + " 기준"]
      ];
      document.getElementById("kpis").innerHTML = kpis.map(([label, value, note]) => (
        '<article class="kpi"><div class="kpi-label">' + escapeHtml(label) + '</div><div class="kpi-value">' + escapeHtml(value) + '</div><div class="kpi-note">' + escapeHtml(note) + '</div></article>'
      )).join("");
    }

    function renderMatrix() {
      const map = latestRows();
      const cards = carrierOrder.map((carrier) => {
        const row7 = map.get(carrier + "|7GB");
        const row11 = map.get(carrier + "|11GB");
        const color = carrierColors[carrier];
        const rowsHtml = [["7GB", row7], ["11GB", row11]].map(([bucket, row]) => {
          const prev = row ? previousFor(row) : null;
          const delta = row && prev ? row.mnp - prev.mnp : 0;
          return '<div class="amount-row"><div class="bucket">' + bucket + '</div><div><div class="amount">' + fmtAmount(row) + '</div><div class="delta ' + deltaClass(delta) + '">' + escapeHtml(deltaText(row, "mnp")) + '</div></div></div>';
        }).join("");
        return '<article class="carrier-card"><div class="carrier-name"><span class="carrier-mark" style="background:' + color + '"></span>' + carrierLabels[carrier] + '</div>' + rowsHtml + '</article>';
      }).join("");
      document.getElementById("matrix").innerHTML = cards;
      const latest = posts[0];
      document.getElementById("latestPost").textContent = latest ? latest.date + " · #" + latest.article_id : "-";
    }

    function computeChanges() {
      return rows.map((row) => {
        const prev = previousFor(row);
        if (!prev) return null;
        return {
          ...row,
          prev,
          deltaNew: row.new - prev.new,
          deltaMnp: row.mnp - prev.mnp
        };
      }).filter(Boolean).filter((row) => {
        if (state.carrier !== "all" && row.carrier !== state.carrier) return false;
        if (row.bucket !== state.bucket) return false;
        if (state.status !== "all" && row.status !== state.status) return false;
        return row.deltaNew !== 0 || row.deltaMnp !== 0;
      }).sort((a, b) => Math.max(Math.abs(b.deltaNew), Math.abs(b.deltaMnp)) - Math.max(Math.abs(a.deltaNew), Math.abs(a.deltaMnp)));
    }

    function renderChanges() {
      const changes = computeChanges().slice(0, 8);
      const html = changes.length ? changes.map((row) => {
        const primaryDelta = state.metric === "mnp" ? row.deltaMnp : row.deltaNew;
        const sign = primaryDelta > 0 ? "+" : "";
        return '<article class="change-item"><div class="item-top"><span>' + row.date + ' · #' + row.article_id + '</span><span class="pill ' + statusClass(row.status) + '">' + statusLabel(row.status) + '</span></div><div class="item-main"><strong style="color:' + carrierColors[row.carrier] + '">' + carrierLabels[row.carrier] + '</strong> ' + row.bucket + ' ' + metricLabel() + ' ' + fmtMoney(row.prev[state.metric]) + ' → ' + fmtMoney(row[state.metric]) + ' <span class="' + deltaClass(primaryDelta) + '">(' + sign + fmtMoney(primaryDelta) + ')</span></div></article>';
      }).join("") : '<article class="change-item"><div class="item-main">선택한 조건에서는 변동 항목이 없습니다.</div></article>';
      document.getElementById("changesList").innerHTML = html;
    }

    function renderPosts() {
      document.getElementById("postsList").innerHTML = posts.slice(0, 8).map((post) => (
        '<article class="post-item"><div class="item-top"><span>' + post.write_date + '</span><span class="pill">#' + post.article_id + '</span></div><div class="item-main">' + escapeHtml(post.subject) + '</div></article>'
      )).join("");
    }

    function renderDetails() {
      const data = filteredRows().slice().sort((a, b) => b.write_date.localeCompare(a.write_date)).slice(0, 240);
      document.getElementById("detailRows").innerHTML = data.map((row) => (
        '<tr><td>' + row.write_date + '</td><td>#' + row.article_id + '</td><td>' + carrierLabels[row.carrier] + '</td><td>' + row.bucket + '</td><td class="strong">' + fmtMoney(row.new) + '</td><td class="strong">' + fmtMoney(row.mnp) + '</td><td><span class="pill ' + statusClass(row.status) + '">' + statusLabel(row.status) + '</span></td><td class="subject-cell">' + escapeHtml(row.subject) + '</td></tr>'
      )).join("");
    }

    function renderTrend() {
      const data = buildTrendData();
      const canvas = document.getElementById("trendChart");
      drawLineChart(canvas, data, { thick: state.carrier !== "all" });
      document.getElementById("chartTitle").textContent = state.bucket + " " + metricLabel() + " 정책금 추이";
      document.getElementById("chartCaption").textContent = state.cadence === "monthly" ? "월별 평균 기준" : "게시글 차수 기준";
      document.getElementById("activeState").textContent = carrierLabels[state.carrier] + " · " + state.bucket + " · " + metricLabel();
      document.getElementById("legend").innerHTML = data.datasets.map((set) => (
        '<span class="legend-item" style="color:' + set.color + '"><span class="swatch"></span>' + set.label + '</span>'
      )).join("");
    }

    function bindTooltip() {
      const canvas = document.getElementById("trendChart");
      const tip = document.getElementById("chartTooltip");
      canvas.onmousemove = (event) => {
        if (!trendPoints.length) return;
        const rect = canvas.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        let nearest = null;
        let distance = Infinity;
        trendPoints.forEach((item) => {
          const d = Math.hypot(item.x - x, item.y - y);
          if (d < distance) {
            distance = d;
            nearest = item;
          }
        });
        if (!nearest || distance > 26) {
          tip.style.display = "none";
          return;
        }
        const raw = nearest.point.raw;
        tip.innerHTML = '<strong style="color:' + nearest.set.color + '">' + nearest.set.label + '</strong><br>' + escapeHtml(nearest.point.label) + '<br>' + metricLabel() + ': ' + fmtMoney(nearest.point.value) + (raw && raw.status ? '<br>상태: ' + statusLabel(raw.status) : '');
        tip.style.display = "block";
        tip.style.left = Math.min(rect.width - tip.offsetWidth - 12, Math.max(12, x + 14)) + "px";
        tip.style.top = Math.min(rect.height - tip.offsetHeight - 12, Math.max(12, y + 14)) + "px";
      };
      canvas.onmouseleave = () => { tip.style.display = "none"; };
    }

    function renderCoverage() {
      drawBars(document.getElementById("coverageChart"), coverageRows);
    }

    function render() {
      buildControls();
      renderKpis();
      renderTrend();
      renderChanges();
      renderMatrix();
      renderPosts();
      renderDetails();
      renderCoverage();
    }

    document.getElementById("generatedAt").textContent = "생성 " + meta.generated_at;
    bindTooltip();
    render();
    window.addEventListener("resize", () => {
      renderTrend();
      renderCoverage();
    });
  </script>
</body>
</html>
"""
    replacements = {
        "__ROWS_JSON__": json.dumps(payload, ensure_ascii=False),
        "__MONTHLY_JSON__": json.dumps(monthly_payload, ensure_ascii=False),
        "__POSTS_JSON__": json.dumps(posts_payload, ensure_ascii=False),
        "__IMAGES_JSON__": json.dumps(images_payload, ensure_ascii=False),
        "__COVERAGE_JSON__": json.dumps(coverage_payload, ensure_ascii=False),
        "__META_JSON__": json.dumps(meta_payload, ensure_ascii=False),
    }
    for placeholder, value in replacements.items():
        dashboard_html = dashboard_html.replace(placeholder, value)
    DASHBOARD_PATH.write_text(dashboard_html, encoding="utf-8")
    print(DASHBOARD_PATH)


def export_csv(_args):
    init_db()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    daily_path = EXPORT_DIR / "policy_values_daily.csv"
    monthly_path = EXPORT_DIR / "policy_values_monthly.csv"
    summary_path = EXPORT_DIR / "policy_summary_by_post.csv"
    changes_path = EXPORT_DIR / "policy_changes.csv"
    review_priority_path = EXPORT_DIR / "review_priority.csv"
    coverage_path = EXPORT_DIR / "extraction_coverage.csv"
    review_values_path = EXPORT_DIR / "review_values.csv"
    manual_template_path = EXPORT_DIR / "manual_values_template.csv"
    missing_manual_template_path = EXPORT_DIR / "missing_image_manual_template.csv"
    status_report_path = EXPORT_DIR / "status_report.md"
    with connect() as db:
        rows = db.execute(
            """
            select p.write_date, p.article_id, p.subject, v.carrier, v.plan_bucket,
                   coalesce(m.plan_name, v.plan_name) plan_name,
                   coalesce(m.new_amount, v.new_amount) new_amount,
                   coalesce(m.mnp_amount, v.mnp_amount) mnp_amount,
                   case when m.id is not null then 'manual_fixed' else v.status end status,
                   coalesce(m.memo, v.conditions) source_text
            from policy_values v
            join posts p on p.article_id = v.article_id
            left join manual_values m
              on m.article_id = v.article_id
             and m.carrier = v.carrier
             and m.plan_bucket = v.plan_bucket
            order by p.write_ts, v.carrier, v.plan_bucket
            """
        ).fetchall()
        monthly = db.execute(
            """
            with effective as (
              select p.write_date, v.carrier, v.plan_bucket,
                     coalesce(m.new_amount, v.new_amount) new_amount,
                     coalesce(m.mnp_amount, v.mnp_amount) mnp_amount
              from policy_values v
              join posts p on p.article_id = v.article_id
              left join manual_values m
                on m.article_id = v.article_id
               and m.carrier = v.carrier
               and m.plan_bucket = v.plan_bucket
            )
            select substr(write_date, 1, 7) month, carrier, plan_bucket,
                   round(avg(new_amount), 1) avg_new,
                   round(avg(mnp_amount), 1) avg_mnp,
                   max(new_amount) max_new,
                   max(mnp_amount) max_mnp,
                   min(new_amount) min_new,
                   min(mnp_amount) min_mnp,
                   count(*) rows
            from effective
            group by month, carrier, plan_bucket
            order by month, carrier, plan_bucket
            """
        ).fetchall()
        coverage = db.execute(
            """
            select p.write_date, p.article_id, p.subject, i.carrier,
                   i.extract_status,
                   case when exists (select 1 from policy_values v where v.source_image_id=i.id)
                          or exists (select 1 from manual_values m where m.article_id=i.article_id and m.carrier=i.carrier)
                        then 1 else 0 end has_value,
                   i.local_path
            from images i
            join posts p on p.article_id = i.article_id
            order by p.write_ts, i.carrier
            """
        ).fetchall()
        review_values = db.execute(
            """
            select p.write_date, p.article_id, p.subject, v.carrier, v.plan_bucket,
                   v.plan_name, v.new_amount, v.mnp_amount, v.status, v.conditions, i.local_path
            from policy_values v
            join posts p on p.article_id = v.article_id
            left join images i on i.id = v.source_image_id
            where v.status = 'needs_review'
            order by p.write_ts, v.carrier, v.plan_bucket
            """
        ).fetchall()
        stats = {
            "posts": db.execute("select count(*) from posts").fetchone()[0],
            "images": db.execute("select count(*) from images").fetchone()[0],
            "policy_values": db.execute("select count(*) from policy_values").fetchone()[0],
            "manual_values": db.execute("select count(*) from manual_values").fetchone()[0],
            "missing_images": db.execute(
                """
                select count(*)
                from images i
                where not exists (select 1 from policy_values v where v.source_image_id=i.id)
                  and not exists (select 1 from manual_values m where m.article_id=i.article_id and m.carrier=i.carrier)
                """
            ).fetchone()[0],
            "post_min": db.execute("select min(write_date) from posts").fetchone()[0],
            "post_max": db.execute("select max(write_date) from posts").fetchone()[0],
            "value_min": db.execute(
                "select min(p.write_date) from policy_values v join posts p on p.article_id=v.article_id"
            ).fetchone()[0],
            "value_max": db.execute(
                "select max(p.write_date) from policy_values v join posts p on p.article_id=v.article_id"
            ).fetchone()[0],
        }
        carrier_stats = db.execute(
            """
            select carrier,
                   count(*) images,
                   sum(case when exists (select 1 from policy_values v where v.source_image_id=images.id)
                              or exists (select 1 from manual_values m where m.article_id=images.article_id and m.carrier=images.carrier)
                            then 1 else 0 end) with_value,
                   sum(case when not exists (select 1 from policy_values v where v.source_image_id=images.id)
                              and not exists (select 1 from manual_values m where m.article_id=images.article_id and m.carrier=images.carrier)
                            then 1 else 0 end) missing
            from images
            group by carrier
            order by carrier
            """
        ).fetchall()
    with daily_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["write_date", "article_id", "subject", "carrier", "plan_bucket", "plan_name", "new_amount", "mnp_amount", "status", "source_text"])
        writer.writerows(rows)
    with monthly_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["month", "carrier", "plan_bucket", "avg_new", "avg_mnp", "max_new", "max_mnp", "min_new", "min_mnp", "rows"])
        writer.writerows(monthly)
    summary = {}
    for write_date, article_id, subject, carrier, extract_status, has_value, local_path in coverage:
        key = (article_id, carrier)
        summary.setdefault(
            key,
            {
                "write_date": write_date,
                "article_id": article_id,
                "subject": subject,
                "carrier": carrier,
                "7GB_new": "",
                "7GB_mnp": "",
                "11GB_new": "",
                "11GB_mnp": "",
                "status": "needs_image_review" if not has_value else "needs_review",
                "local_path": local_path,
            },
        )
    for write_date, article_id, subject, carrier, plan_bucket, _plan_name, new_amount, mnp_amount, status, _source_text in rows:
        key = (article_id, carrier)
        item = summary.setdefault(
            key,
            {
                "write_date": write_date,
                "article_id": article_id,
                "subject": subject,
                "carrier": carrier,
                "7GB_new": "",
                "7GB_mnp": "",
                "11GB_new": "",
                "11GB_mnp": "",
                "status": status,
                "local_path": "",
            },
        )
        if plan_bucket in ("7GB", "11GB"):
            item[f"{plan_bucket}_new"] = new_amount
            item[f"{plan_bucket}_mnp"] = mnp_amount
        item["status"] = "manual_fixed" if status == "manual_fixed" else item["status"]
    with summary_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["write_date", "article_id", "subject", "carrier", "7GB_new", "7GB_mnp", "11GB_new", "11GB_mnp", "status", "local_path"])
        for item in sorted(summary.values(), key=lambda x: (x["write_date"], x["carrier"] or "")):
            writer.writerow([
                item["write_date"],
                item["article_id"],
                item["subject"],
                item["carrier"],
                item["7GB_new"],
                item["7GB_mnp"],
                item["11GB_new"],
                item["11GB_mnp"],
                item["status"],
                item["local_path"],
            ])
    previous = {}
    change_rows = []
    for write_date, article_id, subject, carrier, plan_bucket, plan_name, new_amount, mnp_amount, status, source_text in rows:
        key = (carrier, plan_bucket)
        prev = previous.get(key)
        if prev:
            prev_write_date, prev_article_id, prev_subject, prev_new, prev_mnp = prev
            delta_new = new_amount - prev_new if new_amount is not None and prev_new is not None else ""
            delta_mnp = mnp_amount - prev_mnp if mnp_amount is not None and prev_mnp is not None else ""
            changed = int(delta_new != 0 or delta_mnp != 0)
        else:
            prev_write_date, prev_article_id, prev_subject, prev_new, prev_mnp = "", "", "", "", ""
            delta_new, delta_mnp, changed = "", "", 0
        change_rows.append([
            write_date,
            article_id,
            subject,
            carrier,
            plan_bucket,
            plan_name,
            new_amount,
            mnp_amount,
            prev_write_date,
            prev_article_id,
            prev_subject,
            prev_new,
            prev_mnp,
            delta_new,
            delta_mnp,
            changed,
            status,
            source_text,
        ])
        previous[key] = (write_date, article_id, subject, new_amount, mnp_amount)
    with changes_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "write_date",
            "article_id",
            "subject",
            "carrier",
            "plan_bucket",
            "plan_name",
            "new_amount",
            "mnp_amount",
            "prev_write_date",
            "prev_article_id",
            "prev_subject",
            "prev_new_amount",
            "prev_mnp_amount",
            "delta_new",
            "delta_mnp",
            "changed",
            "status",
            "source_text",
        ])
        writer.writerows(change_rows)
    priority_rows = []
    for write_date, article_id, subject, carrier, plan_bucket, plan_name, new_amount, mnp_amount, status, source_text in rows:
        if status == "manual_fixed":
            continue
        priority = review_priority(new_amount, mnp_amount, source_text)
        if priority in ("high", "medium"):
            priority_rows.append([
                priority,
                write_date,
                article_id,
                subject,
                carrier,
                plan_bucket,
                plan_name,
                new_amount,
                mnp_amount,
                status,
                source_text,
            ])
    with review_priority_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["priority", "write_date", "article_id", "subject", "carrier", "plan_bucket", "plan_name", "new_amount", "mnp_amount", "status", "source_text"])
        writer.writerows(priority_rows)
    with coverage_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["write_date", "article_id", "subject", "carrier", "extract_status", "has_value", "local_path"])
        writer.writerows(coverage)
    with review_values_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["write_date", "article_id", "subject", "carrier", "plan_bucket", "plan_name", "new_amount", "mnp_amount", "status", "source_text", "local_path"])
        writer.writerows(review_values)
    with manual_template_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["article_id", "carrier", "plan_bucket", "plan_name", "new_amount", "mnp_amount", "memo", "write_date", "subject", "source_text", "local_path"])
        for write_date, article_id, subject, carrier, plan_bucket, plan_name, new_amount, mnp_amount, _status, source_text, local_path in review_values:
            writer.writerow([article_id, carrier, plan_bucket, plan_name, new_amount, mnp_amount, "", write_date, subject, source_text, local_path])
    with missing_manual_template_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["write_date", "article_id", "subject", "carrier", "7GB_new", "7GB_mnp", "11GB_new", "11GB_mnp", "memo", "local_path"])
        for write_date, article_id, subject, carrier, _extract_status, has_value, local_path in coverage:
            if not has_value:
                writer.writerow([write_date, article_id, subject, carrier, "", "", "", "", "", local_path])
    carrier_lines = "\n".join(
        f"- {carrier}: 이미지 {images}장, 후보값 있음 {with_value}장, 원본 검수 필요 {missing}장"
        for carrier, images, with_value, missing in carrier_stats
    )
    status_report_path.write_text(
        f"""# 알뜰폰 정책 트래커 상태 보고

생성 기준: {datetime.now(KST).date().isoformat()} KST

## 수집 범위

- 네이버 카페: `gray9de5o`
- 게시판: 후불유심정책
- 기간: {stats["post_min"][:10]} ~ {stats["post_max"][:10]}
- 정책글: {stats["posts"]:,}개
- 대상 통신사: 유모비, 헬로, KTM, 스카이, SK7
- 제외: 이야기 모바일

## 적재 현황

- 원본 정책 이미지: {stats["images"]:,}장
- 자동 후보 정책값: {stats["policy_values"]:,}개
- 수동 확정값: {stats["manual_values"]:,}개
- 자동 후보 추출 범위: {stats["value_min"][:10]} ~ {stats["value_max"][:10]}
- 원본 대조가 더 필요한 이미지: {stats["missing_images"]:,}장

## 통신사별 검수 현황

{carrier_lines}

## 산출물

- 대시보드: `dashboard/index.html`
- 게시글별 요약: `data/exports/policy_summary_by_post.csv`
- 이전 차수 대비 변동: `data/exports/policy_changes.csv`
- 우선 검수 후보: `data/exports/review_priority.csv`
- 일별 후보값: `data/exports/policy_values_daily.csv`
- 월별 요약: `data/exports/policy_values_monthly.csv`
- 커버리지: `data/exports/extraction_coverage.csv`
- 자동 후보값 원본 대조: `dashboard/candidate_review.html`
- 우선 검수 화면: `dashboard/priority_review.html`
- 자동 추출 실패 이미지 검토: `dashboard/review.html`
- 수동 확정 입력 양식: `data/exports/manual_values_template.csv`
- 미추출 이미지 수동 입력 양식: `data/exports/missing_image_manual_template.csv`

## 남은 검증 작업

- 자동 후보값은 `needs_review` 상태이며, 원본 이미지 대조 후 확정해야 함.
- `policy_summary_by_post.csv`에서 빈 칸은 해당 통신사 이미지에서 7GB/11GB 후보값을 자동 확정하지 못한 항목임.
- 후보값 보정은 `import-manual`, 미추출 이미지 확정값은 `import-summary-manual` 명령으로 반영 가능.
""",
        encoding="utf-8",
    )
    print(daily_path)
    print(monthly_path)
    print(summary_path)
    print(changes_path)
    print(review_priority_path)
    print(coverage_path)
    print(review_values_path)
    print(manual_template_path)
    print(missing_manual_template_path)
    print(status_report_path)


def generate_review(_args):
    init_db()
    with connect() as db:
        rows = db.execute(
            """
            select p.write_date, p.article_id, p.subject, i.carrier, i.local_path
            from images i
            join posts p on p.article_id = i.article_id
            where not exists (select 1 from policy_values v where v.source_image_id = i.id)
              and not exists (select 1 from manual_values m where m.article_id = i.article_id and m.carrier = i.carrier)
            order by p.write_ts, i.carrier
            """
        ).fetchall()
    items = []
    for write_date, article_id, subject, carrier, local_path in rows:
        rel = os.path.relpath(local_path, REVIEW_PATH.parent)
        items.append(
            f"""
            <article>
              <h3>{html.escape(write_date[:10])} · {article_id} · {html.escape(carrier or '')}</h3>
              <p>{html.escape(subject)}</p>
              <a href="{html.escape(rel)}"><img src="{html.escape(rel)}" loading="lazy"></a>
            </article>
            """
        )
    REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_PATH.write_text(
        f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>정책 이미지 검토 대상</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #17202a; }}
    article {{ border-top: 1px solid #d6dbe1; padding: 16px 0; }}
    img {{ max-width: 960px; width: 100%; border: 1px solid #d6dbe1; }}
    h1 {{ font-size: 24px; }}
    h3 {{ margin-bottom: 4px; }}
  </style>
</head>
<body>
  <h1>자동 추출 검토 대상</h1>
  <p>총 {len(rows)}장입니다. 원본 이미지는 모두 저장되어 있으며, 자동 후보값이 잡히지 않은 항목입니다.</p>
  {''.join(items)}
</body>
</html>
""",
        encoding="utf-8",
    )
    print(REVIEW_PATH)


def generate_candidate_review(_args):
    init_db()
    with connect() as db:
        rows = db.execute(
            """
            select p.write_date, p.article_id, p.subject, v.carrier, v.plan_bucket,
                   v.plan_name, v.new_amount, v.mnp_amount, v.conditions, i.local_path
            from policy_values v
            join posts p on p.article_id = v.article_id
            left join images i on i.id = v.source_image_id
            where v.status = 'needs_review'
            order by p.write_ts desc, v.carrier, v.plan_bucket
            """
        ).fetchall()
    items = []
    for write_date, article_id, subject, carrier, bucket, plan_name, new_amount, mnp_amount, conditions, local_path in rows:
        rel = os.path.relpath(local_path, CANDIDATE_REVIEW_PATH.parent) if local_path else ""
        image_html = f'<a href="{html.escape(rel)}"><img src="{html.escape(rel)}" loading="lazy"></a>' if rel else ""
        items.append(
            f"""
            <article>
              <div class="meta">{html.escape(write_date[:10])} · {article_id} · {html.escape(subject)}</div>
              <h3>{html.escape(carrier or '')} · {html.escape(bucket or '')}</h3>
              <table>
                <tbody>
                  <tr><th>후보 요금제</th><td>{html.escape(plan_name or '')}</td></tr>
                  <tr><th>후보 금액</th><td>{money(new_amount)} / {money(mnp_amount)}</td></tr>
                  <tr><th>OCR 근거</th><td><pre>{html.escape(conditions or '')}</pre></td></tr>
                </tbody>
              </table>
              {image_html}
            </article>
            """
        )
    CANDIDATE_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_REVIEW_PATH.write_text(
        f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>정책 후보값 검토</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #17202a; }}
    article {{ border-top: 1px solid #d6dbe1; padding: 18px 0; }}
    img {{ max-width: 980px; width: 100%; border: 1px solid #d6dbe1; margin-top: 12px; }}
    h1 {{ font-size: 24px; }}
    h3 {{ margin: 6px 0 10px; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 980px; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e5e8ec; text-align: left; padding: 8px; vertical-align: top; }}
    th {{ width: 120px; }}
    pre {{ white-space: pre-wrap; margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .meta {{ color: #52606d; font-size: 13px; }}
  </style>
</head>
<body>
  <h1>자동 후보값 검토</h1>
  <p>총 {len(rows)}개입니다. 금액 표기는 <code>신규 / 번호이동</code>이며, 확정 전 후보값입니다.</p>
  {''.join(items)}
</body>
</html>
""",
        encoding="utf-8",
    )
    print(CANDIDATE_REVIEW_PATH)


def generate_priority_review(_args):
    init_db()
    with connect() as db:
        rows = db.execute(
            """
            select p.write_date, p.article_id, p.subject, v.carrier, v.plan_bucket,
                   v.plan_name, v.new_amount, v.mnp_amount, v.status, v.conditions, i.local_path
            from policy_values v
            join posts p on p.article_id = v.article_id
            left join images i on i.id = v.source_image_id
            where v.status = 'needs_review'
            order by p.write_ts, v.carrier, v.plan_bucket
            """
        ).fetchall()
    priority_rows = []
    for row in rows:
        write_date, article_id, subject, carrier, bucket, plan_name, new_amount, mnp_amount, status, conditions, local_path = row
        priority = review_priority(new_amount, mnp_amount, conditions)
        if priority in ("high", "medium"):
            priority_rows.append((priority,) + row)
    priority_rows.sort(key=lambda r: (0 if r[0] == "high" else 1, r[1], r[4] or "", r[5] or ""))

    items = []
    for priority, write_date, article_id, subject, carrier, bucket, plan_name, new_amount, mnp_amount, status, conditions, local_path in priority_rows:
        rel = os.path.relpath(local_path, PRIORITY_REVIEW_PATH.parent) if local_path else ""
        image_html = f'<a href="{html.escape(rel)}"><img src="{html.escape(rel)}" loading="lazy"></a>' if rel else ""
        manual_line = f"{article_id},{carrier},{bucket},manual,{new_amount or ''},{mnp_amount or ''},"
        items.append(
            f"""
            <article class="{html.escape(priority)}">
              <div class="meta">{html.escape(priority.upper())} · {html.escape(write_date[:10])} · {article_id} · {html.escape(subject)}</div>
              <h3>{html.escape(carrier or '')} · {html.escape(bucket or '')} · {money(new_amount)} / {money(mnp_amount)}</h3>
              <table>
                <tbody>
                  <tr><th>후보 요금제</th><td>{html.escape(plan_name or '')}</td></tr>
                  <tr><th>CSV 입력줄</th><td><code>{html.escape(manual_line)}</code></td></tr>
                  <tr><th>OCR 근거</th><td><pre>{html.escape(conditions or '')}</pre></td></tr>
                </tbody>
              </table>
              {image_html}
            </article>
            """
        )
    PRIORITY_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRIORITY_REVIEW_PATH.write_text(
        f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>우선 검수 정책 후보</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #17202a; }}
    article {{ border-top: 1px solid #d6dbe1; padding: 18px 0; }}
    article.high {{ background: #fff8f3; }}
    article.medium {{ background: #f7fbff; }}
    img {{ max-width: 1040px; width: 100%; border: 1px solid #d6dbe1; margin-top: 12px; }}
    h1 {{ font-size: 24px; }}
    h3 {{ margin: 6px 0 10px; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 1040px; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e5e8ec; text-align: left; padding: 8px; vertical-align: top; }}
    th {{ width: 120px; }}
    pre {{ white-space: pre-wrap; margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    code {{ background: #f3f5f7; padding: 2px 4px; }}
    .meta {{ color: #52606d; font-size: 13px; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>우선 검수 정책 후보</h1>
  <p>총 {len(priority_rows)}개입니다. 금액 표기는 <code>신규 / 번호이동</code>이며, 후보값과 원본 이미지를 대조해 확정 CSV에 반영합니다.</p>
  {''.join(items)}
</body>
</html>
""",
        encoding="utf-8",
    )
    print(PRIORITY_REVIEW_PATH)


def daily_update(args):
    since = args.since
    class CollectArgs:
        pass
    collect_args = CollectArgs()
    collect_args.since = since
    collect_args.per_page = 100
    collect_args.sleep = 0.15
    collect_args.download = True
    collect_args.force_download = False
    collect(collect_args)

    class ExtractArgs:
        pass
    extract_args = ExtractArgs()
    extract_args.limit = args.extract_limit
    extract_args.missing_only = True
    extract_values(extract_args)
    generate_dashboard(args)
    generate_review(args)
    generate_candidate_review(args)
    generate_priority_review(args)
    export_csv(args)


def import_manual(args):
    init_db()
    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"manual CSV not found: {path}")
    required = {"article_id", "carrier", "plan_bucket", "new_amount", "mnp_amount"}
    now = datetime.now(KST).isoformat()
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as f, connect() as db:
        reader = csv.DictReader(f)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"manual CSV missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            if not row.get("article_id") or not row.get("carrier") or not row.get("plan_bucket"):
                continue
            count += upsert_manual_value(db, row, now)
    print(f"manual rows imported={count}")


def import_summary_manual(args):
    init_db()
    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"summary manual CSV not found: {path}")
    required = {"article_id", "carrier", "7GB_new", "7GB_mnp", "11GB_new", "11GB_mnp"}
    now = datetime.now(KST).isoformat()
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as f, connect() as db:
        reader = csv.DictReader(f)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"summary manual CSV missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            if not row.get("article_id") or not row.get("carrier"):
                continue
            for bucket in ("7GB", "11GB"):
                new_amount = row.get(f"{bucket}_new")
                mnp_amount = row.get(f"{bucket}_mnp")
                if parse_int(new_amount) is None and parse_int(mnp_amount) is None:
                    continue
                manual_row = {
                    "article_id": row["article_id"],
                    "carrier": row["carrier"],
                    "plan_bucket": bucket,
                    "plan_name": "manual",
                    "new_amount": new_amount,
                    "mnp_amount": mnp_amount,
                    "memo": row.get("memo") or "",
                }
                count += upsert_manual_value(db, manual_row, now)
    print(f"summary manual rows imported={count}")


def status(_args):
    init_db()
    with connect() as db:
        for name, query in [
            ("posts", "select count(*) from posts"),
            ("images", "select count(*) from images"),
            ("ocr_done", "select count(*) from images where ocr_status='done'"),
            ("policy_values", "select count(*) from policy_values"),
        ]:
            print(name, db.execute(query).fetchone()[0])
        latest = db.execute("select article_id, subject, write_date from posts order by write_ts desc limit 5").fetchall()
        for row in latest:
            print(row)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(func=lambda _args: init_db())
    p = sub.add_parser("collect")
    p.add_argument("--since", default="2025-01-01")
    p.add_argument("--per-page", type=int, default=100)
    p.add_argument("--sleep", type=float, default=0.15)
    p.add_argument("--download", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--force-download", action="store_true")
    p.set_defaults(func=collect)
    p = sub.add_parser("ocr")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=run_ocr)
    p = sub.add_parser("extract")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--missing-only", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--oldest-first", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=extract_values)
    sub.add_parser("dashboard").set_defaults(func=generate_dashboard)
    sub.add_parser("export-csv").set_defaults(func=export_csv)
    sub.add_parser("review").set_defaults(func=generate_review)
    sub.add_parser("candidate-review").set_defaults(func=generate_candidate_review)
    sub.add_parser("priority-review").set_defaults(func=generate_priority_review)
    p = sub.add_parser("daily-update")
    p.add_argument("--since", default="2025-01-01")
    p.add_argument("--extract-limit", type=int, default=20)
    p.set_defaults(func=daily_update)
    p = sub.add_parser("import-manual")
    p.add_argument("path")
    p.set_defaults(func=import_manual)
    p = sub.add_parser("import-summary-manual")
    p.add_argument("path")
    p.set_defaults(func=import_summary_manual)
    sub.add_parser("status").set_defaults(func=status)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
