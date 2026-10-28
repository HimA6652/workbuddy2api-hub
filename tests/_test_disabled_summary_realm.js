/* 「当前禁用账号与模型」只描述当前视图的 realm（issue #31）。

   本节的数据来自 GET /accounts?realm=all —— window.ACCOUNTS 是全量的，所以过滤
   必须发生在面板里，而面板里 realm 的真源只有 window.VIEW_REALM（账号卡片一直
   用它）。这个套件用一个 CN + INTL 的混合 fixture 钉住：

     1. CN 视图只含 CN 的停用/受限状态，且不含任何 INTL 账号；
     2. INTL 视图对称；
     3. cn → intl → cn 来回切之后不残留上一个视图的行；
     4. 空状态与计数出自同一份过滤后的数据（另一侧有停用项也一样）；
     5. 每一行的类型/原因/恢复时间与改动前一致，正常账号不出现；
     6. 全页只允许存在一处 realm 过滤表达式，两个消费者共用它；
     7. 真实的切换函数（switchViewRealm / toggleActiveGatewayRealm）在异步账号
        重载**挂起或失败**时，也立刻把上一个 realm 的行清掉 —— 重画用的是已经
        加载好的全量 window.ACCOUNTS，不等重载。

   Run with Node.
*/
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');

const script = dashboardScript();
// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');

// The switch paths re-enter the loaders, so the fetch stub is part of the
// fixture: accountsReply decides whether the account reload answers, hangs or
// fails, and accountsRequests counts the reloads that were issued - which is how
// the last section proves the repaint did not come from a reload.
let accountsReply = 'ok';      // 'ok' | 'pending' | 'fail'
let accountsRequests = 0;
let realmReply = 'cn';

const answer = body => Promise.resolve({
  status: 200, ok: true,
  json: () => Promise.resolve(body),
  text: () => Promise.resolve(JSON.stringify(body)),
});
const payload = extra => Object.assign(
  {current: realmReply, accounts: [], slots: [], data: [], results: [],
   byAccount: [], summary: {window: {}, all_time: {}}, series: []}, extra);

dom.installDom({
  fetch: url => {
    const target = String(url);
    if(target.startsWith('/accounts?realm=all')){
      accountsRequests += 1;
      if(accountsReply === 'pending') return new Promise(() => {});
      if(accountsReply === 'fail') return Promise.reject(new Error('accounts reload failed'));
      return answer(payload({accounts: MIXED}));
    }
    if(target.startsWith('/accounts/credits')) return answer(payload({accounts: MIXED}));
    if(target.startsWith('/realm')) return answer({current: realmReply});
    return answer(payload());
  },
});

const api = new Function(script + `
  window.updateUI = updateUI;
  window.toast = toast;
  return {
    renderDisabled,
    disabledRows,
    accountsInView,
    switchViewRealm,
    toggleActiveGatewayRealm,
    setAccounts: list => { window.ACCOUNTS = list; },
    setView: view => { window.VIEW_REALM = view; },
    view: () => window.VIEW_REALM,
    setGatewayRealm: r => { window.ACTIVE_GATEWAY_REALM = r; },
  };`)();

const html = () => dom.byId('disabledList').innerHTML;
const countText = () => dom.byId('disabledCount').textContent;
// The who cell is "nickname (uid8)".
const shows = (markup, uid) => markup.includes('(' + uid.slice(0, 8) + ')');
const bodyRows = markup => (markup.match(/<tr>/g) || []).length - 1;  // minus <thead>

const NOW = Math.floor(Date.now() / 1000);
const CN_UIDS = ['cn-off-0001', 'cn-cool-0002', 'cn-daily-0003', 'cn-model-0004'];
const INTL_UIDS = ['intl-off-0005', 'intl-modelcool-0006', 'intl-credit-0007'];
const OK_UIDS = ['intl-ok-0008', 'cn-ok-0009'];

