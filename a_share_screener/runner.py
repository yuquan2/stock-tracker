"""AkShare integration and CSV persistence for the A-share screener."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
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
BAR_FIELDS = DAILY_FIELDS[1:]
HISTORY_REQUEST_ATTEMPTS = 3
HISTORY_REQUEST_TIMEOUT_SECONDS = 30
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
DAILY_DATA_COLUMNS = [
    "trade_date",
    "ts_code",
    "name",
    "open",
    "close",
    "high",
    "low",
    "vol",
]
DATA_CSV_COLUMN_NAMES = {
    "trade_date": "日期",
    "ts_code": "股票代码",
    "name": "股票名称",
    "open": "开盘价",
    "close": "收盘价",
    "high": "最高价",
    "low": "最低价",
    "vol": "成交量(股)",
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
    required_columns = {"code", "name", "stock_type"}
    if stocks.empty or not required_columns.issubset(stocks.columns):
        raise RuntimeError("未获取到完整股票列表，已停止筛选。")

    stocks = stocks.loc[:, ["code", "name", "stock_type"]].rename(
        columns={"code": "ts_code"}
    )
    stocks["ts_code"] = stocks["ts_code"].astype(str).str[-6:].str.zfill(6)
    excluded = stocks.apply(
        lambda row: is_excluded_stock(row["ts_code"], row["name"]),
        axis=1,
    )
    is_a_share = stocks["stock_type"].str.startswith("GP-A", na=False)
    return stocks.loc[is_a_share & ~excluded, ["ts_code", "name"]].drop_duplicates(
        "ts_code"
    )


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
                timeout=HISTORY_REQUEST_TIMEOUT_SECONDS,
            )
            break
        except (RequestException, IndexError, KeyError) as error:
            if attempt == HISTORY_REQUEST_ATTEMPTS:
                if isinstance(error, (IndexError, KeyError)):
                    print(
                        f"{ts_code} 腾讯历史日线不可用或格式异常"
                        f"（{error!s}），已从本次筛选跳过。",
                        file=sys.stderr,
                    )
                    return pd.DataFrame(columns=["trade_date", *DAILY_FIELDS])
                raise RuntimeError(f"{ts_code} 历史日线请求连续失败。") from error
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
    # AkShare 1.18.x leaves sz000xxx equities in lots while other equities are shares.
    if ts_code.startswith("000"):
        history["vol"] = history["vol"] * 100
    return history


def fetch_daily_bars(
    ak: Any,
    stocks: pd.DataFrame,
    trading_days: list[str],
    workers: int,
    *,
    on_history_fetched: Callable[[pd.DataFrame], None] | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch histories concurrently, reporting each completed history to a checkpoint."""
    if workers < 1:
        raise ValueError("workers must be at least 1")

    # AkShare's per-request tqdm instances are not safe to update from worker threads.
    from akshare.stock_feature import stock_hist_tx

    stock_hist_tx.get_tqdm = lambda: _plain_progress
    stock_codes = stocks["ts_code"].tolist()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_stock_history, ak, ts_code, trading_days): ts_code
            for ts_code in stock_codes
        }
        histories = []
        for completed, future in enumerate(as_completed(futures), start=1):
            history = future.result()
            if not history.empty:
                histories.append(history)
                if on_history_fetched is not None:
                    on_history_fetched(history)
            print(f"已获取 {completed}/{len(futures)} 只股票。")
    histories = [history for history in histories if not history.empty]
    if not histories:
        return {
            trading_day: pd.DataFrame(columns=DAILY_FIELDS)
            for trading_day in trading_days
        }
    combined = pd.concat(histories, ignore_index=True)
    return {
        trading_day: combined.loc[
            combined["trade_date"] == trading_day, DAILY_FIELDS
        ].copy()
        for trading_day in trading_days
    }


def _plain_progress(iterable: Any, **_: Any) -> Any:
    """Return the iterable without rendering a shared terminal progress bar."""
    return iterable


