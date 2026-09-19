# -*- coding: utf-8 -*-
"""
复盘笔记系统 —— 把"事后复盘"变成可对账、可蒸馏的知识（2026-09-19 落地）

设计原则（docs/因子扩容与权重论证_20260918.md 第九章）：
  ① 可检索：结构化落库（不是写文章），LLM 只填 thesis / missed_risk /
     key_factors / prediction 四个字段；verdict 与 realized_outcome 由程序
     按客观涨跌写入，防止 LLM 自评自嗨。
  ② 可对照：每条笔记必须含次日方向预测（prediction），T+1 由 kline 回填
     realized_outcome 自动对账；周命中率 ≤ 50% 即熔断（复盘没有信息增量，
     停下调整 prompt 而不是继续堆笔记）。
  ③ 可升级：月度把 bad 笔记的 key_factors 聚类 → 候选规则 → 历史回测 →
     审批生效。LLM 产生假设，量化验证决定采纳。

子命令：
  add        写入一条笔记（由每日自动化/Agent 调用，每票一条）
  backfill   回填 realized_outcome 与 verdict（T+1 收盘后运行）
  stats      预测命中率统计（周检用；≤50% 输出熔断告警，退出码 1）
  similar    检索相似历史案例（key_factors 标签匹配，供次日复盘注入）
  candidates 月度蒸馏候选（bad 笔记的 key_factors 频次）

用法示例：
  python scripts/postmortem.py add --date 2026-09-19 --code 300001 --rank 1 \
      --thesis "机器人题材+缩量回踩" --key-factors "缩量,龙头,题材高位" \
      --prediction up --pred-confidence 0.6 --missed-risk "解禁压力"
  python scripts/postmortem.py backfill
  python scripts/postmortem.py stats --days 7
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, 'data', 'cache', 'postmortem_notes.db')
DEFAULT_KLINE_DB = os.path.join(PROJECT_ROOT, 'data', 'cache', 'kline_cache.db')

# verdict 阈值（T+1 收益 %）
GOOD_THRESHOLD = 0.5
BAD_THRESHOLD = -0.5
# 预测命中判定阈值（flat 口径）
FLAT_BAND = 0.5
# 熔断线：命中率 ≤ 50% 视为无信息增量
HIT_RATE_FLOOR = 0.50

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    rank_in_day INTEGER,
    thesis TEXT DEFAULT '',
    missed_risk TEXT DEFAULT '',
    key_factors TEXT DEFAULT '[]',
    prediction TEXT NOT NULL,
    pred_confidence REAL DEFAULT 0.5,
    llm_note TEXT DEFAULT '',
    realized_outcome REAL,
    verdict TEXT,
    created_at TEXT,
    UNIQUE(date, code)
);
"""