const MIXED = [
  // —— 国内版：手动停用 / 账号级限流 / 账号日限额 / 单模型日限额 ——
  {uid: 'cn-off-0001', nickname: 'CN停用', realm: 'cn', enabled: false},
  {uid: 'cn-cool-0002', nickname: 'CN限流', realm: 'cn', enabled: true,
   inCooldown: true, cooldownFor: 600, lastError: '上游 429',
   lastErrorDetail: 'HTTP 429 upstream'},
  {uid: 'cn-daily-0003', nickname: 'CN日限', realm: 'cn', enabled: true,
   dailyLimitBlocked: true, dailyTokensToday: 200000000, dailyTokenLimit: 200000000},
  {uid: 'cn-model-0004', nickname: 'CN模型日限', realm: 'cn', enabled: true,
   modelDailyTokenLimit: 100, modelDailyTokens: {hy4: 150}},
  // —— 国际版：手动停用 / 模型级限流 / 积分保留线 ——
  {uid: 'intl-off-0005', nickname: 'INTL停用', realm: 'intl', enabled: false},
  {uid: 'intl-modelcool-0006', nickname: 'INTL模型限流', realm: 'intl', enabled: true,
   modelCooldowns: [{model: 'gpt-6-sol', expiresAt: NOW + 900}]},
  {uid: 'intl-credit-0007', nickname: 'INTL积分线', realm: 'intl', enabled: true,
   creditLimitReached: true},
  // —— 正常账号：任何视图都不该出现 ——
  {uid: 'intl-ok-0008', nickname: 'INTL正常', realm: 'intl', enabled: true},
  {uid: 'cn-ok-0009', nickname: 'CN正常', realm: 'cn', enabled: true},
];

function render(view, fixture) {
  api.setAccounts(fixture || MIXED);
  api.setView(view);
  api.renderDisabled();
  return {markup: html(), count: countText(), rows: api.disabledRows()};
}

// 1. The reported bug: the CN view showed INTL state (and vice versa).
const cn = render('cn');
CN_UIDS.forEach(uid => assert.ok(shows(cn.markup, uid), 'cn view must show ' + uid));
INTL_UIDS.concat(OK_UIDS).forEach(uid =>
  assert.ok(!shows(cn.markup, uid), 'cn view must not show ' + uid));

const intl = render('intl');
INTL_UIDS.forEach(uid => assert.ok(shows(intl.markup, uid), 'intl view must show ' + uid));
CN_UIDS.concat(OK_UIDS).forEach(uid =>
  assert.ok(!shows(intl.markup, uid), 'intl view must not show ' + uid));

// 2. Switching back and forth must not retain the previous realm's rows.
const backToCn = render('cn').markup;
CN_UIDS.forEach(uid => assert.ok(shows(backToCn, uid), 'cn → intl → cn keeps ' + uid));
INTL_UIDS.forEach(uid =>
  assert.ok(!shows(backToCn, uid), 'cn → intl → cn must drop ' + uid));

// 3. Counts, row count and labels all come from the same filtered set.
assert.equal(cn.count, '(' + cn.rows.length + ')', 'the count is the filtered row count');
assert.equal(bodyRows(cn.markup), cn.rows.length, 'one table row per filtered entry');
assert.equal(cn.rows.length, 4, 'CN fixture: 停用 + 账号限流 + 日限额 + 模型日限额');
assert.ok(cn.rows.every(row => row.realm === '国内版'), 'every CN row is labelled 国内版');
assert.equal(intl.rows.length, 3, 'INTL fixture: 停用 + 模型限流 + 积分限额');
assert.ok(intl.rows.every(row => row.realm === '国际版'), 'every INTL row is labelled 国际版');

// 4. An empty active realm shows the empty state even though the other realm has
//    entries, and the count goes with it.
const intlOnly = MIXED.filter(a => a.realm === 'intl');
const empty = render('cn', intlOnly);
assert.equal(empty.rows.length, 0);
assert.equal(empty.count, '', 'an empty realm must not show a count');
assert.ok(empty.markup.includes('当前没有禁用的账号或模型。'), 'empty state must be shown');
assert.ok(render('intl', intlOnly).rows.length > 0, 'the other realm still has entries');

// 5. Row shape and enable/disable/limit behaviour are unchanged.
assert.ok(!cn.markup.includes('CN正常'), 'an enabled, unlimited account produces no row');
assert.ok(!intl.markup.includes('INTL正常'), 'an enabled, unlimited account produces no row');
['本地停用', '上游限流', '日限额'].forEach(kind =>
  assert.ok(cn.markup.includes(kind), 'cn view keeps the ' + kind + ' badge'));
