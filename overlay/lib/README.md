# overlay/lib —— 浏览器侧第三方运行时

这些文件由页面按需加载（`<script>` / `<link>` 动态插入），不是本项目源码，原样再分发。

| 文件 | 来源 | 用途 |
|---|---|---|
| `pixi.min.js` | pixi.js | Live2D 的 WebGL 渲染宿主 |
| `live2dcubismcore.min.js` | Live2D Cubism Core | Cubism 模型内核 |
| `live2d-engine.min.js` | pixi-live2d-display | Live2D 与 PIXI 的桥 |
| `spine-player-3.8.js` | 官方 Spine Web Player 3.8 | Spine 3.8 骨架播放 |
| `spine-player-4.0.js` | 官方 Spine Web Player 4.0 | Spine 4.0 |
| `spine-player-4.1.js` | 官方 Spine Web Player 4.1 | Spine 4.1 |
| `spine-player-4.2.js` | 官方 Spine Web Player 4.2 | Spine 4.2 |
| `spine-player.css` | 官方 Spine Web Player 4.2 的样式 | 播放器自带控件/加载层的样式 |

Spine 用的是**官方播放器**（`esotericsoftware.com/files/spine-player/<版本>/spine-player.js`），
不是自己拼 spine-webgl：4.0 的模型在自研渲染下会在半透明边缘出现一圈细线，
官方播放器没有这个问题，而且四个版本的接口一致（`skelUrl/jsonUrl/atlasUrl`、
`setAnimation/setSkin/play/pause`），前端只有一套适配代码。
版本按骨架文件头嗅探后只加载命中的那一份。

Spine Runtimes（含这个播放器）的许可全文在 `vendor/spine/SPINE-RUNTIMES-LICENSE-<版本>.txt`，
再分发义务见 `THIRD-PARTY-NOTICES.md` 和 `vendor/spine/README.md`。
