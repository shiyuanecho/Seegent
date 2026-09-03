import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from seegent.trend import (
    CHAIN_BY_ID,
    CHART_HISTORY_PAGE_CACHE_TTL,
    CHART_LATEST_CACHE_TTL,
    CHART_RANGE_SECONDS,
    FetchResult,
    TrendError,
    TrendService,
    _aggregate_candles,
    _alias_for_query,
    _asset_id,
    _breakdown_value,
    _normalize_address,
    _parse_asset_id,
    _parse_market_id,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class TrendServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = TrendService(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def fetched(self, data, stale=False):
        return FetchResult(data=data, retrieved_at="2026-09-01T00:00:00Z", stale=stale)

    def test_chain_registry_uses_morph_not_mantle(self):
        self.assertIn("morph", CHAIN_BY_ID)
        self.assertEqual(CHAIN_BY_ID["morph"]["chainNumericId"], 2818)
        self.assertEqual(CHAIN_BY_ID["morph"]["nativeSymbol"], "BGB")
        self.assertNotIn("mantle", CHAIN_BY_ID)

    def test_chinese_and_english_aliases_resolve_to_same_asset(self):
        self.assertEqual(_alias_for_query("BTC")["symbol"], "BTC")
        self.assertEqual(_alias_for_query("Bitcoin")["symbol"], "BTC")
        self.assertEqual(_alias_for_query("比特币")["symbol"], "BTC")
        self.assertEqual(_alias_for_query("Gram")["symbol"], "TON")

    def test_evm_addresses_are_normalized_but_solana_is_case_sensitive(self):
        evm = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        sol = "AbCdEf123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
        self.assertEqual(_normalize_address("ethereum", evm), evm.lower())
        self.assertEqual(_asset_id("token", "base", evm), "token:base:" + evm.lower())
        self.assertEqual(_normalize_address("solana", sol), sol)
        self.assertEqual(_parse_asset_id("token:ethereum:" + evm)[2], evm.lower())
        self.assertEqual(_parse_market_id("dex:ethereum:" + evm)[2], evm.lower())

    def test_binance_search_parses_quote_without_inventing_market_cap(self):
        self.service._request_json = lambda *args, **kwargs: self.fetched(fixture("binance_ticker.json"))
        result = self.service._search_binance("BTC", "")[0]
        self.assertEqual(result["asset"]["nameZh"], "比特币")
        self.assertEqual(result["market"]["marketId"], "cex:binance:BTCUSDT")
        self.assertTrue(result["asset"]["logoUrl"].endswith("/bitcoin.png"))
        self.assertEqual(result["quote"]["priceUsd"], 61234.5)
        self.assertEqual(result["quote"]["volume24hUsd"], 75555555.0)
        self.assertIsNone(result["quote"]["marketCapUsd"])

    def test_dex_providers_dedupe_same_pool_and_preserve_both_sources(self):
        def fake_request(provider, *args, **kwargs):
            if provider == "dexscreener":
                return self.fetched(fixture("dexscreener_search.json"))
            if provider == "geckoterminal":
                return self.fetched(fixture("geckoterminal_search.json"))
            raise AssertionError(provider)

        self.service._request_json = fake_request
        dex_results = self.service._search_dexscreener("EXM", "ethereum")
        gecko_results = self.service._search_geckoterminal("EXM", "ethereum")
        self.assertEqual(dex_results[0]["asset"]["address"], "0x" + "b" * 40)
        self.assertEqual(dex_results[0]["market"]["poolAddress"], "0x" + "a" * 40)
        self.assertEqual(dex_results[0]["market"]["createdAt"], 1700000000000)
        self.assertEqual(dex_results[0]["quote"]["marketCapUsd"], 1000000.0)

        with patch.object(self.service, "_search_binance", return_value=[]), \
             patch.object(self.service, "_search_dexscreener", return_value=dex_results), \
             patch.object(self.service, "_search_geckoterminal", return_value=gecko_results), \
             patch.object(self.service, "_search_robinhood", return_value=[]):
            response = self.service.search("EXM", "ethereum")

        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["results"][0]["providers"], ["dexscreener", "geckoterminal"])

    def test_provider_logo_is_kept_only_when_it_is_https(self):
        data = fixture("dexscreener_search.json")
        data["pairs"][0]["info"] = {"imageUrl": "https://cdn.example.test/exm.png"}
        self.service._request_json = lambda *args, **kwargs: self.fetched(data)
        result = self.service._search_dexscreener("EXM", "ethereum")[0]
        self.assertEqual(result["asset"]["logoUrl"], "https://cdn.example.test/exm.png")

        data["pairs"][0]["info"] = {"imageUrl": "http://cdn.example.test/exm.png"}
        result = self.service._search_dexscreener("EXM", "ethereum")[0]
        self.assertIsNone(result["asset"]["logoUrl"])

    def test_dex_detail_normalizes_evm_display_address(self):
        self.service._request_json = lambda *args, **kwargs: self.fetched(fixture("dexscreener_search.json"))
        asset_id = "token:ethereum:0x" + "b" * 40
        detail = self.service._dex_asset(asset_id, "ethereum", "0x" + "a" * 40)
        self.assertEqual(detail["asset"]["address"], "0x" + "b" * 40)
        self.assertEqual(detail["market"]["marketId"], "dex:ethereum:0x" + "a" * 40)

    def test_symbol_search_ranks_native_cex_before_spoofable_dex_name(self):
        self.service._request_json = lambda *args, **kwargs: self.fetched(fixture("binance_ticker.json"))
        binance = self.service._search_binance("BTC", "")
        dex_fixture = fixture("dexscreener_search.json")
        dex_fixture["pairs"][0]["baseToken"].update({"name": "Bitcoin", "symbol": "BTC"})
        dex_fixture["pairs"][0]["liquidity"]["usd"] = 999999999999
        self.service._request_json = lambda *args, **kwargs: self.fetched(dex_fixture)
        dex = self.service._search_dexscreener("BTC", "ethereum")

        with patch.object(self.service, "_search_binance", return_value=binance), \
             patch.object(self.service, "_search_dexscreener", return_value=dex), \
             patch.object(self.service, "_search_geckoterminal", return_value=[]), \
             patch.object(self.service, "_search_robinhood", return_value=[]):
            response = self.service.search("BTC")

        self.assertEqual(response["results"][0]["market"]["marketId"], "cex:binance:BTCUSDT")

    def test_ohlcv_selects_effective_interval_and_keeps_utc(self):
        self.service._request_json = lambda *args, **kwargs: self.fetched(fixture("binance_klines.json"))
        response = self.service._binance_ohlcv("BTCUSDT", "1d")
        self.assertEqual(response["requestedRange"], "1d")
        self.assertEqual(response["effectiveInterval"], "15m")
        self.assertEqual(response["timezone"], "UTC")
        self.assertEqual(response["candles"][0]["open"], 100.0)
        self.assertEqual(response["candles"][1]["tradeCount"], 51)

    def test_chart_request_keeps_requested_interval_and_returns_history_cursor(self):
        captured = {}

        def fake_request(provider, url, *args, **kwargs):
            captured["provider"] = provider
            captured["url"] = url
            return self.fetched(fixture("binance_klines.json"))

        self.service._request_json = fake_request
        response = self.service._binance_ohlcv("BTCUSDT", "30d", "1m")
        self.assertEqual(captured["provider"], "binance")
        self.assertIn("interval=1m", captured["url"])
        self.assertIn("limit=1000", captured["url"])
        self.assertEqual(response["requestedRange"], "30d")
        self.assertEqual(response["requestedInterval"], "1m")
        self.assertEqual(response["effectiveInterval"], "1m")
        self.assertEqual(response["meta"]["nativeOrAggregated"], "native")
        self.assertNotIn("interval_coarsened", [error["code"] for error in response["meta"]["partialErrors"]])
        self.assertIn("historyComplete", response)

    def test_all_history_uses_provider_page_and_exposes_cursor(self):
        captured = {}

        def fake_request(provider, url, *args, **kwargs):
            captured["url"] = url
            rows = fixture("binance_klines.json")
            while len(rows) < 1000:
                row = list(rows[-1])
                row[0] -= 60000
                row[6] -= 60000
                rows.append(row)
            return self.fetched(rows)

        self.service._request_json = fake_request
        response = self.service._binance_ohlcv("BTCUSDT", "all", "1d")
        self.assertIn("endTime=", captured["url"])
        self.assertNotIn("startTime=", captured["url"])
        self.assertEqual(response["requestedRange"], "all")
        self.assertEqual(response["effectiveInterval"], "1d")
        self.assertTrue(response["hasMore"])
        self.assertIsNotNone(response["nextCursor"])

    def test_chart_requests_use_stable_cache_identity_for_moving_latest_timestamp(self):
        captured = []

        def fake_request(provider, url, *args, **kwargs):
            captured.append((provider, kwargs.get("cache_key")))
            return self.fetched(fixture("binance_klines.json"))

        self.service._request_json = fake_request
        self.service._binance_ohlcv("BTCUSDT", "all", "1d")

        self.assertEqual(captured, [("binance", "ohlcv:binance:BTCUSDT:1d:latest")])

    def test_chart_latest_page_is_short_cached_but_history_page_is_long_cached(self):
        captured = []

        def fake_request(provider, url, *args, **kwargs):
            captured.append(kwargs.get("ttl"))
            return self.fetched(fixture("binance_klines.json"))

        self.service._request_json = fake_request
        self.service._binance_ohlcv("BTCUSDT", "all", "1d")
        self.service._binance_ohlcv("BTCUSDT", "all", "1d", "1700000000")

        self.assertEqual(captured, [CHART_LATEST_CACHE_TTL, CHART_HISTORY_PAGE_CACHE_TTL])

    def test_gecko_chart_requests_use_stable_cache_identity_for_moving_latest_timestamp(self):
        captured = []

        def fake_request(provider, url, *args, **kwargs):
            captured.append((provider, kwargs.get("cache_key")))
            return self.fetched({"data": {"attributes": {"ohlcv_list": []}}})

        self.service._request_json = fake_request
        self.service._gecko_ohlcv("ton", "pool", "all", "1M")

        self.assertEqual(captured, [("geckoterminal", "ohlcv:geckoterminal:ton:pool:day:1:latest")])

    def test_expired_cache_is_returned_when_internal_rate_budget_is_exhausted(self):
        cache_key = "ohlcv:test:latest"
        self.service._cache["geckoterminal|" + cache_key] = (
            time.monotonic() - 1,
            {"cached": True},
            "2026-09-01T00:00:00Z",
        )
        limited = TrendError("rate_limited", "budget", 429, "geckoterminal", 1000)
        with patch.object(self.service, "_take_rate_slot", side_effect=limited):
            fetched = self.service._request_json(
                "geckoterminal",
                "https://api.geckoterminal.com/test",
                ttl=60,
                cache_key=cache_key,
            )

        self.assertEqual(fetched.data, {"cached": True})
        self.assertTrue(fetched.stale)

    def test_utc_candle_aggregation_keeps_ohlcv_semantics_without_gap_filling(self):
        candles = [
            {"timestamp": 1704067200, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 2, "quoteVolume": 20, "tradeCount": 3, "confirmed": True},
            {"timestamp": 1704067260, "open": 11, "high": 14, "low": 10, "close": 13, "volume": 4, "quoteVolume": 52, "tradeCount": 5, "confirmed": True},
            {"timestamp": 1704067800, "open": 13, "high": 15, "low": 12, "close": 14, "volume": 1, "quoteVolume": 14, "tradeCount": 1, "confirmed": True},
        ]
        aggregated = _aggregate_candles(candles, "5m", 300)
        self.assertEqual(len(aggregated), 2)
        self.assertEqual(aggregated[0]["open"], 10)
        self.assertEqual(aggregated[0]["high"], 14)
        self.assertEqual(aggregated[0]["low"], 9)
        self.assertEqual(aggregated[0]["close"], 13)
        self.assertEqual(aggregated[0]["volume"], 6)
        self.assertEqual(aggregated[0]["tradeCount"], 8)
        self.assertEqual(aggregated[1]["open"], 13)

    def test_capabilities_expose_independent_chart_intervals_and_ranges(self):
        capabilities = self.service.capabilities()
        self.assertIn("1M", capabilities["chartIntervals"])
        self.assertIn("1y", capabilities["chartRanges"])
        self.assertIn("all", capabilities["chartRanges"])
        self.assertTrue(capabilities["chartHistory"]["pagination"])
        self.assertEqual(CHART_RANGE_SECONDS["90d"], 90 * 24 * 60 * 60)

    def test_robinhood_daily_volume_is_token_units_not_usd(self):
        self.service._request_json = lambda *args, **kwargs: self.fetched(fixture("robinhood_price.json"))
        quote, _ = self.service._robinhood_quote("TSLA")
        self.assertEqual(quote["dailyTradingVolumeToken"], 61732015.0)
        self.assertIsNone(quote["volume24hUsd"])

        detail = {
            "quote": quote,
            "meta": {"retrievedAt": "2026-09-01T00:00:00Z"},
        }
        with patch.object(self.service, "_robinhood_asset", return_value=detail):
            flow = self.service.flows("token:robinhood:0x" + "a" * 40, "stock:robinhood:TSLA", "1d")
        self.assertIsNone(flow["metrics"]["tradeVolumeUsd"])
        self.assertEqual(flow["metrics"]["tradeVolumeToken"], 61732015.0)

    def test_defillama_breakdown_matches_case_insensitively(self):
        value = _breakdown_value({"Ethereum": {"uniswap": 10}, "ethereum": 5, "Solana": 99}, CHAIN_BY_ID["ethereum"])
        self.assertEqual(value, 15.0)

    def test_overview_marks_listed_chain_available_without_fake_zero(self):
        response_data = {
            "allChains": ["Ethereum", "Morph"],
            "protocols": [
                {"name": "One", "breakdown24h": {"Ethereum": 123.5}, "breakdown30d": {"Ethereum": 456.5}},
                {"name": "Double", "doublecounted": True, "breakdown24h": {"Ethereum": 999}},
            ],
        }
        self.service._request_json = lambda *args, **kwargs: self.fetched(response_data)
        overview = self.service.overview()
        ethereum = next(item for item in overview["chains"] if item["chainId"] == "ethereum")
        morph = next(item for item in overview["chains"] if item["chainId"] == "morph")
        bsc = next(item for item in overview["chains"] if item["chainId"] == "bsc")
        self.assertEqual(ethereum["trackedDexVolume24hUsd"], 123.5)
        self.assertEqual(ethereum["activeProtocols"], 1)
        self.assertEqual(morph["metricStatus"], "estimated")
        self.assertIsNone(morph["trackedDexVolume24hUsd"])
        self.assertEqual(bsc["metricStatus"], "unavailable")

    def test_watchlist_is_atomic_persistent_and_deduplicated(self):
        item = {
            "assetId": "native:bitcoin:BTC",
            "marketId": "cex:binance:BTCUSDT",
            "symbol": "BTC",
            "name": "Bitcoin",
            "nameZh": "比特币",
            "chainId": "bitcoin",
            "marketLabel": "BTC/USDT",
            "provider": "binance",
            "logoUrl": "http://cdn.example.test/btc.png",
            "quote": {"priceUsd": "79000.25", "change24h": "1.5", "volume24hUsd": "1000000", "liquidityUsd": None},
            "quoteMeta": {"provider": "binance", "retrievedAt": "2026-09-01T00:00:00Z", "metricStatus": "exact"},
        }
        first = self.service.save_watch_item(item)
        second = self.service.save_watch_item(item)
        self.assertTrue(first["ok"])
        self.assertEqual(len(second["items"]), 1)

        reloaded = TrendService(self.temp_dir.name).get_watchlist()
        self.assertEqual(reloaded["items"][0]["symbol"], "BTC")
        self.assertEqual(reloaded["items"][0]["quote"]["priceUsd"], 79000.25)
        self.assertEqual(reloaded["items"][0]["quoteMeta"]["metricStatus"], "exact")
        self.assertTrue(reloaded["items"][0]["logoUrl"].endswith("/bitcoin.png"))
        removed = self.service.delete_watch_item(reloaded["items"][0]["targetId"])
        self.assertEqual(removed["removed"], 1)

    def test_blocked_host_and_rate_limit_are_structured(self):
        with self.assertRaises(TrendError) as blocked:
            self.service._request_json("binance", "https://example.com/private", ttl=1)
        self.assertEqual(blocked.exception.code, "blocked_host")

        self.service._rate_events["geckoterminal"] = deque([time.monotonic()] * 8)
        with self.assertRaises(TrendError) as limited:
            self.service._take_rate_slot("geckoterminal")
        self.assertEqual(limited.exception.status, 429)
        self.assertIsNotNone(limited.exception.retry_after_ms)

    def test_partial_provider_failure_does_not_remove_catalog_result(self):
        error = TrendError("provider_unavailable", "offline", 502, "dexscreener")
        with patch.object(self.service, "_search_binance", side_effect=error), \
             patch.object(self.service, "_search_dexscreener", side_effect=error), \
             patch.object(self.service, "_search_geckoterminal", side_effect=error), \
             patch.object(self.service, "_search_robinhood", side_effect=error):
            response = self.service.search("比特币")
        self.assertEqual(response["results"][0]["asset"]["symbol"], "BTC")
        self.assertEqual(len(response["meta"]["partialErrors"]), 4)


if __name__ == "__main__":
    unittest.main()
