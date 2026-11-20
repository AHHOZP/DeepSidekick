/* DeepSidekick 浏览器侧 Spine 播放器：官方 spine-player 的薄适配层。
 *
 * 之前我们自己用 spine-webgl 拼加载/取景/绘制，4.0 的 Nikke 模型会在眼睛、鼻梁这类
 * 分层附件边界露出一圈细线（透明画布合成时才能看到，画布不透明则消失）；同一份模型、
 * 同一个运行时，官方 spine-player 渲染是干净的。所以这里不再自己拼渲染，
 * 改成按骨架版本加载官方播放器，只把我们舞台需要的几件事接出来：
 *   · 版本 → 运行时：3.8 / 4.0 / 4.1 / 4.2 各一份官方构建，接口一致，所以只有这一套代码；
 *   · 取景：用播放器自己的 viewport（左右 2%、上 10%、下 0%，让形象靠近底部）；
 *   · 整体大小/位置：对容器做 CSS 变换（绕中心缩放 + 平移），不改播放器内部的 fit；
 *   · 气泡锚点：读画布 alpha 包围盒（和 Live2D 同一套 S.content 语义）。
 */
(function () {
  'use strict';

  var PLAYER = {
    '3.8': 'spine-player-3.8.js',
    '4.0': 'spine-player-4.0.js',
    '4.1': 'spine-player-4.1.js',
    '4.2': 'spine-player-4.2.js',
  };
  var FALLBACK_VERSION = '4.2';
  var IDLE_EXACT = ['idle', 'wait', 'stand', '待机'];

  var P = {
    player: null,
    host: null,
    canvas: null,
    version: '',
    ready: false,
    error: '',
    animations: [],
    skins: [],
    playing: '',
    content: null,
    view: { zoom: 1, offX: 0, offY: 0 },
    timer: 0,
    talkName: '',
    talk: { target: 0, level: 0, velocity: 0, silence: 1, active: false, last: 0 },
    onError: null,
  };

  function fail(msg) {
    P.error = String(msg || '');
    if (P.onError) P.onError(P.error);
    console.warn('[spine]', P.error);
  }

  function scriptOnce(src) {
    return new Promise(function (resolve, reject) {
      if (document.querySelector('script[data-spine="' + src + '"]')) {
        resolve();
        return;
      }
      var s = document.createElement('script');
      s.src = src;
      s.dataset.spine = src;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error('播放器加载失败：' + src)); };
      document.head.appendChild(s);
    });
  }

  // ── 版本嗅探（决定加载哪份官方播放器）───────────────
  function versionOfJson(text) {
    var m = /"spine"\s*:\s*"(\d+)\.(\d+)/i.exec(String(text || '').slice(0, 4096));
    return m ? m[1] + '.' + m[2] : FALLBACK_VERSION;
  }

  function versionOfBinary(buf) {
    // 头部是 8 字节 hash，紧接着是 ASCII 版本串；hash 里也会凑出“8.3”这种假版本，
    // 所以只认 3.x / 4.x。
    var bytes = new Uint8Array(buf, 0, Math.min(48, buf.byteLength));
    var out = '';
    for (var i = 8; i < bytes.length; i += 1) {
      var c = bytes[i];
      out += (c >= 32 && c < 127) ? String.fromCharCode(c) : '.';
    }
    var m = /([34])\.([0-9])\.[0-9]+/.exec(out);
    return m ? m[1] + '.' + m[2] : FALLBACK_VERSION;
  }

  function idleOf(names, wanted) {
    var want = String(wanted || '').trim();
    if (want && names.indexOf(want) >= 0) return want;
    for (var i = 0; i < names.length; i += 1) {
      if (IDLE_EXACT.indexOf(String(names[i]).toLowerCase()) >= 0) return names[i];
    }
    for (var j = 0; j < names.length; j += 1) {
      if (String(names[j]).toLowerCase().indexOf('idle') >= 0) return names[j];
    }
    return names.length ? names[0] : '';
  }

  function talkOf(names, idle) {
    var low = {};
    for (var i = 0; i < names.length; i += 1) low[String(names[i]).toLowerCase()] = names[i];
    var fixed = ['talk', 'speak', 'idle_talk'];
    for (var j = 0; j < fixed.length; j += 1) {
      if (low[fixed[j]]) return low[fixed[j]];
    }
    var suffixed = idle ? String(idle + '_talk').toLowerCase() : '';
    if (suffixed && low[suffixed]) return low[suffixed];
    for (var k = 0; k < names.length; k += 1) {
      if (String(names[k]).toLowerCase().slice(-5) === '_talk') return names[k];
    }
    return '';
  }

  // ── 视图变换（CSS，绕中心缩放 + 平移）───────────────
  function applyTransform() {
    if (!P.host) return;
    P.host.style.transformOrigin = '50% 50%';
    P.host.style.transform = 'translate(' + (P.view.offX * 100) + '%,'
      + (P.view.offY * 100) + '%) scale(' + P.view.zoom + ')';
  }

  /** 画布上真有像素的包围盒（归一化），用来给气泡贴头顶。 */
  function alphaBox() {
    var img = P.canvas;
    if (!img || !img.width || !img.height) return null;
    try {
      var n = 96;
      var c = document.createElement('canvas');
      c.width = c.height = n;
      var ctx = c.getContext('2d', { willReadFrequently: true });
      ctx.clearRect(0, 0, n, n);
      ctx.drawImage(img, 0, 0, n, n);
      var d = ctx.getImageData(0, 0, n, n).data;
      var x0 = n;
      var y0 = n;
      var x1 = -1;
      var y1 = -1;
      for (var y = 0; y < n; y += 1) {
        for (var x = 0; x < n; x += 1) {
          if (d[(y * n + x) * 4 + 3] <= 16) continue;
          if (x < x0) x0 = x;
          if (y < y0) y0 = y;
          if (x > x1) x1 = x;
          if (y > y1) y1 = y;
        }
      }
      if (x1 < 0) return null;
      return { x0: x0 / n, y0: y0 / n, x1: (x1 + 1) / n, y1: (y1 + 1) / n };
    } catch (e) {
      return null;
    }
  }

  function refreshContent() {
    var b = alphaBox();
    if (!b) return;
    var z = P.view.zoom;
    var mapX = function (v) { return 0.5 + (v - 0.5) * z + P.view.offX; };
    var mapY = function (v) { return 0.5 + (v - 0.5) * z + P.view.offY; };
    P.content = {
      x0: mapX(b.x0), x1: mapX(b.x1),
      y0: mapY(b.y0), y1: mapY(b.y1),
    };
  }

  async function init(cfg, opts) {
    opts = opts || {};
    P.onError = opts.onError || null;
    P.error = '';
    P.view = { zoom: 1, offX: 0, offY: 0 };

    var modelUrl = new URL(cfg.model, location.href).href;
    var res = await fetch(modelUrl, { cache: 'no-cache' });
    if (!res.ok) throw new Error('骨架读取失败：HTTP ' + res.status);
    var isJson = /\.json(\?.*)?$/i.test(modelUrl);
    var raw = isJson ? await res.text() : await res.arrayBuffer();
    P.version = isJson ? versionOfJson(raw) : versionOfBinary(raw);

    var base = String(opts.base || '/s').replace(/\/+$/, '');
    var file = PLAYER[P.version];
    await scriptOnce(base + '/lib/' + (file || PLAYER[FALLBACK_VERSION]));
    if (!window.spine || !window.spine.SpinePlayer) {
      throw new Error('官方播放器没挂上 window.spine.SpinePlayer');
    }
    if (!file) fail('骨架版本 ' + P.version + ' 没有对应播放器，试 ' + FALLBACK_VERSION);

    var old = document.getElementById('canvas');
    if (!old || !old.parentNode) throw new Error('找不到 #canvas 的容器');
    var host = document.createElement('div');
    host.id = 'spine-stage';
    host.style.cssText = 'position:absolute;inset:0;transform-origin:50% 50%';
    old.parentNode.insertBefore(host, old);
    old.style.display = 'none';
    P.host = host;

    var atlasUrl = modelUrl.replace(/\.(json|skel)(\?[^#]*)?$/i, '.atlas$2');
    // 预乘由服务端（/config.json 的 pma）给结论：浏览器 canvas 会把预乘图解预乘再
    // 交回来，自己按像素判永远得到“预乘”，棕色尘埃 2 那两个模型就是这么判错的。
    var pma = (typeof cfg.pma === 'boolean') ? cfg.pma : true;
    // 官方播放器直接把页图名拼进 Image.src；文件名里带 # 时浏览器会把后面当 fragment
    // 丢掉，请求就 404（例如 build_char_xxx#27.png）。这里在创建播放器期间拦截一次
    // Image 的 src 赋值，把 # 编码成 %23，创建完就还原。
    var origImage = window.Image;
    window.Image = function () {
      var img = new origImage();
      try {
        var proto = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
        Object.defineProperty(img, 'src', {
          configurable: true,
          get: function () { return proto.get.call(img); },
          set: function (value) { proto.set.call(img, String(value).replace(/#/g, '%23')); },
        });
      } catch (e) { /* 不支持就按原样 */ }
      return img;
    };
    try {
      await new Promise(function (resolve, reject) {
      P.player = new window.spine.SpinePlayer(host, {
        jsonUrl: isJson ? modelUrl : undefined,
        skelUrl: isJson ? undefined : modelUrl,
        atlasUrl: atlasUrl,
        alpha: true,
        backgroundColor: '#00000000',
        premultipliedAlpha: pma,
        showControls: false,
        showLoading: false,
        preserveDrawingBuffer: true,
        webglConfig: {
          alpha: true, antialias: true, premultipliedAlpha: true,
          preserveDrawingBuffer: true,
        },
        viewport: { padLeft: '2%', padRight: '2%', padTop: '10%', padBottom: '0%' },
        success: function (player) { resolve(player); },
        error: function (_player, msg) { reject(new Error(msg || '播放器初始化失败')); },
      });
    });
    } finally {
      window.Image = origImage;
    }

    P.pma = pma;
    P.canvas = host.querySelector('canvas');
    if (!P.canvas) throw new Error('播放器没创建画布');
    var data = P.player.skeleton && P.player.skeleton.data;
    P.animations = data ? (data.animations || []).map(function (a) { return a.name; }) : [];
    P.skins = data ? (data.skins || []).map(function (s) { return s.name; }) : [];

    var idle = idleOf(P.animations, cfg.capabilities && cfg.capabilities.idle);
    P.idleName = idle || '';
    P.talkName = talkOf(P.animations, idle);
    if (idle) {
      try {
        P.player.setAnimation(idle);
        P.player.play();
        P.playing = idle;
      } catch (e) {
        fail('待机动画播不了：' + (e && e.message));
      }
    }

    applyTransform();
    refreshContent();
    clearInterval(P.timer);
    P.timer = setInterval(refreshContent, 500);
    P.ready = true;
    return P;
  }

  function play(name, loop) {
    if (!P.player) return false;
    try {
      P.player.setAnimation(String(name), !!loop);
      P.player.play();
      P.playing = String(name);
      clearTimeout(P.returnTimer);
      if (!loop && P.idleName && P.playing !== P.idleName) {
        var duration = 0;
        try {
          var anim = P.player.skeleton.data.findAnimation(P.playing);
          duration = anim && Number(anim.duration) || 0;
        } catch (e) { duration = 0; }
        if (duration > 0) {
          P.returnTimer = setTimeout(function () {
            if (P.playing === String(name)) play(P.idleName, true);
          }, Math.min(30000, duration * 1000 + 80));
        }
      }
      return true;
    } catch (e) {
      fail('动作 "' + name + '" 播不了：' + (e && e.message));
      return false;
    }
  }

  function setSkin(name) {
    var skel = P.player && P.player.skeleton;
    if (!skel || !skel.setSkinByName) return false;
    try {
      skel.setSkinByName(String(name));
      if (skel.setSlotsToSetupPose) skel.setSlotsToSetupPose();
      return true;
    } catch (e) {
      fail('皮肤 "' + name + '" 切不了：' + (e && e.message));
      return false;
    }
  }

  function setMouth(level) {
    var target = Math.max(0, Math.min(1, Number(level) || 0));
    var now = performance.now();
    var dt = P.talk.last ? Math.min(0.05, (now - P.talk.last) / 1000) : 1 / 60;
    P.talk.last = now;
    var omega = 2 * Math.PI * 3.2;
    var zeta = 0.45;
    P.talk.velocity += dt * (omega * omega * (target - P.talk.level)
      - 2 * zeta * omega * P.talk.velocity);
    P.talk.level += P.talk.velocity * dt;
    P.talk.target = target;
    if (target > 0.02) {
      P.talk.silence = 0;
      if (P.talkName && !P.talk.active && P.playing === P.idleName) {
        P.talk.active = play(P.talkName, true);
      }
    } else {
      P.talk.silence += dt;
      if (P.talk.active && P.talk.silence > 0.24) {
        P.talk.active = false;
        if (P.idleName) play(P.idleName, true);
      }
    }
    if (P.canvas) {
      var levelNow = Math.max(0, Math.min(1, P.talk.level));
      var sy = 1 + 0.022 * levelNow;
      var sx = 1 - 0.011 * levelNow;
      var skew = Math.max(-1, Math.min(1, P.talk.velocity * 0.35)) * 1.2;
      P.canvas.style.transformOrigin = '50% 100%';
      P.canvas.style.transform = 'scale(' + sx.toFixed(4) + ',' + sy.toFixed(4)
        + ') skewX(' + skew.toFixed(2) + 'deg)';
    }
  }

  window.SP = {
    init: init,
    get ready() { return P.ready; },
    get version() { return P.version; },
    get error() { return P.error; },
    get pma() { return P.pma; },
    get fitInfo() { return P.version + ' pma=' + (P.pma ? 1 : 0); },
    idle: function () { return P.playing || ''; },
    animations: function () { return P.animations.slice(); },
    skins: function () { return P.skins.slice(); },
    playing: function () { return P.playing; },
    content: function () { return P.content; },
    play: function (name, loop) { return play(name, loop); },
    dispose: function () {
      try { if (P.player && P.player.dispose) P.player.dispose(); } catch (e) {}
      P.player = null;
      P.ready = false;
      if (P.timer) { clearInterval(P.timer); P.timer = 0; }
    },
    mouth: setMouth,
    skin: setSkin,
    view: {
      get state() {
        return {
          zoom: +P.view.zoom.toFixed(2),
          normX: +P.view.offX.toFixed(3),
          normY: +P.view.offY.toFixed(3),
        };
      },
      setZoom: function (z) {
        var n = Number(z);
        P.view.zoom = Math.min(4, Math.max(0.2, Number.isFinite(n) ? n : 1));
        applyTransform();
        refreshContent();
      },
      setOffsetNorm: function (nx, ny) {
        P.view.offX = Number(nx) || 0;
        P.view.offY = Number(ny) || 0;
        applyTransform();
        refreshContent();
      },
      refit: function () {
        P.view = { zoom: 1, offX: 0, offY: 0 };
        applyTransform();
        refreshContent();
      },
    },
    control: {
      motion: function (group) { return play(group); },
      view: function (zoom, offX, offY) {
        var v = window.SP.view;
        var s = v.state;
        if (zoom !== null && zoom !== undefined) v.setZoom(zoom);
        if ((offX !== null && offX !== undefined)
            || (offY !== null && offY !== undefined)) {
          v.setOffsetNorm(offX === null || offX === undefined ? s.normX : offX,
            offY === null || offY === undefined ? s.normY : offY);
        }
      },
      reset: function () { window.SP.view.refit(); },
    },
  };
})();
