from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from templates import build_email_html, build_pages_index_html


RSS_URL = "https://feeds.transistor.fm/cheeky-pint-with-john-collison"
MODEL_NAME = "gemini-3.1-flash-lite-preview"
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

# 翻译分段阈值/块大小（按 input 字符数控制，避免超过限制）
DIRECT_TRANSLATE_THRESHOLD = 6000
MAX_CHUNK_CHARS = 5500

DOCS_DIR = "docs"
DATA_JSON_PATH = os.path.join(DOCS_DIR, "data.json")
INDEX_HTML_PATH = os.path.join(DOCS_DIR, "index.html")


@dataclass
class Episode:
    title: str
    link: str
    pub_date_bj: str          # yyyy-mm-dd
    pub_datetime_bj: datetime
    summary: str
    transcript_url: str
    transcript_html_en: str   # <p>...</p> 拼接
    transcript_text_en: str   # 纯文本（备用）


def must_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Missing required env: {name}")
    return v


def http_get(url: str, timeout: int = 30) -> requests.Response:
    headers = {
        "User-Agent": "cheeky-pint-bot/1.0 (+https://github.com/)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_latest_episode() -> Episode:
    print("▶ Fetching RSS feed...", flush=True)
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        raise RuntimeError("RSS feed has no entries")

    entry = feed.entries[0]
    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    if not title or not link:
        raise RuntimeError("Missing title/link in RSS latest entry")

    # 日期解析
    pub_raw = entry.get("published") or entry.get("updated") or ""
    if pub_raw:
        pub_dt = dateparser.parse(pub_raw)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    else:
        pub_dt = datetime.now(timezone.utc)

    bj_tz = ZoneInfo("Asia/Shanghai")
    pub_bj = pub_dt.astimezone(bj_tz)
    pub_date_bj = pub_bj.strftime("%Y-%m-%d")

    # 摘要
    summary_html = entry.get("summary") or entry.get("description") or ""
    summary = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)

    transcript_url = link.rstrip("/") + "/transcript"
    print(f"▶ Fetching transcript from {transcript_url}...", flush=True)
    transcript_html_en, transcript_text_en = fetch_transcript(transcript_url)

    print(f"✔ Episode: \"{title}\" ({pub_date_bj})", flush=True)
    print(f"  Transcript length: {len(transcript_html_en)} chars", flush=True)

    return Episode(
        title=title,
        link=link,
        pub_date_bj=pub_date_bj,
        pub_datetime_bj=pub_bj,
        summary=summary,
        transcript_url=transcript_url,
        transcript_html_en=transcript_html_en,
        transcript_text_en=transcript_text_en,
    )


def fetch_transcript(transcript_url: str) -> tuple[str, str]:
    resp = http_get(transcript_url, timeout=40)
    soup = BeautifulSoup(resp.text, "html.parser")

    section = (
        soup.select_one("section.episode-description.episode-transcript")
        or soup.select_one("section.episode-transcript")
    )
    if not section:
        raise RuntimeError("Transcript section not found")

    ps = section.find_all("p")
    if not ps:
        raise RuntimeError("No <p> found in transcript section")

    ts_pat = re.compile(r"^\[\d{2}:\d{2}:\d{2}")
    start_idx = 0
    for i, p in enumerate(ps):
        txt = p.get_text("\n", strip=True)
        if ts_pat.search(txt):
            start_idx = i
            break

    ps = ps[start_idx:]

    html_parts = []
    text_parts = []

    for p in ps:
        p_copy = BeautifulSoup(str(p), "html.parser").p
        if p_copy is None:
            continue

        existing_style = (p_copy.get("style") or "").strip()
        style = "margin:0 0 10px 0;color:#111827;font-size:14px !important;line-height:1.6 !important;"
        if existing_style:
            style = existing_style.rstrip(";") + ";" + style
        p_copy["style"] = style

        html_parts.append(str(p_copy))
        text_parts.append(p.get_text("\n", strip=True))

    transcript_html = "\n".join(html_parts).strip()
    transcript_text = "\n\n".join(text_parts).strip()
    return transcript_html, transcript_text


def split_html_by_paragraphs(transcript_html_en: str, max_chars: int) -> list[str]:
    soup = BeautifulSoup(f"<div>{transcript_html_en}</div>", "html.parser")
    ps = soup.find_all("p")
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    for p in ps:
        s = str(p)
        if not s.strip():
            continue

        if not buf:
            buf = [s]
            buf_len = len(s)
        elif buf_len + 1 + len(s) <= max_chars:
            buf.append(s)
            buf_len += 1 + len(s)
        else:
            chunks.append("\n".join(buf))
            buf = [s]
            buf_len = len(s)

    if buf:
        chunks.append("\n".join(buf))
    return chunks