assert.ok(intl.markup.includes('积分限额'), 'intl view keeps the 积分限额 badge');
assert.ok(cn.markup.includes('—（需手动启用）'), 'manual rows keep their 恢复时间 cell');
assert.ok(intl.markup.includes('该模型被上游临时限流（429）'), 'model cooldown without a cap reads as a transient 429');
assert.ok(!intl.markup.includes('code 6004'), 'a plain 429 must not be labelled as a quota cap');
assert.ok(!cn.markup.includes('剩余积分低于保留线'), '积分限额 is not a CN state here');

// 6. One realm filter expression, shared by the account cards and this section:
//    two copies are what drift apart.
const filters = script.match(/\.filter\(a => !a\.realm \|\| a\.realm ===/g) || [];
assert.equal(filters.length, 1, 'the realm filter must exist exactly once in the page');
assert.ok(/function renderAccounts\(\)[\s\S]{0,400}?accountsInView\(\)/.test(script),
          'the account cards must use the shared realm filter');
assert.ok(/function disabledRows\(\)[\s\S]*?accountsInView\(\)/.test(script),
          'the disabled summary must use the shared realm filter');

// 7. Consistency with the account cards: same realm rule, so a realm-less account
//    stays visible on both sides exactly as its card does.
const withNoRealm = MIXED.concat([{uid: 'norealm-0010', nickname: '无区域停用', enabled: false}]);
assert.ok(shows(render('cn', withNoRealm).markup, 'norealm-0010'), 'realm-less account in cn view');
assert.ok(shows(render('intl', withNoRealm).markup, 'norealm-0010'), 'realm-less account in intl view');
api.setView('cn');
assert.deepEqual(api.accountsInView().map(a => a.uid),
                 CN_UIDS.concat(['cn-ok-0009', 'norealm-0010']),
                 'the shared filter is what the account cards get');

// 8. The real switch paths repaint synchronously from the already-loaded list.
//    The reload is left hanging or made to fail, so the rows below can only have
//    come from the synchronous repaint - not from a completed reload.
(async () => {
  const cnOnScreen = label => {
    assert.ok(shows(html(), 'cn-off-0001'), label + ': cn rows must be on screen');
    CN_UIDS.forEach(uid => assert.ok(shows(html(), uid), label + ': ' + uid + ' present'));
    INTL_UIDS.forEach(uid => assert.ok(!shows(html(), uid), label + ': ' + uid + ' must be gone'));
  };

  function armSwitch(){
    api.setAccounts(MIXED);
    api.setView('intl');
    api.renderDisabled();
    assert.ok(shows(html(), 'intl-off-0005'), 'intl rows before the switch');
    assert.ok(!shows(html(), 'cn-off-0001'), 'no cn rows before the switch');
  }

  // 8a. switchViewRealm(): the account reload never answers.
  accountsReply = 'pending';
  accountsRequests = 0;
  armSwitch();
  const hanging = api.switchViewRealm('cn');
  hanging.catch(() => {});
  cnOnScreen('hanging reload, right after switchViewRealm()');
  assert.equal(accountsRequests, 1, 'the account reload was issued');
  await Promise.resolve();
  cnOnScreen('hanging reload, after a microtask');

  // 8b. switchViewRealm(): the account reload fails. The rows are already
  //     correct, and the failure cannot bring the previous realm's rows back.
  accountsReply = 'fail';
  accountsRequests = 0;
  armSwitch();
  const failing = api.switchViewRealm('cn');
  failing.catch(() => {});
  cnOnScreen('failing reload, right after switchViewRealm()');
  await failing.catch(() => {});
  cnOnScreen('failing reload, after it settled');

  // 8c. toggleActiveGatewayRealm() is the other path that moves VIEW_REALM. Its
  //     account reload is still outstanding when the view flips, so the repaint
  //     can only have come from the already-loaded list.
  accountsReply = 'pending';
  accountsRequests = 0;
  realmReply = 'cn';
  api.setGatewayRealm('intl');
  armSwitch();
  const toggling = api.toggleActiveGatewayRealm();
  toggling.catch(() => {});
  for(let spins = 0; api.view() !== 'cn' && spins < 50; spins++){
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.equal(api.view(), 'cn', 'toggleActiveGatewayRealm must move the view');
  assert.equal(accountsRequests, 1, 'its account reload is still outstanding');
  cnOnScreen('toggle path, reload outstanding');

  console.log('disabled summary realm isolation assertions passed');
})().catch(error => { console.error(error); process.exit(1); });
