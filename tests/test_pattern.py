import unittest

import pandas as pd
from requests import ConnectionError

from a_share_screener.pattern import is_excluded_stock, matches_pattern, prices_equal
from a_share_screener.runner import (
    completed_trading_days,
    eligible_stocks,
    fetch_stock_history,
    screen,
)


class PatternTests(unittest.TestCase):
    def setUp(self) -> None:
        self.d0 = {"open": 9.8, "close": 10.0, "high": 10.1, "low": 9.7, "vol": 100}
        self.d1 = {"open": 10.0, "close": 10.5, "high": 10.7, "low": 9.9, "vol": 150}
        self.d2 = {"open": 10.2, "close": 10.3, "high": 10.5, "low": 10.0, "vol": 80}

    def test_matches_complete_pattern(self) -> None:
        self.assertTrue(matches_pattern(self.d0, self.d1, self.d2))

    def test_accepts_half_tick_floating_point_difference(self) -> None:
        self.d2["high"] = 10.505
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

    def test_keeps_eligible_main_board_and_chinext_stocks(self) -> None:
        self.assertFalse(is_excluded_stock("600001.SH", "普通公司", "主板", "SSE"))
        self.assertFalse(is_excluded_stock("300001.SZ", "创业公司", "创业板", "SZSE"))


class AkShareAdapterTests(unittest.TestCase):
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
            ["20260826", "20260827", "20260828"],
        )

    def test_filters_akshare_spot_list(self) -> None:
        class SpotAkShare:
            @staticmethod
            def stock_zh_a_spot_tx() -> pd.DataFrame:
                return pd.DataFrame(
                    {
                        "code": ["sh600001", "sh688001", "bj830001", "sz300001"],
                        "name": ["普通公司", "科创公司", "*ST公司", "创业公司"],
                    }
                )

        stocks = eligible_stocks(SpotAkShare())

        self.assertEqual(stocks["ts_code"].tolist(), ["600001", "300001"])

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


if __name__ == "__main__":
    unittest.main()