def assemble_data(
    stocks: pd.DataFrame, daily_bars: dict[str, pd.DataFrame], trading_days: list[str]
) -> pd.DataFrame:
    """Create a complete three-day OHLCV dataset for eligible securities."""
    d0_date, d1_date, d2_date = trading_days
    merged = stocks.copy()
    for label, trading_day in zip(("d0", "d1", "d2"), trading_days, strict=True):
        bars = daily_bars[trading_day].rename(
            columns={field: f"{label}_{field}" for field in DAILY_FIELDS if field != "ts_code"}
        )
        merged = merged.merge(bars, on="ts_code", how="inner", validate="one_to_one")
        merged.insert(
            merged.columns.get_loc(f"{label}_open"),
            f"{label}_date",
            trading_day,
        )
    merged.insert(0, "pattern_date", d2_date)
    return merged.reindex(
        columns=[
            "pattern_date",
            "ts_code",
            "name",
            *[
                field
                for day in ("d0", "d1", "d2")
                for field in (
                    f"{day}_date",
                    f"{day}_open",
                    f"{day}_close",
                    f"{day}_high",
                    f"{day}_low",
                    f"{day}_vol",
                )
            ],
        ]
    )


def screen_data(data: pd.DataFrame) -> pd.DataFrame:
    """Apply the requested pattern to a complete three-day OHLCV dataset."""
    matches = data.apply(
        lambda row: matches_pattern(
            {field: row[f"d0_{field}"] for field in BAR_FIELDS},
            {field: row[f"d1_{field}"] for field in BAR_FIELDS},
            {field: row[f"d2_{field}"] for field in BAR_FIELDS},
        ),
        axis=1,
    )
    return (
        data.loc[matches, RESULT_COLUMNS]
        .sort_values("ts_code")
        .reset_index(drop=True)
    )


def screen(
    stocks: pd.DataFrame, daily_bars: dict[str, pd.DataFrame], trading_days: list[str]
) -> pd.DataFrame:
    """Screen eligible stocks that have complete bars for all three days."""
    return screen_data(assemble_data(stocks, daily_bars, trading_days))


def write_results(result: pd.DataFrame, output_dir: Path, pattern_date: str) -> Path:
    """Atomically replace the dated CSV only after all API data passed validation."""
    destination = output_dir / pattern_date[:4] / pattern_date[4:6] / f"{pattern_date}.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".csv.tmp")
    result = result.copy()
    result_headers = {
        "pattern_date": "形态日期",
        "ts_code": "股票代码",
        "name": "股票名称",
    }
    for day in ("d0", "d1", "d2"):
        dates = result[f"{day}_date"].dropna().unique()
        if len(dates) > 1:
            raise RuntimeError(f"结果包含多个 {day.upper()} 日期，无法写入 CSV。")
        date_suffix = str(dates[0])[-4:] if len(dates) else ""
        prefix = f"{day.upper()}({date_suffix})"
        result_headers.update(
            {
                f"{day}_open": f"{prefix}开盘价",
                f"{day}_close": f"{prefix}收盘价",
                f"{day}_high": f"{prefix}最高价",
                f"{day}_low": f"{prefix}最低价",
                f"{day}_vol": f"{prefix}成交量(股)",
            }
        )
    result.drop(columns=["d0_date", "d1_date", "d2_date"]).rename(
        columns=result_headers
    ).to_csv(
        temporary, index=False, encoding="utf-8-sig"
    )
    temporary.replace(destination)
    return destination


def write_data_log(data: pd.DataFrame, data_dir: Path, pattern_date: str) -> Path:
    """Atomically persist the D2 all-stock OHLCV snapshot."""
    snapshot = data.loc[
        :,
        ["d2_date", "ts_code", "name", "d2_open", "d2_close", "d2_high", "d2_low", "d2_vol"],
    ].rename(
        columns={
            "d2_date": "trade_date",
            "d2_open": "open",
            "d2_close": "close",
            "d2_high": "high",
            "d2_low": "low",
            "d2_vol": "vol",
        }
    )
    return write_daily_data_log(snapshot, data_dir, pattern_date)


def write_daily_data_log(
    snapshot: pd.DataFrame, data_dir: Path, trading_day: str
) -> Path:
    """Atomically persist one all-stock OHLCV snapshot sorted by stock code."""
    data_dir.mkdir(parents=True, exist_ok=True)
    destination = data_dir / f"{trading_day}.csv"
    temporary = destination.with_suffix(".csv.tmp")
    snapshot.sort_values("ts_code", kind="stable").reindex(
        columns=DAILY_DATA_COLUMNS
    ).rename(
        columns=DATA_CSV_COLUMN_NAMES
    ).to_csv(
        temporary, index=False, encoding="utf-8-sig"
    )
    temporary.replace(destination)
    return destination


