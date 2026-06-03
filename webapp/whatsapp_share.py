"""Daily WhatsApp share — composes a single PNG image summarising the day's
checklists (with photo thumbnails) and temperature records, ready to be
shared straight to a WhatsApp group via the browser's native share sheet.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from io import BytesIO

from flask import Blueprint, render_template, request, send_file, redirect, url_for, session
from functools import wraps

whatsapp_bp = Blueprint('whatsapp', __name__, url_prefix='/whatsapp')

DB_PATH: str | None = None
STATIC_DIR: str | None = None
UPLOAD_DIR: str | None = None
CHECKLISTS_META: dict = {}
TEMPERATURES_META: dict = {}


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

def _collect_today(date_str: str) -> dict:
    """Pull today's checklist + temperature submissions with photo paths."""
    out = {'date': date_str, 'checklists': [], 'temperatures': []}
    with _conn() as conn:
        # Checklists — one entry per (type, section) combination submitted today
        chk_rows = conn.execute('''
            SELECT cs.*,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id) AS total_tasks,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id AND done=1) AS done_tasks
            FROM checklist_sessions cs
            WHERE cs.date=?
            ORDER BY cs.type, cs.section
        ''', (date_str,)).fetchall()

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
        temp_rows = conn.execute('''
            SELECT ts.*,
                   (SELECT COUNT(*) FROM temp_readings WHERE session_id=ts.id) AS reading_count,
                   (SELECT COUNT(*) FROM temp_readings WHERE session_id=ts.id AND discarded='Y') AS discarded
            FROM temp_sessions ts WHERE ts.date=?
            ORDER BY ts.type
        ''', (date_str,)).fetchall()
        for r in temp_rows:
            r = dict(r)
            meta = TEMPERATURES_META.get(r['type'], {})
            # Out-of-zone count uses food_kind: cold→unsafe if >5, hot→unsafe if <60
            reading_rows = conn.execute('''
                SELECT tr.food_name, tr.c1_temp, tr.c2_temp, tr.c3_temp, tr.c4_temp, tr.c5_temp,
                       COALESCE(ft.food_kind, 'cold') AS kind
                FROM temp_readings tr
                LEFT JOIN temp_food_templates ft
                  ON ft.temp_type = ? AND ft.food_name = tr.food_name
                WHERE tr.session_id = ?''', (r['type'], r['id'])).fetchall()
            bad = 0
            for rr in reading_rows:
                kind = rr['kind'] or 'cold'
                for col in ('c1_temp', 'c2_temp', 'c3_temp', 'c4_temp', 'c5_temp'):
                    v = rr[col]
                    if v is None:
                        continue
                    if (kind == 'cold' and v > 5) or (kind == 'hot' and v < 60):
                        bad += 1
            r['out_of_zone'] = bad
            r['meta']        = meta
            out['temperatures'].append(r)
    return out


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


