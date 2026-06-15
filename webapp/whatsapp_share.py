"""Daily WhatsApp share — composes a single PNG image summarising the day's
checklists (with photo thumbnails) and temperature records, ready to be
shared straight to a WhatsApp group via the browser's native share sheet.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO

from flask import Blueprint, render_template, request, send_file, redirect, url_for, session
from functools import wraps

whatsapp_bp = Blueprint('whatsapp', __name__, url_prefix='/whatsapp')

DB_PATH: str | None = None
STATIC_DIR: str | None = None
UPLOAD_DIR: str | None = None
CHECKLISTS_META: dict = {}
TEMPERATURES_META: dict = {}

SHARE_CUTOFF_HOUR = 16
SHARE_PERIODS = {
    'opening': {
        'label': 'Opening',
        'cover': 'OPENING OPERATIONS REPORT',
        'equipment_check': 'morning',
        'equipment_label': 'Morning Equipment Check',
        'includes_food_temps': True,
    },
    'closing': {
        'label': 'Closing',
        'cover': 'CLOSING OPERATIONS REPORT',
        'equipment_check': 'closing',
        'equipment_label': 'Closing Equipment Check',
        'includes_food_temps': False,
    },
}
OPENING_TEMPERATURE_TYPES = {'chef', 'pastry', 'banh_mi'}


def init_whatsapp(db_path: str, static_dir: str, upload_dir: str,
                  checklists: dict, temperatures: dict) -> None:
    global DB_PATH, STATIC_DIR, UPLOAD_DIR, CHECKLISTS_META, TEMPERATURES_META
    DB_PATH           = db_path
    STATIC_DIR        = static_dir
    UPLOAD_DIR        = upload_dir
    CHECKLISTS_META   = checklists
    TEMPERATURES_META = temperatures


def _login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get('logged_in'):
            return redirect(url_for('login_page'))
        return f(*a, **kw)
    return d


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ── Data collection ──────────────────────────────────────────────────────────

def _resolve_share_period(raw: str | None = None) -> str:
    value = (raw or '').strip().lower()
    if value in ('closing', 'close', 'pm', 'night', 'evening'):
        return 'closing'
    if value in ('opening', 'open', 'morning', 'am'):
        return 'opening'
    return 'closing' if datetime.now().hour >= SHARE_CUTOFF_HOUR else 'opening'


def _filter_equipment_for_period(equip: dict | None, period: str) -> dict | None:
    if not equip:
        return None
    selected_key = SHARE_PERIODS.get(period, SHARE_PERIODS['opening'])['equipment_check']
    selected_meta = None
    for check in equip.get('check_types') or []:
        if check.get('key') == selected_key:
            selected_meta = dict(check)
            break
    if not selected_meta:
        selected_meta = {'key': selected_key, 'label': selected_key.title(), 'short': selected_key.title()}

    due_keys = set(equip.get('due_check_keys') or [])
    units, recorded, alerts, missing, missing_due, recorded_by = [], 0, 0, 0, 0, ''
    for unit in equip.get('units') or []:
        reading = (unit.get('checks') or {}).get(selected_key) or {}
        temp = reading.get('temp')
        unsafe = bool(reading.get('unsafe'))
        if temp is None:
            missing += 1
            if selected_key in due_keys:
                missing_due += 1
        else:
            recorded += 1
            if unsafe:
                alerts += 1
            if reading.get('recorded_by'):
                recorded_by = reading.get('recorded_by')
        filtered_unit = dict(unit)
        filtered_unit['checks'] = {selected_key: reading}
        filtered_unit['temp'] = temp
        filtered_unit['unsafe'] = unsafe
        filtered_unit['status'] = 'alert' if unsafe else (
            'missing' if temp is None and selected_key in due_keys else
            'pending' if temp is None else 'ok'
        )
        units.append(filtered_unit)

    total = int(equip.get('total') or len(units))
    return {
        **equip,
        'units': units,
        'recorded': recorded,
        'alerts': alerts,
        'missing': missing,
        'missing_due': missing_due,
        'total': total,
        'total_checks': total,
        'total_due_checks': total if selected_key in due_keys else 0,
        'due_check_keys': [selected_key] if selected_key in due_keys else [],
        'check_types': [selected_meta],
        'period_check_key': selected_key,
        'period_check_label': selected_meta.get('label') or selected_key.title(),
        'recorded_by': recorded_by,
    }


def _collect_today(date_str: str, period: str | None = None) -> dict:
    """Pull today's checklist + temperature submissions with photo paths,
    plus equipment temperatures and any logged violations / reported issues."""
    period = _resolve_share_period(period)
    period_meta = SHARE_PERIODS[period]
    out = {'date': date_str, 'checklists': [], 'temperatures': [],
           'equipment': None, 'violations': [], 'issues': [], 'prep_timetable': {},
           'period': period, 'period_meta': period_meta}
    with _conn() as conn:
        # Checklists — one entry per (type, section) combination submitted today
        chk_rows = conn.execute('''
            SELECT cs.*,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id) AS total_tasks,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id AND done=1) AS done_tasks
            FROM checklist_sessions cs
            WHERE cs.date=? AND cs.section=?
            ORDER BY cs.type, cs.section
        ''', (date_str, period)).fetchall()

        for r in chk_rows:
            r = dict(r)
            meta = CHECKLISTS_META.get(r['type'], {})
            photos = [dict(p) for p in conn.execute(
                'SELECT filename, photo_number FROM checklist_photos '
                'WHERE session_id=? ORDER BY photo_number LIMIT 4',
                (r['id'],)).fetchall()]
            r['photos'] = photos
            r['meta']   = meta
            out['checklists'].append(r)

        # Temperature
        temp_rows = []
        if period_meta['includes_food_temps']:
            placeholders = ','.join('?' for _ in OPENING_TEMPERATURE_TYPES)
            temp_rows = conn.execute(f'''
                SELECT ts.*,
                       (SELECT COUNT(*) FROM temp_readings WHERE session_id=ts.id) AS reading_count,
                       (SELECT COUNT(*) FROM temp_readings WHERE session_id=ts.id AND discarded='Y') AS discarded
                FROM temp_sessions ts
                WHERE ts.date=? AND ts.type IN ({placeholders})
                ORDER BY ts.type
            ''', (date_str, *sorted(OPENING_TEMPERATURE_TYPES))).fetchall()
        for r in temp_rows:
            r = dict(r)
            meta = TEMPERATURES_META.get(r['type'], {})
            # Out-of-zone count uses food_kind: cold→unsafe if >5, hot→unsafe if <60
            reading_rows = conn.execute('''
                SELECT tr.food_name, tr.c1_temp, tr.c2_temp, tr.c3_temp, tr.c4_temp, tr.c5_temp,
                       COALESCE(tr.defrosted, 'N') AS defrosted,
                       COALESCE(ft.food_kind, 'cold') AS kind
                FROM temp_readings tr
                LEFT JOIN temp_food_templates ft
                  ON ft.temp_type = ? AND ft.food_name = tr.food_name
                WHERE tr.session_id = ?''', (r['type'], r['id'])).fetchall()
            bad = 0
            for rr in reading_rows:
                # Defrosting cold item — higher temp expected, not an alert.
                if (rr['defrosted'] or 'N').upper() == 'Y':
                    continue
                kind = rr['kind'] or 'cold'
                for idx, col in enumerate(('c1_temp', 'c2_temp', 'c3_temp', 'c4_temp', 'c5_temp'), start=1):
                    v = rr[col]
                    if v is None:
                        continue
                    # Pastry hot display: 3rd check is informational — no alert.
                    if r['type'] == 'pastry' and idx == 3:
                        continue
                    if _temp_unsafe(kind, v):
                        bad += 1
            r['out_of_zone'] = bad
            r['meta']        = meta
            out['temperatures'].append(r)

        # Equipment temperatures for the date (cold/freezer/hot units)
        try:
            import equipment_routes
            equipment_routes.DB_PATH = DB_PATH
            equip = equipment_routes.collect_equipment_for_date(conn, date_str)
            out['equipment'] = _filter_equipment_for_period(equip, period)
        except Exception:
            out['equipment'] = None

        # Prep timetable for the day — shared by the opening & closing reports.
        out['prep_timetable'] = _collect_prep_timetable(conn, date_str)

    return out


def _collect_prep_timetable(conn, date_str):
    """Per-station prep tasks for this weekday, linked to the weekly prep
    schedule's real done / not-done status. Same content for the AM & PM share
    (prep is one shared list per day, not split by period)."""
    empty = {'stations': [], 'total': 0, 'done': 0, 'not_done': 0, 'has_schedule': False}
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        d = datetime.now().date()
    day_key = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'][d.weekday()]
    week_start = (d - timedelta(days=d.weekday())).isoformat()

    try:
        stations_meta = {s['id']: {'name': s['name_en'], 'color': s['color'] or '#607D8B'}
                         for s in conn.execute(
                             'SELECT id, name_en, color FROM prep_stations ORDER BY id').fetchall()}
    except sqlite3.OperationalError:
        return empty

    by_station = {}
    total = done = 0
    has_schedule = False

    # 1) Prefer the actual weekly schedule → real per-task done status for the date.
    try:
        sched = conn.execute(
            'SELECT id FROM prep_weekly_schedules WHERE week_start=?', (week_start,)).fetchone()
        if sched:
            has_schedule = True
            rows = conn.execute('''
                SELECT wt.station_id AS sid, wt.task_name_en AS name,
                       COALESCE(ds.scheduled_time, wt.scheduled_time) AS time,
                       ds.status AS status, ds.done_by AS done_by, ds.note AS note
                FROM prep_daily_status ds
                JOIN prep_weekly_tasks wt ON wt.id = ds.weekly_task_id
                WHERE ds.date=? AND ds.is_required=1 AND COALESCE(wt.is_supplier,0)=0
                ORDER BY wt.station_id, wt.sort_order, wt.id''', (date_str,)).fetchall()
            for r in rows:
                is_done = (r['status'] == 'done')
                by_station.setdefault(r['sid'], []).append(
                    {'name': r['name'], 'time': r['time'] or '',
                     'done': is_done, 'done_by': r['done_by'] or '',
                     'reason': (r['note'] or '').strip()})
                total += 1
                if is_done:
                    done += 1
    except sqlite3.OperationalError:
        pass

    # 2) Fallback: no schedule built yet → recurring templates (all not-done).
    if not has_schedule:
        try:
            for t in conn.execute(
                'SELECT station_id AS sid, task_name_en AS name, default_time AS time, active_days '
                'FROM prep_task_templates WHERE active=1 ORDER BY station_id, sort_order, id').fetchall():
                days = (t['active_days'] or '').split(',') if t['active_days'] else []
                if day_key in days:
                    by_station.setdefault(t['sid'], []).append(
                        {'name': t['name'], 'time': t['time'] or '', 'done': False,
                         'done_by': '', 'reason': ''})
                    total += 1
        except sqlite3.OperationalError:
            pass

    stations = []
    for sid, meta in stations_meta.items():
        tasks = by_station.get(sid)
        if not tasks:
            continue
        stations.append({'station': meta['name'], 'color': meta['color'], 'tasks': tasks,
                         'done': sum(1 for t in tasks if t['done']), 'total': len(tasks)})

    return {'stations': stations, 'total': total, 'done': done,
            'not_done': total - done, 'has_schedule': has_schedule}


def _equipment_stats(equip: dict | None) -> dict:
    """Normalize equipment counts for share previews and exported reports."""
    equip = equip or {}
    total_units = int(equip.get('total') or 0)
    total_checks = equip.get('total_checks')
    if total_checks is None:
        check_count = len(equip.get('check_types') or []) or 2
        total_checks = total_units * check_count
    recorded = int(equip.get('recorded') or 0)
    alerts = int(equip.get('alerts') or 0)
    missing = equip.get('missing')
    if missing is None:
        missing = max(int(total_checks or 0) - recorded, 0)
    missing_due = equip.get('missing_due')
    if missing_due is None:
        missing_due = missing
    total_due_checks = equip.get('total_due_checks')
    if total_due_checks is None:
        total_due_checks = int(total_checks or 0)
    return {
        'total_units': total_units,
        'total_checks': int(total_checks or 0),
        'recorded': recorded,
        'alerts': alerts,
        'missing': int(missing or 0),
        'missing_due': int(missing_due or 0),
        'total_due_checks': int(total_due_checks or 0),
        'attention': alerts + int(missing_due or 0),
    }


# ── PNG composition (Pillow) ────────────────────────────────────────────────

def _font(size: int, bold: bool = False):
    """Try a few system fonts; fall back to PIL default if all are missing."""
    from PIL import ImageFont
    candidates = [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        '/Library/Fonts/Arial.ttf',
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _hex_to_rgb(s: str, fb=(96, 125, 139)) -> tuple:
    s = (s or '').lstrip('#')
    if len(s) != 6:
        return fb
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return fb


def _rounded(draw, xy, radius, fill, outline=None, width=1):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=fill,
                            outline=outline, width=width)


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines = []
    cur = ''
    for w in words:
        trial = (cur + ' ' + w).strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [text]


def build_daily_png(date_str: str, period: str | None = None) -> bytes:
    from PIL import Image, ImageDraw

    data = _collect_today(date_str, period)
    period_meta = data.get('period_meta') or SHARE_PERIODS['opening']

    # Canvas — 1080 width is the WhatsApp share sweet spot
    W = 1080
    PAD = 32
    THUMB = 220     # photo thumbnail size
    NAVY = (26, 26, 46)
    BRAND = (192, 57, 43)
    LIGHT_BG = (244, 246, 250)
    MUTED = (110, 117, 125)

    # ── First measure how tall we need ────────────────────────────────────
    f_title  = _font(46, bold=True)
    f_sub    = _font(22)
    f_h2     = _font(30, bold=True)
    f_body   = _font(22)
    f_body_b = _font(22, bold=True)
    f_small  = _font(18)
    f_chip   = _font(16, bold=True)

    # Header block
    h_header = 220
    # Summary KPI strip
    h_kpi = 160
    # Each checklist card
    h_chk_card_base = 260   # title + meta rows
    h_chk_photos    = THUMB + 24
    # Each temperature card
    h_temp_card     = 150
    # Footer
    h_footer = 100

    total_h = h_header + h_kpi
    # Each section has: 46px title space + (cards | 90px empty placeholder)
    total_h += 46
    if data['checklists']:
        for c in data['checklists']:
            total_h += h_chk_card_base + (h_chk_photos if c['photos'] else 0) + 24
    else:
        total_h += 90
    total_h += 46
    if data['temperatures']:
        for _ in data['temperatures']:
            total_h += h_temp_card + 16
    else:
        total_h += 90
    total_h += h_footer + 30

    img = Image.new('RGB', (W, total_h), LIGHT_BG)
    draw = ImageDraw.Draw(img)

    # ── HEADER (gradient-ish solid + brand) ───────────────────────────────
    draw.rectangle((0, 0, W, h_header), fill=NAVY)
    draw.rectangle((0, h_header - 6, W, h_header), fill=BRAND)

    logo_path = os.path.join(STATIC_DIR, 'logo.png') if STATIC_DIR else ''
    if os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert('RGBA')
            logo.thumbnail((130, 130), Image.LANCZOS)
            # White rounded bg behind logo
            lx, ly = PAD, 40
            _rounded(draw, (lx - 8, ly - 8, lx + logo.width + 8, ly + logo.height + 8),
                     radius=14, fill=(255, 255, 255))
            img.paste(logo, (lx, ly), logo)
        except Exception:
            pass

    # Date + branding text
    text_x = PAD + 160
    try:
        date_pretty = datetime.strptime(date_str, '%Y-%m-%d').strftime('%A, %d %b %Y').upper()
    except Exception:
        date_pretty = date_str
    draw.text((text_x, 36), 'MCQ MIRRABOOKA CAFE', fill=(255, 255, 255), font=f_title)
    draw.text((text_x, 96), period_meta['cover'],
              fill=(255, 255, 255, 200), font=f_sub)
    draw.text((text_x, 134), date_pretty, fill=(255, 200, 200), font=f_body_b)

    y = h_header + 24

    # ── KPI strip ─────────────────────────────────────────────────────────
    chk_done = sum(1 for c in data['checklists'] if c['done_tasks'] == c['total_tasks'] and c['total_tasks'] > 0)
    chk_late = sum(1 for c in data['checklists'] if c.get('is_late'))
    temp_done = len(data['temperatures'])
    temp_alerts = sum(c.get('out_of_zone', 0) for c in data['temperatures'])
    equip_stats = _equipment_stats(data.get('equipment'))
    total_attention = (temp_alerts + chk_late + equip_stats['attention']
                       + len(data.get("violations", [])))

    if data.get('period') == 'closing':
        kpi_cards = [
            ('CHECKLISTS', f'{len(data["checklists"])}', (46, 125, 50)),
            ('EQUIPMENT', f'{equip_stats["recorded"]}/{equip_stats["total_checks"]}', (0, 131, 143)),
            ('ALERTS', f'{total_attention}', (198, 40, 40)),
            ('REPORT', 'CLOSE', (26, 26, 46)),
        ]
    else:
        kpi_cards = [
            ('CHECKLISTS', f'{len(data["checklists"])}', (46, 125, 50)),
            ('FOOD TEMP', f'{temp_done}', (216, 67, 21)),
            ('EQUIPMENT', f'{equip_stats["recorded"]}/{equip_stats["total_checks"]}', (0, 131, 143)),
            ('ALERTS', f'{total_attention}', (198, 40, 40)),
        ]
    card_w = (W - PAD * 2 - 36) // 4
    for i, (label, val, col) in enumerate(kpi_cards):
        cx = PAD + i * (card_w + 12)
        _rounded(draw, (cx, y, cx + card_w, y + h_kpi - 12), radius=14,
                 fill=(255, 255, 255))
        draw.rectangle((cx, y, cx + card_w, y + 6), fill=col)
        draw.text((cx + 18, y + 24), label, fill=MUTED, font=f_chip)
        draw.text((cx + 18, y + 56), val, fill=col, font=_font(56, bold=True))
    y += h_kpi + 12

    # ── CHECKLISTS section ────────────────────────────────────────────────
    draw.text((PAD, y), 'CHECKLISTS', fill=NAVY, font=f_h2)
    y += 46

    if not data['checklists']:
        _rounded(draw, (PAD, y, W - PAD, y + 70), radius=12, fill=(255, 255, 255))
        draw.text((PAD + 24, y + 22),
                  'No checklists submitted yet today.',
                  fill=MUTED, font=f_body)
        y += 90
    else:
        for c in data['checklists']:
            meta_color = _hex_to_rgb(c['meta'].get('color'))
            card_h = h_chk_card_base + (h_chk_photos if c['photos'] else 0)
            _rounded(draw, (PAD, y, W - PAD, y + card_h), radius=14,
                     fill=(255, 255, 255))
            # Left colour stripe
            draw.rectangle((PAD, y, PAD + 8, y + card_h), fill=meta_color)

            title = (c['meta'].get('title') or c['type']).upper()
            section_label = (c['section'] or '').upper()
            draw.text((PAD + 28, y + 18),
                      f"{title}", fill=NAVY, font=f_h2)
            # Section pill
            sec_color = (231, 76, 60) if section_label == 'CLOSING' else (243, 156, 18)
            sec_w = 130
            sec_x = PAD + 28 + draw.textbbox((0, 0), title, font=f_h2)[2] + 20
            _rounded(draw, (sec_x, y + 26, sec_x + sec_w, y + 56),
                     radius=14, fill=sec_color)
            sec_tx = sec_x + (sec_w - draw.textbbox((0, 0), section_label, font=f_chip)[2]) // 2
            draw.text((sec_tx, y + 33), section_label,
                      fill=(255, 255, 255), font=f_chip)

            # Status pills row
            pill_y = y + 72
            pct = round(c['done_tasks'] / c['total_tasks'] * 100) if c['total_tasks'] else 0
            pills = [
                (f'{c["done_tasks"]}/{c["total_tasks"]} TASKS', (76, 175, 80) if pct >= 90 else (255, 152, 0)),
                (f'{pct}% COMPLETE',                            (33, 150, 243)),
            ]
            if c.get('is_late'):
                pills.append(('LATE', (198, 40, 40)))
            else:
                pills.append(('ON TIME', (46, 125, 50)))
            if c.get('verified'):
                pills.append(('VERIFIED', (26, 26, 46)))

            px = PAD + 28
            for txt, col in pills:
                tw = draw.textbbox((0, 0), txt, font=f_chip)[2] + 24
                _rounded(draw, (px, pill_y, px + tw, pill_y + 34),
                         radius=14, fill=col)
                draw.text((px + 12, pill_y + 8), txt,
                          fill=(255, 255, 255), font=f_chip)
                px += tw + 8

            # Meta lines
            meta_lines = [
                f"Submitted by: {c.get('submitted_by') or '-'}",
                f"Responsible: {c.get('responsible') or '-'}    Submitted at: {(c.get('submitted_at') or '-')[:16]}",
            ]
            if c.get('general_note'):
                meta_lines.append(f"Note: {c['general_note']}")
            my = y + 122
            for line in meta_lines:
                for sub in _wrap_text(draw, line, f_body, W - PAD * 2 - 56)[:1]:
                    draw.text((PAD + 28, my), sub, fill=(51, 51, 51), font=f_body)
                    my += 30

            # Photo strip
            if c['photos']:
                px = PAD + 28
                py = y + card_h - h_chk_photos + 6
                for p in c['photos'][:4]:
                    src = os.path.join(UPLOAD_DIR, p['filename'])
                    if not os.path.exists(src):
                        _rounded(draw, (px, py, px + THUMB, py + THUMB),
                                 radius=10, fill=(245, 245, 245))
                        draw.text((px + 50, py + THUMB // 2 - 12),
                                  'photo missing', fill=MUTED, font=f_small)
                    else:
                        try:
                            thumb = Image.open(src).convert('RGB')
                            # Center-crop to square
                            tw, th = thumb.size
                            sz = min(tw, th)
                            left = (tw - sz) // 2
                            top  = (th - sz) // 2
                            thumb = thumb.crop((left, top, left + sz, top + sz))
                            thumb = thumb.resize((THUMB, THUMB), Image.LANCZOS)
                            # Rounded mask
                            mask = Image.new('L', (THUMB, THUMB), 0)
                            ImageDraw.Draw(mask).rounded_rectangle(
                                (0, 0, THUMB, THUMB), radius=10, fill=255)
                            img.paste(thumb, (px, py), mask)
                        except Exception:
                            _rounded(draw, (px, py, px + THUMB, py + THUMB),
                                     radius=10, fill=(245, 245, 245))
                    px += THUMB + 12
            y += card_h + 18

    # ── TEMPERATURE section ──────────────────────────────────────────────
    draw.text((PAD, y), 'TEMPERATURE RECORDS', fill=NAVY, font=f_h2)
    y += 46

    if not data['temperatures']:
        _rounded(draw, (PAD, y, W - PAD, y + 70), radius=12, fill=(255, 255, 255))
        draw.text((PAD + 24, y + 22),
                  'No temperature records submitted yet today.',
                  fill=MUTED, font=f_body)
        y += 90
    else:
        for t in data['temperatures']:
            meta_color = _hex_to_rgb(t['meta'].get('color'))
            _rounded(draw, (PAD, y, W - PAD, y + h_temp_card), radius=14,
                     fill=(255, 255, 255))
            draw.rectangle((PAD, y, PAD + 8, y + h_temp_card), fill=meta_color)
            title = (t['meta'].get('title') or t['type']).upper()
            draw.text((PAD + 28, y + 18), title, fill=NAVY, font=f_h2)

            pills = [
                (f'{t["reading_count"]} FOODS', (76, 175, 80)),
            ]
            if t.get('discarded'):
                pills.append((f'{t["discarded"]} DISCARDED', (198, 40, 40)))
            if t.get('out_of_zone'):
                pills.append((f'{t["out_of_zone"]} OUT OF ZONE', (198, 40, 40)))
            else:
                pills.append(('ALL SAFE', (46, 125, 50)))

            px = PAD + 28
            pill_y = y + 64
            for txt, col in pills:
                tw = draw.textbbox((0, 0), txt, font=f_chip)[2] + 24
                _rounded(draw, (px, pill_y, px + tw, pill_y + 34),
                         radius=14, fill=col)
                draw.text((px + 12, pill_y + 8), txt,
                          fill=(255, 255, 255), font=f_chip)
                px += tw + 8

            draw.text((PAD + 28, y + 108),
                      f"Recorded by: {t.get('recorded_by') or '-'}    Checked by: {t.get('checked_by') or '-'}",
                      fill=(51, 51, 51), font=f_body)
            y += h_temp_card + 16

    # ── Footer ────────────────────────────────────────────────────────────
    fy = total_h - h_footer
    draw.rectangle((0, fy, W, total_h), fill=NAVY)
    draw.text((PAD, fy + 22),
              f'Generated {datetime.now().strftime("%a %d %b %Y · %H:%M")}',
              fill=(255, 255, 255, 180), font=f_body)
    draw.text((PAD, fy + 56),
              'MCQ Mirrabooka Cafe — Vietnamese Street Food',
              fill=(255, 200, 200), font=f_small)

    out = BytesIO()
    img.save(out, 'PNG', optimize=True)
    out.seek(0)
    return out.getvalue()


# ── Per-checklist detail PNG ─────────────────────────────────────────────────

def _checklist_detail(session_id: int) -> dict | None:
    with _conn() as conn:
        sess = conn.execute('''
            SELECT cs.*,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id) AS total_tasks,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id AND done=1) AS done_tasks
            FROM checklist_sessions cs WHERE cs.id=?''', (session_id,)).fetchone()
        if not sess:
            return None
        sess = dict(sess)
        sess['tasks'] = [dict(r) for r in conn.execute(
            'SELECT task_order, task_name, done, note FROM checklist_tasks '
            'WHERE session_id=? ORDER BY task_order', (session_id,)).fetchall()]
        sess['photos'] = [dict(r) for r in conn.execute(
            'SELECT filename, photo_number FROM checklist_photos '
            'WHERE session_id=? ORDER BY photo_number', (session_id,)).fetchall()]
        sess['meta'] = CHECKLISTS_META.get(sess['type'], {})
    return sess


def build_checklist_png(session_id: int) -> bytes:
    from PIL import Image, ImageChops, ImageDraw, ImageOps

    c = _checklist_detail(session_id)
    if c is None:
        raise ValueError(f'Checklist session {session_id} not found')

    W = 1800
    PAD = 60
    CARD_PAD = 42
    NAVY = (26, 26, 46)
    LIGHT_BG = (244, 246, 250)
    MUTED = (110, 117, 125)
    TEXT = (38, 43, 52)
    LINE = (226, 230, 238)
    OK = (46, 125, 50)
    BAD = (198, 40, 40)

    color = _hex_to_rgb(c['meta'].get('color'))

    f_title  = _font(54, bold=True)
    f_sub    = _font(28)
    f_h2     = _font(34, bold=True)
    f_body   = _font(26)
    f_body_b = _font(26, bold=True)
    f_small  = _font(22)
    f_note   = _font(24)
    f_note_b = _font(24, bold=True)
    f_task   = _font(26)
    f_task_b = _font(26, bold=True)
    f_chip   = _font(20, bold=True)

    measure = ImageDraw.Draw(Image.new('RGB', (W, 100), LIGHT_BG))
    content_w = W - PAD * 2
    inner_w = content_w - CARD_PAD * 2

    def line_h(font) -> int:
        b = measure.textbbox((0, 0), 'Ag', font=font)
        return b[3] - b[1]

    def wrap(text, font, max_width) -> list[str]:
        raw = str(text or '').replace('\r', '')
        if not raw.strip():
            return []
        lines: list[str] = []
        for para in raw.split('\n'):
            if not para.strip():
                lines.append('')
                continue
            lines.extend(_wrap_text(measure, para, font, max_width))
        return lines

    def wrapped_h(lines: list[str], font, gap=6) -> int:
        if not lines:
            return 0
        return len(lines) * line_h(font) + (len(lines) - 1) * gap

    general_note_lines = wrap(c.get('general_note') or '', f_note, inner_w - 40)
    manager_note_lines = wrap(c.get('manager_notes') or '', f_note, inner_w - 40)
    issues_lines = wrap(c.get('issues_found') or '', f_note, inner_w - 40)
    action_lines = wrap(c.get('action_responsible') or '', f_note, inner_w - 40)
    detail_text = [
        f"Submitted by: {c.get('submitted_by') or '-'}    Responsible: {c.get('responsible') or '-'}",
        f"Submitted at: {(c.get('submitted_at') or '-')[:16]}",
        f"Photos attached: {len(c['photos'])}",
    ]
    detail_lines = []
    for line in detail_text:
        detail_lines.extend(wrap(line, f_body, inner_w))

    def note_box_h(note_lines: list[str], font=f_note) -> int:
        return 34 + line_h(f_note_b) + 10 + wrapped_h(note_lines, font)

    h_header = 250
    h_footer = 110
    h_meta = 88 + wrapped_h(detail_lines, f_body, 8) + CARD_PAD
    if general_note_lines:
        h_meta += 8 + note_box_h(general_note_lines) + 14
    if c.get('verified') or manager_note_lines or issues_lines or action_lines:
        verification_lines = []
        if c.get('verified'):
            verified_by = c.get('verified_by') or '-'
            verified_at = (c.get('verified_at') or '-')[:16]
            verification_lines.extend(wrap(f"Verified by: {verified_by}    Verified at: {verified_at}", f_note, inner_w - 40))
        if c.get('overall_result'):
            verification_lines.extend(wrap(f"Result: {c.get('overall_result')}", f_note, inner_w - 40))
        if issues_lines:
            verification_lines.extend(['Issues found:'] + issues_lines)
        if action_lines:
            verification_lines.extend(['Action responsible:'] + action_lines)
        if manager_note_lines:
            verification_lines.extend(['Manager notes:'] + manager_note_lines)
        h_meta += note_box_h(verification_lines or ['Verified']) + 14

    task_rows = []
    task_text_w = inner_w - 76
    for i, t in enumerate(c['tasks'], start=1):
        name_lines = wrap(f"{i}. {t['task_name']}", f_task, task_text_w)
        note_lines = wrap(t.get('note') or '', f_note, task_text_w - 26)
        row_h = 28 + wrapped_h(name_lines, f_task, 7)
        if note_lines:
            row_h += 18 + line_h(f_note_b) + 8 + wrapped_h(note_lines, f_note, 7)
        row_h = max(row_h, 74)
        task_rows.append((t, name_lines, note_lines, row_h))
    h_tasks = 92 + sum(row_h for _, _, _, row_h in task_rows) + CARD_PAD

    photo_gap = 24
    photo_cols = 1 if len(c['photos']) <= 8 else 2
    photo_w = (inner_w - (photo_cols - 1) * photo_gap) // photo_cols
    photo_label_h = 42
    fallback_photo_h = int(photo_w * 0.68)

    def trim_photo_borders(photo):
        """Remove large same-colour margins from screenshots/scans without
        cropping normal camera photos aggressively."""
        try:
            bg = Image.new(photo.mode, photo.size, photo.getpixel((0, 0)))
            diff = ImageChops.difference(photo, bg)
            diff = ImageOps.grayscale(diff).point(lambda p: 255 if p > 18 else 0)
            bbox = diff.getbbox()
            if not bbox:
                return photo
            iw, ih = photo.size
            margin = max(10, int(min(iw, ih) * 0.025))
            left, top, right, bottom = bbox
            bbox = (
                max(0, left - margin),
                max(0, top - margin),
                min(iw, right + margin),
                min(ih, bottom + margin),
            )
            cropped_w = bbox[2] - bbox[0]
            cropped_h = bbox[3] - bbox[1]
            if cropped_w < iw * 0.95 or cropped_h < ih * 0.95:
                return photo.crop(bbox)
        except Exception:
            pass
        return photo

    def photo_box_height(p: dict) -> int:
        src = os.path.join(UPLOAD_DIR, p['filename'])
        if not os.path.exists(src):
            return fallback_photo_h
        try:
            with Image.open(src) as src_img:
                src_img = trim_photo_borders(ImageOps.exif_transpose(src_img).convert('RGB'))
                iw, ih = src_img.size
            if iw > 0 and ih > 0:
                return min(max(int(photo_w * ih / iw), 360), 1280)
        except Exception:
            pass
        return fallback_photo_h
    photo_heights = [photo_box_height(p) for p in c['photos']]
    photo_layout = []
    photo_row_heights = []
    for row_start in range(0, len(c['photos']), photo_cols):
        row_items = []
        row_h = 0
        for col, ix in enumerate(range(row_start, min(row_start + photo_cols, len(c['photos'])))):
            tile_h = photo_label_h + photo_heights[ix]
            row_h = max(row_h, tile_h)
            row_items.append((ix, col))
        row_y = sum(photo_row_heights) + len(photo_row_heights) * photo_gap
        photo_row_heights.append(row_h)
        for ix, col in row_items:
            photo_layout.append({
                'index': ix,
                'col': col,
                'x': PAD + CARD_PAD + col * (photo_w + photo_gap),
                'y': row_y,
                'w': photo_w,
                'h': photo_heights[ix],
            })
    h_photos = 0
    if c['photos']:
        h_photos = 94 + sum(photo_row_heights) + max(0, len(photo_row_heights) - 1) * photo_gap + CARD_PAD

    total_h = h_header + 28 + h_meta + 24 + h_tasks + (h_photos + 24 if h_photos else 0) + h_footer + 30

    img = Image.new('RGB', (W, total_h), LIGHT_BG)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle((0, 0, W, h_header), fill=NAVY)
    draw.rectangle((0, h_header - 8, W, h_header), fill=color)
    logo_path = os.path.join(STATIC_DIR, 'logo.png') if STATIC_DIR else ''
    if os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert('RGBA')
            logo.thumbnail((145, 145), Image.LANCZOS)
            lx, ly = PAD, 44
            _rounded(draw, (lx - 10, ly - 10, lx + logo.width + 10, ly + logo.height + 10),
                     radius=18, fill=(255, 255, 255))
            img.paste(logo, (lx, ly), logo)
        except Exception:
            pass
    text_x = PAD + 180
    title = (c['meta'].get('title') or c['type']).upper()
    section_label = (c['section'] or '').upper()
    draw.text((text_x, 42), title, fill=(255, 255, 255), font=f_title)
    draw.text((text_x, 112), f"{section_label} CHECKLIST",
              fill=(255, 255, 255, 220), font=f_sub)
    try:
        date_pretty = datetime.strptime(c['date'], '%Y-%m-%d').strftime('%A, %d %b %Y').upper()
    except Exception:
        date_pretty = c['date']
    draw.text((text_x, 158), date_pretty, fill=(255, 216, 216), font=f_body_b)

    y = h_header + 28

    # Meta card
    _rounded(draw, (PAD, y, W - PAD, y + h_meta), radius=18, fill=(255, 255, 255))
    draw.rectangle((PAD, y, PAD + 10, y + h_meta), fill=color)
    pct = round(c['done_tasks'] / c['total_tasks'] * 100) if c['total_tasks'] else 0
    pills = [
        (f'{c["done_tasks"]}/{c["total_tasks"]} TASKS', OK if pct >= 90 else (255, 152, 0)),
        (f'{pct}% COMPLETE', (33, 150, 243)),
        ('LATE' if c.get('is_late') else 'ON TIME', BAD if c.get('is_late') else OK),
    ]
    if c.get('verified'):
        pills.append(('VERIFIED', NAVY))

    px = PAD + CARD_PAD
    for txt, col in pills:
        tw = draw.textbbox((0, 0), txt, font=f_chip)[2] + 30
        _rounded(draw, (px, y + 26, px + tw, y + 66), radius=18, fill=col)
        draw.text((px + 15, y + 36), txt, fill=(255, 255, 255), font=f_chip)
        px += tw + 10

    my = y + 88
    for line in detail_lines:
        draw.text((PAD + CARD_PAD, my), line, fill=TEXT, font=f_body)
        my += line_h(f_body) + 8

    def draw_note_box(box_y: int, label: str, note_lines: list[str],
                      fill=(255, 249, 232), accent=(229, 156, 40)) -> int:
        box_h = note_box_h(note_lines)
        bx1, bx2 = PAD + CARD_PAD, W - PAD - CARD_PAD
        _rounded(draw, (bx1, box_y, bx2, box_y + box_h), radius=14, fill=fill,
                 outline=(245, 223, 166), width=1)
        draw.rectangle((bx1, box_y, bx1 + 7, box_y + box_h), fill=accent)
        draw.text((bx1 + 24, box_y + 18), label, fill=(103, 72, 21), font=f_note_b)
        ly = box_y + 18 + line_h(f_note_b) + 12
        for line in note_lines:
            draw.text((bx1 + 24, ly), line, fill=TEXT, font=f_note)
            ly += line_h(f_note) + 7
        return box_h

    if general_note_lines:
        my += 8
        my += draw_note_box(my, 'GENERAL NOTE', general_note_lines) + 14

    if c.get('verified') or manager_note_lines or issues_lines or action_lines:
        verification_lines = []
        if c.get('verified'):
            verified_by = c.get('verified_by') or '-'
            verified_at = (c.get('verified_at') or '-')[:16]
            verification_lines.extend(wrap(f"Verified by: {verified_by}    Verified at: {verified_at}", f_note, inner_w - 40))
        if c.get('overall_result'):
            verification_lines.extend(wrap(f"Result: {c.get('overall_result')}", f_note, inner_w - 40))
        if issues_lines:
            verification_lines.extend(['Issues found:'] + issues_lines)
        if action_lines:
            verification_lines.extend(['Action responsible:'] + action_lines)
        if manager_note_lines:
            verification_lines.extend(['Manager notes:'] + manager_note_lines)
        my += draw_note_box(my, 'MANAGER REVIEW', verification_lines or ['Verified'],
                            fill=(238, 246, 255), accent=(33, 150, 243)) + 14

    y += h_meta + 24

    # Tasks card
    _rounded(draw, (PAD, y, W - PAD, y + h_tasks), radius=18, fill=(255, 255, 255))
    draw.text((PAD + CARD_PAD, y + 24), 'TASKS', fill=NAVY, font=f_h2)
    ty = y + 78
    for i, (t, name_lines, note_lines, row_h) in enumerate(task_rows):
        # Status icon
        row_fill = (255, 255, 255) if i % 2 == 0 else (248, 250, 253)
        if not t['done']:
            row_fill = (255, 246, 246)
        _rounded(draw, (PAD + CARD_PAD - 10, ty, W - PAD - CARD_PAD + 10, ty + row_h - 10),
                 radius=12, fill=row_fill)
        icon_x = PAD + CARD_PAD
        if t['done']:
            _rounded(draw, (icon_x, ty + 19, icon_x + 32, ty + 51), radius=7, fill=OK)
            draw.text((icon_x + 8, ty + 22), '✓', fill=(255, 255, 255), font=f_chip)
            txt_color = TEXT
        else:
            _rounded(draw, (icon_x, ty + 19, icon_x + 32, ty + 51), radius=7, fill=BAD)
            draw.text((icon_x + 10, ty + 22), '×', fill=(255, 255, 255), font=f_chip)
            txt_color = BAD
        text_x = icon_x + 50
        line_y = ty + 18
        for line in name_lines:
            draw.text((text_x, line_y), line, fill=txt_color, font=f_task_b if not t['done'] else f_task)
            line_y += line_h(f_task) + 7
        if note_lines:
            note_y = line_y + 8
            draw.text((text_x, note_y), 'Note:', fill=(129, 91, 25), font=f_note_b)
            note_y += line_h(f_note_b) + 8
            for line in note_lines:
                draw.text((text_x + 26, note_y), line, fill=MUTED, font=f_note)
                note_y += line_h(f_note) + 7
        ty += row_h
    y += h_tasks + 24

    # Photos card
    if c['photos']:
        _rounded(draw, (PAD, y, W - PAD, y + h_photos), radius=18, fill=(255, 255, 255))
        draw.text((PAD + CARD_PAD, y + 24), f"PHOTO EVIDENCE ({len(c['photos'])})", fill=NAVY, font=f_h2)
        py = y + 82
        for item in photo_layout:
            ix = item['index']
            p = c['photos'][ix]
            px = item['x']
            ppy = py + item['y']
            tile_w = item['w']
            photo_h = item['h']
            src = os.path.join(UPLOAD_DIR, p['filename'])
            label = f"Photo {int(p.get('photo_number') or 0) + 1}"
            _rounded(draw, (px, ppy, px + 128, ppy + 34),
                     radius=16, fill=(0, 0, 0))
            draw.text((px + 18, ppy + 8), label, fill=(255, 255, 255), font=f_chip)
            ppy += photo_label_h
            if not os.path.exists(src):
                _rounded(draw, (px, ppy, px + tile_w, ppy + photo_h),
                         radius=16, fill=(245, 245, 245))
                draw.text((px + 150, ppy + photo_h // 2 - 12),
                          'photo missing', fill=MUTED, font=f_small)
                continue
            try:
                photo = trim_photo_borders(ImageOps.exif_transpose(Image.open(src)).convert('RGB'))
                frame = Image.new('RGB', (tile_w, photo_h), (235, 238, 244))
                photo.thumbnail((tile_w, photo_h), Image.LANCZOS)
                frame.paste(photo, ((tile_w - photo.width) // 2, (photo_h - photo.height) // 2))
                mask = Image.new('L', (tile_w, photo_h), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, tile_w, photo_h), radius=16, fill=255)
                img.paste(frame, (px, ppy), mask)
            except Exception:
                _rounded(draw, (px, ppy, px + tile_w, ppy + photo_h),
                         radius=16, fill=(245, 245, 245))
        y += h_photos + 24

    # Footer
    fy = total_h - h_footer
    draw.rectangle((0, fy, W, total_h), fill=NAVY)
    draw.text((PAD, fy + 22),
              f'Generated {datetime.now().strftime("%a %d %b %Y · %H:%M")}',
              fill=(255, 255, 255, 180), font=f_body)
    draw.text((PAD, fy + 54),
              'MCQ Mirrabooka Cafe — Vietnamese Street Food',
              fill=(255, 200, 200), font=f_small)

    out = BytesIO()
    img.save(out, 'PNG', optimize=True)
    out.seek(0)
    return out.getvalue()


# ── Per-temperature detail PNG ──────────────────────────────────────────────

def _temperature_detail(session_id: int) -> dict | None:
    with _conn() as conn:
        sess = conn.execute(
            'SELECT * FROM temp_sessions WHERE id=?', (session_id,)).fetchone()
        if not sess:
            return None
        sess = dict(sess)
        rows = conn.execute('''
            SELECT tr.*, COALESCE(ft.food_kind, 'cold') AS food_kind
            FROM temp_readings tr
            LEFT JOIN temp_food_templates ft
              ON ft.temp_type=? AND ft.food_name=tr.food_name
            WHERE tr.session_id=?
            ORDER BY tr.food_order''', (sess['type'], session_id)).fetchall()
        sess['readings'] = [dict(r) for r in rows]
        sess['meta']     = TEMPERATURES_META.get(sess['type'], {})
    return sess


def _temp_unsafe(kind: str, v) -> bool:
    if v is None:
        return False
    if kind == 'hot':
        return v < 60
    if kind == 'room':           # ambient items: safe 15–30°C
        return v < 15 or v > 30
    return v > 5


def build_temperature_png(session_id: int) -> bytes:
    from PIL import Image, ImageDraw

    t = _temperature_detail(session_id)
    if t is None:
        raise ValueError(f'Temperature session {session_id} not found')

    W = 1080
    PAD = 32
    NAVY = (26, 26, 46)
    LIGHT_BG = (244, 246, 250)
    MUTED = (110, 117, 125)
    OK = (46, 125, 50)
    BAD = (198, 40, 40)

    color = _hex_to_rgb(t['meta'].get('color'))

    f_title  = _font(38, bold=True)
    f_sub    = _font(22)
    f_h2     = _font(28, bold=True)
    f_body   = _font(22)
    f_body_b = _font(22, bold=True)
    f_small  = _font(17)
    f_food   = _font(22, bold=True)
    f_chip   = _font(15, bold=True)
    f_temp   = _font(20, bold=True)

    h_header = 200
    h_meta   = 130
    row_h    = 70
    h_table  = 90 + len(t['readings']) * row_h
    h_footer = 90
    total_h  = h_header + h_meta + h_table + h_footer + 30

    img = Image.new('RGB', (W, total_h), LIGHT_BG)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle((0, 0, W, h_header), fill=NAVY)
    draw.rectangle((0, h_header - 8, W, h_header), fill=color)
    logo_path = os.path.join(STATIC_DIR, 'logo.png') if STATIC_DIR else ''
    if os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert('RGBA')
            logo.thumbnail((120, 120), Image.LANCZOS)
            lx, ly = PAD, 36
            _rounded(draw, (lx - 8, ly - 8, lx + logo.width + 8, ly + logo.height + 8),
                     radius=14, fill=(255, 255, 255))
            img.paste(logo, (lx, ly), logo)
        except Exception:
            pass
    text_x = PAD + 150
    draw.text((text_x, 36),
              (t['meta'].get('title') or t['type']).upper(),
              fill=(255, 255, 255), font=f_title)
    draw.text((text_x, 90), 'TEMPERATURE RECORD',
              fill=(255, 255, 255, 220), font=f_sub)
    try:
        date_pretty = datetime.strptime(t['date'], '%Y-%m-%d').strftime('%A, %d %b %Y').upper()
    except Exception:
        date_pretty = t['date']
    draw.text((text_x, 124), date_pretty, fill=(255, 200, 200), font=f_body_b)

    y = h_header + 24

    # Meta card
    _rounded(draw, (PAD, y, W - PAD, y + h_meta - 24), radius=14, fill=(255, 255, 255))
    unsafe_count = sum(
        1 for r in t['readings']
        for col in ('c1_temp', 'c2_temp', 'c3_temp', 'c4_temp', 'c5_temp')
        if _temp_unsafe(r['food_kind'], r[col])
    )
    discarded = sum(1 for r in t['readings'] if (r.get('discarded') or 'N').upper() == 'Y')
    pills = [
        (f'{len(t["readings"])} FOODS', OK),
        (f'{unsafe_count} OUT-OF-ZONE', BAD if unsafe_count else OK),
    ]
    if discarded:
        pills.append((f'{discarded} DISCARDED', BAD))

    px = PAD + 24
    for txt, col in pills:
        tw = draw.textbbox((0, 0), txt, font=f_chip)[2] + 24
        _rounded(draw, (px, y + 18, px + tw, y + 50), radius=14, fill=col)
        draw.text((px + 12, y + 26), txt, fill=(255, 255, 255), font=f_chip)
        px += tw + 8

    draw.text((PAD + 24, y + 70),
              f"Recorded by: {t.get('recorded_by') or '-'}    Checked by: {t.get('checked_by') or '-'}",
              fill=(51, 51, 51), font=f_body)
    y += h_meta

    # Table card
    _rounded(draw, (PAD, y, W - PAD, y + h_table - 12), radius=14, fill=(255, 255, 255))
    # Header row
    hx = PAD + 24
    draw.text((hx, y + 18), 'FOOD ITEM', fill=NAVY, font=f_h2)
    # Column labels
    col_xs = [W - PAD - 24 - i * 90 for i in reversed(range(5))]
    for ci, cx in enumerate(col_xs):
        draw.text((cx - 24, y + 24), f'C{ci+1}', fill=MUTED, font=f_small)
    ty = y + 62
    for r in t['readings']:
        kind = r['food_kind'] or 'cold'
        # Kind badge
        kind_label = {'hot': 'HOT', 'room': 'ROOM'}.get(kind, 'COLD')
        kind_col   = {'hot': (192, 57, 43), 'room': (0, 137, 123)}.get(kind, (21, 101, 192))
        kind_bg    = {'hot': (255, 224, 178), 'room': (224, 242, 241)}.get(kind, (225, 245, 254))
        kw = draw.textbbox((0, 0), kind_label, font=f_chip)[2] + 18
        _rounded(draw, (PAD + 24, ty + 14, PAD + 24 + kw, ty + 42), radius=10, fill=kind_bg)
        draw.text((PAD + 32, ty + 20), kind_label, fill=kind_col, font=f_chip)
        # Food name
        name = r['food_name']
        name_line = _wrap_text(draw, name, f_food, 380)[0]
        draw.text((PAD + 24 + kw + 12, ty + 18), name_line, fill=(40, 40, 40), font=f_food)
        # Notes line
        if r.get('notes'):
            nx = PAD + 24 + kw + 12
            note_line = _wrap_text(draw, f"Note: {r['notes']}", f_small, 480)[0]
            draw.text((nx, ty + 46), note_line, fill=MUTED, font=f_small)
        # Discarded flag
        if (r.get('discarded') or 'N').upper() == 'Y':
            draw.text((PAD + 24 + kw + 12 + 380, ty + 18),
                      'DISCARDED', fill=BAD, font=f_chip)
        # Temp columns
        for ci, cx in enumerate(col_xs):
            v = r[f'c{ci+1}_temp']
            if v is None:
                draw.text((cx - 18, ty + 22), '—', fill=MUTED, font=f_temp)
                continue
            unsafe = _temp_unsafe(kind, v)
            col = BAD if unsafe else OK
            txt = f'{v:g}°'
            tw_ = draw.textbbox((0, 0), txt, font=f_temp)[2]
            draw.text((cx - tw_, ty + 22), txt, fill=col, font=f_temp)
        # Row separator
        draw.line((PAD + 24, ty + row_h, W - PAD - 24, ty + row_h),
                  fill=(238, 240, 243), width=1)
        ty += row_h
    y += h_table

    # Footer
    fy = total_h - h_footer
    draw.rectangle((0, fy, W, total_h), fill=NAVY)
    draw.text((PAD, fy + 22),
              f'Generated {datetime.now().strftime("%a %d %b %Y · %H:%M")}',
              fill=(255, 255, 255, 180), font=f_body)
    draw.text((PAD, fy + 54),
              'MCQ Mirrabooka Cafe — Vietnamese Street Food',
              fill=(255, 200, 200), font=f_small)

    out = BytesIO()
    img.save(out, 'PNG', optimize=True)
    out.seek(0)
    return out.getvalue()


# ── Daily PDF report (one polished A4 doc, designed for WhatsApp share) ─────

def _pdf_photo_buffer(src, max_px=850):
    """Downscale + exif-correct a photo to a small in-memory JPEG before it is
    embedded in the PDF. Phone photos are 3-4000px but the report shows them at
    ~100mm, so this both speeds up rendering and shrinks the PDF a lot."""
    try:
        st = os.stat(src)
        cache_key = (src, st.st_mtime_ns, st.st_size, max_px)
        cached = _PDF_PHOTO_CACHE.get(cache_key)
        if cached:
            return BytesIO(cached)

        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(src)).convert('RGB')
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, 'JPEG', quality=74, optimize=False, progressive=False)
        return _remember_pdf_photo(cache_key, buf.getvalue())
    except Exception:
        return None


def build_daily_pdf(date_str: str, period: str | None = None) -> bytes:
    """Compose a magazine-style A4 PDF combining cover, every checklist
    (with all photos at large size), and every temperature record into one
    document that's pleasant to scroll on a phone and lands well as a
    WhatsApp attachment."""
    from html import escape as _esc
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        Image as RLImage, PageBreak, KeepTogether, HRFlowable,
    )

    # Reuse the live data collector + the per-session helpers already in
    # this module — keeps the PDF in sync with what the page shows.
    data = _collect_today(date_str, period)
    period_meta = data.get('period_meta') or SHARE_PERIODS['opening']

    # Try the app's font registration helper so we get a TTF that supports
    # Vietnamese diacritics; ReportLab Helvetica doesn't.
    try:
        from app import register_pdf_fonts
        font_name, bold_font = register_pdf_fonts()
    except Exception:
        font_name, bold_font = 'Helvetica', 'Helvetica-Bold'

    NAVY      = colors.HexColor('#1A1A2E')
    NAVY_DK   = colors.HexColor('#0F0F1F')
    BRAND     = colors.HexColor('#C0392B')
    GOLD      = colors.HexColor('#D4AF37')
    OK        = colors.HexColor('#2E7D32')
    BAD       = colors.HexColor('#C62828')
    INK       = colors.HexColor('#1A1A2E')
    MUTED     = colors.HexColor('#6E757E')
    LIGHT_BG  = colors.HexColor('#F4F6FA')
    SOFT      = colors.HexColor('#EEF0F4')

    base = getSampleStyleSheet()
    S = {
        'cover_brand': ParagraphStyle('cover_brand', parent=base['Title'],
            fontName=bold_font, fontSize=40, leading=46,
            textColor=colors.white, alignment=TA_CENTER, spaceAfter=4),
        'cover_sub':   ParagraphStyle('cover_sub', parent=base['Normal'],
            fontName=font_name, fontSize=14, leading=18,
            textColor=colors.HexColor('#FFE082'), alignment=TA_CENTER, spaceAfter=4),
        'cover_date':  ParagraphStyle('cover_date', parent=base['Normal'],
            fontName=bold_font, fontSize=20, leading=24,
            textColor=colors.white, alignment=TA_CENTER, spaceBefore=20),
        'page_title':  ParagraphStyle('page_title', parent=base['Title'],
            fontName=bold_font, fontSize=22, leading=26,
            textColor=colors.white, alignment=TA_LEFT, spaceAfter=0),
        'page_sub':    ParagraphStyle('page_sub', parent=base['Normal'],
            fontName=font_name, fontSize=11, leading=14,
            textColor=colors.HexColor('#EADCC0'), alignment=TA_LEFT, spaceAfter=0),
        'section':     ParagraphStyle('section', parent=base['Heading2'],
            fontName=bold_font, fontSize=16, leading=20, textColor=INK,
            spaceBefore=10, spaceAfter=6),
        'body':        ParagraphStyle('body', parent=base['BodyText'],
            fontName=font_name, fontSize=11.5, leading=15, textColor=INK),
        'body_b':      ParagraphStyle('body_b', parent=base['BodyText'],
            fontName=bold_font, fontSize=10.5, leading=14, textColor=INK),
        'small':       ParagraphStyle('small', parent=base['Normal'],
            fontName=font_name, fontSize=8.5, leading=10, textColor=MUTED),
        'small_b':     ParagraphStyle('small_b', parent=base['Normal'],
            fontName=bold_font, fontSize=8.5, leading=10, textColor=INK),
        'pill':        ParagraphStyle('pill', parent=base['Normal'],
            fontName=bold_font, fontSize=9, leading=11,
            textColor=colors.white, alignment=TA_CENTER),
        'task':        ParagraphStyle('task', parent=base['Normal'],
            fontName=font_name, fontSize=12.5, leading=17, textColor=INK),
        'task_done':   ParagraphStyle('task_done', parent=base['Normal'],
            fontName=font_name, fontSize=12.5, leading=17,
            textColor=MUTED),
        'photo_cap':   ParagraphStyle('photo_cap', parent=base['Normal'],
            fontName=font_name, fontSize=8, leading=10,
            textColor=MUTED, alignment=TA_CENTER),
        'food':        ParagraphStyle('food', parent=base['Normal'],
            fontName=bold_font, fontSize=10, leading=13, textColor=INK),
    }

    PAGE_W, PAGE_H = A4
    MARGIN = 14 * mm
    USABLE_W = PAGE_W - 2 * MARGIN

    # ── Cover page background painter (drawn on the canvas, behind story) ──
    def _draw_cover(canvas, doc_obj):
        canvas.saveState()
        # Full-bleed deep-navy base
        canvas.setFillColor(NAVY_DK)
        canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
        # Vertical gradient feel via stacked translucent bands (top lighter)
        bands = 16
        for i in range(bands):
            t = i / float(bands - 1)
            r = int(26 + t * 14); g = int(22 + t * 12); b = int(48 + t * 20)
            canvas.setFillColorRGB(r/255, g/255, b/255)
            canvas.rect(0, PAGE_H - (i + 1) * (PAGE_H / bands), PAGE_W,
                        PAGE_H / bands + 1, fill=1, stroke=0)

        # Decorative gold double-frame inset from the edges
        inset = 9 * mm
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(1.4)
        canvas.rect(inset, inset, PAGE_W - 2 * inset, PAGE_H - 2 * inset, fill=0, stroke=1)
        canvas.setLineWidth(0.5)
        canvas.rect(inset + 2.2 * mm, inset + 2.2 * mm,
                    PAGE_W - 2 * (inset + 2.2 * mm), PAGE_H - 2 * (inset + 2.2 * mm),
                    fill=0, stroke=1)
        # Corner accents
        canvas.setFillColor(GOLD)
        for cx, cy in [(inset, PAGE_H - inset), (PAGE_W - inset, PAGE_H - inset),
                       (inset, inset), (PAGE_W - inset, inset)]:
            canvas.circle(cx, cy, 1.6 * mm, fill=1, stroke=0)

        # Bottom brand band + gold rule
        canvas.setFillColor(BRAND)
        canvas.rect(0, 0, PAGE_W, 22 * mm, fill=1, stroke=0)
        canvas.setFillColor(GOLD)
        canvas.rect(0, 22 * mm, PAGE_W, 1.4 * mm, fill=1, stroke=0)
        # Tagline inside the brand band
        canvas.setFont(bold_font, 13)
        canvas.setFillColor(colors.white)
        canvas.drawCentredString(PAGE_W / 2, 13.5 * mm, 'Freshly made · authentically Vietnamese')
        canvas.setFont(font_name, 8.5)
        canvas.setFillColor(colors.HexColor('#FFE082'))
        canvas.drawCentredString(PAGE_W / 2, 8 * mm,
                                 'THANK YOU FOR SUPPORTING LOCAL · MCQ MIRRABOOKA CAFE')

        # Logo (centered, large) on a white rounded card with gold ring
        logo_path = os.path.join(STATIC_DIR, 'logo.png') if STATIC_DIR else ''
        if os.path.exists(logo_path):
            try:
                size = 66 * mm
                lx = (PAGE_W - size) / 2
                ly = PAGE_H - 92 * mm
                canvas.setFillColor(GOLD)
                canvas.roundRect(lx - 7.4 * mm, ly - 7.4 * mm, size + 14.8 * mm, size + 14.8 * mm,
                                 12, fill=1, stroke=0)
                canvas.setFillColor(colors.white)
                canvas.roundRect(lx - 6 * mm, ly - 6 * mm, size + 12 * mm, size + 12 * mm,
                                 10, fill=1, stroke=0)
                canvas.drawImage(logo_path, lx, ly, width=size, height=size,
                                 preserveAspectRatio=True, mask='auto')
            except Exception:
                pass

        # Footer line above the brand band
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor('#9AA3B2'))
        canvas.drawCentredString(PAGE_W / 2, 25 * mm,
                          f'Generated {datetime.now().strftime("%a %d %b %Y · %H:%M")}')
        canvas.restoreState()

    def _draw_page(canvas, doc_obj):
        canvas.saveState()
        # Soft page background tint
        canvas.setFillColor(LIGHT_BG)
        canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
        # Top brand bar
        canvas.setFillColor(NAVY)
        canvas.rect(0, PAGE_H - 12 * mm, PAGE_W, 12 * mm, fill=1, stroke=0)
        canvas.setFillColor(BRAND)
        canvas.rect(0, PAGE_H - 12 * mm - 1.5 * mm, PAGE_W, 1.5 * mm, fill=1, stroke=0)

        canvas.setFont(bold_font, 9)
        canvas.setFillColor(colors.white)
        canvas.drawString(MARGIN, PAGE_H - 8 * mm,
                          f'MCQ MIRRABOOKA · {period_meta["label"].upper()} REPORT')
        try:
            d_str = datetime.strptime(date_str, '%Y-%m-%d').strftime('%a %d %b %Y')
        except Exception:
            d_str = date_str
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 8 * mm, d_str)

        # Footer
        canvas.setFont(font_name, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 7 * mm,
                          f'mcqstreetfoodchecklist.pythonanywhere.com')
        canvas.drawRightString(PAGE_W - MARGIN, 7 * mm,
                               f'Page {doc_obj.page}')
        canvas.restoreState()

    # ── Story builder ──────────────────────────────────────────────────────

    story = []

    # ── Cover content (positioned via Spacers because background is canvas) ──
    story.append(Spacer(1, 100 * mm))     # leave room for logo painted by _draw_cover
    story.append(Paragraph('MCQ MIRRABOOKA CAFE', S['cover_brand']))
    story.append(Paragraph(f'VIETNAMESE STREET FOOD · {period_meta["cover"]}', S['cover_sub']))
    try:
        date_pretty = datetime.strptime(date_str, '%Y-%m-%d').strftime('%A · %d %B %Y').upper()
    except Exception:
        date_pretty = date_str
    story.append(Paragraph(date_pretty, S['cover_date']))
    story.append(Spacer(1, 12 * mm))

    # ── Elegant eyebrow + gold hairline ────────────────────────────────────
    story.append(HRFlowable(width=58 * mm, thickness=1, color=GOLD,
                            spaceBefore=0, spaceAfter=6, hAlign='CENTER'))
    story.append(Paragraph(' '.join('OPERATIONS · SUMMARY'),
        ParagraphStyle('eyebrow', fontName=bold_font, fontSize=11, leading=13,
                       textColor=GOLD, alignment=TA_CENTER, spaceAfter=6)))

    # ── Headline stats ─────────────────────────────────────────────────────
    chk_done = sum(1 for c in data['checklists']
                   if c['done_tasks'] == c['total_tasks'] and c['total_tasks'] > 0)
    chk_late = sum(1 for c in data['checklists'] if c.get('is_late'))
    temp_alerts = sum(c.get('out_of_zone', 0) for c in data['temperatures'])
    equip_stats = _equipment_stats(data.get('equipment'))
    equip_recorded = f'{equip_stats["recorded"]}/{equip_stats["total_checks"]}'
    total_alerts = (temp_alerts + chk_late + equip_stats['attention']
                    + len(data.get('violations', [])))

    # One reusable stat tile: bold gold/colour hairline, big value, status sub
    def _stat_card(label, value, label_hex, sub_markup=None, accent_rule='#D4AF37',
                   value_size=32, value_hex='#FFFFFF', card_w=54):
        rows = [
            [Paragraph(f'<font color="{label_hex}" size="10"><b>{label}</b></font>',
                       ParagraphStyle('sc_l', fontName=bold_font, alignment=TA_CENTER))],
            [Paragraph(f'<font color="{value_hex}" size="{value_size}"><b>{value}</b></font>',
                       ParagraphStyle('sc_v', fontName=bold_font, alignment=TA_CENTER,
                                      leading=value_size + 2))],
        ]
        rh = [6.5 * mm, 13.5 * mm]
        if sub_markup is not None:
            rows.append([Paragraph(sub_markup, ParagraphStyle('sc_s', fontName=font_name,
                         fontSize=8, leading=10, alignment=TA_CENTER))])
            rh.append(5 * mm)
        t = Table(rows, colWidths=[card_w * mm], rowHeights=rh)
        t.hAlign = 'CENTER'
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#1E1E38')),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2A2A49')),
            ('LINEABOVE', (0, 0), (-1, 0), 1.8, colors.HexColor(accent_rule)),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ]))
        return t

    def _stat_row(cards):
        n = max(len(cards), 1)
        row = Table([cards], colWidths=[USABLE_W / n] * n)
        row.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        return row

    chk_sub = (f'<font color="#FF9A9A">{chk_late} late</font>' if chk_late
               else f'<font color="#9AE6A0">{chk_done}/{len(data["checklists"])} done</font>')
    if equip_stats['alerts']:
        eq_sub = f'<font color="#FF9A9A">{equip_stats["alerts"]} out of range</font>'
    elif equip_stats['missing_due']:
        eq_sub = f'<font color="#FFD39B">{equip_stats["missing_due"]} due missing</font>'
    else:
        eq_sub = '<font color="#9AE6A0">all in range</font>'
    al_sub = ('<font color="#9AE6A0">all clear</font>' if total_alerts == 0
              else '<font color="#FF9A9A">needs review</font>')

    prep_data = data.get('prep_timetable') or {}
    prep_total = int(prep_data.get('total') or 0)
    prep_done = int(prep_data.get('done') or 0)
    if not prep_total:
        prep_sub = '<font color="#9AA3B2">no tasks today</font>'
    elif prep_done >= prep_total:
        prep_sub = '<font color="#9AE6A0">all done</font>'
    else:
        prep_sub = f'<font color="#FFD39B">{prep_total - prep_done} not done</font>'

    story.append(_stat_row([
        _stat_card('CHECKLISTS', str(len(data['checklists'])), '#A5D6A7', chk_sub,
                   accent_rule='#43A047', value_size=26, card_w=42),
        _stat_card('PREP', f'{prep_done}/{prep_total}', '#C8E6C9', prep_sub,
                   accent_rule='#2E7D32', value_size=23, card_w=42),
        _stat_card('EQUIPMENT', equip_recorded, '#80DEEA', eq_sub,
                   accent_rule='#00ACC1', value_size=23, card_w=42),
        _stat_card('ALERTS', str(total_alerts), '#FFCDD2', al_sub,
                   accent_rule='#C62828' if total_alerts else '#43A047',
                   value_size=26, card_w=42),
    ]))

    # ── Food temperature by station — opening reports include food temps ────
    if period_meta.get('includes_food_temps'):
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph(' '.join('FOOD · TEMPERATURE'),
            ParagraphStyle('food_eyebrow', fontName=bold_font, fontSize=9.5, leading=12,
                           textColor=colors.HexColor('#FFE082'), alignment=TA_CENTER,
                           spaceAfter=5)))
        temp_by_type = {t['type']: t for t in data['temperatures']}
        food_cards = []
        for ttype in ('chef', 'banh_mi', 'pastry'):
            meta = TEMPERATURES_META.get(ttype, {})
            short = (meta.get('short') or ttype.replace('_', ' ').title()).upper()
            accent = meta.get('color') or '#888888'
            sess = temp_by_type.get(ttype)
            if sess:
                ooz = sess.get('out_of_zone') or 0
                sub = (f'<font color="#FF9A9A">{ooz} out of zone</font>' if ooz
                       else '<font color="#9AE6A0">All safe</font>')
                food_cards.append(_stat_card(short, str(sess.get('reading_count') or 0),
                                             '#FFFFFF', sub, accent_rule=accent))
            else:
                food_cards.append(_stat_card(short, 'Missing', '#FFFFFF',
                                             '<font color="#9AA3B2">not recorded</font>',
                                             accent_rule=accent, value_size=19,
                                             value_hex='#8A8FA3'))
        story.append(_stat_row(food_cards))
    else:
        # Closing reports carry no food temps — surface the equipment closing
        # check breakdown instead so the cover stays balanced and informative.
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph(' '.join('EQUIPMENT · CHECK'),
            ParagraphStyle('eq_eyebrow', fontName=bold_font, fontSize=9.5, leading=12,
                           textColor=colors.HexColor('#FFE082'), alignment=TA_CENTER,
                           spaceAfter=5)))
        in_range = max(equip_stats['recorded'] - equip_stats['alerts'], 0)
        attn = equip_stats['attention']
        story.append(_stat_row([
            _stat_card('UNITS', str(equip_stats['total_units']), '#80DEEA',
                       '<font color="#C7CBD6">closing check</font>', accent_rule='#00ACC1'),
            _stat_card('IN RANGE', str(in_range), '#A5D6A7',
                       '<font color="#9AE6A0">in range</font>', accent_rule='#43A047'),
            _stat_card('ATTENTION', str(attn), '#FFCDD2',
                       ('<font color="#FF9A9A">review</font>' if attn
                        else '<font color="#9AE6A0">all clear</font>'),
                       accent_rule='#C62828' if attn else '#43A047'),
        ]))

    story.append(Spacer(1, 6 * mm))

    # ── Status banner: green = all clear, red = attention needed ────────────
    if total_alerts == 0:
        _bn_txt, _bn_col = 'ALL CLEAR · NO ALERTS TODAY', OK
    else:
        _bn_txt, _bn_col = f'{total_alerts} ITEM(S) NEED ATTENTION', BAD
    banner = Table([[Paragraph(
        f'<font color="white" size="13"><b>{_bn_txt}</b></font>',
        ParagraphStyle('bn', fontName=bold_font, alignment=TA_CENTER))]],
        colWidths=[USABLE_W], rowHeights=[11 * mm])
    banner.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), _bn_col),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LINEABOVE', (0, 0), (-1, 0), 1.2, GOLD),
    ]))
    story.append(banner)

    # ── Per-checklist pages ────────────────────────────────────────────────
    if data['checklists']:
        story.append(PageBreak())
        story.append(Paragraph(f'{period_meta["label"].upper()} CHECKLISTS', S['section']))
        story.append(Paragraph(
            f"{len(data['checklists'])} checklist(s) included in this {period_meta['label'].lower()} report. "
            "Each station follows on its own page below.", S['body']))

    for c in data['checklists']:
        full = _checklist_detail(c['id']) or c
        col_hex = (full['meta'].get('color') or '#888888')
        col = colors.HexColor(col_hex)

        story.append(PageBreak())

        # Header band table with station name + section pill
        title = (full['meta'].get('title') or full['type']).upper()
        section_label = (full['section'] or '').upper()
        hdr_tbl = Table([[
            Paragraph(f'<font color="white" size="22"><b>{_esc(title)}</b></font><br/>'
                      f'<font color="#EADCC0" size="11">{_esc(section_label)} CHECKLIST · '
                      f'{full["date"]}</font>',
                      ParagraphStyle('h1', fontName=bold_font, leading=26))
        ]], colWidths=[USABLE_W])
        hdr_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), col),
            ('LEFTPADDING', (0, 0), (-1, -1), 16),
            ('RIGHTPADDING', (0, 0), (-1, -1), 16),
            ('TOPPADDING', (0, 0), (-1, -1), 14),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ]))
        story.append(hdr_tbl)
        story.append(Spacer(1, 4 * mm))

        # Status pills row
        pct = round(full['done_tasks'] / full['total_tasks'] * 100) if full['total_tasks'] else 0
        pills = [
            (f'{full["done_tasks"]}/{full["total_tasks"]} TASKS', OK if pct >= 90 else colors.HexColor('#E65100')),
            (f'{pct}% COMPLETE', colors.HexColor('#1565C0')),
            ('LATE' if full.get('is_late') else 'ON TIME', BAD if full.get('is_late') else OK),
        ]
        if full.get('verified'):
            pills.append(('VERIFIED', NAVY))
        pill_cells = []
        for txt, c_ in pills:
            cell = Table([[Paragraph(f'<font color="white"><b>{_esc(txt)}</b></font>',
                                     ParagraphStyle('p', fontName=bold_font, fontSize=9,
                                                    alignment=TA_CENTER))]],
                         colWidths=[None], rowHeights=[7 * mm])
            cell.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), c_),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (-1, -1), 12),
                ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ]))
            pill_cells.append(cell)
        # Lay them out horizontally
        pill_row = Table([pill_cells], colWidths=[USABLE_W / max(len(pills), 1)] * len(pills))
        pill_row.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 2),
            ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ]))
        story.append(pill_row)
        story.append(Spacer(1, 5 * mm))

        # Meta block (submitted by / responsible / submitted at)
        meta_rows = [
            ['Submitted by', full.get('submitted_by') or '—',
             'Responsible',  full.get('responsible')  or '—'],
            ['Submitted at', (full.get('submitted_at') or '—')[:16],
             'Section',     section_label.title()],
        ]
        if full.get('general_note'):
            meta_rows.append(['Note', full['general_note'], '', ''])
        meta = Table(meta_rows, colWidths=[USABLE_W * 0.16, USABLE_W * 0.34,
                                            USABLE_W * 0.16, USABLE_W * 0.34])
        meta.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.white),
            ('FONTNAME', (0, 0), (-1, -1), font_name),
            ('FONTNAME', (0, 0), (0, -1), bold_font),
            ('FONTNAME', (2, 0), (2, -1), bold_font),
            ('TEXTCOLOR', (0, 0), (0, -1), MUTED),
            ('TEXTCOLOR', (2, 0), (2, -1), MUTED),
            ('TEXTCOLOR', (1, 0), (1, -1), INK),
            ('TEXTCOLOR', (3, 0), (3, -1), INK),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('LINEBELOW', (0, 0), (-1, -2), 0.3, SOFT),
            ('LINEAFTER', (1, 0), (1, -1), 0.3, SOFT),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(meta)
        story.append(Spacer(1, 6 * mm))

        # Tasks list
        story.append(Paragraph('TASKS', S['section']))
        task_rows = []
        for t in full['tasks']:
            mark = '<font color="#2E7D32"><b>✓</b></font>' if t['done'] \
                   else '<font color="#C62828"><b>✗</b></font>'
            style = S['task_done'] if t['done'] else S['task']
            name = Paragraph(_esc(t['task_name']), style)
            note = Paragraph(f'<i>{_esc(t["note"] or "")}</i>', S['small']) if t.get('note') else ''
            task_rows.append([Paragraph(mark, S['task']), name, note])
        if task_rows:
            tt = Table(task_rows,
                       colWidths=[10 * mm, USABLE_W * 0.55, USABLE_W * 0.32])
            tt.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.white),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('LEFTPADDING', (0, 0), (-1, -1), 10),
                ('LINEBELOW', (0, 0), (-1, -1), 0.25, SOFT),
            ]))
            story.append(tt)
        story.append(Spacer(1, 6 * mm))

        # Photos — BIG and beautiful. 2 columns, full usable width.
        if full.get('photos'):
            story.append(PageBreak())
            story.append(hdr_tbl)
            story.append(Spacer(1, 4 * mm))
            story.append(Paragraph(f'PHOTO EVIDENCE ({len(full["photos"])})', S['section']))

            col_w = (USABLE_W - 6 * mm) / 2
            max_h = 100 * mm
            cells = []
            for p in full['photos']:
                src = os.path.join(UPLOAD_DIR, p['filename'])
                cap = Paragraph(f'Photo {p["photo_number"] + 1}', S['photo_cap'])
                if not os.path.exists(src):
                    cells.append([Paragraph('(photo missing)', S['small']), cap])
                    continue
                try:
                    rl_img = RLImage(_pdf_photo_buffer(src) or src)
                    iw, ih = rl_img.imageWidth, rl_img.imageHeight
                    ratio = col_w / float(iw) if iw else 1
                    h = ih * ratio
                    if h > max_h:
                        h = max_h
                        ratio = h / float(ih) if ih else 1
                        rl_img.drawWidth  = iw * ratio
                        rl_img.drawHeight = h
                    else:
                        rl_img.drawWidth = col_w
                        rl_img.drawHeight = h
                    cells.append([rl_img, cap])
                except Exception:
                    cells.append([Paragraph('(could not load)', S['small']), cap])

            # Pair into rows of 2
            grid_rows = []
            for i in range(0, len(cells), 2):
                pair = cells[i:i + 2]
                if len(pair) == 1: pair.append([''] * 2)
                # build two stacked subtables (img on top, caption below)
                grid_rows.append([
                    Table([[pair[0][0]], [pair[0][1]]],
                           colWidths=[col_w],
                           style=TableStyle([
                               ('TOPPADDING', (0, 0), (-1, -1), 0),
                               ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                               ('LEFTPADDING', (0, 0), (-1, -1), 0),
                               ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                           ])),
                    Table([[pair[1][0]], [pair[1][1]]],
                           colWidths=[col_w],
                           style=TableStyle([
                               ('TOPPADDING', (0, 0), (-1, -1), 0),
                               ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                               ('LEFTPADDING', (0, 0), (-1, -1), 0),
                               ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                           ])),
                ])
            photo_grid = Table(grid_rows,
                               colWidths=[col_w, col_w],
                               style=TableStyle([
                                   ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                   ('LEFTPADDING', (0, 0), (-1, -1), 3),
                                   ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                                   ('TOPPADDING', (0, 0), (-1, -1), 3),
                                   ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                               ]))
            story.append(photo_grid)

    # ── Per-temperature pages ──────────────────────────────────────────────
    if data['temperatures']:
        story.append(PageBreak())
        story.append(Paragraph('OPENING TEMPERATURE RECORDS', S['section']))
        story.append(Paragraph(
            f"{len(data['temperatures'])} food temperature record(s) included in this opening report. "
            "Each station's readings follow on its own page.", S['body']))

    for t in data['temperatures']:
        full = _temperature_detail(t['id']) or t
        col_hex = (full['meta'].get('color') or '#888888')
        col = colors.HexColor(col_hex)

        story.append(PageBreak())
        title = (full['meta'].get('title') or full['type']).upper()
        hdr_tbl = Table([[
            Paragraph(f'<font color="white" size="20"><b>{_esc(title)}</b></font><br/>'
                      f'<font color="#EADCC0" size="11">TEMPERATURE RECORD · '
                      f'{full["date"]}</font>',
                      ParagraphStyle('th1', fontName=bold_font, leading=24))
        ]], colWidths=[USABLE_W])
        hdr_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), col),
            ('LEFTPADDING', (0, 0), (-1, -1), 16),
            ('RIGHTPADDING', (0, 0), (-1, -1), 16),
            ('TOPPADDING', (0, 0), (-1, -1), 14),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ]))
        story.append(hdr_tbl)
        story.append(Spacer(1, 4 * mm))

        unsafe_count = sum(
            1 for r in full['readings']
            for cc in ('c1_temp','c2_temp','c3_temp','c4_temp','c5_temp')
            if _temp_unsafe(r['food_kind'], r[cc])
        )
        discarded = sum(1 for r in full['readings'] if (r.get('discarded') or 'N').upper() == 'Y')

        # Pills
        pills = [
            (f'{len(full["readings"])} FOODS', OK),
            (f'{unsafe_count} OUT OF ZONE', BAD if unsafe_count else OK),
        ]
        if discarded:
            pills.append((f'{discarded} DISCARDED', BAD))
        pill_cells = []
        for txt, c_ in pills:
            cell = Table([[Paragraph(f'<font color="white"><b>{_esc(txt)}</b></font>',
                                     ParagraphStyle('pt', fontName=bold_font, fontSize=9, alignment=TA_CENTER))]],
                         rowHeights=[7 * mm])
            cell.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), c_),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (-1, -1), 12),
                ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ]))
            pill_cells.append(cell)
        pill_row = Table([pill_cells], colWidths=[USABLE_W / max(len(pills),1)] * len(pills))
        pill_row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                                      ('LEFTPADDING', (0, 0), (-1, -1), 2),
                                      ('RIGHTPADDING', (0, 0), (-1, -1), 2)]))
        story.append(pill_row)
        story.append(Spacer(1, 5 * mm))

        # Meta
        meta = Table([
            ['Recorded by', full.get('recorded_by') or '—',
             'Checked by',  full.get('checked_by')  or '—'],
        ], colWidths=[USABLE_W * 0.16, USABLE_W * 0.34,
                       USABLE_W * 0.16, USABLE_W * 0.34])
        meta.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.white),
            ('FONTNAME', (0, 0), (-1, -1), font_name),
            ('FONTNAME', (0, 0), (0, -1), bold_font),
            ('FONTNAME', (2, 0), (2, -1), bold_font),
            ('TEXTCOLOR', (0, 0), (0, -1), MUTED),
            ('TEXTCOLOR', (2, 0), (2, -1), MUTED),
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(meta)
        story.append(Spacer(1, 6 * mm))

        # Readings table
        story.append(Paragraph('READINGS', S['section']))
        head = ['Kind', 'Food item', 'C1', 'C2', 'C3', 'C4', 'C5', 'Note']
        rows_data = [head]
        for r in full['readings']:
            kind = r['food_kind'] or 'cold'
            kind_label = {'hot': 'HOT', 'room': 'ROOM'}.get(kind, 'COLD')
            kind_color = colors.HexColor({'hot': '#FFE0B2', 'room': '#E0F2F1'}.get(kind, '#E1F5FE'))
            kind_text  = colors.HexColor({'hot': '#C0392B', 'room': '#00897B'}.get(kind, '#1565C0'))

            cells = [
                Paragraph(f'<font color="{kind_text.hexval()}"><b>{kind_label}</b></font>',
                          ParagraphStyle('kc', fontName=bold_font, fontSize=8, alignment=TA_CENTER)),
                Paragraph(_esc(r['food_name']), S['food']),
            ]
            for cc in ('c1_temp','c2_temp','c3_temp','c4_temp','c5_temp'):
                v = r[cc]
                if v is None:
                    cells.append(Paragraph('<font color="#999">—</font>',
                                           ParagraphStyle('v', fontName=font_name, alignment=TA_CENTER, fontSize=10)))
                else:
                    unsafe = _temp_unsafe(kind, v)
                    color_hex = '#C62828' if unsafe else '#2E7D32'
                    cells.append(Paragraph(
                        f'<font color="{color_hex}"><b>{v:g}°</b></font>',
                        ParagraphStyle('v', fontName=bold_font, alignment=TA_CENTER, fontSize=10)))
            note = r.get('notes') or ''
            cells.append(Paragraph(f'<i>{_esc(note)}</i>', S['small']))
            rows_data.append(cells)

        col_widths = [USABLE_W * 0.08, USABLE_W * 0.32] + [USABLE_W * 0.07] * 5 + [USABLE_W * 0.25]
        rt = Table(rows_data, colWidths=col_widths, repeatRows=1)
        rt.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), NAVY),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), bold_font),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.3, SOFT),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            # Tint the kind cell to match
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FAFBFD')]),
        ]))
        story.append(rt)

    # ── Equipment temperature page ─────────────────────────────────────────
    equip = data.get('equipment')
    if equip and equip.get('total'):
        story.append(PageBreak())
        eq_col = colors.HexColor('#00838F')
        WARN = colors.HexColor('#E65100')
        equip_stats = _equipment_stats(equip)
        due_keys = set(equip.get('due_check_keys') or [])
        check_types = equip.get('check_types') or []
        if not check_types:
            check_types = [{'key': 'morning', 'label': 'Morning', 'short': 'AM'}]
        check_keys = [c.get('key') for c in check_types]
        hdr = Table([[Paragraph(
            '<font color="white" size="20"><b>EQUIPMENT TEMPERATURE</b></font><br/>'
            f'<font color="#D7F2F5" size="11">{_esc(equip.get("period_check_label") or "Equipment Check").upper()} · '
            f'FRIDGES · FREEZERS · HOT UNITS · {date_str}</font>',
            ParagraphStyle('eqh', fontName=bold_font, leading=24))]], colWidths=[USABLE_W])
        hdr.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), eq_col),
            ('LEFTPADDING', (0, 0), (-1, -1), 16), ('RIGHTPADDING', (0, 0), (-1, -1), 16),
            ('TOPPADDING', (0, 0), (-1, -1), 14), ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ]))
        story.append(hdr)
        story.append(Spacer(1, 4 * mm))

        # Pills
        e_pills = [
            (f'{equip_stats["recorded"]}/{equip_stats["total_checks"]} CHECKS RECORDED',
             OK if equip_stats['missing'] == 0 else WARN),
            (f'{equip_stats["alerts"]} OUT OF RANGE',
             BAD if equip_stats['alerts'] else OK),
            (f'{equip_stats["missing_due"]} DUE MISSING',
             BAD if equip_stats['missing_due'] else OK),
        ]
        e_cells = []
        for txt, c_ in e_pills:
            cell = Table([[Paragraph(f'<font color="white"><b>{_esc(txt)}</b></font>',
                ParagraphStyle('ep', fontName=bold_font, fontSize=9, alignment=TA_CENTER))]],
                rowHeights=[7 * mm])
            cell.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), c_),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (-1, -1), 12), ('RIGHTPADDING', (0, 0), (-1, -1), 12)]))
            e_cells.append(cell)
        epr = Table([e_cells], colWidths=[USABLE_W / len(e_pills)] * len(e_pills))
        epr.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 2), ('RIGHTPADDING', (0, 0), (-1, -1), 2)]))
        story.append(epr)
        story.append(Spacer(1, 4 * mm))
        eq_check_names = ' and '.join(_esc(c.get('label') or c.get('key', '').title()) for c in check_types)
        story.append(Paragraph(
            f'Report scope — <b>{eq_check_names}</b>. '
            'Safe ranges — Fridge <b>0 to 5°C</b> · Freezer <b>-20 to -15°C</b> · Hot unit <b>&ge; 60°C</b>.',
            S['body']))
        story.append(Spacer(1, 3 * mm))

        kind_label = {'cold': 'FRIDGE', 'freezer': 'FREEZER', 'hot': 'HOT'}
        def _eq_temp_cell(reading, key):
            reading = reading or {}
            if reading.get('defrosted'):
                return Paragraph('<font color="#1565C0"><b>DEFROST</b></font>',
                    ParagraphStyle('ed', fontName=bold_font, fontSize=8.5, alignment=TA_CENTER))
            temp = reading.get('temp')
            if temp is None:
                label = 'MISSING' if key in due_keys else 'PENDING'
                col_hex = '#C62828' if key in due_keys else '#999999'
                return Paragraph(f'<font color="{col_hex}"><b>{label}</b></font>',
                    ParagraphStyle('en', fontName=bold_font, fontSize=8.5, alignment=TA_CENTER))
            unsafe = bool(reading.get('unsafe'))
            col_hex = '#C62828' if unsafe else '#2E7D32'
            tag = 'OUT' if unsafe else 'OK'
            return Paragraph(f'<font color="{col_hex}"><b>{temp:g}°C</b></font><br/>'
                             f'<font color="{col_hex}" size="7"><b>{tag}</b></font>',
                ParagraphStyle('et', fontName=bold_font, fontSize=10, leading=11, alignment=TA_CENTER))

        rows_data = [['Equipment', 'Type', 'Safe range']
                     + [c.get('label') or c.get('key', '').title() for c in check_types]
                     + ['Status']]
        for u in equip['units']:
            checks = u.get('checks') or {}
            is_defrost = bool(u.get('defrosted')) or any(
                (checks.get(k) or {}).get('defrosted') for k in check_keys)
            unsafe = any(bool((checks.get(k) or {}).get('unsafe')) for k in check_keys)
            due_missing = any((checks.get(k) or {}).get('temp') is None and k in due_keys
                              for k in check_keys)
            pending = any((checks.get(k) or {}).get('temp') is None for k in check_keys)
            if is_defrost:
                status_p = Paragraph('<font color="#1565C0"><b>DEFROSTING</b></font>',
                    ParagraphStyle('es', fontName=bold_font, fontSize=8.5, alignment=TA_CENTER))
            elif unsafe:
                status_p = Paragraph('<font color="#C62828"><b>OUT OF RANGE</b></font>',
                    ParagraphStyle('es', fontName=bold_font, fontSize=8.5, alignment=TA_CENTER))
            elif due_missing:
                status_p = Paragraph('<font color="#C62828"><b>MISSING</b></font>',
                    ParagraphStyle('es', fontName=bold_font, fontSize=8.5, alignment=TA_CENTER))
            elif pending:
                status_p = Paragraph('<font color="#777777"><b>PENDING</b></font>',
                    ParagraphStyle('es', fontName=bold_font, fontSize=8.5, alignment=TA_CENTER))
            else:
                status_p = Paragraph('<font color="#2E7D32"><b>OK</b></font>',
                    ParagraphStyle('es', fontName=bold_font, fontSize=8.5, alignment=TA_CENTER))
            rows_data.append([
                Paragraph(_esc(u['name']), S['food']),
                Paragraph(kind_label.get(u['kind'], u['kind'].upper()), S['small_b']),
                Paragraph(_esc(u.get('range', '')), S['small']),
                *[_eq_temp_cell(checks.get(k) or {}, k) for k in check_keys],
                status_p,
            ])
        if len(check_types) == 1:
            col_widths = [USABLE_W * 0.36, USABLE_W * 0.13,
                          USABLE_W * 0.21, USABLE_W * 0.15,
                          USABLE_W * 0.15]
        else:
            remaining = USABLE_W * 0.28
            col_widths = [USABLE_W * 0.29, USABLE_W * 0.11, USABLE_W * 0.17]
            col_widths += [remaining / len(check_types)] * len(check_types)
            col_widths.append(USABLE_W * 0.15)
        et = Table(rows_data, colWidths=col_widths, repeatRows=1)
        et.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), NAVY),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), bold_font), ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.3, SOFT),
            ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FAFBFD')]),
        ]))
        story.append(et)

    # ── Prep timetable (same content on the opening & closing reports) ─────
    prep_tt = data.get('prep_timetable') or {}
    prep_stations = prep_tt.get('stations') or []
    if prep_stations:
        story.append(PageBreak())
        try:
            _pd = datetime.strptime(date_str, '%Y-%m-%d').strftime('%A · %d %b %Y')
        except Exception:
            _pd = date_str
        is_opening = data.get('period') != 'closing'
        _pdone, _ptot = prep_tt.get('done', 0), prep_tt.get('total', 0)
        _ppend = max(_ptot - _pdone, 0)
        sub_line = (f'{_ppend} PENDING of {_ptot} · {_pd}' if is_opening
                    else f'{_pdone} DONE · {_ppend} NOT DONE of {_ptot} · {_pd}')
        hdr = Table([[Paragraph(
            '<font color="white" size="20"><b>PREP TIMETABLE — TODAY</b></font><br/>'
            f'<font color="#D7F5DD" size="11">{sub_line}</font>',
            ParagraphStyle('ptth', fontName=bold_font, leading=24))]], colWidths=[USABLE_W])
        hdr.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#2E7D32')),
            ('LEFTPADDING', (0, 0), (-1, -1), 16), ('RIGHTPADDING', (0, 0), (-1, -1), 16),
            ('TOPPADDING', (0, 0), (-1, -1), 14), ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ]))
        story.append(hdr)
        story.append(Spacer(1, 4 * mm))
        if is_opening:
            intro = ('Morning report — prep is ticked off at closing. '
                     '<font color="#B45309"><b>PENDING</b></font> means not done yet (normal in the morning).')
        else:
            intro = ('Closing report — <font color="#1B7F3B"><b>DONE</b></font> = ticked complete · '
                     '<font color="#C62828"><b>NOT DONE</b></font> = not completed (reason shown).')
        if not prep_tt.get('has_schedule'):
            intro += ' (No weekly schedule built yet — showing the planned timetable.)'
        story.append(Paragraph(intro, S['body']))
        story.append(Spacer(1, 4 * mm))
        for st in prep_stations:
            col = colors.HexColor(st.get('color') or '#2E7D32')
            st_total = st.get('total', len(st['tasks']))
            st_summary = (f'{st_total - st.get("done", 0)} pending' if is_opening
                          else f'{st.get("done", 0)}/{st_total} done')
            sub = Table([[Paragraph(
                f'<font color="white"><b>{_esc(st["station"])}</b>'
                f'<font size="9">  ·  {st_summary}</font></font>',
                ParagraphStyle('pst', fontName=bold_font, fontSize=12))]], colWidths=[USABLE_W])
            sub.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), col),
                ('LEFTPADDING', (0, 0), (-1, -1), 12), ('RIGHTPADDING', (0, 0), (-1, -1), 12),
                ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ]))
            story.append(sub)
            trows = []
            for t in st['tasks']:
                reason = (t.get('reason') or '').strip()
                if t.get('done'):
                    badge = '<font color="#1B7F3B"><b>DONE</b></font>'
                    extra = (f'<font color="#7A8089">{_esc(t.get("done_by") or "")}</font>'
                             if t.get('done_by') else (_esc(t['time']) if t['time'] else ''))
                elif is_opening:
                    badge = '<font color="#B45309"><b>PENDING</b></font>'
                    extra = _esc(t['time']) if t['time'] else ''
                else:
                    badge = '<font color="#C62828"><b>NOT DONE</b></font>'
                    extra = (f'<font color="#C62828"><b>Reason:</b> {_esc(reason)}</font>' if reason
                             else '<font color="#B45309">(no reason given)</font>')
                trows.append([
                    Paragraph(badge, ParagraphStyle('pbk', fontName=bold_font, fontSize=9, alignment=TA_CENTER)),
                    Paragraph(_esc(t['name']), S['task']),
                    Paragraph(extra, S['small']),
                ])
            tt = Table(trows, colWidths=[26 * mm, USABLE_W - 26 * mm - USABLE_W * 0.20, USABLE_W * 0.20])
            tt.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.white),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('LINEBELOW', (0, 0), (-1, -1), 0.25, SOFT),
            ]))
            story.append(tt)
            story.append(Spacer(1, 5 * mm))

    # ── Issues & Violations page ───────────────────────────────────────────
    violations = data.get('violations', [])
    issues = data.get('issues', [])
    if violations or issues:
        story.append(PageBreak())
        hdr = Table([[Paragraph(
            '<font color="white" size="20"><b>ISSUES &amp; VIOLATIONS</b></font><br/>'
            f'<font color="#FFE0B2" size="11">FLAGGED FOR FOLLOW-UP · {date_str}</font>',
            ParagraphStyle('ivh', fontName=bold_font, leading=24))]], colWidths=[USABLE_W])
        hdr.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BAD),
            ('LEFTPADDING', (0, 0), (-1, -1), 16), ('RIGHTPADDING', (0, 0), (-1, -1), 16),
            ('TOPPADDING', (0, 0), (-1, -1), 14), ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ]))
        story.append(hdr)
        story.append(Spacer(1, 5 * mm))

        sev_col = {'major': '#C62828', 'serious': '#C62828', 'moderate': '#E65100', 'minor': '#F9A825'}
        if violations:
            story.append(Paragraph(f'STAFF VIOLATIONS ({len(violations)})', S['section']))
            vrows = [['Staff', 'Violation', 'Severity', 'Action / Status']]
            for v in violations:
                sc = sev_col.get((v.get('severity') or 'minor').lower(), '#F9A825')
                vrows.append([
                    Paragraph(_esc(v.get('staff_name') or '—'), S['body_b']),
                    Paragraph(_esc(v.get('rule_title') or v.get('description') or '—'), S['body']),
                    Paragraph(f'<font color="{sc}"><b>{_esc((v.get("severity") or "minor").upper())}</b></font>',
                              ParagraphStyle('sv', fontName=bold_font, fontSize=9)),
                    Paragraph(_esc((v.get('action_taken') or v.get('status') or '—')), S['small']),
                ])
            vt = Table(vrows, colWidths=[USABLE_W * 0.20, USABLE_W * 0.40,
                       USABLE_W * 0.14, USABLE_W * 0.26], repeatRows=1)
            vt.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), NAVY), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), bold_font), ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('GRID', (0, 0), (-1, -1), 0.3, SOFT),
                ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
                ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FFF8F6')]),
            ]))
            story.append(vt)
            story.append(Spacer(1, 6 * mm))

        if issues:
            story.append(Paragraph(f'REPORTED ISSUES ({len(issues)})', S['section']))
            irows = [['Title', 'Category', 'Priority', 'Status']]
            for it in issues:
                pr = (it.get('priority') or 'normal').lower()
                pc = '#C62828' if pr in ('urgent', 'high') else '#6E757E'
                irows.append([
                    Paragraph(_esc(it.get('title') or '—'), S['body_b']),
                    Paragraph(_esc(it.get('category') or '—'), S['small']),
                    Paragraph(f'<font color="{pc}"><b>{_esc(pr.upper())}</b></font>',
                              ParagraphStyle('ip', fontName=bold_font, fontSize=9)),
                    Paragraph(_esc((it.get('status') or 'open').upper()), S['small']),
                ])
            it_tbl = Table(irows, colWidths=[USABLE_W * 0.42, USABLE_W * 0.24,
                           USABLE_W * 0.16, USABLE_W * 0.18], repeatRows=1)
            it_tbl.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), NAVY), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), bold_font), ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('GRID', (0, 0), (-1, -1), 0.3, SOFT),
                ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
                ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FAFBFD')]),
            ]))
            story.append(it_tbl)

    # Empty-state if nothing today
    if (not data['checklists'] and not data['temperatures']
            and not (equip and equip.get('total')) and not violations and not issues
            and not prep_stations):
        story.append(PageBreak())
        story.append(Spacer(1, 60 * mm))
        story.append(Paragraph('No data recorded yet today.', S['section']))
        story.append(Paragraph(
            'Submit a checklist or a temperature record and re-export the PDF.',
            S['body']))

    # ── Build ──────────────────────────────────────────────────────────────
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=16 * mm, bottomMargin=12 * mm,
                            title='MCQ Daily Report')
    doc.build(story, onFirstPage=_draw_cover, onLaterPages=_draw_page)
    buf.seek(0)
    return buf.getvalue()


# ── PDF cache ────────────────────────────────────────────────────────────────
# Rendering the A4 PDF (ReportLab + embedded photos) is the expensive part of the
# daily share. Keep the last few rendered PDFs in memory keyed by (date, period)
# and reuse them until the day's data actually changes — detected by a cheap
# signature query — so opening the page, downloading and sharing don't each
# re-render the report from scratch.
_PDF_CACHE: dict = {}
_PDF_CACHE_MAX = 8
_PDF_PHOTO_CACHE: dict = {}
_PDF_PHOTO_CACHE_MAX = 96


def _remember_pdf_photo(key, data: bytes) -> BytesIO:
    _PDF_PHOTO_CACHE[key] = data
    if len(_PDF_PHOTO_CACHE) > _PDF_PHOTO_CACHE_MAX:
        for old_key in list(_PDF_PHOTO_CACHE.keys())[:-_PDF_PHOTO_CACHE_MAX]:
            _PDF_PHOTO_CACHE.pop(old_key, None)
    return BytesIO(data)


def _pdf_signature(date_str: str, period: str) -> str:
    """Cheap fingerprint of every record the PDF renders for (date, period).
    Changes on any save / edit / verify / new photo so a stale PDF is never
    served from cache."""
    period = _resolve_share_period(period)
    meta = SHARE_PERIODS[period]
    parts: list = []
    try:
        with _conn() as conn:
            parts.append(tuple(conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(submitted_at),''), "
                "COALESCE(MAX(verified_at),''), COALESCE(SUM(verified),0) "
                "FROM checklist_sessions WHERE date=? AND section=?",
                (date_str, period)).fetchone()))
            parts.append(tuple(conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(t.done),0), COALESCE(MAX(t.id),0) "
                "FROM checklist_tasks t JOIN checklist_sessions s ON s.id=t.session_id "
                "WHERE s.date=? AND s.section=?", (date_str, period)).fetchone()))
            parts.append(tuple(conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(p.id),0) "
                "FROM checklist_photos p JOIN checklist_sessions s ON s.id=p.session_id "
                "WHERE s.date=? AND s.section=?", (date_str, period)).fetchone()))
            if meta.get('includes_food_temps'):
                parts.append(tuple(conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(submitted_at),'') "
                    "FROM temp_sessions WHERE date=?", (date_str,)).fetchone()))
                parts.append(tuple(conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(r.id),0), "
                    "COALESCE(SUM(COALESCE(r.c1_temp,0)+COALESCE(r.c2_temp,0)+COALESCE(r.c3_temp,0)"
                    "+COALESCE(r.c4_temp,0)+COALESCE(r.c5_temp,0)),0) "
                    "FROM temp_readings r JOIN temp_sessions ts ON ts.id=r.session_id "
                    "WHERE ts.date=?", (date_str,)).fetchone()))
            parts.append(tuple(conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(morning_recorded_at),''), "
                "COALESCE(MAX(closing_recorded_at),''), "
                "COALESCE(SUM(COALESCE(morning_temp,0)+COALESCE(closing_temp,0)),0) "
                "FROM equipment_temp_readings WHERE date=?", (date_str,)).fetchone()))
            parts.append(tuple(conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id),0) "
                "FROM equipment_units WHERE active=1").fetchone()))
            # Prep timetable status — ticking a prep task OR editing a not-done
            # reason/note must refresh the PDF.
            try:
                parts.append(tuple(conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN status='done' THEN 1 ELSE 0 END),0), "
                    "COALESCE(MAX(done_at),''), COALESCE(SUM(issue_flag),0), "
                    "COALESCE(SUM(LENGTH(COALESCE(note,''))),0) "
                    "FROM prep_daily_status WHERE date=?",
                    (date_str,)).fetchone()))
            except Exception:
                parts.append(('prep?',))
            # Violations + issues rendered on the report.
            try:
                parts.append(tuple(conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(id),0) FROM staff_violations WHERE incident_date=?",
                    (date_str,)).fetchone()))
                parts.append(tuple(conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(id),0) FROM issue_reports WHERE date=?",
                    (date_str,)).fetchone()))
            except Exception:
                parts.append(('vi?',))
    except Exception:
        # On any probe failure, force a rebuild rather than risk a stale PDF.
        return f'err-{datetime.now().timestamp()}'
    return repr(parts)


def get_daily_pdf(date_str: str, period: str | None = None) -> bytes:
    """Return the daily PDF, re-rendering only when the day's data changed."""
    period = _resolve_share_period(period)
    key = (date_str, period)
    sig = _pdf_signature(date_str, period)
    hit = _PDF_CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    pdf = build_daily_pdf(date_str, period)
    _PDF_CACHE[key] = (sig, pdf)
    if len(_PDF_CACHE) > _PDF_CACHE_MAX:
        for old_key in list(_PDF_CACHE.keys())[:-_PDF_CACHE_MAX]:
            _PDF_CACHE.pop(old_key, None)
    return pdf


