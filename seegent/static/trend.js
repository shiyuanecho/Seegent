import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  createChart,
} from './lightweight-charts.production.mjs';

const root = document.getElementById('trend-module');

const RANGE_OPTIONS = ['1m', '5m', '15m', '30m', '1h', '4h', '12h', '1d', '7d', '30d'];
const CHART_INTERVAL_OPTIONS = ['1m', '5m', '15m', '30m', '1h', '4h', '12h', '1d', '7d', '1M'];
const CHART_HISTORY_RANGE = 'all';
// A chart must become usable after one provider page. Older history is loaded
// one page per explicit click so a long BTC 1m history or a rate-limited DEX
// cannot keep the detail page in a prolonged serial request loop.
const CHART_HISTORY_AUTO_PAGES = 1;
const CHART_HISTORY_MAX_PAGES = 1;
const CHART_MODEL_VERSION_KEY = 'seegent-trend-chart-model';
const TREND_PAGES = ['overview', 'watchlist', 'detail'];
const WATCH_PAGE_SIZE = 12;
const RESULT_FILTER_STORAGE_KEY = 'seegent-trend-result-filters';
const WATCH_SORT_STORAGE_KEY = 'seegent-trend-watch-sort';
const RESULT_SORT_OPTIONS = ['relevance', 'volume24hUsd', 'marketCapUsd', 'liquidityUsd', 'createdAt'];
const WATCH_SORT_OPTIONS = ['addedAt', 'marketCapUsd', 'change1h', 'change6h', 'change24h', 'volume24hUsd', 'liquidityUsd'];
const RESULT_VOLUME_MIN_OPTIONS = ['0', '1000', '10000', '100000', '1000000'];
const RESULT_MARKET_CAP_MIN_OPTIONS = ['0', '100000', '1000000', '10000000', '100000000'];
const RESULT_AGE_OPTIONS = ['all', '1h', '24h', '7d', '30d'];
const RESULT_AGE_MAX_MS = {
  '1h': 60 * 60 * 1000,
  '24h': 24 * 60 * 60 * 1000,
  '7d': 7 * 24 * 60 * 60 * 1000,
  '30d': 30 * 24 * 60 * 60 * 1000,
};
const DEFAULT_RESULT_FILTERS = Object.freeze({
  sortBy: 'relevance',
  sortDir: 'desc',
  volumeMin: '0',
  marketCapMin: '0',
  ageMax: 'all',
});
const DEFAULT_WATCH_SORT = Object.freeze({
  sortBy: 'addedAt',
  sortDir: 'desc',
});
const WATCH_SORT_LABELS = {
  addedAt: '最近收藏',
  marketCapUsd: '市值',
  change1h: '1h 涨跌幅',
  change6h: '6h 涨跌幅',
  change24h: '24h 涨跌幅',
  volume24hUsd: '24h 成交额',
  liquidityUsd: '流动性',
};
const RANGE_LABELS = {
  '1m': '1分', '5m': '5分', '15m': '15分', '30m': '30分',
  '1h': '1小时', '4h': '4小时', '12h': '12小时',
  '1d': '1天', '7d': '7天', '30d': '30天',
};
const CHART_INTERVAL_LABELS = {
  '1m': '1分', '5m': '5分', '15m': '15分', '30m': '30分',
  '1h': '1小时', '4h': '4小时', '12h': '12小时',
  '1d': '1天', '7d': '7天', '1M': '1月',
};
const CHART_INTERVAL_SECONDS = {
  '1m': 60, '5m': 5 * 60, '15m': 15 * 60, '30m': 30 * 60,
  '1h': 60 * 60, '4h': 4 * 60 * 60, '12h': 12 * 60 * 60,
  '1d': 24 * 60 * 60, '7d': 7 * 24 * 60 * 60, '1M': 30 * 24 * 60 * 60,
};
const LEGACY_CHART_RANGE = localStorage.getItem('seegent-trend-chart-range');
const CHART_MODEL_VERSION = localStorage.getItem(CHART_MODEL_VERSION_KEY);
const STATUS_LABELS = {
  exact: '完整', estimated: '估算', partial: '部分', unavailable: '不可用',
};
const CHAIN_FALLBACK = [
  { id: 'robinhood', name: 'Robinhood Chain', shortName: 'Robinhood', nativeSymbol: 'ETH', explorer: 'https://robinhoodchain.blockscout.com' },
  { id: 'ethereum', name: 'Ethereum', shortName: 'ETH', nativeSymbol: 'ETH', explorer: 'https://etherscan.io' },
  { id: 'bsc', name: 'BNB Smart Chain', shortName: 'BSC', nativeSymbol: 'BNB', explorer: 'https://bscscan.com' },
  { id: 'base', name: 'Base', shortName: 'Base', nativeSymbol: 'ETH', explorer: 'https://basescan.org' },
  { id: 'solana', name: 'Solana', shortName: 'Solana', nativeSymbol: 'SOL', explorer: 'https://solscan.io' },
  { id: 'ton', name: 'TON / Gram', shortName: 'TON', nativeSymbol: 'TON', explorer: 'https://tonscan.org' },
  { id: 'morph', name: 'Morph', shortName: 'Morph', nativeSymbol: 'BGB', explorer: 'https://explorer.morphl2.io' },
];

function normalizeResultFilters(value = {}) {
  const sortBy = RESULT_SORT_OPTIONS.includes(value.sortBy) ? value.sortBy : DEFAULT_RESULT_FILTERS.sortBy;
  const sortDir = value.sortDir === 'asc' ? 'asc' : DEFAULT_RESULT_FILTERS.sortDir;
  const volumeMin = RESULT_VOLUME_MIN_OPTIONS.includes(String(value.volumeMin))
    ? String(value.volumeMin) : DEFAULT_RESULT_FILTERS.volumeMin;
  const marketCapMin = RESULT_MARKET_CAP_MIN_OPTIONS.includes(String(value.marketCapMin))
    ? String(value.marketCapMin) : DEFAULT_RESULT_FILTERS.marketCapMin;
  const ageMax = RESULT_AGE_OPTIONS.includes(value.ageMax) ? value.ageMax : DEFAULT_RESULT_FILTERS.ageMax;
  return { sortBy, sortDir, volumeMin, marketCapMin, ageMax };
}

function loadResultFilters() {
  try {
    const saved = JSON.parse(localStorage.getItem(RESULT_FILTER_STORAGE_KEY) || '{}');
    return normalizeResultFilters(saved);
  } catch (_) {
    return { ...DEFAULT_RESULT_FILTERS };
  }
}

function normalizeWatchSort(value = {}) {
  return {
    sortBy: WATCH_SORT_OPTIONS.includes(value.sortBy) ? value.sortBy : DEFAULT_WATCH_SORT.sortBy,
    sortDir: value.sortDir === 'asc' ? 'asc' : DEFAULT_WATCH_SORT.sortDir,
  };
}

function loadWatchSort() {
  try {
    const saved = JSON.parse(localStorage.getItem(WATCH_SORT_STORAGE_KEY) || '{}');
    return normalizeWatchSort(saved);
  } catch (_) {
    return { ...DEFAULT_WATCH_SORT };
  }
}

const state = {
  initialized: false,
  active: false,
  capabilities: null,
  overview: null,
  watchlist: [],
  page: TREND_PAGES.includes(localStorage.getItem('seegent-trend-page'))
    ? localStorage.getItem('seegent-trend-page') : 'overview',
  watchPage: 0,
  watchRefreshAt: null,
  detailOrigin: 'overview',
  results: [],
  searchMeta: null,
  resultFilters: loadResultFilters(),
  watchSort: loadWatchSort(),
  selection: null,
  detail: null,
  ohlcv: null,
  flows: null,
  trades: null,
  risk: null,
  range: RANGE_OPTIONS.includes(localStorage.getItem('seegent-trend-range'))
    ? localStorage.getItem('seegent-trend-range') : '1d',
  chartInterval: CHART_MODEL_VERSION === '2' && !LEGACY_CHART_RANGE && CHART_INTERVAL_OPTIONS.includes(localStorage.getItem('seegent-trend-chart-interval'))
    ? localStorage.getItem('seegent-trend-chart-interval') : '1d',
  chartRange: CHART_HISTORY_RANGE,
  chainId: localStorage.getItem('seegent-trend-chain') || '',
  selectionVersion: 0,
  timers: new Set(),
  controllers: new Set(),
  chart: null,
  candleSeries: null,
  volumeSeries: null,
  chartResizeObserver: null,
  chartHistoryLoading: false,
  chartRequestKey: null,
  chartRequestGeneration: 0,
  themeObserver: null,
  loading: new Set(),
};

class TrendApiError extends Error {
  constructor(message, code = 'request_failed', status = 0, retryAfterMs = null) {
    super(message);
    this.name = 'TrendApiError';
    this.code = code;
    this.status = status;
    this.retryAfterMs = retryAfterMs;
  }
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function safeHttpsUrl(value) {
  try {
    const url = new URL(String(value || ''));
    return url.protocol === 'https:' ? url.href : '';
  } catch (_) {
    return '';
  }
}

function tokenLogoHtml(url, style = '') {
  const safeUrl = safeHttpsUrl(url);
  if (!safeUrl) return '';
  const styleAttribute = style ? ` style="${escapeHtml(style)}"` : '';
  return `<img class="trend-token-avatar" data-token-logo src="${escapeHtml(safeUrl)}" alt="" loading="lazy" decoding="async"${styleAttribute}>`;
}

function handleTokenLogoError(event) {
  const image = event.target;
  if (!image || image.tagName !== 'IMG' || !image.matches('[data-token-logo]')) return;
  image.closest('.trend-result-open, .trend-favorite-top, .trend-asset-hero')?.classList.remove('has-token-logo');
  image.remove();
}

function buildUrl(path, params = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, String(value));
  });
  return url.pathname + url.search;
}

async function api(path, { params, method = 'GET', body, timeoutMs = 12000 } = {}) {
  const controller = new AbortController();
  state.controllers.add(controller);
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(buildUrl(path, params), {
      method,
      cache: 'no-store',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* structured fallback below */ }
    if (!response.ok || payload.error) {
      const error = payload.error || {};
      throw new TrendApiError(
        error.message || `请求失败（${response.status}）`,
        error.code || 'request_failed',
        response.status,
        error.retryAfterMs,
      );
    }
    return payload;
  } catch (error) {
    if (error.name === 'AbortError') throw new TrendApiError('请求已取消或超时', 'timeout', 0);
    if (error instanceof TrendApiError) throw error;
    throw new TrendApiError('无法连接本地趋势服务', 'network_error', 0);
  } finally {
    window.clearTimeout(timeout);
    state.controllers.delete(controller);
  }
}

function cancelChartRequest() {
  state.chartRequestGeneration += 1;
  state.chartRequestKey = null;
  state.chartHistoryLoading = false;
  document.getElementById('trend-chart-tooltip')?.style.setProperty('display', 'none');
}

function abortRequests() {
  state.controllers.forEach(controller => controller.abort());
  state.controllers.clear();
  cancelChartRequest();
}

function formatNumber(value, options = {}) {
  if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '暂不可用';
  const number = Number(value);
  const absolute = Math.abs(number);
  const maximumFractionDigits = options.maximumFractionDigits ?? (absolute >= 100 ? 2 : absolute >= 1 ? 4 : 8);
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits }).format(number);
}

function formatCompact(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '暂不可用';
  return new Intl.NumberFormat('zh-CN', {
    notation: Math.abs(Number(value)) >= 10000 ? 'compact' : 'standard',
    maximumFractionDigits: 2,
  }).format(Number(value));
}

function formatUsd(value, compact = false) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '暂不可用';
  const number = Number(value);
  if (compact && Math.abs(number) >= 10000) return '$' + formatCompact(number);
  const digits = Math.abs(number) >= 1000 ? 2 : Math.abs(number) >= 1 ? 4 : 8;
  return '$' + new Intl.NumberFormat('en-US', { maximumFractionDigits: digits }).format(number);
}

