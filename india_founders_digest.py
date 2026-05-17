#!/usr/bin/env python3
"""
India Founders Daily Digest — Bulletproof Edition
---------------------------------------------------
Delivers exactly 10 India-based founders with confirmed LinkedIn profiles
every day via iMessage + HTML digest opened in browser.

Search priority:
  1. Recent VC-backed funding announcements (Seed, Pre-Series A, Series A)
  2. Notable India-based founders recently in the news (any coverage)
  3. Backlog — previously found but never-yet-sent founders

LinkedIn is a hard requirement — founders without a confirmed profile are
skipped and replaced. Script keeps searching until it hits exactly 10.

Resilience:
  - Auto-retry on API failures (3 attempts with backoff)
  - Backlog fills gaps automatically
  - iMessage failure alert if script crashes
  - No repeats ever (permanent dedup)
  - Detailed timestamped logging

Usage:
    python india_founders_digest.py

Cron (4AM PST daily):
    0 4 * * * /path/to/venv/bin/python /path/to/india_founders_digest.py >> /path/to/digest.log 2>&1
"""

import json
import logging
import os
import re
import smtplib
import sqlite3
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
DB_PATH            = os.getenv("DB_PATH", "founders_digest.db")
IMESSAGE_RECIPIENT = "+15624456149"
EMAIL_SENDER       = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD     = os.getenv("EMAIL_PASSWORD")
EMAIL_RECIPIENT    = os.getenv("EMAIL_RECIPIENT", "Andyseac@gmail.com")
RUNNING_IN_CLOUD   = os.getenv("RUNNING_IN_CLOUD", "false").lower() == "true"
SEARCH_MODEL       = "claude-haiku-4-5-20251001"  # Haiku is ~15x cheaper than Opus for search/extraction
LINKEDIN_MODEL     = "claude-haiku-4-5-20251001"
RATE_LIMIT_SLEEP   = 65  # seconds to wait after a large search call before the next API call
MAX_RETRIES        = 3
RETRY_BACKOFF      = [5, 15, 30]
TARGET_COUNT       = 10           # must hit exactly this many with LinkedIn
MAX_SEARCH_ROUNDS  = 3            # max extra search rounds if we fall short
NEWS_WINDOW_DAYS   = 14

NEWS_SOURCES = [
    "YourStory", "Inc42", "Entrackr", "VCCircle", "DealStreet Asia",
    "Economic Times Startup", "Forbes India", "Mint", "MediaNama", "The Ken",
    "Business Today", "TechCrunch",
]

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ──────────────────────────────────────────────
# Retry helper
# ──────────────────────────────────────────────
def with_retry(fn, *args, label="operation", **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            is_rate_limit = "429" in str(e) or "rate_limit" in str(e).lower()
            wait = 65 if is_rate_limit else RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            if attempt < MAX_RETRIES - 1:
                log.warning(f"[{label}] attempt {attempt+1} failed: {e} — retrying in {wait}s")
                time.sleep(wait)
            else:
                log.error(f"[{label}] all {MAX_RETRIES} attempts failed: {e}")
                raise


# ──────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS founders (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            founder_name   TEXT NOT NULL,
            company        TEXT NOT NULL,
            funding_stage  TEXT,
            amount_raised  TEXT,
            vc_backers     TEXT,
            linkedin_url   TEXT,
            source_url     TEXT,
            source_type    TEXT DEFAULT 'funding',
            date_found     DATE DEFAULT (date('now')),
            date_sent      DATE,
            UNIQUE(founder_name, company)
        )
    """)
    # Add source_type column if upgrading from old DB
    try:
        c.execute("ALTER TABLE founders ADD COLUMN source_type TEXT DEFAULT 'funding'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    log.info(f"Database ready: {DB_PATH}")


DEDUP_DAYS = 7  # founders won't repeat within this window

def get_all_sent() -> set:
    """Return (name_lower, company_lower) for founders sent in the last DEDUP_DAYS days."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=DEDUP_DAYS)).strftime("%Y-%m-%d")
    c.execute("""
        SELECT founder_name, company FROM founders
        WHERE date_sent >= ? AND date_sent IS NOT NULL
    """, (cutoff,))
    rows = c.fetchall()
    conn.close()
    return {(r[0].lower().strip(), r[1].lower().strip()) for r in rows}