# ── Routes ───────────────────────────────────────────────────────────────────

@whatsapp_bp.route('/')
@_login_required
def whatsapp_today():
    from datetime import date
    today_str = date.today().isoformat()
    period = _resolve_share_period(request.args.get('period'))
    data = _collect_today(today_str, period)
    return render_template('whatsapp_share.html',
        date=today_str, data=data, period=period,
        period_meta=SHARE_PERIODS[period], share_periods=SHARE_PERIODS,
        cutoff_hour=SHARE_CUTOFF_HOUR,
        checklists_meta=CHECKLISTS_META,
        temperatures_meta=TEMPERATURES_META)


@whatsapp_bp.route('/pdf')
@_login_required
def whatsapp_pdf():
    """One polished A4 PDF report for the day — designed to share to WhatsApp."""
    from datetime import date
    date_str = request.args.get('date') or date.today().isoformat()
    period = _resolve_share_period(request.args.get('period'))
    try:
        pdf_bytes = get_daily_pdf(date_str, period)
    except Exception as e:
        return f'PDF generation failed: {type(e).__name__}: {e}', 500
    buf = BytesIO(pdf_bytes); buf.seek(0)
    return send_file(buf, mimetype='application/pdf',
                     as_attachment=False,
                     download_name=f'MCQ_{period.title()}_Report_{date_str}.pdf')