function formatPercent(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '暂不可用';
  const number = Number(value);
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`;
}

function shortAddress(value) {
  const text = String(value || '');
  if (!text) return '无合约（原生资产）';
  return text.length > 17 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
}

function formatDateTime(value, includeYear = false) {
  if (!value) return '暂不可用';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return '暂不可用';
  return new Intl.DateTimeFormat('zh-CN', {
    year: includeYear ? 'numeric' : undefined,
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
}

function formatChartTime(value) {
  if (!value) return '暂不可用';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return '暂不可用';
  const intraday = (CHART_INTERVAL_SECONDS[state.chartInterval] || 0) < 24 * 60 * 60;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: intraday ? '2-digit' : undefined,
    minute: intraday ? '2-digit' : undefined,
    hour12: false,
  }).format(date);
}

function chartTickLabel(value) {
  return formatChartTime(Number(value));
}

function formatAge(value) {
  if (!value) return '未更新';
  const milliseconds = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(milliseconds)) return '时间未知';
  if (milliseconds < 0 || milliseconds < 5000) return '刚刚';
  if (milliseconds < 60000) return `${Math.floor(milliseconds / 1000)} 秒前`;
  if (milliseconds < 3600000) return `${Math.floor(milliseconds / 60000)} 分钟前`;
  if (milliseconds < 86400000) return `${Math.floor(milliseconds / 3600000)} 小时前`;
  return `${Math.floor(milliseconds / 86400000)} 天前`;
}

function parseTimestampMs(value) {
  if (value === null || value === undefined || value === '') return null;
  let timestamp;
  if (typeof value === 'number' || /^\d+(?:\.\d+)?$/.test(String(value).trim())) {
    timestamp = Number(value);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return null;
    if (timestamp < 100000000000) timestamp *= 1000;
  } else {
    timestamp = Date.parse(String(value));
  }
  return Number.isFinite(timestamp) && !Number.isNaN(new Date(timestamp).getTime()) ? timestamp : null;
}

function formatTimestamp(value) {
  const timestamp = parseTimestampMs(value);
  if (timestamp === null) return '暂不可用';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(timestamp));
}

function formatMarketCreated(value) {
  const timestamp = parseTimestampMs(value);
  if (timestamp === null) return '市场创建 暂不可用';
  const elapsed = Date.now() - timestamp;
  if (elapsed < 0) return `市场创建 ${formatTimestamp(timestamp)}`;
  if (elapsed < 60000) return '市场创建 刚刚';
  if (elapsed < 3600000) return `市场创建 ${Math.floor(elapsed / 60000)} 分钟前`;
  if (elapsed < 86400000) return `市场创建 ${Math.floor(elapsed / 3600000)} 小时前`;
  if (elapsed < 30 * 86400000) return `市场创建 ${Math.floor(elapsed / 86400000)} 天前`;
  return `市场创建 ${formatTimestamp(timestamp).split(' ')[0]}`;
}

function getChains() {
  return state.capabilities?.chains?.length ? state.capabilities.chains : CHAIN_FALLBACK;
}

function chainById(chainId) {
  return getChains().find(chain => chain.id === chainId) || null;
}

function chainLabel(chainId) {
  if (chainId === 'bitcoin') return 'Bitcoin';
  if (chainId === 'multi') return '多链 / CEX';
  if (chainId === 'unknown') return '链归属未知';
  return chainById(chainId)?.name || chainId || '未指定';
}

function providerLabel(provider) {
  return state.capabilities?.providers?.find(item => item.id === provider)?.label || provider || '未知来源';
}

function statusBadge(status, stale = false) {
  const normalized = STATUS_LABELS[status] ? status : 'unavailable';
  const staleBadge = stale ? '<span class="trend-badge stale">陈旧缓存</span>' : '';
  return `<span class="trend-badge ${normalized}">${escapeHtml(STATUS_LABELS[normalized])}</span>${staleBadge}`;
}

function metaHtml(meta, sourceUrl = '', options = {}) {
  if (!meta) return '<span>来源与覆盖信息暂不可用</span>';
  const source = escapeHtml(meta.source || providerLabel(meta.provider));
  const url = safeHttpsUrl(sourceUrl);
  const sourcePart = url
    ? `<a class="trend-source-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${source}</a>`
    : `<span>${source}</span>`;
  const coverage = meta.coverageStart || meta.coverageEnd
    ? `覆盖 ${escapeHtml(formatDateTime(meta.coverageStart, options.includeYear))} → ${escapeHtml(formatDateTime(meta.coverageEnd, options.includeYear))}`
    : '覆盖范围未提供';
  return `${sourcePart}${statusBadge(meta.metricStatus, meta.stale)}<span>更新 ${escapeHtml(formatAge(meta.retrievedAt))}</span><span>${coverage}</span>`;
}

function partialErrors(meta) {
  return (meta?.partialErrors || []).map(error => error?.message).filter(Boolean);
}

function errorText(error) {
  if (!error) return '未知错误';
  if (error.code === 'rate_limited') {
    const seconds = error.retryAfterMs ? Math.ceil(error.retryAfterMs / 1000) : null;
    return seconds ? `数据源已限流，约 ${seconds} 秒后重试` : '数据源已限流，请稍后重试';
  }
  return error.message || '请求失败';
}

function renderShell() {
  root.innerHTML = `
    <div class="trend-shell">
      <div class="trend-inner" id="trend-inner">
        <div class="trend-topbar">
          <div>
            <div class="trend-title-row">
              <span class="trend-title-mark" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 17l5-5 4 3 8-9"/><path d="M15 6h5v5"/></svg>
              </span>
              <h1>趋势</h1>
            </div>
            <p class="trend-subtitle">多链市场与链上活动。指标始终保留它的数据源和覆盖范围。</p>
          </div>
          <div class="trend-runtime" id="trend-runtime" data-state="unknown" title="Provider 运行时状态">
            <span class="trend-runtime-dot"></span>
            <span id="trend-runtime-text">正在读取数据源状态</span>
          </div>
        </div>

        <nav class="trend-subtabs" aria-label="趋势页面">
          <button class="trend-subtab" type="button" data-action="switch-page" data-page="overview">市场总览</button>
          <button class="trend-subtab" type="button" data-action="switch-page" data-page="watchlist">我的收藏 <span class="trend-tab-count" id="trend-watch-tab-count">0</span></button>
          <button class="trend-subtab" id="trend-detail-tab" type="button" data-action="switch-page" data-page="detail" disabled>代币详情</button>
        </nav>

        <section class="trend-page" data-trend-page="overview" aria-label="市场总览">
        <div class="trend-search-panel">
          <form class="trend-search-form" id="trend-search-form">
            <div class="trend-search-input-wrap">
              <svg class="trend-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>
              <input class="trend-search-input" id="trend-search-input" autocomplete="off" spellcheck="false"
                     placeholder="搜索 BTC、比特币、Solana 或合约地址" aria-label="搜索币名、Symbol 或合约地址">
              <button class="trend-search-clear" id="trend-search-clear" type="button" aria-label="清空搜索">×</button>
            </div>
            <select class="trend-chain-select" id="trend-chain-select" aria-label="按链筛选"></select>
            <button class="trend-search-submit" id="trend-search-submit" type="submit">搜索</button>
          </form>
          <div class="trend-quick-search">
            <span class="trend-quick-label">快速查看</span>
            ${['BTC', '比特币', 'ETH', 'SOL', 'TSLA', 'Gram', 'BGB'].map(query => `<button class="trend-quick-btn" type="button" data-action="quick-search" data-query="${escapeHtml(query)}">${escapeHtml(query)}</button>`).join('')}
          </div>
          <div class="trend-search-state" id="trend-search-state" aria-live="polite">
            <div class="trend-search-summary" id="trend-search-summary"></div>
            <div class="trend-result-tools" id="trend-result-tools" aria-label="搜索结果筛选与排序">
              <span class="trend-result-tools-title">筛选</span>
              <label class="trend-filter-control">
                <span>24h 成交额</span>
                <select id="trend-filter-volume" aria-label="最低 24 小时成交额">
                  <option value="0">不限</option>
                  <option value="1000">≥ $1千</option>
                  <option value="10000">≥ $1万</option>
                  <option value="100000">≥ $10万</option>
                  <option value="1000000">≥ $100万</option>
                </select>
              </label>
              <label class="trend-filter-control">
                <span>市值</span>
                <select id="trend-filter-marketcap" aria-label="最低市值">
                  <option value="0">不限</option>
                  <option value="100000">≥ $10万</option>
                  <option value="1000000">≥ $100万</option>
                  <option value="10000000">≥ $1000万</option>
                  <option value="100000000">≥ $1亿</option>
                </select>
              </label>
              <label class="trend-filter-control">
                <span>市场创建</span>
                <select id="trend-filter-age" aria-label="市场创建时间范围" title="公开源提供的是交易池或市场创建时间，不代表 Token 合约部署时间">
                  <option value="all">不限</option>
                  <option value="1h">1 小时内</option>
                  <option value="24h">24 小时内</option>
                  <option value="7d">7 天内</option>
                  <option value="30d">30 天内</option>
                </select>
              </label>
              <span class="trend-result-tools-divider" aria-hidden="true"></span>
              <label class="trend-filter-control trend-sort-control">
                <span>排序</span>
                <select id="trend-sort-by" aria-label="搜索结果排序字段">
                  <option value="relevance">综合相关性</option>
                  <option value="volume24hUsd">24h 成交额</option>
                  <option value="marketCapUsd">市值</option>
                  <option value="liquidityUsd">流动性</option>
                  <option value="createdAt">市场创建时间</option>
                </select>
              </label>
              <button class="trend-sort-direction" id="trend-sort-direction" type="button" data-action="toggle-sort-dir">平台相关性</button>
              <button class="trend-filter-reset" type="button" data-action="reset-result-filters">重置</button>
            </div>
            <div class="trend-result-list" id="trend-result-list"></div>
          </div>
        </div>

        <section class="trend-section" aria-labelledby="trend-overview-heading">
          <div class="trend-section-head">
            <h2 id="trend-overview-heading">多链概览</h2>
            <div class="trend-section-note" id="trend-overview-note">口径：DefiLlama 已跟踪 DEX 协议成交量，不代表整条链的全部活动</div>
          </div>
          <div class="trend-chain-grid" id="trend-chain-grid">${chainSkeletons()}</div>
        </section>
        </section>

        <section class="trend-page" data-trend-page="watchlist" aria-labelledby="trend-watch-heading">
          <section class="trend-panel trend-watch-page-panel">
            <div class="trend-watch-page-head">
              <div>
                <div class="trend-watch-heading-row"><h2 id="trend-watch-heading">我的收藏</h2><span class="trend-badge" id="trend-watch-count">0</span></div>
                <p>集中查看你保存的代币与具体市场。行情只在打开本页时刷新。</p>
              </div>
              <div class="trend-watch-actions">
                <label class="trend-watch-sort-control">
                  <span>排序</span>
                  <select id="trend-watch-sort-by" aria-label="收藏排序字段">
                    ${WATCH_SORT_OPTIONS.map(sortBy => `<option value="${sortBy}">${escapeHtml(WATCH_SORT_LABELS[sortBy])}</option>`).join('')}
                  </select>
                </label>
                <button class="trend-sort-direction trend-watch-sort-direction" id="trend-watch-sort-direction" type="button" data-action="toggle-watch-sort-dir">↓ 最近收藏</button>
                <button class="trend-watch-refresh" type="button" data-action="refresh-watch">刷新行情</button>
              </div>
            </div>
            <div class="trend-watch-summary" id="trend-watch-summary">收藏保存在本机</div>
            <div class="trend-watch-grid" id="trend-watch-list"></div>
            <div class="trend-watch-pagination" id="trend-watch-pagination"></div>
          </section>
        </section>

        <section class="trend-page" data-trend-page="detail" aria-label="代币详情">
          <div class="trend-detail-page-head">
            <button class="trend-back-button" type="button" data-action="back-from-detail">← <span id="trend-detail-back-label">返回市场总览</span></button>
          </div>

        <div class="trend-workspace" id="trend-workspace">
          <main class="trend-main-column">
            <section class="trend-panel" id="trend-asset-panel">
              <div class="trend-asset-hero" id="trend-asset-hero"></div>
              <div class="trend-quote-grid" id="trend-quote-grid"></div>
              <div class="trend-meta-line" id="trend-asset-meta" style="padding:0 12px 12px"></div>
            </section>

            <section class="trend-panel">
              <div class="trend-panel-head trend-chart-panel-head">
                <div>
                  <h3>价格与成交量</h3>
                  <div class="trend-meta-line" id="trend-chart-meta" style="margin-top:4px"></div>
                </div>
                <div class="trend-chart-toolbar" id="trend-chart-toolbar">
                  <div class="trend-chart-control-group">
                    <span class="trend-chart-control-label">周期（每根）</span>
                    <div class="trend-chart-control-buttons">
                      ${CHART_INTERVAL_OPTIONS.map(interval => `<button class="trend-range-btn trend-chart-interval-btn${interval === state.chartInterval ? ' active' : ''}" type="button" data-action="chart-interval" data-interval="${interval}" title="每根 K 线 ${escapeHtml(CHART_INTERVAL_LABELS[interval])}">${interval}</button>`).join('')}
                    </div>
                  </div>
                  <span class="trend-chart-history-note" id="trend-chart-history-status">最新一页 · 可拖动缩放</span>
                </div>
              </div>
              <div class="trend-chart-wrap" id="trend-chart-wrap">
                <div class="trend-chart" id="trend-chart"></div>
                <div class="trend-chart-message visible" id="trend-chart-message">选择具体市场后加载 K 线</div>
                <div class="trend-chart-tooltip" id="trend-chart-tooltip"></div>
                <a class="trend-chart-attribution" href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">Charts by TradingView</a>
              </div>
            </section>

            <section class="trend-panel">
              <div class="trend-panel-head">
                <div>
                  <h3>资金与交易活动 · <span id="trend-flow-range">${escapeHtml(RANGE_LABELS[state.range])}</span></h3>
                  <div class="trend-meta-line" id="trend-flow-meta" style="margin-top:4px"></div>
                </div>
              </div>
              <div class="trend-flow-grid" id="trend-flow-grid"></div>
              <div id="trend-flow-warning"></div>
            </section>

            <section class="trend-panel">
              <div class="trend-panel-head">
                <div>
                  <h3>最近成交明细</h3>
                  <div class="trend-meta-line" id="trend-trades-meta" style="margin-top:4px"></div>
                </div>
              </div>
              <div class="trend-table-wrap" id="trend-trades-wrap"></div>
            </section>
          </main>

          <aside class="trend-side-column">
            <section class="trend-panel">
              <div class="trend-panel-head"><h3>Token 风险信号</h3><span class="trend-badge">GoPlus</span></div>
              <div class="trend-risk-card" id="trend-risk-card"></div>
            </section>
            <section class="trend-panel">
              <div class="trend-panel-head"><h3>数据源状态</h3><span class="trend-badge">运行时</span></div>
              <div class="trend-provider-list" id="trend-provider-list"></div>
            </section>
          </aside>
        </div>
        </section>
      </div>
    </div>`;

  renderChainSelect();
  renderWatchlist();
  renderRisk(null);
  bindEvents();
  syncResultFilterControls();
  renderTrendPage();
}

