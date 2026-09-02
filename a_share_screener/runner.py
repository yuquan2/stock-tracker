"""Tushare Pro integration and CSV persistence for the A-share screener."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from a_share_screener.pattern import is_excluded_stock, matches_pattern

DAILY_FIELDS = ["ts_code", "open", "close", "high", "low", "vol"]
RESULT_COLUMNS = [
    "pattern_date",
    "ts_code",
    "name",
    "d0_date",
    "d0_vol",
    "d1_date",
    "d1_open",
    "d1_close",
    "d1_high",
    "d1_vol",
    "d2_date",
    "d2_high",
    "d2_low",
]


def completed_trading_days(
    pro: Any, *, reference_date: date | None = None
) -> list[str]:
    """Return the three latest open days strictly before the local date."""
    reference_date = reference_date or date.today()
    start_date = reference_date - timedelta(days=21)
    calendar = pro.trade_cal(
        exchange="",
        start_date=start_date.strftime("%Y%m%d"),
        end_date=reference_date.strftime("%Y%m%d"),
        fields="cal_date,is_open",
    )
    required_columns = {"cal_date", "is_open"}
    if calendar.empty or not required_columns.issubset(calendar.columns):
        raise RuntimeError("未获取到完整交易日历，已停止筛选。")

    calendar = calendar.copy()
    calendar["cal_date"] = calendar["cal_date"].astype(str)
    calendar["is_open"] = pd.to_numeric(calendar["is_open"], errors="coerce")
    today_text = reference_date.strftime("%Y%m%d")
    open_days = sorted(
        calendar.loc[
            (calendar["is_open"] == 1) & (calendar["cal_date"] < today_text),
            "cal_date",
        ].unique()
    )
    if len(open_days) < 3:
        raise RuntimeError("完整交易日不足三个，已停止筛选。")
    return list(open_days[-3:])


def fetch_daily_bars(pro: Any, trading_days: list[str]) -> dict[str, pd.DataFrame]:
    """Fetch and validate one complete market snapshot for each requested day."""
    daily_bars: dict[str, pd.DataFrame] = {}
    for trading_day in trading_days:
        frame = pro.daily(trade_date=trading_day)
        if frame.empty or not set(DAILY_FIELDS).issubset(frame.columns):
            raise RuntimeError(f"{trading_day} 日线数据不完整，已停止筛选。")

        frame = frame.loc[:, DAILY_FIELDS].copy()
        if frame["ts_code"].duplicated().any():
            raise RuntimeError(f"{trading_day} 日线数据存在重复证券，已停止筛选。")
        for field in DAILY_FIELDS[1:]:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        daily_bars[trading_day] = frame
    return daily_bars


def eligible_stocks(pro: Any) -> pd.DataFrame:
    """Fetch listed stocks and remove excluded boards and ST-labelled names."""
    stocks = pro.stock_basic(
        exchange="",
        list_status="L",
        fields="ts_code,name,market,exchange",
    )
    required_columns = {"ts_code", "name", "market", "exchange"}
    if stocks.empty or not required_columns.issubset(stocks.columns):
        raise RuntimeError("未获取到完整股票列表，已停止筛选。")

    stocks = stocks.loc[:, ["ts_code", "name", "market", "exchange"]].copy()
    excluded = stocks.apply(
        lambda row: is_excluded_stock(
            row["ts_code"], row["name"], row["market"], row["exchange"]
        ),
        axis=1,
    )
    return stocks.loc[~excluded, ["ts_code", "name"]].drop_duplicates("ts_code")


def screen(
    stocks: pd.DataFrame, daily_bars: dict[str, pd.DataFrame], trading_days: list[str]
) -> pd.DataFrame:
    """Screen only securities with all three daily bars present and valid."""
    d0_date, d1_date, d2_date = trading_days
    merged = stocks.copy()
    for label, trading_day in zip(("d0", "d1", "d2"), trading_days, strict=True):
        bars = daily_bars[trading_day].rename(
            columns={field: f"{label}_{field}" for field in DAILY_FIELDS if field != "ts_code"}
        )
        merged = merged.merge(bars, on="ts_code", how="inner", validate="one_to_one")

    matches = merged.apply(
        lambda row: matches_pattern(
            {
                "open": row["d0_open"],
                "close": row["d0_close"],
                "high": row["d0_high"],
                "low": row["d0_low"],
                "vol": row["d0_vol"],
            },
            {
                "open": row["d1_open"],
                "close": row["d1_close"],
                "high": row["d1_high"],
                "low": row["d1_low"],
                "vol": row["d1_vol"],
            },
            {
                "open": row["d2_open"],
                "close": row["d2_close"],
                "high": row["d2_high"],
                "low": row["d2_low"],
                "vol": row["d2_vol"],
            },
        ),
        axis=1,
    )
    result = merged.loc[
        matches,
        [
            "ts_code",
            "name",
            "d0_vol",
            "d1_open",
            "d1_close",
            "d1_high",
            "d1_vol",
            "d2_high",
            "d2_low",
        ],
    ].copy()
    result.insert(0, "pattern_date", d2_date)
    result.insert(3, "d0_date", d0_date)
    result.insert(5, "d1_date", d1_date)
    result.insert(10, "d2_date", d2_date)
    return result.reindex(columns=RESULT_COLUMNS).sort_values("ts_code").reset_index(drop=True)


def write_results(result: pd.DataFrame, output_dir: Path, pattern_date: str) -> Path:
    """Atomically replace the dated CSV only after all API data passed validation."""
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{pattern_date}.csv"
    temporary = destination.with_suffix(".csv.tmp")
    result.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(destination)
    return destination


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="筛选 A 股三日形态。")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results"),
        help="结果 CSV 的目录（默认：results）。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        print("缺少环境变量 TUSHARE_TOKEN。", file=sys.stderr)
        return 2

    import tushare as ts

    pro = ts.pro_api(token)
    trading_days = completed_trading_days(
        pro, reference_date=datetime.now(ZoneInfo("Asia/Shanghai")).date()
    )
    daily_bars = fetch_daily_bars(pro, trading_days)
    result = screen(eligible_stocks(pro), daily_bars, trading_days)
    destination = write_results(result, args.output_dir, trading_days[-1])
    print(f"已写入 {destination}，共 {len(result)} 只股票。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
