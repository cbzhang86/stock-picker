"""
回测入口

用法：
  # 短线策略回测（默认）
  python scripts/run_backtest.py

  # 指定区间
  python scripts/run_backtest.py --start 2026-01-01 --end 2026-06-11

  # 长线策略
  python scripts/run_backtest.py --mode long

  # 回测 + 对比
  python scripts/run_backtest.py --compare 1 2

  # 查看历史回测记录
  python scripts/run_backtest.py --list

  # 保存回测报告
  python scripts/run_backtest.py --out reports/backtest.md
"""

import argparse
import json
import logging
import sys
import os

# 全局socket超时15秒：回测缺缓存时会走网络源，非交易时段部分实时接口
# （腾讯/同花顺）可能挂死无响应——与 eod_stock_picker.py 同款防护。
# 2026-09-05 全量验证发现：周六凌晨跑回测进程挂 30 分钟（CPU 仅 7 分钟）。
import socket; socket.setdefaulttimeout(15)

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import yaml

from core.backtest_engine import BacktestEngine
from core.backtest_store import BacktestStore
from reports.backtest_report import generate_backtest_report

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='A股选股策略回测')
    parser.add_argument('--mode', choices=['short', 'long', 'kfactor', 'oos'], default='short',
                        help='策略模式: short(短线) / long(长线) / kfactor(K线因子回测) / oos(样本外因子IC验证)')
    parser.add_argument('--start', default='2026-04-01', help='开始日期')
    parser.add_argument('--end', default='2026-06-27', help='结束日期')
    parser.add_argument('--out', help='输出文件路径')
    parser.add_argument('--config', default='config.yml', help='配置文件')
    parser.add_argument('--list', action='store_true', help='查看历史回测记录')
    parser.add_argument('--compare', nargs=2, type=int, metavar=('RUN_ID_A', 'RUN_ID_B'),
                        help='对比两次回测记录')
    parser.add_argument('--train-ratio', type=float, default=0.7,
                        help='样本外验证的训练集时间占比（默认 0.7）')
    parser.add_argument('--folds', type=int, default=3,
                        help='walk-forward 折数（默认 3）')
    parser.add_argument('--ablate', action='append', default=None,                        metavar='FACTOR',
                        help='消融实验：移除指定因子（权重置 0，剩余重新归一化）。'
                             '可重复或逗号分隔，如 --ablate hot_theme 或 '
                             '--ablate hot_theme,momentum。与 --start/--end 组合使用。')
    parser.add_argument('--ret', choices=['hold1d', 'intraday', 'overnight'],
                        default='hold1d',
                        help='收益口径: hold1d=尾盘买→T+1收盘卖(默认) / '
                             'intraday=T+1开盘买→收盘卖 / overnight=隔夜跳空(旧kfactor口径)')
    # P1（2026-09-18）：卖出规则参数覆盖——出场网格实验用，仅作用于本次运行，
    # 不修改 config.yml。与 --start/--end 组合使用。
    parser.add_argument('--sell-tp', type=float, default=None,
                        help='覆盖 take_profit（如 0.03）')
    parser.add_argument('--sell-sl', type=float, default=None,
                        help='覆盖 stop_loss（如 -0.03）')
    parser.add_argument('--sell-days', type=int, default=None,
                        help='覆盖 time_stop_days（如 1 = T+1 收盘退出）')
    parser.add_argument('--sell-stop-mode', choices=['fixed', 'atr'], default=None,
                        help='覆盖 stop_mode（atr = max(atr_mult×ATR14, |stop_loss|)）')
    parser.add_argument('--crowding-avg-pct', type=float, default=None,
                        help='覆盖拥挤度断路器阈值（全市场日均涨幅绝对值，0=关闭）'
                             '——P3 A/B 实验用，不修改 config.yml')
    parser.add_argument('--max-close-pos', type=float, default=None,
                        help='覆盖尾盘急拉过滤阈值（收盘位置 0-1，0=关闭）'
                             '——P4 A/B 实验用，不修改 config.yml')
    parser.add_argument('--crowding-scale', default=None,
                        metavar='SPEC',
                        help='覆盖拥挤度分档仓位（R1 A/B）："off" 关闭，或 '
                             '"1.5:0.25,0.5:0.5" 形式。不修改 config.yml')
    parser.add_argument('--sizing-mode', choices=['normalized', 'absolute'], default=None,
                        help='仓位口径：normalized=按当日委托合计归一化（默认，尺度不变，'
                             '只衡量选股质量）/ absolute=按 alloc%% 绝对投入（未投出留现金，'
                             '使弱市压缩等仓位机制可被验证）。不修改 config.yml')
    parser.add_argument('--position-scaling', choices=['on', 'off'], default=None,
                        help='仓位机制栈总开关（A/B 用）：off = 同时关闭弱市压缩、'
                             '波动保险丝、连亏压缩、拥挤度分档（需配合 --sizing-mode '
                             'absolute 才可测量效果）。不修改 config.yml')
    parser.add_argument('--weight-override', action='append', default=None,
                        metavar='FACTOR=VALUE',
                        help='覆盖因子权重（基于 v1.json 修改，可重复）：'
                             '如 --weight-override size=0.146 --weight-override hot_theme=0.384。'
                             '仅作用于本次运行，不写 v1.json（A/B 用）。')

    args = parser.parse_args()
    config = load_config(args.config)

    store = BacktestStore()

    if args.list:
        runs = store.list_runs(20)
        if not runs:
            print("暂无回测记录")
            return
        print(f"{'ID':>4} {'策略':<16} {'模式':<8} {'区间':<24} {'交易':<6} {'胜率':<8} {'平均收益':<10} {'夏普':<8}")
        print("-" * 90)
        for r in runs:
            print(f"{r['id']:>4} {r['strategy']:<16} {r['mode']:<8} "
                  f"{r['start']}~{r['end']:<14} "
                  f"{r['trades']:<6} {r['win_rate']:.1f}%    "
                  f"{r['avg_return']:+.2f}%    {r['sharpe']:<8.2f}")
        return

    if args.compare:
        diff = store.compare_runs(args.compare[0], args.compare[1])
        print(diff)
        return

    # ── 样本外因子 IC 验证（不依赖策略，纯因子层统计检验）──
    if args.mode == 'oos':
        from core.oos_validator import (OOSValidator, RET_HOLD1D,
                                        RET_INTRADAY, RET_OVERNIGHT)
        ret_col = {'hold1d': RET_HOLD1D, 'intraday': RET_INTRADAY,
                   'overnight': RET_OVERNIGHT}[args.ret]

        validator = OOSValidator()
        panel = validator.build_panel(args.start, args.end)
        if panel.empty:
            print("面板为空：K线缓存或因子快照无数据，无法做样本外验证")
            return

        result = validator.evaluate(panel, train_ratio=args.train_ratio, ret_col=ret_col)
        wf = validator.walk_forward(panel, n_splits=args.folds,
                                    train_ratio=args.train_ratio, ret_col=ret_col)
        report = validator.report(result, wf)

        # 落盘：给权重校准脚本（scripts/calibrate_weights.py）提供 IC 输入
        # P2-1（2026-09-17）：补上 factor_corr 矩阵。此前只 dump result +
        # walk_forward 两个键，导致 calibrate_weights.apply_correlation_discount
        # 永远走"报告未含矩阵，跳过相关性折扣"分支（factor_corr 在
        # OOSValidator 已实现但从不落盘）。
        import json
        corr_df = validator.factor_corr(panel)
        payload = build_oos_report_payload(result, wf, corr_df)
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'data', 'reports')
        os.makedirs(out_dir, exist_ok=True)
        json_path = os.path.join(
            out_dir, f"oos_ic_{args.start}_{args.end}_{args.ret}.json")
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        if args.out:
            os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
            with open(args.out, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"报告已保存: {args.out}")
        else:
            print(report)
        print(f"\nIC 结果已落盘: {json_path}")
        return

    # 正常回测
    bt_config = config.get('backtest', {})
    strategy_config = config.get(args.mode + '_term', {})
    full_config = {**bt_config, **strategy_config}

    # P1（2026-09-18）：卖出规则参数覆盖（仅本次运行，落盘 run_meta 的
    # config_snapshot 可追溯）。不修改 config.yml。
    _sell_overrides = {}
    if getattr(args, 'sell_tp', None) is not None:
        _sell_overrides['take_profit'] = args.sell_tp
    if getattr(args, 'sell_sl', None) is not None:
        _sell_overrides['stop_loss'] = args.sell_sl
    if getattr(args, 'sell_days', None) is not None:
        _sell_overrides['time_stop_days'] = args.sell_days
    if getattr(args, 'sell_stop_mode', None):
        _sell_overrides['stop_mode'] = args.sell_stop_mode
    if getattr(args, 'position_scaling', None) == 'off':
        # 仓位机制栈总开关（A/B 用）：同时关闭四类仓位缩放
        _buy = full_config.setdefault('buy', {})
        _buy['weak_market_scale'] = 1.0
        _buy['vol_breaker_scale'] = 1.0
        _buy['losing_streak_days'] = 0
        _buy['crowding_scale_levels'] = []
        logger.info("仓位机制栈已全部关闭（weak_market/vol_breaker/losing_streak/crowding）")
    elif getattr(args, 'position_scaling', None) == 'on':
        logger.info("仓位机制栈保持开启（使用 config 配置）")
    if getattr(args, 'sizing_mode', None):
        full_config['sizing_mode'] = args.sizing_mode
        logger.info(f"仓位口径覆盖: sizing_mode={args.sizing_mode}")
    if _sell_overrides:
        full_config.setdefault('sell', {}).update(_sell_overrides)
        logger.info(f"卖出规则覆盖: {_sell_overrides}")
    if getattr(args, 'crowding_avg_pct', None) is not None:
        full_config.setdefault('buy', {})['crowding_avg_pct'] = args.crowding_avg_pct
        logger.info(f"拥挤度断路器覆盖: crowding_avg_pct={args.crowding_avg_pct}")
    if getattr(args, 'max_close_pos', None) is not None:
        full_config.setdefault('buy', {})['max_close_pos'] = args.max_close_pos
        logger.info(f"尾盘急拉过滤覆盖: max_close_pos={args.max_close_pos}")
    if getattr(args, 'crowding_scale', None) is not None:
        spec = str(args.crowding_scale).strip()
        levels = []
        if spec and spec.lower() not in ('off', 'none', '0'):
            for part in spec.split(','):
                if ':' not in part:
                    logger.warning(f"拥挤度分档格式错误（应为 阈值:系数）: {part}")
                    continue
                t, s = part.split(':', 1)
                try:
                    levels.append([float(t), float(s)])
                except ValueError:
                    logger.warning(f"拥挤度分档数值无法解析: {part}")
        full_config.setdefault('buy', {})['crowding_scale_levels'] = levels
        logger.info(f"拥挤度分档覆盖: {levels if levels else 'off'}")

    # 因子权重覆盖（A/B 用；基于 v1.json 修改，不写盘）
    # 复用消融通道（engine._ablated_weights 会在策略实例化后显式覆盖 ScoringModel，
    # 因为 ScoringModel 的加载优先级 v1.json > config，走 config 通道会被静默忽略）。
    weight_override = None
    if getattr(args, 'weight_override', None):
        base_w = load_weights_from_v1(args.mode)
        if not base_w:
            logger.warning("v1.json 未找到，权重覆盖不可用")
        else:
            weight_override = dict(base_w)
            for pair in args.weight_override:
                if '=' not in pair:
                    logger.warning(f"权重覆盖格式错误（应为 FACTOR=VALUE）: {pair}")
                    continue
                k, v = pair.split('=', 1)
                try:
                    weight_override[k.strip()] = float(v)
                except ValueError:
                    logger.warning(f"权重值无法解析: {pair}")
            _ov_desc = ', '.join(
                f"{p.split('=')[0].strip()}={weight_override.get(p.split('=')[0].strip())}"
                for p in args.weight_override if '=' in p)
            logger.info(f"权重覆盖: {_ov_desc}")

    # P2-5（2026-09-17）：--ablate 消融。将被消融因子权重置 0，剩余归一化，
    # 通过 config['weights'] 注入策略（ShortTermStrategy 读取
    # config.get('weights') → ScoringModel(weights=...)）。不修改 v1.json。
    ablation_label = ""
    ablated = None  # 2026-09-18 修复：非消融模式下未定义，L181 引用会 UnboundLocalError
    if getattr(args, 'ablate', None):
        ablate_factors = []
        for a in args.ablate:
            ablate_factors.extend(x.strip() for x in a.split(',') if x.strip())
        base_weights = load_weights_from_v1(args.mode)
        if not base_weights:
            logger.warning("data/weights/v1.json 未找到，消融仅打印、不生效")
            ablated = {}
        else:
            ablated = apply_ablation(base_weights, ablate_factors)
        full_config = {**full_config, 'weights': ablated}
        ablation_label = (
            "\n" + "=" * 60 + "\n"
            "⚠️ 本次为消融实验（--ablate）：\n"
            f"  已移除因子: {', '.join(ablate_factors)} "
            f"(原权重: { {f: base_weights.get(f) for f in ablate_factors} })\n"
            "  剩余权重已重新归一化到和为 1（未消融因子的相对比例不变）。\n"
            "=" * 60 + "\n"
        )
        logger.info(f"消融实验：已移除 {ablate_factors}，剩余权重已归一化")

    engine = BacktestEngine(full_config)
    # P2-5 修复：消融权重必须显式传递到 strategy.scoring_model（v1.json 优先级
    # 高于 config，config 通道的消融权重被静默覆盖 → 消融不生效）
    if ablated:
        engine._ablated_weights = ablated
    elif weight_override:
        engine._ablated_weights = weight_override

    if args.mode == 'kfactor':
        # 降级标记（2026-09-05 审查 P1-1）：此模式的因子口径与生产不一致
        # （momentum=原始20日涨幅而非RPS、technical=MA简化版而非6维评分、
        # volume_price=log映射而非生产映射函数），且对全部(股票,日)样本
        # 池化计算 IC（被跨日市场方差污染）。其结论与 core/oos_validator.py
        # （逐日横截面 IC + 口径对齐）可能矛盾。因子有效性判定请以
        #   python scripts/run_backtest.py --mode oos --ret hold1d
        # 为准；本模式仅作历史参考保留。
        print("=" * 70)
        print("⚠️  DEPRECATED: --mode kfactor 的因子口径与生产不一致、IC 为池化口径，")
        print("    结论不可用于权重决策。请改用 --mode oos（逐日横截面 IC，口径对齐实盘）。")
        print("=" * 70)
        logger.warning("kfactor 模式已降级为参考用途（P1-1），因子有效性判定请用 --mode oos")
        result = engine.run_kline_factor_backtest(
            factors=['momentum', 'technical', 'volume_price'],
            start_date=args.start,
            end_date=args.end,
        )
    else:
        result = engine.run(
            mode=args.mode,
            start_date=args.start,
            end_date=args.end,
        )

    # 保存回测结果
    store.save_run(result, full_config)

    report = generate_backtest_report(result)
    # 消融实验：报告头部显式标注（P2-5）
    report = ablation_label + report

    if args.out:
        os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"报告已保存: {args.out}")
    else:
        print(report)