function chainSkeletons() {
  return CHAIN_FALLBACK.map(chain => `
    <button class="trend-chain-card trend-skeleton" type="button" disabled>
      <div class="trend-chain-top"><span class="trend-chain-name">${escapeHtml(chain.name)}</span></div>
      <div class="trend-chain-volume">—</div>
      <div class="trend-chain-caption">正在读取公开数据源</div>
    </button>`).join('');
}

function renderChainSelect() {
  const select = document.getElementById('trend-chain-select');
  if (!select) return;
  if (state.chainId && !getChains().some(chain => chain.id === state.chainId)) state.chainId = '';
  select.innerHTML = '<option value="">全部链 + CEX</option>' + getChains().map(chain =>
    `<option value="${escapeHtml(chain.id)}"${state.chainId === chain.id ? ' selected' : ''}>${escapeHtml(chain.name)}</option>`
  ).join('');
}

function bindEvents() {
  document.getElementById('trend-search-form')?.addEventListener('submit', event => {
    event.preventDefault();
    runSearch();
  });
  document.getElementById('trend-search-input')?.addEventListener('input', event => {
    document.getElementById('trend-search-clear')?.classList.toggle('visible', Boolean(event.target.value));
  });
  document.getElementById('trend-search-clear')?.addEventListener('click', () => {
    const input = document.getElementById('trend-search-input');
    input.value = '';
    input.focus();
    document.getElementById('trend-search-clear')?.classList.remove('visible');
  });
  document.getElementById('trend-chain-select')?.addEventListener('change', event => {
    setChain(event.target.value);
  });
  document.getElementById('trend-filter-volume')?.addEventListener('change', event => {
    updateResultFilters({ volumeMin: event.target.value });
  });
  document.getElementById('trend-filter-marketcap')?.addEventListener('change', event => {
    updateResultFilters({ marketCapMin: event.target.value });
  });
  document.getElementById('trend-filter-age')?.addEventListener('change', event => {
    updateResultFilters({ ageMax: event.target.value });
  });
  document.getElementById('trend-sort-by')?.addEventListener('change', event => {
    updateResultFilters({ sortBy: event.target.value });
  });
  document.getElementById('trend-watch-sort-by')?.addEventListener('change', event => {
    updateWatchSort({ sortBy: event.target.value });
  });
  root.addEventListener('click', handleRootClick);
  root.addEventListener('error', handleTokenLogoError, true);
  document.addEventListener('visibilitychange', handleVisibility);

  state.themeObserver = new MutationObserver(() => applyChartTheme());
  state.themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
}

function handleRootClick(event) {
  const target = event.target.closest('[data-action]');
  if (!target || !root.contains(target)) return;
  const action = target.dataset.action;
  if (action === 'quick-search') {
    const input = document.getElementById('trend-search-input');
    input.value = target.dataset.query || '';
    document.getElementById('trend-search-clear')?.classList.add('visible');
    runSearch();
  } else if (action === 'select-result') {
    selectResult(Number(target.dataset.index));
  } else if (action === 'toggle-result-watch') {
    toggleResultWatch(Number(target.dataset.index));
  } else if (action === 'select-chain') {
    setChain(target.dataset.chain || '');
    document.getElementById('trend-search-input')?.focus();
  } else if (action === 'chart-interval') {
    setChartInterval(target.dataset.interval);
  } else if (action === 'load-chart-history') {
    loadOhlcv(state.selectionVersion, false, true);
  } else if (action === 'toggle-watch') {
    toggleCurrentWatch();
  } else if (action === 'open-watch') {
    openWatch(Number(target.dataset.index));
  } else if (action === 'remove-watch') {
    removeWatch(Number(target.dataset.index));
  } else if (action === 'switch-page') {
    setTrendPage(target.dataset.page || 'overview');
  } else if (action === 'back-from-detail') {
    setTrendPage(state.detailOrigin === 'watchlist' ? 'watchlist' : 'overview');
  } else if (action === 'refresh-watch') {
    refreshWatchlistQuotes(true);
  } else if (action === 'watch-page') {
    setWatchPage(Number(target.dataset.pageIndex));
  } else if (action === 'toggle-sort-dir') {
    if (state.resultFilters.sortBy !== 'relevance') {
      updateResultFilters({ sortDir: state.resultFilters.sortDir === 'desc' ? 'asc' : 'desc' });
    }
  } else if (action === 'toggle-watch-sort-dir') {
    updateWatchSort({ sortDir: state.watchSort.sortDir === 'desc' ? 'asc' : 'desc' });
  } else if (action === 'reset-result-filters') {
    resetResultFilters();
  }
}

function setChain(chainId) {
  state.chainId = getChains().some(chain => chain.id === chainId) ? chainId : '';
  localStorage.setItem('seegent-trend-chain', state.chainId);
  renderChainSelect();
  renderOverview();
}

function normalizeChartSelection() {
  if (!CHART_INTERVAL_OPTIONS.includes(state.chartInterval)) state.chartInterval = '1d';
  // The previous UI persisted a separate range (for example 1y) and could
  // silently coarsen a 5m request into 1d. Migrate that state once to the new
  // single-period, all-history chart model.
  state.chartRange = CHART_HISTORY_RANGE;
  localStorage.setItem('seegent-trend-chart-interval', state.chartInterval);
  localStorage.setItem(CHART_MODEL_VERSION_KEY, '2');
  localStorage.removeItem('seegent-trend-chart-range');
}

function updateChartControls() {
  root.querySelectorAll('.trend-chart-interval-btn').forEach(button => button.classList.toggle('active', button.dataset.interval === state.chartInterval));
}

function setChartInterval(interval) {
  if (!CHART_INTERVAL_OPTIONS.includes(interval)) return;
  const changed = state.chartInterval !== interval;
  state.chartInterval = interval;
  state.chartRange = CHART_HISTORY_RANGE;
  localStorage.setItem('seegent-trend-chart-interval', state.chartInterval);
  localStorage.removeItem('seegent-trend-chart-range');
  updateChartControls();
  if (!changed || !state.selection) return;
  cancelChartRequest();
  state.ohlcv = null;
  state.candleSeries?.setData([]);
  state.volumeSeries?.setData([]);
  resetChartLoadingState();
  loadOhlcv(state.selectionVersion, true);
}

async function loadCapabilities(silent = false) {
  if (!state.active && !silent) return;
  try {
    state.capabilities = await api('/api/trend/capabilities');
    renderChainSelect();
    renderProviders();
    updateRuntimeStatus();
  } catch (error) {
    if (!silent) setRuntime('degraded', errorText(error));
  }
}

async function loadOverview() {
  if (!state.active || state.loading.has('overview')) return;
  state.loading.add('overview');
  try {
    const data = await api('/api/trend/overview', { timeoutMs: 15000 });
    if (!state.active) return;
    state.overview = data;
    renderOverview();
  } catch (error) {
    if (!state.active) return;
    if (!state.overview) renderOverviewError(error);
    setRuntime('degraded', errorText(error));
  } finally {
    state.loading.delete('overview');
    if (state.active) loadCapabilities(true);
  }
}

function renderOverview() {
  const grid = document.getElementById('trend-chain-grid');
  if (!grid || !state.overview) return;
  const byId = new Map((state.overview.chains || []).map(chain => [chain.chainId, chain]));
  grid.innerHTML = getChains().map(definition => {
    const chain = byId.get(definition.id) || { chainId: definition.id, metricStatus: 'unavailable' };
    const activeClass = state.chainId === definition.id ? ' active' : '';
    const volume = chain.trackedDexVolume24hUsd;
    return `
      <button class="trend-chain-card${activeClass}" type="button" data-action="select-chain" data-chain="${escapeHtml(definition.id)}">
        <div class="trend-chain-top">
          <span class="trend-chain-name" title="${escapeHtml(definition.name)}">${escapeHtml(definition.shortName || definition.name)}</span>
          <span class="trend-status-dot ${escapeHtml(chain.metricStatus || 'unavailable')}" title="${escapeHtml(STATUS_LABELS[chain.metricStatus] || '不可用')}"></span>
        </div>
        <div class="trend-chain-native">Gas · ${escapeHtml(definition.nativeSymbol || '未知')}</div>
        <div class="trend-chain-volume">${escapeHtml(formatUsd(volume, true))}</div>
        <div class="trend-chain-caption">24h 已跟踪 DEX 量${chain.activeProtocols !== null && chain.activeProtocols !== undefined ? ` · ${escapeHtml(formatNumber(chain.activeProtocols, { maximumFractionDigits: 0 }))} 协议` : ''}</div>
      </button>`;
  }).join('');
  const note = document.getElementById('trend-overview-note');
  if (note) note.textContent = `口径：已跟踪 DEX 协议，非全链全量 · ${formatAge(state.overview.meta?.retrievedAt)}`;
}

function renderOverviewError(error) {
  const grid = document.getElementById('trend-chain-grid');
  if (!grid) return;
  grid.innerHTML = getChains().map(definition => `
    <button class="trend-chain-card${state.chainId === definition.id ? ' active' : ''}" type="button" data-action="select-chain" data-chain="${escapeHtml(definition.id)}">
      <div class="trend-chain-top"><span class="trend-chain-name">${escapeHtml(definition.shortName || definition.name)}</span><span class="trend-status-dot"></span></div>
      <div class="trend-chain-native">Gas · ${escapeHtml(definition.nativeSymbol || '未知')}</div>
      <div class="trend-chain-volume">暂不可用</div>
      <div class="trend-chain-caption">${escapeHtml(errorText(error))}</div>
    </button>`).join('');
}

function persistResultFilters() {
  try {
    localStorage.setItem(RESULT_FILTER_STORAGE_KEY, JSON.stringify(state.resultFilters));
  } catch (_) {
    // localStorage may be disabled; filtering still works for the current page session.
  }
}

function resultFiltersAreDefault() {
  return Object.keys(DEFAULT_RESULT_FILTERS).every(key => state.resultFilters[key] === DEFAULT_RESULT_FILTERS[key]);
}

function activeResultFilterCount() {
  return Number(state.resultFilters.volumeMin !== '0')
    + Number(state.resultFilters.marketCapMin !== '0')
    + Number(state.resultFilters.ageMax !== 'all');
}

