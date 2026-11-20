# DeepSidekick

**一个坐在屏幕旁边、陪玩家看画面打游戏的 AI 伴侣。**

DeepSidekick 是 Windows 上只接受语音输入的 AI 陪玩 / 桌宠：它听玩家说话、看玩家正在玩的游戏，用语音接话；形象可以是 Live2D、Spine 或图片。开启直播模式时，它还会接入 B 站弹幕、礼物和上舰；关闭直播后，仅作为单人陪玩。

> **当前状态**：Windows 10/11 x64 可运行；一键发行版尚未打包。
>
> **首次使用**：参见 [从源码运行](#从源码运行)；仅需修改代码的 agent 参见下一节。

## 供 AI agent 使用的入口

AI agent 在读取或修改代码前，应按以下顺序阅读：

1. 先读 [`AGENTS.md`](AGENTS.md)：仓库硬约束、代码规则、验收标准。
2. 再读 [`docs/DeepSidekick.md`](docs/DeepSidekick.md)：**当前实现**的规格文档，含完整运行逻辑、配置、模块边界。
3. 历史调研/计划不随仓库分发，以 `docs/DeepSidekick.md` 为准。
4. 修改完成后运行 `.venv\Scripts\python -m unittest discover -s tests` 和 `.venv\Scripts\python -m compileall -q sidekick tests scripts`。

README 仅负责使用入口与导航；实现细节以 `docs/DeepSidekick.md` 为准。

## 主要特点

- **画面感知**：按节奏抓取多帧、去重、拼图，并在最后附一张当前高清帧；前台窗口标题也会作为材料，隐私窗口则暂停抓取。
- **语音交互与打断**：本地 VAD/STT 将语音转成文字；用户开口时 TTS 会让路。回复可边生成边播，气泡和形象随语音更新。
- **长期记忆与游戏记忆**：本地 SQLite 保存陪玩记忆、剧情、NPC、任务；每轮包含常驻块与语义召回，长历史会自动总结并审核。
- **桌面形象**：支持live2d/spine，以及桌宠常见功能。
- **AI 驱动的 Live2D 动作配置**：基于模型自省结果生成动作、表情与自主驱动参数，减少手工调参。
- **直播为可选模式**：单人陪玩默认开启；仅打开直播模式时接入 B 站弹幕/礼物，关闭时 prompt 不含任何直播内容。


## 下载发行版（一键使用）

> **尚未打包。** 发布后将在此补充 Windows 发行版的下载地址、校验值，以及解压、双击、打开 WebUI 的步骤。

<!-- 发布清单（打包后填这里）：
- GitHub Releases 下载链接
- 支持的 Windows / 显卡 / 内存范围
- SHA256 校验值
- 解压后双击哪个文件
- 首次打开 WebUI 要填什么
-->

## 从源码运行

### 1. 准备

- Windows 10/11 x64；语音识别与向量模型在本地 CPU 运行，会额外占用约 2–3 GB 内存（视所选模型而定）。
- **Python 3.11 或 3.12 x64**（推荐 3.12；`live2d-py` 要求 3.11+）。
- 麦克风（语音输入必需）、扬声器/耳机。

### 2. 下载与安装依赖

将 `<仓库地址>` 替换为项目页提供的 Clone 地址：

```powershell
git clone <仓库地址> DeepSidekick
cd DeepSidekick

python -m venv .venv
.venv\Scripts\python -m pip install --no-cache-dir -r requirements.txt
```

### 3. 启动

```bat
.\run.bat
```

双击 `run.bat` 或在当前目录执行 `.\run.bat` 即可。`run.bat` 是唯一启动入口，仅负责启动 WebUI；模型、角色、直播模式与桌面模式均在 WebUI 中配置。

若从 GitHub 下载 ZIP 后首次双击 `run.bat` 弹出 SmartScreen 蓝色提示，点「更多信息」→「仍要运行」即可。

> `run.bat` 必须保持 CRLF 换行；如果手动复制或另存，请不要改成 LF。仓库的 `.gitattributes` 会在 Git 检出/ZIP 导出时处理。

### 4. 使用前配置

1. **LLM 模型 API**：填写 OpenAI 兼容服务的 Base URL、模型名与 API Key，保存。
2. **说话（TTS）**：可先使用免费的 Edge；Fish / MiniMax / MiMo 等需填写对应 Key。
3. **听话（STT）**：需在 WebUI 的「听话（STT）」页下载语音识别模型。
4. **记忆召回（嵌入模型）**：需在 WebUI 的「记忆与任务」页下载或选择嵌入模型。
5. **角色设定 / 对话预设 / 游戏资料**：选择或新建一套（见下一节）。
6. 点击顶部「启动」。

配置缺失时，WebUI 会以红色错误提示阻止启动，不会在不完整配置下运行。各字段语义与边界见 [`docs/DeepSidekick.md`](docs/DeepSidekick.md)。

### 5. 形象、预设与游戏资料

- `models/`：Live2D / Spine / 图片形象；模型文件夹放入后会自动扫描。
- `persona/`：角色设定、用户设定、游戏世界书、对话预设。没有预设/人设也可启动，但回复质量较差。
- `state/`：记忆库、密钥、语音识别/嵌入模型；由程序生成。

以上目录中的模型、用户/角色/游戏资料、记忆与密钥均为本机数据，不随仓库提交；`models/` 只保留 `README.md` 和默认兜底图，`persona/` 只保留默认占位。

> 仓库不附带任何 **Live2D / Spine 模型**。用户自行放入的模型适用其自身许可；直播、商用、二次分发前需自行确认，尤其需注意标注“非商业”“禁止分发”的模型。

## 问题反馈

本项目不提供一对一技术支持。使用中遇到问题时，应先查阅 [`docs/DeepSidekick.md`](docs/DeepSidekick.md) 与 [`AGENTS.md`](AGENTS.md)，或由 AI agent 结合仓库代码排查。

## 致谢

DeepSidekick 的设计和实现参考了多项公开研究、开源项目和厂商文档。以下列表为**设计参考**，不代表代码直接来自这些项目；具体许可与再分发条件见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。

### 研究与论文

- **IG-VLM**，*An Image Grid Can Be Worth a Video*（[arXiv:2403.18406](https://arxiv.org/abs/2403.18406)）：多帧按时间排入网格图，为多帧画面压缩提供依据。
- **Video Panels**，*Video Panels for Long Video Understanding*（[arXiv:2509.23724](https://arxiv.org/abs/2509.23724)）：2×2/3×3 面板在时间覆盖与空间分辨率之间的取舍。
- **SlowFast-LLaVA**（[arXiv:2407.15841](https://arxiv.org/abs/2407.15841)）与 **SlowFast-LLaVA-1.5**（[arXiv:2503.18943](https://arxiv.org/abs/2503.18943)）：慢通路保细节、快通路保动作的双流思想。
- **High-Resolution Image Captions**（[arXiv:2510.27164](https://arxiv.org/abs/2510.27164)）：高分辨率图过度下采样会抹掉小字和细节（“分辨率诅咒”）。
- **Proact-VL**，*A Proactive VideoLLM for Real-Time AI Companions*（[arXiv:2603.03447](https://arxiv.org/abs/2603.03447)）：实时陪伴中“何时开口”与短回复节奏的参考。
- **LiveCC**，*Learning Video LLM with Streaming Speech Transcription at Scale*（[arXiv:2504.16030](https://arxiv.org/abs/2504.16030)）：流式解说与短平快反馈。
- **StreamingVLM**（[arXiv:2510.09608](https://arxiv.org/abs/2510.09608)）：流式视觉语言模型的实时响应。
- **Accelerating Streaming Video LLMs via Hierarchical Token Compression**（[CVPR 2026](https://openaccess.thecvf.com/content/CVPR2026/papers/Wang_Accelerating_Streaming_Video_Large_Language_Models_via_Hierarchical_Token_Compression_CVPR_2026_paper.pdf)）：短期时空窗与持久事件摘要的分层记忆。
- **EMAGE**（[arXiv:2401.00374](https://arxiv.org/abs/2401.00374)）：音频驱动的全身手势，启发了节奏包络与内容包络分离。
- **SAiD**，*Speech-driven Blendshape*（[arXiv:2401.08655](https://arxiv.org/abs/2401.08655)）：情绪对说话韵律/表情的调制参考。

### 开源项目与工具

- [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)：主动发言、句子切分、透明桌宠和语音打断的工程参考。
- [moeru-ai/airi](https://github.com/moeru-ai/airi)：Live2D 视线分摊、弹簧、眨眼状态机、表情/动作管理。
- [m96-chan/VPresentation](https://github.com/m96-chan/VPresentation)：身体多层运动、阶跃朝向、有符号重音和口型。
- [AleySoler/live2dLumiPet](https://github.com/AleySoler/live2dLumiPet)：多表情混音、以双眼中心为原点的注视、VTube 参数映射。
- [Arkueid/live2d-py](https://github.com/Arkueid/live2d-py)（文档也见 [EasyLive2D](https://github.com/EasyLive2D/live2d-py)）：原生 Live2D 绑定；非官方 Cubism 封装。
- [pixi-live2d-display](https://github.com/guansss/pixi-live2d-display) 与 [PixiJS](https://github.com/pixijs/pixijs)：浏览器 Live2D 渲染栈。
- [VTube Studio Wiki](https://github.com/DenchiSoft/VTubeStudio/wiki/Loading-your-own-Models)：参数映射、`.vtube.json` 参考。
- [xfgryujk/blivedm](https://github.com/xfgryujk/blivedm)：B 站直播弹幕/礼物/上舰协议。
- [ra1nty/DXcam](https://github.com/ra1nty/DXcam)：DXGI 单帧抓取。
- [SAOS-3624/dsh-see-now](https://github.com/SAOS-3624/dsh-see-now)：视觉网格、色度变化检测和进程回环音频的实践参考（Apache-2.0）。
- [showlab/livecc](https://github.com/showlab/livecc)：LiveCC 论文的官方代码。
- [EsotericSoftware/spine-runtimes](https://github.com/EsotericSoftware/spine-runtimes)：Spine 运行时源码与构建来源。
- [rany2/edge-tts](https://github.com/rany2/edge-tts)：免费 Edge TTS 兜底。
- [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)：本地语音识别运行时。
- [Qdrant/fastembed](https://github.com/qdrant/fastembed)：本地向量嵌入运行时。
- [modelscope/FunASR](https://github.com/modelscope/FunASR)：SenseVoice 模型与语音识别生态。
- [Qwen/Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) 与 [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3)：可选语义召回模型。
- [DanielSWolf/rhubarb-lip-sync](https://github.com/DanielSWolf/rhubarb-lip-sync)：口型形状参考。
- [SillyTavern](https://github.com/SillyTavern/SillyTavern) 与 [ST-sound-forest-TTS](https://github.com/wyndam-c/ST-sound-forest-TTS)：TTS 厂商接入与字段设计的调研参考。
- [GLFW](https://github.com/glfw/glfw)、[Electron](https://github.com/electron/electron)、[SDL](https://github.com/libsdl-org/SDL)：透明窗/点击穿透的公开实现参考（仅借鉴行为，不引入其运行时）。

调研阶段还比较过 GPT-vup、AI-Vtuber-Bilibili、CosyVoice2、IndexTTS、GPT-SoVITS、piper 等项目；感谢其公开实现和文档。

也感谢 Fish Audio、MiniMax、小米 MiMo、ElevenLabs、Cartesia、Aivis、豆包、OpenAI、Microsoft Edge TTS 等厂商公开的 API 文档和试用额度，以及 B 站直播开放平台/社区协议资料。所有厂商名称、模型名和商标归各自权利人所有。

## 许可证

**本项目原创的代码和文档使用 MIT 许可证**，见 [LICENSE](LICENSE)。

核验结论：原创代码可用 MIT；Live2D Cubism Core / Spine 运行时为各自许可，不适用 MIT；`pygame`/`soxr`/`edge-tts` 等 LGPL 依赖需单独履行再分发义务。整个仓库并非单一 MIT。

| 范围 | 许可证 / 说明 |
|---|---|
| 本项目原创代码、文档 | MIT（[LICENSE](LICENSE)） |
| `overlay/lib/pixi.min.js`、`overlay/lib/live2d-engine.min.js`、`vendor/blivedm/` | MIT |
| `overlay/lib/live2dcubismcore.min.js` | **Live2D Cubism Core 专有许可**（Redistributable Code，文件头有版权与 EULA 链接） |
| `vendor/spine/spine*.dll`、`overlay/lib/spine-player-*` | **Spine Runtimes License**（许可全文在 `vendor/spine/SPINE-RUNTIMES-LICENSE-*.txt`，不得删除） |
| `pygame`、`soxr`、`edge-tts` | LGPL（2.1 / 3）；作为独立 pip 包使用，不改变本项目源码许可 |
| 其他 Python 依赖 | Apache-2.0 / BSD / MIT / PSF / MPL-2.0 等，见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) |
| Live2D / Spine 模型、用户人设、游戏资料 | **不包含**；由使用者提供，许可由使用者负责 |

**注意事项**：

- 仓库**不保存任何 Live2D / Spine 模型文件**；`models/images/deepseek.svg` 仅为默认兜底图，不属于 Live2D/Spine 模型，其标识归 DeepSeek 所有。
- 使用 Live2D 模型/运行时需遵守 Live2D 的专有许可；使用 Spine 形象需自行持有 **Spine Editor** 许可。Live2D/Spine 的许可不因本项目采用 MIT 而改变。
- 如将 Python 依赖一并打包进“一键发行版”，需额外满足 LGPL（pygame/soxr/edge-tts）的再分发义务，并保留全部第三方许可与版权声明。
- 商用/直播打赏前，应分别确认 Live2D、Spine、所用模型、音色/声音克隆、LLM/TTS 服务条款。MIT 仅覆盖本项目原创代码，不覆盖上述内容。

完整清单、各依赖许可证与再分发注意事项见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
