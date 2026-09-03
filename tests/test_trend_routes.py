import unittest
from pathlib import Path
from unittest.mock import patch

from seegent import server
from seegent.trend import TrendService


class TrendRouteTests(unittest.TestCase):
    def test_index_references_current_trend_bundle_version(self):
        index = (Path(__file__).resolve().parents[1] / "seegent" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('/trend.css?v=20260903-01', index)
        self.assertIn('/trend.js?v=20260903-01', index)

    def make_handler(self, path, command="GET"):
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.command = command
        handler.response = None

        def serve_json(payload, status=200):
            handler.response = (status, payload)
            return handler.response

        handler._serve_json = serve_json
        return handler

    def test_capabilities_route_runs_without_api_keys(self):
        handler = self.make_handler("/api/trend/capabilities")
        key_overrides = {key: "" for key in ("BIRDEYE_API_KEY", "ALCHEMY_API_KEY", "HELIUS_API_KEY", "TONAPI_TOKEN", "GOPLUS_ACCESS_TOKEN")}
        with patch.object(server, "TREND_SERVICE", TrendService("/tmp")), patch.dict(server.os.environ, key_overrides):
            handler._handle_trend_get("/api/trend/capabilities")
        status, payload = handler.response
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["chains"]), 7)
        self.assertFalse(payload["optionalKeys"]["BIRDEYE_API_KEY"])

    def test_invalid_range_returns_structured_400(self):
        handler = self.make_handler("/api/trend/ohlcv?marketId=cex%3Abinance%3ABTCUSDT&range=2h")
        with patch.object(server, "TREND_SERVICE", TrendService("/tmp")):
            handler._handle_trend_get("/api/trend/ohlcv")
        status, payload = handler.response
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_range")

    def test_ohlcv_route_forwards_chart_interval_separately_from_range(self):
        handler = self.make_handler("/api/trend/ohlcv?marketId=cex%3Abinance%3ABTCUSDT&range=30d&interval=5m")

        class FakeService:
            def ohlcv(self, market_id, range_key, interval_key=None):
                return {"marketId": market_id, "range": range_key, "interval": interval_key}

        with patch.object(server, "TREND_SERVICE", FakeService()):
            handler._handle_trend_get("/api/trend/ohlcv")
        self.assertEqual(handler.response, (200, {"marketId": "cex:binance:BTCUSDT", "range": "30d", "interval": "5m"}))

    def test_versioned_trend_static_asset_uses_package_static_route(self):
        handler = self.make_handler("/trend.js?v=20260901-1")
        captured = {}

        def serve_static(filename, content_type="text/html"):
            captured.update(filename=filename, content_type=content_type)

        handler._serve_static = serve_static
        handler.do_GET()
        self.assertEqual(captured, {"filename": "trend.js", "content_type": "application/javascript"})

    def test_watchlist_post_delegates_valid_json(self):
        handler = self.make_handler("/api/trend/watchlist", command="POST")
        handler._read_body = lambda: {
            "assetId": "native:bitcoin:BTC",
            "marketId": "cex:binance:BTCUSDT",
            "symbol": "BTC",
        }

        class FakeService:
            def save_watch_item(self, body):
                return {"ok": True, "symbol": body["symbol"]}

        with patch.object(server, "TREND_SERVICE", FakeService()):
            handler._handle_trend_post()
        self.assertEqual(handler.response, (200, {"ok": True, "symbol": "BTC"}))


if __name__ == "__main__":
    unittest.main()
