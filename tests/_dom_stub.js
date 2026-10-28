/* One fake DOM for the dashboard suites.

   The panel is a single script that touches a lot of DOM, and every suite used
   to carry its own element stub. That cost is real: #134 added one classList
   call and seven suites died at load, because each copy modelled only what its
   own assertions needed.

   This is the one copy. It is deliberately STRICT: a property or method that is
   not modelled throws with the name of the missing member, instead of returning
   a no-op. A permissive stub is the thing that hides regressions - a real DOM
   returns undefined for an unknown property, so a fake that returns a truthy
   function can flip `if (el.somethingNew)` in the panel and let a suite pass for
   the wrong reason. Adding a member here is one line; discovering it later is a
   debugging session.

   Usage:

     const dom = require('./_dom_stub.js');
     dom.installDom();                       // document, window, storage, timers
     const el = dom.makeElement('div');          // detached element
     const row = dom.byId('recent');         // persistent per id, like the page

   Per-suite needs go in through installDom():

     dom.installDom({
       querySelector: { '#modelsTable tbody': table },   // must answer
       fetch: (url, options) => ...,                     // the panel's requests
       confirm: () => true,                              // dialog answer
     });

   A suite that genuinely needs a permissive element can ask for it
   (`dom.makeElement('div', {permissive: true})`), but that is opt-in and visible
   in the suite, not the default.
*/
'use strict';

const NOOP = () => undefined;

// The platform timers, captured at load time. installDom() replaces the globals
// with its own wrappers, so a wrapper that called global.setTimeout would call
// itself once installed - RangeError: Maximum call stack size exceeded on the
// first timer. These references are taken before any install happens.
const PLATFORM_TIMERS = {
  setTimeout: global.setTimeout,
  clearTimeout: global.clearTimeout,
  setInterval: global.setInterval,
  clearInterval: global.clearInterval,
};

// What an element models. Everything else throws, so a new DOM call in the
// panel shows up as "add it here" rather than as a suite that quietly drifts.
const ELEMENT_METHODS = [
  'appendChild', 'removeChild', 'insertBefore', 'replaceChild', 'append',
  'prepend', 'remove', 'replaceWith', 'replaceChildren', 'cloneNode',
  'setAttribute', 'getAttribute', 'hasAttribute', 'removeAttribute',
  'toggleAttribute', 'addEventListener', 'removeEventListener', 'dispatchEvent',
  'querySelector', 'querySelectorAll', 'getElementsByClassName',
  'getElementsByTagName', 'closest', 'contains', 'matches', 'focus', 'blur',
  'click', 'scrollIntoView', 'insertAdjacentHTML', 'insertAdjacentElement',
  'getBoundingClientRect', 'getClientRects', 'setPointerCapture',
  'releasePointerCapture', 'animate',
];

function makeClassList() {
  const set = new Set();
  return {
    add: (...names) => { names.forEach(n => set.add(n)); },
    remove: (...names) => { names.forEach(n => set.delete(n)); },
    contains: (name) => set.has(name),
    toggle: (name, force) => {
      if (force === undefined) {
        if (set.has(name)) { set.delete(name); return false; }
        set.add(name); return true;
      }
      if (force) { set.add(name); return true; }
      set.delete(name); return false;
    },
    item: (i) => [...set][i] ?? null,
    replace: (oldName, newName) => (set.delete(oldName) ? (set.add(newName), true) : false),
    get length() { return set.size; },
    toString: () => [...set].join(' '),
    _set: set,
  };
}

// style is the one member that stays permissive: CSS property names are open
// ended, and the panel only ever writes them. setProperty/getPropertyValue are
// modelled because a couple of suites read a value back.
function makeStyle() {
  const store = {};
  const style = {
    setProperty: (name, value) => { store[name] = String(value); },
    getPropertyValue: (name) => (name in store ? store[name] : ''),
    removeProperty: (name) => { const old = store[name] || ''; delete store[name]; return old; },
  };
  return new Proxy(style, {
    get: (t, prop) => (prop in t ? t[prop] : (prop in store ? store[prop] : '')),
    set: (t, prop, value) => {
      if (prop in t) { t[prop] = value; return true; }
      store[prop] = value;
      return true;
    },
  });
}