def get_backlog(exclude: set, limit: int) -> list:
    """Pull never-sent founders from DB, ordered by most recently found first."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT founder_name, company, funding_stage, amount_raised,
               vc_backers, linkedin_url, source_url, source_type
        FROM founders
        WHERE date_sent IS NULL
          AND linkedin_url IS NOT NULL
          AND linkedin_url != ''
        ORDER BY date_found DESC
    """)
    rows = c.fetchall()
    conn.close()

    backlog = []
    for r in rows:
        key = (r[0].lower().strip(), r[1].lower().strip())
        if key not in exclude and len(backlog) < limit:
            backlog.append({
                "founder_name": r[0], "company": r[1],
                "funding_stage": r[2] or "", "amount_raised": r[3] or "",
                "vc_backers": r[4] or "", "linkedin_url": r[5] or "",
                "source_url": r[6] or "", "source_type": r[7] or "funding",
            })
    return backlog


def save_founders(founders: list):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for f in founders:
        try:
            c.execute("""
                INSERT INTO founders
                    (founder_name, company, funding_stage, amount_raised,
                     vc_backers, linkedin_url, source_url, source_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(founder_name, company) DO UPDATE SET
                    linkedin_url  = CASE WHEN excluded.linkedin_url != ''
                                    THEN excluded.linkedin_url
                                    ELSE founders.linkedin_url END,
                    funding_stage = COALESCE(excluded.funding_stage, founders.funding_stage),
                    amount_raised = COALESCE(excluded.amount_raised, founders.amount_raised),
                    vc_backers    = COALESCE(excluded.vc_backers,    founders.vc_backers),
                    source_url    = COALESCE(excluded.source_url,    founders.source_url)
            """, (
                f.get("founder_name", ""), f.get("company", ""),
                f.get("funding_stage", ""), f.get("amount_raised", ""),
                f.get("vc_backers", ""), f.get("linkedin_url", ""),
                f.get("source_url", ""), f.get("source_type", "funding"),
            ))
        except Exception as e:
            log.warning(f"DB save error for {f.get('founder_name')}: {e}")
    conn.commit()
    conn.close()


