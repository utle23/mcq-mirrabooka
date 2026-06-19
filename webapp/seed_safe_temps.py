#!/usr/bin/env python3
"""One-time seed: fill SAFE temperature readings for chef / pastry / banh_mi.

Fills checks 1, 2 and 3 for every day from 2026-06-01 to today, for store 1
(Mirrabooka), with values comfortably inside each food's safe range:

    cold  → ~1.5–4.4 °C   (rule: ≤ 5 °C)
    room  → ~19–28 °C     (rule: 15–30 °C)
    hot   → ~62–72 °C     (rule: ≥ 60 °C)

Only fills a check that is currently EMPTY — any reading already entered (e.g.
production already has "check 1") is left untouched. Idempotent: running it
again does nothing new. Run from the webapp/ folder:  python3 seed_safe_temps.py
"""
import sqlite3
import os
import hashlib
from datetime import date, timedelta

DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mcq_restaurant.db')
START    = date(2026, 6, 1)
END      = date.today()
STORE_ID = 1                       # Mirrabooka
TYPES    = ['chef', 'pastry', 'banh_mi']
CHECKS   = ('c1', 'c2', 'c3')
CHECK_TIMES = {'c1': '08:00', 'c2': '11:00', 'c3': '14:00'}


def safe_val(kind, seed):
    """A varied-but-safe value, deterministic from `seed` (so re-runs match)."""
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    if kind == 'room':
        return round(19.0 + (h % 90) / 10.0, 1)   # 19.0 – 27.9
    if kind == 'hot':
        return round(62.0 + (h % 100) / 10.0, 1)  # 62.0 – 71.9
    return round(1.5 + (h % 30) / 10.0, 1)         # cold: 1.5 – 4.4


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 20000')

    sessions_made = 0
    checks_filled = 0
    d = START
    while d <= END:
        ds = d.isoformat()
        for t in TYPES:
            foods = conn.execute(
                'SELECT food_order, food_name, food_kind FROM temp_food_templates '
                'WHERE temp_type=? ORDER BY food_order', (t,)).fetchall()
            if not foods:
                continue

            sess = conn.execute(
                'SELECT id FROM temp_sessions WHERE type=? AND date=? AND store_id=?',
                (t, ds, STORE_ID)).fetchone()
            if sess:
                sid = sess['id']
            else:
                sid = conn.execute(
                    'INSERT INTO temp_sessions (type,date,recorded_by,checked_by,notes,store_id) '
                    "VALUES (?,?,'','','',?)", (t, ds, STORE_ID)).lastrowid
                sessions_made += 1

            for f in foods:
                kind = f['food_kind'] or 'cold'
                r = conn.execute(
                    'SELECT id, c1_temp, c2_temp, c3_temp, c1_time, c2_time, c3_time, '
                    "COALESCE(defrosted,'N') AS defrosted "
                    'FROM temp_readings WHERE session_id=? AND food_name=?',
                    (sid, f['food_name'])).fetchone()

                if r and r['defrosted'] == 'Y':
                    continue  # defrosting items legitimately have no temperature

                if not r:
                    vals = {ck: safe_val(kind, f'{ds}-{t}-{f["food_name"]}-{ck}') for ck in CHECKS}
                    conn.execute(
                        'INSERT INTO temp_readings '
                        '(session_id,food_order,food_name,c1_time,c1_temp,c2_time,c2_temp,'
                        "c3_time,c3_temp,discarded,defrosted) VALUES (?,?,?,?,?,?,?,?,?,'N','N')",
                        (sid, f['food_order'], f['food_name'],
                         CHECK_TIMES['c1'], vals['c1'], CHECK_TIMES['c2'], vals['c2'],
                         CHECK_TIMES['c3'], vals['c3']))
                    checks_filled += 3
                else:
                    sets, params = [], []
                    for ck in CHECKS:
                        if r[f'{ck}_temp'] is None:
                            sets.append(f'{ck}_temp=?')
                            params.append(safe_val(kind, f'{ds}-{t}-{f["food_name"]}-{ck}'))
                            if not r[f'{ck}_time']:
                                sets.append(f'{ck}_time=?')
                                params.append(CHECK_TIMES[ck])
                            checks_filled += 1
                    if sets:
                        params.append(r['id'])
                        conn.execute(
                            f'UPDATE temp_readings SET {", ".join(sets)} WHERE id=?', params)
        d += timedelta(days=1)

    conn.commit()
    conn.close()
    print(f'Done {START} → {END}: sessions created = {sessions_made}, '
          f'empty checks filled = {checks_filled}')


if __name__ == '__main__':
    main()
