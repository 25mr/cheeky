from __future__ import annotations

from datetime import datetime

def build_pages_index_html(items: list[dict]) -> str:
    # items: [{title, date, summary, link}]
    rows = []
    for it in items:
        rows.append(f"""
        <article class="card">
          <h2 class="title"><a href="{it["link"]}" target="_blank" rel="noopener noreferrer">{escape_html(it["title"])}</a></h2>
          <div class="meta">📅 {escape_html(it["date"])}</div>
          <p class="summary">{escape_html(it["summary"])}</p>
        </article>
        """.strip())

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Cheeky Pint - Latest</title>
  <style>
    :root {{
      --bg: #0b1220;
      --card: #0f172a;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --link: #93c5fd;
      --border: rgba(255,255,255,0.08);
    }}
    body {{
      margin: 0;
      font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,"Noto Sans","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
      background: radial-gradient(1200px 600px at 10% 10%, rgba(59,130,246,0.18), transparent 60%),
                  radial-gradient(900px 500px at 90% 20%, rgba(34,197,94,0.14), transparent 60%),
                  var(--bg);
      color: var(--text);
    }}
    .wrap {{
      max-width: 900px;
      margin: 0 auto;
      padding: 24px 16px 48px;
    }}
    header {{
      padding: 18px 18px;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: linear-gradient(135deg, #0F172A, #111827, #0F172A);
    }}
    header h1 {{
      margin: 0;
      font-size: 18px;
      letter-spacing: 0.2px;
    }}
    header p {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .grid {{
      margin-top: 18px;
      display: grid;
      grid-template-columns: 1fr;
      gap: 14px;
    }}
    .card {{
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px 16px;
      background: rgba(15,23,42,0.75);
      backdrop-filter: blur(6px);
    }}
    .title {{
      margin: 0 0 8px;
      font-size: 16px;
      line-height: 1.4;
    }}
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 10px;
    }}
    .summary {{
      margin: 0;
      color: #d1d5db;
      font-size: 14px;
      line-height: 1.6;
      white-space: pre-wrap;
    }}
    footer {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>🍺 Cheeky Pint (Latest 6)</h1>
      <p>Auto-updated by GitHub Actions. Click the title to open the episode page.</p>
    </header>

    <main class="grid">
      {"".join(rows) if rows else '<p style="color:#9ca3af">No data yet.</p>'}
    </main>

    <footer>
      <div>Source: Transistor RSS</div>
    </footer>
  </div>
</body>
</html>
"""


def build_email_html(
    *,
    title_en: str,
    pub_date_bj: str,
    summary_en: str,
    link: str,
    transcript_html_en: str,
    updated_at_bj: str,
    title_zh: str | None = None,
    summary_zh: str | None = None,
    transcript_html_zh: str | None = None,
) -> str:

    show_zh = (title_zh is not None) and (summary_zh is not None) and (transcript_html_zh is not None)

    preheader = f"{title_en} | {pub_date_bj}"

    def section_heading(text: str) -> str:
        return f"""
          <tr>
            <td style="padding: 14px 18px 8px; font-weight: 700; color: #111827; font-size: 14px; line-height: 1.6;">
              {escape_html(text)}
            </td>
          </tr>
        """

    def section_divider() -> str:
        return """
          <tr>
            <td style="padding: 0 18px;">
              <div style="height:1px;background:#e5e7eb;opacity:0.8;"></div>
            </td>
          </tr>
        """

    # Transcript HTML 进入邮件：需要确保 <p> 有行高与颜色
    transcript_en_block = f"""
      <div style="color:#111827;font-size:14px !important;line-height:1.6 !important;">
        {transcript_html_en}
      </div>
    """

    transcript_zh_block = f"""
      <div style="color:#374151;font-size:14px !important;line-height:1.6 !important;max-width:340px;margin:0 auto;word-break:break-word;">
        {transcript_html_zh}
      </div>
    """ if show_zh else ""

    # 为了邮件客户端兼容，尽量使用 inline；渐变并不保证所有客户端都支持，但会有 background-color fallback
    header_html = f"""
    <tr>
      <td style="background-color:#0F172A;background-image:linear-gradient(135deg,#0F172A,#111827,#0F172A);padding:18px 18px;">
        <div style="font-family:Arial,Helvetica,sans-serif;color:#ffffff;font-size:16px;line-height:1.4;font-weight:700;">
          🍺 Cheeky Pint
        </div>
        <div style="font-family:Arial,Helvetica,sans-serif;color:#cbd5e1;font-size:12px;line-height:1.5;margin-top:4px;">
          Weekly episode transcript digest
        </div>
      </td>
    </tr>
    """

    footer_html = f"""
    <tr>
      <td style="background-color:#0F172A;background-image:linear-gradient(135deg,#0F172A,#111827,#0F172A);padding:16px 18px;text-align:center;">
        <div style="font-family:Arial,Helvetica,sans-serif;color:#cbd5e1;font-size:12px;line-height:1.6;">
          Updated at {escape_html(updated_at_bj)} UTC+8
        </div>
      </td>
    </tr>
    """

    zh_block_html = ""
    if show_zh:
        zh_block_html = f"""
        {section_divider()}
        <tr>
          <td style="padding: 14px 18px 6px; font-family:Arial,Helvetica,sans-serif;">
            <div style="font-weight:700;color:#111827;font-size:14px;line-height:1.6;">🤖 中文翻译</div>
          </td>
        </tr>

        <tr>
          <td style="padding: 0 18px 14px; font-family:Arial,Helvetica,sans-serif;">
            <div style="color:#374151;font-size:14px !important;line-height:1.6 !important;max-width:340px;margin:0 auto;word-break:break-word;">
              <div style="font-weight:700;margin-bottom:6px;">{escape_html(title_zh)}</div>
              <div style="margin-bottom:10px;">📅 {escape_html(pub_date_bj)}</div>
              <div style="margin-bottom:10px;white-space:pre-wrap;">{escape_html(summary_zh)}</div>
              <div style="margin-top:12px;">
                {transcript_zh_block}
              </div>
            </div>
          </td>
        </tr>
        """

    # 英文块
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Cheeky Pint</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;">
  <!-- Preheader (hidden) -->
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">
    {escape_html(preheader)}
  </div>

  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f3f4f6;">
    <tr>
      <td align="center" style="padding:16px 10px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="width:100%;max-width:600px;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e5e7eb;">
          {header_html}

          <tr>
            <td style="padding: 14px 18px 6px; font-family:Arial,Helvetica,sans-serif;">
              <div style="font-weight:700;color:#111827;font-size:14px;line-height:1.6;">📖 ENGLISH</div>
            </td>
          </tr>

          <tr>
            <td style="padding: 0 18px 14px; font-family:Arial,Helvetica,sans-serif;">
              <div style="color:#111827;font-size:14px !important;line-height:1.6 !important;">
                <div style="font-weight:700;margin-bottom:6px;">{escape_html(title_en)}</div>
                <div style="margin-bottom:10px;">📅 {escape_html(pub_date_bj)}</div>
                <div style="margin-bottom:10px;white-space:pre-wrap;">{escape_html(summary_en)}</div>
                <div style="margin-bottom:12px;">
                  <a href="{escape_attr(link)}" target="_blank" rel="noopener noreferrer" style="color:#2563eb;text-decoration:underline;">Open episode</a>
                </div>

                <div style="margin-top:12px;">
                  {transcript_en_block}
                </div>
              </div>
            </td>
          </tr>

          {zh_block_html}

          {footer_html}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return html


def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def escape_attr(s: str) -> str:
    # 简单属性转义
    return escape_html(s).replace("'", "&#39;")
