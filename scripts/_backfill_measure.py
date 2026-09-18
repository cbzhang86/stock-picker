"""回填计量辅助（只读快照 + 前后 diff），不改动任何业务数据。

用途：pick.py backfill 只打印"自动回填 N 条"，不区分成功/失败/滞留。
本脚本在回填前后各取一次快照，diff 出真正的成功条数。
"""
import json
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(__file__), '..', 'data', 'db', 'predictions.db')


def snapshot():
    c = sqlite3.connect(DB)
    rows = {}
    for pid, pdate, code, t1, t5, t20, st, upd in c.execute(
            """SELECT p.id, p.date, p.code, o.t1_close, o.t5_close, o.t20_close,
                      o.status, o.updated_at
               FROM predictions p LEFT JOIN outcomes o ON p.id = o.prediction_id"""):
        rows[pid] = {
            'date': pdate, 'code': code,
            't1': t1, 't5': t5, 't20': t20,
            'status': st, 'updated_at': upd,
        }
    att = dict(c.execute(
        "SELECT id, backfill_attempts FROM predictions").fetchall())
    c.close()
    return rows, att


def pending_ids(rows):
    out = []
    for pid, r in rows.items():
        if r['status'] == 'no_data':
            continue
        if r['t1'] is None or r['t5'] is None or r['t20'] is None:
            out.append(pid)
    return out


def main():
    mode = sys.argv[1]
    out = sys.argv[2]
    rows, att = snapshot()
    payload = {
        'rows': {str(k): v for k, v in rows.items()},
        'att': {str(k): v for k, v in att.items()},
        'pending': pending_ids(rows),
    }
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[{mode}] total={len(rows)} pending={len(payload['pending'])}")


if __name__ == '__main__':
    main()
