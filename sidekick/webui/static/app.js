/* DeepSidekick WebUI —— 原生 JS，无构建。 */
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const S = {
    schema: null, config: null, runtime: null, memory: null,
    csrf: '', tab: 'overview', secretSets: {},
    secretClears: new Set(), games: [], sttModels: [], sttJobs: [],
    sttRoot: '', configError: '', configNotice: '', secretsError: '',
    gameId: '', userId: '',
    selectedAvatar: '', role: null, roleSeq: 0, gameMemKind: 'plot',
    previewLoaded: false, previewDirty: true,
    logLines: [], sttDown: false, llmModels: {},
  };

  // 未保存的修改按「原因」分开记：每类各有一个保存动作，保存后只清掉自己那类。
  const DIRTY = {
    config: false,     // .cfg-input 里的配置字段
    profile: false,    // 角色设定 / 形象设置表单
    persona: false,    // 角色人设文本
    rules: false,      // 动作规则
    user: false,       // 用户人设表单
    game: false,       // 游戏正文
    preset: false,     // 对话预设条目
  };
  function markDirty(kind = 'config') {
    if (typeof kind !== 'string') kind = 'config';
    DIRTY[kind] = true;
  }
  function clearDirty(kind) {
    if (kind) { DIRTY[kind] = false; return; }
    for (const k of Object.keys(DIRTY)) DIRTY[k] = false;
    // 密钥意图也属于“未保存的修改”；整页丢弃时必须一起清掉，
    // 否则下一次任意保存会把已经放弃的 key 写进去/删掉。
    S.secretSets = {};
    S.secretClears.clear();
  }
  function anyDirty() {
    return Object.values(DIRTY).some(Boolean)
      || Object.keys(S.secretSets).length > 0 || S.secretClears.size > 0;
  }
  function rolePageDirty() {
    return DIRTY.config || DIRTY.profile || DIRTY.persona || DIRTY.rules;
  }
  const AUTONOMY = { preview: null, exists: false, busy: false, supported: true };

  const TITLES = {
    overview: ['概览', '运行状态、功能开关、日志和启停。'],
    model: ['LLM 模型 API', 'OpenAI chat/completions；密钥只写不读。'],
    prompt: ['对话预设', '预设管「怎么做」：场合、开口、材料读法、输出契约、直播追加。'],
    user: ['用户设定', '用户自己的人设档案；只显示当前这一条。'],
    role: ['角色设定', '形象 + 角色设定一起切换；声音等设置不随角色变。'],
    game: ['游戏资料', '静态知识包；剧情/人物/任务在记忆页。'],
    memory: ['记忆与任务', '陪玩记忆、当前世界书的剧情/人物/归档任务。'],
    stt: ['听话（STT）', '你的声音转文字：识别引擎、语音识别模型和麦克风切句。'],
    tts: ['说话（TTS）', '按来源选择；密钥只写不读，保存后重启 runtime 生效。'],
    pace: ['画面与节奏', '选择场景后自动决定截图频率、帧数和最大分辨率'],
    live: ['直播设置', '直播模式、消息来源、弹幕攒批和气氛档位。'],
  };

  // ── 基础工具 ────────────────────────────────────────
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function clone(v) { return JSON.parse(JSON.stringify(v)); }
  function setPath(obj, path, val) {
    const parts = String(path || '').split('.').filter(Boolean);
    if (!parts.length) return;
    let cur = obj;
    for (const p of parts.slice(0, -1)) {
      if (!cur[p] || typeof cur[p] !== 'object') cur[p] = {};
      cur = cur[p];
    }
    cur[parts[parts.length - 1]] = val;
  }
  function toast(msg, bad) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.toggle('err', !!bad);
    el.classList.remove('hidden');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.add('hidden'), 3200);
  }
  async function api(path, opts = {}) {
    const method = (opts.method || 'GET').toUpperCase();
    const headers = Object.assign({}, opts.headers || {});
    if (method !== 'GET') headers['X-CSRF-Token'] = S.csrf;
    let body = opts.body;
    if (body && typeof body === 'object' && !(body instanceof Blob)) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    const res = await fetch(path, { method, headers, body });
    const ct = res.headers.get('content-type') || '';
    let data;
    if (ct.includes('application/json')) data = await res.json();
    else data = await res.text();
    if (!res.ok) {
      const msg = (data && data.error) || (data && data.errors && data.errors.join('；'))
        || (typeof data === 'string' ? data : `HTTP ${res.status}`);
      throw new Error(msg);
    }
    return data;
  }

  // ── 字段渲染 ────────────────────────────────────────
  function fieldEl(f) {
    const wrap = document.createElement('div');
    wrap.className = 'field' + (f.type === 'checkbox' ? ' inline' : '')
      + (f.type === 'textarea' || f.type === 'json' ? ' wide' : '');
    const input = f.type === 'select' ? null : document.createElement('input');
    if (input) {
      input.className = 'cfg-input';
      input.dataset.path = f.path;
      input.dataset.ftype = f.type;
      if (f.secret) input.dataset.secret = '1';
    }

    const label = document.createElement('label');
    label.textContent = (f.label || f.path) + (f.required ? ' *' : '');
    if (f.type === 'checkbox') {
      input.type = 'checkbox';
      input.checked = !!f.value;
      input.id = 'f-' + f.path.replace(/\./g, '-');
      label.htmlFor = input.id;
      wrap.appendChild(input);
      wrap.appendChild(label);
    } else {
      wrap.appendChild(label);
      if (f.type === 'select') {
        const sel = document.createElement('select');
        sel.className = 'cfg-input';
        sel.dataset.path = f.path;
        sel.dataset.ftype = 'select';
        for (const o of f.options || []) {
          const opt = document.createElement('option');
          opt.value = String(o.value);
          opt.textContent = o.label;
          if (String(f.value ?? '') === String(o.value)) opt.selected = true;
          sel.appendChild(opt);
        }
        sel.addEventListener('change', () => markDirty());
        wrap.appendChild(sel);
      } else if (f.type === 'textarea' || f.type === 'json') {
        const ta = document.createElement('textarea');
        ta.className = 'cfg-input';
        ta.dataset.path = f.path;
        ta.dataset.ftype = f.type;
        if (f.secret) ta.dataset.secret = '1';
        ta.rows = f.rows || (f.type === 'json' ? 5 : 6);
        ta.value = f.type === 'json'
          ? JSON.stringify(f.value ?? {}, null, 2)
          : (f.value ?? '');
        if (f.secret) {
          ta.value = '';
          ta.placeholder = f.configured ? '已配置（留空保留）' : '未配置';
        }
        ta.addEventListener('input', () => markDirty());
        wrap.appendChild(ta);
      } else if (f.secret) {
        input.type = 'password';
        input.placeholder = f.configured ? '已配置（留空保留）' : '未配置';
        input.autocomplete = 'new-password';
        input.addEventListener('input', () => {
          if (input.value.trim()) S.secretClears.delete(f.path);
          markDirty();
        });
        wrap.appendChild(input);
        const hint = document.createElement('div');
        hint.className = 'secret-state';
        hint.textContent = f.configured ? '已配置，不会回显' : '未配置';
        wrap.appendChild(hint);
        const clr = document.createElement('button');
        clr.type = 'button';
        clr.className = 'small';
        clr.textContent = '清除';
        clr.addEventListener('click', () => {
          S.secretClears.add(f.path);
          delete S.secretSets[f.path];
          input.value = '';
          input.placeholder = '保存后清除';
          hint.textContent = '将清除';
          markDirty();
        });
        wrap.appendChild(clr);
      } else {
        input.type = f.type === 'number' ? 'number' : 'text';
        if (f.type === 'number') {
          if (f.min != null) input.min = f.min;
          if (f.step != null) input.step = f.step;
        }
        if (f.type === 'list') {
          input.value = Array.isArray(f.value) ? f.value.join(', ') : (f.value ?? '');
          input.placeholder = '用逗号分隔';
        } else {
          input.value = f.value == null ? '' : String(f.value);
        }
        input.addEventListener('input', () => markDirty());
        wrap.appendChild(input);
      }
    }
    if (f.help) {
      const h = document.createElement('div');
      h.className = 'help';
      h.textContent = f.help;
      wrap.appendChild(h);
    }
    if (f.doc) {
      const a = document.createElement('a');
      a.className = 'help';
      a.href = f.doc;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = '打开官网';
      wrap.appendChild(a);
    }
    return wrap;
  }

  function renderGroups(ids, container) {
    container.innerHTML = '';
    for (const id of ids) {
      const g = (S.schema.groups || []).find((x) => x.id === id);
      if (!g) continue;
      if (g.desc) {
        const d = document.createElement('div');
        d.className = 'muted';
        d.style.margin = '4px 0 10px';
        d.textContent = g.desc;
        container.appendChild(d);
      }
      for (const sec of g.sections || []) {
        let host = container;
        if (sec.advanced) {
          const fold = document.createElement('details');
          fold.className = 'panel fold';
          const sum = document.createElement('summary');
          const h = document.createElement('h3');
          h.style.fontSize = '13px';
          h.textContent = sec.title;
          sum.appendChild(h);
          fold.appendChild(sum);
          const body = document.createElement('div');
          body.className = 'fold-body';
          fold.appendChild(body);
          container.appendChild(fold);
          host = body;
        }
        const block = document.createElement('div');
        block.className = 'section-block';
        if (sec.provider) block.dataset.provider = sec.provider;
        if (g.sections.length > 1 && !sec.advanced) {
          const h = document.createElement('h3');
          h.style.fontSize = '13px';
          h.style.margin = '14px 0 8px';
          h.style.color = '#c7d3e0';
          h.textContent = sec.title;
          block.appendChild(h);
        }
        if (sec.desc) {
          const d = document.createElement('div');
          d.className = 'help section-desc';
          d.textContent = sec.desc;
          block.appendChild(d);
        }
        const grid = document.createElement('div');
        grid.className = 'form-grid';
        for (const f of sec.fields) grid.appendChild(fieldEl(f));
        grid.querySelectorAll('textarea').forEach(attachLengthHint);
        block.appendChild(grid);
        host.appendChild(block);
      }
    }
  }

  // ── 配置字段的收集与保存 ─────────────────────────────
  function groupPaths(ids) {
    const want = new Set();
    for (const g of (S.schema.groups || [])) {
      if (!ids.includes(g.id)) continue;
      for (const sec of g.sections || []) {
        for (const f of sec.fields || []) want.add(f.path);
      }
    }
    return want;
  }

  function deepMerge(dst, src) {
    for (const [k, v] of Object.entries(src || {})) {
      if (v && typeof v === 'object' && !Array.isArray(v)
          && dst[k] && typeof dst[k] === 'object' && !Array.isArray(dst[k])) {
        deepMerge(dst[k], v);
      } else {
        dst[k] = v;
      }
    }
    return dst;
  }

  // 单个输入框 → 配置值；`skip` 表示"这次不写这个键"（数字框留空）。
  function readCfgValue(el) {
    const path = el.dataset.path;
    const type = el.dataset.ftype;
    if (type === 'checkbox') return { value: !!el.checked };
    if (type === 'number') {
      if (el.value === '') return { value: '' };
      return { value: Number(el.value) };
    }
    if (type === 'json') {
      try { return { value: el.value.trim() ? JSON.parse(el.value) : {} }; }
      catch (e) { throw new Error(`${path} 不是合法 JSON：${e.message}`); }
    }
    if (type === 'list') {
      // 正则里的 | / 是语法，不是分隔符；只按逗号/顿号/分号拆。
      return { value: el.value.split(/[,，、;；]+/).map((x) => x.trim()).filter(Boolean) };
    }
    let v = el.value;
    if (v === '' && path === 'mode.manual') v = null;
    return { value: v };
  }

  function collectPatch(paths) {
    const patch = {};
    for (const el of $$('.cfg-input[data-path]')) {
      const path = el.dataset.path;
      if (paths && !paths.has(path)) continue;
      if (el.dataset.secret) {
        // 密钥只走 /api/secrets：配置里永远只写 sealed / 空
        const v = el.value.trim();
        if (v) S.secretSets[path] = v;
        continue;
      }
      const r = readCfgValue(el);
      if (r.skip) continue;
      setPath(patch, path, r.value);
    }
    return patch;
  }

  // 预览要一份完整配置（含所有页的当前值）。
  function collectFormConfig() {
    return deepMerge(clone(S.config), collectPatch(null));
  }

  // 自定义敏感 header 不能进 config.yaml；先拆到 SecretStore 槽位。
  function splitSecretHeaders(patch) {
    const secretHeader = (name) => /(token|key|auth|secret|cookie)/i.test(name);
    for (const section of ['llm', 'tts']) {
      const block = patch[section];
      if (!block || typeof block !== 'object') continue;
      for (const [owner, profile] of Object.entries(block)) {
        if (owner === 'source' || owner === 'common'
            || !profile || typeof profile !== 'object'
            || !profile.headers || typeof profile.headers !== 'object') continue;
        for (const [name, value] of Object.entries(profile.headers)) {
          const text = String(value == null ? '' : value).trim();
          if (!secretHeader(name) || !text || text.toLowerCase() === 'sealed') continue;
          S.secretSets[`${section}.${owner}.headers.${name}`] = text;
          delete profile.headers[name];
        }
      }
    }
  }

  async function flushSecrets() {
    const saved = Object.keys(S.secretSets);
    const cleared = [...S.secretClears];
    for (const [name, value] of Object.entries(S.secretSets)) {
      await api('/api/secrets', { method: 'POST', body: { name, value, action: 'set' } });
    }
    for (const name of cleared) {
      await api('/api/secrets', { method: 'POST', body: { name, action: 'clear' } });
    }
    S.secretSets = {};
    S.secretClears.clear();
    return { saved, cleared };
  }

  function markSecretInput(name, configured) {
    const el = $(`.cfg-input[data-path="${name}"]`);
    if (!el) return;
    el.value = '';
    el.placeholder = configured ? '已配置（留空保留）' : '未配置';
    const field = el.closest('.field');
    const hint = field && field.querySelector('.secret-state');
    if (hint) hint.textContent = configured ? '已配置，不会回显' : '未配置';
  }

  // 只刷新后端那一份 schema/config 快照，不动页面上正在编辑的表单。
  async function refreshSchema() {
    const data = await api('/api/bootstrap');
    S.schema = data.schema;
    S.config = data.config;
  }

  // 保存某一页/某个模块的配置字段：只提交这些路径，其余键原样留在文件里。
  async function saveConfig(paths, label) {
    let patch;
    try { patch = collectPatch(paths); } catch (e) { return toast(e.message, true); }
    splitSecretHeaders(patch);
    try {
      await api('/api/config', { method: 'PATCH', body: { patch } });
      const flushed = await flushSecrets();
      deepMerge(S.config, patch);
      for (const name of flushed.saved) markSecretInput(name, true);
      for (const name of flushed.cleared) markSecretInput(name, false);
      clearDirty('config');
      await refreshSchema();          // 下次重画表单时读到的就是刚保存的值
      // 直播开关只影响「仅直播」条目的置灰，不重画列表，别把没保存的编辑冲掉。
      const savedPaths = Array.isArray(paths) ? paths : Array.from(paths || []);
      if (savedPaths.some((p) => String(p).startsWith('session.live'))) {
        refreshPresetMeta();
      }
      if (savedPaths.some((p) => String(p) === 'prompt.id')) {
        renderPresets();
      }
      toast(`${label || '配置'}已保存；热更字段下一轮生效，其余重启 runtime 后生效`);
      return true;
    } catch (e) {
      toast('保存失败：' + e.message, true);
      return false;
    }
  }

  function saveBar(container, paths, label) {
    if (!container) return;
    const row = document.createElement('div');
    row.className = 'row';
    const btn = document.createElement('button');
    btn.className = 'primary';
    btn.textContent = '保存';
    btn.title = label ? `保存${label}` : '保存';
    btn.onclick = () => saveConfig(paths, label);
    row.appendChild(btn);
    container.appendChild(row);
  }

  // ── 状态 / 概览 ─────────────────────────────────────
  async function refreshStatus(force = true) {
    try {
      const data = await api('/api/status' + (force ? '?refresh=1' : ''));
      applyStatus(data);
    } catch (e) { /* 静默 */ }
  }

  function applyStatus(data) {
    S.runtime = (data && data.runtime) || {};
    S.memory = (data && data.memory) || {};
    const inner = S.runtime.runtime || {};
    if ('config_error' in inner) S.configError = inner.config_error || '';
    if ('secrets_error' in inner) S.secretsError = inner.secrets_error || '';
    renderStatus();
    renderCards();
  }

  // 全局告警条。
  // 红条是"粘住"的：一旦看到 disabled 就一直挂着，不随状态推送撤掉，
  // 直到你点「重启 / 停止 / 启动」或者刷新页面才重新判定。
  // （冷却中的琥珀条是临时状态，跟着实况走。）
  function renderAlert() {
    const bar = $('#alert-bar');
    if (!bar) return;
    const rt = (S.runtime || {}).runtime || {};
    const stt = rt.stt || {};
    const user = rt.user || {};
    if (stt.disabled) S.sttDown = true;
    let msg = '';
    let cls = 'alert';
    if (S.configError) {
      msg = `⛔ 配置读取失败，runtime 未启动：${S.configError}`;
      cls += ' alert-red';
    } else if (S.configNotice) {
      msg = `⚠ ${S.configNotice}`;
      cls += ' alert-amber';
    } else if (S.secretsError) {
      msg = `⛔ 密钥库读不了：${S.secretsError}`;
      cls += ' alert-red';
    } else if (S.memory && S.memory.error) {
      msg = `⛔ 记忆库不可用：${S.memory.error}`;
      cls += ' alert-red';
    } else if (Number((rt.brain || {}).summary_failures || 0) > 0) {
      const brain = rt.brain || {};
      const failures = Number(brain.summary_failures || 0);
      const max = Number(brain.history_max || 40);
      const extra = Number(brain.summary_history_extra || 10);
      const err = String(brain.summary_error || '').trim();
      if (brain.summary_dropping) {
        msg = `⛔ 记忆总结连续失败 ${failures} 次，prompt 已恢复 ${max} 条上限并开始丢弃最老输入。`
          + (err ? `（${err}）` : '');
        cls += ' alert-red';
      } else {
        msg = `⚠ 记忆总结连续失败 ${failures} 次，prompt 暂时保留最多 ${max + extra} 条历史，总结成功后恢复。`
          + (err ? `（${err}）` : '');
        cls += ' alert-amber';
      }
    } else if (S.sttDown) {
      msg = '⛔ 语音识别已永久停用：她现在听不到你说话。点顶部「重启」才会重新加载。'
        + (stt.error ? `（最后错误：${stt.error}）` : '');
      cls += ' alert-red';
    } else if (rt.config_error) {
      msg = `⛔ 配置读取失败，沿用旧配置：${rt.config_error}`;
      cls += ' alert-red';
    } else if (rt.secrets_error) {
      msg = `⛔ 密钥库读不了：${rt.secrets_error}`;
      cls += ' alert-red';
    } else if (user.error) {
      msg = `⛔ 用户档案读不出来，先用默认设定：${user.error}`;
      cls += ' alert-red';
    } else if (stt.dead) {
      msg = `⚠ 识别引擎正在冷却，约 30 秒后自动重试`
        + `（已复活 ${stt.revivals || 0}/3 次）。`;
      cls += ' alert-amber';
    } else if (rt.stage && rt.stage.notice) {
      msg = `ℹ 桌宠：${rt.stage.notice}`;
      cls += ' alert-amber';
    }
    bar.className = msg ? cls : 'alert hidden';
    bar.textContent = msg;
  }

  function renderStatus() {
    const st = S.runtime || {};
    const on = !!st.running;
    const badge = $('#side-status');
    badge.textContent = on ? '运行中' : '未运行';
    badge.classList.toggle('on', on);
    badge.classList.toggle('off', !on);
    $('#side-port').textContent = st.pid ? `pid ${st.pid}` : '';
    $('#btn-start').disabled = on;
    $('#btn-stop').disabled = !on;
    renderAlert();
  }

  const MODE_LABELS = { headset: '耳机', speaker: '扬声器' };

  // 麦克风只读实况：门限是运行时量，不能填，只能看。
  function micLiveText(mic) {
    if (!mic) return 'runtime 未运行';
    const mode = MODE_LABELS[mic.mode] || '未判定';
    const gate = Number(mic.gate || 0).toFixed(4);
    const noise = Number(mic.noise_floor || 0).toFixed(4);
    const talking = mic.speaking ? ' · 你正在说话' : '';
    return `当前：${mode}（配置 ${mic.mode_cfg || '—'}）· 门限 ${mic.gate_percent}%`
      + ` · 门限 ${gate} · 噪声底 ${noise}${talking}`;
  }

  function renderCards() {
    const st = S.runtime || {};
    const rt = st.runtime || {};
    const b = rt.brain || {};
    const mem = S.memory || {};
    const mic = rt.mic || null;
    const role = rt.avatar || {};
    const user = rt.user || {};
    const cards = [
      ['运行状态', st.running ? '运行中' : '未运行',
        st.running ? `pid ${st.pid || '?'}` : '点启动'],
      ['模型', b.model || '—', b.source || ''],
      ['TTS', (rt.tts && rt.tts.profile) || '—', rt.tts && rt.tts.speaking ? '正在说话' : ''],
      ['麦克风', mic ? (MODE_LABELS[mic.mode] || '未判定') : '—',
        mic ? `门限 ${Number(mic.gate || 0).toFixed(4)}` : ''],
      ['角色', role.name || '—',
        role.bot_name ? `bot ${role.bot_name}` : '角色设定'],
      ['用户', user.name || '—',
        user.error ? '档案读取失败' : '用户设定'],
      ['陪玩记忆', `${mem.live_summaries ?? 0} 条 · ${mem.summary_vectors ?? 0} 向量`,
        `原始 ${mem.messages ?? 0} 条`],
      ['游戏记忆', `${mem.game_plot ?? 0} 剧情 · ${mem.game_npc ?? 0} 人物`,
        `${mem.game_quests ?? 0} 个归档任务`],
    ];
    const box = $('#overview-cards');
    if (box) box.innerHTML = cards.map((c) => `
      <div class="card"><div class="k">${esc(c[0])}</div>
      <div class="v">${esc(c[1])}</div><div class="s">${esc(c[2])}</div></div>`).join('');
    const live = $('#mic-live');
    if (live) live.textContent = micLiveText(mic);
    const mc = $('#memory-cards');
    if (mc) {
      mc.innerHTML = [
        ['陪玩记忆', mem.live_summaries ?? 0, '全局；按时间/语义召回'],
        ['总结向量', mem.summary_vectors ?? 0, '缺的会后台补'],
        ['游戏剧情', mem.game_plot ?? 0, '随世界书；剧情召回'],
        ['游戏人物', mem.game_npc ?? 0, '随世界书；按名字/别名 upsert'],
        ['活跃任务', mem.tasks_active ?? 0, '随世界书；只在内部状态'],
        ['归档任务', mem.game_quests ?? 0, '完成后进入游戏记忆'],
      ].map((c) => `<div class="card"><div class="k">${esc(c[0])}</div>
        <div class="v">${esc(c[1])}</div><div class="s">${esc(c[2])}</div></div>`).join('');
    }
    renderFeatureCards(rt.features || {});
  }

  const FEATURE_LABELS = {
    mic: '麦克风', stt: '语音识别', capture: '截图', stage: '形象层',
    tts: '语音合成', memory: '长期记忆', embedding: '语义记忆',
    game: '游戏资料', avatar: '形象',
  };
  const STATE_LABELS = {
    healthy: '正常', disabled: '已关闭', unavailable: '缺失', degraded: '降级',
  };

  function renderFeatureCards(features) {
    const box = $('#feature-cards');
    if (!box) return;
    const names = Object.keys(FEATURE_LABELS);
    box.innerHTML = names.filter((n) => features[n]).map((n) => {
      const f = features[n] || {};
      const state = f.state || 'healthy';
      const detail = f.error ? String(f.error) : (state === 'disabled' ? '用户主动关闭' : '');
      return `<div class="card state-${esc(state)}"><div class="k">${esc(FEATURE_LABELS[n])}</div>
        <div class="v">${esc(STATE_LABELS[state] || state)}</div>
        <div class="s">${esc(detail)}</div></div>`;
    }).join('');
  }

  function applyLog(lines) {
    const el = $('#log');
    if (!el) return;
    const text = (lines || []).join('\n');
    if (el.textContent === text) return;       // 没变就别动，省得打断滚动/选中
    // 只有本来就贴着底部才跟着滚；你翻上去看历史时留在原地。
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    const prevTop = el.scrollTop;
    el.textContent = text;
    el.scrollTop = atBottom ? el.scrollHeight : prevTop;
  }

  async function refreshLog() {
    try {
      const data = await api('/api/logs?n=180');
      S.logLines = data.lines || [];
      applyLog(S.logLines);
    } catch (e) { /* ignore */ }
  }

  // 状态和日志共用一条 SSE。事件断线由浏览器自动重连，不回落轮询。
  let eventES = null;
  function applyLogs(lines) {
    S.logLines = lines || [];
    if (S.tab === 'overview') applyLog(S.logLines);
  }
  function startEvents() {
    if (eventES || typeof EventSource === 'undefined') return;
    try {
      eventES = new EventSource('/api/events/stream');
      eventES.addEventListener('status', (ev) => {
        try { applyStatus(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
      });
      eventES.addEventListener('logs', (ev) => {
        try { applyLogs((JSON.parse(ev.data) || {}).lines); } catch (e) { /* ignore */ }
      });
      eventES.onerror = () => { /* 浏览器自动重连 */ };
    } catch (e) { eventES = null; }
  }
  function stopEvents() {
    if (!eventES) return;
    try { eventES.close(); } catch (e) { /* ignore */ }
    eventES = null;
  }

  // ── 模型页（下拉只是浏览，保存才切换）──────────────────
  function renderModel() {
    const sel = $('#model-source');
    if (sel) {
      sel.onchange = null;
      sel.innerHTML = '';
      for (const p of S.schema.llm_profiles || []) {
        const o = document.createElement('option');
        o.value = p.name;
        o.textContent = p.name;
        sel.appendChild(o);
      }
      // 进入页面时回到"已生效"的 profile，不保留上次没保存的选择
      const src = String(((S.config || {}).llm || {}).source || '');
      if (src) sel.value = src;
      sel.dataset.prev = sel.value;
      sel.onchange = () => {
        if (DIRTY.config
            && !confirm('当前 profile 还有未保存的修改，切换会丢失。继续？')) {
          sel.value = sel.dataset.prev || src;
          return;
        }
        sel.dataset.prev = sel.value;
        renderProfileFields();
      };
    }
    renderProfileFields();
  }

  function selectedLlmProfile() {
    const el = $('#model-source');
    const name = el ? el.value : String(((S.config || {}).llm || {}).source || '');
    return (S.schema.llm_profiles || []).find((p) => p.name === name) || null;
  }

  function renderProfileFields() {
    const box = $('#profile-fields');
    if (!box) return;
    box.innerHTML = '';
    const p = selectedLlmProfile();
    const save = $('#btn-profile-save');
    if (save) save.disabled = !p;
    const del = $('#btn-profile-delete');
    if (del) del.disabled = !p;
    const test = $('#btn-profile-test');
    if (test) test.disabled = !p;
    // 名称既是当前 profile 的名字，也是「新建」用的名字
    const name = $('#profile-name');
    if (name) name.value = p ? p.name : '';
    if (!p) return;
    for (const f of p.fields) {
      box.appendChild(fieldEl(f));
      // 顺序按页面要求：base url → 可用模型 → 模型 → api key → 其余
      if (f.path.endsWith('.base_url')) box.appendChild(modelsRow(p));
    }
  }

  // 「可用模型」：测试连接成功后填进来，选中即写进「模型」
  function modelsRow(profile) {
    const wrap = document.createElement('div');
    wrap.className = 'field';
    const label = document.createElement('label');
    label.textContent = '可用模型';
    wrap.appendChild(label);
    const sel = document.createElement('select');
    sel.id = 'profile-models';
    sel.onchange = () => {
      if (!sel.value) return;
      const input = $(`.cfg-input[data-path="llm.${profile.name}.model"]`);
      if (!input) return;
      input.value = sel.value;
      markDirty('config');
    };
    wrap.appendChild(sel);
    const help = document.createElement('div');
    help.className = 'help';
    help.textContent = '点「测试连接」从网关拉；选中后自动填进模型。';
    wrap.appendChild(help);
    fillModelsSelect(profile.name);
    return wrap;
  }

  function fillModelsSelect(name) {
    const sel = $('#profile-models');
    if (!sel) return;
    const ids = S.llmModels[name] || [];
    const current = ($(`.cfg-input[data-path="llm.${name}.model"]`) || {}).value || '';
    sel.innerHTML = '';
    const first = document.createElement('option');
    first.value = '';
    first.textContent = ids.length ? '选择一个填入模型' : '先点「测试连接」获取';
    sel.appendChild(first);
    for (const id of ids) {
      const o = document.createElement('option');
      o.value = id;
      o.textContent = id;
      o.selected = id === current;
      sel.appendChild(o);
    }
  }

  async function saveModelProfile() {
    const p = selectedLlmProfile();
    if (!p) return toast('先选一个 profile', true);
    const paths = new Set(['llm.source']);
    for (const f of p.fields) paths.add(f.path);
    await saveConfig(paths, `模型 API（${p.name}）`);
    renderModel();                   // saveConfig 里已经刷新过 schema
  }

  async function testSelectedProfile() {
    const p = selectedLlmProfile();
    if (!p) return toast('先选一个 profile', true);
    try {
      const data = await api('/api/llm/test', {
        method: 'POST', body: { profile: p.name, draft: collectProfileDraft(p.name) },
      });
      if (!data.ok) {
        toast(`失败：HTTP ${data.status || ''} ${data.error || ''}`, true);
        return;
      }
      S.llmModels[p.name] = data.models || [];
      fillModelsSelect(p.name);
      const extra = (data.models || []).length
        ? `，可用模型 ${data.models.length} 个` : '';
      const warn = data.models_error ? `（模型列表没取到：${data.models_error}）` : '';
      toast(`连接成功：HTTP ${data.status} · ${data.ms}ms${extra}${warn}`,
            !!data.models_error);
    } catch (e) { toast('测试失败：' + e.message, true); }
  }

  async function deleteSelectedProfile() {
    const p = selectedLlmProfile();
    if (!p) return toast('先选一个 profile', true);
    if (p.active) return toast('这是当前生效的 profile，先切到别个并保存再删', true);
    if (!confirm(`删除 profile ${p.name}？`)) return;
    try {
      for (const k of Object.keys(S.secretSets)) {
        if (k.startsWith(`llm.${p.name}.`)) delete S.secretSets[k];
      }
      for (const k of [...S.secretClears]) {
        if (k.startsWith(`llm.${p.name}.`)) S.secretClears.delete(k);
      }
      await api('/api/config', { method: 'PATCH',
                                 body: { path: `llm.${p.name}`, delete: true } });
      await bootstrap(true);
      clearDirty('config');
      toast('已删除');
    } catch (e) { toast(e.message, true); }
  }

  function collectProfileDraft(name) {
    const p = (S.schema.llm_profiles || []).find((x) => x.name === name);
    const out = {};
    const prefix = `llm.${name}.`;
    for (const el of $$(`.cfg-input[data-path^="${prefix}"]`)) {
      const key = el.dataset.path.slice(prefix.length);
      if (el.dataset.secret) {
        if (el.value.trim()) out[key] = el.value.trim();
        continue;
      }
      if (el.dataset.ftype === 'json') {
        try { out[key] = el.value.trim() ? JSON.parse(el.value) : {}; }
        catch (e) { throw new Error('headers 不是合法 JSON'); }
      } else if (el.dataset.ftype === 'checkbox') out[key] = !!el.checked;
      else out[key] = el.value;
    }
    return out;
  }

  // ── 角色设定 / 用户 ────────────────────────────────
  function fieldRow(labelText, input) {
    const wrap = document.createElement('div');
    wrap.className = 'field';
    const label = document.createElement('label');
    label.textContent = labelText;
    wrap.appendChild(label);
    wrap.appendChild(input);
    return wrap;
  }

  function textInput(id, value, placeholder) {
    const input = document.createElement('input');
    input.id = id;
    input.className = 'cfg-input';
    input.value = value || '';
    input.placeholder = placeholder || '';
    return input;
  }

  function textArea(id, value, rows) {
    const ta = document.createElement('textarea');
    ta.id = id;
    ta.className = 'cfg-input code';
    ta.rows = rows || 6;
    ta.value = value || '';
    return ta;
  }

  // ── 角色设定（写 persona/roles/<id>/profile.yaml）───
  function roleProfileValue(key) {
    const p = (S.role && S.role.profile) || {};
    return p[key];
  }

  function renderRoleProfile() {
    const box = $('#role-profile-form');
    if (!box) return;
    box.innerHTML = '';
    box.appendChild(fieldRow('显示名', textInput(
      'role-name', roleProfileValue('name'),
      `只影响下拉显示；留空 = 角色 id（${S.role ? S.role.role_id : ''}）`)));
    box.appendChild(fieldRow('角色主名字', textInput(
      'role-bot-name', roleProfileValue('bot_name'), '留空就是空')));
    const aliases = roleProfileValue('aliases') || [];
    box.appendChild(fieldRow('别名（逗号分隔）', textInput(
      'role-aliases', aliases.join('，'), '留空就是空')));
    for (const el of $$('#role-profile-form input')) {
      el.addEventListener('input', () => markDirty('profile'));
    }
    const meta = $('#role-profile-meta');
    if (meta) {
      meta.textContent = S.role && S.role.role_dir
        ? `写 ${S.role.role_dir}` : '';
    }
  }

  function readAliases(text) {
    return String(text || '').split(/[,，、;；]+/).map((x) => x.trim()).filter(Boolean);
  }

  function numberInput(id, value, step) {
    const input = document.createElement('input');
    input.id = id;
    input.type = 'number';
    input.step = String(step || 'any');
    input.className = 'cfg-input';
    input.value = (value === undefined || value === null) ? '' : value;
    return input;
  }

  function roleStageValue(path) {
    const stage = (S.role && S.role.profile && S.role.profile.stage) || {};
    const def = (S.config && S.config.stage) || {};
    const get = (src) => {
      let cur = src;
      for (const key of path.split('.')) {
        if (!cur || typeof cur !== 'object') return undefined;
        cur = cur[key];
      }
      return cur;
    };
    let value = get(stage);
    if (value === undefined || value === null || value === '') value = get(def);
    return value === undefined || value === null ? '' : value;
  }

  function renderRoleStage() {
    const box = $('#role-stage-form');
    if (!box) return;
    box.innerHTML = '';
    const rows = [
      ['cps', '气泡速度（字/秒）', 'cps', 0.1],
      ['bubble-gap', '气泡间隙（vh）', 'bubble.gap', 0.1],
      ['bubble-max-width', '气泡最大宽度（vw）', 'bubble.max_width', 1],
    ];
    for (const [id, label, path, step] of rows) {
      box.appendChild(fieldRow(label, numberInput(
        `role-stage-${id}`, roleStageValue(path), step)));
    }
    const auto = document.createElement('input');
    auto.type = 'checkbox';
    auto.id = 'role-stage-auto-gap';
    auto.className = 'cfg-input';
    auto.checked = !!roleStageValue('bubble.auto_gap');
    const row = fieldRow('自动贴头顶', auto);
    row.classList.add('inline');
    box.appendChild(row);
    for (const el of $$('#role-stage-form input')) {
      el.addEventListener('input', () => markDirty('profile'));
      el.addEventListener('change', () => markDirty('profile'));
    }
    const meta = $('#role-stage-meta');
    if (meta && S.role) meta.textContent = S.role.profile && S.role.profile.stage
      ? '已保存这个角色的外观' : '还没保存；当前用全局默认';
  }

  function collectRoleStage() {
    const out = {};
    const num = (id) => parseFloat($(`#role-stage-${id}`).value);
    const set = (path, value) => {
      const parts = path.split('.');
      let cur = out;
      for (const p of parts.slice(0, -1)) cur = (cur[p] = cur[p] || {});
      cur[parts[parts.length - 1]] = value;
    };
    for (const [id, path] of [
      ['cps', 'cps'],
      ['bubble-gap', 'bubble.gap'],
      ['bubble-max-width', 'bubble.max_width'],
    ]) {
      const v = num(id);
      if (Number.isFinite(v)) set(path, v);
    }
    const auto = $('#role-stage-auto-gap');
    if (auto) {
      out.bubble = out.bubble || {};
      out.bubble.auto_gap = !!auto.checked;
    }
    return out;
  }

  function roleProfileBody() {
    return {
      name: $('#role-name').value.trim(),
      bot_name: $('#role-bot-name').value.trim(),
      aliases: readAliases($('#role-aliases').value),
      stage: collectRoleStage(),
    };
  }

  async function saveRoleBody(label) {
    const id = S.selectedAvatar;
    if (!id) return;
    try {
      const data = await api(`/api/roles/${encodeURIComponent(id)}/profile`, {
        method: 'PUT', body: roleProfileBody(),
      });
      S.role = {
        id,
        role_id: data.role_id,
        display_name: data.display_name,
        profile: data.profile,
        role_dir: data.role_dir,
        error: data.error,
      };
      const entry = (S.schema.avatars || []).find((a) => a.id === id);
      if (entry) {
        entry.display_name = data.display_name;
        entry.role_id = data.role_id;
        entry.profile_error = data.error;
      }
      clearDirty('profile');
      renderRolePicker();
      renderRoleProfile();
      renderRoleStage();
      refreshPreview();
      toast(`${label}已保存（名字/别名 15 秒内生效；形象设置重启 runtime 生效）`);
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  async function saveRoleProfile() {
    await saveRoleBody('角色设定');
  }

  async function saveRoleStage() {
    await saveRoleBody('形象设置');
  }

  // ── 角色人设（只有一个 persona.md）──────────────────
  function personaFile() {
    return (S.schema.persona_files && S.schema.persona_files[0]) || 'persona.md';
  }

  async function loadRolePersona() {
    const targetId = S.selectedAvatar;
    if (!targetId) return;
    try {
      const data = await api(`/api/roles/${encodeURIComponent(targetId)}`
        + `/persona/${encodeURIComponent(personaFile())}`);
      if (S.selectedAvatar !== targetId) return;
      $('#role-persona-text').value = data.content || '';
      refreshLengthHint('role-persona-text');
      $('#role-persona-meta').textContent = `${(data.content || '').length} 字`;
      clearDirty('persona');
    } catch (e) {
      if (S.selectedAvatar !== targetId) return;
      toast('读取失败：' + e.message, true);
    }
  }

  async function saveRolePersona() {
    const id = S.selectedAvatar;
    if (!id) return;
    const text = $('#role-persona-text').value;
    try {
      await api(`/api/roles/${encodeURIComponent(id)}`
        + `/persona/${encodeURIComponent(personaFile())}`, {
        method: 'PUT', body: { content: text },
      });
      clearDirty('persona');
      toast(text.trim() ? '人设已保存（下一轮生效）' : '人设已清空');
      await loadRolePersona();
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  // ── 对话预设（persona/presets/<id>/preset.yaml）──────
  // 页面只编辑下拉选中的那一份：浏览不写配置，点「保存」才写条目 + prompt.id。
  // 锚点是 prompt 里固定内容的位置，不能移动；条目拖到哪两个锚点之间，就落在哪两个
  // 锚点之间的缝隙里（slot）。同一个缝隙里的先后就是列表里的先后。
  let presetDrag = null;

  function presetState() {
    return (S.schema && S.schema.presets) || {
      items: [], blocks: [], gaps: [], current: '', loaded: '',
      name: '', desc: '', error: '',
    };
  }

  function presetNote(text, bad) {
    const note = $('#preset-note');
    if (!note) return;
    note.textContent = text || '';
    note.classList.toggle('bad', !!bad);
  }

  function presetEntryTitle(wrap) {
    const input = wrap.querySelector('[data-field="title"]');
    const title = (input && input.value || '').trim();
    return title || wrap.dataset.id || '条目';
  }

  function presetEntryLive(el) {
    const box = el.querySelector('[data-field="live"]');
    return !!(box && box.checked);
  }

  function presetEntryMeta(el) {
    const role = el.querySelector('[data-field="role"]').value;
    const text = el.querySelector('[data-field="text"]');
    return `${role} · ${(text.value || '').length} 字`;
  }

  function applyPresetLiveDim(el) {
    el.classList.toggle('live-off', presetEntryLive(el) && !liveMode());
  }

  function refreshPresetMeta() {
    const box = $('#preset-blocks');
    if (!box) return;
    for (const el of Array.from(box.querySelectorAll('details.entry'))) {
      const head = el.querySelector('summary h2');
      if (head) head.textContent = presetEntryTitle(el);
      const meta = el.querySelector('summary .entry-meta');
      if (meta) meta.textContent = presetEntryMeta(el);
      const badge = el.querySelector('summary .entry-live');
      if (badge) badge.hidden = !presetEntryLive(el);
      applyPresetLiveDim(el);
    }
  }

  function presetAnchorEl(label, nextSlot) {
    const row = document.createElement('div');
    row.className = 'preset-anchor';
    row.dataset.slot = nextSlot || '';
    row.title = '提示词里的固定内容；锚点不能移动，把条目拖到它上下就是调整位置';
    const text = document.createElement('span');
    text.textContent = label;
    row.appendChild(text);
    return row;
  }

  function presetEntryEl(block) {
    const wrap = document.createElement('details');
    wrap.className = 'panel fold entry';
    wrap.dataset.id = block.id || '';
    wrap.open = !block.text;

    const summary = document.createElement('summary');
    const handle = document.createElement('span');
    handle.className = 'drag-handle';
    handle.textContent = '⠿';
    handle.title = '拖动条目到锚点之间';
    handle.draggable = true;
    handle.addEventListener('dragstart', (e) => {
      presetDrag = wrap;
      wrap.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', block.id || 'entry'); } catch (_) {}
    });
    handle.addEventListener('dragend', () => {
      presetDrag = null;
      wrap.classList.remove('dragging');
    });
    const head = document.createElement('h2');
    head.textContent = block.title || block.id || '条目';
    const badge = document.createElement('span');
    badge.className = 'entry-live';
    badge.textContent = '仅直播';
    badge.hidden = !block.live;
    const meta = document.createElement('span');
    meta.className = 'muted entry-meta';
    summary.append(handle, head, badge, meta);
    wrap.appendChild(summary);

    const body = document.createElement('div');
    body.className = 'fold-body';
    const row = document.createElement('div');
    row.className = 'row';
    const titleCaption = document.createElement('span');
    titleCaption.className = 'muted';
    titleCaption.textContent = '标题';
    titleCaption.title = '只用于列表识别，不进入 prompt';
    const title = textInput('', block.title, '条目标题');
    title.dataset.field = 'title';
    title.addEventListener('input', refreshPresetMeta);
    title.title = titleCaption.title;
    const liveLabel = document.createElement('label');
    liveLabel.className = 'muted live-toggle';
    liveLabel.title = '勾上后只有开直播模式才注入；没开直播时这行变灰，但照常拖、照常编辑';
    const live = document.createElement('input');
    live.type = 'checkbox';
    live.dataset.field = 'live';
    live.checked = !!block.live;
    live.addEventListener('change', refreshPresetMeta);
    liveLabel.append(live, '仅直播');
    const role = document.createElement('select');
    role.dataset.field = 'role';
    role.title = '这条消息的角色';
    for (const value of ['system', 'user']) {
      const o = document.createElement('option');
      o.value = value;
      o.textContent = value;
      role.appendChild(o);
    }
    role.value = block.role || 'system';
    role.addEventListener('change', refreshPresetMeta);
    const del = document.createElement('button');
    del.className = 'small danger';
    del.textContent = '删除';
    del.onclick = () => {
      wrap.remove();
      markDirty('preset');
    };
    row.append(titleCaption, title, liveLabel, role, del);

    const text = document.createElement('textarea');
    text.dataset.field = 'text';
    text.className = 'code';
    text.rows = 6;
    text.value = block.text || '';
    text.addEventListener('input', refreshPresetMeta);
    body.append(row, text);
    wrap.appendChild(body);
    wrap.addEventListener('input', () => markDirty('preset'));
    wrap.addEventListener('change', () => markDirty('preset'));
    return wrap;
  }

  function renderPresetList(box) {
    if (!box) return;
    box.innerHTML = '';
    const st = presetState();
    const gaps = st.gaps || [];
    const byGap = new Map(gaps.map((gap) => [gap.slot, []]));
    for (const b of st.blocks || []) {
      if (byGap.has(b.slot)) byGap.get(b.slot).push(b);
    }
    gaps.forEach((gap, i) => {
      for (const b of byGap.get(gap.slot)) box.appendChild(presetEntryEl(b));
      if (gap.anchor) {
        const next = gaps[i + 1];
        box.appendChild(presetAnchorEl(gap.anchor, next ? next.slot : ''));
      }
    });
    box.ondragover = (e) => {
      if (!presetDrag || !box.contains(presetDrag)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const hit = Array.from(box.children).find((el) => {
        if (el === presetDrag) return false;
        const r = el.getBoundingClientRect();
        return e.clientY < r.top + r.height / 2;
      });
      if (hit) {
        if (hit.previousElementSibling !== presetDrag) {
          box.insertBefore(presetDrag, hit);
          markDirty('preset');
        }
      } else if (box.lastElementChild !== presetDrag) {
        box.appendChild(presetDrag);
        markDirty('preset');
      }
    };
    box.ondrop = (e) => {
      if (!presetDrag) return;
      e.preventDefault();
      // 落在哪个锚点区间由 DOM 位置决定，保存时再写成 slot。
      presetDrag.classList.remove('dragging');
      presetDrag = null;
      refreshPresetMeta();
      markDirty('preset');
    };
    box.ondragend = () => {
      if (presetDrag) presetDrag.classList.remove('dragging');
      presetDrag = null;
    };
  }

  function renderPresets() {
    const st = presetState();
    const sel = $('#preset-list');
    if (sel) {
      sel.innerHTML = '';
      for (const item of st.items || []) {
        const o = document.createElement('option');
        o.value = item.id;
        o.textContent = item.name && item.name !== item.id
          ? `${item.id}（${item.name}）` : item.id;
        sel.appendChild(o);
      }
      if (st.loaded) sel.value = st.loaded;
    }
    renderPresetList($('#preset-blocks'));
    refreshPresetMeta();
    if (st.error) presetNote(`预设有问题：${st.error}`, true);
    else presetNote('');
  }

  function liveMode() {
    return !!(S.config && S.config.session && S.config.session.live);
  }

  function collectPresetEntries() {
    const out = [];
    const box = $('#preset-blocks');
    if (!box) return out;
    const gaps = presetState().gaps || [];
    let slot = (gaps[0] || {}).slot || 'start';
    for (const el of Array.from(box.children)) {
      if (el.classList.contains('preset-anchor')) {
        if (el.dataset.slot) slot = el.dataset.slot;
        continue;
      }
      if (!el.classList.contains('entry')) continue;
      out.push({
        id: el.dataset.id || '',
        title: el.querySelector('[data-field="title"]').value,
        slot,
        role: el.querySelector('[data-field="role"]').value,
        live: presetEntryLive(el),
        text: el.querySelector('[data-field="text"]').value,
      });
    }
    return out;
  }

  function applyPresetPayload(data) {
    S.schema.presets = {
      items: data.items || [],
      current: data.current || '',
      loaded: data.loaded || '',
      blocks: data.blocks || [], gaps: data.gaps || [],
      name: data.name || '', desc: data.desc || '', error: data.error || '',
    };
    clearDirty('preset');
    renderPresets();
  }

  async function refreshPresets() {
    try { applyPresetPayload(await api('/api/presets')); }
    catch (e) { toast('读取预设失败：' + e.message, true); }
  }

  async function browsePreset(id) {
    if (!id || id === presetState().loaded) return;
    if (DIRTY.preset
        && !confirm('预设条目还有未保存的修改，切换会丢失。继续？')) {
      const sel = $('#preset-list');
      if (sel) sel.value = presetState().loaded || '';
      return;
    }
    clearDirty('preset');
    try { applyPresetPayload(await api('/api/presets/' + encodeURIComponent(id))); }
    catch (e) { toast('读取预设失败：' + e.message, true); }
  }

  async function savePreset() {
    const st = presetState();
    const id = st.loaded;
    if (!id) { toast('先新建或选择一个预设', true); return; }
    try {
      const saved = await api(`/api/presets/${encodeURIComponent(id)}`, {
        method: 'PUT',
        body: {
          name: st.name || id,
          desc: st.desc || '',
          blocks: collectPresetEntries(),
        },
      });
      const selected = await api('/api/presets/select',
                                 { method: 'POST', body: { id } });
      applyPresetPayload(selected);
      S.config.prompt = { ...(S.config.prompt || {}), id };
      const issues = saved.issues || [];
      if (issues.length) {
        presetNote(`已保存；跳过 ${issues.length} 条：${issues[0]}`, true);
      } else {
        presetNote('已保存（下一轮生效）');
      }
      toast('预设已保存（下一轮生效）');
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  async function createPreset() {
    const id = ($('#preset-new').value || '').trim();
    if (!id) { toast('先填预设名', true); return; }
    if ((DIRTY.preset || DIRTY.config)
        && !confirm('当前预设/配置还有未保存的修改，新建会丢失。继续？')) return;
    try {
      const data = await api('/api/presets', { method: 'POST', body: { id } });
      applyPresetPayload(data);
      $('#preset-new').value = '';
      presetNote(`已新建 ${id}；点「保存」设为当前预设`);
    } catch (e) { toast('新建失败：' + e.message, true); }
  }

  async function deletePreset() {
    const id = presetState().loaded;
    if (!id) return;
    if (!confirm(`删除预设 ${id}？`)) return;
    try {
      applyPresetPayload(await api(`/api/presets/${encodeURIComponent(id)}`,
        { method: 'DELETE' }));
      toast(`已删除 ${id}`);
    } catch (e) { toast('删除失败：' + e.message, true); }
  }

  function addPresetEntry() {
    const box = $('#preset-blocks');
    if (!box) return;
    // 新条目加到列表最下面，也就是「本轮互动消息」之后（after_material）。
    const entry = presetEntryEl(
      { id: '', title: '', role: 'system', live: false, text: '' });
    box.appendChild(entry);
    entry.open = true;
    refreshPresetMeta();
    markDirty('preset');
    entry.scrollIntoView({ block: 'nearest' });
  }

  async function showPromptPreview() {
    const panel = $('#prompt-preview-panel');
    const pre = $('#prompt-preview');
    if (!panel || !pre) return;
    const btn = $('#btn-prompt-preview');
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '拼装中…';
    panel.hidden = false;
    pre.textContent = '正在按已保存的配置现拼一次（要起一个临时进程，几秒钟）…';
    try {
      const data = await api('/api/prompt/preview');
      pre.textContent = data.text || '（空）';
    } catch (e) {
      pre.textContent = '预演失败：' + e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  // ── 导入导出（预设 / 世界书 / 角色）──────────────
  function downloadPack(kind, id, note) {
    if (!id) { toast('先选中一个再导出', true); return; }
    const a = document.createElement('a');
    a.href = `/api/pack/export?kind=${encodeURIComponent(kind)}`
      + `&id=${encodeURIComponent(id)}`;
    a.download = `${kind}-${id}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    if (note) toast(note);
  }

  async function uploadPack(kind, input, label) {
    const file = input.files && input.files[0];
    if (!file) return null;
    try {
      const data = await api(`/api/pack/import?kind=${encodeURIComponent(kind)}`
        + '&overwrite=1', { method: 'POST', body: file });
      input.value = '';
      toast(`${label}已导入：${data.id}`);
      return data;
    } catch (e) {
      input.value = '';
      toast('导入失败：' + e.message, true);
      return null;
    }
  }

  function pickPack(input) {
    input.value = '';
    input.click();
  }

  async function importPresetPack() {
    const data = await uploadPack('preset', $('#preset-import'), '预设');
    if (!data) return;
    await refreshPresets();
    await browsePreset(data.id);
  }

  function exportRolePack() {
    downloadPack('role', S.selectedAvatar, '正在打包角色（含形象），文件大时会等一下');
  }

  async function importRolePack() {
    const data = await uploadPack('role', $('#role-import'), '角色');
    if (!data) return;
    await bootstrap(true);
    await rescanRoles();
    const note = $('#role-pack-note');
    note.hidden = false;
    note.textContent = `已导入：角色 ${data.id} · 形象 ${data.avatar || '无'}`;
  }

  function exportGamePack() {
    downloadPack('worldbook', S.gameId, '正在打包世界书');
  }

  async function importGamePack() {
    const data = await uploadPack('worldbook', $('#game-import'), '世界书');
    if (data) {
      await loadGames();
      $('#game-pack-note').textContent = `已导入：${data.id}`;
    }
  }

  // ── 形象层全局四项（enabled/mode/host/port）──────────
  function renderStageGlobal() {
    const box = $('#stage-global-form');
    if (!box) return;
    box.innerHTML = '';
    const want = new Set(['stage.mode', 'stage.host', 'stage.port']);
    const group = (S.schema.groups || []).find((g) => g.id === 'stage');
    const grid = document.createElement('div');
    grid.className = 'form-grid';
    for (const sec of (group && group.sections) || []) {
      for (const f of sec.fields || []) {
        if (want.has(f.path)) grid.appendChild(fieldEl(f));
      }
    }
    box.appendChild(grid);
    saveBar(box, want, '形象层');
  }

  // ── 用户人设（persona/users/<id>/profile.yaml）─────
  function currentUserItem() {
    const items = (S.schema.users && S.schema.users.items) || [];
    return items.find((x) => x.id === S.userId) || null;
  }

  function renderUserPicker() {
    const sel = $('#user-list');
    if (!sel) return;
    const data = S.schema.users || { items: [] };
    const items = data.items || [];
    sel.innerHTML = '';
    for (const u of items) {
      const o = document.createElement('option');
      o.value = u.id;
      o.textContent = `${u.id}（${u.name || '未命名'}）`;
      sel.appendChild(o);
    }
    // 浏览中的那个保持选中；没有浏览就回到"已生效"的用户
    if (!items.some((x) => x.id === S.userId)) S.userId = data.current || '';
    if (!S.userId && items.length) S.userId = items[0].id;
    if (S.userId) sel.value = S.userId;
    const nameInput = $('#user-id-input');
    if (nameInput) nameInput.value = S.userId || '';
    const note = $('#user-note');
    if (note) {
      const cur = items.find((x) => x.id === data.current);
      const head = data.current && !cur
        ? `当前用户：${data.current}（没有档案）`
        : (data.current
          ? `当前用户：${data.current}${cur && cur.error ? `（读取失败：${cur.error}）` : ''}`
          : '还没有用户人设');
      note.textContent = head + (S.userId && S.userId !== data.current
        ? ` · 正在浏览 ${S.userId}（未保存）` : '');
    }
  }

  function renderUserForm() {
    const box = $('#user-form');
    if (!box) return;
    box.innerHTML = '';
    const u = currentUserItem() || {};
    if (u.error) {
      const bad = document.createElement('div');
      bad.className = 'muted';
      bad.textContent = `这份用户档案读不出来：${u.error}`;
      box.appendChild(bad);
    }
    box.appendChild(fieldRow('用户名字', textInput(
      'user-name', u.name || '', '替换 prompt 里的用户名字；默认就用它称呼你')));
    const personaRow = fieldRow('用户设定（背景，不是命令）',
                                textArea('user-persona', u.persona || '', 10));
    personaRow.classList.add('wide');
    box.appendChild(personaRow);
    for (const el of $$('#user-form input, #user-form textarea, #user-form select')) {
      el.addEventListener('input', () => markDirty('user'));
      el.addEventListener('change', () => markDirty('user'));
    }
    attachLengthHint($('#user-persona'));
  }

  async function saveUserAll() {
    const uid = $('#user-list').value || S.userId;
    if (!uid) return toast('先新建或选择一个用户', true);
    const body = {
      name: $('#user-name').value.trim(),
      persona: $('#user-persona').value,
    };
    try {
      await api('/api/users/' + encodeURIComponent(uid), {
        method: 'PUT', body,
      });
      // 人设先落盘，再把"当前用户"切过去
      const data = await api('/api/users/select', {
        method: 'POST', body: { id: uid },
      });
      S.schema.users = data.users;
      S.userId = data.current;
      S.config.user = { ...(S.config.user || {}), id: data.current };
      clearDirty('user');
      const item = (S.schema.users.items || []).find((x) => x.id === S.userId);
      if (item) Object.assign(item, body);
      renderUserPicker();
      renderUserForm();
      toast(`用户人设与当前用户已保存：${data.current}（15 秒内生效）`);
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  async function createUser() {
    const uid = ($('#user-id-input').value || '').trim();
    if (!uid) return toast('先填用户 ID', true);
    if (DIRTY.user
        && !confirm('当前用户设定还有未保存的修改，新建会丢失。继续？')) return;
    try {
      const data = await api('/api/users', { method: 'POST', body: { id: uid } });
      S.schema.users = data.users;
      S.userId = uid;
      renderUserPicker();
      renderUserForm();
      toast(`已新建用户 ${uid}；点「保存」设为当前`);
    } catch (e) { toast('新建失败：' + e.message, true); }
  }

  async function deleteUser() {
    const uid = $('#user-list').value;
    if (!uid) return;
    if (!confirm(`删除用户人设 ${uid}？文件夹会一起删掉。`)) return;
    try {
      const data = await api(`/api/users/${encodeURIComponent(uid)}?confirm=1`,
                             { method: 'DELETE' });
      S.schema.users = data.users;
      renderUserPicker();
      renderUserForm();
      toast('已删除');
    } catch (e) { toast('删除失败：' + e.message, true); }
  }

  function renderGamePicker() {
    const sel = $('#game-list');
    if (!sel) return;
    sel.innerHTML = '';
    for (const g of S.games) {
      const o = document.createElement('option');
      o.value = g.id;
      o.textContent = g.id;
      sel.appendChild(o);
    }
    const current = (S.games.find((g) => g.current) || {}).id;
    if (current) sel.value = current;
    const nameInput = $('#game-id-input');
    if (nameInput) nameInput.value = sel.value || '';
  }

  async function loadGames() {
    const data = await api('/api/games');
    S.games = data.games || [];
    renderGamePicker();
    if (S.games.length) await loadGameText($('#game-list').value);
  }

  async function loadGameText(gid) {
    if (!gid) return;
    const data = await api('/api/games/' + encodeURIComponent(gid));
    $('#game-text').value = data.content || '';
    $('#game-meta').textContent = `${gid} · ${(data.content || '').length} 字` +
      (data.worldbook ? ` · ${(data.entries || []).length} 个条目` : ' · 仅 core');
    renderEntries(data.entries || []);
    S.gameId = gid;
    clearDirty('game');
    refreshLengthHint('game-text');
  }

  function renderEntries(entries) {
    const box = $('#worldbook-entries');
    if (!box) return;
    if (!entries.length) {
      box.innerHTML = '<div class="muted">还没有条目。新增后可按 keys 自动召回。</div>';
      return;
    }
    box.innerHTML = `<table><thead><tr><th>标题</th><th>注入</th><th>keys / chapter</th><th></th></tr></thead>
      <tbody>${entries.map((e) => `<tr>
      <td><strong>${esc(e.title || e.id)}</strong><div class="muted">${esc(e.id)}</div></td>
      <td>${e.always ? '<span class="pill blue">常驻</span>' : '<span class="pill green">关键词</span>'}</td>
      <td>${(e.keys || []).map((k) => `<span class="pill">${esc(k)}</span>`).join(' ')}
        ${e.chapter ? `<span class="muted">第 ${esc(e.chapter)} 章</span>` : ''}</td>
      <td><button class="small" data-entry-edit="${esc(e.id)}">编辑</button>
        <button class="small danger" data-entry-del="${esc(e.id)}">删除</button></td>
      </tr>`).join('')}</tbody></table>`;
    box.querySelectorAll('[data-entry-edit]').forEach((b) => {
      b.onclick = () => editEntry(entries.find((x) => x.id === b.dataset.entryEdit));
    });
    box.querySelectorAll('[data-entry-del]').forEach((b) => {
      b.onclick = async () => {
        if (!confirm('删除这个条目？')) return;
        await api(`/api/games/${encodeURIComponent($('#game-list').value)}/entries/${encodeURIComponent(b.dataset.entryDel)}`,
          { method: 'DELETE' });
        await loadGameText($('#game-list').value);
        toast('已删除');
      };
    });
  }

  function entryForm(e) {
    e = e || { id: '', title: '', keys: [], aliases: [], always: false,
               chapter: '', spoiler: false, priority: 0, content: '' };
    return `<div class="form-grid">
      <div class="field"><label>条目 ID（文件名）</label><input id="e-id" value="${esc(e.id || '')}" ${e.id ? 'readonly' : ''}></div>
      <div class="field"><label>标题</label><input id="e-title" value="${esc(e.title || '')}"></div>
      <div class="field wide"><label>keys（逗号分隔）</label><input id="e-keys" value="${esc((e.keys || []).join(', '))}"></div>
      <div class="field wide"><label>别名（逗号分隔）</label><input id="e-aliases" value="${esc((e.aliases || []).join(', '))}"></div>
      <div class="field"><label>常驻（always）</label><input id="e-always" type="checkbox" ${e.always ? 'checked' : ''}></div>
      <div class="field"><label>章节</label><input id="e-chapter" value="${esc(e.chapter || '')}"></div>
      <div class="field"><label>剧透保护</label><input id="e-spoiler" type="checkbox" ${e.spoiler ? 'checked' : ''}></div>
      <div class="field"><label>priority</label><input id="e-prio" type="number" value="${Number(e.priority || 0)}"></div>
      <div class="field wide"><label>正文</label><textarea id="e-content" rows="10">${esc(e.content || '')}</textarea></div>
    </div>`;
  }

  function editEntry(e) {
    const gid = $('#game-list').value;
    if (!gid) return toast('先选游戏', true);
    const isNew = !e;
    openModal(isNew ? '新增世界书条目' : '编辑世界书条目', entryForm(e), async () => {
      const eid = ($('#e-id').value || '').trim();
      if (!eid) throw new Error('条目 ID 不能为空');
      await api(`/api/games/${encodeURIComponent(gid)}/entries/${encodeURIComponent(eid)}`, {
        method: 'PUT',
        body: { title: $('#e-title').value, content: $('#e-content').value,
                keys: $('#e-keys').value, aliases: $('#e-aliases').value,
                always: $('#e-always').checked, chapter: $('#e-chapter').value,
                spoiler: $('#e-spoiler').checked, priority: Number($('#e-prio').value || 0) },
      });
      await loadGameText(gid);
      toast('已保存条目');
    });
  }

  async function saveGameText() {
    const gid = $('#game-list').value;
    if (!gid) return toast('先选一个游戏', true);
    try {
      const data = await api('/api/games/' + encodeURIComponent(gid), {
        method: 'PUT', body: { content: $('#game-text').value },
      });
      clearDirty('game');
      toast(`已保存 ${gid}（${data.chars} 字）`);
      await loadGames();
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  async function createGame() {
    const gid = ($('#game-id-input').value || '').trim();
    if (!gid) return toast('填一个游戏 ID', true);
    try {
      await api('/api/games', { method: 'POST', body: { id: gid } });
      toast('已新建；点「保存」设成当前游戏');
      await loadGames();
      $('#game-list').value = gid;     // 停在新游戏上（只是浏览）
      const nameInput = $('#game-id-input');
      if (nameInput) nameInput.value = gid;
      await loadGameText(gid);
    } catch (e) { toast(e.message, true); }
  }

  async function deleteGame() {
    const gid = $('#game-list').value;
    if (!gid) return;
    if (!confirm(`确定删除游戏资料 ${gid}？`)) return;
    try {
      await api(`/api/games/${encodeURIComponent(gid)}?confirm=1`, { method: 'DELETE' });
      toast('已删除'); await loadGames();
    } catch (e) { toast(e.message, true); }
  }

  // 保存当前游戏：写 config.yaml 的 game.id（启用开关在概览页）
  async function saveGameSelection() {
    const gid = $('#game-list').value;
    if (!gid) return;
    try {
      const data = await api('/api/games/select', {
        method: 'POST', body: { id: gid },
      });
      // 同步本地配置快照，否则下一次“保存”会用旧 game.id 覆盖刚切换的世界书。
      if (S.config) {
        S.config.game = { ...(S.config.game || {}), id: data.id || gid };
        delete S.config.game.path;
      }
      clearDirty('config');
      toast('已保存当前世界书；runtime 下一轮自动切换记忆');
      S.games = S.games.map((g) => ({ ...g, current: g.id === gid }));
      renderGamePicker();          // 只刷下拉，不动正在编辑的正文
      await loadGameMemory();
      await refreshStatus();
    } catch (e) { toast(e.message, true); }
  }

  // ── 记忆 ─────────────────────────────────────────────
  const MEMORY_KIND_LABELS = {
    summary: '陪玩记忆', plot: '剧情', npc: '人物', quests: '任务',
  };

  function memoryKindLabel(k) {
    return MEMORY_KIND_LABELS[k] || k;
  }

  function fmtUsed(ts) {
    if (!ts) return '—';
    try { return new Date(Number(ts) * 1000).toLocaleString(); }
    catch (e) { return '—'; }
  }

  function fmtSpan(m) {
    const start = Number(m.start_ts || 0);
    const end = Number(m.end_ts || 0) || start;
    if (!start && !end) return '时间不详';
    try {
      const a = new Date((start || end) * 1000);
      const b = new Date(end * 1000);
      const day = (d) => `${d.getMonth() + 1}月${d.getDate()}日`;
      const hm = (d) => `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
      if (a.toDateString() === b.toDateString()) {
        return end - start >= 60 ? `${day(a)} ${hm(a)}–${hm(b)}` : `${day(a)} ${hm(a)}`;
      }
      return `${day(a)} ${hm(a)}–${day(b)} ${hm(b)}`;
    } catch (e) { return '时间不详'; }
  }

  function memoryRows(items, scope) {
    return items.map((m) => `
      <tr>
        <td>${scope === 'live' ? esc(fmtSpan(m)) : `<strong>${esc(m.title || '')}</strong>`}</td>
        <td>${scope === 'live' ? '' : `<div class="muted">${esc(m.content || '')}</div>`}
          ${scope === 'live' ? `<div>${esc(m.content || '')}</div>` : ''}</td>
        <td>${(m.keywords || []).map((k) => `<span class="pill">${esc(k)}</span>`).join(' ')}</td>
        <td>${m.has_vector ? '<span class="pill green">有</span>' : '<span class="pill">待补</span>'}</td>
        <td class="muted">${esc(fmtUsed(m.last_used_at))}</td>
        <td><button class="small" data-mem-edit="${esc(m.id)}">编辑</button>
        <button class="small danger" data-mem-del="${esc(m.id)}">删除</button></td>
      </tr>`).join('');
  }

  async function loadCompanionMemory() {
    const box = $('#companion-table');
    if (!box) return;
    const data = await api('/api/memories?scope=live&kind=summary&limit=5000');
    const items = (data.items || []).slice().sort(
      (a, b) => Number(b.end_ts || 0) - Number(a.end_ts || 0));
    if (!items.length) {
      box.innerHTML = '<div class="muted">还没有陪玩记忆。</div>';
      return;
    }
    box.innerHTML = `<table><thead><tr><th>时间</th><th>内容</th><th>关键词</th>
      <th>向量</th><th>最后想起</th><th></th></tr></thead>
      <tbody>${memoryRows(items, 'live')}</tbody></table>`;
    bindMemoryButtons(box, items, loadCompanionMemory);
  }

  async function loadGameMemory() {
    const box = $('#game-memory-table');
    if (!box) return;
    const kind = S.gameMemKind || 'plot';
    const data = await api(`/api/memories?scope=game&kind=${kind}&limit=5000`);
    const items = data.items || [];
    const title = $('#game-memory-title');
    if (title) {
      const gid = String((((S.config || {}).game || {}).id) || '');
      title.textContent = gid ? `当前世界书：${gid}` : '当前世界书';
    }
    if (!items.length) {
      box.innerHTML = `<div class="muted">还没有${memoryKindLabel(kind)}记忆。</div>`;
      return;
    }
    box.innerHTML = `<table><thead><tr><th>标题</th><th>内容</th><th>关键词</th>
      <th>向量</th><th>最后想起</th><th></th></tr></thead>
      <tbody>${memoryRows(items, 'game')}</tbody></table>`;
    bindMemoryButtons(box, items, loadGameMemory);
  }

  function bindMemoryButtons(box, items, reload) {
    box.querySelectorAll('[data-mem-edit]').forEach((b) => {
      b.onclick = () => editMemory(items.find((x) => x.id === b.dataset.memEdit));
    });
    box.querySelectorAll('[data-mem-del]').forEach((b) => {
      b.onclick = async () => {
        if (!confirm('删除这条记忆？')) return;
        await api('/api/memories/' + b.dataset.memDel, { method: 'DELETE' });
        await reload(); await refreshStatus();
      };
    });
  }

  function memoryForm(m, scope, kind) {
    m = m || { title: '', content: '', keywords: [] };
    return `
      <div class="form-grid">
        <div class="muted wide">${esc(memoryKindLabel(kind))}（${scope === 'live' ? '全局' : '当前世界书'}）</div>
        <div class="field wide"><label>标题</label><input id="m-title" value="${esc(m.title || '')}"></div>
        <div class="field wide"><label>内容</label><textarea id="m-content" rows="8" data-warn="1">${esc(m.content || '')}</textarea></div>
        <div class="field wide"><label>关键词（逗号分隔）</label><input id="m-keys" value="${esc((m.keywords || []).join(', '))}"></div>
      </div>
      <div class="muted">改内容会清掉旧向量，下一轮后台补。</div>`;
  }

  function editMemory(m, scope = '', kind = '') {
    const isNew = !m || !m.id;
    scope = scope || (m && m.scope) || 'live';
    kind = kind || (m && m.kind) || 'summary';
    openModal(isNew ? `新增${memoryKindLabel(kind)}` : `编辑${memoryKindLabel(kind)}`,
      memoryForm(m, scope, kind), async () => {
        const body = {
          scope, kind,
          title: $('#m-title').value, content: $('#m-content').value,
          keywords: $('#m-keys').value,
        };
        if (isNew) await api('/api/memories', { method: 'POST', body });
        else await api('/api/memories/' + m.id, { method: 'PUT', body });
        if (scope === 'live') await loadCompanionMemory();
        else await loadGameMemory();
        await refreshStatus();
        toast('已保存');
      });
  }

  // ── 语义模型 ────────────────────────────────────────
  function fmtBytes(n) {
    const v = Number(n || 0);
    if (v >= 1e9) return (v / 1e9).toFixed(2) + ' GB';
    if (v >= 1e6) return (v / 1e6).toFixed(0) + ' MB';
    if (v >= 1e3) return (v / 1e3).toFixed(0) + ' KB';
    return v + ' B';
  }

  async function loadEmbedModels() {
    const box = $('#embed-models');
    if (!box) return;
    try {
      const data = await api('/api/memory/model');
      renderEmbedModels(data);
    } catch (e) {
      box.innerHTML = `<div class="muted">模型列表读取失败：${esc(e.message)}</div>`;
    }
  }

  function renderEmbedModels(data) {
    const box = $('#embed-models');
    if (!box) return;
    const models = data.models || [];
    const jobs = {};
    for (const j of (data.jobs || [])) jobs[j.model] = j;
    box.innerHTML = `<table class="wide"><thead><tr><th>模型</th><th>语言</th>
      <th>上下文</th><th>下载大小</th><th>峰值内存</th><th>CPU 延迟<br>(512 tokens)</th>
      <th>C-MTEB</th><th>MTEB</th><th>MMTEB</th><th>状态</th><th></th></tr></thead>
      <tbody>${models.map((m) => {
        const job = jobs[m.id];
        const running = job && job.status === 'running';
        let state = m.downloaded ? '已下载' : '未下载';
        if (running) {
          const pct = job.total ? Math.min(100, Math.round(100 * job.bytes / job.total)) : 0;
          state = `${esc(job.stage || '下载')} ${pct}%（${fmtBytes(job.bytes)} / ${fmtBytes(job.total)}）`;
        } else if (job && job.status === 'failed') {
          state = `下载失败：${esc(job.error || '')}`;
        }
        const stateHtml = running || (job && job.status === 'failed')
          ? `<div>${state}</div>` : `<div class="status-line">${state}</div>`;
        const ctx = m.max_context
          ? Number(m.max_context).toLocaleString() : '—';
        const current = m.current ? '<div class="status-line">当前使用</div>' : '';
        const use = m.downloaded && !m.current && !running
          ? `<button class="small" data-embed-use="${esc(m.id)}">使用</button>` : '';
        const dl = !m.downloaded && !running
          ? `<button class="small" data-embed-dl="${esc(m.id)}">下载</button>` : '';
        const del = m.downloaded
          ? `<button class="small danger" data-embed-del="${esc(m.id)}"
               ${m.current || running ? 'disabled title="当前使用中的模型不能删除"' : ''}>删除</button>` : '';
        return `<tr><td><strong>${esc(m.name)}</strong><div class="muted">${esc(m.note || '')}</div></td>
          <td>${esc(m.langs || '—')}</td><td>${ctx}</td>
          <td>${esc(m.size)}</td><td>${esc(m.mem || '—')}</td>
          <td>${esc(m.latency || '—')}</td>
          <td>${esc(m.cmteb || '—')}</td><td>${esc(m.mteb || '—')}</td><td>${esc(m.mmteb || '—')}</td>
          <td>${stateHtml}${current}</td>
          <td>${use}${dl}${del}</td></tr>`;
      }).join('')}</tbody></table>
      <div class="muted">模型目录：${esc(data.root || 'state/embed')}<br>
      MTEB（越高越好）；C-MTEB = 中文评分；MTEB = 英文评分；MMTEB = 多语言评分。CPU 延迟和内存跟 CPU/线程数强相关，只作横向参考。</div>`;
    box.querySelectorAll('[data-embed-dl]').forEach((b) => {
      b.onclick = () => downloadEmbed(b.dataset.embedDl);
    });
    box.querySelectorAll('[data-embed-del]').forEach((b) => {
      if (b.disabled) return;
      b.onclick = () => deleteEmbed(b.dataset.embedDel);
    });
    box.querySelectorAll('[data-embed-use]').forEach((b) => {
      b.onclick = () => useEmbedModel(b.dataset.embedUse);
    });
  }

  async function downloadEmbed(model) {
    if (!confirm(`下载 ${model}？下载完成后需要重启 runtime。`)) return;
    try {
      const r = await api('/api/memory/model/download', {
        method: 'POST', body: { model, confirm: true },
      });
      if (r.already) { toast('已经下载好了'); await loadEmbedModels(); return; }
      await pollEmbedJob(r.job.id);
    } catch (e) { toast(e.message, true); }
  }

  async function pollJob(jid, path, refresh, doneText, failText) {
    const tick = async () => {
      try {
        const r = await api(path + encodeURIComponent(jid));
        await refresh();
        if (r.job.status === 'running') {
          setTimeout(tick, 1200);
        } else if (r.job.status === 'failed') {
          toast(failText + (r.job.error || '未知错误'), true);
        } else {
          toast(doneText);
        }
      } catch (e) { toast(e.message, true); }
    };
    setTimeout(tick, 800);
  }

  async function pollEmbedJob(jid) {
    await pollJob(jid, '/api/memory/model/download/', loadEmbedModels,
      '模型下载完成；重启 runtime 后生效', '下载失败：');
  }

  async function deleteEmbed(model) {
    if (!confirm(`删除模型 ${model}？`)) return;
    try {
      await api('/api/memory/model/delete', {
        method: 'POST', body: { model, confirm: true },
      });
      toast('已删除');
      await loadEmbedModels();
    } catch (e) { toast(e.message, true); }
  }

  async function useEmbedModel(model) {
    try {
      await api('/api/config', { method: 'PATCH', body: { patch: { memory: { embed_model: model } } } });
      S.config.memory = { ...(S.config.memory || {}), embed_model: model };
      await refreshSchema();
      const sel = document.querySelector('.cfg-input[data-path="memory.embed_model"]');
      if (sel) sel.value = model;
      await loadEmbedModels();
      toast('已切换语义模型；重启 runtime 后加载');
    } catch (e) { toast(e.message, true); }
  }

  // ── 语音 ────────────────────────────────────────────
  // tts 下这几个键是配置结构本身，不能当服务名。
  const TTS_RESERVED_NAMES = new Set(['enabled', 'source', 'common', 'none']);

  function ttsPreset(id) {
    return (S.schema.tts_presets || []).find((p) => p.id === id) || null;
  }

  function ttsProfile(id) {
    return (S.schema.tts_profiles || []).find((p) => p.name === id) || null;
  }

  function ttsCurrent() {
    return ttsProfile(S.ttsProfile);
  }

  function ttsEffectiveSource() {
    return String(((S.config || {}).tts || {}).source || '');
  }

  function ttsProfileBlock(name) {
    const t = (S.config || {}).tts || {};
    return (t[name] && typeof t[name] === 'object') ? t[name] : {};
  }

  function clearTtsSecrets(name) {
    for (const k of Object.keys(S.secretSets)) {
      if (k.startsWith(`tts.${name}.`)) delete S.secretSets[k];
    }
    for (const k of [...S.secretClears]) {
      if (k.startsWith(`tts.${name}.`)) S.secretClears.delete(k);
    }
  }

  function ttsFieldsFor(name, presetId, block, configured) {
    const preset = ttsPreset(presetId);
    if (!preset) return [];
    const out = [];
    for (const meta of preset.fields) {
      out.push({
        key: meta.key,
        path: `tts.${name}.${meta.key}`, label: meta.label,
        type: meta.type, value: (block[meta.key] ?? meta.default),
        secret: false, configured: false, options: meta.options || [],
        help: meta.help || '', doc: meta.doc || '',
        required: !!meta.required,
      });
    }
    if (preset.auth) {
      const field = {
        key: 'api_key', path: `tts.${name}.api_key`, label: 'API Key',
        type: 'password', value: '', secret: true,
        configured: !!(configured && configured[`tts.${name}.api_key`]),
        options: [], help: '去官网注册/申请后粘贴；不会回显',
        doc: preset.doc || '', required: true,
      };
      const modelAt = out.findIndex((f) => f.key === 'model');
      const baseAt = out.findIndex((f) => f.key === 'base_url');
      out.splice(modelAt >= 0 ? modelAt + 1 : (baseAt >= 0 ? baseAt + 1 : 0),
                 0, field);
    }
    return out;
  }

  function applyTtsStreamState() {
    const stream = $('.cfg-input[data-path="tts.common.stream"]');
    const segments = $('.cfg-input[data-path="tts.common.segments"]');
    if (!segments) return;
    const on = !!(stream && stream.checked);
    segments.disabled = on;
    segments.title = on ? '流式合成开启时分段模式不生效' : '';
  }

  function renderTtsSection(section, container) {
    if (!container) return;
    container.innerHTML = '';
    if (!section) return;
    const grid = document.createElement('div');
    grid.className = 'form-grid';
    for (const f of section.fields || []) grid.appendChild(fieldEl(f));
    container.appendChild(grid);
  }

  function renderTtsSettings() {
    const group = (S.schema.groups || []).find((g) => g.id === 'tts');
    const sections = (group && group.sections) || [];
    renderTtsSection(sections.find((s) => s.title === '通用'),
                     $('#tts-common-form'));
    renderTtsSection(sections.find((s) => s.title === '出口过滤'),
                     $('#tts-guard-form'));
    const stream = $('.cfg-input[data-path="tts.common.stream"]');
    if (stream) {
      stream.addEventListener('change', () => {
        markDirty();
        applyTtsStreamState();
      });
    }
    applyTtsStreamState();
  }

  function renderTts() {
    const profiles = S.schema.tts_profiles || [];
    const effective = ttsEffectiveSource();
    if (!S.ttsProfile || !profiles.some((p) => p.name === S.ttsProfile)) {
      S.ttsProfile = effective || (profiles[0] ? profiles[0].name : '');
    }
    const sel = $('#tts-profile');
    if (sel) {
      sel.onchange = null;
      sel.innerHTML = '';
      const counts = {};
      for (const p of profiles) counts[p.label] = (counts[p.label] || 0) + 1;
      const seen = {};
      for (const p of profiles) {
        const o = document.createElement('option');
        o.value = p.name;
        let text = p.label;
        if (counts[p.label] > 1) {
          seen[p.label] = (seen[p.label] || 0) + 1;
          text += ` · ${seen[p.label]}`;
        }
        o.textContent = text;
        sel.appendChild(o);
      }
      sel.value = S.ttsProfile;
      sel.onchange = () => browseTtsProfile(sel.value);
    }

    const p = ttsCurrent();
    const source = $('#tts-source');
    if (source) source.value = S.ttsProfile;
    const presetInput = $('#tts-preset');
    const vendorSel = $('#tts-vendor');
    const box = $('#tts-fields');
    const doc = $('#tts-doc');
    const note = $('#tts-profile-note');
    if (box) box.innerHTML = '';
    if (!p) {
      if (vendorSel) { vendorSel.innerHTML = ''; vendorSel.disabled = true; }
      if (note) note.textContent = '配置里没有可用的声音';
      return;
    }

    const preset = ttsPreset(p.preset);
    if (presetInput) {
      presetInput.dataset.path = `tts.${p.name}.preset`;
      presetInput.value = p.preset;
    }
    if (vendorSel) {
      vendorSel.onchange = null;
      vendorSel.innerHTML = '';
      for (const x of (S.schema.tts_presets || [])) {
        const o = document.createElement('option');
        o.value = x.id;
        o.textContent = x.label;
        vendorSel.appendChild(o);
      }
      vendorSel.value = p.preset;
      vendorSel.disabled = p.name === 'edge';
      vendorSel.onchange = () => switchTtsVendor(vendorSel.value);
    }
    if (doc) {
      doc.href = (preset && preset.doc) || '#';
      doc.hidden = !(preset && preset.doc);
    }
    if (note) {
      note.textContent = ((preset && preset.desc) || '')
        + ' · ' + (p.active ? '当前生效' : '点「保存」后生效');
    }

    const block = p.block || ttsProfileBlock(p.name);
    const configured = {};
    for (const f of (p.fields || [])) {
      if (f.secret) configured[f.path] = f.configured && !S.secretClears.has(f.path);
    }
    const fields = ttsFieldsFor(p.name, p.preset, block, configured);
    p.fields = fields;
    for (const f of fields) box.appendChild(fieldEl(f));
  }

  async function browseTtsProfile(name) {
    if (!name || name === S.ttsProfile) return;
    if (DIRTY.config
        && !confirm('当前声音还有未保存的修改，切换会丢失。继续？')) {
      const sel = $('#tts-profile');
      if (sel) sel.value = S.ttsProfile;
      return;
    }
    try {
      await refreshSchema();
    } catch (e) {
      toast('刷新配置失败：' + e.message, true);
      return;
    }
    clearDirty('config');
    clearTtsSecrets(S.ttsProfile);
    S.ttsProfile = name;
    renderTts();
    renderTtsSettings();
  }

  function ttsFieldsTouched() {
    const p = ttsCurrent();
    if (!p) return false;
    for (const f of (p.fields || [])) {
      const el = $(`.cfg-input[data-path="${f.path}"]`);
      if (!el) continue;
      if (f.secret) {
        if (el.value.trim()) return true;
        continue;
      }
      const initial = f.value == null ? '' : String(f.value);
      if (String(el.value) !== initial) return true;
    }
    return false;
  }

  function switchTtsVendor(next) {
    const p = ttsCurrent();
    if (!p || !next || next === p.preset) return;
    const sel = $('#tts-vendor');
    if (p.name === 'edge') {
      if (sel) sel.value = p.preset;
      return;
    }
    if (ttsFieldsTouched()
        && !confirm('切换来源会清空该声音已填字段（API Key 需重新填），继续？')) {
      if (sel) sel.value = p.preset;
      return;
    }
    p.preset = next;
    p.label = (ttsPreset(next) || {}).label || next;
    p.block = {};
    // 换来源等于换声音：旧 key（含刚输入未保存的）不再适用，保存时一并清掉。
    clearTtsSecrets(p.name);
    S.secretClears.add(`tts.${p.name}.api_key`);
    markDirty('config');
    renderTts();
  }

  function newTts() {
    // 声音名和模型/用户页一致：先填名字再新建，名字直接当 tts.<name> 的键。
    const input = $('#tts-profile-name');
    const name = String((input && input.value) || '').trim();
    if (!/^[a-z0-9_-]{1,40}$/.test(name)) {
      toast('声音名称只能用小写字母/数字/下划线/短横线（1–40 字）', true);
      if (input) input.focus();
      return;
    }
    if (TTS_RESERVED_NAMES.has(name)) {
      toast(`「${name}」是配置保留字，换一个名字`, true);
      if (input) input.focus();
      return;
    }
    if (ttsProfile(name)) {
      toast(`声音「${name}」已经存在`, true);
      if (input) input.focus();
      return;
    }
    if (DIRTY.config
        && !confirm('当前声音还有未保存的修改，新建会丢失。继续？')) return;
    const preset = ttsPreset('edge') || { label: 'Edge' };
    clearTtsSecrets(S.ttsProfile);
    S.schema.tts_profiles.push({
      name, preset: 'edge', label: preset.label, active: false,
      unsaved: true, block: {}, fields: [],
    });
    S.ttsProfile = name;
    if (input) input.value = '';
    clearDirty('config');
    markDirty('config');
    renderTts();
    renderTtsSettings();
    toast(`已新建 ${preset.label}：${name}；选好来源、填好后点「保存」`);
  }

  async function deleteTts() {
    const p = ttsCurrent();
    if (!p) return;
    if (p.name === 'edge') return toast('内置 Edge 不能删除', true);
    if (!confirm(`删除声音「${p.label}」？`)) return;
    clearTtsSecrets(p.name);
    if (p.unsaved) {
      S.schema.tts_profiles = S.schema.tts_profiles.filter(
        (x) => x.name !== p.name);
      S.ttsProfile = ttsEffectiveSource();
      clearDirty('config');
      renderTts();
      renderTtsSettings();
      return;
    }
    const wasSource = p.name === ttsEffectiveSource();
    try {
      if (wasSource) {
        const fallback = ((S.schema.tts_profiles.find(
          (x) => x.name !== p.name) || {}).name) || 'edge';
        await api('/api/config', {
          method: 'PATCH', body: { patch: { tts: { source: fallback } } },
        });
        deepMerge(S.config, { tts: { source: fallback } });
      }
      await api('/api/config', {
        method: 'PATCH', body: { path: `tts.${p.name}`, delete: true },
      });
      await bootstrap(true);
      clearDirty('config');
      S.ttsProfile = ttsEffectiveSource();
      renderTts();
      toast('已删除');
    } catch (e) { toast('删除失败：' + e.message, true); }
  }

  async function saveTts() {
    const p = ttsCurrent();
    if (!p) return;
    for (const f of (p.fields || [])) {
      if (!f.required) continue;
      const el = $(`.cfg-input[data-path="${f.path}"]`);
      if (f.secret) {
        if (!el || (!el.value.trim() && !f.configured)) {
          toast(`${f.label} 不能为空`, true);
          if (el) el.focus();
          return;
        }
        continue;
      }
      if (!el || !String(el.value).trim()) {
        toast(`${f.label} 不能为空`, true);
        if (el) el.focus();
        return;
      }
    }
    const paths = new Set(['tts.source', `tts.${p.name}.preset`]);
    for (const f of (p.fields || [])) paths.add(f.path);
    for (const path of groupPaths(['tts'])) paths.add(path);
    const src = $('#tts-source');
    if (src) src.value = p.name;
    const pre = $('#tts-preset');
    if (pre) {
      pre.dataset.path = `tts.${p.name}.preset`;
      pre.value = p.preset;
    }
    const ok = await saveConfig(paths, `声音（${p.label}）`);
    if (ok) renderTts();
  }

  function renderStt() {
    renderGroups(['stt'], $('#stt-form'));
    renderGroups(['mic'], $('#mic-form'));
    // 麦克风和识别引擎共用 config dirty；两个按钮都保存两组，避免只保存一半。
    saveBar($('#mic-form'), groupPaths(['mic', 'stt']), '麦克风');
    saveBar($('#stt-form'), groupPaths(['mic', 'stt']), '识别引擎');
    loadSttModels();
  }

  // 下载完成、或者刚进来时：顺手把 stt.model 的下拉也刷成"已下载的那些"，
  // 不用整页 bootstrap（那会把没保存的改动冲掉）。
  function refreshModelSelect(models) {
    const sel = $('select.cfg-input[data-path="stt.model"]');
    if (!sel) return;
    const prev = sel.value;
    const saved = String(((S.config || {}).stt || {}).model || '');
    sel.innerHTML = '';
    for (const m of models) {
      // 只列 stt.models 里配置过的模型；否则保存后 runtime 找不到引擎。
      if (!m.configured && m.id !== prev && m.id !== saved) continue;
      if (!m.downloaded && m.id !== prev && m.id !== saved) continue;
      const o = document.createElement('option');
      o.value = m.id;
      o.textContent = `${m.name}${m.downloaded ? '' : '（没装齐）'}`;
      sel.appendChild(o);
    }
    if (prev && Array.from(sel.options).some((o) => o.value === prev)) sel.value = prev;
    else if (saved && Array.from(sel.options).some((o) => o.value === saved)) sel.value = saved;
  }

  async function loadSttModels() {
    try {
      const data = await api('/api/stt/models');
      S.sttModels = data.models || [];
      S.sttRoot = data.root || '';
      S.sttJobs = data.jobs || [];
      refreshModelSelect(S.sttModels);
      const box = $('#stt-models');
      if (!box) return;
      const jobs = {};
      for (const j of S.sttJobs) jobs[j.model] = j;
      const rows = S.sttModels.map((m) => {
        const job = jobs[m.id];
        const running = job && job.status === 'running';
        let state = m.downloaded ? '已下载' : '未下载';
        if (running) {
          const pct = job.total ? Math.min(100, Math.round(100 * job.bytes / job.total)) : 0;
          state = `${esc(job.stage || '下载')} ${pct}%（${fmtBytes(job.bytes)} / ${fmtBytes(job.total)}）`;
        } else if (job && job.status === 'failed') {
          state = `下载失败：${esc(job.error || '')}`;
        }
        const stateHtml = running || (job && job.status === 'failed')
          ? `<div>${state}</div>` : `<div class="status-line">${state}</div>`;
        const current = m.current ? '<div class="status-line">当前使用</div>' : '';
        const dl = !m.downloaded && m.downloadable && !running
          ? `<button class="small" data-stt-dl="${esc(m.id)}">下载</button>` : '';
        const del = m.downloaded
          ? `<button class="small danger" data-stt-del="${esc(m.id)}"
               ${m.current || running ? 'disabled title="当前使用中的模型不能删除"' : ''}>删除</button>` : '';
        const manual = !m.downloaded && !m.downloadable
          ? '<span class="muted">手动放置</span>' : '';
        return `<tr><td><strong>${esc(m.name)}</strong>
            <div class="muted">${esc(m.note || '')}</div></td>
          <td class="m-lang"><span>${esc(m.langs || '—')}</span></td>
          <td>${esc(m.size || '')}</td>
          <td>${esc(m.rtf || '—')}</td>
          <td>${esc(m.mem || '—')}</td>
          <td>${esc(m.aishell1 || '—')}</td>
          <td>${esc(m.librispeech || '—')}</td>
          <td>${stateHtml}${current}</td>
          <td>${dl}${del}${manual}</td></tr>`;
      }).join('');
      box.innerHTML = `<table class="wide"><thead><tr>
        <th>模型</th><th>语言</th><th>下载大小</th><th>CPU RTF</th><th>峰值内存</th>
        <th>AISHELL-1</th><th>LibriSpeech</th><th>状态</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table>
        <div class="muted">模型目录：${esc(S.sttRoot || '—')}<br>
        AISHELL-1 = 中文 CER（越低越好）；LibriSpeech = 英文 WER（越低越好）。RTF 和内存跟 CPU/线程数强相关，只作横向参考。</div>`;
      box.querySelectorAll('[data-stt-dl]').forEach((b) => {
        b.onclick = () => downloadStt(b.dataset.sttDl);
      });
      box.querySelectorAll('[data-stt-del]').forEach((b) => {
        if (b.disabled) return;
        b.onclick = () => deleteStt(b.dataset.sttDel);
      });
    } catch (e) { /* ignore */ }
  }

  async function deleteStt(model) {
    const m = S.sttModels.find((x) => x.id === model);
    if (!confirm(`删除模型 ${m ? m.name : model}？\n会删除 ${S.sttRoot || '模型目录'} 下该模型的文件，不影响其他模型。`)) return;
    try {
      await api('/api/stt/delete', { method: 'POST', body: { model, confirm: true } });
      toast('已删除：' + (m ? m.name : model));
      await loadSttModels();
    } catch (e) { toast('删除失败：' + e.message, true); }
  }

  async function downloadStt(model) {
    const m = S.sttModels.find((x) => x.id === model);
    const where = S.sttRoot || '模型目录';
    const msg = `下载 ${m ? m.name : model}？\n大小 ${m ? m.size : '未知'}，会解压到：\n${where}\n不会自动切换，下载后需在页面选择并保存/重启。`;
    if (!confirm(msg)) return;
    try {
      const data = await api('/api/stt/download', { method: 'POST', body: { model, confirm: true } });
      if (data.already) { toast('已经下载过了'); await loadSttModels(); return; }
      toast('开始下载：' + model);
      await pollJob(data.job.id, '/api/stt/download/', loadSttModels,
        '下载完成：' + model, '下载失败：');
    } catch (e) { toast(e.message, true); }
  }

  async function previewTts() {
    const p = ttsCurrent();
    if (!p) return toast('先选一个声音', true);
    const btn = $('#btn-tts-preview');
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '合成中…';
    try {
      const block = {};
      for (const f of (p.fields || [])) {
        if (f.secret) continue;
        const el = $(`.cfg-input[data-path="${f.path}"]`);
        if (!el) continue;
        const r = readCfgValue(el);
        if (!r.skip) block[f.path.split('.').pop()] = r.value;
      }
      const body = {
        profile: p.name, preset: p.preset, block,
        text: $('#tts-preview-text').value,
      };
      const apiKey = S.secretSets[`tts.${p.name}.api_key`];
      if (apiKey) body.api_key = apiKey;
      const res = await fetch('/api/tts/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': S.csrf },
        body: JSON.stringify(body),
      });
      const ct = res.headers.get('content-type') || '';
      if (!res.ok || ct.includes('application/json')) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.error || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const audio = $('#tts-audio');
      audio.src = URL.createObjectURL(blob);
      audio.classList.remove('hidden');
      audio.play().catch(() => {});
    } catch (e) { toast('试听失败：' + e.message, true); }
    finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  // ── 角色（形象 + 角色设定）──────────────────────────
  function schedulePreview() {
    clearTimeout(schedulePreview._t);
    schedulePreview._t = setTimeout(refreshPreview, 180);
  }

  function roleLabel(a) {
    const roleId = String(a.role_id || a.name || a.id || '');
    const display = String(a.display_name || a.label || '');
    if (a.kind === 'error') return roleId;
    const tail = a.kind === 'image' ? '（图片）'
      : (a.kind === 'spine' ? '（Spine）' : (a.usable === false ? '（不可用）' : ''));
    const bad = a.profile_error ? '（角色设定有错）' : '';
    // 下拉只显示给人看的名字；role.id 是存储键，放在下面的角色信息里。
    return (display || roleId) + tail + bad;
  }

  function renderRoleInfo() {
    const el2 = $('#role-info');
    if (!el2) return;
    const role = S.role || {};
    const list = (S.schema && S.schema.avatars) || [];
    const a = list.find((x) => x.id === S.selectedAvatar);
    if (!a) { el2.textContent = ''; return; }
    const bad = role.error || a.profile_error;
    const effective = String(((S.config || {}).role || {}).id || '');
    let text = `role.id：${role.role_id || a.role_id || a.name || a.id}`
      + ` · 显示名：${a.display_name || a.name}`;
    if (role.role_dir) text += ` · 设定目录：${role.role_dir}`;
    const activeRole = String(role.role_id || a.role_id || '');
    text += activeRole === effective
      ? ' · 当前生效' : ' · 未保存（点「保存」切换）';
    el2.textContent = bad ? `${text} · ⚠ ${bad}` : text;
    el2.style.color = bad ? 'var(--danger, #c0392b)' : '';
  }

  function renderRolePicker() {
    const sel = $('#role-list');
    if (!sel) return;
    const all = (S.schema && S.schema.avatars) || [];
    // 不可用的模型不进下拉（和 --scan 的判定同一套）；一个能用的都没有时照旧全列，
    // 否则用户不知道该去修哪个。
    const usable = all.filter((a) => a.usable !== false);
    const list = usable.length ? usable : all;
    sel.innerHTML = '';
    for (const a of list) {
      const opt = document.createElement('option');
      opt.value = String(a.id || '');
      opt.textContent = roleLabel(a);
      if (a.kind === 'live2d' && a.broken && a.broken.length) {
        opt.title = a.broken.join('；');
      }
      sel.appendChild(opt);
    }
    const current = list.some((a) => a.id === S.selectedAvatar)
      ? S.selectedAvatar : (list[0] ? String(list[0].id) : '');
    if (current) sel.value = current;
    S.selectedAvatar = current;
    renderRoleInfo();
    const live = list.filter((a) => a.kind === 'live2d');
    const images = list.filter((a) => a.kind === 'image');
    const spines = list.filter((a) => a.kind === 'spine');
    const note = $('#role-scan-note');
    if (note) {
      const rel = (S.schema && S.schema.model_dir_rel) || 'models';
      const hidden = all.length - usable.length;
      note.textContent = `${live.length} 套 Live2D · ${spines.length} 套 Spine · `
        + `${images.length} 张图片 · ${rel}`
        + (hidden > 0 ? ` · 已隐藏 ${hidden} 个不可用模型` : '');
    }
    // 损坏的模型不进下拉，但用户得知道该去修哪个：在这里逐条列出来（刷新后按当前扫描结果重画）。
    const broken = all.filter((a) => a.usable === false);
    const warn = $('#role-broken');
    if (warn) {
      warn.textContent = broken.length
        ? '损坏模型：' + broken.map((a) => {
          const why = (a.broken || []).filter(Boolean).join('；') || '不可用';
          return `${a.display_name || a.label || a.id}（${why}）`;
        }).join('、')
        : '';
      warn.hidden = !broken.length;
    }
  }

  function applyRolePayload(data) {
    if (Array.isArray(data.avatars)) S.schema.avatars = data.avatars;
    if (data.selected_avatar) {
      S.roleSeq += 1;
      S.selectedAvatar = data.selected_avatar;
      S.schema.selected_avatar = data.selected_avatar;
      // 保存后本地快照跟着走，别让下一次保存拿旧的 role.id 覆盖回去
      const rid = data.selected_role || (data.role && data.role.role_id)
        || data.selected_avatar;
      S.config.role = { ...(S.config.role || {}), id: rid };
    }
    if (data.role) S.role = data.role;
  }

  // 把某个角色的设定/人设/规则装进页面（只读，不写配置）
  async function loadRoleInto(id, seq) {
    const data = await api(`/api/roles/${encodeURIComponent(id)}/profile`);
    // 连续换角色时只认最后一次请求；迟到的旧响应不能把页面切回去。
    if (seq !== undefined && seq !== S.roleSeq) return;
    S.selectedAvatar = id;
    S.role = {
      id,
      role_id: data.role_id,
      display_name: data.display_name,
      profile: data.profile || {},
      role_dir: data.role_dir,
      error: data.error,
    };
    renderRolePicker();
    renderRoleProfile();
    renderRoleStage();
    // 三个读取互不依赖，串行 await 会把“换角色”拖成三倍往返；
    // 浏览器连接被 Inspector 控制请求占满时尤其明显。
    await Promise.all([
      loadRolePersona(),
      loadAvatarRules(id),
      loadAutonomy(id),
    ]);
    refreshPreview();
  }

  // 下拉选中 = 浏览：立刻换成这个角色的内容，但不写暂存配置
  async function browseRole(id) {
    if (!id || id === S.selectedAvatar) return;
    if (rolePageDirty()
        && !confirm('当前角色还有未保存的修改，切换会丢失。继续？')) {
      renderRolePicker();
      return;
    }
    const seq = ++S.roleSeq;
    try {
      clearDirty('profile'); clearDirty('persona'); clearDirty('rules');
      clearDirty('config');
      // 先让旧预览页卸载，释放它的 WebGL 上下文；否则连续换角色会卡在
      // 新模型创建上下文的阶段。
      const frame = $('#stage-preview');
      if (frame) {
        frame.src = 'about:blank';
        frame.dataset.src = '';
      }
      await loadRoleInto(id, seq);
    } catch (e) {
      if (seq !== S.roleSeq) return;
      toast('读取角色失败：' + e.message, true);
      renderRolePicker();
    }
  }

  // 保存当前选择：写 config.yaml 的 role.id（重启 runtime 后生效）
  async function saveRoleSelection() {
    const id = S.selectedAvatar;
    if (!id) return;
    try {
      const data = await api('/api/roles/select', { method: 'POST', body: { id } });
      applyRolePayload(data);
      renderRolePicker();
      renderRoleInfo();
      const label = roleLabel(
        (S.schema.avatars || []).find((a) => a.id === S.selectedAvatar) || {})
        || S.selectedAvatar;
      toast(`已保存当前角色「${label}」，重启 runtime 后生效`);
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  // 进入页面时回到"已生效"的角色，不保留上次没保存的浏览
  async function resetRoleToEffective() {
    const id = S.schema.selected_avatar
      || ((S.schema.avatars || []).find((a) => a.usable !== false) || {}).id || '';
    if (!id) return;
    const seq = ++S.roleSeq;
    try { await loadRoleInto(id, seq); }
    catch (e) { /* 读取失败保持现状，不打断切页 */ }
  }

  async function rescanRoles() {
    if (rolePageDirty()
        && !confirm('本页还有未保存的修改，重新扫描可能改变列表。继续？')) return;
    const btn = $('#btn-role-rescan');
    if (!btn) return;
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '扫描中…';
    try {
      const before = new Set((S.schema.avatars || []).map((a) => a.id));
      const data = await api('/api/roles/scan', { method: 'POST', body: {} });
      applyRolePayload(data);
      const live = S.schema.avatars.filter((a) => a.kind === 'live2d');
      const images = S.schema.avatars.filter((a) => a.kind === 'image');
      const added = S.schema.avatars.filter((a) => !before.has(a.id)).length;
      renderRolePicker();
      renderRoleProfile();
      renderRoleStage();
      renderStageGlobal();
      refreshPreview();
      await loadAvatarRules(S.selectedAvatar);
      await loadAutonomy(S.selectedAvatar);
      const tail = added ? `，新增 ${added} 个` : '，没有新增';
      toast(`扫描完成：${live.length} 套 Live2D，${images.length} 张图片${tail}`);
    } catch (e) {
      toast('扫描失败：' + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  // ── 动作规则（actions.md）────────────────────────────
  function rulesCharState() {
    const ta = $('#rules-text');
    const n = ta ? ta.value.length : 0;
    const state = $('#rules-state');
    if (state) state.textContent = `${n}/2000` + (DIRTY.rules ? ' · 未保存' : ' · 已保存');
  }

  function configEnabled(path, fallback = true) {
    const parts = String(path || '').split('.');
    let cur = S.config || {};
    for (const part of parts) {
      if (!cur || typeof cur !== 'object') return fallback;
      cur = cur[part];
    }
    return cur === undefined || cur === null ? fallback : !!cur;
  }

  async function setConfigToggle(path, checkbox, label) {
    const checked = !!checkbox.checked;
    const patch = {};
    const parts = path.split('.');
    let cur = patch;
    for (const part of parts.slice(0, -1)) cur = (cur[part] = cur[part] || {});
    cur[parts[parts.length - 1]] = checked;
    try {
      await api('/api/config', { method: 'PATCH', body: { patch } });
      deepMerge(S.config || (S.config = {}), patch);
      const other = $(`.cfg-input[data-path="${path}"]`);
      if (other) other.checked = checked;
      toast(`${label}已${checked ? '开启' : '关闭'}；重启 runtime 后生效`);
    } catch (e) {
      checkbox.checked = !checked;
      toast('保存失败：' + e.message, true);
    }
  }

  function renderActionToggles() {
    const rules = $('#rules-enabled');
    if (rules) rules.checked = configEnabled('stage.actions.enabled', true);
    const auto = $('#autonomy-enabled');
    if (auto) auto.checked = configEnabled('stage.autonomy.enabled', true);
  }

  function renderRules(data) {
    const caps = (data && data.capabilities) || {};
    const path = $('#rules-path');
    if (path) {
      const unknown = (data && data.unknown_tags) || [];
      path.textContent = ((data && data.error) ? '读取失败：' + data.error
        : ((data && data.path) || '')
          + (unknown.length ? ' · 未知标签：' + unknown.join(' / ') : ''));
    }
    const tags = $('#rules-tags');
    if (tags) {
      const acts = (caps.actions || []).join(' / ') || '无';
      const exprs = (caps.expressions || []).join(' / ') || '无';
      tags.textContent = `动作：${acts}　表情：${exprs}`;
    }
    const ta = $('#rules-text');
    const hasTags = !!((caps.actions || []).length || (caps.expressions || []).length);
    if (ta) {
      ta.value = (data && data.content) || '';
      ta.disabled = !data;
    }
    const save = $('#btn-rules-save');
    const clr = $('#btn-rules-clear');
    const gen = $('#btn-rules-generate');
    if (save) save.disabled = !data || !!data.error;
    if (clr) clr.disabled = !data || !!data.error;
    if (gen) gen.disabled = !data || !hasTags;
    clearDirty('rules');
    rulesCharState();
    renderActionToggles();
  }

  async function loadAvatarRules(id) {
    if (!id) { renderRules(null); return; }
    const targetId = id;
    const avatar = (S.schema.avatars || []).find((a) => a.id === id);
    if (avatar && !['live2d', 'spine'].includes(avatar.kind)) { renderRules(null); return; }
    try {
      const data = await api('/api/avatars/' + encodeURIComponent(id) + '/rules');
      if (S.selectedAvatar !== targetId) return;
      renderRules(data);
    } catch (e) {
      if (S.selectedAvatar !== targetId) return;
      renderRules(null);
      const ta = $('#rules-text');
      if (ta) { ta.value = ''; ta.disabled = true; }
      const path = $('#rules-path');
      if (path) path.textContent = e.message;
    }
  }

  async function saveAvatarRules() {
    const id = S.selectedAvatar;
    if (!id) return;
    const ta = $('#rules-text');
    if (!ta) return;
    const btn = $('#btn-rules-save');
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '保存中…';
    try {
      const data = await api('/api/avatars/' + encodeURIComponent(id) + '/rules', {
        method: 'PUT', body: { content: ta.value },
      });
      renderRules(data);
      const unknown = (data.unknown_tags || []);
      if (unknown.length) {
        toast('规则已保存，但有未知标签：' + unknown.join(' / '), true);
      } else {
        toast(data.exists ? '规则已保存（下一轮生效）' : '规则已清空');
      }
    } catch (e) { toast('保存失败：' + e.message, true); }
    finally { btn.disabled = false; btn.textContent = old; }
  }

  async function generateAvatarRules() {
    const id = S.selectedAvatar;
    if (!id) return;
    const ta = $('#rules-text');
    if (!ta) return;
    if ((ta.value.trim() || DIRTY.rules)
        && !confirm('生成会覆盖编辑框里的内容（不会自动保存）。继续？')) return;
    const btn = $('#btn-rules-generate');
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '生成中…';
    try {
      const data = await api('/api/avatars/' + encodeURIComponent(id)
        + '/rules/generate', { method: 'POST', body: {} });
      ta.value = data.content || '';
      markDirty('rules');
      rulesCharState();
      let note = '已按当前形象生成标签表，记得保存';
      if (data.unknown_tags && data.unknown_tags.length) {
        note += '；未知标签：' + data.unknown_tags.join(' / ');
      }
      toast(note, !!(data.unknown_tags && data.unknown_tags.length));
    } catch (e) { toast('生成失败：' + e.message, true); }
    finally { btn.disabled = false; btn.textContent = old; }
  }

  async function clearAvatarRules() {
    const ta = $('#rules-text');
    if (!ta || !confirm('清空并删除当前形象的 actions.md？')) return;
    ta.value = '';
    markDirty('rules');
    await saveAvatarRules();
  }

  function wireRules() {
    const ta = $('#rules-text');
    if (ta) ta.oninput = () => { markDirty('rules'); rulesCharState(); };
    const save = $('#btn-rules-save');
    if (save) save.onclick = saveAvatarRules;
    const gen = $('#btn-rules-generate');
    if (gen) gen.onclick = generateAvatarRules;
    const clr = $('#btn-rules-clear');
    if (clr) clr.onclick = clearAvatarRules;
    const toggle = $('#rules-enabled');
    if (toggle) {
      toggle.onchange = () => setConfigToggle(
        'stage.actions.enabled', toggle, '动作规则');
    }
    renderActionToggles();
  }

  // ── 自主驱动（motion_profile.json）───────────────────
  function autonomyButtons() {
    const exists = !!AUTONOMY.exists;
    const hasPreview = !!AUTONOMY.preview;
    const busy = AUTONOMY.busy;
    const audit = $('#btn-autonomy-audit');
    const gen = $('#btn-autonomy-generate');
    const apply = $('#btn-autonomy-apply');
    const del = $('#btn-autonomy-delete');
    const supported = AUTONOMY.supported !== false;
    if (audit) audit.disabled = busy || !supported;
    if (gen) gen.disabled = busy || !supported;
    if (apply) apply.disabled = busy || !supported || !hasPreview;
    if (del) del.disabled = busy || !supported || !exists;
  }

  function renderAutonomy(data, supported = true) {
    AUTONOMY.busy = false;
    AUTONOMY.supported = supported;
    AUTONOMY.exists = !!(data && data.exists);
    AUTONOMY.preview = (data && data.profile) || null;
    const path = $('#autonomy-path');
    if (path) path.textContent = (data && data.path) || '';
    const summary = $('#autonomy-summary');
    const issues = (data && data.issues) || [];
    if (summary) {
      if (!supported) {
        summary.textContent = '只有 Live2D 形象支持程序化自主驱动；Spine 只做动画级路由。';
      } else if (data && data.error) {
        summary.textContent = '读取失败：' + data.error;
      } else if (AUTONOMY.preview) {
        const p = AUTONOMY.preview;
        summary.textContent = `已有配置 · ${p.archetype || 'unknown'}`
          + ` · 动作 ${Object.keys(p.actions || {}).length}`
          + (issues.length ? ' · 校验提示：' + issues.join('；') : '');
      } else {
        summary.textContent = '尚无配置；运行时使用模型自省默认值。';
      }
    }
    const ta = $('#autonomy-preview');
    if (ta) {
      ta.value = AUTONOMY.preview
        ? JSON.stringify(AUTONOMY.preview, null, 2) : '';
    }
    const meta = $('#autonomy-meta');
    if (meta) meta.textContent = issues.length ? `${issues.length} 条校验提示` : '';
    renderActionToggles();
    autonomyButtons();
  }

  async function loadAutonomy(id) {
    if (!id) { renderAutonomy(null); return; }
    const avatar = (S.schema.avatars || []).find((a) => a.id === id);
    if (avatar && avatar.kind !== 'live2d') { renderAutonomy(null, false); return; }
    try {
      const data = await api('/api/avatars/' + encodeURIComponent(id) + '/autonomy');
      if (S.selectedAvatar === id) renderAutonomy(data);
    } catch (e) {
      renderAutonomy(null);
      const summary = $('#autonomy-summary');
      if (summary) summary.textContent = e.message;
    }
  }

  async function auditAutonomy() {
    const id = S.selectedAvatar;
    if (!id || AUTONOMY.busy) return;
    AUTONOMY.busy = true;
    autonomyButtons();
    const meta = $('#autonomy-meta');
    try {
      const data = await api('/api/avatars/' + encodeURIComponent(id)
        + '/autonomy/audit', { method: 'POST', body: {} });
      const sum = data.summary || {};
      if (meta) {
        meta.textContent = `tier=${sum.tier || 'none'} · 绑定 ${sum.bound || 0}`
          + ` · motion ${sum.motions || 0} · 表情 ${sum.expressions || 0}`
          + ((sum.warnings || []).length ? ` · ${sum.warnings.length} 条告警` : '');
      }
      toast('模型审计完成');
    } catch (e) {
      toast('审计失败：' + e.message, true);
    } finally {
      AUTONOMY.busy = false;
      autonomyButtons();
    }
  }

  async function generateAutonomy() {
    const id = S.selectedAvatar;
    if (!id || AUTONOMY.busy) return;
    if (!confirm('生成需要调用一次 LLM，结果只进预览、不会自动保存。继续？')) return;
    AUTONOMY.busy = true;
    autonomyButtons();
    const btn = $('#btn-autonomy-generate');
    const meta = $('#autonomy-meta');
    const ta = $('#autonomy-preview');
    const old = btn ? btn.textContent : '';
    const stages = {
      auditing: '审计模型中…',
      sending: '发送请求中…',
      thinking: '思考中…',
      outputting: '输出中…',
    };
    let gotFirst = false;
    let streamed = '';
    let gotResult = false;
    let currentStage = 'auditing';
    const startedAt = Date.now();
    const setStage = (stage) => {
      currentStage = stage;
      const text = stages[stage] || '生成中…';
      if (btn) btn.textContent = text;
      if (meta) meta.textContent = text;
    };
    const ticker = setInterval(() => {
      if (!AUTONOMY.busy || !meta || gotResult) return;
      const text = stages[currentStage] || '生成中…';
      meta.textContent = `${text} · ${Math.floor((Date.now() - startedAt) / 1000)}s`;
    }, 1000);
    setStage('auditing');
    if (ta) ta.value = '';
    try {
      const res = await fetch('/api/avatars/' + encodeURIComponent(id)
        + '/autonomy/generate/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': S.csrf,
        },
        body: '{}',
      });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).error || ''; } catch (_) { /* ignore */ }
        throw new Error(detail || `HTTP ${res.status}`);
      }
      if (!res.body) throw new Error('浏览器不支持流式响应');
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let split;
        while ((split = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const line = block.split('\n').find((x) => x.startsWith('data:'));
          if (!line) continue;
          let event;
          try { event = JSON.parse(line.slice(5).trim()); } catch (_) { continue; }
          if (event.type === 'status') {
            setStage(event.stage);
            if (!gotResult && (event.stage === 'sending'
                || event.stage === 'auditing')) {
              streamed = '';
              gotFirst = false;
              if (ta) ta.value = '';
            }
          } else if (event.type === 'delta') {
            if (!gotFirst) {
              gotFirst = true;
              if (ta) ta.value = '';
            }
            streamed += event.text || '';
            if (ta) {
              ta.value = streamed;
              ta.scrollTop = ta.scrollHeight;
            }
            if (meta) meta.textContent = `输出中… · ${streamed.length} 字`;
          } else if (event.type === 'error') {
            if (meta) meta.textContent = event.message || '生成失败';
            toast(event.message || '生成失败', true);
          } else if (event.type === 'result') {
            gotResult = true;
            if (S.selectedAvatar !== id) return;
            AUTONOMY.preview = event.profile;
            AUTONOMY.exists = AUTONOMY.exists || !!event.profile;
            if (ta) ta.value = JSON.stringify(event.profile, null, 2);
            const summary = $('#autonomy-summary');
            if (summary) {
              summary.textContent = `预览（未应用） · ${event.profile.archetype || 'unknown'}`
                + ` · 动作 ${Object.keys(event.profile.actions || {}).length}`;
            }
            const d = event.dossier || {};
            if (meta) {
              meta.textContent = `tier=${d.tier || 'none'} · 绑定 ${d.bound || 0}`
                + ` · motion ${d.motions || 0} · 表情 ${d.expressions || 0}`
                + (event.fallback ? ' · 已使用默认配置' : '');
            }
            const issues = event.issues || [];
            toast(event.fallback
              ? '生成未完全通过，已给出默认配置；检查后点「应用」'
              : (issues.length
                ? `生成完成，${issues.length} 条字段被丢弃/收敛；检查预览后点「应用」`
                : '生成完成，检查预览后点「应用」'),
              event.fallback || issues.length > 0);
          }
        }
      }
      if (!gotResult) throw new Error('流式连接结束，但没有收到结果');
    } catch (e) {
      if (meta) meta.textContent = '生成失败：' + e.message;
      toast('生成失败：' + e.message, true);
    } finally {
      clearInterval(ticker);
      AUTONOMY.busy = false;
      if (btn) btn.textContent = old;
      autonomyButtons();
    }
  }

  async function applyAutonomy() {
    const id = S.selectedAvatar;
    if (!id || !AUTONOMY.preview || AUTONOMY.busy) return;
    AUTONOMY.busy = true;
    autonomyButtons();
    try {
      const data = await api('/api/avatars/' + encodeURIComponent(id)
        + '/autonomy', {
        method: 'PUT', body: { profile: AUTONOMY.preview },
      });
      AUTONOMY.preview = data.profile || AUTONOMY.preview;
      AUTONOMY.exists = true;
      toast('自主驱动配置已保存；重启 runtime 后生效');
      await loadAutonomy(id);
    } catch (e) {
      toast('应用失败：' + e.message, true);
    } finally {
      AUTONOMY.busy = false;
      autonomyButtons();
    }
  }

  async function deleteAutonomy() {
    const id = S.selectedAvatar;
    if (!id || AUTONOMY.busy) return;
    if (!confirm('删除这个角色的 motion_profile.json？删除后回到模型自省默认值。')) return;
    AUTONOMY.busy = true;
    autonomyButtons();
    try {
      await api('/api/avatars/' + encodeURIComponent(id) + '/autonomy',
                { method: 'DELETE', body: {} });
      toast('已删除；重启 runtime 后回到自省默认值');
      await loadAutonomy(id);
    } catch (e) {
      toast('删除失败：' + e.message, true);
    } finally {
      AUTONOMY.busy = false;
      autonomyButtons();
    }
  }

  function wireAutonomy() {
    const audit = $('#btn-autonomy-audit');
    if (audit) audit.onclick = auditAutonomy;
    const gen = $('#btn-autonomy-generate');
    if (gen) gen.onclick = generateAutonomy;
    const apply = $('#btn-autonomy-apply');
    if (apply) apply.onclick = applyAutonomy;
    const del = $('#btn-autonomy-delete');
    if (del) del.onclick = deleteAutonomy;
    const toggle = $('#autonomy-enabled');
    if (toggle) {
      toggle.onchange = () => setConfigToggle(
        'stage.autonomy.enabled', toggle, '自主驱动');
    }
    renderActionToggles();
    autonomyButtons();
  }

  function refreshPreview(force = false) {
    let cfg;
    try { cfg = collectFormConfig(); }
    catch (e) { return; }
    const st = { ...(cfg.stage || {}), ...collectRoleStage() };
    st.bubble = { ...((cfg.stage || {}).bubble || {}), ...(st.bubble || {}) };
    const avatar = S.selectedAvatar || (($('#role-list') || {}).value || '');
    const p = new URLSearchParams({
      preview: '1', assets: '/ov', avatar,
      gap: String((st.bubble || {}).gap ?? 0.4),
      w: String((st.bubble || {}).max_width ?? 60),
      auto: (st.bubble || {}).auto_gap === false ? '0' : '1',
      cps: String(st.cps ?? 4.6),
    });
    // avatar 就是角色 id（模型内容指纹）。
    const url = '/preview?' + p.toString();
    const open = $('#stage-open');
    if (open) open.href = url;
    if (S.tab !== 'role') {
      // 不在「角色设定」页时不加载 iframe：Live2D 预览是独立的 30fps WebGL 页面。
      S.previewDirty = true;
      S.previewLoaded = false;
      return;
    }
    const frame = $('#stage-preview');
    if (frame && (force || frame.dataset.src !== url)) {
      // 表单里每敲一个字都重载整个 WebGL 预览，会不断新建/丢弃上下文；
      // 地址没变就只更新链接；只有「刷新预览」按钮才强制重载。
      frame.dataset.src = url;
      frame.src = url;
    }
    S.previewDirty = false;
    S.previewLoaded = true;
    /* 预览 iframe 常驻 Inspector；不需要父页面按钮。 */
  }

  // ── 启动 / 初始化 ───────────────────────────────────
  async function bootstrap(silent) {
    const data = await api('/api/bootstrap');
    S.schema = data.schema;
    S.config = data.config;
    S.runtime = data.status;
    S.memory = data.memory;
    S.csrf = data.csrf;
    S.configError = data.config_error || '';
    if (!silent) S.configNotice = data.config_notice || '';
    S.secretsError = data.secrets_error || '';
    S.roleSeq += 1;
    S.selectedAvatar = data.schema.selected_avatar || '';
    S.role = data.schema.role || null;
    renderAll();
    renderActionToggles();
    renderUserPicker();
    renderUserForm();
    renderStatus();
    renderCards();
    if (!silent) {
      const note = (e) => (e && e.message) ? e.message : String(e);
      await Promise.allSettled([
        refreshLog(), loadGames(),
        loadRolePersona(),
        loadCompanionMemory().catch((e) => {
          const box = $('#companion-table');
          if (box) box.innerHTML = `<div class="muted">${esc(note(e))}</div>`;
        }),
        loadGameMemory().catch((e) => {
          const box = $('#game-memory-table');
          if (box) box.innerHTML = `<div class="muted">${esc(note(e))}</div>`;
        }),
        loadEmbedModels().catch(() => {}),
      ]);
    }
  }

  function renderAll() {
    renderModel();
    renderGroups(['toggles'], $('#toggles-form'));
    saveBar($('#toggles-form'), groupPaths(['toggles']), '启用控制');
    renderPresets();
    renderGroups(['memory'], $('#memory-config'));
    saveBar($('#memory-config'), groupPaths(['memory']), '记忆设置');
    renderTts();
    renderTtsSettings();
    renderStt();
    renderGroups(['pace'], $('#pace-forms'));
    saveBar($('#pace-forms'), groupPaths(['pace']), '画面与节奏');
    renderGroups(['live'], $('#live-forms'));
    saveBar($('#live-forms'), groupPaths(['live']), '直播设置');
    renderStage();
    for (const id of ['role-persona-text', 'user-persona', 'game-text']) {
      attachLengthHint($('#' + id));
    }
  }

  function renderStage() {
    renderRolePicker();
    renderRoleProfile();
    renderRoleStage();
    renderStageGlobal();
    for (const el of $$('#stage-global-form .cfg-input')) {
      el.addEventListener('change', schedulePreview);
    }
    for (const el of $$('#role-stage-form .cfg-input')) {
      el.addEventListener('input', schedulePreview);
      el.addEventListener('change', schedulePreview);
    }
    refreshPreview();
    wireRules();
    wireAutonomy();
    loadAvatarRules(S.selectedAvatar);
    loadAutonomy(S.selectedAvatar);
  }

  // 进入页面时把"下拉/表单"摆回已生效的状态：浏览过但没保存的选择不保留。
  async function enterTab(name) {
    if (name === 'role') {
      await resetRoleToEffective();
    } else if (name === 'model') {
      renderModel();
    } else if (name === 'user') {
      S.userId = (S.schema.users || {}).current || S.userId;
      renderUserPicker();
      renderUserForm();
      clearDirty('user');
    } else if (name === 'game') {
      await loadGames();
      clearDirty('game');
    } else if (name === 'tts') {
      S.ttsProfile = ttsEffectiveSource();
      renderTts();
    } else if (name === 'prompt') {
      // 离开再回来要回到已生效的预设，不保留上次浏览未保存的 loaded。
      const ps = (S.schema && S.schema.presets) || {};
      if (ps.current) ps.loaded = ps.current;
      renderPresets();
    }
  }

  async function switchTab(name, setHash = true) {
    if (anyDirty()) {
      if (!confirm('当前页面还有未保存的修改，切换会丢失。确定切换？')) {
        if (setHash) history.replaceState(null, '', '#' + S.tab);
        return;
      }
      clearDirty();
      await bootstrap(false);         // 丢掉未保存的修改，回到已保存的状态
    }
    S.tab = name;
    $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    $$('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + name));
    const [title, desc] = TITLES[name] || [name, ''];
    $('#page-title').textContent = title;
    $('#page-desc').textContent = desc;
    if (name === 'role') {
      if (S.previewDirty || !S.previewLoaded) refreshPreview();
    } else if (S.previewLoaded) {
      const frame = $('#stage-preview');
      if (frame) {
        frame.src = 'about:blank';
        frame.dataset.src = '';
      }
      S.previewLoaded = false;
    }
    if (setHash && location.hash !== '#' + name) {
      history.replaceState(null, '', '#' + name);
    }
    if (name === 'overview') applyLog(S.logLines);
    await enterTab(name);
  }

  function attachLengthHint(el) {
    if (!el || el.dataset.hintAttached) return;
    if (el.maxLength && el.maxLength > 0 && el.maxLength < 5000) return;
    const limit = Number((S.schema && S.schema.limits
      && S.schema.limits.text_warn_chars) || 6000);
    const hint = document.createElement('div');
    hint.className = 'help';
    const update = () => {
      const n = String(el.value || '').length;
      if (n > limit) {
        hint.textContent = `已 ${n} 字，超过 ${limit} 字只提示、不截断；注意上下文占用。`;
        hint.style.color = '#e0b34a';
      } else {
        hint.textContent = `${n} 字`;
        hint.style.color = '';
      }
    };
    el.addEventListener('input', update);
    el.insertAdjacentElement('afterend', hint);
    el.dataset.hintAttached = '1';
    el._hintUpdate = update;
    update();
  }

  function attachLengthHints(root) {
    if (!root) return;
    root.querySelectorAll('textarea[data-warn], textarea#m-content, textarea#e-content')
      .forEach(attachLengthHint);
  }

  function refreshLengthHint(id) {
    const el = $('#' + id);
    if (el && el._hintUpdate) el._hintUpdate();
  }

  function openModal(title, html, onOk) {
    $('#modal-title').textContent = title;
    $('#modal-body').innerHTML = html;
    attachLengthHints($('#modal-body'));
    $('#modal').classList.remove('hidden');
    $('#modal-ok').onclick = async () => {
      try { await onOk(); $('#modal').classList.add('hidden'); }
      catch (e) { toast(e.message, true); }
    };
  }

  function wire() {
    $$('#nav button').forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
    $('#btn-start').onclick = async () => {
      try { const r = await api('/api/runtime/start', {
        method: 'POST', body: {} });
        if (!r.ok) {
          toast(r.error || '启动失败', true);
          if (r.page) switchTab(r.page);
          return;
        }
        toast('已启动');
        S.sttDown = false;                      // 重新起来就重新判定
        await refreshStatus(); await refreshLog();
      } catch (e) { toast(e.message, true); }
    };
    $('#btn-stop').onclick = async () => {
      try { await api('/api/runtime/stop', { method: 'POST', body: {} });
        S.sttDown = false;
        toast('已停止'); await refreshStatus(); }
      catch (e) { toast(e.message, true); }
    };
    $('#btn-restart').onclick = async () => {
      try { const r = await api('/api/runtime/restart', {
        method: 'POST', body: {} });
        if (!r.ok) {
          toast(r.error || '重启失败', true);
          if (r.page) switchTab(r.page);
          return;
        }
        toast('已重启');
        S.sttDown = false;                      // 重启后重新判定
        await refreshStatus(); await refreshLog();
      } catch (e) { toast(e.message, true); }
    };
    $('#btn-refresh-log').onclick = refreshLog;
    $('#game-text').addEventListener('input', () => { markDirty('game'); });

    // 角色：设定 / 人设文本 / 形象设置
    $('#role-persona-text').addEventListener('input', () => { markDirty('persona'); });
    $('#role-list').onchange = () => browseRole($('#role-list').value);
    $('#btn-role-select').onclick = saveRoleSelection;
    $('#btn-role-rescan').onclick = rescanRoles;
    $('#btn-role-profile-save').onclick = saveRoleProfile;
    $('#btn-role-persona-save').onclick = saveRolePersona;
    $('#btn-role-stage-save').onclick = saveRoleStage;
    $('#btn-role-export').onclick = exportRolePack;
    $('#btn-role-import').onclick = () => pickPack($('#role-import'));
    $('#role-import').onchange = importRolePack;

    // 用户
    $('#user-list').onchange = () => {
      const sel = $('#user-list');
      const next = sel.value;
      if (next !== S.userId && DIRTY.user
          && !confirm('用户设定还有未保存的修改，切换会丢失。继续？')) {
        sel.value = S.userId;
        return;
      }
      S.userId = next;
      clearDirty('user');
      renderUserPicker();
      renderUserForm();
    };
    $('#btn-user-create').onclick = createUser;
    $('#btn-user-select').onclick = saveUserAll;
    $('#btn-user-delete').onclick = deleteUser;

    $('#game-list').onchange = async () => {
      const sel = $('#game-list');
      const next = sel.value;
      if (next === S.gameId) return;
      if (DIRTY.game && !confirm('游戏正文还有未保存的修改，切换会丢失。仍要切换？')) {
        sel.value = S.gameId;
        return;
      }
      clearDirty('game');
      await loadGameText(next);
    };
    $('#btn-entry-add').onclick = () => editEntry(null);
    $('#btn-game-save').onclick = saveGameText;
    $('#btn-game-create').onclick = createGame;
    $('#btn-game-delete').onclick = deleteGame;
    $('#btn-game-export').onclick = exportGamePack;
    $('#btn-game-import').onclick = () => pickPack($('#game-import'));
    $('#game-import').onchange = importGamePack;

    // 对话预设
    $('#btn-preset-create').onclick = createPreset;
    $('#btn-preset-save').onclick = savePreset;
    $('#btn-preset-delete').onclick = deletePreset;
    $('#btn-preset-add').onclick = () => addPresetEntry();
    $('#preset-list').onchange = () => browsePreset($('#preset-list').value);
    $('#btn-preset-export').onclick = () => downloadPack(
      'preset', presetState().loaded, '正在打包预设');
    $('#btn-preset-import').onclick = () => pickPack($('#preset-import'));
    $('#preset-import').onchange = importPresetPack;
    $('#btn-prompt-preview').onclick = showPromptPreview;
    $('#btn-prompt-preview-close').onclick = () => {
      $('#prompt-preview-panel').hidden = true;
    };
    $('#btn-game-select').onclick = saveGameSelection;
    $('#btn-companion-add').onclick = () => editMemory(null, 'live', 'summary');
    $('#btn-game-mem-add').onclick = () => editMemory(
      null, 'game', S.gameMemKind || 'plot');
    $$('#game-mem-tabs [data-game-mem]').forEach((b) => {
      b.onclick = () => {
        S.gameMemKind = b.dataset.gameMem;
        $$('#game-mem-tabs [data-game-mem]').forEach((x) => {
          x.classList.toggle('primary', x === b);
        });
        loadGameMemory().catch((e) => toast(e.message, true));
      };
    });
    $('#btn-tts-preview').onclick = previewTts;
    $('#btn-tts-new').onclick = newTts;
    $('#tts-profile-name').onkeydown = (e) => {
      if (e.key === 'Enter') newTts();
    };
    $('#btn-tts-save').onclick = saveTts;
    $('#btn-tts-delete').onclick = deleteTts;
    $('#btn-preview-refresh').onclick = () => refreshPreview(true);
    $('#btn-profile-save').onclick = saveModelProfile;
    $('#btn-profile-test').onclick = testSelectedProfile;
    $('#btn-profile-delete').onclick = deleteSelectedProfile;
    let controlTimer = 0;
    let controlPending = null;
    let controlBusy = false;
    const flushStageControl = async () => {
      controlTimer = 0;
      if (controlBusy || !controlPending) return;
      const body = controlPending;
      controlPending = null;
      controlBusy = true;
      try {
        await api('/api/stage/control', { method: 'POST', body });
      } catch (e) {
        // stage 没运行时丢弃这一帧；下一帧参数变化会再发。
      } finally {
        controlBusy = false;
        // 拖动滑块时只保留最新一份，绝不让请求堆满浏览器的同源连接池
        // （否则切角色/读档案会排在几十个 2 秒超时后面）。
        if (controlPending && !controlTimer) {
          controlTimer = setTimeout(flushStageControl, 50);
        }
      }
    };
    window.addEventListener('message', (e) => {
      if (e.origin !== location.origin || !e.data
          || e.data.type !== 'deepsidekick-control'
          || !e.data.ctl || typeof e.data.ctl !== 'object') return;
      controlPending = e.data.ctl;
      if (!controlTimer) controlTimer = setTimeout(flushStageControl, 50);
    });
    $('#btn-add-profile').onclick = () => {
      if (DIRTY.config
          && !confirm('模型配置还有未保存的修改，新增 profile 会丢失。继续？')) return;
      const name = ($('#profile-name').value || '').trim();
      if (!/^[A-Za-z0-9_-]{1,40}$/.test(name)) return toast('profile 名只能字母/数字/下划线/短横线', true);
      if ((S.schema.llm_profiles || []).some((p) => p.name === name)) return toast('已存在', true);
      const presetId = Object.keys(S.schema.llm_presets || {})
        .find((k) => k.toLowerCase() === name.toLowerCase());
      const preset = presetId ? S.schema.llm_presets[presetId] : null;
      const prefix = `llm.${name}.`;
      const fields = (S.schema.llm_profile_fields || []).map((f) => {
        const out = {
          path: prefix + f.key, label: f.label, type: f.type,
          value: f.value, secret: f.type === 'password',
          configured: false, help: f.help || '', options: [],
        };
        if (f.min !== undefined) out.min = f.min;
        if (f.step !== undefined) out.step = f.step;
        return out;
      });
      const block = {};
      for (const f of fields) block[f.path.slice(prefix.length)] = f.value;
      if (preset) {
        // 内置厂商只带出连接信息；temperature / reasoning_effort 仍用模板默认值
        block.base_url = preset.base_url || '';
        block.model = preset.model || '';
        block.headers = { ...(preset.headers || {}) };
      }
      block.body = {};
      S.config.llm[name] = block;
      for (const f of fields) {
        const key = f.path.slice(prefix.length);
        if (key === 'base_url') f.value = block.base_url;
        if (key === 'model') f.value = block.model;
        if (key === 'headers') f.value = { ...block.headers };
      }
      S.schema.llm_profiles.push({ name, active: false, fields });
      const sel = $('#model-source');
      if (sel) {
        const o = document.createElement('option');
        o.value = name; o.textContent = name;
        sel.appendChild(o);
        sel.value = name;              // 新建后直接停在这份，方便填完就保存
      }
      markDirty('config');
      renderProfileFields();
      toast(`已新建 ${name}；填好 base_url/model/key 后点「保存」`);
    };
    $('#modal-close').onclick = $('#modal-cancel').onclick = () => $('#modal').classList.add('hidden');
    $('#modal').addEventListener('click', (e) => { if (e.target === $('#modal')) $('#modal').classList.add('hidden'); });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stopEvents(); else startEvents();
    });
    window.addEventListener('beforeunload', (e) => {
      if (anyDirty()) {
        e.preventDefault(); e.returnValue = '';
      }
    });
    window.addEventListener('hashchange', () => {
      const n = (location.hash || '').replace('#', '');
      if (TITLES[n] && n !== S.tab) switchTab(n, false);
    });
  }

  (async function main() {
    wire();
    try {
      await bootstrap(false);
      const initial = (location.hash || '').replace('#', '');
      if (TITLES[initial]) switchTab(initial, false);
      if (!document.hidden) startEvents();
    } catch (e) {
      toast('页面初始化失败：' + e.message, true);
      console.error(e);
    }
  })();
})();
