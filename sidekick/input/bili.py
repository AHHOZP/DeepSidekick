"""真弹幕源：blivedm。

**只读。** 不调发送接口、不关注、不点赞——这条源里根本没有那些函数。

昵称：不带 SESSDATA 时 B 站会把名字打成 a***b、uid=0；
带上（一个 cookie，和你在浏览器里看直播时带的是同一个）就能拿真名。

人数：blivedm 不直接给，另开一个 30 秒的 HTTP 轮询拿 room/get_info 的 online。
"""
from __future__ import annotations

import asyncio
import sys
from http.cookies import Morsel
from pathlib import Path

# blivedm 不在 PyPI，源码 vendor 在项目根的 vendor/ 下（commit 已经钉死）。
# 装它会让 pip 卡在隔离构建环境，所以这里直接把 vendor 加进导入路径。
_VENDOR = Path(__file__).resolve().parents[2] / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from sidekick.core.events import DANMU, ENTER, GIFT, GUARD_BUY, SC, Event, EventBus

ROOM_INFO = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room}"
# B 站接口不带浏览器 UA 会返回非 JSON（get_info 直接 412/风控），人数就永远读不到
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}


async def _room_state(room_id: int) -> dict:
    """查房间状态。失败返回空 dict（调用方按"未知"处理）。"""
    import aiohttp

    url = ROOM_INFO.format(room=room_id)
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json(content_type=None)
        return (data or {}).get("data") or {}
    except Exception:
        return {}


async def _viewers_loop(bus: EventBus, room_id: int, log, every: float = 30.0) -> None:
    import aiohttp

    url = ROOM_INFO.format(room=room_id)
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        while True:
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json(content_type=None)
                d = (data or {}).get("data") or {}
                bus.set_viewers(int(d.get("online") or 0))
            except Exception as e:
                log(f"[bili] 人数轮询失败：{type(e).__name__}: {e}")
            await asyncio.sleep(every)


def _limit(value, n: int = 200) -> str:
    """弹幕/醒目留言是外部输入：截断到固定长度，防止单条撑爆材料。"""
    text = str(value or "")
    return text if len(text) <= n else text[:n - 1] + "…"


def _handler_class(bus: EventBus):
    """blivedm 1.1.7 的回调是**同步**的（全库没有 iscoroutine），所以这里不能用 async def。

    字段名也对过模型：DanmakuMessage.uname/msg、GiftMessage.uname/gift_name/num、
    GuardBuyMessage.**username**（不是 uname）、SuperChatMessage.uname/message/price、
    InteractWordV2Message.**username**。
    """
    import blivedm

    class Handler(blivedm.BaseHandler):
        def _on_danmaku(self, client, message):  # noqa: ANN001
            bus.push(Event(kind=DANMU,
                           name=_limit(getattr(message, "uname", ""), 40),
                           text=_limit(getattr(message, "msg", ""))))

        def _on_gift(self, client, message):  # noqa: ANN001
            name = getattr(message, "gift_name", "") or ""
            num = int(getattr(message, "num", 0) or 0)
            if not name or num <= 0:
                return
            bus.push(Event(kind=GIFT,
                           name=_limit(getattr(message, "uname", ""), 40),
                           text=_limit(name, 80), extra={"num": num}))

        def _on_buy_guard(self, client, message):  # noqa: ANN001
            bus.push(Event(kind=GUARD_BUY,
                           name=_limit(getattr(message, "username", ""), 40),
                           text=_limit(getattr(message, "gift_name", "") or "舰长", 80)))

        def _on_super_chat(self, client, message):  # noqa: ANN001
            bus.push(Event(kind=SC,
                           name=_limit(getattr(message, "uname", ""), 40),
                           text=_limit(getattr(message, "message", ""), 300),
                           price=float(getattr(message, "price", 0) or 0)))

        def _on_interact_word_v2(self, client, message):  # noqa: ANN001
            if int(getattr(message, "msg_type", 0) or 0) == 1:  # 1 = 进场
                bus.push(Event(kind=ENTER, name=getattr(message, "username", "") or ""))

    return Handler


async def run(bus: EventBus, cfg: dict, log=print) -> None:
    import aiohttp

    room_id = int(cfg["room_id"])
    if not room_id:
        log("[bili] room_id 是 0，不连。config.yaml 里填上房间号再切 danmu_source: bili")
        return

    sessdata = str(cfg["sessdata"])
    if not sessdata:
        log("[bili] 没配 SESSDATA：能收到弹幕，但昵称会是 a***b")

    Handler = _handler_class(bus)
    viewers_task = asyncio.create_task(_viewers_loop(bus, room_id, log))

    import blivedm

    backoff = 3.0
    try:
        while True:
            # B 站**只有开播期间才有弹幕流**：房间没开播时连上去也是空的，
            # 还会一秒重连一次刷屏。所以先看 live_status，没开播就每 30 秒看一次。
            st = await _room_state(room_id)
            if st and int(st.get("live_status") or 0) != 1:
                title = str(st.get("title") or "")[:30]
                log(f"[bili] 房间 {room_id}（{title}）没开播，弹幕要开播后才有 —— "
                    f"每 30 秒看一次，开播自动连上")
                await asyncio.sleep(30)
                continue

            try:
                async with aiohttp.ClientSession(headers=HEADERS) as session:
                    if sessdata:
                        # 不能 response_url=https://www.bilibili.com：aiohttp 会存成
                        # host-only www 域，blivedm 查 api.bilibili.com 的登录态时
                        # 根本看不到，uid 永远是 0、昵称永远是 a***b。
                        m = Morsel()
                        m.set("SESSDATA", sessdata, sessdata)
                        m["domain"] = ".bilibili.com"
                        m["path"] = "/"
                        session.cookie_jar.update_cookies({"SESSDATA": m})
                    # 1.1.7 的正确用法：客户端负责房间，处理器通过 set_handler 挂上去；
                    # start() 是同步的（内部起 task），join() 才是要 await 的那个。
                    client = blivedm.BLiveClient(room_id, session=session)
                    client.set_handler(Handler())
                    client.start()
                    log(f"[bili] 开始连接房间 {room_id} …")
                    backoff = 3.0
                    await client.join()
                    log("[bili] 连接断了，5 秒后重连")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"[bili] 出错：{type(e).__name__}: {e}；{backoff:.0f} 秒后重试")
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            await asyncio.sleep(5.0)
    finally:
        viewers_task.cancel()
        try:
            await viewers_task
        except (asyncio.CancelledError, Exception):
            pass
