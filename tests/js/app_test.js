// node tests/js/app_test.js：site/app.js 純函式單元測試（由 tests/test_site.py 呼叫）
'use strict';
const assert = require('assert');
const path = require('path');
const SW20 = require(path.join(__dirname, '..', '..', 'site', 'app.js'));

const rows = [
  { stock_id: '2330', stock_name: '台積電', asset_type: 'common_stock', industry: '半導體業／電子工業', is_volume_spike: false, score: 45,
    signals: { trendline_break: true, w_bottom: false, w20_slope_up: true, d20_cross_w20: false, sector_leader: true },
    sector_label: '族群共振（同業 4 檔）', box_label: null, warnings: ['處置股', '近45週曾減資/分割', '距週 20MA 較遠'] },
  { stock_id: '2317', stock_name: '鴻海', asset_type: 'common_stock', industry: '其他電子業', is_volume_spike: true, score: 60,
    signals: {}, warnings: ['注意股'] },
  { stock_id: '6949', stock_name: '沛爾生醫-創', asset_type: 'common_stock', industry: null, is_volume_spike: true, score: null,
    signals: { w_bottom: true }, box_label: '箱型橫盤中，宜確認突破有效性', warnings: [] },
];
const etfs = [
  { stock_id: '0050', stock_name: '元大台灣50', etf_subtype: 'equity_regular', etf_zone: 'new_cross' },
  { stock_id: '00679B', stock_name: '元大美債20年', etf_subtype: 'bond', etf_zone: 'above_and_spike' },
  { stock_id: '00631L', stock_name: '元大台灣50正2', etf_subtype: 'leveraged_inverse', etf_zone: null },
  { stock_id: '00400A', stock_name: '主動國泰動能高息', etf_subtype: 'active', etf_zone: null },
];
const ids = (rs) => rs.map((r) => r.stock_id);

// 頁 1「僅看同時爆量」預設不勾選；勾選後只剩 is_volume_spike
let st = SW20.defaultState();
assert.strictEqual(st.spikeOnly, false);
assert.strictEqual(st.showFund, false);                              // 參考欄預設收合
assert.deepStrictEqual(ids(SW20.filterRows(rows, st)), ['2330', '2317', '6949']);
assert.deepStrictEqual(ids(SW20.filterRows(rows, { ...st, spikeOnly: true })), ['2317', '6949']);
// 出場分頁不套用爆量勾選
assert.strictEqual(SW20.filterRows(rows, { ...st, tab: 'exit_alerts', spikeOnly: true }).length, 3);
// 搜尋與產業（多類別以「／」分隔）
assert.deepStrictEqual(ids(SW20.filterRows(rows, { ...st, q: '鴻' })), ['2317']);
assert.deepStrictEqual(ids(SW20.filterRows(rows, { ...st, q: '233' })), ['2330']);
assert.deepStrictEqual(ids(SW20.filterRows(rows, { ...st, industry: '電子工業' })), ['2330']);
assert.deepStrictEqual(SW20.industryOptions(rows), ['半導體業', '其他電子業', '電子工業'].sort((a, b) => a.localeCompare(b, 'zh-Hant')));

// ETF：預設隱藏債券與槓反、預設只看兩區；一鍵展開全部子類型
const e = { ...st, tab: 'etf' };
assert.deepStrictEqual(ids(SW20.filterRows(etfs, e)), ['0050']);
assert.deepStrictEqual(ids(SW20.filterRows(etfs, { ...e, etfZone: 'all' })), ['0050', '00400A']);
assert.deepStrictEqual(ids(SW20.filterRows(etfs, { ...e, etfZone: 'all', etfSubtypes: ['equity_regular', 'active', 'bond', 'leveraged_inverse'] })),
  ['0050', '00679B', '00631L', '00400A']);
assert.deepStrictEqual(ids(SW20.filterRows(etfs, { ...e, etfZone: 'above_and_spike', etfSubtypes: ['bond'] })), ['00679B']);

// 標籤與警示徽章
assert.deepStrictEqual(SW20.tagsFor(rows[0]), ['切線突破', '均線翻揚', '族群共振（同業 4 檔）', '族群龍頭']);
assert.deepStrictEqual(SW20.tagsFor(rows[2]), ['W底', '箱型橫盤中，宜確認突破有效性']);
assert.deepStrictEqual(SW20.warningsFor(rows[0], { is_week_settled: true }), ['處置', '減資/分割', '距週 20MA 較遠']);
assert.deepStrictEqual(SW20.warningsFor(rows[1], { is_week_settled: false, trading_days_in_current_week: 3 }),
  ['注意', '週中未結算（已交易 3 天）']);

