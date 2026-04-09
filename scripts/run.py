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
        # fallback: now
        pub_dt = datetime.now(timezone.utc)

    bj_tz = ZoneInfo("Asia/Shanghai")
    pub_bj = pub_dt.astimezone(bj_tz)
    pub_date_bj = pub_bj.strftime("%Y-%m-%d")

    # 摘要（RSS description/summary 常含 HTML）
    summary_html = entry.get("summary") or entry.get("description") or ""
    summary = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)

    transcript_url = link.rstrip("/") + "/transcript"
    transcript_html_en, transcript_text_en = fetch_transcript(transcript_url)

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

    section = soup.select_one("section.episode-description.episode-transcript") or soup.select_one("section.episode-transcript")
    if not section:
        raise RuntimeError("Transcript section not found")

    ps = section.find_all("p")
    if not ps:
        raise RuntimeError("No <p> found in transcript section")

    # 只要从 [00:00...] 开始：通常第一个 <p> 就是。
    # 若前面有非时间戳段落，则从首次出现 [00:00 或 [00: 开头的段落开始。
    start_idx = 0
    ts_pat = re.compile(r"^\[\d{2}:\d{2}:\d{2}")
    for i, p in enumerate(ps):
        txt = p.get_text("\n", strip=True)
        if ts_pat.search(txt):
            start_idx = i
            break

    ps = ps[start_idx:]

    # 组装英文 transcript HTML：给每个 <p> 增加 inline 样式以保证邮件展示
    # 注意：不改动内容结构（保持 <p> 与 <br>）
    html_parts = []
    text_parts = []

    for p in ps:
        # transcript 页面通常是:
        # <p>[00:..] Speaker<br />Text ...</p>
        # 我们尽量保留 <br>，并加上样式
        p_copy = BeautifulSoup(str(p), "html.parser").p
        if p_copy is None:
            continue

        # 强制 inline style（避免部分邮件客户端丢失外层样式）
        existing_style = (p_copy.get("style") or "").strip()
        style = "margin:0 0 10px 0;color:#111827;font-size:14px !important;line-height:1.6 !important;"
        if existing_style:
            style = existing_style.rstrip(";") + ";" + style
        p_copy["style"] = style

        html_parts.append(str(p_copy))

        # 纯文本备用
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

        # 单个段落过长：仍然单独作为一块（不从标签中间切断）
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

            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                wait_s = int(ra) if ra and ra.isdigit() else 60
                # 抖动
                wait_s = wait_s + random.uniform(0, 1.0)
                time.sleep(wait_s)
                continue

            if 400 <= resp.status_code < 500:
                # 不可重试的 4xx（除 429 外）
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

        except Exception as e:
            if attempt >= max_retries:
                raise

            # 指数退避 + 抖动
            backoff = min(2 ** (attempt - 1), 64)
            sleep_s = backoff + random.uniform(0, 1.0)
            time.sleep(sleep_s)

    raise RuntimeError("Unreachable")


def translate_episode(api_key: str, ep: Episode) -> tuple[str | None, str | None, str | None]:
    """
    返回 (title_zh, summary_zh, transcript_html_zh)
    任一失败 -> 返回全 None（确保邮件只发英文）
    """
    try:
        title_zh = gemini_translate_html(api_key, f"<p>{escape_min(ep.title)}</p>")
        title_zh = BeautifulSoup(title_zh, "html.parser").get_text(" ", strip=True) or None

        summary_zh = gemini_translate_html(api_key, f"<p>{escape_min(ep.summary)}</p>")
        summary_zh = BeautifulSoup(summary_zh, "html.parser").get_text(" ", strip=True) or None

        # transcript：按长度分段
        if len(ep.transcript_html_en) < DIRECT_TRANSLATE_THRESHOLD:
            transcript_zh = gemini_translate_html(api_key, ep.transcript_html_en)
            transcript_html_zh = normalize_transcript_html_style(transcript_zh, color="#374151")
        else:
            chunks = split_html_by_paragraphs(ep.transcript_html_en, MAX_CHUNK_CHARS)
            out_parts: list[str] = []
            for i, ch in enumerate(chunks):
                zh = gemini_translate_html(api_key, ch)
                out_parts.append(zh)

                # 每段成功后暂停 15s（最后一段不需要）
                if i != len(chunks) - 1:
                    time.sleep(15)

            transcript_html_zh = normalize_transcript_html_style("\n".join(out_parts), color="#374151")

        if not title_zh or not summary_zh or not transcript_html_zh:
            return None, None, None

        return title_zh, summary_zh, transcript_html_zh

    except Exception:
        # 按要求：翻译失败则继续发仅英文邮件
        return None, None, None