function syncResultFilterControls() {
  const controls = {
    'trend-filter-volume': state.resultFilters.volumeMin,
    'trend-filter-marketcap': state.resultFilters.marketCapMin,
    'trend-filter-age': state.resultFilters.ageMax,
    'trend-sort-by': state.resultFilters.sortBy,
  };
  Object.entries(controls).forEach(([id, value]) => {
    const control = document.getElementById(id);
    if (control && control.value !== value) control.value = value;
  });
  const direction = document.getElementById('trend-sort-direction');
  if (direction) {
    const relevance = state.resultFilters.sortBy === 'relevance';
    direction.disabled = relevance;
    if (relevance) {
      direction.textContent = '平台相关性';
      direction.title = '综合排序保留数据源返回的相关性顺序';
    } else if (state.resultFilters.sortBy === 'createdAt') {
      direction.textContent = state.resultFilters.sortDir === 'desc' ? '↓ 最新优先' : '↑ 最早优先';
      direction.title = '切换市场创建时间排序方向';
    } else {
      direction.textContent = state.resultFilters.sortDir === 'desc' ? '↓ 从高到低' : '↑ 从低到高';
      direction.title = '切换数值排序方向';
    }
  }
  const reset = root.querySelector('.trend-filter-reset');
  if (reset) reset.disabled = resultFiltersAreDefault();
  const title = root.querySelector('.trend-result-tools-title');
  if (title) {
    const count = activeResultFilterCount();
    title.textContent = count ? `筛选 ${count}` : '筛选';
  }
}

function updateResultFilters(patch) {
  state.resultFilters = normalizeResultFilters({ ...state.resultFilters, ...patch });
  persistResultFilters();
  syncResultFilterControls();
  if (state.results.length) renderSearchResults({ meta: state.searchMeta });
}

function resetResultFilters() {
  state.resultFilters = { ...DEFAULT_RESULT_FILTERS };
  try { localStorage.removeItem(RESULT_FILTER_STORAGE_KEY); } catch (_) { /* session-only fallback */ }
  syncResultFilterControls();
  if (state.results.length) renderSearchResults({ meta: state.searchMeta });
}