def gemini_translate_html(api_key: str, html: str, *, max_retries: int = 10) -> str:
    """
    - 401/403: API Key 无效，立即终止
    - 429: respect Retry-After
    - other non-retriable 4xx: abort immediately
    - retriable: exponential backoff + jitter
    """
    prompt = (
        "Translate the following HTML from English to Simplified Chinese.\n"
        "Requirements:\n"
        "1) Keep all HTML tags unchanged and in the same order (do not add/remove tags).\n"
        "2) Do NOT translate timestamps like [00:00:25.06].\n"
        "3) Do NOT translate speaker names.\n"
        "4) Only translate the spoken sentences.\n"
        "5) Return ONLY the translated HTML.\n\n"
        "HTML:\n"
        f"{html}"
    )

    url = f"{GEMINI_ENDPOINT}?key={api_key}"

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                url,
                timeout=60,
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2},
                },
            )

            # ---- API Key 无效：立即终止，不重试 ----
            if resp.status_code in (401, 403):
                raise SystemExit(
                    f"✖ GEMINI_API_KEY invalid or unauthorized (HTTP {resp.status_code}): "
                    f"{resp.text[:200]}"
                )

            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                wait_s = int(ra) if ra and ra.isdigit() else 60
                wait_s = wait_s + random.uniform(0, 1.0)
                print(f"  ⏳ Rate-limited by Gemini, waiting {wait_s:.0f}s (attempt {attempt})...", flush=True)
                time.sleep(wait_s)
                continue

            if 400 <= resp.status_code < 500:
                raise RuntimeError(f"Gemini non-retriable error: {resp.status_code} {resp.text[:300]}")

            if resp.status_code >= 500:
                raise RuntimeError(f"Gemini server error: {resp.status_code}")

            data = resp.json()
            text = (
                data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
            )
            text = (text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned empty text")
            return text

        except SystemExit:
            raise  # 不捕获 SystemExit，让它直接终止进程
        except Exception as e:
            if attempt >= max_retries:
                raise

            backoff = min(2 ** (attempt - 1), 64)
            sleep_s = backoff + random.uniform(0, 1.0)
            print(f"  ⚠ Gemini error (attempt {attempt}/{max_retries}): {e}, retrying in {sleep_s:.1f}s...", flush=True)
            time.sleep(sleep_s)

    raise RuntimeError("Unreachable")


def translate_episode(api_key: str, ep: Episode) -> tuple[str | None, str | None, str | None]:
    """
    返回 (title_zh, summary_zh, transcript_html_zh)
    任一失败 -> 返回全 None（确保邮件只发英文）
    注意：SystemExit（API Key 无效）不会被捕获，会直接终止。
    """
    try:
        print("▶ Translating title...", flush=True)
        title_zh = gemini_translate_html(api_key, f"<p>{escape_min(ep.title)}</p>")
        title_zh = BeautifulSoup(title_zh, "html.parser").get_text(" ", strip=True) or None
        print(f"  ✔ Title (zh): {title_zh}", flush=True)

        print("▶ Translating summary...", flush=True)
        summary_zh = gemini_translate_html(api_key, f"<p>{escape_min(ep.summary)}</p>")
        summary_zh = BeautifulSoup(summary_zh, "html.parser").get_text(" ", strip=True) or None
        print(f"  ✔ Summary (zh): {summary_zh[:60]}...", flush=True)

        # transcript：按长度分段
        if len(ep.transcript_html_en) < DIRECT_TRANSLATE_THRESHOLD:
            print("▶ Translating transcript (single chunk)...", flush=True)
            transcript_zh = gemini_translate_html(api_key, ep.transcript_html_en)
            transcript_html_zh = normalize_transcript_html_style(transcript_zh, color="#374151")
        else:
            chunks = split_html_by_paragraphs(ep.transcript_html_en, MAX_CHUNK_CHARS)
            print(f"▶ Translating transcript ({len(chunks)} chunks)...", flush=True)
            out_parts: list[str] = []
            for i, ch in enumerate(chunks):
                print(f"  ▶ Chunk {i + 1}/{len(chunks)} ({len(ch)} chars)...", flush=True)
                zh = gemini_translate_html(api_key, ch)
                out_parts.append(zh)

                if i != len(chunks) - 1:
                    print(f"  ⏳ Pausing 15s before next chunk...", flush=True)
                    time.sleep(15)

            transcript_html_zh = normalize_transcript_html_style("\n".join(out_parts), color="#374151")

        print("✔ Translation complete.", flush=True)

        if not title_zh or not summary_zh or not transcript_html_zh:
            return None, None, None

        return title_zh, summary_zh, transcript_html_zh

    except SystemExit:
        raise  # API Key 无效等，直接终止
    except Exception as e:
        print(f"✖ Translation failed, will send English-only email: {e}", flush=True)
        return None, None, None


def normalize_transcript_html_style(html: str, *, color: str) -> str:
    soup = BeautifulSoup(f"<div>{html}</div>", "html.parser")
    for p in soup.find_all("p"):
        existing = (p.get("style") or "").strip()
        style = f"margin:0 0 10px 0;color:{color};font-size:14px !important;line-height:1.6 !important;"
        p["style"] = (existing.rstrip(";") + ";" + style) if existing else style
    div = soup.find("div")
    return "".join(str(x) for x in div.contents) if div else html


def escape_min(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_email_via_maileroo(
    api_key: str,
    *,
    email_from: str,
    email_to_list: list[str],
    subject: str,
    html: str,
    plain: str,
) -> None:
    """
    Maileroo API — POST https://api.maileroo.com/v1/email/send
    """
    url = "https://api.maileroo.com/v1/email/send"
    payload = {
        "from": {"email": email_from, "name": "Newsletter"},
        "to": [{"email": x.strip()} for x in email_to_list if x.strip()],
        "subject": subject,
        "html": html,
        "plain": plain,
    }
    print(f"▶ Sending email via Maileroo to {len(email_to_list)} recipient(s)...", flush=True)
    resp = requests.post(
        url,
        timeout=60,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        json=payload,
    )
    if not resp.ok:
        print(f"✖ Maileroo response: {resp.status_code} {resp.text[:300]}", flush=True)
    resp.raise_for_status()
    print("✔ Email sent successfully.", flush=True)


def update_pages(ep: Episode) -> None:
    print("▶ Updating GitHub Pages...", flush=True)
    os.makedirs(DOCS_DIR, exist_ok=True)

    items: list[dict] = []
    if os.path.exists(DATA_JSON_PATH):
        try:
            with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
                items = json.load(f) or []
        except Exception:
            items = []

    record = {
        "title": ep.title,
        "date": ep.pub_date_bj,
        "summary": ep.summary,
        "link": ep.link,
    }

    links = [x.get("link") for x in items]
    if ep.link in links:
        idx = links.index(ep.link)
        items.pop(idx)
    items.insert(0, record)
    items = items[:6]

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(build_pages_index_html(items))

    print("✔ Pages updated.", flush=True)


def main() -> None:
    print("═══ Cheeky Pint Newsletter ═══", flush=True)

    print("▶ Validating environment variables...", flush=True)
    gemini_key = must_env("GEMINI_API_KEY")
    maileroo_key = must_env("MAILEROO_API_KEY")
    email_to = must_env("EMAIL_TO")
    email_from = must_env("EMAIL_FROM")
    print("✔ All env vars present.", flush=True)

    email_to_list = re.split(r"[,\s;]+", email_to.strip())
    email_to_list = [x for x in email_to_list if x]

    ep = fetch_latest_episode()

    title_zh, summary_zh, transcript_html_zh = translate_episode(gemini_key, ep)

    bj_tz = ZoneInfo("Asia/Shanghai")
    updated_at_bj = datetime.now(tz=bj_tz).strftime("%Y-%m-%d %H:%M")

    subject = f"🍺Cheeky Pint - {ep.pub_date_bj}"

    html = build_email_html(
        title_en=ep.title,
        pub_date_bj=ep.pub_date_bj,
        summary_en=ep.summary,
        link=ep.link,
        transcript_html_en=ep.transcript_html_en,
        updated_at_bj=updated_at_bj,
        title_zh=title_zh,
        summary_zh=summary_zh,
        transcript_html_zh=transcript_html_zh,
    )

    text = (
        f"ENGLISH\n\n"
        f"{ep.title}\n"
        f"Date: {ep.pub_date_bj}\n"
        f"Summary: {ep.summary}\n"
        f"Link: {ep.link}\n\n"
        f"Transcript (EN):\n{ep.transcript_text_en}\n"
    )

    send_email_via_maileroo(
        maileroo_key,
        email_from=email_from,
        email_to_list=email_to_list,
        subject=subject,
        html=html,
        plain=text,
    )

    update_pages(ep)

    print("═══ Done ═══", flush=True)


if __name__ == "__main__":
    main()
