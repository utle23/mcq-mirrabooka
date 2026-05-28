"""Email notification service for MCQ Mirrabooka.

Uses Brevo (formerly Sendinblue) HTTP API instead of SMTP, because:
- PythonAnywhere FREE tier blocks outbound SMTP (port 587/465) to Gmail etc.
- Brevo's API host (api.brevo.com) IS on PythonAnywhere's whitelist.
- HTTP POST to Brevo over port 443 works on every host.
- 300 emails / day free, no credit card needed.

Design goals:
- Never crash a request: every send runs in a daemon thread, wrapped in try/except.
- Off by default: if API key not configured or globally disabled, all send() calls are no-ops.
- Granular: each recipient picks which event types they receive.
- Self-contained: stdlib only (urllib + json + ssl + threading).
"""
from __future__ import annotations

import json
import sqlite3
import ssl
import threading
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from html import escape

DB_PATH: str | None = None

BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'

# All event types known to the system. Keep keys stable — they are stored in DB columns.
EVENT_TYPES = [
    ('checklist',   'Daily Checklist',        'fa-clipboard-check', '#2E7D32'),
    ('temperature', 'Temperature Record',     'fa-temperature-half', '#D84315'),
    ('violation',   'Staff Violation',        'fa-triangle-exclamation', '#C62828'),
    ('issue',       'Issue Report',           'fa-circle-exclamation', '#E65100'),
    ('prep',        'Weekly Prep Schedule',   'fa-calendar-week',  '#1565C0'),
    ('training',    'Training Assessment',    'fa-graduation-cap', '#6A1B9A'),
    ('pastry',      'Pastry Daily Check',     'fa-bread-slice',    '#FB8C00'),
    ('jobs',        'Job Schedule',           'fa-id-badge',       '#7B1FA2'),
]
VALID_EVENTS = {e[0] for e in EVENT_TYPES}

PREP_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
PREP_DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
PREP_STATIONS = {
    1: {'name_en': 'Banh Mi Station', 'name_vi': 'Khu banh mi', 'color': '#FF9800'},
    2: {'name_en': 'Pho / Kitchen Station', 'name_vi': 'Khu pho / bep chinh', 'color': '#F44336'},
    3: {'name_en': 'Drink Station', 'name_vi': 'Khu nuoc uong', 'color': '#00BCD4'},
    4: {'name_en': 'Chef / General Prep', 'name_vi': 'So che chung / phu bep', 'color': '#4CAF50'},
}


# ── DB setup ───────────────────────────────────────────────────────────────────

