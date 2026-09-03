"""Trend data service for Seegent.

The module deliberately keeps market instruments separate from underlying assets:

* Asset: native coin or contract token.
* Market: a CEX pair, DEX pool, or Robinhood Stock Token quote.
* Metrics: provider-scoped values with freshness and coverage metadata.

Only allow-listed, documented public endpoints are queried. Optional provider keys are
read from environment variables and never returned to the browser.
"""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import tempfile
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen


SCHEMA_VERSION = 1
MAX_RESPONSE_BYTES = 3 * 1024 * 1024
MAX_SEARCH_RESULTS = 30
MAX_WATCHLIST_ITEMS = 100


CHAIN_DEFS: List[Dict[str, Any]] = [
    {
        "id": "robinhood",
        "name": "Robinhood Chain",
        "shortName": "Robinhood",
        "nativeSymbol": "ETH",
        "chainNumericId": 4663,
        "explorer": "https://robinhoodchain.blockscout.com",
        "geckoId": "robinhood",
        "dexScreenerId": "robinhood",
        "defillamaKey": "robinhood",
        "defillamaName": "Robinhood Chain",
    },
    {
        "id": "ethereum",
        "name": "Ethereum",
        "shortName": "ETH",
        "nativeSymbol": "ETH",
        "chainNumericId": 1,
        "explorer": "https://etherscan.io",
        "geckoId": "eth",
        "dexScreenerId": "ethereum",
        "defillamaKey": "ethereum",
        "defillamaName": "Ethereum",
    },
    {
        "id": "bsc",
        "name": "BNB Smart Chain",
        "shortName": "BSC",
        "nativeSymbol": "BNB",
        "chainNumericId": 56,
        "explorer": "https://bscscan.com",
        "geckoId": "bsc",
        "dexScreenerId": "bsc",
        "defillamaKey": "bsc",
        "defillamaName": "BSC",
    },
    {
        "id": "base",
        "name": "Base",
        "shortName": "Base",
        "nativeSymbol": "ETH",
        "chainNumericId": 8453,
        "explorer": "https://basescan.org",
        "geckoId": "base",
        "dexScreenerId": "base",
        "defillamaKey": "base",
        "defillamaName": "Base",
    },
    {
        "id": "solana",
        "name": "Solana",
        "shortName": "Solana",
        "nativeSymbol": "SOL",
        "chainNumericId": "solana",
        "explorer": "https://solscan.io",
        "geckoId": "solana",
        "dexScreenerId": "solana",
        "defillamaKey": "solana",
        "defillamaName": "Solana",
    },
    {
        "id": "ton",
        "name": "TON / Gram",
        "shortName": "TON",
        "nativeSymbol": "TON",
        "chainNumericId": "ton",
        "explorer": "https://tonscan.org",
        "geckoId": "ton",
        "dexScreenerId": "ton",
        "defillamaKey": "ton",
        "defillamaName": "TON",
    },
    {
        "id": "morph",
        "name": "Morph",
        "shortName": "Morph",
        "nativeSymbol": "BGB",
        "chainNumericId": 2818,
        "explorer": "https://explorer.morphl2.io",
        "geckoId": "morph",
        "dexScreenerId": "morph",
        "defillamaKey": "morph",
        "defillamaName": "Morph",
    },
]

CHAIN_BY_ID = {chain["id"]: chain for chain in CHAIN_DEFS}
CHAIN_BY_GECKO = {chain["geckoId"]: chain["id"] for chain in CHAIN_DEFS}
CHAIN_BY_DEX = {chain["dexScreenerId"]: chain["id"] for chain in CHAIN_DEFS}


COMMON_ASSETS: List[Dict[str, Any]] = [
    {"symbol": "BTC", "name": "Bitcoin", "nameZh": "比特币", "chainId": "bitcoin", "aliases": ["btc", "bitcoin", "比特币", "大饼"]},
    {"symbol": "ETH", "name": "Ethereum", "nameZh": "以太坊", "chainId": "ethereum", "aliases": ["eth", "ethereum", "以太坊"]},
    {"symbol": "BNB", "name": "BNB", "nameZh": "币安币", "chainId": "bsc", "aliases": ["bnb", "币安币"]},
    {"symbol": "SOL", "name": "Solana", "nameZh": "索拉纳", "chainId": "solana", "aliases": ["sol", "solana", "索拉纳"]},
    {"symbol": "TON", "name": "Toncoin", "nameZh": "TON", "chainId": "ton", "aliases": ["ton", "toncoin", "gram", "ton币"]},
    {"symbol": "BGB", "name": "Bitget Token", "nameZh": "Bitget 平台币", "chainId": "morph", "aliases": ["bgb", "bitget token", "bitget平台币"]},
    {"symbol": "DOGE", "name": "Dogecoin", "nameZh": "狗狗币", "chainId": "dogecoin", "aliases": ["doge", "dogecoin", "狗狗币"]},
    {"symbol": "XRP", "name": "XRP", "nameZh": "瑞波币", "chainId": "xrp", "aliases": ["xrp", "ripple", "瑞波币"]},
    {"symbol": "USDT", "name": "Tether", "nameZh": "泰达币", "chainId": "multi", "aliases": ["usdt", "tether", "泰达币"]},
    {"symbol": "USDC", "name": "USD Coin", "nameZh": "美元币", "chainId": "multi", "aliases": ["usdc", "usd coin", "美元币"]},
]

# Binance's public ticker endpoint does not include an asset image.  Keep this
# small, explicit fallback limited to a well-known native asset; market data
# still comes from the selected market provider and never from this image URL.
COMMON_ASSET_LOGOS = {
    "BTC": "https://assets.coingecko.com/coins/images/1/large/bitcoin.png",
}


RANGE_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}


# K 线周期与资金活动窗口是两个不同概念。图表请求使用 `range=all`，由
# Provider 分页返回全部可取得历史；不能为了把数据压到固定根数而偷偷改掉
# 用户选择的周期。`CHART_RANGE_SECONDS` 仍保留给兼容旧调用的有限覆盖范围。
CHART_INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "1M": 30 * 24 * 60 * 60,
}

CHART_RANGE_SECONDS = {
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
    "90d": 90 * 24 * 60 * 60,
    "180d": 180 * 24 * 60 * 60,
    "1y": 365 * 24 * 60 * 60,
}

CHART_RANGE_ALL = "all"
CHART_PAGE_SIZE = 1000
MAX_CHART_CANDLES = CHART_PAGE_SIZE
# The newest chart page is deliberately short-lived: a stable cache key must
# not turn a moving "latest" request into a one-minute-old snapshot. Older
# cursor pages are immutable enough for a longer cache and should not consume
# provider budget again when the user browses backward.
CHART_LATEST_CACHE_TTL = 5
CHART_HISTORY_PAGE_CACHE_TTL = 300


BINANCE_INTERVALS = {
    "1m": ("1m", 60),
    "5m": ("1m", 60),
    "15m": ("1m", 60),
    "30m": ("1m", 60),
    "1h": ("1m", 60),
    "4h": ("5m", 5 * 60),
    "12h": ("15m", 15 * 60),
    "1d": ("15m", 15 * 60),
    "7d": ("1h", 60 * 60),
    "30d": ("4h", 4 * 60 * 60),
}


GECKO_INTERVALS = {
    "1m": ("minute", 1, 60),
    "5m": ("minute", 1, 60),
    "15m": ("minute", 1, 60),
    "30m": ("minute", 1, 60),
    "1h": ("minute", 1, 60),
    "4h": ("minute", 5, 5 * 60),
    "12h": ("minute", 15, 15 * 60),
    "1d": ("minute", 15, 15 * 60),
    "7d": ("hour", 1, 60 * 60),
    "30d": ("hour", 4, 4 * 60 * 60),
}


PROVIDER_DEFS: Dict[str, Dict[str, Any]] = {
    "binance": {"label": "Binance", "auth": "none", "maxPerMinute": 120, "capabilities": ["search", "quote", "ohlcv", "trades", "sampled-flow"]},
    "geckoterminal": {"label": "GeckoTerminal", "auth": "none", "maxPerMinute": 8, "capabilities": ["search", "pool", "ohlcv", "trades"]},
    "dexscreener": {"label": "DEX Screener", "auth": "none", "maxPerMinute": 120, "capabilities": ["search", "pool-summary", "buy-sell-count"]},
    "defillama": {"label": "DefiLlama", "auth": "none", "maxPerMinute": 6, "capabilities": ["chain-dex-overview"]},
    "robinhood_stock": {"label": "Robinhood Stock Token", "auth": "none", "maxPerMinute": 50, "capabilities": ["stock-token-search", "quote", "mint-burn-volume"]},
    "goplus": {"label": "GoPlus", "auth": "optional", "maxPerMinute": 20, "capabilities": ["token-risk"]},
}


