"""形象层自检：不接直播姬，直接在终端里连上 WS，看服务器推了什么。

    python scripts/smoke_stage.py

它自己起服务、自己连、自己推一条 say + 一串 level + end，
把收到的消息打出来。浏览器那边能不能画出来，得你在直播姬里看。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import config
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.avatar.stage import StageClient, serve_stage, stage_url


async def main() -> int:
    import aiohttp

    cfg = config.load()
    cfg["stage"] = {**(cfg.get("stage") or {}), "port": 17892}   # 换个端口，别撞上正在跑的
    client = StageClient()
    runner = await serve_stage(client, cfg, catalog=AvatarCatalog(ROOT))
    url = stage_url(cfg)
    print(f"服务起来了：{url}")

    got: list[dict] = []
    bad = 0
    async with aiohttp.ClientSession() as s:
        # /config.json 里的 model 就是当前形象的入口；id 可能是中文，正好验一遍编码
        async with s.get(url.rstrip("/") + "/config.json") as r:
            conf = await r.json()
        model_url = str(conf.get("model") or "")
        print(f"当前形象：{conf.get('name')!r}  id={conf.get('avatar')!r}")
        paths = ["/", "/probe", "/s/app.js", "/s/style.css",
                 "/s/lib/pixi.min.js"]
        if model_url:
            paths.append(model_url)
        for path in paths:
            async with s.get(url.rstrip("/") + path) as r:
                if r.status != 200:
                    bad += 1
                print(f"  GET {path:44s} {r.status}  {r.headers.get('Content-Length', '?')} 字节")

        async with s.ws_connect(url.replace("http://", "ws://") + "ws") as ws:
            hello = await ws.receive_json()
            print(f"\n握手：{hello}")

            async def reader() -> None:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        got.append(json.loads(msg.data))

            task = asyncio.create_task(reader())
            await asyncio.sleep(0.2)

            pages = ["欸你看这个血条。", "都快空了还不躲。", "算了，我陪你再打一次。"]
            print("\n推一条 say + 20 个 level + end")
            client.emit("say", pages=pages, cps=4.6, idx=0)
            await asyncio.sleep(0.15)
            for i in range(20):
                client.emit("level", v=round(0.1 + 0.5 * abs((i % 10) - 5) / 5, 3))
                await asyncio.sleep(0.05)
            client.emit("page", idx=1)
            await asyncio.sleep(0.15)
            client.emit("end", pages=len(pages))
            await asyncio.sleep(0.3)
            task.cancel()

    print(f"\n收到 {len(got)} 条：")
    for m in got[:4]:
        print("  ", json.dumps(m, ensure_ascii=False))
    if len(got) > 4:
        print(f"   …（后面 {len(got) - 4} 条 level）")
    print(f"\n客户端统计：{client.stats()}")
    await runner.cleanup()
    if bad:
        print(f"\n[!] 有 {bad} 个请求不是 200 —— 静态文件或模型入口有问题")
        return 1
    return 0 if got else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