def build_oos_report_payload(result: dict, wf: dict, corr_df=None) -> dict:
    """
    构造可 JSON 序列化的 OOS 报告 dict，并补上 `factor_corr` 矩阵（P2-1）。

    `OOSValidator.factor_corr` 返回的 DataFrame 此前从不落盘，导致
    `scripts/calibrate_weights.py::apply_correlation_discount` 永远走
    "报告未含矩阵，跳过相关性折扣" 分支。这里把矩阵转成嵌套 dict
    （行/列均为因子名，值为 float），同时放到顶层和 `result` 内 —— 读侧
    `apply_correlation_discount` 会先找顶层 `factor_corr`，再找
    `result.factor_corr`，二者兼容。

    注意：
      - 因子数约 8，矩阵很小，不会让报告体积爆炸；
      - numpy 类型需转 float 才能 json 序列化；
      - corr_df 为空（无数据）时 factor_corr 落 `{}`，读侧会静默跳过，不崩溃。
    """
    payload = {'result': result, 'walk_forward': wf, 'factor_corr': {}}
    corr = {}
    if corr_df is not None and not corr_df.empty:
        for f in corr_df.index:
            row = {}
            for g in corr_df.columns:
                v = corr_df.loc[f, g]
                # 跳过 NaN（某些日某因子退化为常数 → corr=NaN，已在
                # factor_corr 内按"因子对"分别累计，这里仅剔除坏值）
                if v is not None and v == v:  # 跳过 NaN（np.nan != np.nan）
                    row[str(g)] = float(v)
            if row:
                corr[str(f)] = row
    # 无论是否有矩阵都写入（空矩阵 = {}，读侧 apply_correlation_discount
    # 会判 not corr 静默跳过）。同时放到顶层与 result 内，兼容两种读侧路径。
    payload = {
        'result': {**result, 'factor_corr': corr},
        'walk_forward': wf,
        'factor_corr': corr,
    }
    return payload


