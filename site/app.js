/* 林恩如週 20MA 選股系統：靜態前端（spec_v1 第 7 節）。純 Vanilla JS，無需 build。
 * 資料：data/latest.json（http(s) 下以 fetch 讀取）；以 file:// 直接開啟時改載入 data/latest.js。
 * 純函式掛在 SW20 物件上（Node 也可 require 做單元測試），DOM 相關只在瀏覽器執行。
 */
(function (root) {
  'use strict';

  var SW20 = {};
  var STOCK_TABS = ['new_cross', 'above_and_spike', 'exit_alerts'];
  var ETF_DEFAULT = ['equity_regular', 'active'];          // 預設隱藏 bond、leveraged_inverse（第 7 節）
  var ETF_SUBTYPE_ZH = { equity_regular: '原型股票/商品', bond: '債券', leveraged_inverse: '槓桿/反向', active: '主動式' };
  var ETF_ZONE_ZH = { new_cross: '新站上週 20MA', above_and_spike: '已站上且本週爆量' };
  var LIGHT_EMOJI = { RED: '🔴', YELLOW: '🟡', GREEN: '🟢' };
  var WARN_SHORT = { '處置股': '處置', '注意股': '注意', '近45週曾減資/分割': '減資/分割' };
  var CHART_HEIGHT = 380;

  // ---------------------------------------------------------------- 格式化

  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function fmt(v, d) {
    if (!isNum(v)) return '—';
    return v.toLocaleString('zh-TW', { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function fmtLots(v) { return isNum(v) ? Math.round(v).toLocaleString('zh-TW') : '—'; }   // 顯示時才四捨五入到整張（M3-Q5）
  function fmtRatio(v) { return isNum(v) ? fmt(v, 2) + 'x' : '—'; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function signCls(v) { return !isNum(v) ? '' : (v > 0 ? 'pos' : (v < 0 ? 'neg' : '')); }
  function generatedText(iso) {      // "2026-11-25T18:05:12+08:00" → "2026-11-25 18:05 Asia/Taipei"
    if (!iso) return '—';
    return String(iso).slice(0, 10) + ' ' + String(iso).slice(11, 16) + ' Asia/Taipei';
  }
  SW20.fmt = fmt; SW20.fmtLots = fmtLots; SW20.generatedText = generatedText;

  // ---------------------------------------------------------------- 標籤與警示

  function tagsFor(r) {
    var s = r.signals || {};
    var out = [];
    if (s.trendline_break) out.push('切線突破');
    if (s.w_bottom) out.push('W底');
    if (s.w20_slope_up) out.push('均線翻揚');
    if (s.d20_cross_w20) out.push('日20上穿');
    if (r.sector_label) out.push(r.sector_label);
    if (s.sector_leader) out.push('族群龍頭');
    if (r.box_label) out.push(r.box_label);
    return out;
  }

  function warningsFor(r, latest) {
    var out = (r.warnings || []).map(function (w) { return WARN_SHORT[w] || w; });
    if (latest && latest.is_week_settled === false) {
      out.push('週中未結算（已交易 ' + latest.trading_days_in_current_week + ' 天）');
    }
    return out;
  }
  SW20.tagsFor = tagsFor; SW20.warningsFor = warningsFor;

  // ---------------------------------------------------------------- 篩選與排序

  function defaultState() {
    return { tab: 'new_cross', q: '', industry: '', spikeOnly: false, showFund: false,
             etfSubtypes: ETF_DEFAULT.slice(), etfZone: 'signal', sortKey: null, sortDir: -1, openCode: null };
  }

  function filterRows(rows, state) {
    var q = (state.q || '').trim().toLowerCase();
    return rows.filter(function (r) {
      if (q && String(r.stock_id).toLowerCase().indexOf(q) < 0 && String(r.stock_name || '').toLowerCase().indexOf(q) < 0) return false;
      if (state.tab === 'etf') {
        if (state.etfSubtypes.indexOf(r.etf_subtype) < 0) return false;
        if (state.etfZone === 'signal' && !r.etf_zone) return false;
        if ((state.etfZone === 'new_cross' || state.etfZone === 'above_and_spike') && r.etf_zone !== state.etfZone) return false;
        return true;
      }
      if (state.industry && String(r.industry || '').split('／').indexOf(state.industry) < 0) return false;
      if (state.spikeOnly && (state.tab === 'new_cross' || state.tab === 'above_and_spike') && !r.is_volume_spike) return false;
      return true;
    });
  }

  function industryOptions(rows) {
    var set = {};
    rows.forEach(function (r) {
      if (r.asset_type !== 'common_stock' || !r.industry) return;
      String(r.industry).split('／').forEach(function (x) { if (x) set[x] = true; });
    });
    return Object.keys(set).sort(function (a, b) { return a.localeCompare(b, 'zh-Hant'); });
  }

  function sortRows(rows, col, dir) {
    if (!col) return rows.slice();
    return rows.slice().sort(function (a, b) {
      var x = col.sort(a), y = col.sort(b);
      var xn = x === null || x === undefined || (typeof x === 'number' && !isFinite(x));
      var yn = y === null || y === undefined || (typeof y === 'number' && !isFinite(y));
      if (xn && yn) return 0;
      if (xn) return 1;                // 空值一律排最後
      if (yn) return -1;
      if (typeof x === 'string') return dir * x.localeCompare(y, 'zh-Hant');
      return dir * (x - y);
    });
  }
  SW20.defaultState = defaultState; SW20.filterRows = filterRows; SW20.industryOptions = industryOptions; SW20.sortRows = sortRows;

  // ---------------------------------------------------------------- 欄位

  function num(key, label, d, kind) {
    return {
      key: key, label: label,
      sort: function (r) { return r[key]; },
      html: function (r) {
        var v = r[key];
        var text = kind === 'ratio' ? fmtRatio(v) : (kind === 'lots' ? fmtLots(v) : fmt(v, d));
        return '<td class="' + (kind === 'signed' ? signCls(v) : '') + '">' + text + '</td>';
      }
    };
  }

  function columns(tab, latest, state) {
    var settled = latest.is_week_settled;
    var cols = [{
      key: 'stock_id', label: '代碼／名稱', cls: 'l',
      sort: function (r) { return r.stock_id; },
      html: function (r) { return '<td class="l"><span class="code">' + esc(r.stock_id) + '</span> ' + esc(r.stock_name) + '</td>'; }
    }, {
      key: 'market', label: '市場', cls: 'l',
      sort: function (r) { return r.market; },
      html: function (r) { return '<td class="l">' + (r.market === 'twse' ? '上市' : '上櫃') + '</td>'; }
    }];
    if (tab === 'exit_alerts') {
      cols.push({ key: 'exit_status', label: '出場狀態', cls: 'l', sort: function (r) { return r.exit_status; },
        html: function (r) { return '<td class="l">' + esc(r.exit_action_note) + '</td>'; } });
    }
    if (tab === 'etf') {
      cols.push({ key: 'etf_subtype', label: '子類型', cls: 'l', sort: function (r) { return r.etf_subtype; },
        html: function (r) { return '<td class="l"><span class="badge sub">' + esc(ETF_SUBTYPE_ZH[r.etf_subtype] || r.etf_subtype) + '</span></td>'; } });
      cols.push({ key: 'etf_zone', label: '區別', cls: 'l', sort: function (r) { return r.etf_zone || ''; },
        html: function (r) { return '<td class="l">' + esc(ETF_ZONE_ZH[r.etf_zone] || '—') + '</td>'; } });
    } else {
      cols.push({ key: 'is_tib', label: '創新板', cls: 'l', sort: function (r) { return r.is_tib ? 1 : 0; },
        html: function (r) { return '<td class="l">' + (r.is_tib ? '<span class="badge tag">創新板</span>' : '') + '</td>'; } });
    }
    cols.push(num('close_raw', '原始現價', 2));
    cols.push(num('w20ma', '週20MA（還原）', 2));
    cols.push(num('d100ma', '日100MA（對照）', 2));
    cols.push(num('ma_diff', '均差距（元）', 2, 'signed'));
    cols.push(num('bias_20w_pct', '乖離率（%）', 2, 'signed'));
    cols.push(num('vol_ratio_20w', '爆量倍數（20週均）', 2, 'ratio'));
    cols.push(num('vol_ratio_prev_week', '較上週倍數', 2, 'ratio'));
    if (settled === false) cols.push(num('vol_ratio_20w_projected', '推算滿週倍數', 2, 'ratio'));
    cols.push(num('avg_daily_lots_20w', '週均量（張/日）', 0, 'lots'));
    if (tab !== 'etf') {
      cols.push(num('capital_billion_ntd', '股本（億）', 1));
      cols.push(num('market_cap_billion_ntd', '參考市值（億）', 1));
    }
    cols.push(num('score', '綜合評分', 0));
    cols.push({ key: 'tags', label: '加分標籤', cls: 'l', sort: function (r) { return tagsFor(r).length; },
      html: function (r) {
        return '<td class="wrap">' + tagsFor(r).map(function (t) { return '<span class="badge tag">' + esc(t) + '</span>'; }).join('') + '</td>';
      } });
    cols.push({ key: 'warnings', label: '警示', cls: 'l', sort: function (r) { return (r.warnings || []).length; },
      html: function (r) {
        return '<td class="wrap">' + warningsFor(r, latest).map(function (t) { return '<span class="badge warn">' + esc(t) + '</span>'; }).join('') + '</td>';
      } });
    if (tab !== 'etf' && state.showFund) {
      var f = function (r) { return r.fundamentals_ref || {}; };
      cols.push({ key: 'eps', label: '最新季 EPS', fund: true, sort: function (r) { return f(r).eps; },
        html: function (r) {
          return '<td class="fund">' + (f(r).eps_quarter ? esc(f(r).eps_quarter) + ' ' : '') + fmt(f(r).eps, 2) + '</td>';
        } });
      cols.push({ key: 'rev', label: '最新月營收 YoY（%）', fund: true, sort: function (r) { return f(r).rev_yoy_pct; },
        html: function (r) {
          return '<td class="fund ' + signCls(f(r).rev_yoy_pct) + '">' + (f(r).rev_month ? esc(f(r).rev_month) + ' ' : '') + fmt(f(r).rev_yoy_pct, 2) + '</td>';
        } });
      cols.push({ key: 'insti', label: '近 5 日三大法人（張）', fund: true, sort: function (r) { return f(r).insti_5d_net_lots; },
        html: function (r) { return '<td class="fund ' + signCls(f(r).insti_5d_net_lots) + '">' + fmtLots(f(r).insti_5d_net_lots) + '</td>'; } });
    }
    return cols;
  }
  SW20.columns = columns;

  // ---------------------------------------------------------------- 週 K 圖

  function renderChart(el, bars, LW) {
    LW = LW || root.LightweightCharts;
    if (!LW) {
      el.textContent = '圖表元件未載入（需連網取得 TradingView Lightweight Charts）。';
      return null;
    }
    if (!bars || !bars.length) {
      el.textContent = '此標的沒有週 K 資料。';
      return null;
    }
    var chart = LW.createChart(el, {
      autoSize: true,
      height: CHART_HEIGHT,
      layout: { background: { color: '#ffffff' }, textColor: '#1f2328' },
      rightPriceScale: { scaleMargins: { top: 0.05, bottom: 0.3 } },
      timeScale: { borderColor: '#d0d7de' },
      grid: { vertLines: { color: '#f0f2f4' }, horzLines: { color: '#f0f2f4' } }
    });
    var candle = chart.addCandlestickSeries({       // 臺股慣例：紅漲綠跌
      upColor: '#d93025', downColor: '#1e8e3e', borderUpColor: '#d93025', borderDownColor: '#1e8e3e',
      wickUpColor: '#d93025', wickDownColor: '#1e8e3e'
    });
    candle.setData(bars.map(function (b) { return { time: b.week_end_date, open: b.open, high: b.high, low: b.low, close: b.close }; }));
    var ma = chart.addLineSeries({ color: '#0969da', lineWidth: 2, priceLineVisible: false, title: '週20MA' });
    ma.setData(bars.filter(function (b) { return isNum(b.w20ma); }).map(function (b) { return { time: b.week_end_date, value: b.w20ma }; }));
    var vol = chart.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
    vol.setData(bars.map(function (b) {
      return { time: b.week_end_date, value: Math.round(b.volume_lots || 0),
               color: b.close >= b.open ? 'rgba(217,48,37,0.45)' : 'rgba(30,142,62,0.45)' };
    }));
    var last = bars[bars.length - 1];
    if (isNum(last.vavg_20w_lots)) {          // 前 20 週均量水平虛線（FR-1.10）
      vol.createPriceLine({ price: last.vavg_20w_lots, color: '#9a6700', lineWidth: 1,
                            lineStyle: (LW.LineStyle && LW.LineStyle.Dashed) || 2, axisLabelVisible: true, title: '前20週均量' });
    }
    chart.timeScale().fitContent();
    return chart;
  }
  SW20.renderChart = renderChart;

  // ---------------------------------------------------------------- DOM

  function App(latest, doc) {
    this.latest = latest;
    this.doc = doc;
    this.state = defaultState();
    this.chart = null;
  }

  App.prototype.$ = function (sel) { return this.doc.querySelector(sel); };
  App.prototype.$$ = function (sel) { return Array.prototype.slice.call(this.doc.querySelectorAll(sel)); };

  App.prototype.renderHeader = function () {
    var L = this.latest;
    var badge = L.is_week_settled
      ? '<span class="badge settled">✅ 週 K 已結算</span>'
      : '<span class="badge midweek">⚠️ 週中未結算（已交易 ' + esc(L.trading_days_in_current_week) + ' 天）</span>';
    this.$('#meta').innerHTML = '<span>基準交易日：' + esc(L.as_of_date) + '</span><span>週別：' + esc(L.week_id) +
      '</span><span>產生時間：' + esc(generatedText(L.generated_at_taipei)) + '</span>' + badge;
    var notes = (L.notes || []).concat(L.errors || []);
    var box = this.$('#notes');
    box.hidden = !notes.length;
    box.innerHTML = notes.length ? '<ul>' + notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>' : '';

    var m = L.market || {};
    var rel = function (v) { return isNum(m.close) && isNum(v) ? (m.close >= v ? '（高於）' : '（低於）') : ''; };
    this.$('#market').innerHTML =
      '<div class="light">' + (LIGHT_EMOJI[m.light] || '⚪') + ' ' + esc(m.light_label_zh || '燈號資料不足') + '</div>' +
      '<div class="vals"><span>' + esc(m.index_name || '加權指數') + '：' + fmt(m.close, 2) + '</span>' +
      '<span>日20MA ' + fmt(m.d20ma, 2) + rel(m.d20ma) + '</span>' +
      '<span>日100MA ' + fmt(m.d100ma, 2) + rel(m.d100ma) + '</span>' +
      '<span>週20MA ' + fmt(m.w20ma, 2) + rel(m.w20ma) + '，乖離率 ' + fmt(m.bias_w20_pct, 2) + '%</span></div>' +
      '<div class="advice">' + esc(m.suggested_exposure || '') + '（燈號不過濾名單）</div>';

    var s = L.summary || {}, tabs = L.tabs || {};
    var counts = { new_cross: s.new_cross_count, above_and_spike: s.above_and_spike_count,
                   etf: (s.etf_signal_count) + '／' + (tabs.etf || []).length, exit_alerts: (tabs.exit_alerts || []).length };
    this.$$('#tabs button').forEach(function (b) { b.querySelector('.count').textContent = '（' + counts[b.dataset.tab] + '）'; });
    this.$('#spikeRatio').textContent = (L.params && L.params.volume_spike_ratio) || 1.5;
  };

  App.prototype.rows = function () { return (this.latest.tabs || {})[this.state.tab] || []; };

  App.prototype.renderControls = function () {
    var st = this.state, isEtf = st.tab === 'etf';
    this.$$('[data-for="stock"]').forEach(function (el) { el.hidden = isEtf; });
    this.$$('[data-for="etf"]').forEach(function (el) { el.hidden = !isEtf; });
    this.$('#spikeOnly').closest('label').hidden = isEtf || st.tab === 'exit_alerts';
    var sel = this.$('#industry');
    var opts = industryOptions(this.rows());
    if (opts.indexOf(st.industry) < 0) st.industry = '';
    sel.innerHTML = '<option value="">全部</option>' + opts.map(function (o) {
      return '<option' + (o === st.industry ? ' selected' : '') + '>' + esc(o) + '</option>';
    }).join('');
    this.$('#csv').setAttribute('href', 'data/' + st.tab + '.csv');
    this.$$('#etfFilters input').forEach(function (cb) { cb.checked = st.etfSubtypes.indexOf(cb.value) >= 0; });
    this.$('#etfZone').value = st.etfZone;
    this.$$('#tabs button').forEach(function (b) { b.classList.toggle('active', b.dataset.tab === st.tab); });
  };

  App.prototype.renderTable = function () {
    var self = this, st = this.state, L = this.latest;
    var cols = columns(st.tab, L, st);
    this.cols = cols;
    var sortCol = cols.filter(function (c) { return c.key === st.sortKey; })[0] || null;
    var rows = sortRows(filterRows(this.rows(), st), sortCol, st.sortDir);
    this.visible = rows;
    this.$('#grid thead').innerHTML = '<tr>' + cols.map(function (c) {
      var arrow = c.key === st.sortKey ? (st.sortDir > 0 ? ' ▲' : ' ▼') : '';
      return '<th data-key="' + c.key + '" class="' + (c.cls || '') + (c.fund ? ' fund' : '') + '">' + esc(c.label) + arrow + '</th>';
    }).join('') + '</tr>';
    var html = rows.map(function (r) {
      var cells = cols.map(function (c) { return c.html(r); }).join('');
      var open = r.stock_id === st.openCode;
      var tr = '<tr class="row' + (open ? ' open' : '') + '" data-code="' + esc(r.stock_id) + '">' + cells + '</tr>';
      if (open) {
        tr += '<tr class="chartrow"><td colspan="' + cols.length + '"><div class="chart" id="chart"></div>' +
              '<div class="legend">' + esc(r.stock_id + ' ' + (r.stock_name || '')) +
              '：週 K（還原價，紅漲綠跌）、藍線為週 20MA、下方為週成交量（張），虛線為前 20 週均量。</div></td></tr>';
      }
      return tr;
    }).join('');
    this.$('#grid tbody').innerHTML = html;
    this.$('#empty').hidden = rows.length > 0;
    if (this.chart && this.chart.remove) { this.chart.remove(); }
    this.chart = null;
    if (st.openCode && this.$('#chart')) {
      this.chart = renderChart(this.$('#chart'), (L.weekly_charts || {})[st.openCode]);
    }
  };

  App.prototype.render = function () { this.renderControls(); this.renderTable(); };

  App.prototype.bind = function () {
    var self = this, st = this.state;
    this.$$('#tabs button').forEach(function (b) {
      b.addEventListener('click', function () {
        st.tab = b.dataset.tab; st.sortKey = null; st.sortDir = -1; st.openCode = null; self.render();
      });
    });
    this.$('#q').addEventListener('input', function (e) { st.q = e.target.value; self.renderTable(); });
    this.$('#industry').addEventListener('change', function (e) { st.industry = e.target.value; self.renderTable(); });
    this.$('#spikeOnly').addEventListener('change', function (e) { st.spikeOnly = e.target.checked; self.renderTable(); });
    this.$('#showFund').addEventListener('change', function (e) { st.showFund = e.target.checked; self.renderTable(); });
    this.$$('#etfFilters input').forEach(function (cb) {
      cb.addEventListener('change', function () {
        st.etfSubtypes = self.$$('#etfFilters input').filter(function (x) { return x.checked; }).map(function (x) { return x.value; });
        self.renderTable();
      });
    });
    this.$('#etfAll').addEventListener('click', function () {
      st.etfSubtypes = Object.keys(ETF_SUBTYPE_ZH); self.render();
    });
    this.$('#etfZone').addEventListener('change', function (e) { st.etfZone = e.target.value; self.renderTable(); });
    this.$('#grid thead').addEventListener('click', function (e) {
      var th = e.target.closest('th');
      if (!th) return;
      if (st.sortKey === th.dataset.key) st.sortDir = -st.sortDir; else { st.sortKey = th.dataset.key; st.sortDir = -1; }
      self.renderTable();
    });
    this.$('#grid tbody').addEventListener('click', function (e) {
      var tr = e.target.closest('tr.row');
      if (!tr) return;
      st.openCode = st.openCode === tr.dataset.code ? null : tr.dataset.code;
      self.renderTable();
    });
  };

  function start(latest) {
    var app = new App(latest, root.document);
    root.SW20_APP = app;
    app.renderHeader();
    app.bind();
    app.render();
    return app;
  }

  function fail(msg) {
    var el = root.document.getElementById('meta');
    if (el) el.textContent = msg;
  }

  function load() {
    var viaScript = function () {
      if (root.__SW20_LATEST__) return start(root.__SW20_LATEST__);
      var s = root.document.createElement('script');
      s.src = 'data/latest.js';
      s.onload = function () { root.__SW20_LATEST__ ? start(root.__SW20_LATEST__) : fail('讀不到 data/latest.js 的內容。'); };
      s.onerror = function () { fail('找不到 data/latest.json／latest.js：請先執行選股程式產出 site/data/。'); };
      root.document.head.appendChild(s);
    };
    if (root.location && root.location.protocol === 'file:') return viaScript();
    if (!root.fetch) return viaScript();
    root.fetch('data/latest.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(start)
      .catch(viaScript);
  }

  SW20.App = App; SW20.start = start;
  if (typeof module !== 'undefined' && module.exports) module.exports = SW20;
  if (root.document) {
    root.SW20 = SW20;
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', load); else load();
  }
})(typeof window !== 'undefined' ? window : this);