ALLOWED_HOSTS = {
    "data-api.binance.vision",
    "api.geckoterminal.com",
    "api.dexscreener.com",
    "api.llama.fi",
    "api.robinhood.com",
    "api.gopluslabs.io",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_from_timestamp(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _sum_optional(left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None and right is None:
        return None
    return (left or 0.0) + (right or 0.0)


def _candle_bucket_start(timestamp: int, interval_key: str, step_seconds: int) -> int:
    if interval_key == "1M":
        current = datetime.fromtimestamp(timestamp, timezone.utc)
        return int(datetime(current.year, current.month, 1, tzinfo=timezone.utc).timestamp())
    return int(math.floor(timestamp / step_seconds) * step_seconds)


def _candle_end_timestamp(timestamp: int, interval_key: str, step_seconds: int) -> int:
    if interval_key == "1M":
        current = datetime.fromtimestamp(timestamp, timezone.utc)
        if current.month == 12:
            next_month = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
        return int(next_month.timestamp())
    return timestamp + step_seconds


def _aggregate_candles(candles: Iterable[Dict[str, Any]], interval_key: str, step_seconds: int) -> List[Dict[str, Any]]:
    """Aggregate real candles in UTC without filling missing periods."""
    grouped: Dict[int, Dict[str, Any]] = {}
    for candle in sorted(candles, key=lambda item: int(item.get("timestamp") or 0)):
        timestamp = int(candle.get("timestamp") or 0)
        if not timestamp:
            continue
        values = [_number(candle.get(key)) for key in ("open", "high", "low", "close")]
        if any(value is None for value in values):
            continue
        bucket = _candle_bucket_start(timestamp, interval_key, step_seconds)
        current = grouped.get(bucket)
        if current is None:
            grouped[bucket] = {
                "timestamp": bucket,
                "open": values[0],
                "high": values[1],
                "low": values[2],
                "close": values[3],
                "volume": _number(candle.get("volume")),
                "quoteVolume": _number(candle.get("quoteVolume")),
                "tradeCount": int(candle["tradeCount"]) if candle.get("tradeCount") is not None else None,
                "confirmed": bool(candle.get("confirmed", False)),
            }
            continue
        current["high"] = max(current["high"], values[1])
        current["low"] = min(current["low"], values[2])
        current["close"] = values[3]
        current["volume"] = _sum_optional(current.get("volume"), _number(candle.get("volume")))
        current["quoteVolume"] = _sum_optional(current.get("quoteVolume"), _number(candle.get("quoteVolume")))
        if candle.get("tradeCount") is not None:
            current["tradeCount"] = int(current["tradeCount"] or 0) + int(candle["tradeCount"])
        current["confirmed"] = bool(current["confirmed"] and candle.get("confirmed", False))
    return [grouped[key] for key in sorted(grouped)]


def _sum_numeric(value: Any) -> float:
    if isinstance(value, dict):
        return sum(_sum_numeric(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_sum_numeric(item) for item in value)
    return _number(value) or 0.0


def _clean_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _safe_https_url(value: Any) -> Optional[str]:
    text = _clean_text(value, 500)
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    return text if parsed.scheme == "https" and parsed.netloc else None


def _token_logo_url(*candidates: Any) -> Optional[str]:
    """Return a provider-supplied HTTPS token image URL, if one is available."""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("logoUrl", "logo_url", "imageUrl", "image_url", "logo", "image"):
            value = candidate.get(key)
            if isinstance(value, dict):
                value = value.get("url") or value.get("uri")
            safe_url = _safe_https_url(value)
            if safe_url:
                return safe_url
    return None


def _common_asset_logo(symbol: Any) -> Optional[str]:
    value = _clean_text(symbol, 32).upper()
    return _safe_https_url(COMMON_ASSET_LOGOS.get(value))


def _watchlist_items_with_common_logos(items: Any) -> List[Dict[str, Any]]:
    """Hydrate known common-asset icons without changing provider metrics."""
    if not isinstance(items, list):
        return []
    hydrated = []
    for item in items:
        if not isinstance(item, dict):
            continue
        copy = dict(item)
        if not _safe_https_url(copy.get("logoUrl")):
            logo = _common_asset_logo(copy.get("symbol"))
            if logo:
                copy["logoUrl"] = logo
        hydrated.append(copy)
    return hydrated


def _alias_for_query(query: str) -> Optional[Dict[str, Any]]:
    normalized = query.strip().lower()
    for asset in COMMON_ASSETS:
        names = set(asset.get("aliases", []))
        names.update({asset["symbol"].lower(), asset["name"].lower(), asset.get("nameZh", "").lower()})
        if normalized in names:
            return asset
    return None


def _localized_name(symbol: str, name: str = "") -> Optional[str]:
    symbol_lower = (symbol or "").lower()
    name_lower = (name or "").lower()
    for asset in COMMON_ASSETS:
        if symbol_lower == asset["symbol"].lower() or name_lower == asset["name"].lower():
            return asset.get("nameZh")
    return None


def _looks_like_address(query: str) -> bool:
    value = query.strip()
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", value):
        return True
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,48}", value):
        return True
    if re.fullmatch(r"(?:EQ|UQ)[A-Za-z0-9_-]{40,60}", value):
        return True
    return False


def _normalize_address(chain_id: str, address: str) -> str:
    """Normalize only address families where case is not identity-bearing."""
    value = _clean_text(address, 180)
    if chain_id in {"robinhood", "ethereum", "bsc", "base", "morph"} and value.startswith("0x"):
        return value.lower()
    return value


def _asset_id(kind: str, chain_id: str, identity: str) -> str:
    if kind == "token":
        identity = _normalize_address(chain_id, identity)
    return "%s:%s:%s" % (kind, chain_id, identity)


def _parse_asset_id(asset_id: str) -> Tuple[str, str, str]:
    parts = (asset_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] not in {"native", "token", "cex-asset"}:
        raise TrendError("invalid_asset", "资产标识无效", 400)
    if parts[0] == "token":
        parts[2] = _normalize_address(parts[1], parts[2])
    return parts[0], parts[1], parts[2]


def _parse_market_id(market_id: str) -> Tuple[str, str, str]:
    parts = (market_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] not in {"cex", "dex", "stock"}:
        raise TrendError("invalid_market", "市场标识无效", 400)
    if parts[0] == "dex":
        parts[2] = _normalize_address(parts[1], parts[2])
    return parts[0], parts[1], parts[2]


def _market_id(kind: str, venue: str, identity: str) -> str:
    if kind == "dex":
        identity = _normalize_address(venue, identity)
    return "%s:%s:%s" % (kind, venue, identity)


def _breakdown_value(breakdown: Any, chain: Dict[str, Any]) -> float:
    if not isinstance(breakdown, dict):
        return 0.0
    candidates = {
        str(chain.get("defillamaKey") or "").lower(),
        str(chain.get("defillamaName") or "").lower(),
        str(chain.get("name") or "").lower(),
        str(chain.get("shortName") or "").lower(),
    }
    return sum(_sum_numeric(value) for key, value in breakdown.items() if str(key).lower() in candidates)


class TrendError(Exception):
    def __init__(self, code: str, message: str, status: int = 500, provider: Optional[str] = None, retry_after_ms: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.provider = provider
        self.retry_after_ms = retry_after_ms

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.provider:
            data["provider"] = self.provider
        if self.retry_after_ms is not None:
            data["retryAfterMs"] = self.retry_after_ms
        return data


@dataclass
class FetchResult:
    data: Any
    retrieved_at: str
    stale: bool = False


class TrendService:
    """Thread-safe facade around documented market-data providers."""

    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.watchlist_file = os.path.join(self.data_dir, ".seegent-trend.json")
        self._cache: Dict[str, Tuple[float, Any, str]] = {}
        self._cache_lock = threading.RLock()
        self._key_locks: Dict[str, threading.Lock] = {}
        self._rate_events: Dict[str, deque] = defaultdict(deque)
        self._rate_lock = threading.RLock()
        self._status: Dict[str, Dict[str, Any]] = {}
        self._watchlist_lock = threading.RLock()

    def set_data_dir(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.watchlist_file = os.path.join(self.data_dir, ".seegent-trend.json")

    # ----- public API -----

    def capabilities(self) -> Dict[str, Any]:
        providers = []
        for provider_id, definition in PROVIDER_DEFS.items():
            runtime = dict(self._status.get(provider_id, {}))
            providers.append({
                "id": provider_id,
                "label": definition["label"],
                "auth": definition["auth"],
                "maxPerMinute": definition["maxPerMinute"],
                "capabilities": list(definition["capabilities"]),
                "status": runtime.get("status", "unknown"),
                "lastSuccessAt": runtime.get("lastSuccessAt"),
                "lastErrorAt": runtime.get("lastErrorAt"),
                "lastError": runtime.get("lastError"),
            })
        optional = {
            "BIRDEYE_API_KEY": bool(os.environ.get("BIRDEYE_API_KEY")),
            "ALCHEMY_API_KEY": bool(os.environ.get("ALCHEMY_API_KEY")),
            "HELIUS_API_KEY": bool(os.environ.get("HELIUS_API_KEY")),
            "TONAPI_TOKEN": bool(os.environ.get("TONAPI_TOKEN")),
            "GOPLUS_ACCESS_TOKEN": bool(os.environ.get("GOPLUS_ACCESS_TOKEN")),
        }
        chains = []
        for definition in CHAIN_DEFS:
            chain = dict(definition)
            chain["capabilities"] = {
                "market": "provider-dependent",
                "ohlcv": "provider-dependent",
                "dexTrades": "provider-dependent",
                "tokenTransfers": "key-required",
                "bridgeFlow": "unavailable-v1",
                "risk": "provider-dependent",
            }
            chains.append(chain)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": _iso_now(),
            "chains": chains,
            "providers": providers,
            "optionalKeys": optional,
            "ranges": list(RANGE_SECONDS.keys()),
            "chartIntervals": list(CHART_INTERVAL_SECONDS.keys()),
            "chartRanges": list(CHART_RANGE_SECONDS.keys()) + [CHART_RANGE_ALL],
            "chartHistory": {
                "defaultRange": CHART_RANGE_ALL,
                "pagination": True,
                "pageSize": CHART_PAGE_SIZE,
                "semantics": "每根 K 线严格等于所选周期；全历史按 Provider 分页读取",
            },
            "limits": {"maxCandlesPerPage": MAX_CHART_CANDLES, "maxWatchlistItems": MAX_WATCHLIST_ITEMS},
        }

    def overview(self) -> Dict[str, Any]:
        errors: List[Dict[str, Any]] = []
        cards: Dict[str, Dict[str, Any]] = {}
        for chain in CHAIN_DEFS:
            cards[chain["id"]] = {
                "chainId": chain["id"],
                "name": chain["name"],
                "shortName": chain["shortName"],
                "nativeSymbol": chain["nativeSymbol"],
                "explorer": chain["explorer"],
                "trackedDexVolume24hUsd": None,
                "trackedDexVolume30dUsd": None,
                "activeProtocols": None,
                "metricStatus": "unavailable",
            }
        retrieved_at = _iso_now()
        stale = False
        try:
            url = "https://api.llama.fi/overview/dexs?" + urlencode({
                "excludeTotalDataChart": "true",
                "excludeTotalDataChartBreakdown": "true",
                "dataType": "dailyVolume",
            })
            fetched = self._request_json("defillama", url, ttl=300)
            retrieved_at = fetched.retrieved_at
            stale = fetched.stale
            protocols = fetched.data.get("protocols", []) if isinstance(fetched.data, dict) else []
            for protocol in protocols:
                if not isinstance(protocol, dict) or protocol.get("doublecounted"):
                    continue
                breakdown_24h = protocol.get("breakdown24h") or {}
                breakdown_30d = protocol.get("breakdown30d") or {}
                for chain in CHAIN_DEFS:
                    value_24h = _breakdown_value(breakdown_24h, chain)
                    value_30d = _breakdown_value(breakdown_30d, chain)
                    card = cards[chain["id"]]
                    if value_24h > 0:
                        card["trackedDexVolume24hUsd"] = (card["trackedDexVolume24hUsd"] or 0) + value_24h
                        card["activeProtocols"] = (card["activeProtocols"] or 0) + 1
                    if value_30d > 0:
                        card["trackedDexVolume30dUsd"] = (card["trackedDexVolume30dUsd"] or 0) + value_30d
            available_names = {str(name).lower() for name in fetched.data.get("allChains", [])} if isinstance(fetched.data, dict) else set()
            for chain in CHAIN_DEFS:
                card = cards[chain["id"]]
                if card["trackedDexVolume24hUsd"] is not None or chain["defillamaName"].lower() in available_names:
                    card["metricStatus"] = "estimated"
        except TrendError as error:
            errors.append(error.as_dict())

        return {
            "schemaVersion": SCHEMA_VERSION,
            "chains": list(cards.values()),
            "meta": {
                "source": "DefiLlama",
                "provider": "defillama",
                "retrievedAt": retrieved_at,
                "coverageStart": None,
                "coverageEnd": retrieved_at,
                "nativeOrAggregated": "aggregated-tracked-protocols",
                "metricDefinition": "Tracked DEX protocol volume; excludes rows marked double-counted and is not a guarantee of complete chain volume.",
                "stale": stale,
                "partialErrors": errors,
            },
        }

    def search(self, query: str, chain_id: str = "", limit: int = 20) -> Dict[str, Any]:
        query = _clean_text(query, 120)
        if not query:
            raise TrendError("missing_query", "请输入币名、Symbol 或合约地址", 400)
        if chain_id and chain_id not in CHAIN_BY_ID:
            raise TrendError("invalid_chain", "不支持的链", 400)
        limit = min(max(int(limit or 20), 1), MAX_SEARCH_RESULTS)
        errors: List[Dict[str, Any]] = []
        results: List[Dict[str, Any]] = self._search_catalog(query, chain_id)

        jobs = {
            "binance": lambda: self._search_binance(query, chain_id),
            "dexscreener": lambda: self._search_dexscreener(query, chain_id),
            "geckoterminal": lambda: self._search_geckoterminal(query, chain_id),
            "robinhood_stock": lambda: self._search_robinhood(query, chain_id),
        }
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="trend-search") as pool:
            future_map = {pool.submit(job): provider for provider, job in jobs.items()}
            for future in as_completed(future_map):
                provider = future_map[future]
                try:
                    results.extend(future.result())
                except TrendError as error:
                    # A guessed Binance symbol returning 400 is a normal no-result condition.
                    if not (provider == "binance" and error.status in {400, 404}):
                        errors.append(error.as_dict())
                except Exception as error:  # provider isolation: one source must not fail the search
                    errors.append({"code": "provider_error", "message": _clean_text(error), "provider": provider})

        merged: Dict[str, Dict[str, Any]] = {}
        for result in results:
            market_id = result.get("market", {}).get("marketId")
            if not market_id:
                continue
            current = merged.get(market_id)
            if not current:
                merged[market_id] = result
                continue
            providers = set(current.get("providers", [])) | set(result.get("providers", []))
            current["providers"] = sorted(providers)
            for key in ("priceUsd", "change24h", "volume24hUsd", "liquidityUsd", "marketCapUsd", "fdvUsd"):
                if current.get("quote", {}).get(key) is None and result.get("quote", {}).get(key) is not None:
                    current.setdefault("quote", {})[key] = result["quote"][key]
            if current.get("metricStatus") == "unavailable" and result.get("metricStatus") != "unavailable":
                current["metricStatus"] = result.get("metricStatus")
                current["source"] = result.get("source", current.get("source"))

        exact_address = query.lower() if _looks_like_address(query) else ""
        alias = _alias_for_query(query)

        def sort_key(item: Dict[str, Any]) -> Tuple[int, int, int, int, float, float]:
            asset = item.get("asset", {})
            market = item.get("market", {})
            address = str(asset.get("address") or "").lower()
            exact = int(bool(exact_address and address == exact_address))
            market_type = market.get("type")
            trusted_market = 2 if market_type in {"cex", "stock-token"} else 1
            known_native = int(bool(
                alias
                and str(asset.get("symbol") or "").upper() == alias["symbol"]
                and asset.get("kind") == "native"
                and market_type == "cex"
            ))
            live = int(item.get("metricStatus") != "unavailable")
            quote_data = item.get("quote", {})
            # Contract-address searches are exact-first. For names/symbols, verified
            # centralized or official stock-token markets outrank spoofable DEX names.
            return (
                exact,
                trusted_market,
                known_native,
                live,
                _number(quote_data.get("volume24hUsd")) or 0,
                _number(quote_data.get("liquidityUsd")) or 0,
            )

        ordered = sorted(merged.values(), key=sort_key, reverse=True)[:limit]
        return {
            "schemaVersion": SCHEMA_VERSION,
            "query": query,
            "chainId": chain_id or None,
            "results": ordered,
            "meta": {
                "source": "multi-provider",
                "provider": "trend-search",
                "retrievedAt": _iso_now(),
                "coverageStart": None,
                "coverageEnd": _iso_now(),
                "nativeOrAggregated": "provider-separated",
                "partialErrors": errors,
            },
        }

    def asset(self, asset_id: str, market_id: str) -> Dict[str, Any]:
        kind, venue, identity = _parse_market_id(market_id)
        if kind == "cex" and venue == "binance":
            return self._binance_asset(asset_id, identity)
        if kind == "stock" and venue == "robinhood":
            return self._robinhood_asset(asset_id, identity)
        if kind == "dex":
            return self._dex_asset(asset_id, venue, identity)
        raise TrendError("unsupported_market", "当前市场暂不支持详情", 422)

    def ohlcv(
        self,
        market_id: str,
        range_key: str,
        interval_key: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return K-lines using legacy range semantics or exact chart controls.

        Calls without ``interval_key`` remain compatible with the first Trend
        API release. Chart callers use ``range=all`` and follow ``nextCursor``
        until the provider has no older page left. A cursor is a provider
        independent Unix timestamp boundary; the provider-specific request
        still stays inside this module's allow-list and pagination budget.
        """
        if interval_key:
            range_key = self._validate_chart_range(range_key)
            interval_key = self._validate_chart_interval(interval_key)
        else:
            range_key = self._validate_range(range_key)
        kind, venue, identity = _parse_market_id(market_id)
        if kind == "cex" and venue == "binance":
            return self._binance_ohlcv(identity, range_key, interval_key, cursor)
        if kind == "dex":
            return self._gecko_ohlcv(venue, identity, range_key, interval_key, cursor)
        return self._unavailable_metric("ohlcv", "该市场没有公开的历史 K 线接口", market_id=market_id, range_key=range_key, interval_key=interval_key)

    def flows(self, asset_id: str, market_id: str, range_key: str) -> Dict[str, Any]:
        range_key = self._validate_range(range_key)
        kind, venue, identity = _parse_market_id(market_id)
        if kind == "cex" and venue == "binance":
            return self._binance_flow(identity, range_key)
        if kind == "dex":
            return self._dex_flow(asset_id, venue, identity, range_key)
        if kind == "stock" and venue == "robinhood":
            detail = self._robinhood_asset(asset_id, identity)
            quote_data = detail.get("quote", {})
            return {
                "schemaVersion": SCHEMA_VERSION,
                "range": range_key,
                "metrics": {
                    "tradeVolumeUsd": None,
                    "tradeVolumeToken": quote_data.get("dailyTradingVolumeToken") if range_key == "1d" else None,
                    "buyVolumeUsd": None,
                    "sellVolumeUsd": None,
                    "buyCount": None,
                    "sellCount": None,
                    "transferAmountToken": None,
                    "transferValueUsd": None,
                    "transferCount": None,
                    "uniqueWallets": None,
                    "mintBurnTokenVolume": quote_data.get("mintBurnTokenVolume") if range_key == "1d" else None,
                    "mintBurnUsdVolume": quote_data.get("mintBurnUsdVolume") if range_key == "1d" else None,
                },
                "meta": {
                    "source": "Robinhood Stock Token",
                    "provider": "robinhood_stock",
                    "retrievedAt": detail["meta"]["retrievedAt"],
                    "coverageStart": None,
                    "coverageEnd": detail["meta"]["retrievedAt"],
                    "nativeOrAggregated": "source-provided-daily" if range_key == "1d" else "unavailable",
                    "metricStatus": "partial" if range_key == "1d" else "unavailable",
                    "metricDefinition": "dailyTradingVolume is reported in token/share units; it is not treated as USD volume.",
                    "partialErrors": [] if range_key == "1d" else [{"code": "unsupported_range", "message": "Robinhood 公开接口仅提供日内汇总"}],
                },
            }
        return self._unavailable_metric("flows", "该市场没有公开的资金活动接口", market_id=market_id, range_key=range_key)

    def trades(self, market_id: str, range_key: str, limit: int = 80) -> Dict[str, Any]:
        range_key = self._validate_range(range_key)
        limit = min(max(int(limit or 80), 1), 200)
        kind, venue, identity = _parse_market_id(market_id)
        if kind == "cex" and venue == "binance":
            return self._binance_trades(identity, range_key, limit)
        if kind == "dex":
            return self._gecko_trades(venue, identity, range_key, limit)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "range": range_key,
            "trades": [],
            "nextCursor": None,
            "meta": {
                "source": None,
                "provider": None,
                "retrievedAt": _iso_now(),
                "coverageStart": None,
                "coverageEnd": None,
                "nativeOrAggregated": "unavailable",
                "metricStatus": "unavailable",
                "partialErrors": [{"code": "unavailable", "message": "该市场没有公开的逐笔成交接口"}],
            },
        }

    def risk(self, asset_id: str) -> Dict[str, Any]:
        kind, chain_id, address = _parse_asset_id(asset_id)
        if kind != "token" or chain_id not in CHAIN_BY_ID or not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
            return self._risk_unavailable("当前资产不适用 EVM Token 风险检测")
        chain_numeric = CHAIN_BY_ID[chain_id].get("chainNumericId")
        if not isinstance(chain_numeric, int):
            return self._risk_unavailable("当前链暂未接入风险检测")
        url = "https://api.gopluslabs.io/api/v1/token_security/%s?%s" % (
            chain_numeric,
            urlencode({"contract_addresses": address.lower()}),
        )
        headers = {}
        token = os.environ.get("GOPLUS_ACCESS_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            fetched = self._request_json("goplus", url, ttl=600, headers=headers)
            body = fetched.data if isinstance(fetched.data, dict) else {}
            result = body.get("result") or {}
            info = result.get(address.lower()) or result.get(address) or {}
            if not info:
                return self._risk_unavailable("风险数据源没有返回该合约", provider="goplus")
            flags = []
            critical_fields = {
                "is_honeypot": "疑似蜜罐",
                "cannot_sell_all": "可能无法全部卖出",
                "owner_change_balance": "所有者可修改余额",
                "hidden_owner": "可能存在隐藏所有者",
                "selfdestruct": "合约包含自毁能力",
            }
            caution_fields = {
                "is_open_source": "合约未开源",
                "is_proxy": "代理合约",
                "slippage_modifiable": "滑点可能被修改",
                "transfer_pausable": "转账可能暂停",
            }
            for field, label in critical_fields.items():
                if str(info.get(field, "0")) == "1":
                    flags.append({"code": field, "label": label, "severity": "high"})
            if str(info.get("is_open_source", "1")) == "0":
                flags.append({"code": "is_open_source", "label": caution_fields["is_open_source"], "severity": "medium"})
            for field, label in caution_fields.items():
                if field != "is_open_source" and str(info.get(field, "0")) == "1":
                    flags.append({"code": field, "label": label, "severity": "medium"})
            buy_tax = _number(info.get("buy_tax"))
            sell_tax = _number(info.get("sell_tax"))
            if (buy_tax or 0) > 0.1 or (sell_tax or 0) > 0.1:
                flags.append({"code": "high_tax", "label": "交易税较高", "severity": "medium"})
            level = "high" if any(flag["severity"] == "high" for flag in flags) else ("caution" if flags else "low")
            return {
                "schemaVersion": SCHEMA_VERSION,
                "risk": {
                    "level": level,
                    "label": {"high": "高风险信号", "caution": "需要留意", "low": "未发现明显风险"}[level],
                    "isHoneypot": str(info.get("is_honeypot", "0")) == "1",
                    "isOpenSource": str(info.get("is_open_source", "0")) == "1",
                    "isProxy": str(info.get("is_proxy", "0")) == "1",
                    "buyTax": buy_tax,
                    "sellTax": sell_tax,
                    "holderCount": _number(info.get("holder_count")),
                    "flags": flags,
                    "disclaimer": "第三方风险检测结果，不构成安全认证或投资建议。",
                },
                "meta": {
                    "source": "GoPlus",
                    "provider": "goplus",
                    "retrievedAt": fetched.retrieved_at,
                    "coverageStart": None,
                    "coverageEnd": fetched.retrieved_at,
                    "nativeOrAggregated": "source-provided",
                    "metricStatus": "partial" if fetched.stale else "exact",
                    "stale": fetched.stale,
                    "partialErrors": [],
                },
            }
        except TrendError as error:
            result = self._risk_unavailable("风险检测暂不可用", provider="goplus")
            result["meta"]["partialErrors"] = [error.as_dict()]
            return result

    def get_watchlist(self) -> Dict[str, Any]:
        with self._watchlist_lock:
            data = self._load_watchlist()
        return {
            "schemaVersion": SCHEMA_VERSION,
            "items": _watchlist_items_with_common_logos(data.get("items", [])),
            "updatedAt": data.get("updatedAt"),
        }

    def save_watch_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(item, dict):
            raise TrendError("invalid_item", "关注对象格式无效", 400)
        asset_id = _clean_text(item.get("assetId"), 240)
        market_id = _clean_text(item.get("marketId"), 240)
        _parse_asset_id(asset_id)
        _parse_market_id(market_id)
        target_id = asset_id + "|" + market_id
        quote_input = item.get("quote") if isinstance(item.get("quote"), dict) else {}
        quote_meta_input = item.get("quoteMeta") if isinstance(item.get("quoteMeta"), dict) else {}
        quote_snapshot = {
            "priceUsd": _number(quote_input.get("priceUsd")),
            "change1h": _number(quote_input.get("change1h")),
            "change6h": _number(quote_input.get("change6h")),
            "change24h": _number(quote_input.get("change24h")),
            "volume24hUsd": _number(quote_input.get("volume24hUsd")),
            "liquidityUsd": _number(quote_input.get("liquidityUsd")),
            "marketCapUsd": _number(quote_input.get("marketCapUsd")),
        }
        quote_meta = {
            "provider": _clean_text(quote_meta_input.get("provider") or item.get("provider"), 40),
            "retrievedAt": _clean_text(quote_meta_input.get("retrievedAt"), 64) or _iso_now(),
            "metricStatus": _clean_text(quote_meta_input.get("metricStatus"), 24) or "partial",
        }
        normalized = {
            "targetId": target_id,
            "assetId": asset_id,
            "marketId": market_id,
            "symbol": _clean_text(item.get("symbol"), 32),
            "name": _clean_text(item.get("name"), 120),
            "nameZh": _clean_text(item.get("nameZh"), 120),
            "logoUrl": _safe_https_url(item.get("logoUrl")) or _common_asset_logo(item.get("symbol")),
            "chainId": _clean_text(item.get("chainId"), 40),
            "marketLabel": _clean_text(item.get("marketLabel"), 120),
            "provider": _clean_text(item.get("provider"), 40),
            "quote": quote_snapshot,
            "quoteMeta": quote_meta,
            "addedAt": _iso_now(),
        }
        with self._watchlist_lock:
            data = self._load_watchlist()
            items = [existing for existing in data.get("items", []) if existing.get("targetId") != target_id]
            items.insert(0, normalized)
            data = {"schemaVersion": SCHEMA_VERSION, "updatedAt": _iso_now(), "items": items[:MAX_WATCHLIST_ITEMS]}
            self._write_watchlist(data)
        return {"ok": True, "item": normalized, "items": _watchlist_items_with_common_logos(data["items"])}

    def delete_watch_item(self, target_id: str) -> Dict[str, Any]:
        target_id = _clean_text(target_id, 500)
        if not target_id:
            raise TrendError("missing_target", "缺少关注对象标识", 400)
        with self._watchlist_lock:
            data = self._load_watchlist()
            before = len(data.get("items", []))
            items = [item for item in data.get("items", []) if item.get("targetId") != target_id]
            data = {"schemaVersion": SCHEMA_VERSION, "updatedAt": _iso_now(), "items": items}
            self._write_watchlist(data)
        return {"ok": True, "removed": before - len(items), "items": _watchlist_items_with_common_logos(items)}

    # ----- provider implementation -----

    def _search_catalog(self, query: str, chain_id: str) -> List[Dict[str, Any]]:
        alias = _alias_for_query(query)
        if not alias:
            return []
        if chain_id and alias.get("chainId") not in {chain_id, "multi"}:
            return []
        symbol = alias["symbol"]
        market_id = "cex:binance:%sUSDT" % symbol
        asset_kind = "native" if alias.get("chainId") not in {"multi"} else "cex-asset"
        return [self._result(
            asset={
                "assetId": _asset_id(asset_kind, alias.get("chainId", "unknown"), symbol),
                "kind": asset_kind,
                "chainId": alias.get("chainId"),
                "address": None,
                "symbol": symbol,
                "name": alias["name"],
                "nameZh": alias.get("nameZh"),
                "logoUrl": _common_asset_logo(symbol),
            },
            market={"marketId": market_id, "type": "cex", "venue": "Binance", "pair": "%s/USDT" % symbol, "poolAddress": None},
            quote_data={},
            provider="catalog",
            source_label="内置别名",
            metric_status="unavailable",
        )]

    def _search_binance(self, query: str, chain_id: str) -> List[Dict[str, Any]]:
        alias = _alias_for_query(query)
        if alias:
            symbol = alias["symbol"]
            if chain_id and alias.get("chainId") not in {chain_id, "multi"}:
                return []
        else:
            symbol = re.sub(r"[^A-Za-z0-9]", "", query).upper()
            if not 2 <= len(symbol) <= 15:
                return []
        if symbol == "USDT":
            return []
        pair = symbol + "USDT"
        url = "https://data-api.binance.vision/api/v3/ticker/24hr?" + urlencode({"symbol": pair})
        fetched = self._request_json("binance", url, ttl=10)
        data = fetched.data if isinstance(fetched.data, dict) else {}
        if not data.get("symbol"):
            return []
        definition = alias or {"symbol": symbol, "name": symbol, "nameZh": None, "chainId": chain_id or "unknown"}
        asset_kind = "native" if alias and alias.get("chainId") != "multi" else "cex-asset"
        return [self._result(
            asset={
                "assetId": _asset_id(asset_kind, definition.get("chainId") or "unknown", symbol),
                "kind": asset_kind,
                "chainId": definition.get("chainId"),
                "address": None,
                "symbol": symbol,
                "name": definition.get("name") or symbol,
                "nameZh": definition.get("nameZh"),
                "logoUrl": _common_asset_logo(symbol),
            },
            market={"marketId": "cex:binance:" + pair, "type": "cex", "venue": "Binance", "pair": "%s/USDT" % symbol, "poolAddress": None},
            quote_data={
                "priceUsd": _number(data.get("lastPrice")),
                "change24h": _number(data.get("priceChangePercent")),
                "volume24hUsd": _number(data.get("quoteVolume")),
                "baseVolume24h": _number(data.get("volume")),
                "high24h": _number(data.get("highPrice")),
                "low24h": _number(data.get("lowPrice")),
                "change1h": None,
                "change6h": None,
                "liquidityUsd": None,
                "marketCapUsd": None,
                "fdvUsd": None,
            },
            provider="binance",
            source_label="Binance",
            source_url="https://www.binance.com/en/trade/%s_USDT" % symbol,
            metric_status="partial" if fetched.stale else "exact",
        )]

    def _search_dexscreener(self, query: str, chain_id: str) -> List[Dict[str, Any]]:
        url = "https://api.dexscreener.com/latest/dex/search?" + urlencode({"q": query})
        fetched = self._request_json("dexscreener", url, ttl=60)
        pairs = fetched.data.get("pairs", []) if isinstance(fetched.data, dict) else []
        results = []
        for pair in pairs[:50]:
            if not isinstance(pair, dict):
                continue
            canonical_chain = CHAIN_BY_DEX.get(str(pair.get("chainId") or "").lower())
            if not canonical_chain or (chain_id and canonical_chain != chain_id):
                continue
            base = pair.get("baseToken") or {}
            quote_token = pair.get("quoteToken") or {}
            selected = base
            if _looks_like_address(query):
                if str(quote_token.get("address") or "").lower() == query.lower():
                    selected = quote_token
                elif str(base.get("address") or "").lower() != query.lower():
                    continue
            address = _normalize_address(canonical_chain, selected.get("address"))
            pool = _normalize_address(canonical_chain, pair.get("pairAddress"))
            if not address or not pool:
                continue
            symbol = _clean_text(selected.get("symbol"), 32)
            name = _clean_text(selected.get("name"), 120) or symbol
            logo_url = _token_logo_url(selected, pair.get("info") if selected is base else None)
            volume = pair.get("volume") or {}
            change = pair.get("priceChange") or {}
            liquidity = pair.get("liquidity") or {}
            results.append(self._result(
                asset={
                    "assetId": _asset_id("token", canonical_chain, address),
                    "kind": "token",
                    "chainId": canonical_chain,
                    "address": address,
                    "symbol": symbol,
                    "name": name,
                    "nameZh": _localized_name(symbol, name),
                    "logoUrl": logo_url,
                },
                market={
                    "marketId": _market_id("dex", canonical_chain, pool),
                    "type": "dex",
                    "venue": _clean_text(pair.get("dexId"), 80) or "DEX",
                    "pair": "%s/%s" % (base.get("symbol", "?"), quote_token.get("symbol", "?")),
                    "poolAddress": pool,
                    "createdAt": pair.get("pairCreatedAt"),
                },
                quote_data={
                    "priceUsd": _number(pair.get("priceUsd")),
                    "change1h": _number(change.get("h1")),
                    "change6h": _number(change.get("h6")),
                    "change24h": _number(change.get("h24")),
                    "volume24hUsd": _number(volume.get("h24")),
                    "liquidityUsd": _number(liquidity.get("usd")),
                    "marketCapUsd": _number(pair.get("marketCap")),
                    "fdvUsd": _number(pair.get("fdv")),
                    "buyCount24h": _number((pair.get("txns") or {}).get("h24", {}).get("buys")),
                    "sellCount24h": _number((pair.get("txns") or {}).get("h24", {}).get("sells")),
                },
                provider="dexscreener",
                source_label="DEX Screener",
                source_url=_safe_https_url(pair.get("url")),
                metric_status="partial" if fetched.stale else "exact",
            ))
        return results

    def _search_geckoterminal(self, query: str, chain_id: str) -> List[Dict[str, Any]]:
        url = "https://api.geckoterminal.com/api/v2/search/pools?" + urlencode({"query": query, "include": "base_token,quote_token"})
        fetched = self._request_json(
            "geckoterminal",
            url,
            ttl=60,
            headers={"Accept": "application/json;version=20230203"},
        )
        body = fetched.data if isinstance(fetched.data, dict) else {}
        included = {item.get("id"): item for item in body.get("included", []) if isinstance(item, dict)}
        results = []
        for pool_item in body.get("data", [])[:50]:
            if not isinstance(pool_item, dict):
                continue
            attrs = pool_item.get("attributes") or {}
            rel = pool_item.get("relationships") or {}
            network_id = ((rel.get("network") or {}).get("data") or {}).get("id")
            if not network_id:
                pool_id = str(pool_item.get("id") or "")
                network_id = pool_id.split("_", 1)[0] if "_" in pool_id else ""
            canonical_chain = CHAIN_BY_GECKO.get(str(network_id).lower())
            if not canonical_chain or (chain_id and canonical_chain != chain_id):
                continue
            base_ref = ((rel.get("base_token") or {}).get("data") or {}).get("id")
            quote_ref = ((rel.get("quote_token") or {}).get("data") or {}).get("id")
            base = (included.get(base_ref) or {}).get("attributes") or {}
            quote_token = (included.get(quote_ref) or {}).get("attributes") or {}
            selected = base
            if _looks_like_address(query):
                if str(quote_token.get("address") or "").lower() == query.lower():
                    selected = quote_token
                elif str(base.get("address") or "").lower() != query.lower():
                    continue
            address = _normalize_address(canonical_chain, selected.get("address"))
            pool_address = _normalize_address(canonical_chain, attrs.get("address"))
            if not address or not pool_address:
                continue
            symbol = _clean_text(selected.get("symbol"), 32)
            name = _clean_text(selected.get("name"), 120) or symbol
            logo_url = _token_logo_url(selected)
            volume = attrs.get("volume_usd") or {}
            change = attrs.get("price_change_percentage") or {}
            txns = attrs.get("transactions") or {}
            results.append(self._result(
                asset={
                    "assetId": _asset_id("token", canonical_chain, address),
                    "kind": "token",
                    "chainId": canonical_chain,
                    "address": address,
                    "symbol": symbol,
                    "name": name,
                    "nameZh": _localized_name(symbol, name),
                    "logoUrl": logo_url,
                },
                market={
                    "marketId": _market_id("dex", canonical_chain, pool_address),
                    "type": "dex",
                    "venue": "GeckoTerminal",
                    "pair": _clean_text(attrs.get("name"), 100),
                    "poolAddress": pool_address,
                    "createdAt": attrs.get("pool_created_at"),
                },
                quote_data={
                    "priceUsd": _number(attrs.get("base_token_price_usd")) if selected is base else _number(attrs.get("quote_token_price_usd")),
                    "change1h": _number(change.get("h1")),
                    "change6h": _number(change.get("h6")),
                    "change24h": _number(change.get("h24")),
                    "volume24hUsd": _number(volume.get("h24")),
                    "liquidityUsd": _number(attrs.get("reserve_in_usd")),
                    "marketCapUsd": _number(attrs.get("market_cap_usd")),
                    "fdvUsd": _number(attrs.get("fdv_usd")),
                    "buyCount24h": _number((txns.get("h24") or {}).get("buys")),
                    "sellCount24h": _number((txns.get("h24") or {}).get("sells")),
                },
                provider="geckoterminal",
                source_label="GeckoTerminal",
                source_url="https://www.geckoterminal.com/%s/pools/%s" % (network_id, pool_address),
                metric_status="partial" if fetched.stale else "exact",
            ))
        return results

    def _search_robinhood(self, query: str, chain_id: str) -> List[Dict[str, Any]]:
        if chain_id and chain_id != "robinhood":
            return []
        fetched = self._request_json("robinhood_stock", "https://api.robinhood.com/rhj/assets", ttl=300)
        assets = fetched.data.get("assets", []) if isinstance(fetched.data, dict) else []
        needle = query.lower()
        matches = []
        for item in assets:
            if not isinstance(item, dict):
                continue
            deployments = item.get("deployments") or []
            deployment = next((entry for entry in deployments if str(entry.get("chainId")) == "4663"), deployments[0] if deployments else {})
            address = _normalize_address("robinhood", deployment.get("contractAddress"))
            symbol = _clean_text(item.get("tokenSymbol"), 32)
            name = _clean_text(item.get("tokenName"), 160)
            if needle not in symbol.lower() and needle not in name.lower() and needle != address.lower():
                continue
            matches.append((item, address, symbol, name))
            if len(matches) >= 15:
                break
        quote_map: Dict[str, Dict[str, Any]] = {}
        if len(matches) == 1:
            try:
                quote_map[matches[0][2]] = self._robinhood_quote(matches[0][2])[0]
            except TrendError:
                pass
        results = []
        for item, address, symbol, name in matches:
            quote_data = quote_map.get(symbol, {})
            results.append(self._result(
                asset={
                    "assetId": _asset_id("token", "robinhood", address),
                    "kind": "token",
                    "chainId": "robinhood",
                    "address": address,
                    "symbol": symbol,
                    "name": name,
                    "nameZh": None,
                    "logoUrl": _safe_https_url(item.get("logoUrl")),
                },
                market={
                    "marketId": "stock:robinhood:" + symbol,
                    "type": "stock-token",
                    "venue": "Robinhood Stock Token",
                    "pair": symbol + "/USD",
                    "poolAddress": None,
                },
                quote_data=quote_data,
                provider="robinhood_stock",
                source_label="Robinhood Stock Token",
                source_url="https://robinhood.com/chain",
                metric_status="partial" if quote_data or fetched.stale else "unavailable",
            ))
        return results

    def _binance_asset(self, asset_id: str, symbol_pair: str) -> Dict[str, Any]:
        url = "https://data-api.binance.vision/api/v3/ticker/24hr?" + urlencode({"symbol": symbol_pair})
        fetched = self._request_json("binance", url, ttl=10)
        data = fetched.data if isinstance(fetched.data, dict) else {}
        base_symbol = symbol_pair[:-4] if symbol_pair.endswith("USDT") else symbol_pair
        alias = next((asset for asset in COMMON_ASSETS if asset["symbol"] == base_symbol), None)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "asset": {
                "assetId": asset_id,
                "kind": _parse_asset_id(asset_id)[0],
                "chainId": _parse_asset_id(asset_id)[1],
                "address": None,
                "symbol": base_symbol,
                "name": alias.get("name") if alias else base_symbol,
                "nameZh": alias.get("nameZh") if alias else None,
                "logoUrl": _common_asset_logo(base_symbol),
            },
            "market": {"marketId": "cex:binance:" + symbol_pair, "type": "cex", "venue": "Binance", "pair": "%s/USDT" % base_symbol, "sourceUrl": "https://www.binance.com/en/trade/%s_USDT" % base_symbol},
            "quote": {
                "priceUsd": _number(data.get("lastPrice")),
                "change24h": _number(data.get("priceChangePercent")),
                "volume24hUsd": _number(data.get("quoteVolume")),
                "baseVolume24h": _number(data.get("volume")),
                "high24h": _number(data.get("highPrice")),
                "low24h": _number(data.get("lowPrice")),
                "change1h": None,
                "change6h": None,
                "liquidityUsd": None,
                "marketCapUsd": None,
                "fdvUsd": None,
            },
            "meta": self._meta("Binance", "binance", fetched, "source-provided", "partial" if fetched.stale else "exact"),
        }

    def _robinhood_quote(self, symbol: str) -> Tuple[Dict[str, Any], FetchResult]:
        fetched = self._request_json("robinhood_stock", "https://api.robinhood.com/rhj/prices/" + quote(symbol), ttl=15)
        quotes = fetched.data.get("quotes", []) if isinstance(fetched.data, dict) else []
        item = quotes[0] if quotes else {}
        bid = _number(item.get("bid"))
        ask = _number(item.get("ask"))
        mid = (bid + ask) / 2 if bid is not None and ask is not None else (bid if bid is not None else ask)
        return ({
            "priceUsd": mid,
            "bidUsd": bid,
            "askUsd": ask,
            "change24h": None,
            "dailyTradingVolumeToken": _number(item.get("dailyTradingVolume")),
            "volume24hUsd": None,
            "high24h": _number(item.get("dailyHigh")),
            "low24h": _number(item.get("dailyLow")),
            "mintBurnTokenVolume": _number(item.get("mintBurnTokenVolume")),
            "mintBurnUsdVolume": _number(item.get("mintBurnUsdVolume")),
            "isTradingHalt": bool(item.get("isTradingHalt")),
            "generatedAt": item.get("generatedAt"),
            "liquidityUsd": None,
            "marketCapUsd": None,
            "fdvUsd": None,
        }, fetched)

    def _robinhood_asset(self, asset_id: str, symbol: str) -> Dict[str, Any]:
        assets_fetch = self._request_json("robinhood_stock", "https://api.robinhood.com/rhj/assets", ttl=300)
        items = assets_fetch.data.get("assets", []) if isinstance(assets_fetch.data, dict) else []
        item = next((entry for entry in items if str(entry.get("tokenSymbol", "")).upper() == symbol.upper()), {})
        quote_data, quote_fetch = self._robinhood_quote(symbol)
        deployments = item.get("deployments") or []
        deployment = next((entry for entry in deployments if str(entry.get("chainId")) == "4663"), deployments[0] if deployments else {})
        address = _normalize_address("robinhood", deployment.get("contractAddress"))
        return {
            "schemaVersion": SCHEMA_VERSION,
            "asset": {
                "assetId": asset_id,
                "kind": "token",
                "chainId": "robinhood",
                "address": address,
                "symbol": symbol,
                "name": _clean_text(item.get("tokenName"), 160) or symbol,
                "nameZh": None,
                "logoUrl": _safe_https_url(item.get("logoUrl")),
                "status": item.get("status"),
                "isin": item.get("isin"),
                "multiplier": item.get("currentMultiplier"),
            },
            "market": {"marketId": "stock:robinhood:" + symbol, "type": "stock-token", "venue": "Robinhood Stock Token", "pair": symbol + "/USD", "sourceUrl": "https://robinhood.com/chain"},
            "quote": quote_data,
            "meta": self._meta("Robinhood Stock Token", "robinhood_stock", quote_fetch, "source-provided-daily", "partial" if quote_fetch.stale else "exact"),
        }

    def _dex_asset(self, asset_id: str, chain_id: str, pool_address: str) -> Dict[str, Any]:
        if chain_id not in CHAIN_BY_ID:
            raise TrendError("invalid_chain", "不支持的 DEX 链", 400)
        chain = CHAIN_BY_ID[chain_id]
        url = "https://api.dexscreener.com/latest/dex/pairs/%s/%s" % (quote(chain["dexScreenerId"]), quote(pool_address))
        errors: List[Dict[str, Any]] = []
        try:
            fetched = self._request_json("dexscreener", url, ttl=15)
            pairs = fetched.data.get("pairs", []) if isinstance(fetched.data, dict) else []
            if pairs:
                pair = pairs[0]
                base = pair.get("baseToken") or {}
                quote_token = pair.get("quoteToken") or {}
                selected_address = _parse_asset_id(asset_id)[2].lower()
                selected = quote_token if str(quote_token.get("address", "")).lower() == selected_address else base
                symbol = _clean_text(selected.get("symbol"), 32)
                name = _clean_text(selected.get("name"), 120) or symbol
                change = pair.get("priceChange") or {}
                volume = pair.get("volume") or {}
                liquidity = pair.get("liquidity") or {}
                logo_url = _token_logo_url(selected, pair.get("info") if selected is base else None)
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "asset": {"assetId": asset_id, "kind": "token", "chainId": chain_id, "address": _normalize_address(chain_id, selected.get("address")), "symbol": symbol, "name": name, "nameZh": _localized_name(symbol, name), "logoUrl": logo_url},
                    "market": {
                        "marketId": _market_id("dex", chain_id, pool_address),
                        "type": "dex",
                        "venue": _clean_text(pair.get("dexId"), 80) or "DEX",
                        "pair": "%s/%s" % (base.get("symbol", "?"), quote_token.get("symbol", "?")),
                        "poolAddress": pool_address,
                        "createdAt": pair.get("pairCreatedAt"),
                        "sourceUrl": _safe_https_url(pair.get("url")),
                    },
                    "quote": {
                        "priceUsd": _number(pair.get("priceUsd")),
                        "change1h": _number(change.get("h1")),
                        "change6h": _number(change.get("h6")),
                        "change24h": _number(change.get("h24")),
                        "volume24hUsd": _number(volume.get("h24")),
                        "liquidityUsd": _number(liquidity.get("usd")),
                        "marketCapUsd": _number(pair.get("marketCap")),
                        "fdvUsd": _number(pair.get("fdv")),
                        "buyCount24h": _number((pair.get("txns") or {}).get("h24", {}).get("buys")),
                        "sellCount24h": _number((pair.get("txns") or {}).get("h24", {}).get("sells")),
                        "timeframes": {"volume": volume, "transactions": pair.get("txns") or {}, "change": change},
                    },
                    "meta": self._meta("DEX Screener", "dexscreener", fetched, "source-provided", "partial" if fetched.stale else "exact", errors),
                }
        except TrendError as error:
            errors.append(error.as_dict())

        gecko_id = chain["geckoId"]
        gecko_url = "https://api.geckoterminal.com/api/v2/networks/%s/pools/%s?include=base_token,quote_token" % (quote(gecko_id), quote(pool_address))
        fetched = self._request_json("geckoterminal", gecko_url, ttl=30, headers={"Accept": "application/json;version=20230203"})
        body = fetched.data if isinstance(fetched.data, dict) else {}
        item = body.get("data") or {}
        attrs = item.get("attributes") or {}
        included = {entry.get("id"): entry for entry in body.get("included", []) if isinstance(entry, dict)}
        relationships = item.get("relationships") or {}
        base_ref = ((relationships.get("base_token") or {}).get("data") or {}).get("id")
        quote_ref = ((relationships.get("quote_token") or {}).get("data") or {}).get("id")
        base_token = (included.get(base_ref) or {}).get("attributes") or {}
        quote_token = (included.get(quote_ref) or {}).get("attributes") or {}
        _, _, selected_identity = _parse_asset_id(asset_id)
        selected_token = quote_token if str(quote_token.get("address") or "").lower() == selected_identity.lower() else base_token
        pair_name = _clean_text(attrs.get("name"), 100) or "? / ?"
        selected_symbol = _clean_text(selected_token.get("symbol"), 32)
        selected_name = _clean_text(selected_token.get("name"), 120) or selected_symbol or pair_name.split(" / ", 1)[0]
        logo_url = _token_logo_url(selected_token)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "asset": {"assetId": asset_id, "kind": "token", "chainId": chain_id, "address": selected_identity, "symbol": selected_symbol or pair_name.split(" / ", 1)[0], "name": selected_name, "nameZh": _localized_name(selected_symbol, selected_name), "logoUrl": logo_url},
            "market": {"marketId": _market_id("dex", chain_id, pool_address), "type": "dex", "venue": "GeckoTerminal", "pair": pair_name, "poolAddress": _normalize_address(chain_id, pool_address), "sourceUrl": "https://www.geckoterminal.com/%s/pools/%s" % (gecko_id, pool_address)},
            "quote": {
                "priceUsd": _number(attrs.get("quote_token_price_usd")) if selected_token is quote_token else _number(attrs.get("base_token_price_usd")),
                "change1h": _number((attrs.get("price_change_percentage") or {}).get("h1")),
                "change6h": _number((attrs.get("price_change_percentage") or {}).get("h6")),
                "change24h": _number((attrs.get("price_change_percentage") or {}).get("h24")),
                "volume24hUsd": _number((attrs.get("volume_usd") or {}).get("h24")),
                "liquidityUsd": _number(attrs.get("reserve_in_usd")),
                "marketCapUsd": _number(attrs.get("market_cap_usd")),
                "fdvUsd": _number(attrs.get("fdv_usd")),
            },
            "meta": self._meta("GeckoTerminal", "geckoterminal", fetched, "source-provided", "partial" if fetched.stale else "exact", errors),
        }

    @staticmethod
    def _chart_cursor_ms(cursor: Optional[str]) -> Optional[int]:
        if cursor in (None, ""):
            return None
        try:
            value = float(cursor)
        except (TypeError, ValueError):
            raise TrendError("invalid_cursor", "K 线历史游标无效", 400)
        if not math.isfinite(value) or value <= 0 or value > 4102444800:
            raise TrendError("invalid_cursor", "K 线历史游标无效", 400)
        return int(value * 1000)

    @staticmethod
    def _binance_candles(rows: Any, now_ms: int) -> List[Dict[str, Any]]:
        candles = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list) or len(row) < 7:
                continue
            try:
                timestamp_ms = int(row[0])
            except (TypeError, ValueError):
                continue
            candles.append({
                "timestamp": int(timestamp_ms / 1000),
                "open": _number(row[1]),
                "high": _number(row[2]),
                "low": _number(row[3]),
                "close": _number(row[4]),
                "volume": _number(row[5]),
                "quoteVolume": _number(row[7]) if len(row) > 7 else None,
                "tradeCount": int(row[8]) if len(row) > 8 and row[8] is not None else None,
                "confirmed": int(row[6]) <= now_ms,
            })
        return sorted(candles, key=lambda item: item["timestamp"])

    def _binance_ohlcv(
        self,
        symbol: str,
        range_key: str,
        interval_key: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        legacy = not interval_key
        if legacy:
            interval, step_seconds = BINANCE_INTERVALS[range_key]
            duration = RANGE_SECONDS[range_key]
            requested_interval = None
            aggregation = "native"
            partial_errors: List[Dict[str, Any]] = []
            now_ms = int(time.time() * 1000)
            start_ms = now_ms - duration * 1000
            url = "https://data-api.binance.vision/api/v3/klines?" + urlencode({
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "limit": min(MAX_CHART_CANDLES, max(2, int(math.ceil(duration / step_seconds)) + 2)),
            })
            fetched = self._request_json(
                "binance",
                url,
                ttl=CHART_LATEST_CACHE_TTL,
                cache_key=f"ohlcv:binance:legacy:{symbol}:{range_key}:{interval}",
            )
            candles = self._binance_candles(fetched.data, now_ms)
            return self._ohlcv_response(
                candles,
                range_key,
                interval,
                step_seconds,
                "Binance",
                "binance",
                fetched,
                aggregation,
                requested_interval=requested_interval,
                requested_start=start_ms / 1000,
                partial_errors=partial_errors,
            )

        requested_interval = interval_key
        provider_interval = "1w" if interval_key == "7d" else interval_key
        step_seconds = CHART_INTERVAL_SECONDS[interval_key]
        aggregation = "native"
        duration = CHART_RANGE_SECONDS.get(range_key)
        now_ms = int(time.time() * 1000)
        requested_start_ms = now_ms - duration * 1000 if duration is not None else None
        cursor_ms = self._chart_cursor_ms(cursor)
        end_ms = cursor_ms - 1 if cursor_ms is not None else now_ms
        params = {
            "symbol": symbol,
            "interval": provider_interval,
            "endTime": max(end_ms, 0),
            "limit": CHART_PAGE_SIZE,
        }
        url = "https://data-api.binance.vision/api/v3/klines?" + urlencode(params)
        fetched = self._request_json(
            "binance",
            url,
            ttl=CHART_LATEST_CACHE_TTL if cursor_ms is None else CHART_HISTORY_PAGE_CACHE_TTL,
            cache_key=f"ohlcv:binance:{symbol}:{provider_interval}:{cursor or 'latest'}",
        )
        page_candles = self._binance_candles(fetched.data, now_ms)
        oldest_ms = int(page_candles[0]["timestamp"] * 1000) if page_candles else None
        candles = page_candles
        if requested_start_ms is not None:
            candles = [item for item in page_candles if int(item["timestamp"] * 1000) >= requested_start_ms]
        has_more = bool(page_candles) and len(page_candles) >= CHART_PAGE_SIZE
        if requested_start_ms is not None and oldest_ms is not None:
            has_more = oldest_ms > requested_start_ms + step_seconds * 1000
        next_cursor = str((oldest_ms - 1) / 1000) if has_more and oldest_ms and oldest_ms > 0 else None
        return self._ohlcv_response(
            candles,
            range_key,
            requested_interval,
            step_seconds,
            "Binance",
            "binance",
            fetched,
            aggregation,
            requested_interval=requested_interval,
            requested_start=requested_start_ms / 1000 if requested_start_ms is not None else None,
            partial_errors=[],
            next_cursor=next_cursor,
            has_more=has_more,
            provider_interval=provider_interval,
        )

    def _gecko_ohlcv(
        self,
        chain_id: str,
        pool_address: str,
        range_key: str,
        interval_key: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        if chain_id not in CHAIN_BY_ID:
            raise TrendError("invalid_chain", "不支持的 DEX 链", 400)
        legacy = not interval_key
        if legacy:
            timeframe, aggregate, step_seconds = GECKO_INTERVALS[range_key]
            duration = RANGE_SECONDS[range_key]
            requested_interval = None
            source_step = step_seconds
            aggregation = "native"
            partial_errors: List[Dict[str, Any]] = []
            response_step = step_seconds
        else:
            requested_interval = interval_key
            duration = CHART_RANGE_SECONDS.get(range_key)
            requested_seconds = CHART_INTERVAL_SECONDS[interval_key]
            if interval_key in {"1m", "5m", "15m", "30m"}:
                timeframe, aggregate, source_step = "minute", int(requested_seconds / 60), 60
                aggregation = "native"
            elif interval_key in {"1h", "4h", "12h"}:
                timeframe, aggregate, source_step = "hour", int(requested_seconds / 3600), 3600
                aggregation = "native"
            elif interval_key == "1d":
                timeframe, aggregate, source_step = "day", 1, 24 * 60 * 60
                aggregation = "native"
            else:
                # GeckoTerminal has no stable 7d/month aggregate. Fetch real
                # daily candles and aggregate them into the requested bucket.
                timeframe, aggregate, source_step = "day", 1, 24 * 60 * 60
                aggregation = "utc-aggregated"
            step_seconds = requested_seconds
            response_step = requested_seconds
            partial_errors = []

        now = int(time.time())
        requested_start = now - duration if duration is not None else None
        cursor_seconds = None
        if not legacy:
            cursor_ms = self._chart_cursor_ms(cursor)
            cursor_seconds = int(cursor_ms / 1000) if cursor_ms is not None else None
        before_timestamp = cursor_seconds - 1 if cursor_seconds is not None else now
        network = CHAIN_BY_ID[chain_id]["geckoId"]
        url = "https://api.geckoterminal.com/api/v2/networks/%s/pools/%s/ohlcv/%s?%s" % (
            quote(network),
            quote(pool_address),
            timeframe,
            urlencode({"aggregate": aggregate, "before_timestamp": before_timestamp, "limit": CHART_PAGE_SIZE if not legacy else min(MAX_CHART_CANDLES, max(2, int(math.ceil(duration / source_step)) + 2)), "currency": "usd"}),
        )
        cache_identity = cursor or (f"legacy:{range_key}" if legacy else "latest")
        fetched = self._request_json(
            "geckoterminal",
            url,
            ttl=CHART_LATEST_CACHE_TTL if cursor_seconds is None else CHART_HISTORY_PAGE_CACHE_TTL,
            headers={"Accept": "application/json;version=20230203"},
            cache_key=f"ohlcv:geckoterminal:{chain_id}:{pool_address}:{timeframe}:{aggregate}:{cache_identity}",
        )
        body = fetched.data if isinstance(fetched.data, dict) else {}
        rows = (((body.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
        source_candles = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                ts = int(row[0])
            except (TypeError, ValueError):
                continue
            source_candles.append({
                "timestamp": ts,
                "open": _number(row[1]),
                "high": _number(row[2]),
                "low": _number(row[3]),
                "close": _number(row[4]),
                "volume": _number(row[5]),
                "quoteVolume": _number(row[5]),
                "tradeCount": None,
                "confirmed": ts + source_step <= now,
            })
        source_candles.sort(key=lambda item: item["timestamp"])
        page_source_candles = source_candles
        if not legacy and requested_start is not None:
            source_candles = [item for item in page_source_candles if item["timestamp"] >= requested_start]
        candles = source_candles if legacy or aggregation == "native" else _aggregate_candles(source_candles, interval_key, response_step)
        page_limit = CHART_PAGE_SIZE if not legacy else min(MAX_CHART_CANDLES, max(2, int(math.ceil(duration / source_step)) + 2))
        oldest_ts = page_source_candles[0]["timestamp"] if page_source_candles else None
        has_more = bool(page_source_candles) and len(page_source_candles) >= page_limit
        if requested_start is not None and oldest_ts is not None:
            has_more = oldest_ts > requested_start + source_step
        next_cursor = str(oldest_ts - 1) if not legacy and has_more and oldest_ts and oldest_ts > 0 else None
        effective = interval_key if not legacy else "%s:%s" % (timeframe, aggregate)
        return self._ohlcv_response(
            candles,
            range_key,
            effective,
            response_step,
            "GeckoTerminal",
            "geckoterminal",
            fetched,
            aggregation,
            requested_interval=requested_interval,
            requested_start=requested_start,
            partial_errors=partial_errors,
            next_cursor=next_cursor,
            has_more=has_more,
            provider_interval="%s:%s" % (timeframe, aggregate) if not legacy else None,
        )

    def _binance_trades(self, symbol: str, range_key: str, limit: int) -> Dict[str, Any]:
        fetch_limit = min(max(limit, 80), 500)
        url = "https://data-api.binance.vision/api/v3/trades?" + urlencode({"symbol": symbol, "limit": fetch_limit})
        fetched = self._request_json("binance", url, ttl=10)
        cutoff_ms = int((time.time() - RANGE_SECONDS[range_key]) * 1000)
        trades = []
        for row in fetched.data if isinstance(fetched.data, list) else []:
            ts = int(row.get("time") or 0)
            if ts < cutoff_ms:
                continue
            qty = _number(row.get("qty"))
            price_value = _number(row.get("price"))
            quote_qty = _number(row.get("quoteQty"))
            trades.append({
                "id": str(row.get("id")),
                "timestamp": int(ts / 1000),
                "side": "sell" if row.get("isBuyerMaker") else "buy",
                "priceUsd": price_value,
                "amount": qty,
                "valueUsd": quote_qty if quote_qty is not None else ((qty or 0) * (price_value or 0)),
                "transactionHash": None,
                "sourceUrl": None,
            })
        trades.sort(key=lambda item: item["timestamp"], reverse=True)
        earliest = min((item["timestamp"] for item in trades), default=None)
        partial = earliest is None or earliest > int(cutoff_ms / 1000)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "range": range_key,
            "trades": trades[:limit],
            "nextCursor": None,
            "meta": {
                "source": "Binance",
                "provider": "binance",
                "retrievedAt": fetched.retrieved_at,
                "coverageStart": _iso_from_timestamp(earliest),
                "coverageEnd": fetched.retrieved_at,
                "nativeOrAggregated": "recent-trade-sample",
                "metricStatus": "partial" if partial or fetched.stale else "exact",
                "stale": fetched.stale,
                "partialErrors": [{"code": "sample_limited", "message": "公开 recent trades 仅覆盖最近一批成交"}] if partial else [],
            },
        }

    def _binance_flow(self, symbol: str, range_key: str) -> Dict[str, Any]:
        trades_response = self._binance_trades(symbol, range_key, 500)
        buy_volume = sum(item.get("valueUsd") or 0 for item in trades_response["trades"] if item.get("side") == "buy")
        sell_volume = sum(item.get("valueUsd") or 0 for item in trades_response["trades"] if item.get("side") == "sell")
        buy_count = sum(1 for item in trades_response["trades"] if item.get("side") == "buy")
        sell_count = sum(1 for item in trades_response["trades"] if item.get("side") == "sell")
        return {
            "schemaVersion": SCHEMA_VERSION,
            "range": range_key,
            "metrics": {
                "tradeVolumeUsd": buy_volume + sell_volume,
                "buyVolumeUsd": buy_volume,
                "sellVolumeUsd": sell_volume,
                "buyCount": buy_count,
                "sellCount": sell_count,
                "transferAmountToken": None,
                "transferValueUsd": None,
                "transferCount": None,
                "uniqueWallets": None,
            },
            "meta": dict(trades_response["meta"], metricDefinition="Recent public trade sample, not account deposits or withdrawals."),
        }

    def _dex_flow(self, asset_id: str, chain_id: str, pool_address: str, range_key: str) -> Dict[str, Any]:
        detail = self._dex_asset(asset_id, chain_id, pool_address)
        quote_data = detail.get("quote", {})
        timeframe_map = {"5m": "m5", "1h": "h1", "1d": "h24"}
        provider_key = timeframe_map.get(range_key)
        volume = None
        buy_count = None
        sell_count = None
        errors: List[Dict[str, Any]] = []
        if provider_key:
            frames = quote_data.get("timeframes") or {}
            volume = _number((frames.get("volume") or {}).get(provider_key))
            txns = (frames.get("transactions") or {}).get(provider_key) or {}
            buy_count = _number(txns.get("buys"))
            sell_count = _number(txns.get("sells"))
        if volume is None:
            try:
                candle_data = self._gecko_ohlcv(chain_id, pool_address, range_key)
                volume = sum(item.get("quoteVolume") or item.get("volume") or 0 for item in candle_data.get("candles", []))
                errors.append({"code": "ohlcv_aggregate", "message": "交易量由池 K 线聚合；买卖金额不可拆分"})
            except TrendError as error:
                errors.append(error.as_dict())
        return {
            "schemaVersion": SCHEMA_VERSION,
            "range": range_key,
            "metrics": {
                "tradeVolumeUsd": volume,
                "buyVolumeUsd": None,
                "sellVolumeUsd": None,
                "buyCount": buy_count,
                "sellCount": sell_count,
                "transferAmountToken": None,
                "transferValueUsd": None,
                "transferCount": None,
                "uniqueWallets": None,
            },
            "meta": {
                "source": detail.get("meta", {}).get("source"),
                "provider": detail.get("meta", {}).get("provider"),
                "retrievedAt": detail.get("meta", {}).get("retrievedAt") or _iso_now(),
                "coverageStart": None,
                "coverageEnd": detail.get("meta", {}).get("retrievedAt") or _iso_now(),
                "nativeOrAggregated": "source-window" if provider_key else "ohlcv-aggregated",
                "metricStatus": "partial",
                "metricDefinition": "DEX activity is measured from the selected pool and selected-asset perspective; it is not whole-token or whole-chain flow.",
                "partialErrors": errors + [{"code": "transfer_index_required", "message": "精确 Token Transfer 统计需要链上索引数据源"}],
            },
        }

    def _gecko_trades(self, chain_id: str, pool_address: str, range_key: str, limit: int) -> Dict[str, Any]:
        if chain_id not in CHAIN_BY_ID:
            raise TrendError("invalid_chain", "不支持的 DEX 链", 400)
        network = CHAIN_BY_ID[chain_id]["geckoId"]
        url = "https://api.geckoterminal.com/api/v2/networks/%s/pools/%s/trades" % (quote(network), quote(pool_address))
        fetched = self._request_json("geckoterminal", url, ttl=20, headers={"Accept": "application/json;version=20230203"})
        cutoff = time.time() - RANGE_SECONDS[range_key]
        trades = []
        body = fetched.data if isinstance(fetched.data, dict) else {}
        for row in body.get("data", []):
            attrs = row.get("attributes") or {}
            raw_time = attrs.get("block_timestamp") or attrs.get("timestamp")
            try:
                ts = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00")).timestamp() if isinstance(raw_time, str) else float(raw_time)
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                continue
            trades.append({
                "id": _clean_text(row.get("id"), 180),
                "timestamp": int(ts),
                "side": _clean_text(attrs.get("kind"), 16).lower() if str(attrs.get("kind", "")).lower() in {"buy", "sell"} else "unknown",
                "priceUsd": _number(attrs.get("price_to_in_usd")) or _number(attrs.get("price_from_in_usd")),
                "amount": _number(attrs.get("to_token_amount")) or _number(attrs.get("from_token_amount")),
                "valueUsd": _number(attrs.get("volume_in_usd")),
                "transactionHash": _clean_text(attrs.get("tx_hash") or attrs.get("transaction_hash"), 180),
                "sourceUrl": None,
            })
        trades.sort(key=lambda item: item["timestamp"], reverse=True)
        earliest = min((item["timestamp"] for item in trades), default=None)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "range": range_key,
            "trades": trades[:limit],
            "nextCursor": None,
            "meta": {
                "source": "GeckoTerminal",
                "provider": "geckoterminal",
                "retrievedAt": fetched.retrieved_at,
                "coverageStart": _iso_from_timestamp(earliest),
                "coverageEnd": fetched.retrieved_at,
                "nativeOrAggregated": "recent-300-trades",
                "metricStatus": "partial",
                "stale": fetched.stale,
                "partialErrors": [{"code": "sample_limited", "message": "公开接口最多返回最近 300 笔且仅覆盖过去 24 小时"}],
            },
        }

    # ----- helpers -----

    def _request_json(
        self,
        provider: str,
        url: str,
        ttl: int,
        headers: Optional[Dict[str, str]] = None,
        cache_key: Optional[str] = None,
    ) -> FetchResult:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise TrendError("blocked_host", "数据源地址不在白名单", 400, provider)
        key = provider + "|" + (cache_key or url)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return FetchResult(cached[1], cached[2], False)
            lock = self._key_locks.setdefault(key, threading.Lock())
        with lock:
            now = time.monotonic()
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached and cached[0] > now:
                    return FetchResult(cached[1], cached[2], False)
            try:
                self._take_rate_slot(provider)
            except TrendError:
                # An expired cache is still useful during a local request
                # budget or provider throttle. Return it as stale instead of
                # turning a temporary rate limit into a blank chart.
                if cached:
                    return FetchResult(cached[1], cached[2], True)
                raise
            request_headers = {
                "Accept": "application/json",
                "User-Agent": "Seegent/0.1 TrendMonitor (+local-desktop)",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
            request_headers.update(headers or {})
            request = Request(url, headers=request_headers, method="GET")
            try:
                response = self._open(request, timeout=8)
                with response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise TrendError("response_too_large", "数据源响应过大", 502, provider)
                data = json.loads(raw.decode("utf-8"))
                retrieved_at = _iso_now()
                with self._cache_lock:
                    self._cache[key] = (time.monotonic() + max(ttl, 1), data, retrieved_at)
                self._record_provider_success(provider, retrieved_at)
                return FetchResult(data, retrieved_at, False)
            except HTTPError as error:
                message = "数据源返回 HTTP %s" % error.code
                self._record_provider_error(provider, message)
                if cached:
                    return FetchResult(cached[1], cached[2], True)
                retry_after = error.headers.get("Retry-After") if error.headers else None
                retry_ms = int(float(retry_after) * 1000) if retry_after and str(retry_after).replace(".", "", 1).isdigit() else None
                raise TrendError("provider_http", message, error.code if 400 <= error.code < 500 else 502, provider, retry_ms)
            except (URLError, TimeoutError, OSError, ssl.SSLError, json.JSONDecodeError) as error:
                message = "连接数据源失败：" + _clean_text(getattr(error, "reason", error), 180)
                self._record_provider_error(provider, message)
                if cached:
                    return FetchResult(cached[1], cached[2], True)
                raise TrendError("provider_unavailable", message, 502, provider)

    @staticmethod
    def _open(request: Request, timeout: int):
        try:
            return urlopen(request, timeout=timeout, context=ssl.create_default_context())
        except URLError as error:
            reason = str(getattr(error, "reason", error))
            if "127.0.0.1" not in reason and "Connection refused" not in reason:
                raise
            opener = build_opener(ProxyHandler({}))
            return opener.open(request, timeout=timeout)

    def _take_rate_slot(self, provider: str) -> None:
        definition = PROVIDER_DEFS.get(provider, {"maxPerMinute": 10})
        limit = int(definition.get("maxPerMinute", 10))
        now = time.monotonic()
        with self._rate_lock:
            events = self._rate_events[provider]
            while events and now - events[0] >= 60:
                events.popleft()
            if len(events) >= limit:
                retry_seconds = max(1, int(60 - (now - events[0])))
                raise TrendError("rate_limited", "数据源请求预算已用完，请稍后重试", 429, provider, retry_seconds * 1000)
            events.append(now)

    def _record_provider_success(self, provider: str, when: str) -> None:
        self._status[provider] = {"status": "healthy", "lastSuccessAt": when, "lastErrorAt": self._status.get(provider, {}).get("lastErrorAt"), "lastError": None}

    def _record_provider_error(self, provider: str, message: str) -> None:
        previous = self._status.get(provider, {})
        self._status[provider] = {"status": "degraded", "lastSuccessAt": previous.get("lastSuccessAt"), "lastErrorAt": _iso_now(), "lastError": message}

    @staticmethod
    def _validate_range(range_key: str) -> str:
        if range_key not in RANGE_SECONDS:
            raise TrendError("invalid_range", "不支持的时间范围", 400)
        return range_key

    @staticmethod
    def _validate_chart_interval(interval_key: str) -> str:
        if interval_key not in CHART_INTERVAL_SECONDS:
            raise TrendError("invalid_interval", "不支持的 K 线周期", 400)
        return interval_key

    @staticmethod
    def _validate_chart_range(range_key: str) -> str:
        if range_key != CHART_RANGE_ALL and range_key not in CHART_RANGE_SECONDS:
            raise TrendError("invalid_chart_range", "不支持的 K 线覆盖范围", 400)
        return range_key

    @staticmethod
    def _result(asset: Dict[str, Any], market: Dict[str, Any], quote_data: Dict[str, Any], provider: str, source_label: str, metric_status: str, source_url: Optional[str] = None) -> Dict[str, Any]:
        if source_url:
            market["sourceUrl"] = source_url
        return {
            "asset": asset,
            "market": market,
            "quote": quote_data,
            "providers": [provider],
            "source": {"provider": provider, "label": source_label, "url": source_url},
            "metricStatus": metric_status,
        }

    @staticmethod
    def _meta(source: str, provider: str, fetched: FetchResult, aggregation: str, metric_status: str, errors: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return {
            "source": source,
            "provider": provider,
            "retrievedAt": fetched.retrieved_at,
            "coverageStart": None,
            "coverageEnd": fetched.retrieved_at,
            "nativeOrAggregated": aggregation,
            "metricStatus": metric_status,
            "stale": fetched.stale,
            "partialErrors": errors or [],
        }

    @staticmethod
    def _ohlcv_response(
        candles: List[Dict[str, Any]],
        range_key: str,
        interval: str,
        step_seconds: int,
        source: str,
        provider: str,
        fetched: FetchResult,
        aggregation: str,
        requested_interval: Optional[str] = None,
        requested_start: Optional[float] = None,
        partial_errors: Optional[List[Dict[str, Any]]] = None,
        next_cursor: Optional[str] = None,
        has_more: bool = False,
        provider_interval: Optional[str] = None,
    ) -> Dict[str, Any]:
        ordered = sorted(candles, key=lambda item: int(item.get("timestamp") or 0))
        coverage_start = _iso_from_timestamp(ordered[0]["timestamp"]) if ordered else None
        coverage_end = _iso_from_timestamp(_candle_end_timestamp(ordered[-1]["timestamp"], interval, step_seconds)) if ordered else None
        errors = list(partial_errors or [])
        if requested_interval and ordered and requested_start is not None:
            actual_start = float(ordered[0].get("timestamp") or 0)
            # Allow alignment to the provider's candle boundary, but surface
            # genuinely shorter history instead of implying full coverage.
            if actual_start > requested_start + max(step_seconds * 2, 300):
                errors.append({
                    "code": "coverage_shortfall",
                    "message": "公开数据源实际覆盖时间短于所选范围，未补齐缺失 K 线",
                })
        if not ordered:
            errors.append({"code": "empty", "message": "该范围没有 K 线数据"})
        metric_status = "partial" if fetched.stale or errors or has_more else "exact"
        response: Dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "requestedRange": range_key,
            "effectiveInterval": interval,
            "timezone": "UTC",
            "candles": ordered[-MAX_CHART_CANDLES:],
            "nextCursor": next_cursor,
            "hasMore": bool(has_more and next_cursor),
            "historyComplete": not bool(has_more and next_cursor),
            "pageSize": MAX_CHART_CANDLES,
            "meta": {
                "source": source,
                "provider": provider,
                "retrievedAt": fetched.retrieved_at,
                "coverageStart": coverage_start,
                "coverageEnd": coverage_end,
                "requestedCoverageStart": _iso_from_timestamp(requested_start),
                "requestedCoverageEnd": fetched.retrieved_at,
                "nativeOrAggregated": aggregation,
                "metricStatus": metric_status,
                "stale": fetched.stale,
                "partialErrors": errors,
            },
        }
        if requested_interval:
            response["requestedInterval"] = requested_interval
        if provider_interval:
            response["providerInterval"] = provider_interval
        return response

    @staticmethod
    def _unavailable_metric(metric: str, message: str, market_id: str, range_key: str, interval_key: Optional[str] = None) -> Dict[str, Any]:
        key = "candles" if metric == "ohlcv" else "metrics"
        value: Any = [] if key == "candles" else {
            "tradeVolumeUsd": None, "buyVolumeUsd": None, "sellVolumeUsd": None,
            "buyCount": None, "sellCount": None, "transferAmountToken": None,
            "transferValueUsd": None, "transferCount": None, "uniqueWallets": None,
        }
        response = {
            "schemaVersion": SCHEMA_VERSION,
            "requestedRange": range_key,
            "marketId": market_id,
            key: value,
            "meta": {
                "source": None,
                "provider": None,
                "retrievedAt": _iso_now(),
                "coverageStart": None,
                "coverageEnd": None,
                "nativeOrAggregated": "unavailable",
                "metricStatus": "unavailable",
                "partialErrors": [{"code": "unavailable", "message": message}],
            },
        }
        if interval_key:
            response["requestedInterval"] = interval_key
        return response

    @staticmethod
    def _risk_unavailable(message: str, provider: Optional[str] = None) -> Dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "risk": {"level": "unknown", "label": "暂不可用", "flags": [], "disclaimer": "第三方风险检测结果不构成安全认证或投资建议。"},
            "meta": {
                "source": "GoPlus" if provider == "goplus" else None,
                "provider": provider,
                "retrievedAt": _iso_now(),
                "coverageStart": None,
                "coverageEnd": None,
                "nativeOrAggregated": "unavailable",
                "metricStatus": "unavailable",
                "partialErrors": [{"code": "unavailable", "message": message}],
            },
        }

    def _load_watchlist(self) -> Dict[str, Any]:
        try:
            with open(self.watchlist_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
                raise ValueError("invalid watchlist")
            return data
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            return {"schemaVersion": SCHEMA_VERSION, "updatedAt": None, "items": []}

    def _write_watchlist(self, data: Dict[str, Any]) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.data_dir, prefix=".seegent-trend-", suffix=".tmp", delete=False) as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = handle.name
            os.replace(temp_path, self.watchlist_file)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