function finiteResultMetric(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function sortableResultValue(item, sortBy) {
  if (sortBy === 'createdAt') return parseTimestampMs(item.market?.createdAt);
  return finiteResultMetric(item.quote?.[sortBy]);
}

function getVisibleSearchResults() {
  const volumeMin = Number(state.resultFilters.volumeMin);
  const marketCapMin = Number(state.resultFilters.marketCapMin);
  const maximumAge = RESULT_AGE_MAX_MS[state.resultFilters.ageMax] ?? null;
  const now = Date.now();
  const visible = state.results.map((item, index) => ({ item, index })).filter(({ item }) => {
    const volume = finiteResultMetric(item.quote?.volume24hUsd);
    const marketCap = finiteResultMetric(item.quote?.marketCapUsd);
    const createdAt = parseTimestampMs(item.market?.createdAt);
    if (volumeMin > 0 && (volume === null || volume < volumeMin)) return false;
    if (marketCapMin > 0 && (marketCap === null || marketCap < marketCapMin)) return false;
    if (maximumAge !== null) {
      const age = createdAt === null ? null : now - createdAt;
      if (age === null || age < 0 || age > maximumAge) return false;
    }
    return true;
  });
  if (state.resultFilters.sortBy === 'relevance') return visible;
  const direction = state.resultFilters.sortDir === 'asc' ? 1 : -1;
  return visible.sort((left, right) => {
    const leftValue = sortableResultValue(left.item, state.resultFilters.sortBy);
    const rightValue = sortableResultValue(right.item, state.resultFilters.sortBy);
    if (leftValue === null && rightValue === null) return left.index - right.index;
    if (leftValue === null) return 1;
    if (rightValue === null) return -1;
    const difference = (leftValue - rightValue) * direction;
    return difference || left.index - right.index;
  });
}

async function runSearch() {
  const input = document.getElementById('trend-search-input');
  const query = input?.value.trim() || '';
  if (!query) {
    input?.focus();
    showSearchMessage('请输入币名、Symbol 或合约地址');
    return;
  }
  if (state.loading.has('search')) return;
  state.loading.add('search');
  const button = document.getElementById('trend-search-submit');
  if (button) { button.disabled = true; button.textContent = '搜索中…'; }
  const panel = document.getElementById('trend-search-state');
  const list = document.getElementById('trend-result-list');
  document.getElementById('trend-result-tools')?.classList.remove('visible');
  panel?.classList.add('visible');
  if (list) list.innerHTML = '<div class="trend-empty" style="grid-column:1/-1">正在并行查询 Binance、DEX Screener、GeckoTerminal 和 Robinhood…</div>';
  const summary = document.getElementById('trend-search-summary');
  if (summary) summary.innerHTML = `<span>搜索「${escapeHtml(query)}」</span><span>正在读取公开数据源</span>`;
  try {
    const data = await api('/api/trend/search', { params: { q: query, chainId: state.chainId, limit: 30 }, timeoutMs: 18000 });
    if (!state.active) return;
    state.results = data.results || [];
    state.searchMeta = data.meta || null;
    renderSearchResults(data);
  } catch (error) {
    if (!state.active) return;
    state.results = [];
    state.searchMeta = null;
    showSearchMessage(errorText(error), true);
  } finally {
    state.loading.delete('search');
    if (button) { button.disabled = false; button.textContent = '搜索'; }
    if (state.active) loadCapabilities(true);
  }
}

function showSearchMessage(message, isError = false) {
  const panel = document.getElementById('trend-search-state');
  const list = document.getElementById('trend-result-list');
  const summary = document.getElementById('trend-search-summary');
  document.getElementById('trend-result-tools')?.classList.remove('visible');
  panel?.classList.add('visible');
  if (summary) summary.innerHTML = '<span>搜索结果</span>';
  if (list) list.innerHTML = `<div class="${isError ? 'trend-error' : 'trend-empty'}" style="grid-column:1/-1">${escapeHtml(message)}</div>`;
}

function renderSearchResults(data) {
  const panel = document.getElementById('trend-search-state');
  const list = document.getElementById('trend-result-list');
  const summary = document.getElementById('trend-search-summary');
  const tools = document.getElementById('trend-result-tools');
  panel?.classList.add('visible');
  const errors = data.meta?.partialErrors || [];
  const visibleResults = getVisibleSearchResults();
  tools?.classList.toggle('visible', state.results.length > 0);
  syncResultFilterControls();
  if (summary) {
    const countLabel = visibleResults.length === state.results.length
      ? `找到 ${state.results.length} 个具体市场`
      : `找到 ${state.results.length} 个具体市场 · 当前显示 ${visibleResults.length} 个`;
    const sourceLabel = errors.length
      ? `${errors.length} 个数据源暂不可用，已保留其他结果`
      : '排序与筛选仅针对本次返回结果';
    summary.innerHTML = `<span>${escapeHtml(countLabel)}</span><span>${escapeHtml(sourceLabel)}</span>`;
  }
  if (!list) return;
  if (!state.results.length) {
    list.innerHTML = '<div class="trend-empty" style="grid-column:1/-1">没有找到匹配市场。请尝试 Symbol、英文名，或先选择正确的链再输入合约地址。</div>';
    return;
  }
  if (!visibleResults.length) {
    list.innerHTML = `<div class="trend-empty trend-empty-filtered" style="grid-column:1/-1">
      <span>当前筛选条件下没有匹配市场。缺少对应指标的结果不会按 0 计算。</span>
      <button type="button" data-action="reset-result-filters">重置筛选</button>
    </div>`;
    return;
  }
  list.innerHTML = visibleResults.map(({ item, index }) => {
    const asset = item.asset || {};
    const market = item.market || {};
    const quote = item.quote || {};
    const source = item.source || {};
    const targetId = `${asset.assetId || ''}|${market.marketId || ''}`;
    const saved = state.watchlist.some(watch => watch.targetId === targetId);
    const createdTitle = parseTimestampMs(market.createdAt) === null
      ? '公开数据源未提供交易池或市场创建时间'
      : `交易池或市场创建于 ${formatTimestamp(market.createdAt)}；不代表 Token 合约部署时间`;
    const logo = tokenLogoHtml(asset.logoUrl);
    return `
      <article class="trend-result-card${saved ? ' saved' : ''}">
        <button class="trend-result-open${logo ? ' has-token-logo' : ''}" type="button" data-action="select-result" data-index="${index}">
          ${logo}
          <span class="trend-result-main">
            <span class="trend-result-name"><span class="trend-result-symbol">${escapeHtml(asset.symbol || '?')}</span><span class="trend-result-fullname">${escapeHtml(asset.nameZh || asset.name || '')}</span></span>
            <span class="trend-result-meta" title="${escapeHtml(asset.address || '')}">${escapeHtml(chainLabel(asset.chainId))} · ${escapeHtml(market.venue || market.type || '未知市场')} · ${escapeHtml(market.pair || '')}${asset.address ? ` · ${escapeHtml(shortAddress(asset.address))}` : ''} · ${escapeHtml(source.label || providerLabel(source.provider))}${market.type === 'dex' ? ' · DEX 同名币需核验合约' : ''}</span>
            <span class="trend-result-facts">
              <span>市值 <strong>${escapeHtml(formatUsd(quote.marketCapUsd, true))}</strong></span>
              <span>流动性 <strong>${escapeHtml(formatUsd(quote.liquidityUsd, true))}</strong></span>
              <span title="${escapeHtml(createdTitle)}">${escapeHtml(formatMarketCreated(market.createdAt))}</span>
            </span>
          </span>
          <span class="trend-result-quote">
            <span class="trend-result-price">${escapeHtml(formatUsd(quote.priceUsd))}</span>
            <span class="trend-result-volume">24h ${escapeHtml(formatUsd(quote.volume24hUsd, true))}</span>
          </span>
        </button>
        <button class="trend-result-watch${saved ? ' saved' : ''}" type="button" data-action="toggle-result-watch" data-index="${index}" aria-label="${saved ? '取消收藏' : '收藏'} ${escapeHtml(asset.symbol || '')}" title="${saved ? '从我的收藏移除' : '加入我的收藏'}">★</button>
      </article>`;
  }).join('');
}

function selectResult(index) {
  const result = state.results[index];
  if (!result) return;
  abortRequests();
  state.selection = result;
  state.selectionVersion += 1;
  state.detail = null;
  state.ohlcv = null;
  state.flows = null;
  state.trades = null;
  state.risk = null;
  state.detailOrigin = 'overview';
  setTrendPage('detail');
  renderSelectionPreview(result);
  renderDetailLoading();
  const version = state.selectionVersion;
  loadDetail(version, true);
  loadOhlcv(version, true);
  loadFlows(version, true);
  loadTrades(version, true);
  loadRisk(version, true);
  restartPollers();
  document.querySelector('[data-trend-page="detail"]')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderSelectionPreview(result) {
  const asset = result.asset || {};
  const market = result.market || {};
  const quote = result.quote || {};
  renderAssetHero(asset, market, quote, result.metricStatus || 'partial', null);
  renderQuoteGrid(quote);
  const meta = document.getElementById('trend-asset-meta');
  if (meta) {
    meta.innerHTML = `<span>${escapeHtml(result.source?.label || providerLabel(result.source?.provider))}</span>${statusBadge(result.metricStatus)}<span>正在读取详情</span>`;
  }
}

function setChartHistoryStatus(text) {
  const historyStatus = document.getElementById('trend-chart-history-status');
  if (historyStatus) historyStatus.textContent = text;
}

function resetChartLoadingState(message = '正在加载最新 K 线…') {
  const chartMessage = document.getElementById('trend-chart-message');
  if (chartMessage) { chartMessage.textContent = message; chartMessage.classList.add('visible'); }
  const chartMeta = document.getElementById('trend-chart-meta');
  if (chartMeta) chartMeta.innerHTML = '<span>正在读取当前市场 K 线…</span>';
  setChartHistoryStatus('正在加载最新 K 线…');
  document.getElementById('trend-chart-tooltip')?.style.setProperty('display', 'none');
}

function renderDetailLoading() {
  cancelChartRequest();
  resetChartLoadingState('正在加载 K 线…');
  // A failed request for a newly selected market must not leave the previous
  // market's candles visible under the new asset identity.
  if (state.candleSeries) state.candleSeries.setData([]);
  if (state.volumeSeries) state.volumeSeries.setData([]);
  document.getElementById('trend-flow-grid').innerHTML = Array.from({ length: 8 }, () => '<div class="trend-stat trend-skeleton"><div class="trend-stat-label">加载</div><div class="trend-stat-value">—</div></div>').join('');
  document.getElementById('trend-trades-wrap').innerHTML = '<div class="trend-empty">正在读取最近成交…</div>';
  renderRisk(null, true);
}

function currentIds() {
  const detail = state.detail || {};
  return {
    assetId: detail.asset?.assetId || state.selection?.asset?.assetId || '',
    marketId: detail.market?.marketId || state.selection?.market?.marketId || '',
  };
}

async function loadDetail(version, firstLoad = false) {
  const { assetId, marketId } = currentIds();
  if (!assetId || !marketId) return;
  const loadingKey = `detail:${version}:${assetId}:${marketId}`;
  if (!state.active || !state.selection || state.loading.has(loadingKey)) return;
  state.loading.add(loadingKey);
  try {
    const data = await api('/api/trend/asset', { params: { assetId, marketId }, timeoutMs: 15000 });
    if (!state.active || state.page !== 'detail' || version !== state.selectionVersion) return;
    state.detail = data;
    renderDetail(data);
  } catch (error) {
    if (!state.active || state.page !== 'detail' || version !== state.selectionVersion) return;
    if (!firstLoad && state.detail) {
      markMetaError('trend-asset-meta', error);
    } else {
      const preview = state.selection;
      renderSelectionPreview(preview);
      markMetaError('trend-asset-meta', error);
    }
  } finally {
    state.loading.delete(loadingKey);
    if (state.active) loadCapabilities(true);
  }
}

function renderDetail(data) {
  const asset = data.asset || {};
  const market = data.market || {};
  const quote = data.quote || {};
  renderAssetHero(asset, market, quote, data.meta?.metricStatus, data.meta);
  renderQuoteGrid(quote);
  const meta = document.getElementById('trend-asset-meta');
  if (meta) meta.innerHTML = metaHtml(data.meta, market.sourceUrl);
  renderWatchlist();
}

function renderAssetHero(asset, market, quote, metricStatus, meta) {
  const hero = document.getElementById('trend-asset-hero');
  if (!hero) return;
  const change = Number(quote.change24h);
  const changeClass = Number.isFinite(change) ? (change > 0 ? 'up' : change < 0 ? 'down' : 'flat') : 'flat';
  const targetId = `${asset.assetId || ''}|${market.marketId || ''}`;
  const saved = state.watchlist.some(item => item.targetId === targetId);
  const logo = tokenLogoHtml(asset.logoUrl, 'width:43px;height:43px;border-radius:13px');
  hero.innerHTML = `
    ${logo}
    <div class="trend-asset-identity">
      <div class="trend-asset-title"><h2>${escapeHtml(asset.symbol || '?')}</h2><span class="trend-asset-name">${escapeHtml(asset.nameZh || asset.name || '')}</span>${statusBadge(metricStatus, meta?.stale)}</div>
      <div class="trend-asset-meta" title="${escapeHtml(asset.address || '')}">${escapeHtml(chainLabel(asset.chainId))} · ${escapeHtml(market.venue || market.type || '')} · ${escapeHtml(market.pair || '')} · ${escapeHtml(shortAddress(asset.address))}</div>
    </div>
    <button class="trend-watch-toggle${saved ? ' saved' : ''}" type="button" data-action="toggle-watch" title="${saved ? '从我的收藏移除' : '加入我的收藏'}" aria-label="${saved ? '取消收藏' : '加入收藏'}">★</button>
    <div class="trend-asset-price">
      <div class="trend-price-main">${escapeHtml(formatUsd(quote.priceUsd))}</div>
      <span class="trend-price-change ${changeClass}">24h ${escapeHtml(formatPercent(quote.change24h))}</span>
    </div>`;
  hero.classList.toggle('has-token-logo', Boolean(logo));
}

function statHtml(label, value, foot = '') {
  return `<div class="trend-stat"><div class="trend-stat-label">${escapeHtml(label)}</div><div class="trend-stat-value" title="${escapeHtml(value)}">${escapeHtml(value)}</div>${foot ? `<div class="trend-stat-foot">${escapeHtml(foot)}</div>` : ''}</div>`;
}

function renderQuoteGrid(quote = {}) {
  const grid = document.getElementById('trend-quote-grid');
  if (!grid) return;
  const dailyTokenVolume = quote.dailyTradingVolumeToken;
  grid.innerHTML = [
    statHtml('24h 成交额', formatUsd(quote.volume24hUsd, true), dailyTokenVolume != null ? '该市场只提供 Token 单位' : ''),
    statHtml('24h 成交量 (Token)', formatCompact(quote.baseVolume24h ?? dailyTokenVolume)),
    statHtml('流动性', formatUsd(quote.liquidityUsd, true)),
    statHtml('市值', formatUsd(quote.marketCapUsd, true)),
    statHtml('FDV', formatUsd(quote.fdvUsd, true)),
    statHtml('24h 最高', formatUsd(quote.high24h)),
    statHtml('24h 最低', formatUsd(quote.low24h)),
    statHtml('买/卖笔数', quote.buyCount24h != null || quote.sellCount24h != null ? `${formatCompact(quote.buyCount24h)} / ${formatCompact(quote.sellCount24h)}` : '暂不可用'),
  ].join('');
}

function renderTrendPage() {
  if (state.page === 'detail' && !state.selection) {
    state.page = 'overview';
    localStorage.setItem('seegent-trend-page', state.page);
  }
  document.querySelectorAll('[data-trend-page]').forEach(page => {
    page.classList.toggle('active', page.dataset.trendPage === state.page);
  });
  root.querySelectorAll('.trend-subtab').forEach(button => {
    button.classList.toggle('active', button.dataset.page === state.page);
  });
  const detailTab = document.getElementById('trend-detail-tab');
  if (detailTab) detailTab.disabled = !state.selection;
  const backLabel = document.getElementById('trend-detail-back-label');
  if (backLabel) backLabel.textContent = state.detailOrigin === 'watchlist' ? '返回我的收藏' : '返回市场总览';
}

function setTrendPage(page, persist = true) {
  if (!TREND_PAGES.includes(page)) return;
  if (page === 'detail' && !state.selection) page = 'overview';
  if (state.page === 'detail' && page !== 'detail') abortRequests();
  state.page = page;
  // 详情页依赖当前内存中的具体市场；只持久化两个稳定入口页。
  // 刷新详情时回到来源页，避免出现没有选择对象的空详情。
  if (persist && page !== 'detail') localStorage.setItem('seegent-trend-page', page);
  renderTrendPage();
  window.requestAnimationFrame(resizeChart);
  restartPollers();
  if (!state.active) return;
  if (page === 'overview') loadOverview();
  if (page === 'watchlist') {
    loadWatchlist().then(() => refreshWatchlistQuotes());
  }
}

function mergeChartCandle(left, right) {
  if (!left) return right;
  if (!right) return left;
  const first = Number(left.timestamp) <= Number(right.timestamp) ? left : right;
  const last = first === left ? right : left;
  const sum = (a, b) => {
    const leftValue = Number(a);
    const rightValue = Number(b);
    if (!Number.isFinite(leftValue) && !Number.isFinite(rightValue)) return null;
    return (Number.isFinite(leftValue) ? leftValue : 0) + (Number.isFinite(rightValue) ? rightValue : 0);
  };
  return {
    ...last,
    timestamp: Number(left.timestamp),
    open: Number(first.open),
    high: Math.max(Number(left.high), Number(right.high)),
    low: Math.min(Number(left.low), Number(right.low)),
    close: Number(last.close),
    volume: sum(left.volume, right.volume),
    quoteVolume: sum(left.quoteVolume, right.quoteVolume),
    tradeCount: sum(left.tradeCount, right.tradeCount),
    confirmed: Boolean(left.confirmed && right.confirmed),
  };
}

function mergeChartPages(current, next, { older = false, latest = false } = {}) {
  const candlesByTime = new Map();
  [...(current?.candles || []), ...(next?.candles || [])].forEach(candle => {
    const timestamp = Number(candle.timestamp);
    if (!Number.isFinite(timestamp)) return;
    candlesByTime.set(timestamp, mergeChartCandle(candlesByTime.get(timestamp), candle));
  });
  const candles = [...candlesByTime.values()].sort((left, right) => Number(left.timestamp) - Number(right.timestamp));
  const metas = [current?.meta, next?.meta].filter(Boolean);
  const coverageStarts = metas.map(meta => meta.coverageStart).filter(Boolean);
  const coverageEnds = metas.map(meta => meta.coverageEnd).filter(Boolean);
  const errors = [];
  metas.flatMap(meta => meta.partialErrors || []).forEach(error => {
    const key = `${error.code || ''}:${error.message || ''}`;
    if (!errors.some(item => `${item.code || ''}:${item.message || ''}` === key)) errors.push(error);
  });
  if (older) {
    const transient = new Set(['history_page_budget', 'history_page_failed', 'rate_limited', 'provider_http']);
    errors.splice(0, errors.length, ...errors.filter(error => !transient.has(error.code)));
  }
  const nextCursor = older
    ? (next?.nextCursor || null)
    : latest
      ? (current?.nextCursor || null)
      : (current?.nextCursor || next?.nextCursor || null);
  const historyComplete = latest
    ? Boolean(current?.historyComplete ?? !nextCursor)
    : !nextCursor && Boolean(next?.historyComplete ?? current?.historyComplete ?? true);
  const meta = {
    ...(current?.meta || {}),
    ...(next?.meta || {}),
    coverageStart: coverageStarts.sort()[0] || null,
    coverageEnd: coverageEnds.sort().at(-1) || null,
    metricStatus: historyComplete && !errors.length ? 'exact' : 'partial',
    partialErrors: errors,
  };
  return {
    ...(current || {}),
    ...(next || {}),
    candles,
    meta,
    nextCursor,
    hasMore: Boolean(nextCursor),
    historyComplete,
  };
}

function addChartHistoryWarning(data, code, message) {
  const errors = [...(data.meta?.partialErrors || [])];
  if (!errors.some(error => error.code === code)) errors.push({ code, message });
  return {
    ...data,
    meta: { ...(data.meta || {}), metricStatus: 'partial', partialErrors: errors },
  };
}

function chartHistoryPageBudget(provider, continueHistory) {
  if (continueHistory) return CHART_HISTORY_MAX_PAGES;
  return CHART_HISTORY_AUTO_PAGES;
}

function isCurrentChartRequest(version, marketId, requestedInterval, requestGeneration, requestKey) {
  const ids = currentIds();
  return state.active
    && state.page === 'detail'
    && Boolean(state.selection)
    && version === state.selectionVersion
    && requestedInterval === state.chartInterval
    && requestGeneration === state.chartRequestGeneration
    && ids.marketId === marketId
    && state.chartRequestKey === requestKey;
}

async function loadOhlcv(version, firstLoad = false, continueHistory = false) {
  const requestedInterval = state.chartInterval;
  const requestedChartRange = CHART_HISTORY_RANGE;
  const existing = continueHistory ? state.ohlcv : (!firstLoad ? state.ohlcv : null);
  const cursor = continueHistory ? existing?.nextCursor : null;
  if (continueHistory && !cursor) return;
  const { marketId } = currentIds();
  const requestGeneration = state.chartRequestGeneration;
  if (!state.active || state.page !== 'detail' || !state.selection || !marketId) return;
  const loadingKey = 'ohlcv:' + requestGeneration + ':' + version + ':' + marketId + ':' + requestedInterval + ':' + requestedChartRange + ':' + (continueHistory ? cursor : 'latest');
  if (state.loading.has(loadingKey)) return;
  state.loading.add(loadingKey);
  state.chartRequestKey = loadingKey;
  state.chartHistoryLoading = true;
  const message = document.getElementById('trend-chart-message');
  if (firstLoad && message) { message.textContent = '正在加载最新 K 线…'; message.classList.add('visible'); }
  const isCurrent = () => isCurrentChartRequest(version, marketId, requestedInterval, requestGeneration, loadingKey);
  try {
    let data;
    if (continueHistory) {
      const page = await api('/api/trend/ohlcv', { params: { marketId, interval: requestedInterval, range: requestedChartRange, cursor }, timeoutMs: 18000 });
      if (!isCurrent()) return;
      data = mergeChartPages(existing, page, { older: true });
      state.ohlcv = data;
      renderChart(data);
    } else {
      const page = await api('/api/trend/ohlcv', { params: { marketId, interval: requestedInterval, range: requestedChartRange }, timeoutMs: 18000 });
      if (!isCurrent()) return;
      data = existing ? mergeChartPages(existing, page, { latest: true }) : page;
      state.ohlcv = data;
      renderChart(data);
    }

    // A periodic refresh only updates the newest page. Older history is loaded
    // only through the explicit “加载更早历史” action.
    let nextCursor = (firstLoad || continueHistory || !existing) ? data.nextCursor : null;
    let pages = 1;
    const pageBudget = chartHistoryPageBudget(data.meta?.provider, continueHistory);
    while (nextCursor && pages < pageBudget && isCurrent()) {
      const page = await api('/api/trend/ohlcv', { params: { marketId, interval: requestedInterval, range: requestedChartRange, cursor: nextCursor }, timeoutMs: 18000 });
      if (!isCurrent()) return;
      data = mergeChartPages(data, page, { older: true });
      state.ohlcv = data;
      nextCursor = data.nextCursor;
      pages += 1;
      renderChart(data);
    }
    if (isCurrent() && nextCursor && pages >= pageBudget) {
      state.ohlcv = addChartHistoryWarning(data, 'history_page_budget', '历史数据较多，已先加载当前周期的一部分；点击“加载更早历史”继续');
      renderChart(state.ohlcv);
    }
  } catch (error) {
    if (!isCurrent()) return;
    if (state.ohlcv?.candles?.length) {
      state.ohlcv = addChartHistoryWarning(
        state.ohlcv,
        error.code || 'history_page_failed',
        `${errorText(error)}，已保留当前已加载历史`,
      );
      renderChart(state.ohlcv);
    } else {
      setChartHistoryStatus('当前市场 K 线暂不可用');
      showChartMessage(errorText(error));
      markMetaError('trend-chart-meta', error);
    }
  } finally {
    const canRender = isCurrent();
    state.loading.delete(loadingKey);
    if (state.chartRequestKey === loadingKey) {
      state.chartHistoryLoading = false;
      state.chartRequestKey = null;
      if (canRender && state.ohlcv) renderChart(state.ohlcv);
    }
  }
}

function chartColors() {
  const style = getComputedStyle(document.documentElement);
  return {
    background: style.getPropertyValue('--bg-card').trim() || '#ffffff',
    text: style.getPropertyValue('--text-tertiary').trim() || '#808080',
    border: style.getPropertyValue('--border-light').trim() || '#eeeeee',
    up: getComputedStyle(root).getPropertyValue('--trend-up').trim() || '#0f9f6e',
    down: getComputedStyle(root).getPropertyValue('--trend-down').trim() || '#ef5350',
  };
}

function resizeChart() {
  const container = document.getElementById('trend-chart');
  if (!container || !state.chart) return;
  const rect = container.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0) {
    state.chart.applyOptions({ width: Math.max(Math.floor(rect.width), 1), height: Math.max(Math.floor(rect.height), 1) });
  }
}

function chartAutoscaleInfo(originalImplementation) {
  const info = originalImplementation();
  if (!info?.priceRange) return info;
  const min = Number(info.priceRange.minValue);
  const max = Number(info.priceRange.maxValue);
  if (!Number.isFinite(min) || !Number.isFinite(max)) return info;
  const span = Math.max(max - min, 0);
  const scale = Math.max(Math.abs(min), Math.abs(max), 1e-12);
  // Keep small real movements visible without changing the candle data or
  // manufacturing points when a market is genuinely flat.
  const padding = Math.max(span * 0.08, scale * 0.002, 1e-12);
  return { ...info, priceRange: { minValue: min - padding, maxValue: max + padding } };
}

function ensureChart() {
  if (state.chart) return;
  const container = document.getElementById('trend-chart');
  if (!container) return;
  const colors = chartColors();
  state.chart = createChart(container, {
    width: Math.max(container.clientWidth, 300),
    height: Math.max(container.clientHeight, 260),
    layout: { background: { type: ColorType.Solid, color: colors.background }, textColor: colors.text, fontFamily: getComputedStyle(document.body).fontFamily, fontSize: 10, attributionLogo: false },
    grid: { vertLines: { color: colors.border }, horzLines: { color: colors.border } },
    rightPriceScale: { borderColor: colors.border, scaleMargins: { top: 0.08, bottom: 0.27 } },
    timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false, rightOffset: 4, barSpacing: 7, tickMarkFormatter: chartTickLabel },
    // Lightweight Charts also draws a date label on the crosshair. The custom
    // OHLC tooltip already owns the timestamp, so hiding the built-in label
    // prevents the same candle date from appearing twice.
    crosshair: { mode: CrosshairMode.Normal, vertLine: { color: colors.text, width: 1, style: 3, labelVisible: false, labelBackgroundColor: colors.text }, horzLine: { color: colors.text, width: 1, style: 3, labelVisible: false, labelBackgroundColor: colors.text } },
    localization: { locale: navigator.language || 'zh-CN' },
  });
  state.candleSeries = state.chart.addSeries(CandlestickSeries, {
    upColor: colors.up, downColor: colors.down, borderUpColor: colors.up, borderDownColor: colors.down,
    wickUpColor: colors.up, wickDownColor: colors.down, priceLineVisible: true,
    autoscaleInfoProvider: chartAutoscaleInfo,
  });
  state.volumeSeries = state.chart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' }, priceScaleId: 'volume', lastValueVisible: false, priceLineVisible: false,
  });
  state.chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 }, borderVisible: false });
  state.chart.subscribeCrosshairMove(updateChartTooltip);
  state.chartResizeObserver = new ResizeObserver(entries => {
    const rect = entries[0]?.contentRect;
    if (rect && state.chart) state.chart.applyOptions({ width: Math.max(Math.floor(rect.width), 1), height: Math.max(Math.floor(rect.height), 1) });
  });
  state.chartResizeObserver.observe(container);
  resizeChart();
}

