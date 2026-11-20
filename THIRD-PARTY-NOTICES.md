# 第三方组件与许可说明

本文件说明 DeepSidekick 仓库中**不属于本项目原创代码**的部分及其许可。它不改变、也不替代各第三方许可证；发生冲突时，以各上游许可证原文为准。

本项目原创代码和文档使用 **MIT**（见 [LICENSE](LICENSE)）。以下组件**不是 MIT**，或虽然 MIT 但需要保留独立版权声明。

> 本文件中标为 MIT 的第三方组件，其 MIT 许可正文与本项目 [LICENSE](LICENSE) 相同；再分发时必须连同对应的版权行一起保留。

## 1. 随仓库再分发的运行时

### PixiJS 8.22.0

- 文件：`overlay/lib/pixi.min.js`
- 许可：**MIT**
- 版权：Copyright (c) 2013-2023 Mathew Groves, Chad Engler
- 上游：<https://github.com/pixijs/pixijs>

### pixi-live2d-display 1.4.0

- 文件：`overlay/lib/live2d-engine.min.js`
- 许可：**MIT**
- 版权：Copyright (c) 2020 Guan
- 上游：<https://github.com/guansss/pixi-live2d-display>

### Live2D Cubism Core

- 文件：`overlay/lib/live2dcubismcore.min.js`
- 许可：**Live2D Proprietary Software License Agreement**（不是 MIT）
- 版权：Copyright (c) 2019 Live2D Inc. All rights reserved.
- 文件头注明：该文件属于协议中的“Redistributable Code”，许可全文：
  <https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html>
- 说明：仓库**不附带 Live2D 模型**；使用 Live2D 模型、Live2D Cubism Core、`live2d-py` 时，需要遵守 Live2D 的专有许可和模型自身的许可。`live2d-py` 的 Python 包本身是 MIT，但它封装的 Cubism Core 仍是 Live2D 专有组件（见下文“Python 依赖”）。

### Spine Runtimes / Spine Web Player

- 文件：
  - `vendor/spine/spine38.dll`、`spine40.dll`、`spine41.dll`、`spine42.dll`
  - `overlay/lib/spine-player-3.8.js`、`spine-player-4.0.js`、`spine-player-4.1.js`、`spine-player-4.2.js`、`spine-player.css`
- 许可：**Spine Runtimes License**（不是 MIT）
- 版权：Copyright (c) 2013-2025, Esoteric Software LLC
- 许可全文（随版本各一份，**不得删除**；全文已随 DLL 同目录保存，不在本文件重复）：
  - `vendor/spine/SPINE-RUNTIMES-LICENSE-3.8.txt`
  - `vendor/spine/SPINE-RUNTIMES-LICENSE-4.0.txt`
  - `vendor/spine/SPINE-RUNTIMES-LICENSE-4.1.txt`
  - `vendor/spine/SPINE-RUNTIMES-LICENSE-4.2.txt`
- 说明：
  - 按该许可，再分发时必须附带许可证和版权声明；删除许可文件后不得再分发这些 DLL/播放器。
  - 使用 Spine 形象的用户需要自行持有 **Spine Editor** 许可：<https://esotericsoftware.com/spine-purchase>
  - 仓库不附带 Spine 模型；`tools/spine/` 是本项目的桥接/构建代码，构建时需要自行准备 `spine-c` 源码（<https://github.com/EsotericSoftware/spine-runtimes>）。桥接代码本身按本项目 MIT，编译产物链接 Spine Runtimes，仍受 Spine Runtimes License 约束。

### blivedm

- 目录：`vendor/blivedm/`
- 许可：**MIT**
- 版权：Copyright (c) 2018 xfgryujk
- 来源：<https://github.com/xfgryujk/blivedm>，vendor 的提交见 `vendor/blivedm/UPSTREAM.txt`（`3bf17fe862b8c2fc6bc04c81c3f0c5db95de93d6`）。

### 默认兜底图

- 文件：`models/images/deepseek.svg`
- 说明：DeepSeek 标识/默认兜底形象，**不是** Live2D/Spine 模型，也不在本项目 MIT 许可范围内；相关名称和标识归 DeepSeek 所有。该文件仅在没有模型时作为图片显示，用户可替换为自己的图片或模型。
- 本项目与 DeepSeek 官方无隶属关系。

