"""AkShare integration and CSV persistence for the A-share screener."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from requests import RequestException

from a_share_screener.pattern import is_excluded_stock, matches_pattern

DAILY_FIELDS = ["ts_code", "open", "close", "high", "low", "vol"]
HISTORY_REQUEST_ATTEMPTS = 3
MARKET_DATA_COMPLETE_HOUR = 16
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
CSV_COLUMN_NAMES = {
    "pattern_date": "形态日期",
    "ts_code": "股票代码",
    "name": "股票名称",
    "d0_date": "D0日期",
    "d0_vol": "D0成交量",
    "d1_date": "D1日期",
    "d1_open": "D1开盘价",
    "d1_close": "D1收盘价",
    "d1_high": "D1最高价",
    "d1_vol": "D1成交量",
    "d2_date": "D2日期",
    "d2_high": "D2最高价",
    "d2_low": "D2最低价",
}


def completed_trading_days(
    ak: Any, *, reference_date: date | None = None
) -> list[str]:
    """Return the three latest open days through the supplied D2 date."""
    reference_date = reference_date or date.today()
    calendar = ak.tool_trade_date_hist_sina()
    required_columns = {"trade_date"}
    if calendar.empty or not required_columns.issubset(calendar.columns):
        raise RuntimeError("未获取到完整交易日历，已停止筛选。")

    calendar = calendar.copy()
    calendar["trade_date"] = pd.to_datetime(
        calendar["trade_date"], errors="coerce"
    )
    open_days = sorted(
        calendar.loc[
            calendar["trade_date"].notna()
            & (calendar["trade_date"] <= pd.Timestamp(reference_date)),
            "trade_date",
        ].dt.strftime("%Y%m%d").unique()
    )
    if len(open_days) < 3:
        raise RuntimeError("完整交易日不足三个，已停止筛选。")
    return list(open_days[-3:])


def latest_completed_reference_date(now: datetime) -> date:
    """Use today's bar only after the daily market data has settled."""
    if now.hour >= MARKET_DATA_COMPLETE_HOUR:
        return now.date()
    return (now - timedelta(days=1)).date()


def eligible_stocks(ak: Any) -> pd.DataFrame:
    """Fetch A-share spot listings and remove excluded boards and ST names."""
    stocks = ak.stock_zh_a_spot_tx()
    required_columns = {"code", "name"}
    if stocks.empty or not required_columns.issubset(stocks.columns):
        raise RuntimeError("未获取到完整股票列表，已停止筛选。")

    stocks = stocks.loc[:, ["code", "name"]].rename(
        columns={"code": "ts_code"}
    )
    stocks["ts_code"] = stocks["ts_code"].astype(str).str[-6:].str.zfill(6)
    excluded = stocks.apply(
        lambda row: is_excluded_stock(row["ts_code"], row["name"]),
        axis=1,
    )
    return stocks.loc[~excluded, ["ts_code", "name"]].drop_duplicates("ts_code")


def fetch_stock_history(
    ak: Any, ts_code: str, trading_days: list[str]
) -> pd.DataFrame:
    """Fetch one stock's unadjusted bars, retaining only the target days."""
    exchange_prefix = "sh" if ts_code.startswith("6") else "sz"
    for attempt in range(1, HISTORY_REQUEST_ATTEMPTS + 1):
        try:
            history = ak.stock_zh_a_hist_tx(
                symbol=f"{exchange_prefix}{ts_code}",
                start_date=trading_days[0],
                end_date=trading_days[-1],
                adjust="",
            )
            break
        except RequestException:
            if attempt == HISTORY_REQUEST_ATTEMPTS:
                raise
            print(
                f"{ts_code} 历史日线请求失败，正在进行第 {attempt + 1} 次尝试。",
                file=sys.stderr,
            )
            time.sleep(attempt)
    source_columns = {"date", "open", "close", "high", "low", "volume"}
    if history.empty:
        return pd.DataFrame(columns=["trade_date", *DAILY_FIELDS])
    if not source_columns.issubset(history.columns):
        raise RuntimeError(f"{ts_code} 历史日线数据字段不完整，已停止筛选。")

    history = history.loc[:, ["date", "open", "close", "high", "low", "volume"]].rename(
        columns={
            "date": "trade_date",
            "volume": "vol",
        }
    )
    history["trade_date"] = pd.to_datetime(
        history["trade_date"], errors="coerce"
    ).dt.strftime("%Y%m%d")
    history.insert(1, "ts_code", ts_code)
    history = history.loc[history["trade_date"].isin(trading_days)].copy()
    if history["trade_date"].duplicated().any():
        raise RuntimeError(f"{ts_code} 历史日线数据存在重复交易日，已停止筛选。")
    for field in DAILY_FIELDS[1:]:
        history[field] = pd.to_numeric(history[field], errors="coerce")
    return history


def fetch_daily_bars(
    ak: Any, stocks: pd.DataFrame, trading_days: list[str], workers: int
) -> dict[str, pd.DataFrame]:
    """Fetch histories concurrently; only complete three-day records are screened."""
    if workers < 1:
        raise ValueError("workers must be at least 1")

    stock_codes = stocks["ts_code"].tolist()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        histories = list(
            executor.map(
                lambda ts_code: fetch_stock_history(ak, ts_code, trading_days),
                stock_codes,
            )
        )
    combined = pd.concat(histories, ignore_index=True)
    return {
        trading_day: combined.loc[
            combined["trade_date"] == trading_day, DAILY_FIELDS
        ].copy()
        for trading_day in trading_days
    }


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
    result.rename(columns=CSV_COLUMN_NAMES).to_csv(
        temporary, index=False, encoding="utf-8-sig"
    )
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
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="并发获取历史日线的请求数（默认：8）。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    if args.workers < 1:
        raise ValueError("--workers 必须至少为 1")

    import akshare as ak

    shanghai_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    trading_days = completed_trading_days(
        ak, reference_date=latest_completed_reference_date(shanghai_now)
    )
    stocks = eligible_stocks(ak)
    daily_bars = fetch_daily_bars(ak, stocks, trading_days, args.workers)
    result = screen(stocks, daily_bars, trading_days)
    destination = write_results(result, args.output_dir, trading_days[-1])
    print(f"已写入 {destination}，共 {len(result)} 只股票。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