@whatsapp_bp.route('/png')
@_login_required
def whatsapp_png():
    from datetime import date
    date_str = request.args.get('date') or date.today().isoformat()
    period = _resolve_share_period(request.args.get('period'))
    try:
        png_bytes = build_daily_png(date_str, period)
    except Exception as e:
        return f'PNG generation failed: {type(e).__name__}: {e}', 500
    buf = BytesIO(png_bytes)
    buf.seek(0)
    return send_file(buf, mimetype='image/png',
                     as_attachment=False,
                     download_name=f'MCQ_{period.title()}_{date_str}.png')


@whatsapp_bp.route('/checklist/<int:session_id>.png')
@_login_required
def whatsapp_checklist_png(session_id):
    try:
        png_bytes = build_checklist_png(session_id)
    except ValueError as e:
        return str(e), 404
    except Exception as e:
        return f'PNG generation failed: {type(e).__name__}: {e}', 500
    buf = BytesIO(png_bytes); buf.seek(0)
    return send_file(buf, mimetype='image/png', as_attachment=False,
                     download_name=f'MCQ_Checklist_{session_id}.png')


@whatsapp_bp.route('/temperature/<int:session_id>.png')
@_login_required
def whatsapp_temperature_png(session_id):
    try:
        png_bytes = build_temperature_png(session_id)
    except ValueError as e:
        return str(e), 404
    except Exception as e:
        return f'PNG generation failed: {type(e).__name__}: {e}', 500
    buf = BytesIO(png_bytes); buf.seek(0)
    return send_file(buf, mimetype='image/png', as_attachment=False,
                     download_name=f'MCQ_Temperature_{session_id}.png')
