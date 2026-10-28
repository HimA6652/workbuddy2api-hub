/* UI-03：积分历史与主看板共用同一区间和同一份 timeseries 响应。
 * 只提取真实页面中的相关函数；DOM/API/时钟均为受控桩，不启动网关。
 */
'use strict';
const assert = require('assert');
const {dashboardHtml} = require('./_dashboard_source.js');
const html = dashboardHtml();
for (const id of ['rangeCustomBox', 'rangeCustomSince', 'rangeCustomUntil']) {
  assert.strictEqual((html.match(new RegExp('id="' + id + '"', 'g')) || []).length, 1, id + ' unique');
}
assert.ok(/<button data-range="custom">自定义<\/button>/.test(html));
assert.ok(!html.includes('function syncOldRange('), '不再把近3日/近30日映射成今日');
assert.ok(!html.includes("$('foldCreditsBox')"), '折叠重开不另发覆盖请求');

function between(start, end) {
  const from = html.indexOf(start);
  const to = html.indexOf(end, from);
  assert.ok(from >= 0 && to > from, start + ' -> ' + end);
  return html.slice(from, to);
}
const source = [
  between('function prefillCustomRange(', 'function pad2('),
  between('function pad2(', '/* 模型性能指标与用量一览'),
  between('function rangeParams(', '/* 区间状态与 B 版 state 同一词法作用域'),
  between('function loadRangeForOldDashboard(', 'let WBA_GEN = 0;'),
  between('let WBA_GEN = 0;', 'async function loadStatic('),
  between('async function loadCreditHistory(', '// 最近一次的积分扣减数据'),
].join('\n');
const state = {range:'today', analytics:null, series:null};
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    id, value:'', textContent:'', innerHTML:'', style:{}, disabled:false,
    classList:{on:false, toggle(name, value){this[name] = value;}},
  });
  return elements.get(id);
}
const rangeButtons = ['today', 'd3', 'week', 'month', 'd30', 'all', 'custom'].map(range => ({
  dataset:{range}, classList:{on:false, toggle(name, value){this[name] = value;}},
}));
element('rangeSeg').children = rangeButtons;
const requests = [];
const manualRequests = [];
const creditSnapshots = [];
let pending = null;
let nowMs = Date.parse('2026-10-28T12:00:00+08:00');
const pollRead = {revalidate:true};
let analyticsLoads = 0;
let matrixLoads = 0;
const api = new Function('state', 'document', '$', 'Date', 'api', 'getJSON', 'POLL_READ',
  'loadPrevSummary', 'renderCreditHistory', 'renderAnalytics', 'renderUsageSeries', 'renderKpis',
  'renderSeriesMeta', 'drawSeries', 'drawSparks', 'renderDim', 'loadRecent',
  'loadAnalytics', 'loadAnalyticsMatrix', 'toast', 'applyTableSort', 'esc', 'fmt', 'pollUntil',
  source + '\nreturn {setAnalyticsRange:loadRangeForOldDashboard, rangeParams, analyticsQuery, prefillCustomRange, loadRange, loadCreditHistory};')(
  state,
  {getElementById:element, querySelector:selector => selector === '#creditHistoryTable tbody' ? element('creditBody') : null},
  element,
  class extends Date { static now(){return nowMs;} },
  async url => {
    requests.push(url);
    if (pending) return pending;
    return {credits:[{iso:url, model:'fixture', account:'uid-fixture', credit:1, total_tokens:2}]};
  },
  async (url, options) => {
    manualRequests.push({url, options});
    return {credits:[{iso:'manual-' + state.range, model:'fixture', account:'uid-fixture', credit:1, total_tokens:2}]};
  },
  pollRead,
  async () => ({}),
  data => creditSnapshots.push(data),
  () => {}, () => {},
  () => {}, () => {}, () => {}, () => {}, () => {}, () => {},
  () => {analyticsLoads++;}, () => {matrixLoads++;},
  () => {}, () => {}, value => String(value), value => String(value),
  () => null
);

let checks = 0;
function check(label, condition, detail) {
  checks++;
  assert.ok(condition, label + (detail === undefined ? '' : ' -> ' + detail));
}
function query(url) { return new URLSearchParams(url.split('?')[1]); }
function activeRange() { return rangeButtons.filter(b => b.classList.on).map(b => b.dataset.range); }
function creditIso() { return creditSnapshots[creditSnapshots.length - 1].credits[0].iso; }