def init_email_tables(db_path: str) -> None:
    global DB_PATH
    DB_PATH = db_path
    with _conn() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS email_settings (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            smtp_host     TEXT NOT NULL DEFAULT 'smtp.gmail.com',
            smtp_port     INTEGER NOT NULL DEFAULT 587,
            smtp_user     TEXT NOT NULL DEFAULT '',
            smtp_password TEXT NOT NULL DEFAULT '',
            from_name     TEXT NOT NULL DEFAULT 'MCQ Mirrabooka Notification',
            base_url      TEXT NOT NULL DEFAULT '',
            enabled       INTEGER NOT NULL DEFAULT 0,
            updated_at    TEXT DEFAULT (datetime('now','localtime')),
            updated_by    TEXT DEFAULT ''
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS email_recipients (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            email               TEXT NOT NULL UNIQUE,
            name                TEXT NOT NULL DEFAULT '',
            active              INTEGER NOT NULL DEFAULT 1,
            notify_checklist    INTEGER NOT NULL DEFAULT 1,
            notify_temperature  INTEGER NOT NULL DEFAULT 1,
            notify_violation    INTEGER NOT NULL DEFAULT 1,
            notify_issue        INTEGER NOT NULL DEFAULT 1,
            notify_prep         INTEGER NOT NULL DEFAULT 1,
            notify_training     INTEGER NOT NULL DEFAULT 1,
            notify_pastry       INTEGER NOT NULL DEFAULT 1,
            notify_jobs         INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT DEFAULT (datetime('now','localtime'))
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS email_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at      TEXT DEFAULT (datetime('now','localtime')),
            event_type   TEXT NOT NULL,
            subject      TEXT NOT NULL,
            recipients   TEXT NOT NULL,
            status       TEXT NOT NULL,
            error_detail TEXT DEFAULT ''
        )''')
        # Migration: add Brevo-specific columns if missing.
        for col, ddl in [
            ('brevo_api_key', "ALTER TABLE email_settings ADD COLUMN brevo_api_key TEXT NOT NULL DEFAULT ''"),
            ('sender_email',  "ALTER TABLE email_settings ADD COLUMN sender_email  TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

        # Seed the singleton settings row if missing.
        conn.execute('INSERT OR IGNORE INTO email_settings (id) VALUES (1)')

        # Carry-over: if Brevo fields are empty but old SMTP fields had a value,
        # populate sender_email from smtp_user so the user keeps their sender address.
        row = conn.execute(
            'SELECT brevo_api_key, sender_email, smtp_user FROM email_settings WHERE id=1').fetchone()
        if row and not row['sender_email'] and row['smtp_user']:
            conn.execute(
                'UPDATE email_settings SET sender_email=? WHERE id=1',
                (row['smtp_user'],))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Settings helpers ──────────────────────────────────────────────────────────

def get_settings() -> dict:
    with _conn() as conn:
        row = conn.execute('SELECT * FROM email_settings WHERE id=1').fetchone()
        return dict(row) if row else {}


def update_settings(**kwargs) -> None:
    allowed = {'brevo_api_key', 'sender_email', 'from_name',
               'base_url', 'enabled', 'updated_by'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ', '.join(f'{k}=?' for k in fields)
    sets += ", updated_at=datetime('now','localtime')"
    with _conn() as conn:
        conn.execute(f'UPDATE email_settings SET {sets} WHERE id=1', list(fields.values()))


def list_recipients() -> list[dict]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM email_recipients ORDER BY active DESC, email').fetchall()]


def get_recent_log(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM email_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]


def add_recipient(email: str, name: str = '', events: dict | None = None) -> int:
    email = (email or '').strip().lower()
    if not email or '@' not in email:
        raise ValueError('Invalid email address.')
    flags = {f'notify_{e[0]}': 1 for e in EVENT_TYPES}
    flags['notify_jobs'] = 0
    if events:
        for k, v in events.items():
            if k in VALID_EVENTS:
                flags[f'notify_{k}'] = 1 if v else 0
    with _conn() as conn:
        cur = conn.execute('''INSERT INTO email_recipients
            (email, name, active,
             notify_checklist, notify_temperature, notify_violation, notify_issue,
             notify_prep, notify_training, notify_pastry, notify_jobs)
            VALUES (?,?,1,?,?,?,?,?,?,?,?)''',
            (email, name.strip(),
             flags['notify_checklist'], flags['notify_temperature'],
             flags['notify_violation'], flags['notify_issue'],
             flags['notify_prep'], flags['notify_training'],
             flags['notify_pastry'], flags['notify_jobs']))
        return cur.lastrowid


def update_recipient(rid: int, email: str, name: str, active: bool, events: dict) -> None:
    email = (email or '').strip().lower()
    if not email or '@' not in email:
        raise ValueError('Invalid email address.')
    cols = ['email=?', 'name=?', 'active=?']
    vals = [email, name.strip(), 1 if active else 0]
    for ev in VALID_EVENTS:
        cols.append(f'notify_{ev}=?')
        vals.append(1 if events.get(ev) else 0)
    vals.append(rid)
    with _conn() as conn:
        conn.execute(f'UPDATE email_recipients SET {", ".join(cols)} WHERE id=?', vals)


def delete_recipient(rid: int) -> None:
    with _conn() as conn:
        conn.execute('DELETE FROM email_recipients WHERE id=?', (rid,))


def toggle_recipient(rid: int) -> int:
    with _conn() as conn:
        row = conn.execute('SELECT active FROM email_recipients WHERE id=?', (rid,)).fetchone()
        if not row:
            return 0
        new_val = 0 if row['active'] else 1
        conn.execute('UPDATE email_recipients SET active=? WHERE id=?', (new_val, rid))
        return new_val


# ── Email rendering ───────────────────────────────────────────────────────────

def _darken(hex_color: str, factor: float = 0.82) -> str:
    try:
        c = hex_color.lstrip('#')
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        return f'#{max(0, int(r * factor)):02X}{max(0, int(g * factor)):02X}{max(0, int(b * factor)):02X}'
    except Exception:
        return hex_color


PEOPLE_KEYS = {
    'submitted by', 'recorded by', 'reported by', 'trainer',
    'verified by', 'approved by', 'checked by', 'received by',
    'staff', 'trainee', 'locked by', 'created by', 'assigned',
    'responsible', 'manager on duty', 'action responsible',
    'general done by',
}

PILL_KEYS = {'status': '#1565C0', 'severity': '#C62828', 'priority': '#E65100',
             'overall rating': '#6A1B9A', 'condition': '#E65100',
             'completion': '#2E7D32', 'late submission': '#C62828',
             'out-of-zone readings': '#C62828'}


def _logo_bar_html(brand_subtitle: str = '') -> str:
    """White header strip with the restaurant logo + brand name.
    The logo URL comes from settings.base_url + /static/logo.png so the email
    client can fetch it. If base_url is empty, falls back to a text-only header.
    """
    settings = get_settings()
    base_url = (settings.get('base_url') or '').rstrip('/')
    sub_html = (f'<div style="font-size:11px;color:#888;letter-spacing:.1em;'
                f'text-transform:uppercase;margin-top:2px">{escape(brand_subtitle)}</div>'
                if brand_subtitle else '')

    if base_url:
        logo_url = f'{base_url}/static/logo.png'
        logo_img = (f'<img src="{escape(logo_url)}" alt="MCQ Mirrabooka Cafe" '
                    f'width="54" height="54" style="display:block;border:0;'
                    f'border-radius:8px;background:#fff">')
    else:
        # Fallback: a small badge "MCQ" if we have no public URL.
        logo_img = ('<div style="width:54px;height:54px;background:#fff;'
                    'border-radius:8px;display:inline-flex;align-items:center;'
                    'justify-content:center;font-family:Arial Black,sans-serif;'
                    'font-weight:900;font-size:20px;letter-spacing:.5px;'
                    'color:#1A1A2E">MCQ</div>')

    return (
        f'<tr><td style="background:#ffffff;padding:14px 22px;'
        f'border-bottom:1px solid #eef0f3">'
        f'<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate">'
        f'<tr>'
        f'<td style="vertical-align:middle;padding-right:14px">{logo_img}</td>'
        f'<td style="vertical-align:middle">'
        f'<div style="font-family:Arial Black,sans-serif;font-size:17px;font-weight:900;'
        f'color:#1A1A2E;line-height:1.1">MCQ MIRRABOOKA <span style="color:#C0392B">CAFE</span></div>'
        f'<div style="font-size:10px;color:#888;letter-spacing:.15em;'
        f'text-transform:uppercase;margin-top:2px">Vietnamese Street Food</div>'
        f'{sub_html}'
        f'</td>'
        f'</tr></table>'
        f'</td></tr>'
    )


def _build_html(event_label: str, color: str, title: str, lines: list[str],
                link: str = '', actor: str = '') -> str:
    dark = _darken(color, 0.75)
    rows_html = []
    people_html = []

    for line in lines:
        if not line:
            continue
        if ':' in line:
            k, v = line.split(':', 1)
            k_clean = k.strip()
            v_clean = v.strip()
            k_lower = k_clean.lower()

            if not v_clean or v_clean == '-':
                continue

            if k_lower in PEOPLE_KEYS:
                people_html.append((k_clean, v_clean))
                continue

            if k_lower in PILL_KEYS:
                pill_color = PILL_KEYS[k_lower]
                rows_html.append(
                    f'<tr>'
                    f'<td style="padding:9px 14px;border-bottom:1px solid #eef0f3;color:#666;font-weight:600;width:40%;font-size:13px">'
                    f'{escape(k_clean)}</td>'
                    f'<td style="padding:9px 14px;border-bottom:1px solid #eef0f3">'
                    f'<span style="background:{pill_color};color:#fff;padding:3px 12px;border-radius:12px;'
                    f'font-size:12px;font-weight:700;display:inline-block">{escape(v_clean)}</span></td></tr>')
            else:
                rows_html.append(
                    f'<tr>'
                    f'<td style="padding:9px 14px;border-bottom:1px solid #eef0f3;color:#666;font-weight:600;width:40%;font-size:13px">'
                    f'{escape(k_clean)}</td>'
                    f'<td style="padding:9px 14px;border-bottom:1px solid #eef0f3;color:#222;font-size:14px">'
                    f'{escape(v_clean)}</td></tr>')
        else:
            rows_html.append(
                f'<tr><td colspan="2" style="padding:9px 14px;border-bottom:1px solid #eef0f3;'
                f'color:#222;font-size:13px;font-style:italic">{escape(line)}</td></tr>')

    rows = ''.join(rows_html)

    if actor and not any(p[1] == actor for p in people_html):
        people_html.insert(0, ('Submitted by', actor))

    people_block = ''
    if people_html:
        chips = []
        for role, name in people_html:
            initials = ''.join(w[0] for w in name.split()[:2]).upper()[:2] or '?'
            chips.append(
                f'<td style="padding:6px 8px;vertical-align:middle">'
                f'<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate">'
                f'<tr>'
                f'<td style="width:38px;height:38px;background:{color};color:#fff;border-radius:50%;'
                f'text-align:center;font-weight:700;font-size:14px;letter-spacing:.5px">{escape(initials)}</td>'
                f'<td style="padding-left:10px;vertical-align:middle">'
                f'<div style="font-size:10px;color:#999;text-transform:uppercase;letter-spacing:.08em;font-weight:600">{escape(role)}</div>'
                f'<div style="font-size:14px;color:#1A1A2E;font-weight:600;line-height:1.3">{escape(name)}</div>'
                f'</td></tr></table>'
                f'</td>')
        people_block = (
            f'<div style="margin-top:18px;padding:14px 16px;background:#f7f9fb;border-radius:8px;'
            f'border-left:3px solid {color}">'
            f'<div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.1em;'
            f'font-weight:700;margin-bottom:10px">People involved</div>'
            f'<table cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:separate">'
            f'<tr>{"".join(chips)}</tr></table>'
            f'</div>'
        )

    link_html = ''
    if link:
        link_html = (
            f'<div style="margin-top:22px;text-align:center">'
            f'<a href="{escape(link)}" style="display:inline-block;background:{color};color:#fff;'
            f'text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:700;font-size:14px;'
            f'box-shadow:0 2px 8px rgba(0,0,0,.12)">View full details in app  →</a>'
            f'</div>')

    logo_bar = _logo_bar_html()

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
</head>
<body style="margin:0;padding:0;background:#eef1f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
  <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#eef1f4">
    <tr><td align="center" style="padding:28px 12px">
      <table cellpadding="0" cellspacing="0" border="0" width="620" style="max-width:620px;background:#ffffff;
              border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(20,30,50,.08)">

        {logo_bar}

        <tr><td style="background:linear-gradient(135deg,{color} 0%,{dark} 100%);padding:22px 28px;color:#fff">
          <div style="font-size:12px;text-transform:uppercase;letter-spacing:.18em;opacity:.92;font-weight:600">
            {escape(event_label)}
          </div>
          <div style="font-size:20px;font-weight:700;margin-top:8px;line-height:1.35">{escape(title)}</div>
        </td></tr>

        <tr><td style="padding:22px 26px 8px">
          <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;
                  border:1px solid #eef0f3;border-radius:8px;overflow:hidden">
            {rows}
          </table>
          {people_block}
          {link_html}
        </td></tr>

        <tr><td style="padding:18px 26px 22px;background:#fafbfc;border-top:1px solid #eef0f3;
                color:#90969f;font-size:11px;text-align:center;line-height:1.6">
          <div style="font-weight:600;color:#5a6068">MCQ Mirrabooka Cafe management system</div>
          <div>Automatic notification · sent {datetime.now().strftime('%a %d %b %Y · %H:%M')}</div>
          <div style="margin-top:6px;opacity:.75">You're receiving this because an admin added your email
          to the notification list. Ask an admin to remove your address if you no longer want updates.</div>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>'''


def _build_text(event_label: str, title: str, lines: list[str], link: str, actor: str) -> str:
    body = [f'MCQ Mirrabooka — {event_label}', '', title, '-' * 40]
    body.extend(lines)
    if actor:
        body.append(f'\nSubmitted by: {actor}')
    if link:
        body.append(f'\nView: {link}')
    body.append(f'\nSent {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    return '\n'.join(body)


# ── Sending via Brevo HTTP API ────────────────────────────────────────────────

def _recipients_for_event(event_type: str) -> list[str]:
    if event_type not in VALID_EVENTS:
        return []
    col = f'notify_{event_type}'
    with _conn() as conn:
        rows = conn.execute(
            f'SELECT email FROM email_recipients WHERE active=1 AND {col}=1 ORDER BY email'
        ).fetchall()
    return [r['email'] for r in rows]


def _is_configured(settings: dict) -> bool:
    return bool(settings.get('brevo_api_key') and settings.get('sender_email'))


def _log(event_type: str, subject: str, recipients: list[str], status: str, error: str = '') -> None:
    try:
        with _conn() as conn:
            conn.execute('''INSERT INTO email_log
                (event_type, subject, recipients, status, error_detail)
                VALUES (?,?,?,?,?)''',
                (event_type, subject[:200], ', '.join(recipients)[:500], status, error[:1000]))
    except Exception:
        pass


def _brevo_post(payload: dict, api_key: str, timeout: int = 30) -> tuple[bool, str]:
    """POST one transactional email to Brevo. Returns (ok, message)."""
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        BREVO_API_URL,
        data=body,
        method='POST',
        headers={
            'accept': 'application/json',
            'content-type': 'application/json',
            'api-key': api_key,
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode('utf-8', 'replace')
            if 200 <= resp.status < 300:
                return True, data or 'OK'
            return False, f'HTTP {resp.status}: {data}'
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode('utf-8', 'replace')
        except Exception:
            detail = str(e)
        return False, f'HTTPError {e.code}: {detail}'
    except urllib.error.URLError as e:
        return False, f'URLError: {e.reason}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _send_sync(event_type: str, subject: str, lines: list[str],
               link_path: str, actor: str, recipients: list[str], settings: dict) -> None:
    """Send the notification — one POST per recipient.
    Sending separately keeps each recipient's email private (they don't see
    others in the To: field) and isolates failures so one bad address can't
    block the rest of the team from getting the email."""
    label_color = next(((lbl, color) for k, lbl, _, color in EVENT_TYPES if k == event_type),
                       (event_type.title(), '#1A1A2E'))
    label, color = label_color

    base_url = (settings.get('base_url') or '').rstrip('/')
    link = f'{base_url}{link_path}' if base_url and link_path else ''

    html_body = _build_html(label, color, subject, lines, link, actor)
    text_body = _build_text(label, subject, lines, link, actor)

    sent_to: list[str] = []
    failures: list[str] = []
    for recipient in recipients:
        payload = {
            'sender': {
                'name':  settings.get('from_name') or 'MCQ Mirrabooka',
                'email': settings['sender_email'],
            },
            'to':          [{'email': recipient}],
            'subject':     f'[MCQ] {subject}',
            'htmlContent': html_body,
            'textContent': text_body,
        }
        ok, msg = _brevo_post(payload, settings['brevo_api_key'])
        if ok:
            sent_to.append(recipient)
        else:
            failures.append(f'{recipient}: {msg}')

    if sent_to and not failures:
        _log(event_type, subject, sent_to, 'sent')
    elif sent_to and failures:
        _log(event_type, subject, recipients, 'partial',
             f'Sent to {len(sent_to)}, failed for {len(failures)}: ' + ' | '.join(failures))
    else:
        _log(event_type, subject, recipients, 'failed', ' | '.join(failures) or 'no recipients')


def _send_html_sync(event_type: str, subject: str, html_body: str, text_body: str,
                    recipients: list[str], settings: dict) -> None:
    """Send a pre-rendered HTML notification, one POST per recipient."""
    sent_to: list[str] = []
    failures: list[str] = []
    for recipient in recipients:
        payload = {
            'sender': {
                'name':  settings.get('from_name') or 'MCQ Mirrabooka',
                'email': settings['sender_email'],
            },
            'to':          [{'email': recipient}],
            'subject':     f'[MCQ] {subject}',
            'htmlContent': html_body,
            'textContent': text_body or subject,
        }
        ok, msg = _brevo_post(payload, settings['brevo_api_key'])
        if ok:
            sent_to.append(recipient)
        else:
            failures.append(f'{recipient}: {msg}')

    if sent_to and not failures:
        _log(event_type, subject, sent_to, 'sent')
    elif sent_to and failures:
        _log(event_type, subject, recipients, 'partial',
             f'Sent to {len(sent_to)}, failed for {len(failures)}: ' + ' | '.join(failures))
    else:
        _log(event_type, subject, recipients, 'failed', ' | '.join(failures) or 'no recipients')


def send_notification(event_type: str, subject: str, lines: list[str],
                       link_path: str = '', actor: str = '') -> None:
    """Fire-and-forget notification. Never raises."""
    try:
        if event_type not in VALID_EVENTS:
            return
        settings = get_settings()
        if not settings.get('enabled') or not _is_configured(settings):
            return
        recipients = _recipients_for_event(event_type)
        if not recipients:
            return
        t = threading.Thread(
            target=_send_sync,
            args=(event_type, subject, lines, link_path, actor, recipients, settings),
            daemon=True,
        )
        t.start()
    except Exception as e:
        try:
            _log(event_type, subject, [], 'queue_failed', f'{type(e).__name__}: {e}')
        except Exception:
            pass


def send_html_notification(event_type: str, subject: str, html_body: str,
                           text_body: str = '') -> None:
    """Fire-and-forget notification using already-rendered HTML. Never raises."""
    try:
        if event_type not in VALID_EVENTS:
            return
        settings = get_settings()
        if not settings.get('enabled') or not _is_configured(settings):
            return
        recipients = _recipients_for_event(event_type)
        if not recipients:
            return
        t = threading.Thread(
            target=_send_html_sync,
            args=(event_type, subject, html_body, text_body, recipients, settings),
            daemon=True,
        )
        t.start()
    except Exception as e:
        try:
            _log(event_type, subject, [], 'queue_failed', f'{type(e).__name__}: {e}')
        except Exception:
            pass


# ── Weekly prep email rendering ───────────────────────────────────────────────

def _prep_week_start(value: str | date | None = None) -> date:
    if isinstance(value, date):
        d = value
    elif isinstance(value, str) and value:
        try:
            d = datetime.strptime(value, '%Y-%m-%d').date()
        except Exception:
            d = date.today()
    else:
        d = date.today()
    return d - timedelta(days=d.weekday())


def _prep_fmt_time(value: str | None) -> str:
    if not value:
        return ''
    try:
        h, m = map(int, value.split(':'))
        return f'{h % 12 or 12}:{m:02d} {"AM" if h < 12 else "PM"}'
    except Exception:
        return value


def collect_weekly_prep(week_start: str | date | None = None) -> dict:
    """Collect one full weekly prep schedule. `week_start` can be any date in the week."""
    ws = _prep_week_start(week_start)
    week_start_str = ws.isoformat()
    week_dates = [(ws + timedelta(days=i)).isoformat() for i in range(7)]
    empty = {
        'week_start': week_start_str,
        'week_end': week_dates[-1],
        'week_dates': week_dates,
        'schedule': None,
        'tasks': [],
        'total_required': 0,
        'total_done': 0,
        'total_pending': 0,
        'total_issues': 0,
        'total_moved': 0,
        'day_stats': [],
        'station_stats': [],
    }

    try:
        with _conn() as conn:
            sched = conn.execute(
                'SELECT * FROM prep_weekly_schedules WHERE week_start=?',
                (week_start_str,)).fetchone()
            if not sched:
                return empty

            tasks = []
            for wt in conn.execute('''
                SELECT * FROM prep_weekly_tasks
                WHERE schedule_id=?
                ORDER BY station_id, scheduled_time, sort_order, id
            ''', (sched['id'],)).fetchall():
                task = dict(wt)
                task['station'] = PREP_STATIONS.get(task['station_id'], {
                    'name_en': f"Station {task['station_id']}",
                    'name_vi': '',
                    'color': '#607D8B',
                })
                task['fmt_time'] = _prep_fmt_time(task.get('scheduled_time'))
                rows = conn.execute('''
                    SELECT * FROM prep_daily_status
                    WHERE weekly_task_id=?
                    ORDER BY date
                ''', (task['id'],)).fetchall()
                task['days'] = {r['day_of_week']: dict(r) for r in rows}
                tasks.append(task)
    except sqlite3.OperationalError:
        return empty

    day_stats = []
    station_map: dict[int, dict] = {}
    total_required = total_done = total_pending = total_issues = total_moved = 0
    for idx, day in enumerate(PREP_DAYS):
        stat = {'day': day, 'label': PREP_DAY_LABELS[idx], 'date': week_dates[idx],
                'required': 0, 'done': 0, 'pending': 0, 'issues': 0}
        for task in tasks:
            ds = task['days'].get(day, {})
            st_id = task['station_id']
            if st_id not in station_map:
                station_map[st_id] = {
                    'station_id': st_id,
                    'station': task['station'],
                    'required': 0,
                    'done': 0,
                    'pending': 0,
                    'issues': 0,
                }
            if not ds.get('is_required'):
                continue
            if ds.get('status') == 'moved':
                total_moved += 1
                continue

            stat['required'] += 1
            station_map[st_id]['required'] += 1
            total_required += 1
            if ds.get('status') == 'done':
                stat['done'] += 1
                station_map[st_id]['done'] += 1
                total_done += 1
            else:
                stat['pending'] += 1
                station_map[st_id]['pending'] += 1
                total_pending += 1
            if ds.get('issue_flag'):
                stat['issues'] += 1
                station_map[st_id]['issues'] += 1
                total_issues += 1
        stat['pct'] = round(stat['done'] / max(stat['required'], 1) * 100)
        day_stats.append(stat)

    station_stats = sorted(station_map.values(), key=lambda r: r['station_id'])
    for row in station_stats:
        row['pct'] = round(row['done'] / max(row['required'], 1) * 100)

    return {
        **empty,
        'schedule': dict(sched),
        'tasks': tasks,
        'total_required': total_required,
        'total_done': total_done,
        'total_pending': total_pending,
        'total_issues': total_issues,
        'total_moved': total_moved,
        'day_stats': day_stats,
        'station_stats': station_stats,
    }


def _prep_badge(text: str, bg: str, color: str = '#fff') -> str:
    return (f'<span style="display:inline-block;background:{bg};color:{color};'
            f'font-size:10px;font-weight:800;padding:3px 7px;border-radius:10px;'
            f'line-height:1.1;white-space:nowrap">{escape(text)}</span>')


def _prep_cell_html(task: dict, ds: dict) -> str:
    if not ds or not ds.get('is_required'):
        return '<span style="color:#BDBDBD;font-weight:700">-</span>'
    note = (ds.get('note') or '').strip()
    note_html = (f'<div style="font-size:10px;color:#6D4C41;margin-top:3px;line-height:1.25">'
                 f'{escape(note[:80])}{"..." if len(note) > 80 else ""}</div>') if note else ''
    if ds.get('issue_flag'):
        return _prep_badge('ISSUE', '#C62828') + note_html
    if ds.get('status') == 'done':
        by = ds.get('done_by') or ''
        by_html = f'<div style="font-size:10px;color:#1B5E20;margin-top:2px">{escape(by)}</div>' if by else ''
        return _prep_badge('DONE', '#2E7D32') + by_html + note_html
    if ds.get('status') == 'moved':
        label = 'MOVED'
        if ds.get('moved_to_day'):
            label += f" -> {ds['moved_to_day'].upper()}"
        return _prep_badge(label, '#F57C00') + note_html
    if task.get('is_supplier'):
        return _prep_badge('ORDER', '#1565C0') + note_html
    return _prep_badge('PENDING', '#ECEFF1', '#455A64') + note_html


def build_prep_week_section_html(data: dict, base_url: str = '') -> str:
    if not data.get('schedule'):
        return ('<div style="padding:14px;background:#FFF8E1;border-left:3px solid #F9A825;'
                'border-radius:6px;color:#6D4C00;font-size:13px">'
                'No weekly prep schedule exists for this week yet.</div>')

    pct = round(data['total_done'] / max(data['total_required'], 1) * 100)
    cards = (
        _digest_kpi_card('Weekly prep', f"{data['total_done']} / {data['total_required']}",
                         '#1565C0', f'{pct}% complete') +
        _digest_kpi_card('Pending', str(data['total_pending']),
                         '#E65100', 'remaining tasks') +
        _digest_kpi_card('Issues', str(data['total_issues']),
                         '#C62828', 'flagged prep cells') +
        _digest_kpi_card('Moved', str(data['total_moved']),
                         '#7B1FA2', 'moved earlier')
    )

    day_rows = ''.join(
        f'<tr>'
        f'<td style="padding:7px;border-bottom:1px solid #eef0f3;font-size:11px;font-weight:700">'
        f'{escape(d["label"])}<div style="color:#888;font-size:10px;font-weight:400">{escape(d["date"])}</div></td>'
        f'<td style="padding:7px;border-bottom:1px solid #eef0f3;text-align:center;font-size:12px;font-weight:800;color:#1565C0">{d["done"]}/{d["required"]}</td>'
        f'<td style="padding:7px;border-bottom:1px solid #eef0f3;text-align:center;font-size:12px;color:#E65100">{d["pending"]}</td>'
        f'<td style="padding:7px;border-bottom:1px solid #eef0f3;text-align:center;font-size:12px;color:#C62828">{d["issues"]}</td>'
        f'</tr>'
        for d in data.get('day_stats', [])
    )
    day_html = (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-top:8px">'
        '<tr><th style="padding:7px;background:#E3F2FD;font-size:10px;text-align:left">Day</th>'
        '<th style="padding:7px;background:#E3F2FD;font-size:10px;text-align:center">Done</th>'
        '<th style="padding:7px;background:#E3F2FD;font-size:10px;text-align:center">Pending</th>'
        '<th style="padding:7px;background:#E3F2FD;font-size:10px;text-align:center">Issues</th></tr>'
        + day_rows + '</table>'
    )

    headers = (
        '<tr>'
        '<th style="padding:7px;background:#1A1A2E;color:#fff;font-size:10px;text-align:left;border:1px solid #dfe5eb">Time</th>'
        '<th style="padding:7px;background:#1A1A2E;color:#fff;font-size:10px;text-align:left;border:1px solid #dfe5eb">Task / Staff</th>'
        + ''.join(
            f'<th style="padding:7px;background:#1A1A2E;color:#fff;font-size:10px;text-align:center;border:1px solid #dfe5eb">'
            f'{escape(PREP_DAY_LABELS[i])}<div style="font-size:9px;font-weight:400;opacity:.75">{escape(data["week_dates"][i][5:])}</div></th>'
            for i in range(7)
        ) + '</tr>'
    )

    rows = []
    current_station = None
    for task in data.get('tasks', []):
        station = task.get('station') or {}
        if task.get('station_id') != current_station:
            current_station = task.get('station_id')
            rows.append(
                f'<tr><td colspan="9" style="padding:8px 10px;background:{station.get("color", "#607D8B")}18;'
                f'color:{station.get("color", "#607D8B")};font-size:12px;font-weight:800;border:1px solid #dfe5eb">'
                f'{escape(station.get("name_en") or "Station")}'
                f'<span style="font-weight:400;color:#666;margin-left:8px">{escape(station.get("name_vi") or "")}</span>'
                f'</td></tr>'
            )
        supplier = ''
        if task.get('is_supplier'):
            supplier = f'<div style="margin-top:3px">{_prep_badge("SUPPLIER: " + (task.get("supplier_name") or "Supplier"), "#1565C0")}</div>'
        rows.append(
            '<tr>'
            f'<td style="padding:7px;border:1px solid #dfe5eb;font-size:11px;font-weight:700;color:#1B4332;white-space:nowrap">{escape(task.get("fmt_time") or "-")}</td>'
            f'<td style="padding:7px;border:1px solid #dfe5eb;font-size:11px;line-height:1.3">'
            f'<div style="font-weight:800;color:#1A1A2E">{escape(task.get("task_name_en") or "")}</div>'
            f'<div style="color:#777;font-size:10px">{escape(task.get("task_name_vi") or "")}</div>'
            f'<div style="color:#1565C0;font-size:10px;margin-top:2px">Staff: {escape(task.get("assigned_to") or "-")}</div>'
            f'{supplier}</td>'
            + ''.join(
                f'<td style="padding:6px;border:1px solid #dfe5eb;text-align:center;vertical-align:middle;font-size:11px">'
                f'{_prep_cell_html(task, task.get("days", {}).get(day, {}))}</td>'
                for day in PREP_DAYS
            )
            + '</tr>'
        )

    schedule_table = (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-top:14px">'
        + headers + ''.join(rows) + '</table>'
    )

    link_html = ''
    if base_url:
        link_html = (
            f'<div style="margin-top:14px;text-align:center">'
            f'<a href="{escape(base_url.rstrip("/"))}/prep/weekly/{escape(data["week_start"])}" '
            f'style="display:inline-block;background:#1565C0;color:#fff;text-decoration:none;'
            f'padding:10px 22px;border-radius:8px;font-weight:700;font-size:13px">Open weekly prep in app</a>'
            f'</div>')

    return (
        f'<div style="font-size:12px;color:#666;margin-bottom:8px">'
        f'Week {escape(data["week_start"])} to {escape(data["week_end"])}'
        f' - {len(data.get("tasks", []))} tasks'
        f' - {"LOCKED" if data.get("schedule", {}).get("locked") else "ACTIVE"}'
        f'</div>'
        f'<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>{cards}</tr></table>'
        f'{day_html}{schedule_table}{link_html}'
    )


def build_prep_weekly_email_html(data: dict, subject: str, actor: str = '',
                                 changed_count: int = 0, base_url: str = '') -> str:
    actor_html = f'<div style="font-size:12px;opacity:.8;margin-top:4px">Saved by {escape(actor)}</div>' if actor else ''
    changed_html = ''
    if changed_count:
        changed_html = (
            f'<div style="margin-top:14px;background:#E8F5E9;border-left:3px solid #2E7D32;'
            f'padding:10px 14px;border-radius:6px;color:#1B5E20;font-size:13px">'
            f'{changed_count} prep status change(s) were saved. The full week schedule is below.</div>'
        )
    logo_bar = _logo_bar_html('Weekly Prep Schedule')
    section_html = build_prep_week_section_html(data, base_url=base_url)
    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(subject)}</title></head>
<body style="margin:0;padding:0;background:#eef1f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#eef1f4">
<tr><td align="center" style="padding:24px 10px">
<table cellpadding="0" cellspacing="0" border="0" width="960" style="max-width:960px;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(20,30,50,.08)">
  {logo_bar}
  <tr><td style="background:linear-gradient(135deg,#1565C0 0%,#0D47A1 100%);padding:24px 30px;color:#fff">
    <div style="font-size:12px;text-transform:uppercase;letter-spacing:.18em;opacity:.9;font-weight:700">Weekly Prep Schedule</div>
    <div style="font-size:23px;font-weight:800;margin-top:7px;line-height:1.25">{escape(subject)}</div>
    {actor_html}
  </td></tr>
  <tr><td style="padding:20px 22px 24px">
    {changed_html}
    {section_html}
  </td></tr>
  <tr><td style="padding:16px 26px 20px;background:#fafbfc;border-top:1px solid #eef0f3;color:#90969f;font-size:11px;text-align:center;line-height:1.6">
    <div style="font-weight:600;color:#5a6068">MCQ Mirrabooka Cafe management system</div>
    <div>Automatic weekly prep email - generated {datetime.now().strftime('%a %d %b %Y - %H:%M')}</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>'''


def build_prep_weekly_text(data: dict, actor: str = '', changed_count: int = 0) -> str:
    lines = [
        'MCQ Mirrabooka - Weekly Prep Schedule',
        f"Week: {data.get('week_start')} to {data.get('week_end')}",
        f"Saved by: {actor or '-'}",
        f"Changes saved: {changed_count}",
        f"Done: {data.get('total_done', 0)} / {data.get('total_required', 0)}",
        f"Pending: {data.get('total_pending', 0)}",
        f"Issues: {data.get('total_issues', 0)}",
        '',
    ]
    for day in data.get('day_stats', []):
        lines.append(
            f"{day['label']} {day['date']}: {day['done']}/{day['required']} done, "
            f"{day['pending']} pending, {day['issues']} issues"
        )
    return '\n'.join(lines)


def send_prep_weekly_schedule(week_start: str | date, subject: str | None = None,
                              actor: str = '', changed_count: int = 0) -> None:
    """Send the full weekly prep schedule to recipients subscribed to prep."""
    data = collect_weekly_prep(week_start)
    settings = get_settings()
    base_url = settings.get('base_url') or ''
    subject = subject or f"Weekly prep schedule - {data['week_start']} to {data['week_end']}"
    html = build_prep_weekly_email_html(
        data, subject=subject, actor=actor, changed_count=changed_count, base_url=base_url)
    text = build_prep_weekly_text(data, actor=actor, changed_count=changed_count)
    send_html_notification('prep', subject, html, text)


# ── Daily digest ──────────────────────────────────────────────────────────────

import secrets


def get_or_create_digest_token() -> str:
    """Return the secret token used to authenticate the public digest cron URL.
    Creates one on first use."""
    with _conn() as conn:
        # Reuse the email_log table's existence as a "table ready" signal; ensure
        # a tiny key/value table for misc app config exists.
        conn.execute('''CREATE TABLE IF NOT EXISTS app_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )''')
        row = conn.execute('SELECT value FROM app_config WHERE key=?', ('digest_token',)).fetchone()
        if row and row['value']:
            return row['value']
        new_tok = secrets.token_urlsafe(24)
        conn.execute('INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)',
                     ('digest_token', new_tok))
        return new_tok


def regenerate_digest_token() -> str:
    new_tok = secrets.token_urlsafe(24)
    with _conn() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')''')
        conn.execute('INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)',
                     ('digest_token', new_tok))
    return new_tok