def load_config(path: str) -> dict:
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}


def load_weights_from_v1(mode: str = 'short',
                         weights_dir: str = None) -> dict:
    """从 data/weights/v1.json 读取某模式的权重（扁平 dict）。

    不修改 v1.json 文件本身（P2-5 约束）。文件缺失返回 {}。
    """
    if weights_dir is None:
        weights_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'weights')
    path = os.path.join(weights_dir, 'v1.json')
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"读取权重失败 {path}: {e}")
        return {}
    if isinstance(data, dict) and mode in data and isinstance(data[mode], dict):
        return {str(k): float(v) for k, v in data[mode].items()}
    # 已是扁平 dict（无模式嵌套）
    if isinstance(data, dict) and any(
            isinstance(v, (int, float)) for v in data.values()):
        return {str(k): float(v) for k, v in data.items()}
    return {}


def apply_ablation(weights: dict, factors) -> dict:
    """
    P2-5 消融：将被消融因子的权重置 0，剩余权重重新归一化为和 1。

    - 不修改入参（返回新 dict）。
    - 被消融因子本就不在权重中 → 原样返回。
    - 剩余权重和 <= 0（全部被消融）→ 返回原 dict（无法归一，避免除零）。
    - 归一化后未消融因子的**相对比例不变**（这是验收关键）。
    """
    if isinstance(factors, str):
        factors = [factors]
    drop = set(factors)
    new = {str(f): float(w) for f, w in weights.items() if f not in drop}
    total = sum(new.values())
    if total <= 0:
        return dict(weights)
    return {f: round(w / total, 10) for f, w in new.items()}


if __name__ == '__main__':
    main()
