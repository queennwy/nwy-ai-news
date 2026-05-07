#!/usr/bin/env python3
"""
AI 日報自動發送系統
每日 23:55 HKT 抓取 The Verge、TechCrunch、36氪、量子位的 AI 新聞
用 Claude API 做語意篩選，發送至 Gmail
"""

import os
import json
import smtplib
import feedparser
import anthropic
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ─── 設定 ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GMAIL_ADDRESS     = os.environ.get("GMAIL_ADDRESS", "")       # 你嘅 Gmail
GMAIL_APP_PASSWORD= os.environ.get("GMAIL_APP_PASSWORD", "")  # Gmail App Password
RECIPIENT_EMAIL   = os.environ.get("RECIPIENT_EMAIL", "")     # 收件人（可以同上）

HKT = timezone(timedelta(hours=8))

# ─── RSS 來源 ─────────────────────────────────────────────────────────────────
SOURCES = {
    "en": [
        {"name": "The Verge",  "rss": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
        {"name": "TechCrunch", "rss": "https://techcrunch.com/feed/"},
    ],
    "zh": [
        {"name": "36氪",  "rss": "https://36kr.com/feed"},
        {"name": "量子位", "rss": "https://www.qbitai.com/feed"},
    ],
}

AI_KEYWORDS = [
    "AI", "artificial intelligence", "machine learning", "LLM", "GPT", "Claude",
    "Gemini", "OpenAI", "Anthropic", "Google DeepMind", "Meta AI", "neural network",
    "deep learning", "generative AI", "大模型", "人工智能", "生成式", "機器學習",
    "大語言模型", "智能體", "Agent", "多模態", "Sora", "Grok", "Llama",
]

# ─── 抓取當日文章 ─────────────────────────────────────────────────────────────
def fetch_today_articles(sources: dict) -> dict:
    today_start = datetime.now(HKT).replace(hour=0, minute=0, second=0, microsecond=0)
    result = {"en": [], "zh": []}

    for lang, feeds in sources.items():
        for feed_info in feeds:
            try:
                feed = feedparser.parse(feed_info["rss"])
                for entry in feed.entries:
                    # 解析發布時間
                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(HKT)
                    elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                        published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).astimezone(HKT)

                    if not published or published < today_start:
                        continue

                    title   = entry.get("title", "")
                    summary = entry.get("summary", entry.get("description", ""))[:500]
                    link    = entry.get("link", "")

                    # 初步 keyword 過濾
                    combined = (title + " " + summary).lower()
                    is_ai = any(kw.lower() in combined for kw in AI_KEYWORDS)
                    if not is_ai:
                        continue

                    result[lang].append({
                        "source":    feed_info["name"],
                        "title":     title,
                        "summary":   summary,
                        "link":      link,
                        "published": published.strftime("%H:%M HKT"),
                        "published_et": published.astimezone(timezone(timedelta(hours=-4))).strftime("%H:%M ET"),
                    })
            except Exception as e:
                print(f"[ERROR] {feed_info['name']}: {e}")

    return result