def collect_daily_digest(target_date: str, checklists_meta: dict | None = None,
                          temperatures_meta: dict | None = None,
                          issue_categories: dict | None = None) -> dict:
    """Aggregate everything that happened on `target_date` (YYYY-MM-DD).

    `*_meta` dicts are the same CHECKLISTS / TEMPERATURES / ISSUE_CATEGORIES
    constants from app.py — passed in so this module doesn't depend on app.
    """
    checklists_meta   = checklists_meta or {}
    temperatures_meta = temperatures_meta or {}
    issue_categories  = issue_categories or {}

    with _conn() as conn:
        # ── Checklists: per (type, section) ──
        chk_rows = [dict(r) for r in conn.execute('''
            SELECT cs.*,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id) as total_tasks,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id AND done=1) as done_tasks,
                   (SELECT COUNT(*) FROM checklist_photos WHERE session_id=cs.id) as photo_count
            FROM checklist_sessions cs WHERE cs.date=? ORDER BY cs.type, cs.section
        ''', (target_date,)).fetchall()]

        # Build matrix: which (type, section) pairs got done, which didn't.
        checklist_matrix = []
        for chk_key, chk in checklists_meta.items():
            row = {
                'key':   chk_key,
                'title': chk.get('title', chk_key),
                'color': chk.get('color', '#888'),
                'opening': None,
                'closing': None,
            }
            for r in chk_rows:
                if r['type'] == chk_key:
                    row[r['section']] = r
            checklist_matrix.append(row)

        chk_total_done = sum(1 for r in chk_rows)
        chk_total_late = sum(1 for r in chk_rows if r.get('is_late'))
        chk_verified   = sum(1 for r in chk_rows if r.get('verified'))
        # Expected = 2 sections per checklist type
        chk_expected   = 2 * len(checklists_meta) if checklists_meta else 0

        # ── Temperatures ──
        temp_rows = [dict(r) for r in conn.execute('''
            SELECT ts.*,
                   (SELECT COUNT(*) FROM temp_readings WHERE session_id=ts.id) AS reading_count,
                   (SELECT COUNT(*) FROM temp_readings tr WHERE tr.session_id=ts.id AND tr.discarded='Y') AS discarded
            FROM temp_sessions ts WHERE ts.date=? ORDER BY ts.type
        ''', (target_date,)).fetchall()]

        # Out-of-zone readings
        oos = [dict(r) for r in conn.execute('''
            SELECT ts.type as temp_type, tr.food_name,
                   tr.c1_temp, tr.c2_temp, tr.c3_temp, tr.c4_temp, tr.c5_temp
            FROM temp_readings tr JOIN temp_sessions ts ON ts.id=tr.session_id
            WHERE ts.date=?''', (target_date,)).fetchall()]
        oos_flagged = []
        for r in oos:
            bad = []
            for n in range(1, 6):
                v = r.get(f'c{n}_temp')
                if v is not None and (v < 5 or v > 60):
                    bad.append(f'{v}°C')
            if bad:
                oos_flagged.append({
                    'food': r['food_name'],
                    'type': temperatures_meta.get(r['temp_type'], {}).get('title', r['temp_type']),
                    'readings': bad,
                })

        temp_matrix = []
        for t_key, t_meta in temperatures_meta.items():
            row = {'key': t_key, 'title': t_meta.get('title', t_key),
                   'color': t_meta.get('color', '#888'), 'session': None}
            for r in temp_rows:
                if r['type'] == t_key:
                    row['session'] = r
            temp_matrix.append(row)

        # ── Issues reported today ──
        issues_today = [dict(r) for r in conn.execute(
            'SELECT * FROM issue_reports WHERE date=? ORDER BY priority DESC, id DESC',
            (target_date,)).fetchall()]
        for it in issues_today:
            it['category_label'] = issue_categories.get(it['category'], {}).get('label', it['category'])

        # ── Violations logged today ──
        violations_today = [dict(r) for r in conn.execute('''
            SELECT sv.*, vr.title as rule_title, vr.category as rule_category
            FROM staff_violations sv LEFT JOIN violation_rules vr ON vr.id = sv.rule_id
            WHERE sv.incident_date=? ORDER BY sv.severity DESC, sv.id DESC
        ''', (target_date,)).fetchall()]

        # ── Training sessions today ──
        training_today = []
        try:
            training_today = [dict(r) for r in conn.execute('''
                SELECT ts.*,
                       (SELECT COUNT(*) FROM training_session_items WHERE session_id=ts.id AND status='achieved') AS achieved,
                       (SELECT COUNT(*) FROM training_session_items WHERE session_id=ts.id AND status='needs_practice') AS practice
                FROM training_sessions ts WHERE ts.session_date=? ORDER BY ts.id DESC
            ''', (target_date,)).fetchall()]
        except sqlite3.OperationalError:
            pass

        # ── Pastry deliveries (any condition != 'good') ──
        pastry_alerts = []
        try:
            pastry_alerts = [dict(r) for r in conn.execute('''
                SELECT pd.*, pi.name as item_name
                FROM pastry_delivery pd LEFT JOIN pastry_items pi ON pi.id=pd.item_id
                WHERE pd.date=? AND pd.condition NOT IN ('', 'good')
                ORDER BY pd.id DESC
            ''', (target_date,)).fetchall()]
        except sqlite3.OperationalError:
            pass

    prep_week = collect_weekly_prep(target_date)

    return {
        'date': target_date,
        'checklist_matrix': checklist_matrix,
        'chk_total_done':   chk_total_done,
        'chk_total_late':   chk_total_late,
        'chk_verified':     chk_verified,
        'chk_expected':     chk_expected,
        'temp_matrix':      temp_matrix,
        'temp_count':       len(temp_rows),
        'temp_expected':    len(temperatures_meta) if temperatures_meta else 0,
        'oos_flagged':      oos_flagged,
        'issues':           issues_today,
        'violations':       violations_today,
        'training':         training_today,
        'pastry_alerts':    pastry_alerts,
        'prep_week':        prep_week,
    }


