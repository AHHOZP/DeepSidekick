# 模型目录（放这里就能用）

> **仓库里只跟一张图走：`models/images/deepseek.svg`**（没有可用形象时显示的兜底形象）。
> 其他模型文件一律不入库（Live2D 一套通常几十 MB，而且多半带第三方授权）。
> 新克隆的仓库默认只有这张图，往本目录放一套模型就能被扫描到。
> 本机已有的模型文件夹不受影响，照常能用。真要提交某一个：`git add -f models/<名字>`。

三种形象，按目录/文件名认，不用改配置：

```text
models/
  my-model/                  # Live2D：含 *.model3.json
    my-model.model3.json
    my-model.moc3
    textures/ motions/ expressions/
  hero/                      # Spine：同名 .skel 或 .json + .atlas + 页图
    hero.skel
    hero.atlas
    hero.png
  images/deepseek.svg        # 单张图片：models/images/ 或 models/ 根目录
  cat.png
```

单张图片支持 `.png` / `.jpg` / `.jpeg` / `.webp` / `.bmp` / `.gif` / `.svg`
（gif 只取第一帧，透明背景照常透到桌面）。**只认 `models/` 根目录和 `models/images/`
两处**：放在别的子目录里不会被当成形象，免得把模型内部纹理误认成独立形象。

Live2D 只支持 **Cubism 3 / 4 / 5**（`xxx.model3.json` + `xxx.moc3`）。
Cubism 2.1 的 `.model.json` + `.moc` 是另一套内核（另一套参数命名、另一套纹理坐标），
本项目只打包了 `cubism4` runtime，**不认得也不会加载**，放进来会被静默忽略。

Spine 只支持 **3.8 / 4.0 / 4.1 / 4.2**（3.5/3.6 的许可不允许分发，3.7/4.3 未验证）。

**这个文件夹只放模型素材**。角色的名字、别名、人设文本、动作规则不放这里：

```text
persona/roles/<角色 id>/
  profile.yaml     # name / bot_name / aliases / stage
  00-core.txt …    # 只写要覆盖的文件
  actions.md       # 动作规则
```

WebUI「角色设定」页就是编辑这组文件；不用手算路径。

## 角色 id 是内容指纹，不是文件夹名

服务端按模型内容生成 id：

- Live2D：`moc3` 文件内容的 SHA1 前 16 位（没有 moc3 时用 `model3.json`）；
- Spine：骨架文件内容的 SHA1 前 16 位；
- 单张图片：文件内容的 SHA1 前 16 位（默认那张图也一样，没有任何内置特例）。

`config.yaml` 里默认的 `role.id: deepseek` 是**角色设定目录名**（`persona/roles/deepseek/`），
跟用哪套形象无关；找不到对应形象时按扫描顺序选中第一套可用的。

所以：

- **文件夹改名、搬家都不会丢角色设定**；改 `model3.json` 里的组名/动作引用也不影响
  （指纹只看 moc3）。
- 重新导出、换 `moc3` 会被当成一套新模型（旧角色设定留在 `persona/roles/<旧 id>/`）。
- 内容完全相同的两份模型会得到同一个 id；后扫到的那份加 `-2` 后缀，各自独立。
- 显示名默认是文件夹名，可以在 `persona/roles/<id>/profile.yaml` 的 `name` 里改。

## 服务端还会

1. 递归找 `*.model3.json`；含模型文件夹之外的路径（`.git`/`node_modules` 等）跳过；
2. 读 `Motions` / `Expressions`；缺引用时自动从 `motions/`、`Animation/`、`Expressions/`、
   `Exp3/` 扫描补齐（不改原文件）；
3. 自动猜待机组；口型参数写成能力清单；眨眼组缺失时补 `ParamEyeLOpen` + `ParamEyeROpen`
   （引擎只读 `Groups`，不补就不眨眼）；
4. 生成 capabilities，页面和 prompt 都按当前模型动态更新；
5. 入库前体检 moc3 / 纹理，失败会标「不可用」并显示原因，不会静默白屏；
6. 合并 `persona/roles/<id>/` 里的角色设定（名字/别名/排版/人设目录）。

## 动作和表情从哪来

- **动作** = `model3.json` 里 `Motions` 的每个 key（motion group），标签就是组名。
  想让标签变成中文，就在 model3 的 `Motions` 里把组名写成中文。
- **表情** = `Expressions` 里的每一项（缺引用时扫 `Expressions/`、`Exp3/` 下的 `*.exp3.json`）。
  标签取 `Expressions[].Name`；没有 Name 时才用文件名。
- **待机** = 组名是 `idle` / `wait` / `stand` / `待机` 的那一个。
- 口型：model3 里写了 `LipSync` 组就按写的来，没写默认 `ParamMouthOpenY`。

在 WebUI「角色设定」页选中后写 `config.yaml` 的 `role.id`，**重启 runtime 才换上**
（人设、形象、动作能力一起换；TTS/LLM/直播记忆不受影响）。
直播姬里的页面每 5 秒看一次 `/config.json`，重启后自动刷新。
不可用的模型不进下拉，原因会黄字列在「角色设定」页的角色选择框下方。

## 动作规则：`persona/roles/<id>/actions.md`

可选（UTF-8，最多 2000 字）：

```text
persona/roles/<id>/
  profile.yaml
  actions.md        # 手写，或在 WebUI「角色设定」页一键生成后保存
```

- 它是**唯一**的动作规则来源；没有文件就表示这个角色不教动作标签；
- 内容直接注入 prompt 的输出约束段；
- WebUI 的编辑器会按当前模型的 capabilities 提示可用标签，并标出未知标签；
- 删除文件 = 删除规则，不需要改代码。
