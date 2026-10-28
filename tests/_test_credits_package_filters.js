/* UI-02：套餐筛选只保留一个状态源。真实页面函数 + 本仓库共享的严格 DOM。 */
'use strict';
const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');
const html = dashboardHtml();
const filters = [...html.matchAll(/<button type="button"[^>]*data-filter="(all|active|expiring|exhausted)"[^>]*>/g)];
assert.deepStrictEqual(filters.map(m => m[1]), ['all', 'active', 'expiring', 'exhausted']);
filters.forEach(m => assert.ok(m[0].includes('aria-pressed='), '初始 aria-pressed: ' + m[0]));
assert.ok(html.includes('id="cmPkgResultBadge"'));
dom.installDom();
const buttons = new Map();
const group = dom.makeElement('div');
for (const name of ['all', 'active', 'expiring', 'exhausted']) {
  const button = dom.makeElement('button');
  button.setAttribute('data-filter', name);
  group.appendChild(button);
  buttons.set(name, button);
}
group.querySelectorAll = selector => selector === 'button[data-filter]' ? [...buttons.values()] : [];
const realDocumentQuerySelectorAll = document.querySelectorAll;
document.querySelectorAll = selector => selector === '#creditsDetailModal button[data-filter]' ? [...buttons.values()] : realDocumentQuerySelectorAll(selector);
const api = new Function(dashboardScript() + '\n return {syncCreditsPackageFilter, filterCreditsPackages, onCreditsSearchInput, renderCreditsPackagesTable, setData(data){CURRENT_CREDITS_DATA=data;}, setFilter(value){CURRENT_PKG_FILTER=value;}, getFilter(){return CURRENT_PKG_FILTER;}};')();
const tbody = document.getElementById('cmPackagesBody');
const total = document.getElementById('cmPkgCountBadge');
const result = document.getElementById('cmPkgResultBadge');
const packages = [
  {name:'月度包', size:100, used:20, remain:80, days_left:30},
  {name:'即将到期', size:50, used:45, remain:5, days_left:3},
  {name:'长期包', size:10, used:0, remain:10, no_expiry:true, days_left:null},
  {name:'用完包', size:40, used:40, remain:0, days_left:30},
  {name:'过期包', size:30, used:0, remain:30, is_expired:true, days_left:-2},
];
let checks = 0;
function check(label, condition, detail){ checks++; assert.ok(condition, label + (detail ? ' -> ' + detail : '')); }
function checkState(filter, expected, packageCount){ if(packageCount === undefined) packageCount = packages.length;
  for (const [name, button] of buttons) {
    check(filter + ': ' + name + ' 高亮', button.classList.contains('active') === (name === filter));
    check(filter + ': ' + name + ' aria-pressed', button.getAttribute('aria-pressed') === String(name === filter));
  }
  check(filter + ': 状态变量', api.getFilter() === filter);
  check(filter + ': 显示数量', result.textContent === expected, result.textContent);
  check(filter + ': 总数不变', total.textContent === '共 ' + packageCount + ' 个');
}
function rows(){ return tbody.innerHTML.split('<tr>').slice(1); }
api.setData({packages});
api.setFilter('active');
api.syncCreditsPackageFilter();
api.renderCreditsPackagesTable();
checkState('active', '有剩余 3 / 5 个');
check('默认仍是三个有剩余套餐', rows().length === 3 && ['月度包','即将到期','长期包'].every(name => tbody.innerHTML.includes(name)));
check('默认仍排除已用完与过期套餐', !tbody.innerHTML.includes('用完包') && !tbody.innerHTML.includes('过期包'));
api.filterCreditsPackages('all', buttons.get('all'));
checkState('all', '全部 5 / 5 个');
check('全部恢复完整五行', rows().length === 5 && tbody.innerHTML.includes('用完包') && tbody.innerHTML.includes('过期包'));
api.filterCreditsPackages('expiring', buttons.get('expiring'));
checkState('expiring', '即将到期 1 / 5 个');
check('即将到期只保留一条', rows().length === 1 && tbody.innerHTML.includes('即将到期'));
api.filterCreditsPackages('exhausted', buttons.get('exhausted'));
checkState('exhausted', '已用完/过期 2 / 5 个');
check('已用完或过期保留两条', rows().length === 2 && tbody.innerHTML.includes('用完包') && tbody.innerHTML.includes('过期包'));
api.filterCreditsPackages('all', buttons.get('all'));
api.onCreditsSearchInput('月度');
checkState('all', '全部 1 / 5 个 · 含搜索');
api.onCreditsSearchInput('not-found');
checkState('all', '全部 0 / 5 个 · 含搜索');
check('搜索无结果仍有明确空态', tbody.innerHTML.includes('无符合条件的套餐权益包'));
api.onCreditsSearchInput('');
api.filterCreditsPackages('expiring', buttons.get('expiring'));
api.setData({packages:[{name:'长期包',remain:10,size:10,no_expiry:true,days_left:null}]});
api.renderCreditsPackagesTable();
check('零临期结果为 0 而不是展示全部', result.textContent === '即将到期 0 / 1 个' && tbody.innerHTML.includes('无符合条件的套餐权益包'));
checkState('expiring', '即将到期 0 / 1 个', 1);
api.setData({packages:[]});
api.setFilter('not-a-filter');
api.renderCreditsPackagesTable();
checkState('active', '有剩余 0 / 0 个', 0);
check('空账号有明确空态', tbody.innerHTML.includes('无符合条件的套餐权益包'));
console.log('package filter state assertions passed (' + checks + ' checks)');