def _digest_kpi_card(label: str, value: str, color: str, sublabel: str = '') -> str:
    sub = f'<div style="font-size:10px;color:#999;margin-top:2px">{escape(sublabel)}</div>' if sublabel else ''
    return (
        f'<td style="padding:6px;width:25%;vertical-align:top">'
        f'<div style="background:#fff;border:1px solid #eef0f3;border-radius:8px;'
        f'padding:14px;text-align:center;border-top:3px solid {color}">'
        f'<div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.1em;font-weight:600">{escape(label)}</div>'
        f'<div style="font-size:22px;font-weight:800;color:{color};margin-top:4px">{escape(value)}</div>'
        f'{sub}'
        f'</div></td>')


def _digest_section_html(title: str, color: str, icon_emoji: str, body_html: str) -> str:
    return (
        f'<div style="margin-top:22px">'
        f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#666;font-weight:700;margin-bottom:8px">'
        f'<span style="display:inline-block;width:8px;height:8px;background:{color};border-radius:50%;margin-right:8px;vertical-align:middle"></span>'
        f'{icon_emoji} {escape(title)}'
        f'</div>'
        f'{body_html}'
        f'</div>')


def build_digest_html(data: dict, base_url: str = '') -> str:
    """Render the day's data dict into a polished HTML email."""
    date_str = data['date']
    try:
        date_pretty = datetime.strptime(date_str, '%Y-%m-%d').strftime('%a %d %b %Y')
    except Exception:
        date_pretty = date_str

    # Top KPIs
    chk_pct = round(data['chk_total_done'] / data['chk_expected'] * 100) if data['chk_expected'] else 0
    kpi_cards = (
        _digest_kpi_card('Checklists', f"{data['chk_total_done']} / {data['chk_expected']}",
                         '#2E7D32', f'{chk_pct}% complete') +
        _digest_kpi_card('Temperature Records', f"{data['temp_count']} / {data['temp_expected']}",
                         '#D84315', f'{len(data["oos_flagged"])} out-of-zone') +
        _digest_kpi_card('Issues Reported', str(len(data['issues'])),
                         '#E65100', f'{sum(1 for i in data["issues"] if i.get("status")=="open")} still open') +
        _digest_kpi_card('Violations', str(len(data['violations'])),
                         '#C62828', f'{len(data["training"])} training sessions')
    )

    # Checklist matrix
    chk_rows = []
    for row in data['checklist_matrix']:
        op = row.get('opening'); cl = row.get('closing')
        def _cell(sess):
            if not sess:
                return ('<td style="padding:8px;text-align:center;background:#fff;border:1px solid #eef0f3;'
                        'color:#bbb;font-size:12px">— missing</td>')
            badge = ''
            if sess.get('is_late'):
                badge = '<span style="background:#C62828;color:#fff;font-size:9px;padding:1px 6px;border-radius:8px;font-weight:700;margin-left:4px">LATE</span>'
            elif sess.get('verified'):
                badge = '<span style="background:#2E7D32;color:#fff;font-size:9px;padding:1px 6px;border-radius:8px;font-weight:700;margin-left:4px">✓ VERIFIED</span>'
            pct = round(sess['done_tasks'] / sess['total_tasks'] * 100) if sess.get('total_tasks') else 0
            return (f'<td style="padding:8px;background:#F1F8E9;border:1px solid #C5E1A5;color:#1B5E20;font-size:12px">'
                    f'<div style="font-weight:700">{pct}%{badge}</div>'
                    f'<div style="color:#666;font-size:11px;margin-top:2px">'
                    f'{escape(sess.get("submitted_by") or "—")}'
                    f'</div></td>')
        chk_rows.append(
            f'<tr>'
            f'<td style="padding:8px;background:#fafafa;border:1px solid #eef0f3;font-weight:700;color:#1A1A2E;font-size:12px">'
            f'<span style="display:inline-block;width:10px;height:10px;background:{row["color"]};border-radius:2px;margin-right:6px;vertical-align:middle"></span>'
            f'{escape(row["title"])}</td>'
            f'{_cell(op)}{_cell(cl)}'
            f'</tr>'
        )
    checklist_html = (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse">'
        '<tr>'
        '<th style="padding:8px;background:#1A1A2E;color:#fff;font-size:11px;text-align:left;border:1px solid #1A1A2E">Station</th>'
        '<th style="padding:8px;background:#1A1A2E;color:#fff;font-size:11px;text-align:center;border:1px solid #1A1A2E">Opening</th>'
        '<th style="padding:8px;background:#1A1A2E;color:#fff;font-size:11px;text-align:center;border:1px solid #1A1A2E">Closing</th>'
        '</tr>'
        + ''.join(chk_rows) +
        '</table>'
    )

    # Temperature
    temp_rows = []
    for row in data['temp_matrix']:
        sess = row.get('session')
        if sess:
            disc = sess.get('discarded') or 0
            label = f'{sess["reading_count"]} foods'
            if disc:
                label += f' · {disc} discarded'
            cell = (f'<td style="padding:8px;background:#F1F8E9;border:1px solid #C5E1A5;font-size:12px">'
                    f'<div style="font-weight:700;color:#1B5E20">✓ {escape(label)}</div>'
                    f'<div style="color:#666;font-size:11px;margin-top:2px">by {escape(sess.get("recorded_by") or "—")}</div>'
                    f'</td>')
        else:
            cell = ('<td style="padding:8px;text-align:center;background:#fff;border:1px solid #eef0f3;'
                    'color:#bbb;font-size:12px">— missing</td>')
        temp_rows.append(
            f'<tr>'
            f'<td style="padding:8px;background:#fafafa;border:1px solid #eef0f3;font-weight:700;color:#1A1A2E;font-size:12px">'
            f'<span style="display:inline-block;width:10px;height:10px;background:{row["color"]};border-radius:2px;margin-right:6px;vertical-align:middle"></span>'
            f'{escape(row["title"])}</td>'
            f'{cell}</tr>'
        )
    temp_html = (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse">'
        '<tr>'
        '<th style="padding:8px;background:#1A1A2E;color:#fff;font-size:11px;text-align:left;border:1px solid #1A1A2E">Station</th>'
        '<th style="padding:8px;background:#1A1A2E;color:#fff;font-size:11px;text-align:center;border:1px solid #1A1A2E">Status</th>'
        '</tr>' + ''.join(temp_rows) + '</table>'
    )

    # Out-of-zone temperature alerts
    oos_html = ''
    if data['oos_flagged']:
        oos_lines = ''.join(
            f'<li style="padding:4px 0;color:#B71C1C"><b>{escape(o["food"])}</b> ({escape(o["type"])}): '
            f'{escape(", ".join(o["readings"]))}</li>'
            for o in data['oos_flagged'][:20]
        )
        oos_html = (
            f'<div style="background:#FFEBEE;border-left:3px solid #C62828;padding:10px 14px;border-radius:6px;margin-top:8px">'
            f'<div style="font-size:11px;font-weight:700;color:#B71C1C;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">'
            f'⚠ Out-of-zone temperature readings ({len(data["oos_flagged"])})</div>'
            f'<ul style="margin:0;padding-left:18px;font-size:13px">{oos_lines}</ul>'
            f'</div>'
        )

    # Issues
    issues_html = ''
    if data['issues']:
        rows = []
        for it in data['issues']:
            pri_color = {'urgent': '#C62828', 'high': '#E65100', 'normal': '#1565C0', 'low': '#757575'}.get(
                (it.get('priority') or 'normal').lower(), '#1565C0')
            status_color = {'open': '#C62828', 'in_progress': '#E65100', 'resolved': '#2E7D32'}.get(
                it.get('status'), '#757575')
            rows.append(
                f'<tr>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px">'
                f'<span style="background:{pri_color};color:#fff;font-size:9px;font-weight:700;padding:2px 8px;border-radius:10px">{escape((it.get("priority") or "normal").upper())}</span></td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px">{escape(it.get("category_label") or "")}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px"><b>{escape(it.get("title") or "")}</b><div style="color:#666;font-size:11px">{escape((it.get("description") or "")[:120])}{"…" if len(it.get("description") or "") > 120 else ""}</div></td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px">{escape(it.get("reported_by") or "—")}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px"><span style="color:{status_color};font-weight:700">{escape((it.get("status") or "open").replace("_"," ").title())}</span></td>'
                f'</tr>'
            )
        issues_html = (
            '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;border:1px solid #eef0f3;border-radius:6px;overflow:hidden">'
            '<tr><th style="padding:8px;background:#FFF3E0;font-size:11px;text-align:left">Priority</th>'
            '<th style="padding:8px;background:#FFF3E0;font-size:11px;text-align:left">Category</th>'
            '<th style="padding:8px;background:#FFF3E0;font-size:11px;text-align:left">Issue</th>'
            '<th style="padding:8px;background:#FFF3E0;font-size:11px;text-align:left">Reporter</th>'
            '<th style="padding:8px;background:#FFF3E0;font-size:11px;text-align:left">Status</th></tr>'
            + ''.join(rows) + '</table>'
        )
    else:
        issues_html = '<div style="padding:14px;background:#F1F8E9;border-radius:6px;color:#1B5E20;font-size:13px">✓ No issues reported today.</div>'

    # Violations
    viol_html = ''
    if data['violations']:
        rows = []
        for v in data['violations']:
            sev_color = {'minor': '#1565C0', 'moderate': '#E65100',
                         'serious': '#C62828', 'critical': '#B71C1C'}.get(v.get('severity'), '#757575')
            rows.append(
                f'<tr>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px">'
                f'<span style="background:{sev_color};color:#fff;font-size:9px;font-weight:700;padding:2px 8px;border-radius:10px">{escape((v.get("severity") or "").upper())}</span></td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px"><b>{escape(v.get("staff_name") or "—")}</b></td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px">{escape(v.get("rule_title") or "—")}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px;color:#666">{escape((v.get("description") or "")[:100])}{"…" if len(v.get("description") or "") > 100 else ""}</td>'
                f'</tr>'
            )
        viol_html = (
            '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;border:1px solid #eef0f3;border-radius:6px;overflow:hidden">'
            '<tr><th style="padding:8px;background:#FFEBEE;font-size:11px;text-align:left">Severity</th>'
            '<th style="padding:8px;background:#FFEBEE;font-size:11px;text-align:left">Staff</th>'
            '<th style="padding:8px;background:#FFEBEE;font-size:11px;text-align:left">Rule</th>'
            '<th style="padding:8px;background:#FFEBEE;font-size:11px;text-align:left">Details</th></tr>'
            + ''.join(rows) + '</table>'
        )
    else:
        viol_html = '<div style="padding:14px;background:#F1F8E9;border-radius:6px;color:#1B5E20;font-size:13px">✓ No violations logged today.</div>'

    # Training
    training_html = ''
    if data['training']:
        rows = []
        for t in data['training']:
            rating_label = (t.get('overall_rating') or '').replace('_', ' ').title() or '—'
            rows.append(
                f'<tr>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px"><b>{escape(t.get("trainee_name") or "—")}</b></td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px">{escape(t.get("trainee_role") or "—")}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px;color:#666">by {escape(t.get("trainer_name") or "—")}</td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px"><span style="color:#2E7D32;font-weight:700">{t.get("achieved", 0)}</span> achieved · <span style="color:#E65100">{t.get("practice", 0)}</span> practice</td>'
                f'<td style="padding:8px;border-bottom:1px solid #eef0f3;font-size:12px">{escape(rating_label)}</td>'
                f'</tr>'
            )
        training_html = (
            '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;border:1px solid #eef0f3;border-radius:6px;overflow:hidden">'
            '<tr><th style="padding:8px;background:#F3E5F5;font-size:11px;text-align:left">Trainee</th>'
            '<th style="padding:8px;background:#F3E5F5;font-size:11px;text-align:left">Role</th>'
            '<th style="padding:8px;background:#F3E5F5;font-size:11px;text-align:left">Trainer</th>'
            '<th style="padding:8px;background:#F3E5F5;font-size:11px;text-align:left">Topics</th>'
            '<th style="padding:8px;background:#F3E5F5;font-size:11px;text-align:left">Rating</th></tr>'
            + ''.join(rows) + '</table>'
        )

    # Pastry delivery alerts
    pastry_html = ''
    if data['pastry_alerts']:
        items = ''.join(
            f'<li style="padding:4px 0"><b>{escape(p.get("item_name") or "—")}</b>: '
            f'condition <span style="background:#FB8C00;color:#fff;padding:1px 7px;border-radius:8px;font-weight:700;font-size:11px">{escape((p.get("condition") or "").upper())}</span>'
            f' — {escape(p.get("notes") or "no notes")}</li>'
            for p in data['pastry_alerts']
        )
        pastry_html = (
            '<div style="background:#FFF3E0;border-left:3px solid #FB8C00;padding:10px 14px;border-radius:6px">'
            '<ul style="margin:0;padding-left:18px;font-size:13px">' + items + '</ul></div>'
        )

    prep_html = build_prep_week_section_html(data.get('prep_week') or {}, base_url=base_url)

    # Link
    link_html = ''
    if base_url:
        link_html = (
            f'<div style="margin-top:26px;text-align:center">'
            f'<a href="{escape(base_url.rstrip("/"))}/" style="display:inline-block;background:#1A1A2E;color:#fff;'
            f'text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:700;font-size:14px">Open MCQ Web →</a>'
            f'</div>')

    sections = []
    sections.append(_digest_section_html('Daily Checklists', '#2E7D32', '📋', checklist_html))
    sections.append(_digest_section_html('Temperature Records', '#D84315', '🌡', temp_html + (f'<div style="margin-top:8px">{oos_html}</div>' if oos_html else '')))
    sections.append(_digest_section_html('Weekly Prep Schedule', '#1565C0', 'Prep', prep_html))
    sections.append(_digest_section_html('Issues Reported', '#E65100', '⚠', issues_html))
    sections.append(_digest_section_html('Staff Violations', '#C62828', '🚨', viol_html))
    if training_html:
        sections.append(_digest_section_html('Training Sessions', '#6A1B9A', '🎓', training_html))
    if pastry_html:
        sections.append(_digest_section_html('Pastry Delivery Alerts', '#FB8C00', '🍞', pastry_html))

    logo_bar = _logo_bar_html()

    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MCQ Daily Digest — {escape(date_pretty)}</title></head>
