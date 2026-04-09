import html as html_mod
import json
import os
import re
import sys
import time
import random
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

# ── Configuration (all from environment, never hardcoded) ────────────────────
GEMINI_API_KEY = os.environ['GEMINI_API_KEY']
MAILEROO_API_KEY = os.environ['MAILEROO_API_KEY']
EMAIL_TO = os.environ['EMAIL_TO']          # comma-separated for multiple
EMAIL_FROM = os.environ['EMAIL_FROM']

MODEL = 'gemini-3.1-flash-lite-preview'    # do NOT change
MAX_CHARS = 5500                           # per-chunk char limit
MAX_RETRIES = 10
CHUNK_PAUSE = 15                           # seconds between chunks
BEIJING_TZ = timezone(timedelta(hours=8))
FEED_URL = 'https://feeds.transistor.fm/cheeky-pint-with-john-collison'
DATA_FILE = Path('docs/data.json')


# ── Utilities ────────────────────────────────────────────────────────────────
def e(text):
    """HTML-escape text."""
    return html_mod.escape(str(text), quote=False)


def strip_html(text):
    """Remove HTML tags and collapse whitespace."""
    clean = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\s+', ' ', clean).strip()


# ── 1. Fetch latest episode from RSS ────────────────────────────────────────
def fetch_latest_episode():
    print('Fetching RSS feed …')
    resp = requests.get(FEED_URL, timeout=30)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)

    if not feed.entries:
        print('ERROR: No episodes found.'); sys.exit(1)

    entry = feed.entries[0]
    title   = entry.get('title', '').strip()
    summary = strip_html(entry.get('summary', '') or entry.get('itunes_summary', ''))
    link    = entry.get('link', '').strip()

    if not link:
        print('ERROR: No link found for episode.'); sys.exit(1)

    if entry.get('published_parsed'):
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        date_str = dt.astimezone(BEIJING_TZ).strftime('%Y-%m-%d')
    else:
        date_str = entry.get('published', 'Unknown')

    print(f'  Title : {title}')
    print(f'  Date  : {date_str}')
    print(f'  Link  : {link}')
    return {'title': title, 'date': date_str, 'summary': summary, 'link': link}


# ── 2. Fetch transcript ─────────────────────────────────────────────────────
def fetch_transcript(episode_link):
    url = episode_link.rstrip('/') + '/transcript'
    print(f'Fetching transcript: {url}')
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f'  WARNING: transcript fetch failed: {exc}'); return ''

    soup = BeautifulSoup(resp.text, 'html.parser')
    section = soup.find('section', class_='episode-transcript')
    if not section:
        print('  WARNING: transcript section not found.'); return ''

    paragraphs = section.find_all('p')
    parts = []
    found_start = False
    for p in paragraphs:
        for br in p.find_all('br'):
            br.replace_with('\n')
        text = p.get_text().strip()
        if not found_start:
            if re.match(r'\[00:00:', text):
                found_start = True
            else:
                continue
        if text:
            parts.append(text)

    transcript = '\n\n'.join(parts)
    print(f'  Transcript length: {len(transcript)} chars')
    return transcript