(async () => {
  const beforeManual = manualRequests.length;
  await api.loadRange();
  check('初始只取主看板与积分历史共同需要的一次 series', requests.length === 2, requests.join('\n'));
  check('积分历史不再额外 GET timeseries', manualRequests.length === beforeManual);
  check('初始今日区间', requests.every(url => query(url).get('range') === 'today'));
  check('主看板 series 原对象交给积分历史', creditSnapshots[0] === state.series);

  for (const range of ['d3', 'week', 'month', 'd30', 'all']) {
    const before = requests.length;
    api.setAnalyticsRange(range);
    check(range + ' 同步新旧按钮状态', activeRange().join() === range && state.range === range);
    await api.loadRange();
    const pair = requests.slice(before);
    check(range + ' 两个端点区间完全相同', pair.length === 2 && query(pair[0]).toString() === query(pair[1]).toString(), pair.join('\n'));
    const q = query(pair[1]);
    if (range === 'd3' || range === 'd30' || range === 'all') {
      const days = range === 'd3' ? 3 : range === 'd30' ? 30 : 365;
      check(range + ' 保留原有天数', q.get('range') === 'custom' && Number(q.get('until')) - Number(q.get('since')) === days * 86400, q.toString());
    } else check(range + ' 不折叠成 today', q.get('range') === range, q.toString());
    check(range + ' 历史行来自本轮响应', creditIso().includes('range=' + (q.get('range') === 'custom' ? 'custom' : range)), creditIso());
    check(range + ' 不额外刷新积分接口', manualRequests.length === beforeManual);
  }

  api.setAnalyticsRange('custom');
  check('自定义有入口且输入框可见', activeRange().join() === 'custom' && element('rangeCustomBox').style.display === 'inline-flex');
  const midnight = new Date(nowMs); midnight.setHours(0, 0, 0, 0);
  const expectedSince = Math.floor(midnight.getTime() / 1000);
  check('默认起点为本地今日零点', element('rangeCustomSince').value.length > 0 && api.analyticsQuery().includes('&since=' + expectedSince), api.analyticsQuery());
  check('空终点交给服务端取当前时间', !api.analyticsQuery().includes('&until='));
  element('rangeCustomSince').value = '2026-10-20T08:30';
  element('rangeCustomUntil').value = '2026-10-21T19:45';
  const fixed = api.analyticsQuery();
  await api.loadRange();
  nowMs += 86400000;
  await api.loadRange(true);
  check('固定自定义区间轮询不漂移', requests.slice(-2).every(url => url.endsWith(fixed.slice('?realm=all&'.length))), requests.slice(-2).join('\n'));
  check('自定义仍由统一入口触发主看板与矩阵', analyticsLoads === 6 && matrixLoads === 6, analyticsLoads + '/' + matrixLoads);
  element('rangeCustomSince').value = '2026-10-25T12:00';
  element('rangeCustomUntil').value = '2026-10-20T12:00';
  check('反向自定义边界原样交给既有后端交换逻辑', api.analyticsQuery().includes('since=') && api.analyticsQuery().includes('until='));
  element('rangeCustomSince').value = 'not-a-date';
  element('rangeCustomUntil').value = '';
  check('无效或空边界不生成 NaN 参数', api.analyticsQuery() === '?realm=all&range=custom', api.analyticsQuery());

  api.setAnalyticsRange('month');
  pending = new Promise(resolve => { api.resolveMonth = resolve; });
  const slowMonth = api.loadRange();
  api.setAnalyticsRange('week');
  pending = null;
  await api.loadRange();
  const latestCredit = creditIso();
  api.resolveMonth({credits:[{iso:'stale-month', model:'old', account:'uid', credit:1}]});
  await slowMonth;
  check('慢旧响应不会覆盖较新区间的积分历史', creditIso() === latestCredit && state.range === 'week', creditIso());

  const button = element('manualRefresh');
  await api.loadCreditHistory(button);
  check('手动刷新保留当前周区间', manualRequests.at(-1).url.includes('range=week'), manualRequests.at(-1).url);
  check('手动刷新强制回源且保留ETag校验', manualRequests.at(-1).options.revalidate === true && manualRequests.at(-1).options.force === true);
  check('手动刷新走共享区间适配器', api.analyticsQuery() === '?realm=all&' + api.rangeParams('week'));
  check('手动刷新仍更新旧趋势和积分表', creditIso() === 'manual-week');
  check('刷新按钮最终恢复可用', button.disabled === false);

  const adapter = between('loadAnalytics = wbaTick;', 'window.loadAnalyticsMatrix = function(){};');
  let legacyLoads = 0;
  const bound = new Function('state', 'analyticsRange', 'document', 'loadAnalytics', 'wbaTick',
    'loadRangeForOldDashboard', 'rangeParams', 'loadUsageSeries',
    adapter + 'return {setAnalyticsRange, analyticsQuery, loadAnalytics};')(
      state, 'today', {getElementById:element}, () => {legacyLoads += 1;}, () => {},
      api.setAnalyticsRange, api.rangeParams, () => {throw new Error('legacy series must not load independently');});
  bound.setAnalyticsRange('d30');
  const expectedD30 = '?realm=all&' + api.rangeParams('d30');
  check('真实接线后近30日仍沿用当前区间', state.range === 'd30' && bound.analyticsQuery() === expectedD30);
  check('真实接线替换旧加载器而非留下第二套', bound.loadAnalytics() === undefined && legacyLoads === 0);

  console.log('analytics/credit range assertions passed (' + checks + ' checks)');
})().catch(error => {console.error(error); process.exit(1);});
