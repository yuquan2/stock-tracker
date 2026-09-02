import unittest

import pandas as pd

from a_share_screener.pattern import is_excluded_stock, matches_pattern, prices_equal
from a_share_screener.runner import screen


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