def build_daily_png(date_str: str) -> bytes:
    from PIL import Image, ImageDraw

    data = _collect_today(date_str)

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
    draw.text((text_x, 96), 'DAILY OPERATIONS SUMMARY',
              fill=(255, 255, 255, 200), font=f_sub)
    draw.text((text_x, 134), date_pretty, fill=(255, 200, 200), font=f_body_b)

    y = h_header + 24

    # ── KPI strip ─────────────────────────────────────────────────────────
    chk_done = sum(1 for c in data['checklists'] if c['done_tasks'] == c['total_tasks'] and c['total_tasks'] > 0)
    chk_late = sum(1 for c in data['checklists'] if c.get('is_late'))
    temp_done = len(data['temperatures'])
    temp_alerts = sum(c.get('out_of_zone', 0) for c in data['temperatures'])

    kpi_cards = [
        ('CHECKLISTS', f'{len(data["checklists"])}', (46, 125, 50)),
        ('FULLY DONE', f'{chk_done}', (33, 150, 83)),
        ('TEMPERATURE', f'{temp_done}', (216, 67, 21)),
        ('ALERTS', f'{temp_alerts + chk_late}', (198, 40, 40)),
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
    from PIL import Image, ImageDraw

    c = _checklist_detail(session_id)
    if c is None:
        raise ValueError(f'Checklist session {session_id} not found')

    W = 1080
    PAD = 32
    NAVY = (26, 26, 46)
    LIGHT_BG = (244, 246, 250)
    MUTED = (110, 117, 125)
    OK = (46, 125, 50)
    BAD = (198, 40, 40)
    PHOTO = 320

    color = _hex_to_rgb(c['meta'].get('color'))

    f_title  = _font(40, bold=True)
    f_sub    = _font(22)
    f_h2     = _font(28, bold=True)
    f_body   = _font(22)
    f_body_b = _font(22, bold=True)
    f_small  = _font(18)
    f_task   = _font(20)
    f_chip   = _font(16, bold=True)

    # Measure
    h_header  = 200
    h_meta    = 180
    h_tasks_row = 42
    h_tasks   = 70 + len(c['tasks']) * h_tasks_row
    # Photos: 2 per row at PHOTO height each
    photo_rows = (len(c['photos']) + 1) // 2
    h_photos  = (70 + photo_rows * (PHOTO + 24)) if c['photos'] else 0
    h_footer  = 90
    total_h = h_header + h_meta + h_tasks + h_photos + h_footer + 30

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
    title = (c['meta'].get('title') or c['type']).upper()
    section_label = (c['section'] or '').upper()
    draw.text((text_x, 36), f"{title}", fill=(255, 255, 255), font=f_title)
    draw.text((text_x, 88), f"{section_label} CHECKLIST",
              fill=(255, 255, 255, 220), font=f_sub)
    try:
        date_pretty = datetime.strptime(c['date'], '%Y-%m-%d').strftime('%A, %d %b %Y').upper()
    except Exception:
        date_pretty = c['date']
    draw.text((text_x, 124), date_pretty, fill=(255, 200, 200), font=f_body_b)

    y = h_header + 24

    # Meta card
    _rounded(draw, (PAD, y, W - PAD, y + h_meta - 24), radius=14, fill=(255, 255, 255))
    pct = round(c['done_tasks'] / c['total_tasks'] * 100) if c['total_tasks'] else 0
    pills = [
        (f'{c["done_tasks"]}/{c["total_tasks"]} TASKS', OK if pct >= 90 else (255, 152, 0)),
        (f'{pct}% COMPLETE', (33, 150, 243)),
        ('LATE' if c.get('is_late') else 'ON TIME', BAD if c.get('is_late') else OK),
    ]
    if c.get('verified'):
        pills.append(('VERIFIED', NAVY))

    px = PAD + 24
    for txt, col in pills:
        tw = draw.textbbox((0, 0), txt, font=f_chip)[2] + 24
        _rounded(draw, (px, y + 20, px + tw, y + 54), radius=14, fill=col)
        draw.text((px + 12, y + 28), txt, fill=(255, 255, 255), font=f_chip)
        px += tw + 8

    lines = [
        f"Submitted by: {c.get('submitted_by') or '-'}",
        f"Responsible: {c.get('responsible') or '-'}",
        f"Submitted at: {(c.get('submitted_at') or '-')[:16]}",
    ]
    if c.get('general_note'):
        lines.append(f"Note: {c['general_note']}")
    my = y + 70
    for line in lines:
        for sub in _wrap_text(draw, line, f_body, W - PAD * 2 - 56)[:1]:
            draw.text((PAD + 24, my), sub, fill=(51, 51, 51), font=f_body)
            my += 28
    y += h_meta

    # Tasks card
    _rounded(draw, (PAD, y, W - PAD, y + h_tasks - 12), radius=14, fill=(255, 255, 255))
    draw.text((PAD + 24, y + 18), 'TASKS', fill=NAVY, font=f_h2)
    ty = y + 60
    for i, t in enumerate(c['tasks']):
        # Status icon
        icon_x = PAD + 30
        if t['done']:
            _rounded(draw, (icon_x, ty + 6, icon_x + 22, ty + 28), radius=4, fill=OK)
            draw.text((icon_x + 4, ty + 4), '✓', fill=(255, 255, 255), font=f_chip)
            txt_color = (40, 40, 40)
        else:
            _rounded(draw, (icon_x, ty + 6, icon_x + 22, ty + 28), radius=4, fill=BAD)
            draw.text((icon_x + 5, ty + 4), '×', fill=(255, 255, 255), font=f_chip)
            txt_color = BAD
        # Task name (wrap-safe — truncate to one line)
        name = t['task_name']
        line = _wrap_text(draw, name, f_task, W - PAD * 2 - 100)[0]
        draw.text((icon_x + 36, ty + 8), line, fill=txt_color, font=f_task)
        if t.get('note'):
            note_line = _wrap_text(draw, f"Note: {t['note']}", f_small, W - PAD * 2 - 100)[0]
            # Render second line — we already reserved a uniform row height; if
            # the note pushes it taller it just overlaps the next row slightly,
            # acceptable trade-off for keeping the layout tidy.
            draw.text((icon_x + 36, ty + 22), note_line, fill=MUTED, font=f_small)
        ty += h_tasks_row
    y += h_tasks

    # Photos card
    if c['photos']:
        _rounded(draw, (PAD, y, W - PAD, y + h_photos - 12), radius=14, fill=(255, 255, 255))
        draw.text((PAD + 24, y + 18), f"PHOTOS ({len(c['photos'])})", fill=NAVY, font=f_h2)
        py = y + 60
        for ix, p in enumerate(c['photos']):
            col = ix % 2
            row = ix // 2
            px = PAD + 24 + col * (PHOTO + 24)
            ppy = py + row * (PHOTO + 24)
            src = os.path.join(UPLOAD_DIR, p['filename'])
            if not os.path.exists(src):
                _rounded(draw, (px, ppy, px + PHOTO, ppy + PHOTO),
                         radius=10, fill=(245, 245, 245))
                draw.text((px + 90, ppy + PHOTO // 2 - 12),
                          'photo missing', fill=MUTED, font=f_small)
                continue
            try:
                thumb = Image.open(src).convert('RGB')
                tw, th = thumb.size
                sz = min(tw, th)
                left = (tw - sz) // 2
                top  = (th - sz) // 2
                thumb = thumb.crop((left, top, left + sz, top + sz))
                thumb = thumb.resize((PHOTO, PHOTO), Image.LANCZOS)
                mask = Image.new('L', (PHOTO, PHOTO), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, PHOTO, PHOTO), radius=10, fill=255)
                img.paste(thumb, (px, ppy), mask)
            except Exception:
                _rounded(draw, (px, ppy, px + PHOTO, ppy + PHOTO),
                         radius=10, fill=(245, 245, 245))
        y += h_photos

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
        kind_label = 'HOT' if kind == 'hot' else 'COLD'
        kind_col   = (192, 57, 43) if kind == 'hot' else (21, 101, 192)
        kind_bg    = (255, 224, 178) if kind == 'hot' else (225, 245, 254)
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


# ── Routes ───────────────────────────────────────────────────────────────────

@whatsapp_bp.route('/')
@_login_required
def whatsapp_today():
    from datetime import date
    today_str = date.today().isoformat()
    data = _collect_today(today_str)
    return render_template('whatsapp_share.html',
        date=today_str, data=data,
        checklists_meta=CHECKLISTS_META,
        temperatures_meta=TEMPERATURES_META)


@whatsapp_bp.route('/png')
@_login_required
def whatsapp_png():
    from datetime import date
    date_str = request.args.get('date') or date.today().isoformat()
    try:
        png_bytes = build_daily_png(date_str)
    except Exception as e:
        return f'PNG generation failed: {type(e).__name__}: {e}', 500
    buf = BytesIO(png_bytes)
    buf.seek(0)
    return send_file(buf, mimetype='image/png',
                     as_attachment=False,
                     download_name=f'MCQ_Daily_{date_str}.png')


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
