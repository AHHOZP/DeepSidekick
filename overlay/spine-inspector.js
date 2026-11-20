/* DeepSidekick 形象页 Inspector（Spine 版）：动画 / 皮肤 / 外观 / 诊断。
 *
 * 入口、位置、交互手感与 Live2D 版一致（同一个「调试」按钮、同一个右侧面板），
 * 内容按引擎能力不同：Spine 没有参数/部件/表情，有的是一张动画表 + 可能的皮肤。
 *
 * 动作与外观会 postMessage 给父页面，由 WebUI 转成 /control 广播给
 * 浏览器舞台和桌宠——调完两边一起变，不是只改这一页。
 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
               '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  var TABS = [
    ['motion', '动画'],
    ['skin', '皮肤'],
    ['look', '外观'],
    ['diag', '诊断'],
  ];

  var I = { on: false, tab: 'motion', wired: false, timer: 0, rows: [] };

  function sp() { return window.SP || null; }

  function publish(op, payload) {
    if (window.parent === window) return;
    try {
      window.parent.postMessage(
        { type: 'deepsidekick-control', ctl: { op: op, ...(payload || {}) } },
        location.origin);
    } catch (e) {}
  }

  function publishView() {
    var v = sp() && sp().view;
    if (!v) return;
    var s = v.state;
    publish('view', { zoom: s.zoom, offX: s.normX, offY: s.normY });
  }

  function buildTitle() {
    var s = sp();
    $('ins-title').textContent = 'Spine ' + (s ? s.version : '');
    $('ins-sub').textContent = s
      ? s.animations().length + ' 个动画 · ' + s.skins().length + ' 套皮肤'
      : '';
  }

  function buildTabs() {
    var nav = $('ins-tabs');
    nav.textContent = '';
    TABS.forEach(function (pair) {
      var b = document.createElement('button');
      b.dataset.tab = pair[0];
      b.textContent = pair[1];
      b.onclick = function () { renderTab(pair[0]); };
      nav.appendChild(b);
    });
  }

  function bar(html) {
    var d = document.createElement('div');
    d.className = 'bar';
    d.innerHTML = html;
    $('ins-body').appendChild(d);
    return d;
  }

  function list() {
    var d = document.createElement('div');
    d.className = 'list';
    $('ins-body').appendChild(d);
    return d;
  }

  function renderTab(tab) {
    I.tab = tab;
    I.rows = [];
    var nav = document.querySelectorAll('#ins-tabs button');
    for (var i = 0; i < nav.length; i += 1) {
      nav[i].classList.toggle('on', nav[i].dataset.tab === tab);
    }
    var body = $('ins-body');
    body.textContent = '';
    ({ motion: renderMotion, skin: renderSkin, look: renderLook,
       diag: renderDiag }[tab] || renderMotion)();
  }

  function renderMotion() {
    var s = sp();
    if (!s) return;
    var names = s.animations();
    bar('<span class="muted">点一条播一次（不循环）。调试面板同时广播给桌宠。</span>');
    if (!names.length) {
      list().innerHTML = '<p class="muted">这个骨架没有动画。</p>';
      return;
    }
    var idle = s.idle();
    var box = list();
    names.forEach(function (name) {
      var btn = document.createElement('button');
      btn.className = 'item';
      btn.textContent = name + (name === idle ? '  · 待机' : '');
      btn.onclick = function () {
        if (!s.play(name, false)) {
          $('ins-foot').textContent = '播不了：' + (s.error || name);
          return;
        }
        $('ins-foot').textContent = '';
        publish('motion', { group: name, index: 0 });
      };
      box.appendChild(btn);
    });
  }

  function renderSkin() {
    var s = sp();
    if (!s) return;
    var skins = s.skins();
    bar('<span class="muted">皮肤只影响这一页的预览；桌宠用的是默认皮肤。</span>');
    if (!skins.length) {
      list().innerHTML = '<p class="muted">这个骨架没有皮肤。</p>';
      return;
    }
    var box = list();
    skins.forEach(function (name) {
      var btn = document.createElement('button');
      btn.className = 'item';
      btn.textContent = name;
      btn.onclick = function () {
        if (!s.skin(name)) $('ins-foot').textContent = s.error || '';
        renderTab('skin');
      };
      box.appendChild(btn);
    });
  }

  function renderLook() {
    var s = sp();
    if (!s) return;
    var v = s.view;
    bar('<span class="muted">同步到已连接的浏览器舞台和桌宠；不再只影响这一页。</span>');
    var box = list();
    function slider(label, min, max, step, get, set) {
      var row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = '<label>' + label + '</label>' +
        '<input type="range" min="' + min + '" max="' + max + '" step="' + step + '">' +
        '<span class="val"></span>';
      var rg = row.querySelector('input');
      var val = row.querySelector('.val');
      rg.value = get();
      val.textContent = Number(get()).toFixed(2);
      rg.oninput = function () {
        set(+rg.value);
        val.textContent = (+rg.value).toFixed(2);
        publishView();
      };
      box.appendChild(row);
    }
    slider('整体大小', 0.2, 4, 0.01, function () { return v.state.zoom; },
      function (x) { v.setZoom(x); });
    slider('水平位置', -1, 1, 0.01, function () { return v.state.normX; },
      function (x) { v.setOffsetNorm(x, v.state.normY); });
    slider('垂直位置', -1, 1, 0.01, function () { return v.state.normY; },
      function (x) { v.setOffsetNorm(v.state.normX, x); });

    var reset = document.createElement('button');
    reset.className = 'item wide';
    reset.textContent = '重新适配（复位大小/位置）';
    reset.onclick = function () {
      v.refit();
      renderTab('look');
      publish('reset', {});
    };
    box.appendChild(reset);
  }

  function renderDiag() {
    var s = sp();
    var pre = document.createElement('pre');
    pre.className = 'diag';
    var c = document.getElementById('canvas') || {};
    pre.textContent = [
      'renderer  spine ' + (s ? s.version : '?'),
      'canvas    ' + (c.width || 0) + '×' + (c.height || 0),
      'pma       ' + (s ? (s.pma ? 'yes' : 'no') : '?'),
      'animations ' + (s ? s.animations().length : 0),
      'skins     ' + (s ? s.skins().length : 0),
      'idle      ' + (s ? (s.idle() || '(无)') : '?'),
      'playing   ' + (s ? (s.playing() || '(无)') : '?'),
      'view      ' + JSON.stringify(s ? s.view.state : {}),
      'error     ' + (s && s.error ? s.error : '(无)'),
    ].join('\n');
    $('ins-body').appendChild(pre);
  }

  function refreshLive() {
    if (!I.on) return;
    if (I.tab === 'diag') renderTab('diag');
  }

  function open() {
    var s = sp();
    if (!s || !s.ready || I.on) return false;
    I.on = true;
    document.body.classList.add('inspect');
    $('inspector').setAttribute('aria-hidden', 'false');
    buildTabs();
    buildTitle();
    renderTab(I.tab);
    clearInterval(I.timer);
    I.timer = setInterval(refreshLive, 500);
    return true;
  }

  function wire() {
    if (I.wired) return;
    I.wired = true;
    $('ins-reset').onclick = function () {
      if (window.SP && window.SP.control) {
        window.SP.control.reset();
      } else {
        var s = sp();
        if (s) s.view.refit();
      }
      renderTab(I.tab);
      publish('reset', {});
    };
    $('ins-reload').onclick = function () {
      publish('reload', {});
      setTimeout(function () { location.reload(); }, 150);
    };
  }

  window.SPI = {
    wire: wire,
    open: open,
    get active() { return I.on; },
  };
})();
