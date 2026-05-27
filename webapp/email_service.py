"""Email notification service for MCQ Mirrabooka.

Design goals:
- Never crash a request: every send runs in a daemon thread, wrapped in try/except.
- Off by default: if SMTP not configured or globally disabled, all send() calls are no-ops.
- Granular: each recipient picks which event types they receive.
- Self-contained: one module, no external dependencies beyond Python stdlib.
"""
from __future__ import annotations

import smtplib
import sqlite3
import ssl
import threading
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from html import escape

DB_PATH: str | None = None

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
        # Seed the singleton settings row if missing.
        conn.execute('INSERT OR IGNORE INTO email_settings (id) VALUES (1)')


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
    allowed = {'smtp_host', 'smtp_port', 'smtp_user', 'smtp_password',
               'from_name', 'base_url', 'enabled', 'updated_by'}
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
    """Insert a new recipient. `events` overrides per-event opt-in (default all ON except jobs)."""
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


# ── Sending ───────────────────────────────────────────────────────────────────

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
    return bool(settings.get('smtp_host')
                and settings.get('smtp_port')
                and settings.get('smtp_user')
                and settings.get('smtp_password'))


def _log(event_type: str, subject: str, recipients: list[str], status: str, error: str = '') -> None:
    try:
        with _conn() as conn:
            conn.execute('''INSERT INTO email_log
                (event_type, subject, recipients, status, error_detail)
                VALUES (?,?,?,?,?)''',
                (event_type, subject[:200], ', '.join(recipients)[:500], status, error[:1000]))
    except Exception:
        pass   # logging must never crash