def append_daily_data_checkpoint(
    history: pd.DataFrame,
    stock_names: dict[str, str],
    data_dir: Path,
) -> None:
    """Persist a completed stock history so interrupted downloads retain progress."""
    data_dir.mkdir(parents=True, exist_ok=True)
    snapshot = history.assign(name=history["ts_code"].map(stock_names)).reindex(
        columns=DAILY_DATA_COLUMNS
    )
    for trading_day, daily_snapshot in snapshot.groupby("trade_date"):
        checkpoint = data_dir / f".{trading_day}.partial.csv"
        append_data_checkpoint(daily_snapshot, checkpoint)


def append_data_checkpoint(snapshot: pd.DataFrame, checkpoint: Path) -> None:
    """Append normalized rows to a single-date checkpoint."""
    snapshot.rename(columns=DATA_CSV_COLUMN_NAMES).to_csv(
        checkpoint,
        mode="a",
        header=not checkpoint.exists(),
        index=False,
        encoding="utf-8-sig",
    )


def load_stock_snapshot(path: Path) -> pd.DataFrame:
    """Load a fixed eligible-stock universe snapshot."""
    reference = pd.read_csv(path, encoding="utf-8-sig", dtype={"股票代码": str})
    required_columns = {"股票代码", "股票名称"}
    missing_columns = required_columns.difference(reference.columns)
    if missing_columns:
        missing = "、".join(sorted(missing_columns))
        raise RuntimeError(f"{path} 缺少股票池字段：{missing}。")
    reference = reference.rename(columns={"股票代码": "ts_code", "股票名称": "name"})
    reference["ts_code"] = reference["ts_code"].str.zfill(6)
    if reference["ts_code"].duplicated().any():
        raise RuntimeError(f"{path} 的股票代码存在重复，无法作为股票池快照。")
    return reference.loc[:, ["ts_code", "name"]]


def write_stock_snapshot(stocks: pd.DataFrame, path: Path, snapshot_date: str) -> Path:
    """Persist a sorted fixed eligible-stock universe snapshot."""
    if len(snapshot_date) != 8 or not snapshot_date.isdigit():
        raise ValueError("snapshot_date must use YYYYMMDD format")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    stocks.assign(日期=snapshot_date).rename(
        columns={"ts_code": "股票代码", "name": "股票名称"}
    ).loc[:, ["日期", "股票代码", "股票名称"]].sort_values(
        "股票代码", kind="stable"
    ).to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)
    return path


def sync_stock_snapshot(
    stocks: pd.DataFrame, snapshot_dir: Path, snapshot_date: str
) -> Path | None:
    """Create a dated snapshot when the current stock universe has changed."""
    snapshots = sorted(snapshot_dir.glob("????????.csv"))
    if snapshots:
        latest = load_stock_snapshot(snapshots[-1]).sort_values(
            "ts_code", kind="stable"
        ).reset_index(drop=True)
        current = stocks.sort_values("ts_code", kind="stable").reset_index(drop=True)
        if latest.equals(current):
            return None
    return write_stock_snapshot(stocks, snapshot_dir / f"{snapshot_date}.csv", snapshot_date)