function applyChartTheme() {
  if (!state.chart) return;
  const colors = chartColors();
  state.chart.applyOptions({
    layout: { background: { type: ColorType.Solid, color: colors.background }, textColor: colors.text },
    grid: { vertLines: { color: colors.border }, horzLines: { color: colors.border } },
    rightPriceScale: { borderColor: colors.border },
    timeScale: { borderColor: colors.border, tickMarkFormatter: chartTickLabel },
  });
  state.candleSeries?.applyOptions({ upColor: colors.up, borderUpColor: colors.up, wickUpColor: colors.up, downColor: colors.down, borderDownColor: colors.down, wickDownColor: colors.down });
  if (state.ohlcv) renderChart(state.ohlcv);
}

function renderChart(data) {
  const candles = (data.candles || []).filter(item =>
    Number.isFinite(Number(item.timestamp)) && ['open', 'high', 'low', 'close'].every(key => Number.isFinite(Number(item[key])))
  );
  const meta = document.getElementById('trend-chart-meta');
  const requestedInterval = data.requestedInterval || state.chartInterval;
  const effectiveInterval = data.effectiveInterval || requestedInterval;
  const chartErrors = partialErrors(data.meta);
  if (meta) {
    const aggregationLabel = data.meta?.nativeOrAggregated === 'utc-aggregated' ? 'UTC 聚合真实 K 线' : 'Provider 原生周期';
    meta.innerHTML = `${metaHtml(data.meta, '', { includeYear: true })}<span>每根 ${escapeHtml(CHART_INTERVAL_LABELS[requestedInterval] || requestedInterval)} · ${candles.length} 根</span><span>${escapeHtml(aggregationLabel)}</span>${effectiveInterval !== requestedInterval ? `<span>来源周期 ${escapeHtml(effectiveInterval)}</span>` : ''}${chartErrors.length ? `<span class="trend-chart-inline-warning">${escapeHtml(chartErrors[0])}</span>` : ''}`;
  }
  const historyStatus = document.getElementById('trend-chart-history-status');
  if (historyStatus) {
    if (state.chartHistoryLoading) {
      historyStatus.innerHTML = `正在读取最新页… ${candles.length} 根`;
    } else if (data.nextCursor) {
      historyStatus.innerHTML = `${candles.length} 根 · <button type="button" data-action="load-chart-history">加载更早历史</button>`;
    } else {
      historyStatus.textContent = `全部可取得历史 · ${candles.length} 根`;
    }
  }
  if (!candles.length) {
    setChartHistoryStatus('当前市场暂无可用 K 线');
    showChartMessage(partialErrors(data.meta)[0] || '该市场在当前范围没有公开 K 线数据');
    if (state.candleSeries) state.candleSeries.setData([]);
    if (state.volumeSeries) state.volumeSeries.setData([]);
    return;
  }
  ensureChart();
  const colors = chartColors();
  state.candleSeries.setData(candles.map(item => ({
    time: Number(item.timestamp), open: Number(item.open), high: Number(item.high), low: Number(item.low), close: Number(item.close),
  })));
  state.volumeSeries.setData(candles.map(item => ({
    time: Number(item.timestamp), value: Number(item.quoteVolume ?? item.volume ?? 0), color: Number(item.close) >= Number(item.open) ? `${colors.up}88` : `${colors.down}88`,
  }))); 
  resizeChart();
  const chartWidth = document.getElementById('trend-chart')?.clientWidth || 640;
  // Fit the complete returned history into the initial viewport. The old
  // 1.5px minimum left a 3,000-candle BTC chart parked on only its newest
  // ~700 candles, which made a full-history response look truncated.
  const barSpacing = Math.max(0.25, Math.min(11, chartWidth / Math.max(candles.length, 1)));
  state.chart.timeScale().applyOptions({
    secondsVisible: false,
    rightOffset: candles.length > 2 ? 4 : 1,
    barSpacing,
    minBarSpacing: 0.1,
  });
  state.chart.priceScale('right').applyOptions({ autoScale: true, scaleMargins: { top: 0.08, bottom: 0.27 } });
  state.chart.timeScale().fitContent();
  document.getElementById('trend-chart-message')?.classList.remove('visible');
}

function showChartMessage(message) {
  const element = document.getElementById('trend-chart-message');
  if (element) { element.textContent = message; element.classList.add('visible'); }
}