<body style="margin:0;padding:0;background:#eef1f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#eef1f4">
<tr><td align="center" style="padding:28px 12px">
<table cellpadding="0" cellspacing="0" border="0" width="680" style="max-width:680px;background:#ffffff;
        border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(20,30,50,.08)">

  {logo_bar}

  <tr><td style="background:linear-gradient(135deg,#1A1A2E 0%,#0D0D1A 100%);padding:24px 30px;color:#fff">
    <div style="font-size:12px;text-transform:uppercase;letter-spacing:.2em;opacity:.85;font-weight:600">
      Daily Operations Digest
    </div>
    <div style="font-size:22px;font-weight:700;margin-top:6px">{escape(date_pretty)}</div>
    <div style="font-size:13px;opacity:.7;margin-top:4px">Summary of every event from today</div>
  </td></tr>

  <tr><td style="padding:18px 22px">
    <table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>{kpi_cards}</tr></table>

    {''.join(sections)}

    {link_html}
  </td></tr>

  <tr><td style="padding:18px 28px 22px;background:#fafbfc;border-top:1px solid #eef0f3;
          color:#90969f;font-size:11px;text-align:center;line-height:1.6">
    <div style="font-weight:600;color:#5a6068">MCQ Mirrabooka Cafe management system</div>
    <div>Automatic daily digest · generated {datetime.now().strftime('%a %d %b %Y · %H:%M')}</div>
  </td></tr>