## 2. Python 依赖

这些包由 `requirements.txt` 安装，**不随本仓库提交**；各包在安装目录中自带自己的许可证。版本以实际安装为准（下表按 2026-11-20 本机环境核对）。

| 包 | 许可证 | 备注 |
|---|---|---|
| aiohttp | Apache-2.0 | WebUI/HTTP/弹幕 |
| Brotli | MIT | HTTP 压缩 |
| pure-protobuf | MIT | blivedm 协议 |
| dxcam | MIT | 屏幕抓取 |
| Pillow | MIT-CMU | 图片处理 |
| numpy | BSD-3-Clause | 数值计算 |
| soxr | LGPL-2.1-or-later | 音频重采样 |
| edge-tts | LGPL-3.0 | 免费 TTS 兜底 |
| miniaudio | MIT | MP3 解码 |
| sounddevice | MIT | 音频设备 |
| sherpa-onnx | Apache-2.0 | 本地语音识别运行时 |
| certifi | MPL-2.0 | CA 证书 |
| fastembed | Apache-2.0 | 本地向量嵌入 |
| onnxruntime | MIT | ONNX 推理（fastembed 依赖） |
| tokenizers | Apache-2.0 | 分词（fastembed 依赖） |
| PyYAML | MIT | 配置读写 |
| ruamel.yaml | MIT | 保留注释写 YAML |
| psutil | BSD-3-Clause | 进程管理 |
| live2d-py | MIT（包本身） | 封装 Live2D Cubism Core；Cubism Core 为 Live2D 专有许可 |
| pygame | LGPL-2.1 | 桌面窗/绘制 |
| PyOpenGL | BSD | OpenGL 绑定 |
| pywin32 | PSF | Windows API |
| Python 3.12 | PSF License | 解释器 |

> `pygame`、`soxr`、`edge-tts` 是 LGPL 组件。它们以独立 pip 包形式使用，不改变本项目的 MIT 源码许可；如将 Python 环境/依赖一并打包进“一键发行版”，需按 LGPL 履行再分发义务（保留许可证、提供对应源码/替换方式等）。

## 3. 模型与数据文件

**本仓库不包含任何 Live2D / Spine 模型。** `models/` 里只有 `README.md` 和默认兜底图 `models/images/deepseek.svg`。

用户自行放入的 Live2D/Spine/图片模型，其版权和许可由用户负责；尤其需注意标注“非商业”“禁止分发”“仅限本机”的模型。仓库不授予这些模型的任何使用权。

以下模型/数据也**不随仓库提交**，由用户按需下载，各自适用自己的模型卡/许可：

- SenseVoice / FunASR（语音识别）：以模型卡和 FunASR 仓库的许可为准。
- Qwen3-Embedding-0.6B（语义召回）：Apache-2.0（以模型卡为准）。
- BGE-M3（可选语义召回）：MIT（以模型卡为准）。
- jina-embeddings-v3 等可选项：以模型卡为准（部分模型为 CC-BY-NC，不可商用）。

`persona/` 下的角色设定、用户设定、游戏世界书、对话预设是用户个人内容，不随仓库提交，也不在本项目 MIT 许可范围内。

## 4. 再分发注意事项

1. 再分发本仓库时，保留：
   - [LICENSE](LICENSE)
   - 本文件
   - `vendor/spine/SPINE-RUNTIMES-LICENSE-*.txt`
   - `overlay/lib/live2dcubismcore.min.js` 文件头及 Live2D EULA 通知
   - `overlay/lib/README.md`、`vendor/spine/README.md` 等来源说明
2. 不得将 `models/` 中的用户模型、`persona/` 用户素材、`state/` 运行态或任何密钥纳入公开包。
3. 如将 Python 依赖一并打包，除上述许可外，还需满足 LGPL 组件的再分发义务，并保留所有第三方包的许可证。
4. Live2D、Spine、所用模型、声音克隆、LLM/TTS 服务的条款独立于本项目；商用、直播打赏、二次分发前应逐项确认。

## 5. 免责声明

以上信息用于说明仓库当前包含的第三方组件，不构成法律意见。第三方许可证可能更新；使用、修改或再分发前应以各上游许可证原文为准。
