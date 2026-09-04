import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from requests import ConnectionError

from a_share_screener.pattern import is_excluded_stock, matches_pattern, prices_equal
from a_share_screener.runner import (
    assemble_data,
    assemble_data_from_logs,
    append_daily_data_checkpoint,
    completed_trading_days,
    eligible_stocks,
    fetch_daily_bars,
    fetch_stock_history,
    latest_completed_reference_date,
    populate_missing_data_logs,
    read_data_log,
    screen,
    sync_stock_snapshot,
    write_daily_data_log,
    write_results,
    write_data_log,
    write_stock_snapshot,
)


class PatternTests(unittest.TestCase):
    def setUp(self) -> None:
        self.d0 = {"open": 9.8, "close": 10.0, "high": 10.1, "low": 9.7, "vol": 100}
        self.d1 = {"open": 10.0, "close": 10.5, "high": 10.7, "low": 9.9, "vol": 150}
        self.d2 = {"open": 10.2, "close": 10.3, "high": 10.5, "low": 10.0, "vol": 80}

    def test_matches_complete_pattern(self) -> None:
        self.assertTrue(matches_pattern(self.d0, self.d1, self.d2))

    def test_accepts_one_tick_price_difference(self) -> None:
        self.d2["high"] = 10.51
        self.assertTrue(prices_equal(self.d2["high"], self.d1["close"]))

    def test_rejects_when_volume_threshold_is_not_met(self) -> None:
        self.d1["vol"] = 149.99
        self.assertFalse(matches_pattern(self.d0, self.d1, self.d2))

    def test_rejects_when_second_day_prices_do_not_match(self) -> None:
        self.d2["low"] = 10.02
        self.assertFalse(matches_pattern(self.d0, self.d1, self.d2))


class StockFilterTests(unittest.TestCase):
    def test_excludes_st_star_and_restricted_boards(self) -> None:
        self.assertTrue(is_excluded_stock("600001.SH", "*ST示例", "主板", "SSE"))
        self.assertTrue(is_excluded_stock("688001.SH", "普通公司", "科创板", "SSE"))
        self.assertTrue(is_excluded_stock("430001.BJ", "普通公司", "北交所", "BSE"))
        self.assertTrue(is_excluded_stock("920045.BJ", "普通公司", "北交所", "BSE"))

    def test_keeps_eligible_main_board_and_chinext_stocks(self) -> None:
        self.assertFalse(is_excluded_stock("600001.SH", "普通公司", "主板", "SSE"))
        self.assertFalse(is_excluded_stock("300001.SZ", "创业公司", "创业板", "SZSE"))