</table>
</td></tr></table>
</body></html>'''


def send_daily_digest(target_date: str, checklists_meta: dict,
                       temperatures_meta: dict, issue_categories: dict) -> tuple[bool, str]:
    """Build + send the digest synchronously. Returns (ok, message).
    Sends to recipients who have ANY event opt-in enabled."""
    settings = get_settings()
    if not _is_configured(settings):
        return False, 'Email service not configured.'
    # Anyone who would receive at least one event type also gets the digest.
    with _conn() as conn:
        rows = conn.execute('''SELECT email FROM email_recipients WHERE active=1 AND (
            notify_checklist=1 OR notify_temperature=1 OR notify_violation=1 OR
            notify_issue=1 OR notify_prep=1 OR notify_training=1 OR
            notify_pastry=1 OR notify_jobs=1) ORDER BY email''').fetchall()
    recipients = [r['email'] for r in rows]
    if not recipients:
        return False, 'No active recipients.'

    data = collect_daily_digest(target_date,
                                 checklists_meta=checklists_meta,
                                 temperatures_meta=temperatures_meta,
                                 issue_categories=issue_categories)
    html = build_digest_html(data, base_url=settings.get('base_url') or '')

    try:
        date_pretty = datetime.strptime(target_date, '%Y-%m-%d').strftime('%a %d %b %Y')
    except Exception:
        date_pretty = target_date

    text = (f'MCQ Mirrabooka — Daily Operations Digest\n'
            f'{date_pretty}\n\n'
            f'Checklists done: {data["chk_total_done"]} / {data["chk_expected"]} '
            f'(late: {data["chk_total_late"]}, verified: {data["chk_verified"]})\n'
            f'Temperatures done: {data["temp_count"]} / {data["temp_expected"]} '
            f'(out-of-zone readings: {len(data["oos_flagged"])})\n'
            f'Issues reported: {len(data["issues"])}\n'
            f'Violations logged: {len(data["violations"])}\n'
            f'Weekly prep: {data.get("prep_week", {}).get("total_done", 0)} / '
            f'{data.get("prep_week", {}).get("total_required", 0)} done '
            f'(pending: {data.get("prep_week", {}).get("total_pending", 0)}, '
            f'issues: {data.get("prep_week", {}).get("total_issues", 0)})\n'
            f'Training sessions: {len(data["training"])}\n'
            f'Pastry alerts: {len(data["pastry_alerts"])}\n\n'
            f'Open MCQ Web: {settings.get("base_url") or "(set base URL in Email Settings)"}\n')

    # Send one email per recipient so each person's address stays private
    # and a bad address doesn't abort the whole batch.
    sent_to: list[str] = []
    failures: list[str] = []
    for recipient in recipients:
        payload = {
            'sender':      {'name': settings.get('from_name') or 'MCQ Mirrabooka',
                            'email': settings['sender_email']},
            'to':          [{'email': recipient}],
            'subject':     f'[MCQ] Daily Digest — {date_pretty}',
            'htmlContent': html,
            'textContent': text,
        }
        ok, msg = _brevo_post(payload, settings['brevo_api_key'])
        if ok:
            sent_to.append(recipient)
        else:
            failures.append(f'{recipient}: {msg}')

    subj = f'Daily Digest — {date_pretty}'
    if sent_to and not failures:
        _log('digest', subj, sent_to, 'sent')
        return True, f'Digest sent to {len(sent_to)} recipient(s).'
    if sent_to and failures:
        _log('digest', subj, recipients, 'partial',
             f'Sent to {len(sent_to)}, failed for {len(failures)}: ' + ' | '.join(failures))
        return True, (f'Digest sent to {len(sent_to)} recipient(s), '
                      f'{len(failures)} failed: ' + ' | '.join(failures)[:200])
    _log('digest', subj, recipients, 'failed', ' | '.join(failures) or 'unknown')
    return False, ' | '.join(failures) or 'unknown error'


def send_test_email(to_email: str) -> tuple[bool, str]:
    """Synchronous test send. Returns (success, message)."""
    settings = get_settings()
    if not _is_configured(settings):
        return False, ('Brevo not configured yet. Fill in the API key and Sender Email first. '
                       'Get a free API key at https://app.brevo.com/settings/keys/api')
    to_email = (to_email or '').strip()
    if not to_email or '@' not in to_email:
        return False, 'Invalid recipient email.'

    html = _build_html(
        'Test Email', '#2E7D32', 'Brevo connection successful',
        ['Status: Configuration looks good',
         f'Sender: {settings["sender_email"]}',
         f'Provider: Brevo HTTP API'],
        link='', actor='')
    text = ('Test email from MCQ Mirrabooka management system.\n\n'
            'If you received this, Brevo is configured correctly and notifications will work.\n\n'
            f'Sent {datetime.now().strftime("%Y-%m-%d %H:%M")}')

    payload = {
        'sender': {
            'name':  settings.get('from_name') or 'MCQ Mirrabooka',
            'email': settings['sender_email'],
        },
        'to':          [{'email': to_email}],
        'subject':     '[MCQ] Email notification test',
        'htmlContent': html,
        'textContent': text,
    }

    ok, msg = _brevo_post(payload, settings['brevo_api_key'])
    if ok:
        _log('test', 'Email notification test', [to_email], 'sent')
        return True, f'Test email sent to {to_email}. Check the inbox (and Spam folder).'

    _log('test', 'Email notification test', [to_email], 'failed', msg)

    # Friendlier error explanations
    lower = msg.lower()
    if 'certificate' in lower or 'ssl' in lower:
        return False, ('SSL certificate error reaching Brevo. This typically only happens on dev '
                       'machines missing CA certificates — PythonAnywhere works fine. '
                       f'Details: {msg}')
    if 'unauthorized' in lower or '401' in msg or 'invalid api key' in lower:
        return False, ('Brevo rejected the API key. Make sure you copied the FULL key '
                       '(starts with "xkeysib-") from https://app.brevo.com/settings/keys/api')
    if 'sender' in lower and ('not' in lower or 'invalid' in lower):
        return False, (f'Brevo says the sender email "{settings["sender_email"]}" is not verified. '
                       'Verify it at https://app.brevo.com/senders/list before sending.')
    if 'urlerror' in lower or 'network' in lower or 'unreachable' in lower:
        return False, ('Network unreachable. If you are on PythonAnywhere free tier, '
                       'check that "api.brevo.com" appears on https://www.pythonanywhere.com/whitelist/')
    return False, msg