# ── 3. Translation via Gemini ───────────────────────────────────────────────
def _translate_chunk(text):
    """Translate one chunk with full retry logic."""
    api_url = (
        f'https://generativelanguage.googleapis.com/v1beta/'
        f'models/{MODEL}:generateContent?key={GEMINI_API_KEY}'
    )
    prompt = (
        'Translate the following English podcast transcript to Simplified Chinese. '
        'Keep timestamps (e.g. [00:00:25.06]) and speaker names unchanged. '
        'Only translate the spoken content. '
        'Maintain the original paragraph structure and line breaks. '
        'Output ONLY the translation, nothing else.\n\n' + text
    )
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.3},
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(api_url, json=payload, timeout=120)

            # ── 429 rate limit ──
            if resp.status_code == 429:
                ra = resp.headers.get('Retry-After')
                wait = int(ra) if ra else (2 ** attempt + random.uniform(0, 2))
                print(f'  429 rate limited. Waiting {wait:.0f}s (attempt {attempt+1}/{MAX_RETRIES})')
                time.sleep(wait)
                continue

            # ── non-retryable client errors ──
            if resp.status_code in (400, 401, 403, 404):
                print(f'  Non-retryable ({resp.status_code}): {resp.text[:300]}')
                return None

            # ── server errors → retry ──
            if resp.status_code >= 500:
                wait = 2 ** attempt + random.uniform(0, 2)
                print(f'  Server {resp.status_code}. Retry {wait:.1f}s (attempt {attempt+1}/{MAX_RETRIES})')
                time.sleep(wait)
                continue

            # ── success ──
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get('candidates', [])
                if candidates:
                    cparts = candidates[0].get('content', {}).get('parts', [])
                    if cparts:
                        result = cparts[0].get('text', '').strip()
                        if result:
                            return result
                print(f'  Empty response. Retry (attempt {attempt+1}/{MAX_RETRIES})')
                time.sleep(2 ** attempt + random.uniform(0, 2))
                continue

            # ── other status ──
            print(f'  Unexpected {resp.status_code}: {resp.text[:300]}')
            return None

        except requests.RequestException as exc:
            wait = 2 ** attempt + random.uniform(0, 2)
            print(f'  Network error: {exc}. Retry {wait:.1f}s (attempt {attempt+1}/{MAX_RETRIES})')
            time.sleep(wait)

    print(f'  All {MAX_RETRIES} retries exhausted.')
    return None


def _split_chunks(text, max_chars=MAX_CHARS):
    """Split by paragraph boundary (block-level), each chunk ≤ max_chars."""
    paragraphs = text.split('\n\n')
    chunks, cur = [], ''
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if not cur:
            cur = para
        elif len(cur) + 2 + len(para) <= max_chars:
            cur += '\n\n' + para
        else:
            chunks.append(cur)
            cur = para
    if cur:
        chunks.append(cur)
    return chunks


def translate_text(text):
    """Translate with auto-chunking and inter-chunk pauses."""
    if not text:
        return ''
    if len(text) < 6000:
        print('  Single-chunk translation …')
        return _translate_chunk(text)

    chunks = _split_chunks(text)
    print(f'  Text ≥ 6000 → {len(chunks)} chunks')
    translated = []
    for i, chunk in enumerate(chunks):
        print(f'  Chunk {i+1}/{len(chunks)} ({len(chunk)} chars) …')
        result = _translate_chunk(chunk)
        if result is None:
            print(f'  Translation failed at chunk {i+1}.')
            return None
        translated.append(result)
        if i < len(chunks) - 1:
            print(f'  Pausing {CHUNK_PAUSE}s …')
            time.sleep(CHUNK_PAUSE)
    return '\n\n'.join(translated)


# ── 4. Email composition ────────────────────────────────────────────────────
def _text_to_html(text, color):
    """Convert plain-text paragraphs to styled HTML <p> tags."""
    if not text:
        return ''
    parts = []
    for para in text.split('\n\n'):
        para = para.strip()
        if para:
            escaped = e(para).replace('\n', '<br/>')
            parts.append(
                f'<p style="margin:0 0 12px 0;color:{color};">{escaped}</p>'
            )
    return '\n'.join(parts)


