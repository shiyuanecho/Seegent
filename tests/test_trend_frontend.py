import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TrendFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "seegent" / "static" / "trend.js").read_text(encoding="utf-8")

    def test_trend_has_three_internal_pages(self):
        self.assertIn('data-page="overview">市场总览', self.script)
        self.assertIn('data-page="watchlist">我的收藏', self.script)
        self.assertIn('data-page="detail"', self.script)

    def test_search_results_can_be_favorited_directly(self):
        self.assertIn('data-action="toggle-result-watch"', self.script)
        self.assertIn("async function toggleResultWatch", self.script)

    def test_watchlist_page_exposes_quote_summary(self):
        self.assertIn("trend-favorite-price", self.script)
        self.assertIn("refreshWatchlistQuotes", self.script)
        self.assertIn('id="trend-watch-sort-by"', self.script)
        self.assertIn("WATCH_SORT_OPTIONS", self.script)
        self.assertIn("marketCapUsd: '市值'", self.script)
        self.assertIn("change24h: '24h 涨跌幅'", self.script)
        self.assertIn('data-action="toggle-watch-sort-dir"', self.script)
        self.assertNotIn('trend-stat-value">0</div>', self.script)

    def test_watchlist_cards_hide_market_provider_and_error_metadata(self):
        self.assertIn("trend-favorite-card", self.script)
        self.assertIn("trend-favorite-metrics", self.script)
        self.assertNotIn("trend-favorite-market", self.script)
        self.assertNotIn("trend-favorite-source", self.script)
        self.assertNotIn("quoteMeta.lastError", self.script)

    def test_watchlist_sort_keeps_missing_metrics_last(self):
        self.assertIn("function getSortedWatchlist", self.script)
        self.assertIn("function sortableWatchValue", self.script)
        self.assertIn("state.watchSort.sortBy", self.script)
        self.assertIn("if (leftValue === null) return 1", self.script)

    def test_common_btc_logo_is_used_but_optional_provider_logos_still_fail_safe(self):
        self.assertIn("COMMON_ASSET_LOGOS", (ROOT / "seegent" / "trend.py").read_text(encoding="utf-8"))
        self.assertIn("_watchlist_items_with_common_logos", (ROOT / "seegent" / "trend.py").read_text(encoding="utf-8"))
        self.assertIn("image.remove()", self.script)

    def test_search_results_expose_okx_style_sort_and_filters(self):
        self.assertIn('id="trend-filter-volume"', self.script)
        self.assertIn('id="trend-filter-marketcap"', self.script)
        self.assertIn('id="trend-filter-age"', self.script)
        self.assertIn('value="createdAt">市场创建时间', self.script)
        self.assertIn("RESULT_FILTER_STORAGE_KEY", self.script)

    def test_missing_sort_values_are_kept_last_instead_of_becoming_zero(self):
        self.assertIn("if (leftValue === null) return 1", self.script)
        self.assertIn("if (rightValue === null) return -1", self.script)
        self.assertIn("缺少对应指标的结果不会按 0 计算", self.script)

    def test_token_logos_are_provider_supplied_and_optional(self):
        self.assertIn("function tokenLogoHtml", self.script)
        self.assertIn("data-token-logo", self.script)
        self.assertIn("image.remove()", self.script)
        self.assertIn("has-token-logo", self.script)
        self.assertNotIn("slice(0, 5))}</span>", self.script)

    def test_chart_uses_one_exact_period_selector_and_history_pagination(self):
        self.assertIn("CHART_INTERVAL_OPTIONS", self.script)
        self.assertIn("CHART_HISTORY_RANGE = 'all'", self.script)
        self.assertIn("nextCursor", self.script)
        self.assertIn("load-chart-history", self.script)
        self.assertIn('data-action="chart-interval"', self.script)
        self.assertIn("requestedInterval", self.script)
        self.assertNotIn('data-action="chart-range"', self.script)

    def test_chart_fits_full_dataset_and_adapts_price_scale(self):
        self.assertIn("chartAutoscaleInfo", self.script)
        self.assertIn("autoscaleInfoProvider", self.script)
        self.assertIn("fitContent()", self.script)
        self.assertIn("ResizeObserver", self.script)

    def test_switching_markets_clears_previous_market_candles(self):
        self.assertIn("newly selected market", self.script)
        self.assertIn("state.candleSeries.setData([])", self.script)

    def test_chart_requests_reset_loading_state_and_ignore_stale_responses(self):
        self.assertIn("chartRequestKey", self.script)
        self.assertIn("chartRequestGeneration", self.script)
        self.assertIn("resetChartLoadingState", self.script)
        self.assertIn("isCurrentChartRequest", self.script)
        self.assertIn("state.page !== 'detail'", self.script)
        self.assertIn("state.chartRequestKey === loadingKey", self.script)
        self.assertIn("当前市场 K 线暂不可用", self.script)

    def test_chart_prioritizes_latest_page_and_bypasses_browser_http_cache(self):
        self.assertIn("const CHART_HISTORY_AUTO_PAGES = 1;", self.script)
        self.assertIn("const CHART_HISTORY_MAX_PAGES = 1;", self.script)
        self.assertIn("cache: 'no-store'", self.script)
        self.assertIn("最新一页 · 可拖动缩放", self.script)
        self.assertNotIn("正在加载全历史 K 线", self.script)


if __name__ == "__main__":
    unittest.main()