def normalize_transcript_html_style(html: str, *, color: str) -> str:
    """
    确保中文 transcript 的 <p> 也有稳定的 inline style（颜色/字号/行高）。
    """
    soup = BeautifulSoup(f"<div>{html}</div>", "html.parser")
    for p in soup.find_all("p"):
        existing = (p.get("style") or "").strip()
        style = f"margin:0 0 10px 0;color:{color};font-size:14px !important;line-height:1.6 !important;"
        p["style"] = (existing.rstrip(";") + ";" + style) if existing else style
    # 返回 div 内部
    div = soup.find("div")
    return "".join(str(x) for x in div.contents) if div else html


def escape_min(s: str) -> str:
    # 简单最小转义用于包进 <p>... 的输入
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_email_via_maileroo(
    api_key: str,
    *,
    email_from: str,
    email_to_list: list[str],
    subject: str,
    html: str,
    text: str,
) -> None:
    """
    Maileroo API（如与你账户的 API Endpoint/字段略有差异，请按 Maileroo 文档调整此处）。
    常见用法：
      POST https://api.maileroo.com/send
      Header: X-API-Key: ...
    """
    url = "https://api.maileroo.com/send"
    payload = {
        "from": {"email": email_from, "name": "Newsletter"},
        "to": [{"email": x.strip()} for x in email_to_list if x.strip()],
        "subject": subject,
        "html": html,
        "text": text,
    }
    resp = requests.post(
        url,
        timeout=60,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        json=payload,
    )
    resp.raise_for_status()


def update_pages(ep: Episode) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)

    # 读取历史
    items: list[dict] = []
    if os.path.exists(DATA_JSON_PATH):
        try:
            with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
                items = json.load(f) or []
        except Exception:
            items = []

    # 新记录（只包含 Pages 要展示的字段）
    record = {
        "title": ep.title,
        "date": ep.pub_date_bj,
        "summary": ep.summary,
        "link": ep.link,
    }

    # 去重（按 link）
    links = [x.get("link") for x in items]
    if ep.link in links:
        # 如果已存在，移动到最前并更新字段
        idx = links.index(ep.link)
        items.pop(idx)
    items.insert(0, record)

    # 保留最近 6 条
    items = items[:6]

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(build_pages_index_html(items))


def main() -> None:
    gemini_key = must_env("GEMINI_API_KEY")
    maileroo_key = must_env("MAILEROO_API_KEY")
    email_to = must_env("EMAIL_TO")
    email_from = must_env("EMAIL_FROM")

    email_to_list = re.split(r"[,\s;]+", email_to.strip())
    email_to_list = [x for x in email_to_list if x]

    ep = fetch_latest_episode()

    # 翻译（允许失败）
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
    # 按要求：翻译失败时发送只含英文原文邮件
    # 这里 text 始终为英文；HTML 中只有当三项中文都成功才显示中文块
    send_email_via_maileroo(
        maileroo_key,
        email_from=email_from,
        email_to_list=email_to_list,
        subject=subject,
        html=html,
        text=text,
    )

    # 更新 Pages（仅标题/日期/摘要；保留 6 条）
    update_pages(ep)


if __name__ == "__main__":
    main()