function makeElement(tag, options) {
  const opts = options || {};
  const listeners = new Map();
  const attributes = {};
  const children = [];
  const unknown = opts.unknown;

  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    nodeName: String(tag || 'div').toUpperCase(),
    nodeType: 1,
    id: '',
    className: '',
    innerHTML: '',
    outerHTML: '',
    textContent: '',
    innerText: '',
    value: '',
    checked: false,
    disabled: false,
    hidden: false,
    selected: false,
    title: '',
    type: '',
    name: '',
    href: '',
    src: '',
    placeholder: '',
    htmlFor: '',
    dataset: {},
    classList: makeClassList(),
    style: makeStyle(),
    attributes,
    children,
    childNodes: children,
    // 面板用 firstElementChild 在日志终端里从头部裁行（跳过文本节点），
    // 真 DOM 的成员，这里也照真 DOM 建模：第一个元素子节点，没有就是 null。
    get firstElementChild() { return children.find(c => c && c.nodeType === 1) || null; },
    // 同一个理由的另一头：设置页的页签清单读文本节点上的 i18n 原稿（__wbSrc），
    // 用的是 firstChild 而不是 firstElementChild，因为原文就在文本节点上。
    get firstChild() { return children[0] || null; },
    parentNode: null,
    parentElement: null,
    offsetWidth: 0,
    offsetHeight: 0,
    offsetTop: 0,
    offsetLeft: 0,
    clientWidth: 0,
    clientHeight: 0,
    scrollTop: 0,
    scrollLeft: 0,
    scrollHeight: 0,
    scrollWidth: 0,
    isConnected: true,
  };

  // className and classList are two views of one set, the way the DOM works:
  // a suite that reads el.className after the panel called classList.add() must
  // see the class.
  Object.defineProperty(el, 'className', {
    get: () => el.classList.toString(),
    set: (value) => {
      el.classList._set.clear();
      String(value || '').split(/\s+/).filter(Boolean)
        .forEach(name => el.classList._set.add(name));
    },
    configurable: true,
    enumerable: true,
  });

  for (const name of ELEMENT_METHODS) el[name] = NOOP;

  // Canvas is a real part of the dashboard now that the shared analytics loader
  // also paints the legacy series chart. Keep DOM reads strict, but model the
  // open-ended 2D drawing surface with no-op methods and real return shapes.
  if (el.tagName === 'CANVAS') {
    const context = new Proxy({}, {
      get(target, prop) {
        if (prop in target) return target[prop];
        if (prop === 'measureText') return text => ({width: String(text).length * 6});
        if (prop === 'createLinearGradient' || prop === 'createRadialGradient') {
          return () => ({addColorStop: NOOP});
        }
        return NOOP;
      },
      set(target, prop, value) { target[prop] = value; return true; },
    });
    el.getContext = kind => kind === '2d' ? context : null;
  }

  // 元素对外的身份是最后返回的那个 Proxy；appendChild 挂 parentNode 时也必须用
  // 同一个身份。否则 panel 沿 parentNode 上溯看到的"祖先"与 childNodes 里的子
  // 节点不是同一个对象，按元素做的缓存（WeakMap 键）会整条链失效——真 DOM 里
  // documentElement === document.documentElement.parentNode 恒成立，桩要一致。
  let self = el;
  // 真 DOM 里 appendChild / insertBefore 是**移动**：先把节点从原来的父节点摘下来
  // 再挂过去。桩少了这一步的话，被搬走的节点会同时留在两个父节点的 children 里，
  // 「把区块从这一页搬到那一页」这类操作写没写对就完全看不出来了。
  const detach = (child) => {
    const parent = child && child.parentNode;
    if (parent && parent !== self && typeof parent.removeChild === 'function') {
      parent.removeChild(child);
    }
  };
  el.appendChild = (child) => {
    detach(child);
    children.push(child);
    if (child) child.parentNode = self;
    return child;
  };
  el.removeChild = (child) => {
    const i = children.indexOf(child);
    if (i >= 0) children.splice(i, 1);
    if (child) child.parentNode = null;
    return child;
  };
  el.insertBefore = (child, before) => {
    detach(child);
    const i = children.indexOf(before);
    children.splice(i < 0 ? children.length : i, 0, child);
    if (child) child.parentNode = self;
    return child;
  };
  el.append = (...nodes) => nodes.forEach(n => el.appendChild(n));
  el.prepend = (...nodes) => nodes.forEach(n => children.unshift(n));
  el.remove = () => { if (el.parentNode) el.parentNode.removeChild(el); };
  el.setAttribute = (name, value) => { attributes[name] = String(value); };
  el.getAttribute = (name) => (name in attributes ? attributes[name] : null);
  el.hasAttribute = (name) => name in attributes;
  // 面板的 i18n doAttrs() 先用 hasAttributes() 跳过完全没属性的元素，
  // 假元素按真 DOM 建模，免得整段 walk 在测试里抛"未建模成员"。
  el.hasAttributes = () => Object.keys(attributes).length > 0;
  el.removeAttribute = (name) => { delete attributes[name]; };
  el.addEventListener = (type, fn) => {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(fn);
  };
  el.removeEventListener = (type, fn) => {
    const list = listeners.get(type) || [];
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  };
  el.dispatchEvent = (event) => {
    const type = event && event.type;
    for (const fn of listeners.get(type) || []) fn(event);
    return true;
  };
  el.click = () => el.dispatchEvent({ type: 'click', target: el });
  el.querySelector = (selector) => {
    const table = opts.querySelector || {};
    return selector in table ? table[selector] : null;
  };
  el.querySelectorAll = (selector) => {
    const table = opts.querySelectorAll || {};
    return selector in table ? table[selector] : [];
  };
  el.closest = (selector) => (el.matches(selector) ? el : null);
  el.matches = (selector) => (selector.startsWith('.') ? el.classList.contains(selector.slice(1)) : false);
  el.contains = (node) => node === el || children.includes(node);
  el.cloneNode = () => makeElement(tag, options);
  el.getBoundingClientRect = () => ({
    top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0,
  });
  el._listeners = listeners;
  el._options = opts;

  if (!opts.permissive) {
    self = new Proxy(el, {
      get(target, prop) {
        if (typeof prop === 'symbol') return target[prop];
        if (prop === 'then') return undefined;          // never a thenable
        if (prop in target) return target[prop];
        const message = 'fake DOM: <' + String(tag) + '>.' + String(prop)
          + ' is not modelled - add it to tests/_dom_stub.js';
        if (unknown) unknown.push(message);
        throw new Error(message);
      },
      set(target, prop, value) {
        // Writing is not the regression risk: the panel may put anything on an
        // element (onclick handlers, caches). Reads are what must stay honest.
        target[prop] = value;
        return true;
      },
    });
    return self;
  }
  if (unknown) {
    self = new Proxy(el, {
      get(target, prop) {
        if (typeof prop === 'symbol' || prop in target) return target[prop];
        if (prop === 'then') return undefined;
        unknown.push('fake DOM (permissive): <' + String(tag) + '>.' + String(prop));
        return NOOP;
      },
      set(target, prop, value) { target[prop] = value; return true; },
    });
    return self;
  }
  return el;
}