def get_conn(db_path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ── add ──────────────────────────────────────────────

def cmd_add(args) -> int:
    # 参数校验前置（不占用连接，避免早退路径泄漏）
    if args.prediction not in ('up', 'down', 'flat'):
        print(f"[error] prediction 必须是 up/down/flat，收到 {args.prediction!r}")
        return 2
    conn = get_conn(args.db)
    factors = [f.strip() for f in (args.key_factors or '').split(',') if f.strip()]
    try:
        conn.execute(
            "INSERT INTO notes (date, code, name, rank_in_day, thesis, missed_risk,"
            " key_factors, prediction, pred_confidence, llm_note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (args.date, args.code, args.name or '', args.rank,
             args.thesis or '', args.missed_risk or '', json.dumps(factors, ensure_ascii=False),
             args.prediction, args.pred_confidence, args.llm_note or '',
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        print(f"[ok] 笔记已落库 {args.date} {args.code} prediction={args.prediction}")
        return 0
    except sqlite3.IntegrityError:
        print(f"[skip] {args.date} {args.code} 已有笔记（UNIQUE 冲突），未覆盖")
        return 0
    finally:
        conn.close()


# ── backfill ─────────────────────────────────────────

def _next_close(kline_conn, code: str, date: str):
    """返回 (close_T, close_T+1)。T+1 取该股在 T 之后最近一个有成交日。"""
    row = kline_conn.execute(
        "SELECT close, date FROM kline_cache WHERE code=? AND date=?",
        (code, date)).fetchone()
    if row is None or not row['close']:
        return None
    nxt = kline_conn.execute(
        "SELECT close FROM kline_cache WHERE code=? AND date>? AND close>0"
        " ORDER BY date LIMIT 1", (code, date)).fetchone()
    if nxt is None:
        return None
    return float(row['close']), float(nxt['close'])


def _verdict_of(outcome: float) -> str:
    if outcome >= GOOD_THRESHOLD:
        return 'good'
    if outcome <= BAD_THRESHOLD:
        return 'bad'
    return 'neutral'


def cmd_backfill(args) -> int:
    if not os.path.exists(args.kline_db):
        print(f"[error] K线缓存不存在: {args.kline_db}")
        return 2
    conn = get_conn(args.db)
    kline = sqlite3.connect(args.kline_db)
    kline.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, date, code, prediction FROM notes WHERE realized_outcome IS NULL"
    ).fetchall()
    filled = skipped = 0
    for r in rows:
        pair = _next_close(kline, r['code'], r['date'])
        if pair is None:
            skipped += 1          # T 尚无 K 线或 T+1 未到 → 留待下次
            continue
        close_t, close_t1 = pair
        outcome = (close_t1 / close_t - 1) * 100.0   # 毛收益%，对账口径（不含成本）
        verdict = _verdict_of(outcome)
        conn.execute("UPDATE notes SET realized_outcome=?, verdict=? WHERE id=?",
                     (round(outcome, 4), verdict, r['id']))
        filled += 1
    conn.commit()
    conn.close()
    kline.close()
    print(f"[ok] 回填 {filled} 条 / 跳过 {skipped} 条（T+1 未到或无K线）")
    return 0


# ── stats（周检熔断闸门）──────────────────────────────

def _hit(pred: str, outcome: float) -> bool:
    if pred == 'up':
        return outcome > 0
    if pred == 'down':
        return outcome < 0
    return abs(outcome) <= FLAT_BAND


def cmd_stats(args) -> int:
    conn = get_conn(args.db)
    since = (datetime.now() - timedelta(days=args.days)).strftime('%Y-%m-%d')
    rows = conn.execute(
        "SELECT prediction, realized_outcome, verdict FROM notes"
        " WHERE realized_outcome IS NOT NULL AND date >= ?"
        " ORDER BY date DESC", (since,)).fetchall()
    all_rows = conn.execute(
        "SELECT COUNT(*) n FROM notes WHERE realized_outcome IS NOT NULL").fetchone()
    conn.close()
    n = len(rows)
    if n == 0:
        print(f"[stats] 近 {args.days} 天无可对账笔记（历史累计已对账 {all_rows['n']} 条）")
        return 0
    hits = sum(1 for r in rows if _hit(r['prediction'], r['realized_outcome']))
    rate = hits / n
    verdicts = Counter(r['verdict'] for r in rows)
    print(f"[stats] 近 {args.days} 天已对账 {n} 条 | 预测命中 {hits}/{n} = {rate:.1%}"
          f" | good/neutral/bad = {verdicts.get('good',0)}/{verdicts.get('neutral',0)}"
          f"/{verdicts.get('bad',0)}（历史累计 {all_rows['n']} 条）")
    for r in rows[:5]:
        print(f"    {r['prediction']:>4} vs {r['realized_outcome']:+.2f}%"
              f" → {'✓' if _hit(r['prediction'], r['realized_outcome']) else '✗'}")
    if rate <= HIT_RATE_FLOOR:
        print(f"[熔断告警] 命中率 {rate:.1%} ≤ {HIT_RATE_FLOOR:.0%}：复盘无信息增量，"
              f"应停下调整 prompt/维度，而不是继续堆笔记")
        return 1
    return 0


# ── similar（次日复盘的相似案例注入）──────────────────

def cmd_similar(args) -> int:
    conn = get_conn(args.db)
    query = set(f.strip() for f in args.key_factors.split(',') if f.strip())
    rows = conn.execute(
        "SELECT date, code, name, key_factors, prediction, realized_outcome,"
        " verdict, missed_risk FROM notes"
        " WHERE realized_outcome IS NOT NULL ORDER BY date DESC LIMIT 500").fetchall()
    conn.close()
    scored = []
    for r in rows:
        ks = set(json.loads(r['key_factors'] or '[]'))
        overlap = ks & query
        if overlap:
            scored.append((len(overlap), r))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        print("[similar] 无相似历史案例（key_factors 无交集）")
        return 0
    print(f"[similar] 与 {sorted(query)} 相关的历史案例（按标签重合度降序）：")
    for ov, r in scored[:args.limit]:
        ks = ', '.join(json.loads(r['key_factors']))
        print(f"  {r['date']} {r['code']} {r['name']} [{ks}] 预测={r['prediction']}"
              f" 实际={r['realized_outcome']:+.2f}% ({r['verdict']})"
              f" 重合={ov} 风险={r['missed_risk'][:40]}")
    return 0


# ── candidates（月度蒸馏候选）─────────────────────────

def cmd_candidates(args) -> int:
    conn = get_conn(args.db)
    month = args.month or datetime.now().strftime('%Y-%m')
    rows = conn.execute(
        "SELECT key_factors, missed_risk FROM notes"
        " WHERE verdict='bad' AND date LIKE ?", (month + '%',)).fetchall()
    conn.close()
    if not rows:
        print(f"[candidates] {month} 无 bad 笔记，无需蒸馏")
        return 0
    counter = Counter()
    for r in rows:
        for k in json.loads(r['key_factors'] or '[]'):
            counter[k] += 1
    print(f"[candidates] {month} bad 笔记 {len(rows)} 条的高频标签"
          f"（候选规则素材，须先历史回测再审批）：")
    for k, c in counter.most_common(10):
        print(f"  {k}: {c} 次")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='复盘笔记系统（可对账/可蒸馏）')
    sub = p.add_subparsers(dest='cmd', required=True)

    a = sub.add_parser('add', help='写入一条笔记')
    a.add_argument('--date', required=True, help='决策日 T（YYYY-MM-DD）')
    a.add_argument('--code', required=True)
    a.add_argument('--name', default='')
    a.add_argument('--rank', type=int, help='当日推荐排名')
    a.add_argument('--thesis', default='', help='当初推荐逻辑（从简报提取，不编造）')
    a.add_argument('--missed-risk', default='', help='LLM 识别的额外风险点')
    a.add_argument('--key-factors', default='', help='逗号分隔结构化标签')
    a.add_argument('--prediction', required=True, choices=['up', 'down', 'flat'])
    a.add_argument('--pred-confidence', type=float, default=0.5)
    a.add_argument('--llm-note', default='', help='LLM 自由补充（不可评估）')
    a.add_argument('--db', default=None)

    b = sub.add_parser('backfill', help='回填 T+1 实际收益与 verdict')
    b.add_argument('--db', default=None)
    b.add_argument('--kline-db', default=None)

    s = sub.add_parser('stats', help='预测命中率统计（熔断闸门）')
    s.add_argument('--days', type=int, default=7)
    s.add_argument('--db', default=None)

    m = sub.add_parser('similar', help='相似历史案例检索')
    m.add_argument('--key-factors', required=True)
    m.add_argument('--limit', type=int, default=3)
    m.add_argument('--db', default=None)

    c = sub.add_parser('candidates', help='月度蒸馏候选（bad 笔记标签频次）')
    c.add_argument('--month', default=None)
    c.add_argument('--db', default=None)

    args = p.parse_args(argv)
    return {'add': cmd_add, 'backfill': cmd_backfill, 'stats': cmd_stats,
            'similar': cmd_similar, 'candidates': cmd_candidates}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
