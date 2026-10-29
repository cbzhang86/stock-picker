# -*- coding: utf-8 -*-
"""每日统一调度入口（2026-09-06 性能与稳定性迭代新增）

设计目标：一条定时任务覆盖所有场景，交易日判断全部内聚在脚本内，
cron/自动化任务只需"每天固定时间叫醒"，无需自己区分交易日/非交易日。

逻辑：
  1. 交易日 14:50 场景   → 尾盘选股（eod_stock_picker --mode short）
  2. 非交易日任意时段    → ASHareHub 闲置配额预取（prefetch_asharehub.py）
  3. 日历不可用（兜底）  → 工作日按选股处理（--skip-non-trading-day 防污染落库），
                           周末按预取处理——保守且不产生错误数据。

用法：
  python scripts/daily_job.py [--time-mode auto|eod|prefetch]
    --time-mode auto     默认：交易日→eod，非交易日→prefetch（按当前小时微调）
    --time-mode eod      强制走选股（仍带 --skip-non-trading-day 保险）
    --time-mode prefetch 强制走预取（脚本内置交易日守卫，交易日会自动跳过）

退出码：0=成功，1=失败（调度器可感知）。

2026-09-16 P2-2 新增（当日事故驱动）：
  - **运行日志落盘**：每次执行把子进程 stdout/stderr 实时写入
    `data/logs/daily_job_YYYYMMDD.log`（含命令、耗时、退出码），保留 30 天。
    此前只有 stdout，一次崩溃的定位成本 = 重跑整条流水线（含配额，当日为此
    多烧 2 次全量运行的 AShareHub 配额）。
  - **配额预检**：eod 路径启动前读 AShareHub 配额账本（只读、不消耗），
    余量不足时明确告警——避免"重试把整日配额烧光后静默产出降级结果"。
  - **失败不自动重试**（有意为之，非缺陷）：同一配额日内自动重试会挤占
    eod 自身的配额，2026-09-16 的降级事故正是这么发生的。失败即退出码 1，
    交由人工判断。
  - **幂等**：由 `eod_stock_picker.run_short_term/long_term` 的批次级防重
    实现（`PredictionTracker.has_predictions(date, mode)`，唯一索引
    `(date, code, mode)`），当日已落库则跳过重复写入，本脚本不重复实现。

注：微信推送不在本脚本内嵌——推送通道属于宿主环境（WorkBuddy ClawBot），
    由自动化任务的提示词负责调用，保持本仓库与具体 harness 解耦
    （2026-09-07 曾短暂内嵌后按用户要求撤除）。
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('daily_job')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(PROJECT_ROOT, 'core')):
    PROJECT_ROOT = r'C:\Users\Administrator\Documents\stock-picker-v2'
sys.path.insert(0, PROJECT_ROOT)

PY311 = r'C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe'
PY = PY311 if os.path.exists(PY311) else sys.executable

# 运行日志（2026-09-16 P2-2）
LOG_DIR = os.path.join(PROJECT_ROOT, 'data', 'logs')
LOG_KEEP_DAYS = 30
# AShareHub 日配额与本地安全闸门（与 data_engine._asharehub_budget 保持一致：
# 闸门 = budget - 10，预留 10 次给手动验证）
ASHAREHUB_BUDGET = 100
# 2026-09-23（交接单 A6）：预留量与 DataEngine.ASHAREHUB_RESERVE 必须一致。
# 此处保留独立常量（daily_job 不 import data_engine，避免拉起其依赖），
# 但由 tests/test_quota_reserve_consistency_20260923.py 守卫二者相等。
QUOTA_RESERVE = 10
QUOTA_FILE = os.path.join(PROJECT_ROOT, 'data', 'cache', 'asharehub_quota.json')

# 2026-09-22 新增（缺陷排查 P2-21）：子进程看门狗上限（秒）。
# 参考耗时：eod_stock_picker ~250s、周日全市场 K 线预取可达数十分钟
# → 默认 3600s 只用于兜底"网络挂死导致永久阻塞"，正常任务不会触碰。
# 置 0 或负数可禁用（不推荐）。可用环境变量 DAILY_JOB_CHILD_TIMEOUT 覆盖。
try:
    _CHILD_TIMEOUT_S = int(os.environ.get('DAILY_JOB_CHILD_TIMEOUT', '3600'))
except ValueError:
    _CHILD_TIMEOUT_S = 3600


def _beijing_now() -> datetime:
    try:
        from core.trading_calendar import beijing_now
        return beijing_now()
    except Exception:
        return datetime.now()


def _log_path() -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, f"daily_job_{_beijing_now():%Y%m%d}.log")


def _prune_logs() -> None:
    """清理超过保留期的运行日志（best-effort，失败不影响主流程）"""
    try:
        cutoff = time.time() - LOG_KEEP_DAYS * 86400
        for fn in os.listdir(LOG_DIR):
            if not (fn.startswith('daily_job_') and fn.endswith('.log')):
                continue
            p = os.path.join(LOG_DIR, fn)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                logger.info(f"清理过期运行日志: {fn}")
    except Exception as e:
        logger.warning(f"清理运行日志失败（忽略）: {e}")


def _read_quota():
    """只读 AShareHub 配额账本（不消耗配额）。返回 (used, budget) 或 None

    2026-09-23（交接单 A2）：账本新增 `owner` / `last_ts` 元数据字段后，
    本函数额外通过 `_read_quota_owner()` 暴露归属（旧账本无该字段 → unknown）。
    """
    try:
        with open(QUOTA_FILE, encoding='utf-8') as f:
            d = json.load(f)
    except Exception as e:
        logger.warning(f"配额账本读取失败（按未知处理）: {str(e)[:60]}")
        return None
    if d.get('date') != _beijing_now().strftime('%Y-%m-%d'):
        return 0, ASHAREHUB_BUDGET      # 跨日账本：视为今日尚未消耗
    try:
        return int(d.get('used', 0)), ASHAREHUB_BUDGET
    except (TypeError, ValueError):
        return None


def _read_quota_owner() -> str:
    """读取配额账本归属（2026-09-23 交接单 A2）；旧账本/读失败 → 'unknown'。"""
    try:
        with open(QUOTA_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return str(d.get('owner') or 'unknown')
    except Exception:
        return 'unknown'


def _precheck_quota() -> None:
    """eod 启动前的配额预检（2026-09-16 P1-1 / P2-2；2026-09-23 交接单 A5 升级）

    刻意只告警不阻断：若在此中止 eod，当日不会生成任何新简报，自动化提示词
    "找不到当日简报就取最近一份"会退化为推送**昨日**简报——比"有明确降级
    标注的低可信结果"更危险。降级可见性由简报首屏的「⚠️ 数据源降级警示」
    保证（market_briefing），本预检负责把它提前写进运行日志、便于事后取证。

    2026-09-23（交接单 A5 + A2）升级内容：
      · 闸门命中时**显式标注本次结果不可用于决策**（不只是"可信度下降"），
        并量化受影响权重（hot_theme 0.55 + technical 0.02 + capital_flow 0.01
        ≈ 0.58），使日志本身就带严重度；
      · 输出**配额归属**（账本 owner 字段）：直接回答"额度被谁吃光"，
        不再需要靠文件 mtime 反推（A2 的落地价值）；
      · 单次 eod 设计预算 55（moneyflow10+tech10+concepts20+financial15）与
        预取预算 90 之和 145 > 日配额 100 —— 二者**不可同日共存**，闸门命中时
        明确提示检查"是否有回测/验证/预取与生产抢额度"。
    """
    q = _read_quota()
    if q is None:
        return
    used, budget = q
    gate = budget - QUOTA_RESERVE
    owner = _read_quota_owner()
    if used >= gate:
        logger.error(
            f"⚠️ AShareHub 配额已用 {used}/{budget}（本地闸门 {gate}，预留 "
            f"{QUOTA_RESERVE} 次；账本归属 owner={owner}）→ 本次 eod 的 AShareHub "
            f"源将降级为中性（受影响因子权重合计约 58%，其中 hot_theme 0.55）"
            f"→ **本次推荐结果不可用于决策**，简报首屏会标注降级。"
            f"请检查是否有回测/验证/预取与生产抢额度"
            f"（eod 需 55、预取已改为**按当日剩余额度动态收缩**并给 eod 预留 "
            f"{QUOTA_RESERVE + 55} 次，日配额 {budget}）。")
    elif used >= gate * 0.6:
        logger.warning(
            f"AShareHub 配额已用 {used}/{budget}（闸门 {gate}，owner={owner}），"
            f"余量偏低，本次 eod 后续可能降级")


def _run(script: str, extra_args: list = None) -> int:
    """执行子脚本：实时透传输出到控制台，同时落盘运行日志

    失败语义（重要）：**任何情况下都不会把已启动的子进程再跑一遍**。
    落盘只是为了可追溯，不能因为日志写入异常而重复消耗 AShareHub 配额
    （重复执行正是 2026-09-16 降级事故的成因）。
    """
    cmd = [PY, os.path.join(PROJECT_ROOT, 'scripts', script)] + (extra_args or [])
    logger.info(f"执行: {' '.join(cmd)}")
    t0 = time.time()
    log_path = _log_path()
    # 子进程统一 utf-8 输出，避免 Windows 控制台代码页（cp936）与日志文件编码错配
    child_env = dict(os.environ)
    child_env.setdefault('PYTHONIOENCODING', 'utf-8')

    fh = None
    try:
        fh = open(log_path, 'a', encoding='utf-8')
    except Exception as e:
        logger.warning(f"运行日志不可写（本次仅输出到控制台）: {e}")

    rc = None
    proc = None
    started = False
    try:
        if fh:
            fh.write(f"\n{'=' * 72}\n"
                     f"[{_beijing_now():%Y-%m-%d %H:%M:%S}] 执行: {' '.join(cmd)}\n"
                     f"{'=' * 72}\n")
            fh.flush()
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT, env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1)
        started = True
        # 2026-09-22 修复（缺陷排查 P2-21）：**子进程看门狗超时**。
        # 原实现无任何超时：子进程若在网络挂死（非交易时段实时行情接口无响应
        # 等）会让 `for line in proc.stdout` 永久阻塞 → 调度器层面"看起来在跑"，
        # 实际当天无推送；且异常分支只记日志、不 kill 子进程，孤儿进程会与
        # 下一次 cron 并发写库/烧配额。
        # 用 watchdog 定时线程实现 kill（流式读取无法用 subprocess 的 timeout）。
        _watchdog = None
        if _CHILD_TIMEOUT_S and _CHILD_TIMEOUT_S > 0:
            import threading as _th

            def _kill_on_timeout():
                logger.error(f"{script} 超过看门狗上限 {_CHILD_TIMEOUT_S}s → 强制终止"
                             f"（按设计不重跑）")
                try:
                    proc.kill()
                except Exception:
                    pass

            _watchdog = _th.Timer(_CHILD_TIMEOUT_S, _kill_on_timeout)
            _watchdog.daemon = True
            _watchdog.start()
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                if fh:
                    fh.write(line)
            proc.wait()
        finally:
            if _watchdog is not None:
                _watchdog.cancel()
            if proc.stdout:
                proc.stdout.close()
        rc = proc.returncode
    except Exception as e:
        if started:
            # 进程已启动 → 绝不重跑，仅记录并沿用其退出码
            logger.error(f"日志写入异常（进程已启动，不重跑）: {e}")
            rc = proc.returncode if proc is not None and proc.returncode is not None else 1
        else:
            logger.warning(f"带日志执行失败（回退普通执行）: {e}")
            rc = subprocess.call(cmd, cwd=PROJECT_ROOT)
    finally:
        dur = time.time() - t0
        if fh:
            try:
                fh.write(f"[{_beijing_now():%Y-%m-%d %H:%M:%S}] {script} "
                         f"退出码: {rc} | 耗时 {dur:.1f}s\n")
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass

    logger.info(f"{script} 退出码: {rc} | 耗时 {dur:.1f}s | 日志: {log_path}")
    if rc != 0:
        # 明确不自动重试：同一配额日内重试会挤占 eod 自身配额（2026-09-16 事故）
        logger.error(f"{script} 执行失败（退出码 {rc}）。按设计**不自动重试**，"
                     f"请查看 {log_path} 定位原因后再人工决定是否重跑。")
    return rc


def _run_eod() -> int:
    """交易日尾盘选股（含配额预检）"""
    _precheck_quota()
    return _run('eod_stock_picker.py', ['--mode', 'short'])


# ---- T10（2026-09-17）：周日全市场 K 线预取调度 ----
def _is_sunday(date_str: str) -> bool:
    """判断给定日期是否为周日（weekday()==6）。非法日期回退 False。"""
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').weekday() == 6
    except Exception:
        return False


def _maintenance_flag(key: str, default: bool = True) -> bool:
    """读取 config.yml 的 maintenance.<key> 开关（默认按 default）。

    读取/解析失败时返回 default（保守：数据积累类任务宁开不关）。
    """
    try:
        import yaml
        cfg_path = os.path.join(PROJECT_ROOT, 'config.yml')
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            return bool(cfg.get('maintenance', {}).get(key, default))
    except Exception:
        pass
    return default


def _weekly_kline_prefetch_enabled() -> bool:
    """读取 config.yml 的 maintenance.weekly_kline_prefetch（默认开）。

    读取/解析失败时保守返回 True（周日仍执行预取，防 kline_cache 塌缩优先于关闭开关）。
    """
    return _maintenance_flag('weekly_kline_prefetch', True)



def main() -> int:
    parser = argparse.ArgumentParser(description='每日统一调度入口')
    parser.add_argument('--time-mode', choices=['auto', 'eod', 'prefetch'], default='auto')
    args = parser.parse_args()

    _prune_logs()

    # 加载 ASHAREHUB_API_KEY（cron 环境不 source bashrc）
    if not os.environ.get('ASHAREHUB_API_KEY'):
        try:
            import re
            for p in (os.path.expanduser('~/.bashrc'), '/c/Users/Administrator/.bashrc'):
                try:
                    with open(p, encoding='utf-8') as f:
                        _src = f.read()
                    # 2026-09-22 修复（缺陷排查 P2-23）：原正则会在**注释行**里命中
                    # （如 `# export ASHAREHUB_API_KEY=旧值`）→ 加载已失效的 key，
                    # 随后 ASHAREHub 四源静默降级。改为锚定行首的可执行 export/赋值。
                    m = None
                    for _line in _src.splitlines():
                        if _line.lstrip().startswith('#'):
                            continue
                        m = re.match(
                            r'\s*(?:export\s+)?ASHAREHUB_API_KEY\s*[=:]\s*["\']?([^"\'\s#]+)',
                            _line)
                        if m:
                            break
                    if m:
                        os.environ['ASHAREHUB_API_KEY'] = m.group(1)
                        break
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"ASHAREHUB_API_KEY 加载失败: {e}")

    from core.trading_calendar import beijing_now
    today = beijing_now().strftime('%Y-%m-%d')

    # 2026-09-18 审查 P1-3：predictions.db 每日备份（实盘唯一真源，此前无任何备份）。
    # 放在主任务**之前**执行——即使当日选股流程失败/中断，也保证有可恢复快照。
    # 失败仅 warning（备份属保障性任务，不应阻断选股）。开关 maintenance.daily_db_backup。
    if _maintenance_flag('daily_db_backup', True):
        logger.info("追加 predictions.db 每日备份（P1-3 修复）")
        brc = _run('backup_predictions.py', ['--keep', '14'])
        if brc != 0:
            logger.warning(f"predictions.db 备份返回 {brc}（不影响主任务）")

    if args.time_mode == 'eod':
        main_rc = _run_eod()
    elif args.time_mode == 'prefetch':
        main_rc = _run('prefetch_asharehub.py')
    else:
        # auto：按交易日历分流
        try:
            from core.trading_calendar import is_trading_day, next_trading_day
            is_td = is_trading_day(today)
        except Exception as e:
            logger.warning(f"交易日历不可用（{e}），按兜底逻辑处理")
            is_td = None

        if is_td is True:
            logger.info(f"{today} 为交易日 → 尾盘选股")
            main_rc = _run_eod()
        elif is_td is False:
            nxt = next_trading_day(today)
            logger.info(f"{today} 为非交易日（下一交易日 {nxt}）→ ASHareHub 闲置配额预取")
            main_rc = _run('prefetch_asharehub.py')
        else:
            # 日历不可用的兜底：周末跑预取（安全、有价值）；工作日跑选股但防污染落库
            wd = datetime.now().weekday()
            if wd >= 5:
                logger.warning(f"日历不可用且今天为周末 → 预取（保守兜底）")
                main_rc = _run('prefetch_asharehub.py')
            else:
                logger.warning("日历不可用且今天为工作日 → 选股（--skip-non-trading-day 防污染）")
                main_rc = _run_eod()

    # T10（2026-09-17）：周日全市场 K 线预取，防 kline_cache 覆盖塌缩。
    # 在现有主任务之后追加；prefetch 脚本自带断点续传（跳过 7 天内已抓的票），
    # 实际增量小。开关 maintenance.weekly_kline_prefetch 默认开。
    if _is_sunday(today) and _weekly_kline_prefetch_enabled():
        logger.info("周日 → 追加全市场 K 线预取（防 kline_cache 覆盖塌缩）")
        krc = _run('prefetch_kline_fullmarket.py', ['--workers', '4'])
        # 主任务失败时优先保留其退出码；主任务成功时以预取结果为准
        main_rc = main_rc if main_rc != 0 else krc

    # P5（2026-09-18）：每日市值/估值快照落库（腾讯行情，免费、零 ASHareHub 配额）——
    # 为 size（精确市值）/PE/PB 因子积累 point-in-time 日频历史，≥60 交易日后可进 OOS
    # 验证 → 权重审批。开关 maintenance.daily_valuation_snapshot（默认开）。
    # 失败不影响主任务退出码（数据积累类任务不应掩盖选股结果）。
    if _maintenance_flag('daily_valuation_snapshot', True):
        logger.info("追加每日市值/估值快照（P5 数据线）")
        vrc = _run('snapshot_valuation_daily.py')
        if vrc != 0:
            logger.warning(f"估值快照写入返回 {vrc}（不影响主任务结果）")

    # 月度滚动复检（2026-09-18 P5③）：每月 1 日执行 IC 趋势检测 + 降权提案。
    # 开关 maintenance.monthly_review（默认开）。失败不影响主任务退出码。
    if datetime.now().day == 1 and _maintenance_flag('monthly_review', True):
        logger.info("每月 1 日 → IC 月度滚动复检（降权提案，只读不自动改权重）")
        mrc = _run('monthly_review.py')
        if mrc != 0:
            logger.warning(f"月度复检返回 {mrc}（不影响主任务结果）")

    return main_rc


if __name__ == '__main__':
    sys.exit(main())
