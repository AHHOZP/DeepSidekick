/* DeepSidekick 形象页 Inspector：右侧面板，参数 / 部件 / 动作 / 表情 / 外观 / 诊断。
 *
 * 设计原则：
 *  · 只用引擎公开数据（coreModel.getModel() 的 parameters/parts 数组 + setXxxByIndex）；
 *  · 不读任何 per-model 配置，模型有什么就显示什么；
 *  · 参数/部件必须每帧在 beforeModelUpdate 里写（引擎每帧 loadParameters 会把值还原）；
 *  · 打开时停掉正在播的动作，之后一切由人点（默认不自动做动作）。
 */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;',
              '"': '&quot;', "'": '&#39;'}[c];
    });
  };

  var I = {
    on: false, tab: 'param', hooked: false,
    model: null, app: null, im: null, fw: null, core: null,
    overrides: new Map(),   // 参数 index -> 值
    partOps: new Map(),     // 部件 index -> 透明度
    labels: new Map(),      // id -> 中文名（来自 cdi3）
    rows: [],               // 参数行的刷新函数
    timer: 0,
  };

  function view() { return window.LMView || null; }

  function publish(op, payload) {
    if (window.parent === window) return;
    try {
      window.parent.postMessage(
        { type: 'deepsidekick-control', ctl: { op, ...(payload || {}) } },
        location.origin);
    } catch (e) {}
  }

  function publishView() {
    var v = view();
    if (!v) return;
    var s = v.state;
    publish('view', { zoom: s.zoom, offX: s.normX, offY: s.normY });
  }

  // ── 常驻面板 ────────────────────────────────────────
  function open() {
    var v = view();
    if (!v || !v.model || I.on) return false;
    I.on = true;
    I.model = v.model;
    I.app = v.app;
    I.im = v.model.internalModel;
    I.fw = I.im.coreModel;
    I.core = I.fw.getModel();

    document.body.classList.add('inspect');
    $('inspector').setAttribute('aria-hidden', 'false');
    buildTitle();
    hook();
    loadLabels();
    renderTab(I.tab);

    // 默认不自动做动作：进面板时把正在播的停掉（之后再点才动）
    try { I.im.motionManager.stopAllMotions(); } catch (e) {}

    clearInterval(I.timer);
    I.timer = setInterval(refreshLive, 250);
    return true;
  }

  // ── 每帧写入（关键：引擎每帧会还原参数）──────────────
  function hook() {
    if (I.hooked) return;
    I.hooked = true;
    var write = function () {
      if (!I.on) return;
      if (I.overrides.size) I.overrides.forEach(function (v, i) {
        try { I.fw.setParameterValueByIndex(i, v, 1); } catch (e) {}
      });
      if (I.partOps.size) I.partOps.forEach(function (v, i) {
        try { I.fw.setPartOpacityByIndex(i, v); } catch (e) {}
      });
    };
    // 物理输入参数必须写在 physics.evaluate 之前；beforeModelUpdate 在之后。
    var pre = window.__LM_PRE_PHYSICS
      || (window.__LM_PRE_PHYSICS = []);
    pre.push(write);
    I.im.on('beforeModelUpdate', write);
  }

  // ── 标题 / 标签 ─────────────────────────────────────
  function buildTitle() {
    var s = I.im.settings || {};
    var name = s.name || (I.model.tag || '') || 'Inspector';
    $('ins-title').textContent = name;
    $('ins-sub').textContent = I.core.parameters.count + ' 参数 · ' +
      I.core.parts.count + ' 部件 · ' + I.core.drawables.count + ' 绘制对象';
  }

  function loadLabels() {
    try {
      var s = I.im.settings || {};
      var json = s.json || (s.settingsJSON || {});
      var refs = json.FileReferences || {};
      var rel = refs.DisplayInfo;
      if (!rel) return;
      // settings.url 可能是相对路径（/m/<id>/xxx.model3.json），new URL 的 base 必须是绝对的
      var base = s.url ? new URL(s.url, location.href).href : location.href;
      var u = new URL(rel, base).href;
      fetch(u).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (j) {
        if (!j) return;
        (j.Parameters || []).forEach(function (p) { if (p.Id) I.labels.set(p.Id, p.Name || ''); });
        (j.Parts || []).forEach(function (p) { if (p.Id) I.labels.set(p.Id, p.Name || ''); });
        if (I.on && I.tab === 'param') renderTab('param');
        if (I.on && I.tab === 'part') renderTab('part');
      }).catch(function () {});
    } catch (e) {}
  }

  // ── 面板 ────────────────────────────────────────────
  function renderTab(tab) {
    I.tab = tab;
    var nav = document.querySelectorAll('#ins-tabs button');
    for (var i = 0; i < nav.length; i++) nav[i].classList.toggle('on', nav[i].dataset.tab === tab);
    I.rows = [];
    var body = $('ins-body');
    body.textContent = '';
    ({ param: renderParam, part: renderPart, motion: renderMotion,
       expr: renderExpr, look: renderLook, diag: renderDiag }[tab] || renderParam)();
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

  // 参数
  function renderParam() {
    var c = I.core.parameters;
    var b = bar('<input id="ins-q" placeholder="搜索参数 / 中文名">' +
                '<label><input type="checkbox" id="ins-only"> 只看非默认值</label>');
    var box = list();

    function build() {
      var q = ($('ins-q').value || '').trim().toLowerCase();
      var only = $('ins-only').checked;
      box.textContent = '';
      I.rows = [];
      var shown = 0;
      for (var i = 0; i < c.count; i++) {
        var id = c.ids[i];
        var meta = I.labels.get(id) || '';
        if (q && id.toLowerCase().indexOf(q) < 0 && meta.toLowerCase().indexOf(q) < 0) continue;
        var cur = c.values[i];
        if (only && Math.abs(cur - c.defaultValues[i]) < 1e-4 && !I.overrides.has(i)) continue;
        box.appendChild(paramRow(i, id, meta));
        shown++;
      }
      if (!shown) box.innerHTML = '<p class="muted">没有匹配的参数</p>';
    }
    $('ins-q').oninput = build;
    $('ins-only').onchange = build;
    build();
  }

  function paramRow(i, id, meta) {
    var c = I.core.parameters;
    var min = c.minimumValues[i], max = c.maximumValues[i], def = c.defaultValues[i];
    var row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = '<label title="' + esc(id) + '">' + esc(meta || id) +
      '</label>' +
      '<input type="range" min="' + min + '" max="' + max + '">' +
      '<span class="val"></span>';
    var input = row.querySelector('input');
    var val = row.querySelector('.val');
    var sync = function () {
      if (document.activeElement === input) return;
      var v = I.overrides.has(i) ? I.overrides.get(i) : c.values[i];
      input.value = v;
      val.textContent = Number(v).toFixed(2);
    };
    input.step = (max - min) / 200 || 0.01;
    input.oninput = function () {
      I.overrides.set(i, +input.value);
      val.textContent = (+input.value).toFixed(2);
      publish('param', { id: id, value: +input.value });
    };
    row.querySelector('label').ondblclick = function () {
      I.overrides.delete(i);
      sync();
      publish('param', { id: id, value: null });
    };
    sync();
    I.rows.push(sync);
    return row;
  }

  // 部件
  function renderPart() {
    var p = I.core.parts;
    bar('<span class="muted">取消勾选 = 每帧写 0；滑杆 0~1。双击名字恢复默认。</span>');
    var box = list();
    for (var i = 0; i < p.count; i++) {
      (function (i) {
        var id = p.ids[i];
        var meta = I.labels.get(id) || '';
        var row = document.createElement('div');
        row.className = 'row';
        row.innerHTML = '<label title="' + esc(id) + '">' + esc(meta || id) +
          '</label><input type="checkbox">' +
          '<input type="range" min="0" max="1" step="0.01">';
        var ck = row.querySelector('input[type=checkbox]');
        var rg = row.querySelector('input[type=range]');
        var v0 = p.opacities[i];
        rg.value = v0; ck.checked = v0 > 0.5;
        ck.onchange = function () {
          var val = ck.checked ? 1 : 0;
          I.partOps.set(i, val); rg.value = val;
          publish('part', { id: id, value: val });
        };
        rg.oninput = function () {
          I.partOps.set(i, +rg.value); ck.checked = +rg.value > 0.5;
          publish('part', { id: id, value: +rg.value });
        };
        row.querySelector('label').ondblclick = function () {
          I.partOps.delete(i);
          var v = I.core.parts.opacities[i];
          rg.value = v; ck.checked = v > 0.5;
          publish('part', { id: id, value: null });
        };
        box.appendChild(row);
      })(i);
    }
  }

  // 动作
  function renderMotion() {
    var mm = I.im.motionManager;
    var defs = (mm && mm.definitions) || {};
    var groups = Object.keys(defs);
    bar('<span class="muted">点一条就播一次；面板打开时不会自动播放。</span>');
    if (!groups.length) {
      list().innerHTML = '<p class="muted">这个 model3.json 没有声明 Motions。</p>';
      return;
    }
    var box = list();
    groups.forEach(function (g) {
      var h = document.createElement('div');
      h.className = 'grp';
      h.textContent = g + '  (' + defs[g].length + ')';
      box.appendChild(h);
      defs[g].forEach(function (m, idx) {
        var btn = document.createElement('button');
        btn.className = 'item';
        btn.textContent = idx + '  ' + String((m && m.File) || '').split('/').pop();
        btn.onclick = function () {
          Promise.resolve(I.model.motion(g, idx, 3)).catch(function (e) {
            $('ins-foot').textContent = 'motion 失败: ' + (e && e.message);
          });
          publish('motion', { group: g, index: idx });
        };
        box.appendChild(btn);
      });
    });
  }

  // 表情
  function renderExpr() {
    var em = I.im.motionManager && I.im.motionManager.expressionManager;
    var defs = (em && em.definitions) || [];
    bar('<span class="muted">点一条就贴上去（会按引擎的淡入时长生效）。</span>');
    if (!defs.length) {
      list().innerHTML = '<p class="muted">这个 model3.json 没有声明 Expressions。</p>';
      return;
    }
    var box = list();
    defs.forEach(function (d, i) {
      var btn = document.createElement('button');
      btn.className = 'item';
      btn.textContent = i + '  ' + ((d && d.Name) || '');
      btn.onclick = function () {
        Promise.resolve(I.model.expression(i)).catch(function () {});
        publish('expression', { name: ((d && d.Name) || i) });
      };
      box.appendChild(btn);
    });
  }

  // 外观（整体大小 / 位置）
  function renderLook() {
    var v = view();
    if (!v) return;
    bar('<span class="muted">同步到已连接的浏览器舞台和桌宠；不再只影响这一页。</span>');
    var box = list();
    function slider(id, label, min, max, step, get, set) {
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
      return rg;
    }
    slider('zoom', '整体大小', 0.2, 4, 0.01, function () { return v.state.zoom; },
      function (x) { v.setZoom(x); });
    slider('offx', '水平位置', -1, 1, 0.01, function () { return v.state.normX; },
      function (x) { v.setOffsetNorm(x, v.state.normY); });
    slider('offy', '垂直位置', -1, 1, 0.01, function () { return v.state.normY; },
      function (x) { v.setOffsetNorm(v.state.normX, x); });

    var reset = document.createElement('button');
    reset.className = 'item wide';
    reset.textContent = '重新适配（复位大小/位置）';
    reset.onclick = function () {
      if (window.LMControl && window.LMControl.reset) {
        window.LMControl.reset();
      } else {
        v.refit();
      }
      renderTab('look');
      publish('reset', {});
    };
    box.appendChild(reset);
  }

  // 诊断
  function renderDiag() {
    var c = I.core;
    var mm = I.im.motionManager;
    var pre = document.createElement('pre');
    pre.className = 'diag';
    pre.textContent = [
      'canvas    ' + I.app.canvas.width + '×' + I.app.canvas.height,
      'screen    ' + Math.round(I.app.screen.width) + '×' + Math.round(I.app.screen.height),
      'params    ' + c.parameters.count,
      'parts     ' + c.parts.count,
      'drawables ' + c.drawables.count,
      'textures  ' + (new Set(Array.from(c.drawables.textureIndices || [])).size),
      'motions   ' + Object.keys((mm && mm.definitions) || {}).length + ' 组',
      'view      ' + JSON.stringify(view() ? view().state : {}),
      'inspector ' + (I.on ? 'on' : 'off'),
    ].join('\n');
    $('ins-body').appendChild(pre);
  }

  function refreshLive() {
    if (!I.on) return;
    for (var i = 0; i < I.rows.length; i++) { try { I.rows[i](); } catch (e) {} }
    if (I.tab === 'diag') renderTab('diag');
  }

  // ── 装配 ────────────────────────────────────────────
  function wire() {
    if (I.wired) return;
    I.wired = true;
    var nav = document.querySelectorAll('#ins-tabs button');
    for (var i = 0; i < nav.length; i++) {
      (function (b) { b.onclick = function () { renderTab(b.dataset.tab); }; })(nav[i]);
    }
    $('ins-reset').onclick = function () {
      I.overrides.clear();
      I.partOps.clear();
      if (window.LMControl && window.LMControl.reset) {
        window.LMControl.reset();
      } else {
        var v = view();
        if (v) v.refit();
      }
      renderTab(I.tab);
      publish('reset', {});
    };
    $('ins-reload').onclick = function () {
      publish('reload', {});
      // 给 postMessage + WebUI relay 一拍时间；舞台/桌面由 relay 广播。
      setTimeout(function () { location.reload(); }, 150);
    };
  }

  window.LMI = {
    wire: wire,
    open: open,
    get active() { return I.on; },
  };
})();