function makeStorage() {
  const store = new Map();
  return {
    getItem: (k) => (store.has(String(k)) ? store.get(String(k)) : null),
    setItem: (k, v) => { store.set(String(k), String(v)); },
    removeItem: (k) => { store.delete(String(k)); },
    clear: () => store.clear(),
    key: (i) => [...store.keys()][i] ?? null,
    get length() { return store.size; },
    _store: store,
  };
}

// installDom() returns these, and they are also reachable from the module, so a
// suite can write `dom.byId(id)` after installing instead of threading the
// return value through.
let installed = null;

function installDom(options) {
  const opts = options || {};
  const unknown = [];
  const byIdMap = new Map();

  const document = {
    readyState: 'complete',
    visibilityState: 'visible',
    hidden: false,
    title: '',
    cookie: '',
    body: makeElement('body', { unknown, permissive: opts.permissive }),
    head: makeElement('head', { unknown, permissive: opts.permissive }),
    documentElement: makeElement('html', { unknown, permissive: opts.permissive }),
    activeElement: null,
    getElementById: (id) => {
      if (!byIdMap.has(id)) {
        const canvasIds = new Set(['seriesCanvas', 'usageSeriesChart', 'sparkTokens',
          'sparkReqs', 'sparkSpeed', 'sparkCache', 'sparkCost']);
        const el = makeElement(canvasIds.has(id) ? 'canvas' : 'div', { unknown, permissive: opts.permissive });
        el.id = id;
        if (el.tagName === 'CANVAS') el.parentElement = document.body;
        byIdMap.set(id, el);
      }
      return byIdMap.get(id);
    },
    createElement: (tag) => makeElement(tag, { unknown, permissive: opts.permissive }),
    createElementNS: (ns, tag) => makeElement(tag, { unknown, permissive: opts.permissive }),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text), data: String(text) }),
    createDocumentFragment: () => makeElement('fragment', { unknown, permissive: opts.permissive }),
    querySelector: (selector) => {
      const table = opts.querySelector || {};
      return selector in table ? table[selector] : null;
    },
    querySelectorAll: (selector) => {
      const table = opts.querySelectorAll || {};
      return selector in table ? table[selector] : [];
    },
    addEventListener: NOOP,
    removeEventListener: NOOP,
    dispatchEvent: () => true,
    execCommand: () => true,
    getElementsByTagName: () => [],
  };

  const storage = makeStorage();
  const location = {
    href: opts.href || 'http://127.0.0.1:8787/',
    origin: 'http://127.0.0.1:8787',
    protocol: 'http:',
    host: '127.0.0.1:8787',
    hostname: '127.0.0.1',
    port: '8787',
    pathname: '/',
    search: '',
    hash: '',
    reload: NOOP,
    assign: NOOP,
    replace: NOOP,
    toString() { return this.href; },
  };

  const fetchImpl = opts.fetch || (() => Promise.reject(new Error('fetch was not stubbed')));

  const MutationObserver = class {
    constructor(callback) { this._callback = callback; this._targets = []; }
    observe(target) { this._targets.push(target); }
    disconnect() { this._targets = []; }
    takeRecords() { return []; }
  };

  // Timers default to no-ops: the panel starts polling intervals at boot, and a
  // real one would keep Node alive until the runner's timeout. A suite that
  // needs a timer to actually fire asks for it (`installDom({realTimers: true})`),
  // and the wrapper then calls the platform timer captured at load time - see
  // PLATFORM_TIMERS above for why it cannot call global.setTimeout here.
  const realTimers = opts.realTimers === true;
  const timers = realTimers ? {
    setTimeout: (fn, ms, ...args) => PLATFORM_TIMERS.setTimeout(fn, ms, ...args),
    clearTimeout: (id) => PLATFORM_TIMERS.clearTimeout(id),
    setInterval: (fn, ms, ...args) => PLATFORM_TIMERS.setInterval(fn, ms, ...args),
    clearInterval: (id) => PLATFORM_TIMERS.clearInterval(id),
    requestAnimationFrame: (fn) => PLATFORM_TIMERS.setTimeout(() => fn(Date.now()), 0),
    cancelAnimationFrame: (id) => PLATFORM_TIMERS.clearTimeout(id),
  } : {
    setTimeout: () => 0,
    clearTimeout: NOOP,
    setInterval: () => 0,
    clearInterval: NOOP,
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: NOOP,
  };

  const window = {
    document,
    location,
    localStorage: storage,
    sessionStorage: makeStorage(),
    navigator: { userAgent: 'node', language: 'zh-CN', languages: ['zh-CN', 'en'], clipboard: null },
    history: { pushState: NOOP, replaceState: NOOP, back: NOOP, forward: NOOP, go: NOOP, length: 1, state: null },
    matchMedia: (query) => ({
      matches: false, media: String(query), onchange: null,
      addEventListener: NOOP, removeEventListener: NOOP,
      addListener: NOOP, removeListener: NOOP, dispatchEvent: () => true,
    }),
    getComputedStyle: () => makeStyle(),
    requestAnimationFrame: timers.requestAnimationFrame,
    cancelAnimationFrame: timers.cancelAnimationFrame,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    alert: NOOP,
    confirm: opts.confirm || (() => false),
    prompt: () => null,
    fetch: fetchImpl,
    MutationObserver,
    addEventListener: NOOP,
    removeEventListener: NOOP,
    dispatchEvent: () => true,
    scrollTo: NOOP,
    scrollBy: NOOP,
    open: () => null,
    print: NOOP,
    innerWidth: 1280,
    innerHeight: 900,
    scrollX: 0,
    scrollY: 0,
    devicePixelRatio: 1,
  };

  // Node 21+ exposes some of these as getter-only globals, so defineProperty is
  // the only way to install them.
  const setGlobal = (name, value) => {
    try {
      if (global[name] === value) return;
    } catch (err) { /* getter-only in this Node: fall through */ }
    Object.defineProperty(global, name, { value, configurable: true, writable: true });
  };

  setGlobal('document', document);
  setGlobal('window', window);
  setGlobal('location', location);
  setGlobal('localStorage', storage);
  setGlobal('sessionStorage', window.sessionStorage);
  setGlobal('navigator', window.navigator);
  setGlobal('MutationObserver', MutationObserver);
  setGlobal('getComputedStyle', window.getComputedStyle);
  setGlobal('requestAnimationFrame', timers.requestAnimationFrame);
  setGlobal('cancelAnimationFrame', timers.cancelAnimationFrame);
  setGlobal('setTimeout', timers.setTimeout);
  setGlobal('clearTimeout', timers.clearTimeout);
  setGlobal('setInterval', timers.setInterval);
  setGlobal('clearInterval', timers.clearInterval);
  setGlobal('alert', NOOP);
  setGlobal('confirm', window.confirm);
  setGlobal('fetch', fetchImpl);

  installed = {
    document, window, unknown,
    byId: (id) => byIdMap.get(id),
    byIdMap,
    makeElement,
    createElement: makeElement,
  };
  return installed;
}

function byId(id) {
  return installed ? installed.byId(id) : null;
}

// The factory is makeElement/createElement; there is deliberately no `element`
// export, because four suites use `const element = id => dom.byId(id)` as their
// own local alias and one ambiguous name for two things is how that mismatch
// started.
module.exports = {
  installDom,
  makeElement,                 // the tag factory: dom.makeElement('div')
  createElement: makeElement,  // the DOM spelling of the same thing
  byId,                        // the persistent element for an id
  unknown: () => (installed ? installed.unknown : []),
};