function updateChartTooltip(param) {
  const tooltip = document.getElementById('trend-chart-tooltip');
  const wrap = document.getElementById('trend-chart-wrap');
  if (!tooltip || !wrap || !param?.point || !param.time || !state.candleSeries) {
    if (tooltip) tooltip.style.display = 'none';
    return;
  }
  const data = param.seriesData.get(state.candleSeries);
  if (!data) { tooltip.style.display = 'none'; return; }
  tooltip.innerHTML = `
    <div>${escapeHtml(formatChartTime(Number(param.time)))}</div>
    <div>O ${escapeHtml(formatNumber(data.open))} &nbsp; H ${escapeHtml(formatNumber(data.high))}</div>
    <div>L ${escapeHtml(formatNumber(data.low))} &nbsp; C ${escapeHtml(formatNumber(data.close))}</div>`;
  tooltip.style.display = 'block';
  const left = Math.min(Math.max(param.point.x + 14, 8), wrap.clientWidth - 164);
  const top = Math.min(Math.max(param.point.y + 14, 8), wrap.clientHeight - 78);
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${top}px`;
}

async function loadFlows(version, firstLoad = false) {
  const requestedRange = state.range;
  const { assetId, marketId } = currentIds();
  if (!assetId || !marketId) return;
  const loadingKey = `flows:${version}:${assetId}:${marketId}:${requestedRange}`;
  if (!state.active || !state.selection || state.loading.has(loadingKey)) return;
  state.loading.add(loadingKey);
  try {
    const data = await api('/api/trend/flows', { params: { assetId, marketId, range: requestedRange }, timeoutMs: 16000 });
    if (!state.active || state.page !== 'detail' || version !== state.selectionVersion || requestedRange !== state.range) return;
    state.flows = data;
    renderFlows(data);
  } catch (error) {
    if (!state.active || state.page !== 'detail' || version !== state.selectionVersion || requestedRange !== state.range) return;
    if (!state.flows || firstLoad) renderFlowError(error);
    markMetaError('trend-flow-meta', error);
  } finally {
    state.loading.delete(loadingKey);
  }
}

function renderFlows(data) {
  const metrics = data.metrics || {};
  const grid = document.getElementById('trend-flow-grid');
  if (grid) grid.innerHTML = [
    statHtml('交易量 (USD)', formatUsd(metrics.tradeVolumeUsd, true), data.meta?.nativeOrAggregated === 'recent-trade-sample' ? '最近成交样本' : '当前选中市场'),
    statHtml('买入量 (USD)', formatUsd(metrics.buyVolumeUsd, true), '选中资产视角'),
    statHtml('卖出量 (USD)', formatUsd(metrics.sellVolumeUsd, true), '选中资产视角'),
    statHtml('买/卖笔数', metrics.buyCount != null || metrics.sellCount != null ? `${formatCompact(metrics.buyCount)} / ${formatCompact(metrics.sellCount)}` : '暂不可用'),
    statHtml('Token 转账总量', formatCompact(metrics.transferAmountToken), '需要链上索引数据源'),
    statHtml('转账估值 (USD)', formatUsd(metrics.transferValueUsd, true), '不用当前价伪造历史估值'),
    statHtml('转账笔数 / 唯一地址', metrics.transferCount != null || metrics.uniqueWallets != null ? `${formatCompact(metrics.transferCount)} / ${formatCompact(metrics.uniqueWallets)}` : '暂不可用'),
    statHtml('铸造/销毁量', metrics.mintBurnUsdVolume != null ? formatUsd(metrics.mintBurnUsdVolume, true) : formatCompact(metrics.mintBurnTokenVolume), metrics.tradeVolumeToken != null ? `日成交 ${formatCompact(metrics.tradeVolumeToken)} Token` : ''),
  ].join('');
  const meta = document.getElementById('trend-flow-meta');
  if (meta) meta.innerHTML = metaHtml(data.meta);
  const errors = partialErrors(data.meta);
  const warning = document.getElementById('trend-flow-warning');
  if (warning) warning.innerHTML = errors.length ? `<div class="trend-data-warning">${escapeHtml(errors.join(' · '))}</div>` : '';
}

function renderFlowError(error) {
  const grid = document.getElementById('trend-flow-grid');
  if (grid) grid.innerHTML = `<div class="trend-error" style="grid-column:1/-1">${escapeHtml(errorText(error))}</div>`;
}

async function loadTrades(version, firstLoad = false) {
  const requestedRange = state.range;
  const { marketId } = currentIds();
  if (!marketId) return;
  const loadingKey = `trades:${version}:${marketId}:${requestedRange}`;
  if (!state.active || !state.selection || state.loading.has(loadingKey)) return;
  state.loading.add(loadingKey);
  try {
    const data = await api('/api/trend/trades', { params: { marketId, range: requestedRange, limit: 80 }, timeoutMs: 16000 });
    if (!state.active || state.page !== 'detail' || version !== state.selectionVersion || requestedRange !== state.range) return;
    state.trades = data;
    renderTrades(data);
  } catch (error) {
    if (!state.active || state.page !== 'detail' || version !== state.selectionVersion || requestedRange !== state.range) return;
    if (!state.trades || firstLoad) renderTradesError(error);
    markMetaError('trend-trades-meta', error);
  } finally {
    state.loading.delete(loadingKey);
  }
}

function transactionUrl(hash) {
  if (!hash) return '';
  const chainId = state.detail?.asset?.chainId || state.selection?.asset?.chainId;
  const explorer = safeHttpsUrl(chainById(chainId)?.explorer);
  if (!explorer) return '';
  const segment = chainId === 'solana' ? '/tx/' : chainId === 'ton' ? '/transaction/' : '/tx/';
  return explorer.replace(/\/$/, '') + segment + encodeURIComponent(hash);
}

function renderTrades(data) {
  const wrap = document.getElementById('trend-trades-wrap');
  const meta = document.getElementById('trend-trades-meta');
  if (meta) meta.innerHTML = metaHtml(data.meta);
  if (!wrap) return;
  const trades = data.trades || [];
  if (!trades.length) {
    wrap.innerHTML = `<div class="trend-empty">${escapeHtml(partialErrors(data.meta)[0] || '当前公开数据源没有逐笔成交数据')}</div>`;
    return;
  }
  wrap.innerHTML = `
    <table class="trend-table">
      <thead><tr><th style="width:25%">时间（本地）</th><th style="width:13%">方向</th><th>价格</th><th>数量</th><th>估值</th></tr></thead>
      <tbody>${trades.map(trade => {
        const side = trade.side === 'buy' ? '买入' : trade.side === 'sell' ? '卖出' : '未知';
        const url = safeHttpsUrl(trade.sourceUrl) || transactionUrl(trade.transactionHash);
        const time = escapeHtml(formatDateTime(trade.timestamp));
        return `<tr>
          <td title="${time}">${url ? `<a class="trend-source-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${time}</a>` : time}</td>
          <td class="${trade.side === 'buy' ? 'trend-side-buy' : trade.side === 'sell' ? 'trend-side-sell' : ''}">${side}</td>
          <td>${escapeHtml(formatUsd(trade.priceUsd))}</td>
          <td>${escapeHtml(formatNumber(trade.amount))}</td>
          <td>${escapeHtml(formatUsd(trade.valueUsd))}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
}

function renderTradesError(error) {
  const wrap = document.getElementById('trend-trades-wrap');
  if (wrap) wrap.innerHTML = `<div class="trend-error">${escapeHtml(errorText(error))}</div>`;
}

async function loadRisk(version, firstLoad = false) {
  const { assetId } = currentIds();
  if (!assetId) return;
  const loadingKey = `risk:${version}:${assetId}`;
  if (!state.active || !state.selection || state.loading.has(loadingKey)) return;
  state.loading.add(loadingKey);
  try {
    const data = await api('/api/trend/risk', { params: { assetId }, timeoutMs: 12000 });
    if (!state.active || state.page !== 'detail' || version !== state.selectionVersion) return;
    state.risk = data;
    renderRisk(data);
  } catch (error) {
    if (!state.active || state.page !== 'detail' || version !== state.selectionVersion) return;
    if (!state.risk || firstLoad) renderRiskError(error);
  } finally {
    state.loading.delete(loadingKey);
  }
}

function renderRisk(data, loading = false) {
  const card = document.getElementById('trend-risk-card');
  if (!card) return;
  if (loading) {
    card.innerHTML = '<div class="trend-empty">正在检查…</div>';
    return;
  }
  if (!data) {
    card.innerHTML = '<div class="trend-watch-empty">选择 EVM 合约代币后显示第三方风险信号。</div>';
    return;
  }
  const risk = data.risk || {};
  const flags = risk.flags || [];
  card.innerHTML = `
    <div class="trend-risk-summary"><span class="trend-risk-label">${escapeHtml(risk.label || '暂不可用')}</span><span class="trend-risk-level ${escapeHtml(risk.level || 'unknown')}">${escapeHtml(risk.level || 'unknown')}</span></div>
    ${flags.length ? `<div class="trend-risk-flags">${flags.map(flag => `<span class="trend-risk-flag">${escapeHtml(flag.label)}</span>`).join('')}</div>` : ''}
    <div class="trend-risk-note">${escapeHtml(risk.disclaimer || '第三方风险检测不构成安全认证或投资建议。')}<br>${escapeHtml(partialErrors(data.meta)[0] || `更新 ${formatAge(data.meta?.retrievedAt)}`)}</div>`;
}

function renderRiskError(error) {
  const card = document.getElementById('trend-risk-card');
  if (card) card.innerHTML = `<div class="trend-error">${escapeHtml(errorText(error))}</div>`;
}

function persistWatchSort() {
  try {
    localStorage.setItem(WATCH_SORT_STORAGE_KEY, JSON.stringify(state.watchSort));
  } catch (_) {
    // localStorage may be disabled; sorting still works for the current page session.
  }
}

function sortableWatchValue(item, sortBy) {
  if (sortBy === 'addedAt') return parseTimestampMs(item.addedAt);
  return finiteResultMetric(item.quote?.[sortBy]);
}

function getSortedWatchlist() {
  const indexed = state.watchlist.map((item, index) => ({ item, index }));
  const direction = state.watchSort.sortDir === 'asc' ? 1 : -1;
  return indexed.sort((left, right) => {
    const leftValue = sortableWatchValue(left.item, state.watchSort.sortBy);
    const rightValue = sortableWatchValue(right.item, state.watchSort.sortBy);
    if (leftValue === null && rightValue === null) return left.index - right.index;
    if (leftValue === null) return 1;
    if (rightValue === null) return -1;
    const difference = (leftValue - rightValue) * direction;
    return difference || left.index - right.index;
  });
}

function syncWatchSortControls() {
  const select = document.getElementById('trend-watch-sort-by');
  if (select) {
    select.value = state.watchSort.sortBy;
    select.disabled = !state.watchlist.length;
  }
  const direction = document.getElementById('trend-watch-sort-direction');
  if (direction) {
    direction.disabled = !state.watchlist.length;
    if (state.watchSort.sortBy === 'addedAt') {
      direction.textContent = state.watchSort.sortDir === 'desc' ? '↓ 最近收藏' : '↑ 最早收藏';
      direction.title = '切换收藏添加时间排序方向';
    } else {
      direction.textContent = state.watchSort.sortDir === 'desc' ? '↓ 从高到低' : '↑ 从低到高';
      direction.title = `切换${WATCH_SORT_LABELS[state.watchSort.sortBy] || '数值'}排序方向`;
    }
  }
}

function updateWatchSort(patch) {
  state.watchSort = normalizeWatchSort({ ...state.watchSort, ...patch });
  state.watchPage = 0;
  persistWatchSort();
  syncWatchSortControls();
  renderWatchlist();
}

async function loadWatchlist() {
  if (!state.active) return [];
  try {
    const data = await api('/api/trend/watchlist');
    state.watchlist = data.items || [];
    const maxPage = Math.max(0, Math.ceil(state.watchlist.length / WATCH_PAGE_SIZE) - 1);
    state.watchPage = Math.min(state.watchPage, maxPage);
    renderWatchlist();
    if (state.detail) renderDetail(state.detail);
    return state.watchlist;
  } catch (_) {
    const list = document.getElementById('trend-watch-list');
    if (list) list.innerHTML = '<div class="trend-error" style="grid-column:1/-1">收藏列表暂时无法读取</div>';
    return [];
  }
}

function renderWatchlist() {
  const list = document.getElementById('trend-watch-list');
  const count = document.getElementById('trend-watch-count');
  const tabCount = document.getElementById('trend-watch-tab-count');
  if (count) count.textContent = String(state.watchlist.length);
  if (tabCount) tabCount.textContent = String(state.watchlist.length);
  syncWatchSortControls();
  if (!list) return;
  if (!state.watchlist.length) {
    list.innerHTML = '<div class="trend-watch-empty trend-watch-empty-page"><strong>还没有收藏代币</strong><span>去市场总览搜索币名、Symbol 或合约地址，点击星标即可收集到这里。</span><button type="button" data-action="switch-page" data-page="overview">去市场搜索</button></div>';
    document.getElementById('trend-watch-pagination').innerHTML = '';
    const summary = document.getElementById('trend-watch-summary');
    if (summary) summary.textContent = '收藏保存在本机，不保存任何 API Key';
    return;
  }
  const offset = state.watchPage * WATCH_PAGE_SIZE;
  const pageItems = getSortedWatchlist().slice(offset, offset + WATCH_PAGE_SIZE);
  list.innerHTML = pageItems.map(({ item, index }) => {
    const quote = item.quote || {};
    const change = Number(quote.change24h);
    const changeClass = Number.isFinite(change) ? (change > 0 ? 'up' : change < 0 ? 'down' : 'flat') : 'flat';
    const logo = tokenLogoHtml(item.logoUrl);
    return `
      <article class="trend-favorite-card">
        <button class="trend-favorite-open" type="button" data-action="open-watch" data-index="${index}">
          <span class="trend-favorite-top${logo ? ' has-token-logo' : ''}">
            ${logo}
            <span class="trend-favorite-identity"><strong>${escapeHtml(item.symbol || '?')}</strong><small>${escapeHtml(item.nameZh || item.name || '')}</small></span>
            <span class="trend-price-change ${changeClass}">${escapeHtml(formatPercent(quote.change24h))}</span>
          </span>
          <span class="trend-favorite-price">${escapeHtml(formatUsd(quote.priceUsd))}</span>
          <span class="trend-favorite-metrics">
            <span><small>24h 成交额</small><strong>${escapeHtml(formatUsd(quote.volume24hUsd, true))}</strong></span>
            <span><small>流动性</small><strong>${escapeHtml(formatUsd(quote.liquidityUsd, true))}</strong></span>
            <span><small>市值</small><strong>${escapeHtml(formatUsd(quote.marketCapUsd, true))}</strong></span>
          </span>
        </button>
        <button class="trend-favorite-remove" type="button" data-action="remove-watch" data-index="${index}" aria-label="删除收藏 ${escapeHtml(item.symbol || '')}" title="从我的收藏移除">×</button>
      </article>`;
  }).join('');
  renderWatchPagination();
  const summary = document.getElementById('trend-watch-summary');
  if (summary) {
    const pages = Math.ceil(state.watchlist.length / WATCH_PAGE_SIZE);
    summary.textContent = state.loading.has('watch-quotes')
      ? `正在刷新第 ${state.watchPage + 1} / ${pages} 页行情…`
      : `共 ${state.watchlist.length} 个收藏 · 第 ${state.watchPage + 1} / ${pages} 页 · ${WATCH_SORT_LABELS[state.watchSort.sortBy]}${state.watchRefreshAt ? ` · 更新 ${formatAge(state.watchRefreshAt)}` : ''}`;
  }
}

function renderWatchPagination() {
  const pagination = document.getElementById('trend-watch-pagination');
  if (!pagination) return;
  const pages = Math.ceil(state.watchlist.length / WATCH_PAGE_SIZE);
  if (pages <= 1) {
    pagination.innerHTML = '';
    return;
  }
  pagination.innerHTML = Array.from({ length: pages }, (_, page) => `
    <button class="${page === state.watchPage ? 'active' : ''}" type="button" data-action="watch-page" data-page-index="${page}" aria-label="收藏第 ${page + 1} 页">${page + 1}</button>`).join('');
}

function setWatchPage(page) {
  const maxPage = Math.max(0, Math.ceil(state.watchlist.length / WATCH_PAGE_SIZE) - 1);
  if (!Number.isInteger(page)) return;
  state.watchPage = Math.max(0, Math.min(page, maxPage));
  renderWatchlist();
  refreshWatchlistQuotes();
}

async function refreshWatchlistQuotes(force = false) {
  if (!state.active || state.page !== 'watchlist' || state.loading.has('watch-quotes')) return;
  const offset = state.watchPage * WATCH_PAGE_SIZE;
  const pageItems = getSortedWatchlist().slice(offset, offset + WATCH_PAGE_SIZE).map(({ item }) => item);
  if (!pageItems.length) return;
  state.loading.add('watch-quotes');
  const refreshButton = root.querySelector('[data-action="refresh-watch"]');
  if (refreshButton) { refreshButton.disabled = true; refreshButton.textContent = '刷新中…'; }
  renderWatchlist();
  let cursor = 0;
  const worker = async () => {
    while (cursor < pageItems.length) {
      const item = pageItems[cursor++];
      try {
        const data = await api('/api/trend/asset', { params: { assetId: item.assetId, marketId: item.marketId }, timeoutMs: force ? 18000 : 15000 });
        const current = state.watchlist.find(entry => entry.targetId === item.targetId);
        if (!current) continue;
        current.quote = data.quote || current.quote || {};
        current.logoUrl = data.asset?.logoUrl || current.logoUrl || null;
        current.quoteMeta = { ...(data.meta || {}), provider: data.meta?.provider || item.provider, lastError: null };
        current.marketLabel = `${data.market?.venue || data.market?.type || ''} · ${data.market?.pair || ''}`;
      } catch (error) {
        const current = state.watchlist.find(entry => entry.targetId === item.targetId);
        if (!current) continue;
        current.quoteMeta = {
          ...(current.quoteMeta || {}),
          provider: current.quoteMeta?.provider || item.provider,
          metricStatus: current.quote?.priceUsd != null ? 'partial' : 'unavailable',
          lastError: errorText(error),
        };
      }
    }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(3, pageItems.length) }, () => worker()));
    if (state.page === 'watchlist') state.watchRefreshAt = new Date().toISOString();
  } finally {
    state.loading.delete('watch-quotes');
    if (refreshButton) { refreshButton.disabled = false; refreshButton.textContent = '刷新行情'; }
    renderWatchlist();
    loadCapabilities(true);
  }
}

