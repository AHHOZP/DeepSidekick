# 游戏知识包（可插拔）

一个游戏一个目录，放在这个目录。程序按 `config.yaml` 的 `game.id` 读取：

```yaml
game:
  enabled: true
  id: <game_id>      # → persona/games/<game_id>/
```

目录格式：

```text
persona/games/<id>/
├── core.md          # 常驻背景总览（必填才有内容）
└── entries/*.md     # 按 keys/always 召回（可选）
```

它是**独立的一个 system 消息**，位置在稳定前缀里：

```text
system = 人设 → 用户档案 → 游戏 core → 永久记忆
…历史 → 动态块（当前游戏状态/任务/召回）→ 本轮材料 → 输出约束
```

没有游戏资料时这一条自动跳过，不会留下空 system。所以换游戏不影响主 prompt：

1. 新建 `persona/games/<新id>/core.md`（可选 `entries/`）
2. 改 `config.yaml` 的 `game.id`
3. 保存。下一轮生效，不用改当前角色的人设文本。

不想注入游戏知识就 `game.enabled: false`：游戏 core、entries/always/召回、当前游戏状态和游戏记忆都跳过；
人设、用户档案、永久记忆、历史、动态块和输出约束都还在，system 条数不是固定的两个。

## 整个 persona 都不入库

`persona/` 整体被 `.gitignore` 忽略。本机已有的游戏知识包留在磁盘上照常能用，只是不进 git；
想提交某个：`git add -f persona/games/<id>`。

没有内置示例：新建 `core.md` 后写 1-2 句世界观就能被当前游戏读到；没写就是空，不注入。

## 写法约定

- 第二人称：称呼角色为「你」，用户为「他」。
- 只写公开资料；不确定的标「据报道」。
- 剧情放在「可能剧透」区，并要求不主动说破。
- 不要写成长篇攻略；这是给她接话用的背景，不是让她背设定集。
- 不要在这里放人的安全底线/输出格式，那些归当前角色的 `00-core.txt`、`30-material.txt`。