# ─── Claude API 語意篩選 ──────────────────────────────────────────────────────
def rank_with_claude(articles: list, lang: str, top_n: int = 5) -> list:
    if not articles:
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    articles_text = "\n\n".join([
        f"[{i+1}] 來源:{a['source']} | 時間:{a['published']}\n標題:{a['title']}\n摘要:{a['summary']}\n連結:{a['link']}"
        for i, a in enumerate(articles)
    ])

    if lang == "en":
        prompt = f"""You are an AI news editor. From the following articles published today, select the TOP {top_n} most impactful and significant AI news stories.

Ranking criteria:
1. Topic importance & industry impact (major product launches, breakthroughs, policy changes)
2. Number of people/companies affected
3. Novelty and newsworthiness

Articles:
{articles_text}

Return ONLY a JSON array of the top {top_n} articles in this exact format (no markdown, no extra text):
[
  {{
    "rank": 1,
    "source": "source name",
    "title": "original title",
    "summary_en": "2-sentence English summary of why this matters",
    "link": "url",
    "published": "HH:MM HKT",
    "published_et": "HH:MM ET",
    "keywords": ["keyword1", "keyword2"]
  }}
]"""
    else:
        prompt = f"""你係一位 AI 科技新聞編輯。從以下今日文章中，揀出最重要、最有影響力嘅 TOP {top_n} AI 新聞。

排序標準：
1. 話題重要性同業界影響（重大產品發布、技術突破、政策變化）
2. 影響人數／公司數量
3. 新聞價值同新鮮感

文章列表：
{articles_text}

只返回 JSON 陣列，格式如下（唔好有 markdown、唔好有多餘文字）：
[
  {{
    "rank": 1,
    "source": "來源名稱",
    "title": "原標題",
    "summary_zh": "用香港口語寫 2 句話解釋點解呢單新聞咁重要",
    "link": "url",
    "published": "HH:MM HKT",
    "published_et": "HH:MM ET",
    "keywords": ["關鍵詞1", "關鍵詞2"]
  }}
]"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        # 清除可能嘅 markdown
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[ERROR] Claude ranking failed: {e}")
        return []

# ─── 生成 HTML 郵件 ───────────────────────────────────────────────────────────
def build_email_html(en_top: list, zh_top: list, date_str: str) -> str:
    def render_article(a: dict, lang: str, rank: int) -> str:
        summary = a.get("summary_en") or a.get("summary_zh", "")
        keywords = " · ".join(a.get("keywords", []))
        return f"""
        <tr>
          <td style="padding:14px 0; border-bottom:1px solid #f0f0f0;">
            <div style="font-size:12px;color:#999;margin-bottom:4px;">
              #{rank} &nbsp;|&nbsp; {a['source']} &nbsp;|&nbsp; {a.get('published_et','')} / {a.get('published','')}
            </div>
            <div style="font-size:16px;font-weight:600;color:#1a1a1a;margin-bottom:6px;">
              <a href="{a['link']}" style="color:#1a1a1a;text-decoration:none;">{a['title']}</a>
            </div>
            <div style="font-size:14px;color:#444;line-height:1.6;margin-bottom:6px;">{summary}</div>
            <div style="font-size:12px;color:#888;">🏷 {keywords}</div>
          </td>
        </tr>"""

    en_rows = "".join(render_article(a, "en", a["rank"]) for a in en_top)
    zh_rows = "".join(render_article(a, "zh", a["rank"]) for a in zh_top)

    en_sources = "、".join(set(a["source"] for a in en_top))
    zh_sources = "、".join(set(a["source"] for a in zh_top))

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:20px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

  <!-- Header -->
  <tr><td style="background:linear-gradient(135deg,#667eea,#764ba2);padding:28px 32px;">
    <div style="color:#fff;font-size:22px;font-weight:700;">🤖 AI 日報</div>
    <div style="color:rgba(255,255,255,0.85);font-size:14px;margin-top:4px;">{date_str} &nbsp;·&nbsp; 每日 23:55 HKT 自動發送</div>
  </td></tr>

  <!-- Intro -->
  <tr><td style="padding:20px 32px 0;">
    <div style="background:#f8f4ff;border-left:4px solid #764ba2;padding:12px 16px;border-radius:0 8px 8px 0;font-size:13px;color:#555;">
      📌 今日重點 — 由 Claude AI 語意分析，按話題影響力排序，keyword 過濾直接交單
    </div>
  </td></tr>

  <!-- EN Top 5 -->
  <tr><td style="padding:24px 32px 0;">
    <div style="font-size:18px;font-weight:700;color:#1a1a1a;border-bottom:2px solid #667eea;padding-bottom:8px;margin-bottom:4px;">
      🇬🇧 英文 Top 5 &nbsp;<span style="font-size:13px;font-weight:400;color:#888;">| {en_sources}</span>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0">{en_rows}</table>
  </td></tr>

  <!-- ZH Top 5 -->
  <tr><td style="padding:24px 32px 0;">
    <div style="font-size:18px;font-weight:700;color:#1a1a1a;border-bottom:2px solid #f093fb;padding-bottom:8px;margin-bottom:4px;">
      🇨🇳 中文 Top 5 &nbsp;<span style="font-size:13px;font-weight:400;color:#888;">| {zh_sources}</span>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0">{zh_rows}</table>
  </td></tr>

  <!-- Footer -->
  <tr><td style="padding:24px 32px;background:#fafafa;border-top:1px solid #eee;margin-top:24px;">
    <div style="font-size:12px;color:#aaa;text-align:center;">
      由 Claude Sonnet 語意分析 · 來源：The Verge · TechCrunch · 36氪 · 量子位<br>
      每日 23:55 HKT 自動發送
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""

# ─── 發送郵件 ─────────────────────────────────────────────────────────────────
def send_email(html: str, subject: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
    print(f"[OK] 郵件已發送至 {RECIPIENT_EMAIL}")

# ─── 主程式 ───────────────────────────────────────────────────────────────────
def main():
    now_hkt  = datetime.now(HKT)
    date_str = now_hkt.strftime("%Y年%m月%d日")
    subject  = f"🤖 AI 日報｜{now_hkt.strftime('%Y/%m/%d')}"

    print(f"[{now_hkt.strftime('%Y-%m-%d %H:%M HKT')}] 開始抓取新聞...")

    # 1. 抓取文章
    articles = fetch_today_articles(SOURCES)
    print(f"[INFO] 英文文章：{len(articles['en'])} 篇 | 中文文章：{len(articles['zh'])} 篇")

    # 2. Claude 語意排序
    print("[INFO] Claude 分析中...")
    en_top = rank_with_claude(articles["en"], "en", top_n=5)
    zh_top = rank_with_claude(articles["zh"], "zh", top_n=5)

    if not en_top and not zh_top:
        print("[WARN] 今日無 AI 相關文章，跳過發送")
        return

    # 3. 生成郵件
    html = build_email_html(en_top, zh_top, date_str)

    # 4. 發送
    send_email(html, subject)

if __name__ == "__main__":
    main()