def _build_html(event_label: str, color: str, title: str, lines: list[str],
                link: str = '', actor: str = '') -> str:
    row_html = []
    for line in lines:
        if not line:
            continue
        if ':' in line:
            k, v = line.split(':', 1)
            row_html.append(
                f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;color:#555;font-weight:600;width:35%">{escape(k.strip())}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;color:#222">{escape(v.strip())}</td></tr>')
        else:
            row_html.append(
                f'<tr><td colspan="2" style="padding:6px 10px;border-bottom:1px solid #eee;color:#222">{escape(line)}</td></tr>')
    rows = ''.join(row_html)
    link_html = ''
    if link:
        link_html = (
            f'<div style="margin-top:18px;text-align:center">'
            f'<a href="{escape(link)}" style="display:inline-block;background:{color};color:#fff;'
            f'text-decoration:none;padding:10px 22px;border-radius:6px;font-weight:600">View in app →</a>'
            f'</div>')
    actor_html = ''
    if actor:
        actor_html = (f'<div style="font-size:12px;color:#888;margin-top:14px">'
                      f'Submitted by: <b>{escape(actor)}</b></div>')
    return f'''<!DOCTYPE html>
<html><body style="margin:0;background:#f4f6f8;font-family:Arial,Helvetica,sans-serif">
  <div style="max-width:600px;margin:24px auto;background:#fff;border-radius:10px;overflow:hidden;
              box-shadow:0 1px 8px rgba(0,0,0,.06)">
    <div style="background:{color};padding:16px 22px;color:#fff">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:.1em;opacity:.85">
        MCQ Mirrabooka  ·  {escape(event_label)}
      </div>
      <div style="font-size:18px;font-weight:700;margin-top:4px">{escape(title)}</div>
    </div>
    <div style="padding:18px 22px">
      <table style="width:100%;border-collapse:collapse;font-size:14px">{rows}</table>
      {actor_html}
      {link_html}
    </div>
    <div style="padding:12px 22px;background:#fafafa;font-size:11px;color:#888;text-align:center;
                border-top:1px solid #eee">
      Automatic notification from MCQ Mirrabooka Cafe management system<br/>
      Sent {datetime.now().strftime('%Y-%m-%d %H:%M')}
    </div>
  </div>
</body></html>'''


def _build_text(event_label: str, title: str, lines: list[str], link: str, actor: str) -> str:
    body = [f'MCQ Mirrabooka — {event_label}', '', title, '-' * 40]
    body.extend(lines)
    if actor:
        body.append(f'\nSubmitted by: {actor}')
    if link:
        body.append(f'\nView: {link}')
    body.append(f'\nSent {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    return '\n'.join(body)


def _send_sync(event_type: str, subject: str, lines: list[str],
               link_path: str, actor: str, recipients: list[str], settings: dict) -> None:
    label_color = next(((lbl, color) for k, lbl, _, color in EVENT_TYPES if k == event_type),
                       (event_type.title(), '#1A1A2E'))
    label, color = label_color

    base_url = (settings.get('base_url') or '').rstrip('/')
    link = f'{base_url}{link_path}' if base_url and link_path else ''

    html_body = _build_html(label, color, subject, lines, link, actor)
    text_body = _build_text(label, subject, lines, link, actor)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'[MCQ] {subject}'
    msg['From']    = formataddr((settings.get('from_name') or 'MCQ Mirrabooka', settings['smtp_user']))
    msg['To']      = ', '.join(recipients)
    msg['Date']    = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain='mcq-mirrabooka.local')
    msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    host = settings['smtp_host']
    port = int(settings['smtp_port'] or 587)

    try:
        if port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
                smtp.login(settings['smtp_user'], settings['smtp_password'])
                smtp.sendmail(settings['smtp_user'], recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(settings['smtp_user'], settings['smtp_password'])
                smtp.sendmail(settings['smtp_user'], recipients, msg.as_string())
        _log(event_type, subject, recipients, 'sent')
    except Exception as e:
        err = f'{type(e).__name__}: {e}\n{traceback.format_exc()}'
        _log(event_type, subject, recipients, 'failed', err)


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
        # Send asynchronously so the request returns immediately.
        t = threading.Thread(
            target=_send_sync,
            args=(event_type, subject, lines, link_path, actor, recipients, settings),
            daemon=True,
        )
        t.start()
    except Exception as e:
        # Last-resort safety net.
        try:
            _log(event_type, subject, [], 'queue_failed', f'{type(e).__name__}: {e}')
        except Exception:
            pass


def send_test_email(to_email: str) -> tuple[bool, str]:
    """Synchronous test send. Returns (success, message)."""
    settings = get_settings()
    if not _is_configured(settings):
        return False, 'SMTP not configured. Fill in host, port, sender email and app password first.'
    to_email = (to_email or '').strip()
    if not to_email or '@' not in to_email:
        return False, 'Invalid recipient email.'

    msg = MIMEMultipart('alternative')
    msg['Subject'] = '[MCQ] Email notification test'
    msg['From']    = formataddr((settings.get('from_name') or 'MCQ Mirrabooka', settings['smtp_user']))
    msg['To']      = to_email
    msg['Date']    = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain='mcq-mirrabooka.local')
    text = ('This is a test email from MCQ Mirrabooka management system.\n\n'
            'If you received this, SMTP is configured correctly.\n\n'
            f'Sent {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    html = _build_html('Test Email', '#2E7D32', 'SMTP test successful',
                       ['Status: Configuration looks good',
                        f'SMTP host: {settings["smtp_host"]}:{settings["smtp_port"]}',
                        f'Sender: {settings["smtp_user"]}'],
                       link='', actor='')
    msg.attach(MIMEText(text, 'plain', 'utf-8'))
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    host = settings['smtp_host']
    port = int(settings['smtp_port'] or 587)

    try:
        if port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
                smtp.login(settings['smtp_user'], settings['smtp_password'])
                smtp.sendmail(settings['smtp_user'], [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(settings['smtp_user'], settings['smtp_password'])
                smtp.sendmail(settings['smtp_user'], [to_email], msg.as_string())
        _log('test', 'Email notification test', [to_email], 'sent')
        return True, f'Test email sent to {to_email}. Check the inbox (and Spam folder).'
    except smtplib.SMTPAuthenticationError as e:
        msg = ('Authentication failed. For Gmail, you must use a 16-character App Password '
               '(not your normal Google password). Enable 2-step verification first, then '
               'generate an App Password at https://myaccount.google.com/apppasswords')
        _log('test', 'Email notification test', [to_email], 'failed', str(e))
        return False, msg
    except smtplib.SMTPConnectError as e:
        _log('test', 'Email notification test', [to_email], 'failed', str(e))
        return False, f'Could not connect to {host}:{port}. Check host/port. ({e})'
    except Exception as e:
        _log('test', 'Email notification test', [to_email], 'failed', f'{type(e).__name__}: {e}')
        return False, f'{type(e).__name__}: {e}'