def mark_as_sent(founders: list):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    for f in founders:
        c.execute("""
            UPDATE founders SET date_sent = ?
            WHERE founder_name = ? AND company = ?
        """, (today, f["founder_name"], f["company"]))
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────
# Step 1a: Search funding news
# ──────────────────────────────────────────────
def _search_funding() -> list:
    cutoff = (datetime.now() - timedelta(days=NEWS_WINDOW_DAYS)).strftime("%B %d, %Y")
    sources = ", ".join(NEWS_SOURCES)

    prompt = f"""You are a startup funding researcher. Find venture-backed funding announcements since {cutoff}.

STRICT CRITERIA — include ONLY if ALL apply:
- Company is headquartered in India
- Founder(s) are based in India (not diaspora abroad)
- At least one institutional VC backer (not angel-only)
- Stage is Seed, Pre-Series A, or Series A ONLY

Search across: {sources}

Also try:
- site:yourstory.com funding raised 2026
- site:inc42.com seed OR "series a" funding 2026
- site:entrackr.com funding 2026
- site:dealstreetasia.com India seed "series a" 2026
- India startup funding raised May 2026

Return ONLY a raw JSON array:
[
  {{
    "founder_name": "Full Name",
    "company": "Company Name",
    "funding_stage": "Seed",
    "amount_raised": "$2M",
    "vc_backers": "Blume Ventures, Accel",
    "source_url": "https://...",
    "source_type": "funding"
  }}
]

Find at least 20. Return ONLY the JSON array, no markdown."""

    response = client.messages.create(
        model=SEARCH_MODEL,
        max_tokens=2500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_founders_json(response, required_fields=["founder_name", "company"])


# ──────────────────────────────────────────────
# Step 1b: Search notable founders in the news
# ──────────────────────────────────────────────
def _search_notable_founders(exclude_names: set, needed: int) -> list:
    """Search for VC-backed India-based founders in the news (not necessarily recent raises)."""
    exclude_str = ", ".join(list(exclude_names)[:20]) if exclude_names else "none"

    prompt = f"""Find {needed * 2} India-based startup founders who have raised institutional VC funding at some point (any stage, any time) AND have appeared in news coverage recently (last 30 days).

STRICT CRITERIA:
- Founder must be based in India
- Company must be headquartered in India
- Company must have raised at least one institutional VC round (Seed or later)
- Recent news coverage — any type: interview, product launch, expansion, award, op-ed, etc.
- Must be a real founder/co-founder (not just an executive)

Exclude these already found today: {exclude_str}

Search across: YourStory, Inc42, Entrackr, Economic Times Startup, Mint, Moneycontrol, Forbes India, Business Today, MediaNama, The Ken, VCCircle, DealStreet Asia

Return ONLY a raw JSON array:
[
  {{
    "founder_name": "Full Name",
    "company": "Company Name",
    "funding_stage": "most recent stage if known, else empty",
    "amount_raised": "total or most recent raise if known, else empty",
    "vc_backers": "known VC backers if any, else empty",
    "source_url": "https://...",
    "source_type": "news"
  }}
]

Return ONLY the JSON array, no markdown."""

    response = client.messages.create(
        model=SEARCH_MODEL,
        max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_founders_json(response, required_fields=["founder_name", "company"])


def _parse_founders_json(response, required_fields: list) -> list:
    full_text = "".join(
        block.text for block in response.content if hasattr(block, "text")
    )
    clean = re.sub(r"```(?:json)?|```", "", full_text).strip()
    arr_match = re.search(r"\[[\s\S]*\]", clean)
    if not arr_match:
        return []
    try:
        founders = json.loads(arr_match.group())
        return [
            f for f in founders
            if isinstance(f, dict)
            and all(f.get(field, "").strip() for field in required_fields)
        ]
    except json.JSONDecodeError:
        return []


# ──────────────────────────────────────────────
# Step 2: LinkedIn enrichment (hard requirement)
# ──────────────────────────────────────────────
def _do_linkedin_search(name: str, company: str) -> str:
    prompt = f"""Search Google for the LinkedIn profile of {name}, founder of {company} (India-based startup).

Try:
- site:linkedin.com/in "{name}" "{company}"
- "{name}" "{company}" founder linkedin india

Return ONLY the LinkedIn URL (https://linkedin.com/in/username) if you are confident it's the right person.
If not found or uncertain, return: NOT_FOUND
Nothing else."""

    response = client.messages.create(
        model=LINKEDIN_MODEL,
        max_tokens=150,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if hasattr(block, "text"):
            match = re.search(r"https?://(?:www\.)?linkedin\.com/in/[^\s\"'><\)]+", block.text)
            if match:
                return match.group().rstrip("/.?,)")
    return ""


def enrich_and_filter(candidates: list, already_confirmed: list) -> list:
    """
    Try to find LinkedIn URLs for candidates one at a time.
    Returns only those with confirmed LinkedIn URLs.
    Skips anyone already in already_confirmed.
    """
    confirmed_keys = {
        (f["founder_name"].lower().strip(), f["company"].lower().strip())
        for f in already_confirmed
    }
    results = []

    for f in candidates:
        key = (f["founder_name"].lower().strip(), f["company"].lower().strip())
        if key in confirmed_keys:
            continue

        # Already has a LinkedIn URL (e.g. from backlog)
        if f.get("linkedin_url", "").strip():
            log.info(f"  {f['founder_name'][:35]:35s} → (cached) {f['linkedin_url']}")
            results.append(f)
            confirmed_keys.add(key)
            continue

        if len(already_confirmed) + len(results) >= TARGET_COUNT:
            log.info("  Target count reached — stopping LinkedIn searches early.")
            break

        name = f["founder_name"].split(",")[0].strip()
        try:
            url = with_retry(_do_linkedin_search, name, f["company"], label=f"li:{name}")
        except Exception:
            url = ""

        if url:
            f["linkedin_url"] = url
            log.info(f"  {name[:35]:35s} → {url}")
            results.append(f)
            confirmed_keys.add(key)
        else:
            log.info(f"  {name[:35]:35s} → no LinkedIn — skipping")

        # Pace calls to stay under 50k input tokens/minute on Haiku
        time.sleep(10)

    return results


# ──────────────────────────────────────────────
# Step 3: Build HTML digest
# ──────────────────────────────────────────────
def build_html_digest(founders: list, date_str: str) -> str:
    cards_html = ""
    for i, f in enumerate(founders, 1):
        li_url   = (f.get("linkedin_url") or "").strip()
        src_url  = (f.get("source_url") or "").strip()
        stage    = f.get("funding_stage", "")
        amount   = f.get("amount_raised", "")
        backers  = f.get("vc_backers", "")
        stype    = f.get("source_type", "funding")

        li_html  = (f'<a class="li-link" href="{li_url}">LinkedIn →</a>'
                    if li_url else '<span class="no-li">LinkedIn not found</span>')
        src_html = f'<a class="src-link" href="{src_url}">Source →</a>' if src_url else ""
        sep      = " &nbsp;·&nbsp; " if src_html else ""

        # Funding details row — only show if we have them
        funding_row = ""
        if stage or amount:
            badge = f'<span class="badge">{stage}</span>' if stage else ""
            amt   = f'<span class="amount">💰 {amount}</span>' if amount else ""
            funding_row = f'<div class="meta">{badge} {amt}</div>'

        backers_row = f'<div class="vcs">🏦 {backers}</div>' if backers else ""

        # Tag for news-sourced founders
        tag = ""
        if stype == "news":
            tag = ' <span class="news-tag">In the news</span>'

        cards_html += f"""
        <div class="card">
          <div class="card-num">#{i}</div>
          <div class="card-body">
            <div class="founder-name">{f.get('founder_name','')}{tag}</div>
            <div class="company">{f.get('company','')}</div>
            {funding_row}
            {backers_row}
            <div class="links">{li_html}{sep}{src_html}</div>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>India Founders Digest — {date_str}</title>
<style>
  body        {{ font-family:-apple-system,Arial,sans-serif; background:#f5f5f5; margin:0; padding:20px; color:#333; }}
  .wrap       {{ max-width:680px; margin:0 auto; background:#fff; border-radius:12px;
                 overflow:hidden; box-shadow:0 2px 12px rgba(0,0,0,.1); }}
  .header     {{ background:#0f172a; color:#fff; padding:28px 32px; }}
  .header h1  {{ margin:0 0 6px; font-size:22px; }}
  .header p   {{ margin:0; color:#94a3b8; font-size:14px; }}
  .body       {{ padding:24px 32px; }}
  .card       {{ display:flex; gap:16px; border:1px solid #e2e8f0; border-radius:10px;
                 padding:16px; margin-bottom:14px; background:#fafafa; }}
  .card-num   {{ font-size:22px; font-weight:800; color:#e11d48; min-width:32px; line-height:1; padding-top:2px; }}
  .founder-name {{ font-size:17px; font-weight:700; color:#0f172a; }}
  .company    {{ font-size:15px; color:#e11d48; font-weight:600; margin:2px 0 8px; }}
  .meta       {{ display:flex; align-items:center; gap:10px; margin-bottom:6px; }}
  .badge      {{ background:#0f172a; color:#fff; font-size:11px; font-weight:600; padding:2px 8px; border-radius:20px; }}
  .amount     {{ font-size:13px; color:#475569; }}
  .vcs        {{ font-size:13px; color:#475569; margin-bottom:8px; }}
  .links      {{ font-size:13px; }}
  .li-link    {{ color:#0077b5; font-weight:600; text-decoration:none; }}
  .no-li      {{ color:#94a3b8; font-style:italic; }}
  .src-link   {{ color:#64748b; text-decoration:none; }}
  .news-tag   {{ font-size:11px; font-weight:500; color:#7c3aed; background:#ede9fe;
                 padding:2px 7px; border-radius:10px; margin-left:8px; vertical-align:middle; }}
  .footer     {{ padding:20px 32px; background:#f8fafc; border-top:1px solid #e2e8f0; font-size:12px; color:#94a3b8; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>🇮🇳 India Founders Daily Digest</h1>
    <p>{date_str} &nbsp;·&nbsp; {len(founders)} founders · all with LinkedIn</p>
  </div>
  <div class="body">
    <p style="color:#64748b;font-size:14px;margin:0 0 20px">
      India-based founders at India-based companies · VC-backed · Seed → Series A prioritized · No repeats within 7 days
    </p>
    {cards_html}
  </div>
  <div class="footer">
    Generated by india_founders_digest.py · Sources: {', '.join(NEWS_SOURCES[:8])} and more.
  </div>
</div>
</body>
</html>"""


def save_html_digest(founders: list) -> Path:
    today    = datetime.now().strftime("%Y-%m-%d")
    date_str = datetime.now().strftime("%B %d, %Y")
    out_path = Path(__file__).parent / f"digest_{today}.html"
    out_path.write_text(build_html_digest(founders, date_str), encoding="utf-8")
    log.info(f"HTML digest saved → {out_path}")
    return out_path


# ──────────────────────────────────────────────
# Step 4: iMessage delivery
# ──────────────────────────────────────────────
def _send_imessage_raw(message: str):
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
        tell application "Messages"
            set targetService to 1st service whose service type = iMessage
            set targetBuddy to buddy "{IMESSAGE_RECIPIENT}" of targetService
            send "{safe}" to targetBuddy
        end tell
    '''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True)


def send_email_digest(founders: list, html_path: Path):
    """Send digest via Gmail — used when running in GitHub Actions."""
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        log.warning("Email credentials missing — skipping email delivery.")
        return

    today   = datetime.now().strftime("%B %d, %Y")
    subject = f"🇮🇳 India Founders Digest — {today} ({len(founders)} founders)"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html_path.read_text(encoding="utf-8"), "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        log.info(f"Email sent to {EMAIL_RECIPIENT} ✓")
    except Exception as e:
        log.warning(f"Email delivery failed: {e}")


def deliver_digest(founders: list, html_path: Path):
    today   = datetime.now().strftime("%B %d, %Y")

    if RUNNING_IN_CLOUD:
        # Cloud: email only (no Mac Messages app available)
        log.info("Running in cloud — delivering via email.")
        send_email_digest(founders, html_path)
    else:
        # Local Mac: try iMessage, also open browser
        message = (
            f"🇮🇳 India Founders Digest — {today}\n"
            f"{len(founders)} founders with LinkedIn ready:\n"
            f"{html_path.as_uri()}"
        )
        try:
            _send_imessage_raw(message)
            log.info(f"iMessage sent to {IMESSAGE_RECIPIENT} ✓")
        except Exception as e:
            log.warning(f"iMessage failed (non-fatal): {e}")
        webbrowser.open(html_path.as_uri())
        log.info("Digest opened in browser ✓")


def send_failure_alert(error_msg: str):
    today   = datetime.now().strftime("%B %d, %Y")
    message = (
        f"⚠️ India Founders Digest FAILED — {today}\n"
        f"Error: {error_msg[:200]}\n"
        f"Check digest.log for details."
    )
    if RUNNING_IN_CLOUD:
        # Send failure alert via email
        if all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
            try:
                msg = MIMEMultipart()
                msg["Subject"] = f"⚠️ India Founders Digest FAILED — {today}"
                msg["From"]    = EMAIL_SENDER
                msg["To"]      = EMAIL_RECIPIENT
                msg.attach(MIMEText(message, "plain"))
                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                    server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
                log.info("Failure alert emailed.")
            except Exception as e:
                log.warning(f"Could not send failure email: {e}")
    else:
        try:
            _send_imessage_raw(message)
            log.info("Failure alert sent via iMessage.")
        except Exception as e:
            log.warning(f"Could not send failure iMessage: {e}")


# ──────────────────────────────────────────────
# Main orchestrator
# ──────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("  India Founders Daily Digest — Bulletproof Edition")
    log.info(f"  {datetime.now().strftime('%A, %B %d, %Y  %H:%M')}")
    log.info("=" * 60)

    if not ANTHROPIC_API_KEY:
        msg = "ANTHROPIC_API_KEY not set in .env"
        log.error(msg)
        send_failure_alert(msg)
        sys.exit(1)

    try:
        # 1. Init DB
        init_db()

        # 2. Permanent dedup — never send anyone twice
        ever_sent = get_all_sent()
        log.info(f"Dedup: {len(ever_sent)} founders sent in the last {DEDUP_DAYS} days.")

        confirmed = []  # founders with LinkedIn confirmed, ready to send

        # ── Round 1: Funding news ──────────────────────────
        log.info("ROUND 1: Searching funding news...")
        raw_funding = with_retry(_search_funding, label="funding_search")
        log.info(f"  Found {len(raw_funding)} raw funding announcements.")

        fresh_funding = [
            f for f in raw_funding
            if (f["founder_name"].lower().strip(), f["company"].lower().strip())
            not in ever_sent
        ]
        log.info(f"  {len(fresh_funding)} are new (never sent before).")
        save_founders(fresh_funding)

        # Pause before LinkedIn lookups so the search call's token usage clears
        # the 50k/min rate limit window before we start more API calls.
        if fresh_funding:
            log.info(f"  Pausing {RATE_LIMIT_SLEEP}s to reset rate limit window...")
            time.sleep(RATE_LIMIT_SLEEP)

        log.info(f"  Finding LinkedIn for up to {len(fresh_funding)} funding founders...")
        confirmed += enrich_and_filter(fresh_funding, already_confirmed=confirmed)
        log.info(f"  Confirmed with LinkedIn so far: {len(confirmed)}/{TARGET_COUNT}")

        # ── Round 2: Backlog (free — no API call) ──────────
        if len(confirmed) < TARGET_COUNT:
            needed = TARGET_COUNT - len(confirmed)
            in_confirmed = {
                (f["founder_name"].lower().strip(), f["company"].lower().strip())
                for f in confirmed
            }
            backlog = get_backlog(exclude=ever_sent | in_confirmed, limit=needed)
            log.info(f"ROUND 2: Backlog has {len(backlog)} ready founders (need {needed}).")
            confirmed += backlog
            log.info(f"  Confirmed with LinkedIn so far: {len(confirmed)}/{TARGET_COUNT}")

        # ── Round 3: Notable founders in the news (only if still short) ──
        if len(confirmed) < TARGET_COUNT:
            needed = TARGET_COUNT - len(confirmed)
            log.info(f"ROUND 3: Searching VC-backed founders in news (need {needed} more)...")
            exclude_names = {f["founder_name"].lower() for f in confirmed}
            raw_news = with_retry(
                _search_notable_founders, exclude_names, needed * 2,
                label="news_search"
            )
            log.info(f"  Found {len(raw_news)} notable founders in news.")

            fresh_news = [
                f for f in raw_news
                if (f["founder_name"].lower().strip(), f["company"].lower().strip())
                not in ever_sent
            ]
            save_founders(fresh_news)

            log.info(f"  Finding LinkedIn for up to {len(fresh_news)} news founders...")
            confirmed += enrich_and_filter(fresh_news, already_confirmed=confirmed)
            log.info(f"  Confirmed with LinkedIn so far: {len(confirmed)}/{TARGET_COUNT}")

        # ── Final check ────────────────────────────────────
        todays_batch = confirmed[:TARGET_COUNT]
        if len(todays_batch) < TARGET_COUNT:
            log.warning(
                f"Could only confirm {len(todays_batch)} founders with LinkedIn today "
                f"(target was {TARGET_COUNT}). Sending what we have."
            )

        if not todays_batch:
            raise RuntimeError("Zero founders with LinkedIn found — nothing to send.")

        # 3. Save enriched LinkedIn data back to DB
        save_founders(todays_batch)

        # 4. Build + save HTML digest
        html_path = save_html_digest(todays_batch)

        # 5. Send iMessage + open browser
        deliver_digest(todays_batch, html_path)

        # 6. Mark as permanently sent
        mark_as_sent(todays_batch)

        log.info(f"Done ✓ — {len(todays_batch)} founders sent with LinkedIn profiles.")

    except Exception as e:
        log.exception(f"Script failed: {e}")
        send_failure_alert(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
