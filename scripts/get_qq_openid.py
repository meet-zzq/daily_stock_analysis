"""
QQ Bot OpenID 获取工具
======================
运行后连接到 QQ Bot WebSocket 网关，
你向机器人发任意消息，它会回复你的 OpenID。

依赖：pip install httpx websockets
"""

import asyncio
import json
import logging
import sys
import time

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("qq_openid_finder")

# 配置——从 .env 读取或直接填
QQ_APP_ID = "1904912741"
QQ_CLIENT_SECRET = "eaWTRPONNOPRUXbfkqw3AIQZis2DOamz"

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"


def get_access_token() -> str:
    """获取 access_token"""
    resp = httpx.post(
        TOKEN_URL,
        json={"appId": QQ_APP_ID, "clientSecret": QQ_CLIENT_SECRET},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"获取 token 失败: {data}")
    logger.info("✅ Token 获取成功")
    return token


def get_gateway_url(token: str) -> str:
    """获取 WebSocket 网关地址"""
    resp = httpx.get(
        f"{API_BASE}/gateway",
        headers={"Authorization": f"QQBot {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    url = resp.json().get("url")
    logger.info("🔗 网关地址: %s", url)
    return url


async def main():
    token = get_access_token()
    gateway_url = get_gateway_url(token)

    try:
        import websockets
    except ImportError:
        logger.error("请先安装 websockets: pip install websockets")
        sys.exit(1)

    async with websockets.connect(gateway_url) as ws:
        logger.info("🔌 WebSocket 已连接")

        seq = 0  # 消息序号，用于心跳

        async def send_heartbeat():
            """发送心跳"""
            nonlocal seq
            while True:
                await asyncio.sleep(30)
                try:
                    await ws.send(json.dumps({"op": 1, "d": seq}))
                except Exception:
                    break

        async def on_message(data: dict):
            """处理收到的消息"""
            nonlocal seq
            op = data.get("op")
            d = data.get("d", {})
            s = data.get("s", 0)
            t = data.get("t", "")

            if s:
                seq = s

            if op == 10:  # Hello — 收到网关欢迎消息
                heartbeat_interval = d.get("heartbeat_interval", 30000) / 1000
                logger.info("👋 收到 Hello, 心跳间隔 %.1f 秒", heartbeat_interval)

                # 发送 Identify
                identify = {
                    "op": 2,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": (1 << 25),  # 仅 C2C_GROUP_AT_MESSAGES（私聊+群@消息）
                        "shard": [0, 1],
                        "properties": {
                            "$os": "windows",
                            "$browser": "daily_stock_analysis",
                            "$device": "pc",
                        },
                    },
                }
                await ws.send(json.dumps(identify))
                logger.info("📤 Identify 已发送，等待就绪...")
                asyncio.create_task(send_heartbeat())

            elif op == 0:  # Dispatch — 事件
                if t == "READY":
                    logger.info("✅ Bot 已就绪！")
                    logger.info("   Bot 用户ID: %s", d.get("user", {}).get("id"))

                elif t in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
                    author_id = d.get("author", {}).get("user_openid", "")
                    group_id = d.get("group_openid", "")
                    content_text = d.get("content", "")

                    if t == "C2C_MESSAGE_CREATE":
                        logger.info("💬 收到私聊消息:")
                        logger.info("   User OpenID: %s", author_id)
                        logger.info("   内容: %s", content_text)

                        # 回复用户其 OpenID
                        reply_body = {
                            "content": f"✅ 你的 QQ OpenID 是：\n`{author_id}`\n\n把这个值填到 QQ_USER_OPENID 即可接收推送 🚀",
                            "msg_type": 0,
                        }
                        async with httpx.AsyncClient(timeout=30) as client:
                            await client.post(
                                f"{API_BASE}/v2/users/{author_id}/messages",
                                headers={
                                    "Authorization": f"QQBot {token}",
                                    "Content-Type": "application/json",
                                },
                                json=reply_body,
                            )

                        logger.info("✅ 已回复 OpenID 给用户")
                        return  # 退出

                    elif t == "GROUP_AT_MESSAGE_CREATE":
                        logger.info("💬 收到群 @ 消息:")
                        logger.info("   Group OpenID: %s", group_id)
                        logger.info("   User OpenID: %s", author_id)

                        # 回复群消息
                        reply_body = {
                            "content": f"✅ 本群的 Group OpenID 是：\n`{group_id}`\n\n把这个值填到 QQ_GROUP_OPENID 即可接收推送 🚀",
                            "msg_type": 0,
                            "msg_seq": int(time.time()),
                        }
                        async with httpx.AsyncClient(timeout=30) as client:
                            await client.post(
                                f"{API_BASE}/v2/groups/{group_id}/messages",
                                headers={
                                    "Authorization": f"QQBot {token}",
                                    "Content-Type": "application/json",
                                },
                                json=reply_body,
                            )
                        logger.info("✅ 已回复 Group OpenID 到群聊")
                        return  # 退出

                elif op == 9:  # Invalid session
                    logger.error("❌ Session 无效，请检查 token 和 intents")

        # 主循环
        async for raw in ws:
            try:
                data = json.loads(raw)
                await on_message(data)
            except Exception as e:
                logger.error("处理消息异常: %s", e)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 已退出")
