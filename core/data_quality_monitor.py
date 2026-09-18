# -*- coding: utf-8 -*-
"""数据质量监控（2026-09-06 第一梯队 T5）

对当日全市场行情快照做自动化巡检，拦截"脏数据悄悄进库"：
  - 重复代码（同 code 多行 → 拼接/去重逻辑出错）
  - 价格/成交额非正值
  - 涨跌幅绝对越界（>31% / <-31%，超过 30cm 板上限即不可能）
  - 关键列缺失率（close / pct_chg / amount 的 NaN 占比）
  - 零成交（疑似停牌）占比异常

输出结构化结果 dict（level/issues/stats），供简报追加展示与日志告警。
只读巡检，不修改任何数据。
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


class DataQualityMonitor:
    """全市场行情快照质量巡检器（每次运行只读，无状态）。"""

    # 关键列与允许的缺失率上限（超过即告警）
    REQUIRED_COLS = ('close', 'pct_chg', 'amount')
    MAX_MISSING_RATE = 0.05
    # 涨跌幅绝对越界线（30cm 板上限 30% + 容差 1%）
    PCT_ABS_LIMIT = 31.0

    def check_quotes(self, quotes_df) -> dict:
        result = {'level': 'ok', 'issues': [], 'stats': {}}
        if quotes_df is None or quotes_df.empty:
            result['level'] = 'error'
            result['issues'].append('行情快照为空（数据源整体失败）')
            return result

        stats = {'total': int(len(quotes_df))}
        df = quotes_df

        # 1. 重复代码
        dup = int(df['code'].duplicated().sum()) if 'code' in df.columns else 0
        stats['duplicate_codes'] = dup
        if dup > 0:
            result['issues'].append(f'重复代码 {dup} 行（拼接/去重异常）')

        # 2. 价格非正值
        if 'close' in df.columns:
            bad_price = int((pd.to_numeric(df['close'], errors='coerce') <= 0).sum())
            stats['non_positive_price'] = bad_price
            if bad_price > 0:
                result['issues'].append(f'价格非正值 {bad_price} 行')

        # 3. 涨跌幅越界
        #    新股豁免（2026-09-12）：注册制下主板/创业板/科创板新股上市前 5 日
        #    无涨跌幅限制（N/C/开头名称），首日 +179% 属合法行情（实证：9/11
        #    N燧原-U +179.22% 被误报脏数据）。XD/XR/DR/ST 等其他前缀仍受限，
        #    不豁免。
        if 'pct_chg' in df.columns:
            pct = pd.to_numeric(df['pct_chg'], errors='coerce')
            # name 列存在且以 N/C 开头（未开板新股）→ 豁免越界检查。
            # fillna('') 防 None→'None' 字符串误豁免（2026-09-12 审查轮发现）。
            is_new_stock = None
            if 'name' in df.columns:
                names = df['name'].fillna('').astype(str).str.strip()
                is_new_stock = names.str.startswith(('N', 'C'))
            checked_pct = pct[~is_new_stock] if is_new_stock is not None else pct
            out_of_range = int((checked_pct.abs() > self.PCT_ABS_LIMIT).sum())
            missing_pct = float(pct.isna().mean())
            stats['pct_out_of_range'] = out_of_range
            stats['pct_missing_rate'] = round(missing_pct, 4)
            if out_of_range > 0:
                result['issues'].append(
                    f'涨跌幅越界（|pct|>{self.PCT_ABS_LIMIT}%）{out_of_range} 行')
            if missing_pct > self.MAX_MISSING_RATE:
                result['issues'].append(
                    f'涨跌幅缺失率 {missing_pct:.1%} 超阈值 {self.MAX_MISSING_RATE:.0%}')

        # 4. 成交额缺失/非正
        if 'amount' in df.columns:
            amt = pd.to_numeric(df['amount'], errors='coerce')
            missing_amt = float(amt.isna().mean())
            stats['amount_missing_rate'] = round(missing_amt, 4)
            if missing_amt > self.MAX_MISSING_RATE:
                result['issues'].append(
                    f'成交额缺失率 {missing_amt:.1%} 超阈值')

        # 5. 零成交占比（停牌属正常，但占比异常高说明快照质量问题）
        if 'volume' in df.columns:
            vol = pd.to_numeric(df['volume'], errors='coerce')
            zero_vol_rate = float((vol <= 0).mean())
            stats['zero_volume_rate'] = round(zero_vol_rate, 4)
            if zero_vol_rate > 0.20:
                result['issues'].append(
                    f'零成交占比 {zero_vol_rate:.1%} 异常偏高（>20%）')

        result['stats'] = stats
        if result['issues']:
            result['level'] = 'error' if any('整体失败' in i or '越界' in i
                                             for i in result['issues']) else 'warn'
            for i in result['issues']:
                logger.warning(f"[数据质量] {i}")
        return result


def format_quality_line(result: dict) -> str:
    """把巡检结果压成简报追加用的一行文本（level=ok 时极简）。"""
    if result['level'] == 'ok':
        total = result.get('stats', {}).get('total', 0)
        return f"    🧪 数据质量: OK（{total} 只，无异常）"
    issues = '; '.join(result['issues'])
    return f"    🧪 数据质量: {result['level'].upper()} — {issues}"
