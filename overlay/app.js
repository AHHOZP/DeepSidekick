/* DeepSidekick overlay：Live2D / 桌宠 / 占位形象 + 气泡。
 *
 * 页面只做两件事：
 *   1. 从 /config.json 拿当前形象和它的 capabilities（动作、表情、待机组）；
 *   2. 接收 Python 推来的 say/page/level/end，以及 gift 这类舞台事件。
 *
 * 模型接口以 model3.json 为准：服务端扫描模型目录，缺 Motions/Expressions
 * 时自动从 Animation/Exp3 文件补全，页面不写死任何模型名字或动作名。
 * 没有额外的清单文件：id 和显示名就是文件夹名，能力从 model3.json 现场推断。
 */
(function () {
  'use strict';

  const qs = new URLSearchParams(location.search);
  const PREVIEW = qs.has('preview');
  // 直播姬页面也能手动开调试面板：?inspect=1
  const WANT_INSPECT = PREVIEW || qs.has('inspect');
  const STATIC_BASE = qs.get('assets') || '/s';
  // ?scale= 是调试用的最高优先级；模型缩放不在配置里调（预览/桌宠/直播姬各自调）。
  const ZOOM_PARAM = parseFloat(qs.get('scale') || '');
  const zoomOf = () => (Number.isFinite(ZOOM_PARAM) ? ZOOM_PARAM : 1);
  const num = (k, d) => {
    const v = parseFloat(qs.get(k));
    return Number.isFinite(v) ? v : d;
  };
  const flag = (k, d) => qs.has(k) ? !/^(0|false|no|off)$/i.test(qs.get(k)) : d;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const el = (id) => document.getElementById(id);
  const pick = (arr) => arr.length ? arr[Math.floor(Math.random() * arr.length)] : '';

  // ── Inspector ─────────────────────────────────────
  // 两套引擎各有一份面板：入口（调试按钮/右侧栏）与手感一致，内容按引擎能力不同。
  const INSPECTOR = {
    live2d: { file: 'inspect.js', global: 'LMI' },
    spine: { file: 'spine-inspector.js', global: 'SPI' },
  };

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src + SCRIPT_VER;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error(src + ' 加载失败'));
      document.head.appendChild(s);
    });
  }

  function loadInspector(kind) {
    const spec = INSPECTOR[kind] || INSPECTOR.live2d;
    if (window[spec.global]) {
      window[spec.global].wire();
      return Promise.resolve(window[spec.global]);
    }
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = STATIC_BASE + '/' + spec.file + SCRIPT_VER;
      s.onload = () => {
        const mod = window[spec.global];
        if (mod) { mod.wire(); resolve(mod); }
        else reject(new Error(spec.file + ' 没挂上 ' + spec.global));
      };
      s.onerror = () => reject(new Error(spec.file + ' 加载失败'));
      document.head.appendChild(s);
    });
  }

  const DEFAULTS = { size: 95, gap: 0.4, lift: 0, maxWidth: 60, autoGap: true };
  const LAYOUT = {
    size: clamp(num('size', DEFAULTS.size), 20, 100),
    gap: clamp(num('gap', DEFAULTS.gap), -50, 50),
    lift: clamp(num('lift', DEFAULTS.lift), -50, 50),
    maxWidth: clamp(num('w', DEFAULTS.maxWidth), 20, 100),
    autoGap: flag('auto', DEFAULTS.autoGap),
  };
  // 角色外观（persona/roles/<id>/profile.yaml 的 stage 段）已合进 layout；
  // 这里不再单独处理 scale/bubble_gap。

  const bubbleEl = el('bubble');
  const textEl = el('bubble-text');
  const avatarEl = el('avatar');
  const imageEl = el('image');
  let canvasEl = el('canvas');
  const dbgEl = el('dbg');

  const S = {
    pages: [], idx: -1,
    cps: parseFloat(qs.get('cps') || '') || 4.6,
    level: 0, mouth: 0, maxMouth: 0, speaking: false, sync: false,
    mouthOpen: 0, mouthForm: 0, mouthAt: 0,
    pose: new Map(), poseAdd: new Map(), poseSeq: 0, autonomy: true,
    poseWrites: 0,
    prevPoseKeys: new Set(), handover: new Map(),
    expressionAt: 0,
    pageTimer: 0, typeTimer: 0, holdTimer: 0, fadeTimer: 0,
    renderer: 'live2d', sock: null, err: '', avatar: null, expressions: new Map(),
    content: null, fitInfo: '',
    caps: { actions: [], expressions: [], idle: '', lipSync: [] },
  };
  // 让动态加载的 Spine/Inspector 脚本继承 app.js 的缓存版本号。
  const SCRIPT_VER = (() => {
    const s = document.currentScript;
    if (!s || !s.src) return '';
    const i = s.src.indexOf('?');
    return i >= 0 ? s.src.slice(i) : '';
  })();
  let L2D = null;
  let IMG = null;
  const REMOTE = { params: new Map(), parts: new Map() };
  let ACTION_MAP = new Map();
  let EXPR_MAP = new Map();
  let IDLE_GROUP = '';
  let LIP_PARAMS = [];
  // Cubism 的 getParameterIndex(id) 只接受 CubismId 对象，传字符串会新建一个
  // “不存在的参数”并静默丢弃。浏览器侧所有参数读写统一走这张 string -> index 表。
  const PARAM_INDEX = new Map();

  function coreModel() {
    return L2D && L2D.model ? L2D.model.internalModel.coreModel : null;
  }

  function paramIndex(id) {
    return PARAM_INDEX.get(String(id));
  }

  function paramValue(id) {
    const cm = coreModel();
    const index = paramIndex(id);
    if (!cm || index === undefined) return 0;
    return Number(cm.getParameterValueByIndex(index)) || 0;
  }

  function paramDefault(id, fallback) {
    const cm = coreModel();
    const index = paramIndex(id);
    if (!cm || index === undefined) return fallback;
    return Number(cm.getParameterDefaultValue(index)) || 0;
  }

  function paramBounds(id) {
    const cm = coreModel();
    const index = paramIndex(id);
    if (!cm || index === undefined) return [-Infinity, Infinity];
    return [
      Number(cm.getParameterMinimumValue(index)),
      Number(cm.getParameterMaximumValue(index)),
    ];
  }

  function setParam(id, value) {
    const cm = coreModel();
    const index = paramIndex(id);
    if (!cm || index === undefined) return false;
    cm.setParameterValueByIndex(index, Number(value));
    return true;
  }

  // ── 布局 ──────────────────────────────────────────
  function applyLayout() {
    const root = document.documentElement.style;
    root.setProperty('--bubble-gap', LAYOUT.gap + 'vh');
    root.setProperty('--bubble-max-width', LAYOUT.maxWidth + 'vw');
    root.setProperty('--avatar-size', LAYOUT.size + 'vh');
    root.setProperty('--avatar-lift', LAYOUT.lift + 'vh');
    if (!LAYOUT.autoGap && bubbleEl) bubbleEl.style.marginBottom = '';
  }

  function applyConfigLayout(l) {
    if (!l || typeof l !== 'object') return;
    const take = (key, qkey, lo, hi) => {
      if (qs.has(qkey) || l[key] === null || l[key] === undefined) return;
      const n = Number(l[key]);
      if (Number.isFinite(n)) LAYOUT[key] = clamp(n, lo, hi);
    };
    take('gap', 'gap', -50, 50);
    take('maxWidth', 'w', 20, 100);
    if (!qs.has('auto') && typeof l.autoGap === 'boolean') LAYOUT.autoGap = l.autoGap;
    applyLayout();
  }

  // ── 能力表 ────────────────────────────────────────
  function normalizeCaps(cfg) {
    const c = (cfg && cfg.capabilities) || {};
    const actions = Array.isArray(c.actions) ? c.actions : [];
    const expressions = Array.isArray(c.expressions) ? c.expressions : [];
    const caps = {
      actions: actions.filter((x) => x && x.tag && x.group),
      expressions: expressions.filter((x) => x && x.tag && x.name),
      idle: String(c.idle || ''),
      lipSync: Array.isArray(c.lipSync) && c.lipSync.length ? c.lipSync : ['ParamMouthOpenY'],
    };
    S.caps = caps;
    S.autonomy = !PREVIEW && (!cfg || !cfg.autonomy
      || (cfg.autonomy.enabled !== false && cfg.autonomy.active !== false));
    ACTION_MAP = new Map();
    for (const a of caps.actions) {
      ACTION_MAP.set(String(a.tag), String(a.group));
      ACTION_MAP.set(String(a.group).toLowerCase(), String(a.group));
    }
    EXPR_MAP = new Map();
    for (const e of caps.expressions) {
      EXPR_MAP.set(String(e.tag), String(e.name));
      EXPR_MAP.set(String(e.name).toLowerCase(), String(e.name));
    }
    IDLE_GROUP = caps.idle;
    LIP_PARAMS = caps.lipSync.slice();
  }

  // ── 气泡 ──────────────────────────────────────────
  function typeText(t) {
    clearInterval(S.typeTimer);
    textEl.textContent = '';
    const step = Math.min(26, Math.max(6, 600 / Math.max(1, t.length)));
    let n = 0;
    S.typeTimer = setInterval(() => {
      n += 1;
      textEl.textContent = t.slice(0, n);
      if (n >= t.length) clearInterval(S.typeTimer);
    }, step);
  }

  function showStatus(text) {
    clearInterval(S.typeTimer);
    clearTimeout(S.pageTimer);
    clearTimeout(S.holdTimer);
    clearTimeout(S.fadeTimer);
    S.pages = [];
    S.idx = -1;
    S.speaking = false;
    if (!text) {
      textEl.textContent = '';
      bubbleEl.classList.remove('on');
      return;
    }
    bubbleEl.classList.add('on');
    textEl.textContent = text;
  }

  function showPage(i) {
    if (!S.pages.length) return;
    i = Math.max(0, Math.min(i, S.pages.length - 1));
    S.idx = i;
    const text = S.pages[i];
    typeText(text);
    clearTimeout(S.pageTimer);
    // sync 模式由 Speaker 的音频时间轴发 page 事件；前端再用 cps 猜一份，
    // 两个时钟会互相抢页（尤其是一句长、一句短的时候）。
    if (!S.sync && i + 1 < S.pages.length) {
      const dur = Math.max(0.45, text.length / Math.max(2, S.cps));
      S.pageTimer = setTimeout(() => showPage(i + 1), dur * 1000);
    }
  }

  function speak(msg) {
    const pages = (msg.pages || []).filter((p) => p && p.length);
    if (!pages.length) return;
    S.pages = pages;
    S.idx = -1;
    S.sync = !!msg.sync;
    if (msg.cps) S.cps = msg.cps;
    S.speaking = true;
    clearTimeout(S.holdTimer);
    clearTimeout(S.fadeTimer);
    bubbleEl.classList.add('on');
    showPage(msg.idx || 0);
    interactOnSpeak(msg.acts || []);
  }

  function finish(msg) {
    clearTimeout(S.pageTimer);
    S.speaking = false;
    interactOnEnd();
    const total = S.pages.length;
    const upto = Math.max(1, Math.min(Number((msg && msg.upto) || total) || total, total));
    if (total && S.idx < upto - 1) showPage(upto - 1);
    clearTimeout(S.pageTimer);
    const last = S.pages[S.idx] || S.pages[total - 1] || '';
    const hold = Math.max(1500, last.length * 60);
    clearTimeout(S.holdTimer);
    S.holdTimer = setTimeout(() => {
      bubbleEl.classList.remove('on');
      S.fadeTimer = setTimeout(() => { textEl.textContent = ''; }, 400);
    }, hold);
  }

  function reset() {
    clearTimeout(S.pageTimer);
    clearTimeout(S.holdTimer);
    clearTimeout(S.fadeTimer);
    clearInterval(S.typeTimer);
    S.speaking = false;
    S.sync = false;
    S.level = 0;
    S.mouth = 0;
    S.mouthOpen = 0;
    S.mouthForm = 0;
    S.mouthAt = 0;
    S.pose.clear();
    S.poseAdd.clear();
    S.prevPoseKeys.clear();
    S.handover.clear();
    S.expressions.clear();
    S.expressionAt = 0;
    S.poseSeq = 0;
    S.pages = [];
    S.idx = -1;
    bubbleEl.classList.remove('on');
    textEl.textContent = '';
    interactOnEnd();
  }

  // ── 气泡自动贴头顶 ────────────────────────────────
  const anchor = { at: 0, cur: null, target: null };

  // 复用同一块离屏 canvas：一次加载这个函数要被调好几次，
  // 每次 createElement 都是新纹理 + GC 压力。
  const PROBE = { canvas: null, ctx: null, size: 96 };
  function alphaBounds(source) {
    if (!source || !source.width || !source.height) return null;
    try {
      if (!PROBE.canvas) {
        PROBE.canvas = document.createElement('canvas');
        PROBE.canvas.width = PROBE.canvas.height = PROBE.size;
        PROBE.ctx = PROBE.canvas.getContext('2d', { willReadFrequently: true });
      }
      const n = PROBE.size;
      PROBE.ctx.clearRect(0, 0, n, n);
      PROBE.ctx.drawImage(source, 0, 0, n, n);
      const d = PROBE.ctx.getImageData(0, 0, n, n).data;
      let x0 = n, y0 = n, x1 = -1, y1 = -1;
      for (let y = 0; y < n; y += 1) {
        for (let x = 0; x < n; x += 1) {
          if (d[(y * n + x) * 4 + 3] <= 16) continue;
          if (x < x0) x0 = x;
          if (y < y0) y0 = y;
          if (x > x1) x1 = x;
          if (y > y1) y1 = y;
        }
      }
      if (x1 < 0) return null;
      return { x0: x0 / n, y0: y0 / n, x1: (x1 + 1) / n, y1: (y1 + 1) / n };
    } catch (e) { /* preserveDrawingBuffer / 模型未就绪 */ }
    return null;
  }

  // 这里原本有个 drawableTop()：手算 localTransform ∘ worldTransform 求最上顶点。
  // 但本项目打的 0.4.0 cubism4 构建里，getDrawableVertices() 是“原样透传 core 顶点”
  // （Y 向上、模型单位），真正画到屏幕时的 Y 取反发生在 draw() 组装 4×4 那一步。
  // 手算出来的 min(y) 其实是模型**底边**——回读一旦失败，气泡会被送到脚底下。
  // 现在只用 fit 时量到的包围盒（S.content）：量不到就不自动贴，
  // 退回 config 的 gap（等价于 ?auto=0）。宁可不贴，也不贴错。
  function measureAvatarTop() {
    // Live2D 与 Spine 都把「内容包围盒」归一化到形象区高度；图片按实际渲染位置算。
    if ((L2D || window.SP) && S.content && Number.isFinite(S.content.y0)) {
      return S.content.y0 * (avatarEl.clientHeight || 0);
    }
    if (IMG && IMG.naturalWidth && IMG.naturalHeight) {
      const r = IMG.getBoundingClientRect();
      const a = avatarEl.getBoundingClientRect();
      const k = Math.min(r.width / IMG.naturalWidth,
                         r.height / IMG.naturalHeight);
      const h = IMG.naturalHeight * k;
      return (r.top - a.top) + Math.max(0, r.height - h);
    }
    return null;
  }

  function setAutoOffset(top) {
    const h = window.innerHeight || 1;
    const gap = LAYOUT.gap * h / 100;
    bubbleEl.style.marginBottom = (gap - top).toFixed(2) + 'px';
  }

  function updateBubbleAnchor(now) {
    if (!LAYOUT.autoGap) {
      if (anchor.cur !== null || anchor.target !== null) {
        anchor.cur = anchor.target = null;
        bubbleEl.style.marginBottom = '';
      }
      return;
    }
    if (!anchor.at || now - anchor.at > 400) {
      anchor.at = now;
      const top = measureAvatarTop();
      anchor.target = Number.isFinite(top) ? top : null;
    }
    if (anchor.target === null) {
      if (anchor.cur !== null) {
        anchor.cur = null;
        bubbleEl.style.marginBottom = '';
      }
      return;
    }
    if (anchor.cur === null) anchor.cur = anchor.target;
    else anchor.cur += (anchor.target - anchor.cur) * 0.18;
    setAutoOffset(anchor.cur);
  }

  setInterval(() => updateBubbleAnchor(performance.now()), 700);

  // ── Live2D ────────────────────────────────────────
  const MOTION_PRIORITY = { IDLE: 1, FORCE: 3 };

  function currentMotion() {
    if (S.renderer === 'spine' && window.SP) return window.SP.playing() || '-';
    try { return L2D.model.internalModel.motionManager.state.currentGroup || '-'; }
    catch (e) { return '-'; }
  }

  function l2dMotion(name) {
    if (!L2D) return false;
    const group = ACTION_MAP.get(name) || ACTION_MAP.get(String(name).toLowerCase()) || name;
    if (IDLE_GROUP && group === IDLE_GROUP) clearExpressions();
    try {
      const started = L2D.model.motion(group, undefined, MOTION_PRIORITY.FORCE);
      if (started && typeof started.then === 'function') {
        return started.then((ok) => {
          if (!ok) S.err = 'motion(' + name + '): 引擎拒绝启动';
          return !!ok;
        }).catch((e) => {
          S.err = 'motion(' + name + '): ' + e.message;
          return false;
        });
      }
      return true;
    } catch (e) {
      S.err = 'motion(' + name + '): ' + e.message;
      return false;
    }
  }

  // ── 多表情混音器 ──────────────────────────────────
  // SDK 的 ExpressionManager 同时只能激活一个表情，切换时还会硬切。这里直接
  // 读取 exp3 的参数（Add/Multiply/Overwrite），每个表情独立淡入、保持、淡出，
  // 因此“脸红 + 爱心眼”可以叠加，也不会被 6 秒后的 ResetExpression 一刀切掉。
  function cubismIdString(param) {
    try {
      const s = param && param.parameterId && param.parameterId.getString
        ? param.parameterId.getString() : '';
      return (s && (s.s || s)) || '';
    } catch (e) {
      return '';
    }
  }

  function playExpression(name) {
    if (!L2D) return false;
    const em = L2D.model.internalModel.motionManager.expressionManager;
    if (!em) return false;
    const real = EXPR_MAP.get(name) || EXPR_MAP.get(String(name).toLowerCase()) || name;
    const defs = em.definitions || [];
    const idx = defs.findIndex((d) => d && d.Name === real);
    if (idx < 0) return false;
    const active = S.expressions.get(real);
    if (active) {
      active.hold = 6000;
      active.target = 1;
      return true;
    }
    const entry = {
      params: [], weight: 0, target: 1, hold: 6000,
      fadeIn: 0.35, fadeOut: 0.55,
    };
    S.expressions.set(real, entry);
    Promise.resolve(em.loadExpression(idx)).then((exp) => {
      if (!exp || !S.expressions.has(real)) return;
      try {
        if (typeof exp.getFadeInTime === 'function') {
          entry.fadeIn = Math.max(0.08, Math.min(0.6, Number(exp.getFadeInTime()) || 0.35));
        }
        if (typeof exp.getFadeOutTime === 'function') {
          entry.fadeOut = Math.max(0.08, Math.min(0.8, Number(exp.getFadeOutTime()) || 0.55));
        }
        const vec = exp.getExpressionParameters();
        for (let i = 0; i < vec.getSize(); i += 1) {
          const p = vec.at(i);
          const id = cubismIdString(p);
          if (!id) continue;
          entry.params.push({
            id, value: Number(p.value) || 0,
            blend: Number(p.blendType) || 0,
          });
        }
      } catch (e) {
        console.warn('[overlay] expression params', real, e);
      }
    }).catch((e) => console.warn('[overlay] expression', real, e));
    return true;
  }

  function clearExpressions() {
    for (const entry of S.expressions.values()) {
      entry.hold = 0;
      entry.target = 0;
    }
  }

  function parameterDefault(_cm, id, fallback) {
    return paramDefault(id, fallback);
  }

  function applyExpressionMixer(now) {
    if (!L2D || !S.expressions.size) return;
    const at = Number(now) || performance.now();
    const dt = Math.min(0.2, Math.max(0, (at - (S.expressionAt || at)) / 1000));
    S.expressionAt = at;
    const cm = L2D.model.internalModel.coreModel;
    for (const [name, entry] of S.expressions) {
      if (entry.hold > 0) entry.hold = Math.max(0, entry.hold - dt * 1000);
      const target = entry.hold > 0 ? 1 : 0;
      const tau = target ? entry.fadeIn : entry.fadeOut;
      entry.weight += (target - entry.weight) * Math.min(1, dt / Math.max(0.08, tau));
      if (target === 0 && entry.weight < 0.01) {
        S.expressions.delete(name);
        continue;
      }
      if (entry.weight <= 0.001) continue;
      for (const p of entry.params) {
        if (LIP_PARAMS.includes(p.id) && (S.speaking || S.mouth > 0.02)) continue;
        try {
          let cur = paramValue(p.id);
          // 表情参数如果没有被 pose/remote 每帧重写，就回到模型 default 再叠，
          // 否则 Add 会在上一帧结果上继续累加。
          if (REMOTE.params.has(p.id)) {
            cur = Number(REMOTE.params.get(p.id)) || 0;
          } else if (!S.pose.has(p.id)) {
            cur = parameterDefault(cm, p.id, cur);
          }
          let next;
          if (p.blend === 1) {
            next = cur * (1 + (p.value - 1) * entry.weight);
          } else if (p.blend === 2) {
            next = cur + (p.value - cur) * entry.weight;
          } else {
            next = cur + p.value * entry.weight;
          }
          const [min, max] = paramBounds(p.id);
          setParam(p.id, Math.max(min, Math.min(max, next)));
        } catch (e) {}
      }
    }
  }

  /** 同时支持动作和表情；缺失的一层自然跳过。 */
  function act(name) {
    if (!name) return false;
    if (S.renderer === 'spine') {
      if (!window.SP) return false;
      if (!ACTION_MAP.has(name) && !ACTION_MAP.has(String(name).toLowerCase())) return false;
      return window.SP.play(name, false);
    }
    if (ACTION_MAP.has(name) || ACTION_MAP.has(String(name).toLowerCase())) {
      return l2dMotion(name);
    }
    if (EXPR_MAP.has(name) || EXPR_MAP.has(String(name).toLowerCase())) {
      return playExpression(name);
    }
    return false;
  }

  function applyActs(acts) {
    if (!acts || !acts.length) return false;
    const motions = [];
    const expressions = [];
    let semantic = false;
    for (const a of acts) {
      const kind = String(a.kind || '');
      const name = String(a.name || '');
      if (kind === 'emotion' || kind === 'action' || kind === 'gaze') {
        // 这些由 runtime 的 MotionEngine 驱动，浏览器不要抢着随机播表情。
        semantic = true;
        continue;
      }
      const isExpr = kind === 'expression' || (!kind && EXPR_MAP.has(name) && !ACTION_MAP.has(name));
      if (isExpr) {
        if (EXPR_MAP.has(name) || EXPR_MAP.has(name.toLowerCase())) expressions.push(name);
        continue;
      }
      if (ACTION_MAP.has(name) || ACTION_MAP.has(name.toLowerCase())) {
        motions.push(name);
      } else if (EXPR_MAP.has(name) || EXPR_MAP.has(name.toLowerCase())) {
        expressions.push(name);
      }
    }
    for (const name of motions) act(name);
    if (S.renderer === 'spine') return semantic || motions.length > 0;
    for (const name of expressions) playExpression(name);
    return semantic || motions.length > 0 || expressions.length > 0;
  }

  function hasActions() { return S.caps.actions.length > 0; }
  function hasExpressions() { return S.caps.expressions.length > 0; }

  function randomGesture() {
    if (hasActions()) return act(pick(S.caps.actions).tag);
    if (hasExpressions()) return act(pick(S.caps.expressions).tag);
    return false;
  }

  function interactOnSpeak(acts) {
    if (applyActs(acts)) return;
    // 自主驱动的 pose 才是动作来源；不要再用浏览器随机资产盖掉它。
    if (S.autonomy && S.renderer === 'live2d') return;
    if (S.renderer === 'spine') return;   // Spine 不随机播游戏拆包动画
    if (hasExpressions()) act(pick(S.caps.expressions).tag);
    else if (hasActions() && Math.random() < 0.18) act(pick(S.caps.actions).tag);
  }

  function interactOnEnd() {
    return;
  }

  function initLive2D(url) {
    if (!window.PIXI || !PIXI.live2d) throw new Error('live2d engine 没加载');
    if (!window.Live2DCubismCore) throw new Error('Cubism Core 没加载');
    REMOTE.params.clear();
    REMOTE.parts.clear();

    // PIXI v8：Live2D 的 render pipe 必须在创建 renderer 之前注册
    if (PIXI.extensions && PIXI.live2d.Live2DPlugin) {
      PIXI.extensions.add(PIXI.live2d.Live2DPlugin);
    }

    // v8：先构造，再 init（renderer 是在 init 里建的）
    const app = new PIXI.Application();
    return app.init({
      backgroundAlpha: 0,
      // 预览只限 30fps；画质保持 MSAA 与设备像素比，避免为了省 GPU 把模型糊掉。
      antialias: true,
      autoStart: true,
      autoDensity: true,
      resolution: Math.min(2, window.devicePixelRatio || 1),
      resizeTo: avatarEl,
      preserveDrawingBuffer: true,   // 还要读画布像素做 autoFit
      preference: 'webgl',           // Live2D 的 RenderPipe 是 WebGL 的，别落到 WebGPU
      powerPreference: PREVIEW ? 'low-power' : 'high-performance',
    }).then(() => {
      if (PREVIEW) app.ticker.maxFPS = 30;
      // v8 的 canvas 由 PIXI 创建：用它顶替静态 <canvas>，现有 CSS 继续生效
      canvasEl.replaceWith(app.canvas);
      app.canvas.id = 'canvas';
      canvasEl = app.canvas;
      return PIXI.live2d.Live2DModel.from(url, {
        autoInteract: false,
        textureOptions: { lod: 'single-auto' },   // 4096 图集省显存
      });
    }).then((model) => {
      app.stage.addChild(model);
      const im = model.internalModel;
      PARAM_INDEX.clear();
      try {
        const cm = im.coreModel;
        const count = Number(cm.getParameterCount()) || 0;
        for (let i = 0; i < count; i += 1) {
          const cid = cm.getParameterId(i);
          const text = cid && cid.getString ? cid.getString() : '';
          const name = text && (text.s || text);
          if (name) PARAM_INDEX.set(String(name), i);
        }
      } catch (e) {
        console.warn('[overlay] parameter index', e);
      }
      const natW = im.width || model.width || 1024;
      const natH = im.height || model.height || 1024;
      // Inspector 用：整体大小 / 位置，叠在 fit 的结果之上（不改模型文件）
      const VIEW = { zoom: 1, offX: 0, offY: 0, baseScale: 1, baseX: 0, baseY: 0 };
      const applyView = () => {
        const s = VIEW.baseScale * VIEW.zoom;
        model.scale.set(s);
        // 以模型实际内容的中心为锚点缩放，避免放大时向右/向下漂。
        const W = app.screen.width;
        const H = app.screen.height;
        const c = S.content;
        const ax = c ? (c.x0 + c.x1) * 0.5 * W : W / 2;
        const ay = c ? (c.y0 + c.y1) * 0.5 * H : H / 2;
        model.x = ax - (ax - VIEW.baseX) * VIEW.zoom + VIEW.offX;
        model.y = ay - (ay - VIEW.baseY) * VIEW.zoom + VIEW.offY;
      };
      const snapshotView = () => {
        VIEW.baseScale = model.scale.x;
        VIEW.baseX = model.x - VIEW.offX;
        VIEW.baseY = model.y - VIEW.offY;
        applyView();
      };
      const fit = () => {
        const W = app.screen.width;
        const H = app.screen.height;
        if (!W || !H) return;
        const s = Math.min(W / natW, H / natH);
        model.scale.set(s);
        model.x = (W - natW * s) / 2;
        model.y = H - natH * s;
        snapshotView();
      };
      // 不同模型的画布空白比例差别很大（有些模型画布比角色大很多），
      // 先按画布 fit 会看起来很小。这里读 WebGL 画布 alpha 的实际包围盒再缩放一次，
      // 让“丢进文件夹就能用”的模型自动填满舞台。
      //
      // 缩放是绕模型局部原点（左上）做的，所以内容包围盒同比放大 k 倍，
      // 位移可以直接算出来——不需要调 model.updateTransform()（PIXI v8 里它会抛错），
      // 也不需要第二次读像素。
      const autoFitContent = () => {
        const b = alphaBounds(app.canvas);
        if (!b) { S.fitInfo = 'nobounds'; S.content = null; return; }
        const W = app.screen.width;
        const H = app.screen.height;
        const w = (b.x1 - b.x0) * W;
        const h = (b.y1 - b.y0) * H;
        if (!(w > 1) || !(h > 1)) { S.fitInfo = 'empty ' + w.toFixed(0) + 'x' + h.toFixed(0); return; }
        // 桌面窗口很窄，左右留更多边；直播舞台按 92% 填满。
        // WebUI 预览要留出放大余量，否则 1.2x 就碰到 iframe 边界。
        const fill = PREVIEW ? 0.78 : 0.92;
        const k = Math.min((W * fill) / w, (H * fill) / h) * zoomOf();
        if (!Number.isFinite(k) || k <= 0.05) { S.fitInfo = 'k-bad ' + k; return; }
        model.scale.set(model.scale.x * k, model.scale.y * k);
        // 缩放后包围盒 = b*k；再平移到“水平居中、底部贴 99%”
        const dx = 0.5 - ((b.x0 + b.x1) / 2) * k;
        const dy = 0.99 - b.y1 * k;
        model.x += dx * W;
        model.y += dy * H;
        // 记下最终包围盒：气泡贴头顶、desktop 点击区都直接用它。
        S.content = {
          x0: b.x0 * k + dx, y0: b.y0 * k + dy,
          x1: b.x1 * k + dx, y1: b.y1 * k + dy,
        };
        snapshotView();
        // 初始取景只记录一次；复位用这份快照，不能按当前动作重新算
        // alpha 包围盒，否则同一个按钮会在两个位置之间来回跳。
        if (!S.initialView) {
          S.initialView = {
            scale: model.scale.x, x: model.x, y: model.y,
            content: { ...S.content },
          };
        }
        // 给调试角标留一行；普通模式不输出。
        S.fitInfo = 'k=' + k.toFixed(2) + ' s=' + model.scale.x.toFixed(2) +
          ' y=' + model.y.toFixed(0) + ' ' + W.toFixed(0) + 'x' + H.toFixed(0) +
          ' b=' + [b.x0, b.y0, b.x1, b.y1].map((v) => v.toFixed(2)).join(',');
      };

      fit();
      // 首帧之后再按“实际有像素的包围盒”缩放一次。纹理/首帧慢的时候会量到空画布，
      // 所以最多重试 6 次；量到了（S.content 有值）就停。
      let fitTries = 0;
      const fitContent = () => {
        fitTries += 1;
        try { fit(); } catch (e) { S.fitInfo = 'fit-err: ' + (e && (e.stack || e.message)); }
        try { autoFitContent(); } catch (e) {
          S.fitInfo = 'autofit-err: ' + String(e && (e.stack || e.message)).slice(0, 240);
        }
        if (!S.content && fitTries < 6) setTimeout(fitContent, 250);
      };
      requestAnimationFrame(fitContent);
      window.addEventListener('resize', () => {
        fit();
        setTimeout(() => { try { autoFitContent(); } catch (e) {} }, 120);
      });
      const mm = im.motionManager;
      if (S.autonomy) {
        // 自主驱动自己写眨眼/呼吸；库的同类更新置空，避免未绑定参数被双重驱动。
        // pixi-live2d-display 的实际入口是 updateParameters，不是 update。
        try {
          if (im.eyeBlink) im.eyeBlink.updateParameters = () => {};
          if (im.breath) im.breath.updateParameters = () => {};
        } catch (e) {}
        // 与桌面自主模式一致：SDK 的 idle motion 不能和 pose 抢参数。
        // 否则模型自带的“圈圈眼/加载”一类 idle 动作会在浏览器里随机出现。
        try { mm.stopAllMotions(); } catch (e) {}
      }
      if (IDLE_GROUP && !S.autonomy) {
        try { mm.startRandomMotion(IDLE_GROUP, MOTION_PRIORITY.IDLE); } catch (e) {}
      }
      // 基础姿态在 physics.evaluate 之前写一次，让头发/裙/胸在同帧读到新的
      // 头身参数；physics 之后再写一次，保证最终值不被物理输出覆盖。
      // 这里没有 pixi-live2d-display 的 afterMotionUpdate 钩子，包装
      // physics.evaluate 等价于“motion 之后、physics 之前”。
      const writeBaseParams = () => {
        try {
          // 没有 idle motion 的模型也要有一点呼吸，否则像贴图。
          if (!IDLE_GROUP && !S.autonomy) {
            const t = performance.now() / 1000;
            setParam('ParamBreath', 0.5 + 0.18 * Math.sin(t * 1.4));
          }
          for (const [id, value] of REMOTE.parts) {
            try { im.coreModel.setPartOpacityById(id, Number(value)); } catch (e) {}
          }
          if (S.pose.size) {
            const nowMs = performance.now();
            for (const [id, value] of S.pose) {
              let target = Number(value);
              if (S.prevPoseKeys.size && !S.prevPoseKeys.has(id)) {
                try {
                  const cur = paramValue(id);
                  if (Number.isFinite(cur)) {
                    S.handover.set(id, { from: cur, at: nowMs });
                  }
                } catch (e) {}
              }
              const h = S.handover.get(id);
              if (h) {
                const alpha = Math.min(1, (nowMs - h.at) / 320);
                const u = alpha * alpha * (3 - 2 * alpha);
                target = h.from + (target - h.from) * u;
                if (alpha >= 1) S.handover.delete(id);
              }
              try { setParam(id, target); } catch (e) {}
            }
            S.prevPoseKeys = new Set(S.pose.keys());
            for (const id of S.handover.keys()) {
              if (!S.pose.has(id)) S.handover.delete(id);
            }
            S.poseWrites += 1;
          }
          // 远程/Inspector 的写入放在 pose 之后：它们是“手动覆盖”，
          // 不能被自主驱动的每帧 pose 盖回去。
          for (const [id, value] of REMOTE.params) {
            try { setParam(id, Number(value)); } catch (e) {}
          }
          // 没有 pose 时才退回旧口型帧，避免同一帧写两次嘴。
          if (!S.pose.size && (S.speaking || S.mouth > 0.02)) {
            for (const p of LIP_PARAMS) setParam(p, S.mouth);
          }
        } catch (e) {}
      };
      // 物理叶结点的增量只能在 physics.evaluate 之后叠加：此时读当前物理值，
      // 与桌面 Live2DWriter 的 phys_now + delta*0.80 等价，且不会在 pre 阶
      // 段重复叠加一次。
      const writePhysicsAdd = () => {
        if (!S.poseAdd.size) return;
        for (const [id, delta] of S.poseAdd) {
          try {
            const cur = paramValue(id);
            const next = cur + Number(delta) * 0.80;
            const [min, max] = paramBounds(id);
            setParam(id, Math.max(min, Math.min(max, next)));
          } catch (e) {}
        }
      };
      const prePhysics = window.__LM_PRE_PHYSICS
        || (window.__LM_PRE_PHYSICS = []);
      prePhysics.push(writeBaseParams);
      const physics = im.physics;
      if (physics && typeof physics.evaluate === 'function'
          && !physics.__deepsidekickPrePhysics) {
        const evaluate = physics.evaluate.bind(physics);
        physics.evaluate = function (target, dt) {
          for (const cb of prePhysics) {
            try { cb(); } catch (e) {}
          }
          return evaluate(target, dt);
        };
        physics.__deepsidekickPrePhysics = true;
      }
      im.on('beforeModelUpdate', () => {
        writeBaseParams();
        // 表情在基础姿态之后叠加，避免被 pose 的 eye/mouth 覆盖；物理叶结点
        // 的增量最后写，保持“物理当前值 + delta”。
        applyExpressionMixer(performance.now());
        writePhysicsAdd();
      });
      L2D = { app, model, fit };
      // 给 Inspector（inspect.js）用的只读入口 + 缩放/位置调整
      window.LMView = {
        model: model,
        app: app,
        idle: IDLE_GROUP,
        get state() {
          const W = app.screen.width || 1;
          const H = app.screen.height || 1;
          return {
            zoom: +VIEW.zoom.toFixed(2),
            normX: +(VIEW.offX / W).toFixed(3),
            normY: +(VIEW.offY / H).toFixed(3),
            baseScale: +VIEW.baseScale.toFixed(4),
          };
        },
        setZoom(z) {
          const n = Number(z);
          VIEW.zoom = Math.min(4, Math.max(0.2, Number.isFinite(n) ? n : 1));
          applyView();
        },
        setOffsetNorm(nx, ny) {
          VIEW.offX = (Number(nx) || 0) * app.screen.width;
          VIEW.offY = (Number(ny) || 0) * app.screen.height;
          applyView();
        },
        refit() {
          VIEW.zoom = 1; VIEW.offX = 0; VIEW.offY = 0;
          fit();
          try { autoFitContent(); } catch (e) {}
          if (S.content) {
            S.initialView = {
              scale: model.scale.x, x: model.x, y: model.y,
              content: { ...S.content },
            };
          }
        },
        resetView() {
          const v = S.initialView;
          if (!v) {
            this.refit();
            return;
          }
          VIEW.zoom = 1;
          VIEW.offX = 0;
          VIEW.offY = 0;
          VIEW.baseScale = v.scale;
          VIEW.baseX = v.x;
          VIEW.baseY = v.y;
          model.scale.set(v.scale);
          model.x = v.x;
          model.y = v.y;
          S.content = { ...v.content };
        },
      };
      window.LMControl = {
        param(id, value) {
          if (value === null || value === undefined) REMOTE.params.delete(id);
          else REMOTE.params.set(String(id), Number(value));
        },
        part(id, value) {
          if (value === null || value === undefined) REMOTE.parts.delete(id);
          else REMOTE.parts.set(String(id), Number(value));
        },
        motion(group, index) {
          try { model.motion(String(group), Number(index) || 0, 3); } catch (e) {}
        },
        expression(name) {
          playExpression(name);
        },
        view(zoom, offX, offY) {
          const s = window.LMView.state;
          if (zoom !== null && zoom !== undefined) window.LMView.setZoom(zoom);
          if ((offX !== null && offX !== undefined)
              || (offY !== null && offY !== undefined)) {
            window.LMView.setOffsetNorm(
              offX !== null && offX !== undefined ? offX : s.normX,
              offY !== null && offY !== undefined ? offY : s.normY);
          }
        },
        reset() {
          REMOTE.params.clear();
          REMOTE.parts.clear();
          S.expressions.clear();
          S.handover.clear();
          S.prevPoseKeys.clear();
          try {
            L2D.model.internalModel.motionManager.stopAllMotions();
          } catch (e) {}
          if (IDLE_GROUP && !S.autonomy) {
            try {
              L2D.model.internalModel.motionManager.startRandomMotion(
                IDLE_GROUP, MOTION_PRIORITY.IDLE);
            } catch (e) {}
          }
          window.LMView.resetView();
        },
      };
      applyLayout();
      document.body.classList.remove('r-image');
      document.body.classList.add('r-live2d');
      return model;
    });
  }

  function initImage(cfg) {
    IMG = imageEl;
    S.renderer = 'image';
    document.body.classList.remove('r-live2d', 'inspect');
    document.body.classList.add('r-image');
    applyLayout();
    IMG.alt = String(cfg.name || cfg.avatar || '');
    IMG.onerror = () => { S.err = '图片加载失败：' + cfg.model; };
    IMG.src = cfg.model;
    return Promise.resolve(IMG);
  }

  function initSpine(cfg) {
    S.renderer = 'spine';
    const base = STATIC_BASE;
    return loadScript(base + '/spine-player.js').then(() => {
      if (!window.SP) throw new Error('spine-player 没挂上 SP');
      return window.SP.init(cfg, {
        base: base,
        onError: (m) => { S.err = m; },
      });
    }).then(() => {
      document.body.classList.remove('r-image', 'r-live2d');
      document.body.classList.add('r-spine');
      applyLayout();
      // 气泡贴头顶复用同一套包围盒（与 Live2D 的 S.content 同语义）
      S.content = window.SP.content();
      S.fitInfo = 'spine ' + (window.SP.fitInfo || '');
      setInterval(() => {
        const c = window.SP.content();
        if (c) S.content = c;
      }, 400);
      return window.SP;
    });
  }

  function applyControl(msg) {
    if (msg.op === 'reload') {
      location.reload();
      return;
    }
    const ctl = S.renderer === 'spine' ? (window.SP && window.SP.control)
      : window.LMControl;
    if (!ctl) return;
    switch (msg.op) {
      case 'param': if (ctl.param) ctl.param(msg.id, msg.value); break;
      case 'part': if (ctl.part) ctl.part(msg.id, msg.value); break;
      case 'motion': if (ctl.motion) ctl.motion(msg.group, msg.index); break;
      case 'expression': if (ctl.expression) ctl.expression(msg.name); break;
      case 'view': if (ctl.view) ctl.view(msg.zoom, msg.offX, msg.offY); break;
      case 'reset': if (ctl.reset) ctl.reset(); break;
      default: break;
    }
  }

  // ── WS ────────────────────────────────────────────
  function onMessage(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch (e) { return; }
    switch (msg.t) {
      case 'status': showStatus(msg.text || ''); break;
      case 'say': speak(msg); break;
      case 'page':
        if (S.pages.length) {
          const nextIdx = Number(msg.idx) || 0;
          if (msg.acts && msg.acts.length) applyActs(msg.acts);
          if (nextIdx > S.idx) {
            clearTimeout(S.pageTimer);
            showPage(nextIdx);
          }
        }
        break;
      case 'level': S.level = Math.max(0, Math.min(1, Number(msg.v) || 0)); break;
      case 'mouth':
        S.mouthOpen = Math.max(0, Math.min(1, Number(msg.open) || 0));
        S.mouthForm = Math.max(-1, Math.min(1, Number(msg.form) || 0));
        S.mouthAt = performance.now();
        break;
      case 'pose':
        if (Number(msg.seq) > S.poseSeq) {
          S.poseSeq = Number(msg.seq) || 0;
          S.pose = new Map(Object.entries(msg.params || {}));
          S.poseAdd = new Map(Object.entries(msg.add || {}));
        }
        break;
      case 'end': finish(msg); break;
      case 'event': onStageEvent(msg); break;
      case 'control': applyControl(msg); break;
      case 'hello': document.body.classList.remove('offline'); break;
      default: break;
    }
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    let ws;
    try { ws = new WebSocket(proto + '://' + location.host + '/ws'); }
    catch (e) { setTimeout(connect, 1500); return; }
    ws.onopen = () => {
      document.body.classList.remove('offline');
      S.sock = ws;
      report();
    };
    ws.onmessage = (ev) => onMessage(ev.data);
    ws.onclose = () => {
      S.sock = null;
      document.body.classList.add('offline');
      reset();
      setTimeout(connect, 1500);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  function report() {
    const ws = S.sock;
    if (!ws || ws.readyState !== 1) return;
    try {
      ws.send(JSON.stringify({
        t: 'report', renderer: S.renderer, err: S.err,
        maxMouth: +S.maxMouth.toFixed(2), level: +S.level.toFixed(3),
        mouth: +S.mouth.toFixed(3), form: +S.mouthForm.toFixed(3),
        poseSeq: S.poseSeq, poseParams: S.pose.size,
        poseWrites: S.poseWrites, autonomy: S.autonomy ? 1 : 0,
        animation: S.renderer === 'spine' && window.SP ? window.SP.playing() : currentMotion(),
        page: S.idx + 1, pages: S.pages.length, cps: +S.cps.toFixed(2),
        w: innerWidth, h: innerHeight, avatar: S.avatar ? S.avatar.id : '',
      }));
      S.maxMouth = 0;
    } catch (e) {}
  }
  setInterval(report, 5000);

  function onStageEvent(msg) {
    if (S.renderer === 'spine') return;   // 不把礼物随机映射成 Attack/Die
    const wanted = msg.kind === 'gift' ? ['爱心', '开心', '星星'] : ['开心', '问号'];
    const expr = S.caps.expressions.find((e) => wanted.some((w) => e.tag.includes(w)));
    if (expr) act(expr.tag);
    else if (S.caps.expressions.length) act(pick(S.caps.expressions).tag);
    else if (S.caps.actions.length) act(pick(S.caps.actions).tag);
  }

  // ── 调试 API ──────────────────────────────────────
  window.LM = {
    say(text, cps) {
      const acts = [];
      const body = String(text).replace(/<ACT\b[^>]*>([\s\S]*?)<\/ACT\s*>/gi, (m, payload) => {
        for (const token of String(payload).split(/[,，;；\s]+/)) {
          const at = token.indexOf('=');
          if (at < 0) continue;
          const key = token.slice(0, at).trim().toLowerCase();
          let value = token.slice(at + 1).trim();
          const colon = Math.max(value.lastIndexOf(':'), value.lastIndexOf('：'));
          if (colon > 0 && Number.isFinite(Number(value.slice(colon + 1)))) {
            value = value.slice(0, colon).trim();
          }
          if (key === 'motion' && ACTION_MAP.has(value)) acts.push({ kind: 'motion', name: value });
          else if (key === 'expression' && EXPR_MAP.has(value)) acts.push({ kind: 'expression', name: value });
        }
        return '';
      }).trim();
      const content = body || '……';
      const pages = content.split(/(?<=[。！？…～；])/).filter((s) => s.length);
      speak({ pages: pages.length ? pages : [content], cps: cps || S.cps, idx: 0, acts });
      return { text: content, acts };
    },
    level(v) { S.level = Math.max(0, Math.min(1, Number(v) || 0)); },
    mouth(open, form) {
      S.mouthOpen = Math.max(0, Math.min(1, Number(open) || 0));
      S.mouthForm = Math.max(-1, Math.min(1, Number(form) || 0));
      S.mouthAt = performance.now();
    },
    act(name) {
      const r = act(name);
      if (r && typeof r.then === 'function') r.then((ok) => console.log('[LM]', name, ok ? 'ok' : 'not started'));
      else console.log('[LM]', name, r ? 'ok' : 'no tag');
      return r;
    },
    tags() { return S.caps; },
    part(id, opacity) {
      try { L2D.model.internalModel.coreModel.setPartOpacityById(String(id), Number(opacity)); return true; }
      catch (e) { return false; }
    },
    param(id, value) {
      try { return setParam(String(id), Number(value)); }
      catch (e) { return false; }
    },
    state() { return { avatar: S.avatar, renderer: S.renderer, caps: S.caps, speaking: S.speaking, idx: S.idx, poseSeq: S.poseSeq, poseParams: S.pose.size, poseAdd: S.poseAdd.size }; },
    finish, reset,
    model() {
      if (!L2D) return null;
      const mm = L2D.model.internalModel.motionManager;
      const em = mm.expressionManager;
      return {
        url: S.avatar && S.avatar.model,
        motions: Object.keys(mm.definitions || {}),
        expressions: ((em && em.definitions) || []).map((d) => d && d.Name),
      };
    },
  };

  setInterval(() => {
    if (S.speaking) return;
    if (S.renderer === 'spine') return;   // 显式 <ACT> 才播，不随机
    if (document.body.classList.contains('inspect')) return;   // 调试时不自动做动作
    randomGesture();
  }, 40000);

  let lastTick = 0;
  const tickGap = PREVIEW ? 33 : 16;

  function tick(now) {
    now = Number(now) || performance.now();
    if (now - lastTick < tickGap) {
      requestAnimationFrame(tick);
      return;
    }
    lastTick = now;
    const fresh = S.mouthAt > 0 && now - S.mouthAt < 200;
    const target = S.speaking && fresh ? S.mouthOpen : 0;
    S.mouth += (target - S.mouth) * 0.5;
    if (S.mouth < 0.02) S.mouth *= 0.7;
    if (S.mouth > S.maxMouth) S.maxMouth = S.mouth;
    if (S.renderer === 'spine' && window.SP && window.SP.mouth) {
      window.SP.mouth(S.speaking ? S.mouth : 0);
    }
    if (L2D || IMG) updateBubbleAnchor(now);
    let errEl = document.getElementById('deepsidekick-error');
    if (S.err) {
      if (!errEl) {
        errEl = document.createElement('div');
        errEl.id = 'deepsidekick-error';
        errEl.style.cssText = 'position:fixed;left:12px;top:12px;max-width:70vw;'
          + 'z-index:99999;padding:8px 10px;border-radius:6px;'
          + 'background:rgba(120,20,20,.88);color:#fff;font:13px/1.45 sans-serif;'
          + 'white-space:pre-wrap;';
        document.body.appendChild(errEl);
      }
      errEl.textContent = '形象加载失败：' + S.err;
    } else if (errEl) {
      errEl.remove();
    }
    if (dbgEl && document.body.classList.contains('debug')) {
      dbgEl.textContent =
        'fit=' + (S.fitInfo || '-') + '  r=' + S.renderer + '  lvl=' + S.level.toFixed(3) +
        '  mouth=' + S.mouth.toFixed(2) + '  max=' + S.maxMouth.toFixed(2) +
        '  page=' + (S.idx + 1) + '/' + S.pages.length +
        '  cps=' + S.cps.toFixed(1) + '  act=' + currentMotion() +
        '  tol=' + (S.avatar ? S.avatar.id : '-') +
        (S.err ? '\n' + S.err : '');
    }
    requestAnimationFrame(tick);
  }

  /** 直播姬里的页面已经打开时，WebUI 换了模型/选择也能自己刷新。 */
  function watchConfig() {
    if (PREVIEW) return;
    const keyOf = (c) => [c.kind || '', c.avatar || c.id || '', c.entry || '',
      c.model || '', c.modelVersion || '',
      c.autonomy && c.autonomy.enabled ? '1' : '0',
      c.autonomy && c.autonomy.active ? '1' : '0',
      JSON.stringify(c.layout || {})].join('|');
    const want = (qs.get('avatar') || '').trim();
    const suffix = want ? '&avatar=' + encodeURIComponent(want) : '';
    let last = S.avatar ? keyOf(S.avatar) : '';
    setInterval(async () => {
      try {
        const r = await fetch('/config.json?watch=' + Date.now() + suffix,
                              { cache: 'no-store' });
        if (!r.ok) return;
        const c = await r.json();
        const now = keyOf(c);
        // runtime 可能比舞台页晚启动：第一次拿到非空配置也要重新初始化。
        if (now && now !== last) location.reload();
        last = now;
      } catch (e) { /* 下一轮再试 */ }
    }, 5000);
  }

  async function loadConfig() {
    const endpoint = PREVIEW ? '/preview-config.json' : '/config.json';
    try {
      const r = await fetch(endpoint + (PREVIEW ? location.search : ''), { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const cfg = await r.json();
      const want = (qs.get('avatar') || '').trim();
      if (want && String(cfg.avatar || '') !== want) {
        // 直接按 ?avatar= 再取一次配置：它带 pma（spine 的预乘判定）等模型属性，
        // /models 列表里没有这些，覆盖形象时会丢掉。
        const alt = await fetch(endpoint + '?avatar=' + encodeURIComponent(want), {
          cache: 'no-store',
        });
        if (alt.ok) {
          const hit = await alt.json();
          if (hit && String(hit.avatar || '') === want) {
            // 保留 config 的布局/模式，只替换形象和 capabilities。
            return { ...cfg, ...hit, avatar: hit.id, layout: cfg.layout };
          }
        }
      }
      return cfg;
    } catch (e) {
      S.err = 'config: ' + e.message;
      return null;
    }
  }

  // 预览 iframe 每次换角色/重载都会销毁重建 WebGL 上下文；显式释放比等浏览器
  // GC 快，连续切几次才不会卡在“新模型加载不出来”。
  window.addEventListener('pagehide', () => {
    try {
      if (L2D && L2D.app) L2D.app.destroy(true, { children: true });
    } catch (e) { /* 页面已经在销毁，失败没有后续动作 */ }
    try {
      if (window.SP && window.SP.dispose) window.SP.dispose();
    } catch (e) { /* 同上 */ }
  });

  (async function main() {
    document.body.classList.add('offline');
    if (qs.has('debug')) document.body.classList.add('debug');
    if (PREVIEW) document.body.classList.add('preview', 'inspect');
    const cfg = await loadConfig();
    if (cfg) {
      S.avatar = cfg;
      if (cfg.layout) applyConfigLayout(cfg.layout);
      normalizeCaps(cfg);
    } else {
      normalizeCaps(null);
    }
    applyLayout();

    if (!cfg || !['live2d', 'image', 'spine'].includes(cfg.kind) || !cfg.model) {
      S.err = '形象不可用：缺少 model3.json 或图片路径';
      console.warn('[overlay]', S.err);
    } else if (cfg.kind === 'spine') {
      try {
        await initSpine(cfg);
        if (WANT_INSPECT) {
          loadInspector('spine').then((spi) => spi && spi.open()).catch(() => {});
        }
      } catch (e) {
        S.err = 'spine: ' + e.message;
        console.warn('[overlay]', S.err);
      }
    } else if (cfg.usable === false) {
      S.err = '形象体检未通过：' +
        ((cfg.broken || []).join('；') || '未知原因');
      console.warn('[overlay]', S.err);
    } else if (cfg.kind === 'image') {
      await initImage(cfg);
    } else {
      try {
        await initLive2D(cfg.model);
        if (WANT_INSPECT) {
          loadInspector('live2d').then((lmi) => lmi && lmi.open()).catch(() => {});
        }
      } catch (e) {
        S.err = 'live2d: ' + e.message;
        console.warn('[overlay]', S.err);
      }
    }
    requestAnimationFrame(tick);
    if (!PREVIEW) {
      connect();
      watchConfig();
    }
  })();
})();
