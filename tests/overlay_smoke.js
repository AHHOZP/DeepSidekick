'use strict';

// 最小 DOM 运行时 smoke：真正执行 overlay/app.js，而不只是 grep 源码。
// 不加载 PIXI/Cubism；只要脚本在“无模型、无 WebSocket”的预览配置下能完成
// 启动流程、不抛未捕获异常，就说明模块顶层/布局/错误兜底没有被改坏。

const fs = require('fs');
const path = require('path');
const vm = require('vm');

function makeClassList() {
  const values = new Set();
  return {
    add: (...names) => names.forEach((x) => values.add(x)),
    remove: (...names) => names.forEach((x) => values.delete(x)),
    contains: (name) => values.has(name),
    toggle: (name, on) => (on ? values.add(name) : values.delete(name)),
  };
}

function makeElement(id) {
  const style = {
    setProperty(name, value) { this[name] = value; },
    getPropertyValue(name) { return this[name] || ''; },
  };
  const element = {
    id,
    style,
    dataset: {},
    classList: makeClassList(),
    textContent: '',
    innerHTML: '',
    value: '',
    width: 0,
    height: 0,
    children: [],
    appendChild(child) { this.children.push(child); return child; },
    remove() {},
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getContext() { return null; },
    getBoundingClientRect() {
      return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
    },
    play() {},
    pause() {},
  };
  return element;
}

const body = makeElement('body');
const head = makeElement('head');
const document = {
  body,
  head,
  documentElement: makeElement('html'),
  readyState: 'loading',
  getElementById: (id) => makeElement(id),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => makeElement(tag),
  addEventListener() {},
  removeEventListener() {},
};

const location = {
  search: '?preview=1',
  reload() {},
};

const windowStub = {
  document,
  location,
  addEventListener() {},
  removeEventListener() {},
  requestAnimationFrame() {},
  cancelAnimationFrame() {},
  setTimeout,
  clearTimeout,
  setInterval() {},
  clearInterval() {},
  performance: { now: () => 0 },
  SP: null,
  LMControl: null,
  LMI: null,
  SPI: null,
};

global.window = windowStub;
global.document = document;
global.location = location;
Object.defineProperty(global, 'navigator', {
  value: { userAgent: 'node-smoke' },
  configurable: true,
});
global.performance = windowStub.performance;
global.requestAnimationFrame = () => {};
global.cancelAnimationFrame = () => {};
global.setInterval = () => {};
global.clearInterval = () => {};
global.fetch = async () => ({
  ok: false,
  status: 503,
  async text() { return ''; },
  async json() { return {}; },
});
global.WebSocket = class {
  constructor() { this.readyState = 3; }
  close() {}
  send() {}
  addEventListener() {}
};
global.Image = class {
  constructor() { this.onload = null; this.onerror = null; }
  set src(_value) { if (this.onerror) this.onerror(new Error('no image')); }
};
global.Audio = class {
  constructor() { this.volume = 1; }
  play() {}
  pause() {}
};

const source = fs.readFileSync(
  path.join(__dirname, '..', 'overlay', 'app.js'), 'utf8');

process.on('unhandledRejection', (error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});

try {
  vm.runInThisContext(source, { filename: 'overlay/app.js' });
} catch (error) {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
}

// main() 是 async；让 fetch 的 rejected promise 跑到 catch 后再退出。
setTimeout(() => process.exit(0), 30);