class AkShareAdapterTests(unittest.TestCase):
    def test_uses_today_as_d2_after_market_data_is_complete(self) -> None:
        after_close = datetime(2026, 8, 31, 17, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(latest_completed_reference_date(after_close).isoformat(), "2026-08-31")

    def test_uses_previous_date_before_market_data_is_complete(self) -> None:
        before_close = datetime(2026, 8, 31, 15, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(latest_completed_reference_date(before_close).isoformat(), "2026-08-30")

    def test_uses_only_completed_days_from_akshare_calendar(self) -> None:
        class CalendarAkShare:
            @staticmethod
            def tool_trade_date_hist_sina() -> pd.DataFrame:
                return pd.DataFrame(
                    {"trade_date": ["2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31"]}
                )

        self.assertEqual(
            completed_trading_days(
                CalendarAkShare(), reference_date=pd.Timestamp("2026-08-31").date()
            ),
            ["20260827", "20260828", "20260831"],
        )

    def test_filters_akshare_spot_list(self) -> None:
        class SpotAkShare:
            @staticmethod
            def stock_zh_a_spot_tx() -> pd.DataFrame:
                return pd.DataFrame(
                    {
                        "code": ["sh600001", "sh688001", "bj830001", "sz300001"],
                        "name": ["普通公司", "科创公司", "*ST公司", "创业公司"],
                        "stock_type": ["GP-A-SH", "GP-A-KCB", "GP-A-BJ", "GP-A-CYB"],
                    }
                )

        stocks = eligible_stocks(SpotAkShare())

        self.assertEqual(stocks["ts_code"].tolist(), ["600001", "300001"])

    def test_excludes_non_a_share_security_types(self) -> None:
        class SpotAkShare:
            @staticmethod
            def stock_zh_a_spot_tx() -> pd.DataFrame:
                return pd.DataFrame(
                    {
                        "code": ["sh600001", "sh900001"],
                        "name": ["普通公司", "B股公司"],
                        "stock_type": ["GP-A", "GP"],
                    }
                )

        stocks = eligible_stocks(SpotAkShare())

        self.assertEqual(stocks["ts_code"].tolist(), ["600001"])

    def test_handles_stock_without_history_without_concat_warning(self) -> None:
        class EmptyHistoryAkShare:
            @staticmethod
            def stock_zh_a_hist_tx(**kwargs: str) -> pd.DataFrame:
                return pd.DataFrame()

        bars = fetch_daily_bars(
            EmptyHistoryAkShare(),
            pd.DataFrame([{"ts_code": "600001", "name": "示例公司"}]),
            ["20260826", "20260827", "20260828"],
            workers=1,
        )

        self.assertTrue(all(frame.empty for frame in bars.values()))
        self.assertTrue(all(frame.columns.tolist() == ["ts_code", "open", "close", "high", "low", "vol"] for frame in bars.values()))

    def test_normalizes_akshare_history_columns(self) -> None:
        class HistoryAkShare:
            @staticmethod
            def stock_zh_a_hist_tx(**kwargs: str) -> pd.DataFrame:
                self.assertEqual(
                    kwargs,
                    {
                        "symbol": "sh600001",
                        "start_date": "20260826",
                        "end_date": "20260828",
                        "adjust": "",
                        "timeout": 30,
                    },
                )
                return pd.DataFrame(
                    {
                        "date": ["2026-08-26", "2026-08-27", "2026-08-28"],
                        "open": [9.8, 10.0, 10.2],
                        "close": [10.0, 10.5, 10.3],
                        "high": [10.1, 10.7, 10.5],
                        "low": [9.7, 9.9, 10.0],
                        "volume": [100, 150, 80],
                    }
                )

        result = fetch_stock_history(
            HistoryAkShare(), "600001", ["20260826", "20260827", "20260828"]
        )

        self.assertEqual(result["trade_date"].tolist(), ["20260826", "20260827", "20260828"])
        self.assertEqual(result["ts_code"].tolist(), ["600001", "600001", "600001"])

    def test_converts_sz000_history_volume_from_lots_to_shares(self) -> None:
        class HistoryAkShare:
            @staticmethod
            def stock_zh_a_hist_tx(**kwargs: str) -> pd.DataFrame:
                return pd.DataFrame(
                    {
                        "date": ["2026-08-26", "2026-08-27", "2026-08-28"],
                        "open": [9.8, 10.0, 10.2],
                        "close": [10.0, 10.5, 10.3],
                        "high": [10.1, 10.7, 10.5],
                        "low": [9.7, 9.9, 10.0],
                        "volume": [100, 150, 80],
                    }
                )

        result = fetch_stock_history(
            HistoryAkShare(), "000001", ["20260826", "20260827", "20260828"]
        )

        self.assertEqual(result["vol"].tolist(), [10000, 15000, 8000])

    def test_retries_transient_history_connection_error(self) -> None:
        class RetryingAkShare:
            attempts = 0

            @classmethod
            def stock_zh_a_hist_tx(cls, **kwargs: str) -> pd.DataFrame:
                cls.attempts += 1
                if cls.attempts == 1:
                    raise ConnectionError("temporary disconnect")
                return pd.DataFrame(
                    {
                        "date": ["2026-08-26", "2026-08-27", "2026-08-28"],
                        "open": [9.8, 10.0, 10.2],
                        "close": [10.0, 10.5, 10.3],
                        "high": [10.1, 10.7, 10.5],
                        "low": [9.7, 9.9, 10.0],
                        "volume": [100, 150, 80],
                    }
                )

        fetch_stock_history(
            RetryingAkShare(), "600001", ["20260826", "20260827", "20260828"]
        )

        self.assertEqual(RetryingAkShare.attempts, 2)

    def test_retries_transient_history_parsing_error(self) -> None:
        class RetryingAkShare:
            attempts = 0

            @classmethod
            def stock_zh_a_hist_tx(cls, **kwargs: str) -> pd.DataFrame:
                cls.attempts += 1
                if cls.attempts == 1:
                    raise IndexError("upstream response has no rows")
                return pd.DataFrame(
                    {
                        "date": ["2026-08-26", "2026-08-27", "2026-08-28"],
                        "open": [9.8, 10.0, 10.2],
                        "close": [10.0, 10.5, 10.3],
                        "high": [10.1, 10.7, 10.5],
                        "low": [9.7, 9.9, 10.0],
                        "volume": [100, 150, 80],
                    }
                )

        fetch_stock_history(
            RetryingAkShare(), "600001", ["20260826", "20260827", "20260828"]
        )

        self.assertEqual(RetryingAkShare.attempts, 2)

    def test_retries_transient_history_missing_day_error(self) -> None:
        class RetryingAkShare:
            attempts = 0

            @classmethod
            def stock_zh_a_hist_tx(cls, **kwargs: str) -> pd.DataFrame:
                cls.attempts += 1
                if cls.attempts == 1:
                    raise KeyError("day")
                return pd.DataFrame(
                    {
                        "date": ["2026-08-26", "2026-08-27", "2026-08-28"],
                        "open": [9.8, 10.0, 10.2],
                        "close": [10.0, 10.5, 10.3],
                        "high": [10.1, 10.7, 10.5],
                        "low": [9.7, 9.9, 10.0],
                        "volume": [100, 150, 80],
                    }
                )

        with patch("a_share_screener.runner.time.sleep"):
            result = fetch_stock_history(
                RetryingAkShare(), "600001", ["20260826", "20260827", "20260828"]
            )

        self.assertFalse(result.empty)
        self.assertEqual(RetryingAkShare.attempts, 2)

    def test_skips_stock_without_available_tencent_history(self) -> None:
        class MissingHistoryAkShare:
            attempts = 0

            @classmethod
            def stock_zh_a_hist_tx(cls, **kwargs: str) -> pd.DataFrame:
                cls.attempts += 1
                raise IndexError("upstream response has no rows")

        with patch("a_share_screener.runner.time.sleep"):
            result = fetch_stock_history(
                MissingHistoryAkShare(), "301688", ["20260826", "20260827", "20260828"]
            )

        self.assertTrue(result.empty)
        self.assertEqual(MissingHistoryAkShare.attempts, 3)

    def test_skips_malformed_tencent_history_without_stopping_other_stocks(self) -> None:
        class MixedHistoryAkShare:
            failed_attempts = 0

            @classmethod
            def stock_zh_a_hist_tx(cls, **kwargs: str) -> pd.DataFrame:
                if kwargs["symbol"] == "sh600001":
                    cls.failed_attempts += 1
                    raise KeyError("day")
                return pd.DataFrame(
                    {
                        "date": ["2026-08-26", "2026-08-27", "2026-08-28"],
                        "open": [9.8, 10.0, 10.2],
                        "close": [10.0, 10.5, 10.3],
                        "high": [10.1, 10.7, 10.5],
                        "low": [9.7, 9.9, 10.0],
                        "volume": [100, 150, 80],
                    }
                )

        with patch("a_share_screener.runner.time.sleep"):
            bars = fetch_daily_bars(
                MixedHistoryAkShare(),
                pd.DataFrame(
                    [
                        {"ts_code": "600001", "name": "异常公司"},
                        {"ts_code": "600002", "name": "正常公司"},
                    ]
                ),
                ["20260826", "20260827", "20260828"],
                workers=1,
            )

        self.assertEqual(MixedHistoryAkShare.failed_attempts, 3)
        self.assertTrue(
            all(frame["ts_code"].tolist() == ["600002"] for frame in bars.values())
        )


class ScreeningTests(unittest.TestCase):
    def test_screens_only_complete_matching_three_day_data(self) -> None:
        stocks = pd.DataFrame(
            [
                {"ts_code": "600001.SH", "name": "匹配公司"},
                {"ts_code": "600002.SH", "name": "数据不完整公司"},
            ]
        )
        daily_bars = {
            "20260826": pd.DataFrame(
                [
                    {
                        "ts_code": "600001.SH",
                        "open": 9.8,
                        "close": 10.0,
                        "high": 10.1,
                        "low": 9.7,
                        "vol": 100,
                    },
                    {
                        "ts_code": "600002.SH",
                        "open": 8.0,
                        "close": 8.1,
                        "high": 8.2,
                        "low": 7.9,
                        "vol": 100,
                    },
                ]
            ),
            "20260827": pd.DataFrame(
                [
                    {
                        "ts_code": "600001.SH",
                        "open": 10.0,
                        "close": 10.5,
                        "high": 10.7,
                        "low": 9.9,
                        "vol": 150,
                    }
                ]
            ),
            "20260828": pd.DataFrame(
                [
                    {
                        "ts_code": "600001.SH",
                        "open": 10.2,
                        "close": 10.3,
                        "high": 10.5,
                        "low": 10.0,
                        "vol": 80,
                    }
                ]
            ),
        }

        result = screen(stocks, daily_bars, ["20260826", "20260827", "20260828"])

        self.assertEqual(result["ts_code"].tolist(), ["600001.SH"])
        self.assertEqual(result.loc[0, "pattern_date"], "20260828")
        self.assertEqual(result.loc[0, "d1_close"], 10.5)

    def test_writes_and_reads_daily_data_log(self) -> None:
        stocks = pd.DataFrame([{"ts_code": "600001", "name": "示例公司"}])
        daily_bars = {
            "20260826": pd.DataFrame(
                [{"ts_code": "600001", "open": 9.8, "close": 10.0, "high": 10.1, "low": 9.7, "vol": 100}]
            ),
            "20260827": pd.DataFrame(
                [{"ts_code": "600001", "open": 10.0, "close": 10.5, "high": 10.7, "low": 9.9, "vol": 150}]
            ),
            "20260828": pd.DataFrame(
                [{"ts_code": "600001", "open": 10.2, "close": 10.3, "high": 10.5, "low": 10.0, "vol": 80}]
            ),
        }
        data = assemble_data(stocks, daily_bars, ["20260826", "20260827", "20260828"])

        with TemporaryDirectory() as directory:
            destination = write_data_log(data, Path(directory), "20260828")
            loaded = read_data_log(destination)

        self.assertEqual(loaded.loc[0, "ts_code"], "600001")
        self.assertEqual(loaded.loc[0, "trade_date"], "20260828")
        self.assertEqual(loaded.loc[0, "open"], 10.2)
        self.assertEqual(loaded.loc[0, "vol"], 80)

    def test_assembles_data_from_daily_logs(self) -> None:
        with TemporaryDirectory() as directory:
            directory_path = Path(directory)
            paths = []
            for trading_day, open_price in zip(
                ["20260826", "20260827", "20260828"], [9.8, 10.0, 10.2], strict=True
            ):
                path = directory_path / f"{trading_day}.csv"
                pd.DataFrame(
                    [
                        {
                            "日期": trading_day,
                            "股票代码": "600001",
                            "股票名称": "示例公司",
                            "开盘价": open_price,
                            "收盘价": 10.5,
                            "最高价": 10.7,
                            "最低价": 9.9,
                            "成交量(股)": 100,
                        }
                    ]
                ).to_csv(path, index=False, encoding="utf-8-sig")
                paths.append(path)

            data = assemble_data_from_logs(
                paths, ["20260826", "20260827", "20260828"]
            )

        self.assertEqual(data.loc[0, "d0_open"], 9.8)
        self.assertEqual(data.loc[0, "d2_date"], "20260828")

    def test_fetches_only_missing_daily_data_log(self) -> None:
        class HistoryAkShare:
            requested_days: list[tuple[str, str]] = []

            @staticmethod
            def stock_zh_a_spot_tx() -> pd.DataFrame:
                return pd.DataFrame(
                    {
                        "code": ["sh600001"],
                        "name": ["示例公司"],
                        "stock_type": ["GP-A"],
                    }
                )

            @classmethod
            def stock_zh_a_hist_tx(cls, **kwargs: str) -> pd.DataFrame:
                cls.requested_days.append((kwargs["start_date"], kwargs["end_date"]))
                return pd.DataFrame(
                    {
                        "date": [kwargs["start_date"]],
                        "open": [10.2],
                        "close": [10.3],
                        "high": [10.5],
                        "low": [10.0],
                        "volume": [80],
                    }
                )

        trading_days = ["20260826", "20260827", "20260828"]
        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            for trading_day in trading_days[:2]:
                pd.DataFrame(
                    [
                        {
                            "日期": trading_day,
                            "股票代码": "600001",
                            "股票名称": "示例公司",
                            "开盘价": 10.0,
                            "收盘价": 10.5,
                            "最高价": 10.7,
                            "最低价": 9.9,
                            "成交量(股)": 100,
                        }
                    ]
                ).to_csv(data_dir / f"{trading_day}.csv", index=False, encoding="utf-8-sig")

            paths = populate_missing_data_logs(
                HistoryAkShare(), data_dir, trading_days, workers=1
            )

            self.assertEqual(HistoryAkShare.requested_days, [("20260828", "20260828")])
            self.assertTrue(all(path.exists() for path in paths))

    def test_persists_completed_history_checkpoint(self) -> None:
        history = pd.DataFrame(
            [
                {
                    "trade_date": "20260828",
                    "ts_code": "600001",
                    "open": 10.2,
                    "close": 10.3,
                    "high": 10.5,
                    "low": 10.0,
                    "vol": 80,
                }
            ]
        )
        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            append_daily_data_checkpoint(
                history, {"600001": "示例公司"}, data_dir
            )
            checkpoint = read_data_log(data_dir / ".20260828.partial.csv")

        self.assertEqual(checkpoint.loc[0, "ts_code"], "600001")
        self.assertEqual(checkpoint.loc[0, "trade_date"], "20260828")

    def test_sorts_daily_data_log_by_stock_code(self) -> None:
        snapshot = pd.DataFrame(
            [
                {
                    "trade_date": "20260828",
                    "ts_code": "600002",
                    "name": "第二家公司",
                    "open": 10.2,
                    "close": 10.3,
                    "high": 10.5,
                    "low": 10.0,
                    "vol": 80,
                },
                {
                    "trade_date": "20260828",
                    "ts_code": "000001",
                    "name": "第一家公司",
                    "open": 10.2,
                    "close": 10.3,
                    "high": 10.5,
                    "low": 10.0,
                    "vol": 80,
                },
            ]
        )
        with TemporaryDirectory() as directory:
            destination = write_daily_data_log(snapshot, Path(directory), "20260828")
            written = read_data_log(destination)

        self.assertEqual(written["ts_code"].tolist(), ["000001", "600002"])

    def test_writes_sorted_stock_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "20260903.csv"
            write_stock_snapshot(
                pd.DataFrame(
                    [
                        {"ts_code": "600001", "name": "乙"},
                        {"ts_code": "000001", "name": "甲"},
                    ]
                ),
                path,
                "20260903",
            )
            snapshot = pd.read_csv(
                path, encoding="utf-8-sig", dtype={"日期": str, "股票代码": str}
            )

        self.assertEqual(snapshot["股票代码"].tolist(), ["000001", "600001"])
        self.assertEqual(snapshot["日期"].tolist(), ["20260903", "20260903"])

    def test_creates_snapshot_when_stock_universe_changes(self) -> None:
        with TemporaryDirectory() as directory:
            snapshot_dir = Path(directory)
            write_stock_snapshot(
                pd.DataFrame([{"ts_code": "600001", "name": "甲"}]),
                snapshot_dir / "20260902.csv",
                "20260902",
            )
            destination = sync_stock_snapshot(
                pd.DataFrame(
                    [
                        {"ts_code": "000001", "name": "乙"},
                        {"ts_code": "600001", "name": "甲"},
                    ]
                ),
                snapshot_dir,
                "20260903",
            )
            unchanged = sync_stock_snapshot(
                pd.DataFrame(
                    [
                        {"ts_code": "600001", "name": "甲"},
                        {"ts_code": "000001", "name": "乙"},
                    ]
                ),
                snapshot_dir,
                "20260903",
            )

        self.assertEqual(destination, snapshot_dir / "20260903.csv")
        self.assertIsNone(unchanged)

    def test_writes_chinese_csv_headers(self) -> None:
        result = pd.DataFrame(
            [
                {
                    "pattern_date": "20260901",
                    "ts_code": "600001",
                    "name": "示例公司",
                    "d0_date": "20260828",
                    "d0_vol": 100,
                    "d1_date": "20260831",
                    "d1_open": 10.0,
                    "d1_close": 10.5,
                    "d1_high": 10.7,
                    "d1_vol": 150,
                    "d2_date": "20260901",
                    "d2_high": 10.5,
                    "d2_low": 10.0,
                }
            ]
        )
        with TemporaryDirectory() as directory:
            destination = write_results(result, Path(directory), "20260901")
            written = pd.read_csv(destination, encoding="utf-8-sig")

        self.assertEqual(
            destination,
            Path(directory) / "2026" / "09" / "20260901.csv",
        )
        self.assertEqual(
            written.columns.tolist(),
            [
                "形态日期",
                "股票代码",
                "股票名称",
                "D0(0828)成交量(股)",
                "D1(0831)开盘价",
                "D1(0831)收盘价",
                "D1(0831)最高价",
                "D1(0831)成交量(股)",
                "D2(0901)最高价",
                "D2(0901)最低价",
            ],
        )


if __name__ == "__main__":
    unittest.main()