// 欄位：週中才有推算滿週倍數；參考欄開關；ETF 無股本欄；不得有資金／張數／停損欄
const L = { is_week_settled: false };
const labels = (tab, s) => SW20.columns(tab, L, s).map((c) => c.label);
assert.ok(labels('new_cross', st).includes('推算滿週倍數'));
assert.ok(!SW20.columns('new_cross', { is_week_settled: true }, st).map((c) => c.label).includes('推算滿週倍數'));
assert.ok(!labels('new_cross', st).includes('最新季 EPS'));
assert.ok(labels('new_cross', { ...st, showFund: true }).includes('最新季 EPS'));
assert.ok(!labels('etf', { ...st, showFund: true }).includes('股本（億）'));
assert.ok(!labels('etf', { ...st, showFund: true }).includes('最新季 EPS'));
assert.ok(labels('exit_alerts', st).includes('出場狀態'));
for (const t of ['new_cross', 'above_and_spike', 'etf', 'exit_alerts']) {
  for (const lab of labels(t, { ...st, showFund: true })) {
    assert.ok(!/張數|資金|停損/.test(lab), lab);
  }
}

// 排序：空值排最後
const col = SW20.columns('new_cross', L, st).find((c) => c.key === 'score');
assert.deepStrictEqual(ids(SW20.sortRows(rows, col, -1)), ['2317', '2330', '6949']);
assert.deepStrictEqual(ids(SW20.sortRows(rows, col, 1)), ['2330', '2317', '6949']);

// 格式：張數顯示時四捨五入到整張；產生時間
assert.strictEqual(SW20.fmtLots(1234.5), '1,235');
assert.strictEqual(SW20.fmtLots(null), '—');
assert.strictEqual(SW20.generatedText('2026-11-25T18:05:12+08:00'), '2026-11-25 18:05 Asia/Taipei');

// 週 K 圖：K 線、週 20MA、成交量柱、前 20 週均量虛線
const calls = [];
const fakeLW = { LineStyle: { Dashed: 2 }, createChart() {
  const s = (k) => ({ setData: (d) => calls.push(`${k}:${d.length}`), createPriceLine: (p) => calls.push(`line:${p.price}:${p.lineStyle}`) });
  return { addCandlestickSeries: () => s('candle'), addLineSeries: () => s('ma'), addHistogramSeries: () => s('vol'),
           priceScale: () => ({ applyOptions() {} }), timeScale: () => ({ fitContent() {} }) };
} };
const bars = [
  { week_end_date: '2026-11-13', open: 1, high: 2, low: 0.5, close: 1.5, volume_lots: 10.4, w20ma: null, vavg_20w_lots: null },
  { week_end_date: '2026-11-20', open: 1.5, high: 2, low: 1, close: 1.2, volume_lots: 20.6, w20ma: 1.3, vavg_20w_lots: 15 },
];
const el = { textContent: '' };
assert.ok(SW20.renderChart(el, bars, fakeLW));
assert.deepStrictEqual(calls, ['candle:2', 'ma:1', 'vol:2', 'line:15:2']);
const el2 = { textContent: '' };
SW20.renderChart(el2, bars, null);
assert.ok(/未載入/.test(el2.textContent));

// 單列高度一致（M5b）：徽章欄單行截斷，完整內容放 title
const cell = SW20.clipCell(['切線突破', '族群共振（同業 4 檔）', '<x>'], 'tag');
assert.ok(cell.startsWith('<td class="clipcell" title="切線突破、族群共振（同業 4 檔）、&lt;x&gt;"><div class="clip">'), cell);
assert.ok(!/class="wrap"/.test(cell));
const tagCol = SW20.columns('new_cross', L, st).find((c) => c.key === 'tags');
assert.ok(/class="clipcell"/.test(tagCol.html(rows[0])));
const exitCol = SW20.columns('exit_alerts', L, st).find((c) => c.key === 'exit_status');
assert.ok(/class="clipcell l" title="週中/.test(exitCol.html({ exit_action_note: '週中收盤低於暫定週 20MA' })));

console.log('app_test: OK');