def build_email(title, date, summary, transcript_en, transcript_cn):
    now_str = datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M UTC+8')
    en_html = _text_to_html(transcript_en, '#111827')

    cn_block = ''
    if transcript_cn:
        cn_html = _text_to_html(transcript_cn, '#374151')
        cn_block = f'''
        <div style="margin-top:28px;">
          <h3 style="margin:0 0 10px 0;font-size:15px;font-weight:700;color:#374151;font-family:Arial,Helvetica,sans-serif;">🤖 中文翻译</h3>
          <div style="max-width:300px;width:100%;font-size:14px!important;line-height:1.6!important;color:#374151;font-family:Arial,Helvetica,sans-serif;">
            {cn_html}
          </div>
        </div>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td align="center" style="padding:0;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;">

  <!-- HEADER -->
  <tr><td align="center" style="background:linear-gradient(135deg,#0F172A 0%,#1e293b 100%);padding:28px 20px;">
    <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;">🍺 Cheeky Pint</h1>
  </td></tr>

  <!-- BODY -->
  <tr><td style="background:#ffffff;padding:28px 24px;">
    <h2 style="margin:0 0 6px 0;font-size:18px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;">{e(title)}</h2>
    <p style="margin:0 0 12px 0;font-size:14px;color:#6b7280;font-family:Arial,Helvetica,sans-serif;">📅 {e(date)}</p>
    <p style="margin:0 0 20px 0;font-size:14px;color:#6b7280;line-height:1.6;font-family:Arial,Helvetica,sans-serif;">{e(summary)}</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #e5e7eb;font-size:0;line-height:0;height:1px;">&nbsp;</td></tr></table>
    <div style="height:20px;font-size:0;line-height:0;">&nbsp;</div>

    <h3 style="margin:0 0 10px 0;font-size:15px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;">📖 ENGLISH</h3>
    <div style="max-width:300px;width:100%;font-size:14px!important;line-height:1.6!important;color:#111827;font-family:Arial,Helvetica,sans-serif;">
      {en_html}
    </div>
    {cn_block}
  </td></tr>

  <!-- FOOTER -->
  <tr><td align="center" style="background:linear-gradient(135deg,#1e293b 0%,#0F172A 100%);padding:20px;">
    <p style="margin:0;font-size:12px;color:#94a3b8;font-family:Arial,Helvetica,sans-serif;">Updated at {now_str}</p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>'''


def send_email(subject, html_content):
    to_list = [a.strip() for a in EMAIL_TO.split(',') if a.strip()]
    if not to_list:
        print('ERROR: no recipients.')
        return

    payload = {
        "from": {"address": EMAIL_FROM, "display_name": "Newsletter"},
        "to": [{"address": a} for a in to_list],
        "subject": subject,
        "html": html_content,
        # "plain": "optional plain text",
    }

    headers = {
        "Authorization": f"Bearer {MAILEROO_API_KEY}",
    }

    resp = requests.post(
        "https://smtp.maileroo.com/api/v2/emails",
        json=payload,
        headers=headers,
        timeout=30,
    )
    print(f"Maileroo: {resp.status_code} {resp.text[:500]}")
    resp.raise_for_status()


# ── 5. GitHub Pages data ────────────────────────────────────────────────────
def update_pages_data(episode):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    records = []
    if DATA_FILE.exists():
        try:
            records = json.loads(DATA_FILE.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, ValueError):
            records = []

    for r in records:
        if r.get('title') == episode['title']:
            print('Episode already in records (no change).')
            return

    records.insert(0, {
        'title':  episode['title'],
        'date':   episode['date'],
        'summary': episode['summary'],
    })
    records = records[:6]

    DATA_FILE.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    print(f'Updated {DATA_FILE} ({len(records)} records).')


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    print('═══ Cheeky Pint Newsletter ═══')
    now = datetime.now(BEIJING_TZ)
    print(f'Beijing time: {now.strftime("%Y-%m-%d %H:%M:%S")}')

    # 1 ── latest episode
    episode = fetch_latest_episode()

    # 2 ── transcript
    transcript_en = fetch_transcript(episode['link'])

    # 3 ── translate
    transcript_cn = None
    if transcript_en:
        print('Translating …')
        transcript_cn = translate_text(transcript_en)
        if transcript_cn:
            print('Translation succeeded.')
        else:
            print('Translation FAILED – will send English-only email.')
    else:
        print('No transcript available.')

    # 4 ── build & send email
    subject = f'🍺Cheeky Pint - {episode["date"]}'
    html = build_email(
        episode['title'], episode['date'], episode['summary'],
        transcript_en, transcript_cn,
    )
    try:
        print('Sending email …')
        send_email(subject, html)
        print('Email sent.')
    except Exception as exc:
        print(f'Email send failed: {exc}')
        traceback.print_exc()

    # 5 ── update GitHub Pages data
    update_pages_data(episode)

    print('═══ Done ═══')


if __name__ == '__main__':
    main()