def populate_missing_data_logs(
    ak: Any,
    data_dir: Path,
    trading_days: list[str],
    workers: int,
    *,
    refresh_data: bool = False,
    stocks: pd.DataFrame | None = None,
) -> list[Path]:
    """Fetch and persist only the requested daily snapshots absent from the cache."""
    data_paths = [data_dir / f"{trading_day}.csv" for trading_day in trading_days]
    days_to_fetch = trading_days if refresh_data else [
        trading_day
        for trading_day, path in zip(trading_days, data_paths, strict=True)
        if not path.exists()
    ]
    if not days_to_fetch:
        return data_paths

    stocks = stocks if stocks is not None else eligible_stocks(ak)
    checkpoint_bars: dict[str, pd.DataFrame] = {}
    completed_codes: set[str] | None = None
    for trading_day in days_to_fetch:
        checkpoint = data_dir / f".{trading_day}.partial.csv"
        if checkpoint.exists():
            bars = read_data_log(checkpoint).loc[:, DAILY_FIELDS]
            checkpoint_bars[trading_day] = bars
            codes = set(bars["ts_code"])
            completed_codes = codes if completed_codes is None else completed_codes & codes
        else:
            checkpoint_bars[trading_day] = pd.DataFrame(columns=DAILY_FIELDS)
            completed_codes = set()
    stocks_to_fetch = stocks.loc[~stocks["ts_code"].isin(completed_codes)].copy()
    stock_names = stocks.set_index("ts_code")["name"].to_dict()
    daily_bars = fetch_daily_bars(
        ak,
        stocks_to_fetch,
        days_to_fetch,
        workers,
        on_history_fetched=lambda history: append_daily_data_checkpoint(
            history, stock_names, data_dir
        ),
    )
    for trading_day in days_to_fetch:
        if not checkpoint_bars[trading_day].empty:
            daily_bars[trading_day] = pd.concat(
                [checkpoint_bars[trading_day], daily_bars[trading_day]],
                ignore_index=True,
            )
        snapshot = stocks.merge(
            daily_bars[trading_day],
            on="ts_code",
            how="inner",
            validate="one_to_one",
        )
        snapshot.insert(0, "trade_date", trading_day)
        data_path = write_daily_data_log(snapshot, data_dir, trading_day)
        (data_dir / f".{trading_day}.partial.csv").unlink(missing_ok=True)
        print(f"已写入日线数据 {data_path}，共 {len(snapshot)} 只股票。")
    return data_paths


def read_data_log(path: Path) -> pd.DataFrame:
    """Load and validate a locally cached all-stock daily snapshot."""
    data = pd.read_csv(
        path, encoding="utf-8-sig", dtype={"日期": str, "股票代码": str}
    )
    data = data.rename(
        columns={chinese_name: field for field, chinese_name in DATA_CSV_COLUMN_NAMES.items()}
    )
    missing_columns = set(DAILY_DATA_COLUMNS).difference(data.columns)
    if missing_columns:
        missing = "、".join(sorted(missing_columns))
        raise RuntimeError(f"{path} 缺少日线数据字段：{missing}。")
    for field in ("open", "close", "high", "low", "vol"):
        data[field] = pd.to_numeric(data[field], errors="coerce")
    data["ts_code"] = data["ts_code"].str.zfill(6)
    return data.reindex(columns=DAILY_DATA_COLUMNS)


def assemble_data_from_logs(
    paths: list[Path], trading_days: list[str]
) -> pd.DataFrame:
    """Assemble three-day screen input from one all-stock snapshot per day."""
    snapshots = [read_data_log(path) for path in paths]
    for snapshot, trading_day in zip(snapshots, trading_days, strict=True):
        dates = snapshot["trade_date"].dropna().astype(str).unique()
        if len(dates) != 1 or dates[0] != trading_day:
            raise RuntimeError(f"{trading_day} 的日线数据文件日期不匹配。")
    stocks = snapshots[0].loc[:, ["ts_code", "name"]]
    daily_bars = {
        trading_day: snapshot.loc[:, DAILY_FIELDS]
        for trading_day, snapshot in zip(trading_days, snapshots, strict=True)
    }
    return assemble_data(stocks, daily_bars, trading_days)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="筛选 A 股三日形态。")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results-exact"),
        help="结果 CSV 的目录（默认：results-exact）。",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="每日全市场 OHLCV CSV 的目录（默认：data）。",
    )
    parser.add_argument(
        "--refresh-data",
        action="store_true",
        help="即使存在同日期原始数据文件，也重新从数据源获取。",
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
    pattern_date = trading_days[-1]
    stocks = eligible_stocks(ak)
    snapshot_path = sync_stock_snapshot(stocks, Path("stock_snapshot"), pattern_date)
    if snapshot_path is not None:
        print(f"已更新股票池快照 {snapshot_path}，共 {len(stocks)} 只股票。")
    data_paths = populate_missing_data_logs(
        ak,
        args.data_dir,
        trading_days,
        args.workers,
        refresh_data=args.refresh_data,
        stocks=stocks,
    )
    data = assemble_data_from_logs(data_paths, trading_days)
    print(f"复用本地日线数据，共 {len(data)} 只股票。")
    result = screen_data(data)
    destination = write_results(result, args.output_dir, pattern_date)
    print(f"已写入 {destination}，共 {len(result)} 只股票。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
