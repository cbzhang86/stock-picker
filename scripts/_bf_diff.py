"""回填前后 diff：算 N(成功补齐) / M(失败) / K(滞留>20 自然日)。"""
import json
import os
import sys
import sqlite3
from datetime import date, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
BEFORE = os.path.join(BASE, '..', 'data', 'cache', '_bf_before.json')
AFTER = os.path.join(BASE, '..', 'data', 'cache', '_bf_after.json')
OUT = os.path.join(BASE, '..', 'data', 'cache', '_bf_diff.json')


def load(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def trading_days(db, start, end):
    """用 kline 表近似交易日历：取一只长期有数据的标的不可靠，改用 distinct date。"""
    c = sqlite3.connect(db)
    try:
        rows = [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM predictions ORDER BY date")]
        return rows
    finally:
        c.close()


def main():
    b = load(BEFORE)
    a = load(AFTER)
    today = date.today()

    filled = []      # N: 字段由 NULL -> 有值
    touched = []     # updated_at 变化
    failed = []      # M: attempts 递增 或 字段回退/异常
    for pid, ar in a['rows'].items():
        br = b['rows'].get(pid)
        if br is None:
            continue
        gained = []
        for f in ('t1', 't5', 't20'):
            if br[f] is None and ar[f] is not None:
                gained.append(f)
        if gained:
            filled.append({'id': pid, 'code': ar['code'], 'date': ar['date'],
                           'gained': gained})
        if br['updated_at'] != ar['updated_at']:
            touched.append(pid)
        ba = b['att'].get(pid)
        aa = a['att'].get(pid)
        if ba is not None and aa is not None and aa > ba:
            failed.append({'id': pid, 'code': ar['code'], 'date': ar['date'],
                           'att': f'{ba}->{aa}'})

    # K: pending 且 date 距今 > 20 个自然日
    pending = sorted({str(p) for p in a['pending']},
                     key=lambda x: a['rows'][x]['date'])
    klist = []
    for pid in pending:
        r = a['rows'][pid]
        try:
            d = datetime.strptime(r['date'], '%Y-%m-%d').date()
        except Exception:
            continue
        age = (today - d).days
        if age > 20:
            miss = [f for f in ('t1', 't5', 't20') if r[f] is None]
            klist.append({'id': pid, 'code': r['code'], 'date': r['date'],
                          'age': age, 'missing': miss})

    # pending 明细（全量，便于判断是否未到期）
    pend_detail = []
    for pid in pending:
        r = a['rows'][pid]
        try:
            d = datetime.strptime(r['date'], '%Y-%m-%d').date()
            age = (today - d).days
        except Exception:
            age = -1
        pend_detail.append({'id': pid, 'code': r['code'], 'date': r['date'],
                            'age': age,
                            'missing': [f for f in ('t1', 't5', 't20') if r[f] is None],
                            'status': r['status']})

    payload = {
        'N': len(filled), 'M': len(failed), 'K': len(klist),
        'filled': filled, 'failed': failed, 'klist': klist,
        'pending_before': len(b['pending']), 'pending_after': len(a['pending']),
        'touched': len(touched),
        'pend_detail': pend_detail,
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in payload.items()
                      if k in ('N', 'M', 'K', 'pending_before', 'pending_after', 'touched')},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