function refreshFavoriteIndicators() {
  renderWatchlist();
  if (state.results.length) renderSearchResults({ meta: state.searchMeta });
  const asset = state.detail?.asset || state.selection?.asset;
  const market = state.detail?.market || state.selection?.market;
  if (asset && market) {
    renderAssetHero(asset, market, state.detail?.quote || state.selection?.quote || {}, state.detail?.meta?.metricStatus || state.selection?.metricStatus, state.detail?.meta);
  }
}

async function saveWatchTarget(asset, market, quote = {}, provider = '', metricStatus = 'partial', meta = null) {
  if (!asset?.assetId || !market?.marketId) return;
  try {
    const data = await api('/api/trend/watchlist', {
      method: 'POST',
      body: {
        assetId: asset.assetId,
        marketId: market.marketId,
        symbol: asset.symbol,
        name: asset.name,
        nameZh: asset.nameZh,
        logoUrl: asset.logoUrl || null,
        chainId: asset.chainId,
        marketLabel: `${market.venue || market.type || ''} · ${market.pair || ''}`,
        provider,
        quote,
        quoteMeta: {
          provider,
          retrievedAt: meta?.retrievedAt || new Date().toISOString(),
          metricStatus,
        },
      },
    });
    state.watchlist = data.items || state.watchlist;
    state.watchPage = 0;
    refreshFavoriteIndicators();
  } catch (error) {
    setRuntime('degraded', errorText(error));
  }
}

async function toggleCurrentWatch() {
  const asset = state.detail?.asset || state.selection?.asset;
  const market = state.detail?.market || state.selection?.market;
  if (!asset?.assetId || !market?.marketId) return;
  const targetId = `${asset.assetId}|${market.marketId}`;
  const existingIndex = state.watchlist.findIndex(item => item.targetId === targetId);
  if (existingIndex >= 0) {
    await removeWatch(existingIndex);
    return;
  }
  await saveWatchTarget(
    asset,
    market,
    state.detail?.quote || state.selection?.quote || {},
    state.detail?.meta?.provider || state.selection?.source?.provider || '',
    state.detail?.meta?.metricStatus || state.selection?.metricStatus || 'partial',
    state.detail?.meta,
  );
}

async function toggleResultWatch(index) {
  const result = state.results[index];
  if (!result?.asset?.assetId || !result?.market?.marketId) return;
  const targetId = `${result.asset.assetId}|${result.market.marketId}`;
  const existingIndex = state.watchlist.findIndex(item => item.targetId === targetId);
  if (existingIndex >= 0) {
    await removeWatch(existingIndex);
    return;
  }
  await saveWatchTarget(result.asset, result.market, result.quote || {}, result.source?.provider || '', result.metricStatus || 'partial', result.meta);
}

async function removeWatch(index) {
  const item = state.watchlist[index];
  if (!item) return;
  try {
    const data = await api('/api/trend/watchlist', { method: 'DELETE', params: { targetId: item.targetId } });
    state.watchlist = data.items || [];
    const maxPage = Math.max(0, Math.ceil(state.watchlist.length / WATCH_PAGE_SIZE) - 1);
    state.watchPage = Math.min(state.watchPage, maxPage);
    refreshFavoriteIndicators();
  } catch (error) {
    setRuntime('degraded', errorText(error));
  }
}

function openWatch(index) {
  const item = state.watchlist[index];
  if (!item) return;
  abortRequests();
  state.selection = {
    asset: { assetId: item.assetId, symbol: item.symbol, name: item.name, nameZh: item.nameZh, logoUrl: item.logoUrl || null, chainId: item.chainId, address: item.assetId?.startsWith('token:') ? item.assetId.split(':').slice(2).join(':') : null },
    market: { marketId: item.marketId, venue: item.marketLabel, pair: item.marketLabel },
    quote: item.quote || {},
    source: { provider: item.provider, label: providerLabel(item.provider) },
    metricStatus: 'partial',
  };
  state.selectionVersion += 1;
  state.detail = null;
  state.ohlcv = null;
  state.flows = null;
  state.trades = null;
  state.risk = null;
  state.detailOrigin = 'watchlist';
  setTrendPage('detail');
  renderSelectionPreview(state.selection);
  renderDetailLoading();
  const version = state.selectionVersion;
  loadDetail(version, true);
  loadOhlcv(version, true);
  loadFlows(version, true);
  loadTrades(version, true);
  loadRisk(version, true);
  restartPollers();
  document.querySelector('[data-trend-page="detail"]')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderProviders() {
  const list = document.getElementById('trend-provider-list');
  if (!list) return;
  const providers = state.capabilities?.providers || [];
  if (!providers.length) {
    list.innerHTML = '<div class="trend-watch-empty">正在读取状态…</div>';
    return;
  }
  list.innerHTML = providers.map(provider => {
    const status = provider.status || 'unknown';
    const title = provider.lastError || (provider.lastSuccessAt ? `最近成功 ${formatAge(provider.lastSuccessAt)}` : '尚未请求');
    return `<div class="trend-provider-row" title="${escapeHtml(title)}"><span class="trend-provider-name">${escapeHtml(provider.label)}${provider.auth === 'optional' ? ' · Key 可选' : ''}</span><span class="trend-provider-state ${escapeHtml(status)}">${escapeHtml(status === 'healthy' ? '正常' : status === 'degraded' ? '降级' : '未请求')}</span></div>`;
  }).join('');
}

function updateRuntimeStatus() {
  const providers = state.capabilities?.providers || [];
  const healthy = providers.filter(provider => provider.status === 'healthy').length;
  const degraded = providers.filter(provider => provider.status === 'degraded').length;
  if (degraded) setRuntime('degraded', `${healthy} 正常 · ${degraded} 降级，已保留部分数据`);
  else if (healthy) setRuntime('healthy', `${healthy} 个数据源已连接 · 页面打开时刷新`);
  else setRuntime('unknown', '数据源按需连接');
}

function setRuntime(status, message) {
  const runtime = document.getElementById('trend-runtime');
  const text = document.getElementById('trend-runtime-text');
  if (runtime) runtime.dataset.state = status;
  if (text) text.textContent = message;
}

function markMetaError(id, error) {
  const element = document.getElementById(id);
  if (!element) return;
  const retained = element.innerHTML;
  element.innerHTML = `${retained}${statusBadge('partial')}<span>${escapeHtml(errorText(error))}，保留上次数据</span>`;
}

function schedule(callback, milliseconds) {
  const id = window.setInterval(() => {
    if (state.active && !document.hidden) callback();
  }, milliseconds);
  state.timers.add(id);
}

function stopPollers() {
  state.timers.forEach(id => window.clearInterval(id));
  state.timers.clear();
}

function restartPollers() {
  stopPollers();
  if (!state.active || document.hidden) return;
  if (state.page === 'overview') {
    schedule(loadOverview, 300000);
    return;
  }
  if (state.page === 'watchlist') {
    schedule(refreshWatchlistQuotes, 60000);
    return;
  }
  if (state.page !== 'detail' || !state.selection) return;
  schedule(() => loadDetail(state.selectionVersion), 15000);
  schedule(() => {
    loadFlows(state.selectionVersion);
    loadTrades(state.selectionVersion);
  }, 30000);
  schedule(() => loadOhlcv(state.selectionVersion), 60000);
}

function handleVisibility() {
  if (!state.active) return;
  if (document.hidden) {
    stopPollers();
    abortRequests();
  } else {
    restartPollers();
    if (state.page === 'overview') loadOverview();
    else if (state.page === 'watchlist') loadWatchlist().then(() => refreshWatchlistQuotes());
    else if (state.page === 'detail' && state.selection) {
      loadDetail(state.selectionVersion);
      loadFlows(state.selectionVersion);
      loadTrades(state.selectionVersion);
      loadOhlcv(state.selectionVersion);
    }
  }
}

async function activate() {
  if (!root) return;
  state.active = true;
  normalizeChartSelection();
  if (!state.initialized) {
    renderShell();
    state.initialized = true;
  }
  setRuntime('unknown', '正在读取数据源状态');
  restartPollers();
  await Promise.allSettled([loadCapabilities(), loadOverview(), loadWatchlist()]);
  if (!state.active) return;
  if (state.page === 'watchlist') refreshWatchlistQuotes();
  restartPollers();
  if (state.chart) {
    resizeChart();
  }
}

function deactivate() {
  state.active = false;
  stopPollers();
  abortRequests();
  setRuntime('unknown', '已暂停刷新');
}

window.SeegentTrend = { activate, deactivate };

// 模块脚本是 defer 的；若主页已在恢复「趋势」，主动补一次激活。
if (root?.classList.contains('active') || localStorage.getItem('seegent-module') === 'trend') {
  activate();
}
