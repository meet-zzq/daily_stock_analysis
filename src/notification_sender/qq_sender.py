# -*- coding: utf-8 -*-
"""
===================================
QQ 机器人通知发送器
===================================

通过 QQ 开放平台 Bot API 发送消息。
基于 Hermes Agent 的 QQ Bot 适配器实现（gateway/platforms/qqbot/）。

认证方式：AppID + AppSecret → 获取 access_token → 调用 REST API

API 文档：https://bot.q.qq.com/wiki/develop/api-v2/

支持两种发送目标：
1. 私聊（C2C）：QQ_USER_OPENID
2. 群聊：QQ_GROUP_OPENID
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional

import requests

from src.config import Config

logger = logging.getLogger(__name__)

# ---- QQ Bot API 端点 ----
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
C2C_MESSAGE_PATH = "/v2/users/{openid}/messages"
GROUP_MESSAGE_PATH = "/v2/groups/{group_openid}/messages"

# 消息类型
MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2

# Token 过期余量（秒），在过期前提前刷新
TOKEN_REFRESH_MARGIN = 60.0

# 消息长度限制
MAX_MESSAGE_LENGTH = 4000


class QqSender:
    """QQ 机器人消息发送器。

    使用 QQ 开放平台 Bot API 发送消息到 QQ 用户或群。
    支持纯文本和 Markdown 格式。

    用法::

        sender = QqSender(config)
        sender.send_to_qq("消息内容", title="股票分析报告")
    """

    def __init__(self, config: Config):
        self._app_id = getattr(config, "qq_app_id", None)
        self._client_secret = getattr(config, "qq_client_secret", None)
        self._user_openid = getattr(config, "qq_user_openid", None)
        self._group_openid = getattr(config, "qq_group_openid", None)
        # Token 缓存（进程生命周期内复用）
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def send_to_qq(
        self,
        content: str,
        title: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """推送消息到 QQ。

        Args:
            content: 消息内容（Markdown 格式）
            title: 消息标题（可选，会在内容前插入 ## 标题）

        Returns:
            是否全部发送成功（所有目标均成功才返回 True）
        """
        if not self._is_configured():
            logger.warning("QQ 机器人配置不完整，跳过推送")
            return False

        # 拼接标题
        if title and not content.startswith("#"):
            full_content = f"## {title}\n\n{content}"
        else:
            full_content = content

        # 发送到所有配置的目标
        try:
            token = self._ensure_token()
            success = True

            if self._user_openid:
                ok = self._send_c2c(token, self._user_openid, full_content, timeout_seconds)
                if not ok:
                    success = False

            if self._group_openid:
                ok = self._send_group(token, self._group_openid, full_content, timeout_seconds)
                if not ok:
                    success = False

            return success

        except Exception as exc:
            logger.error("发送 QQ 消息失败: %s", exc)
            logger.debug("QQ 发送异常详情", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Token 管理
    # ------------------------------------------------------------------

    def _ensure_token(self) -> str:
        """获取有效的 access_token，必要时自动刷新（进程内缓存）。"""
        if self._access_token and time.time() < self._token_expires_at - TOKEN_REFRESH_MARGIN:
            return self._access_token

        try:
            logger.info("正在刷新 QQ Bot access_token...")
            resp = requests.post(
                TOKEN_URL,
                json={"appId": self._app_id, "clientSecret": self._client_secret},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"获取 QQ Bot access_token 失败: {exc}") from exc

        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"QQ Bot token 响应缺少 access_token: {data}")

        expires_in = int(data.get("expires_in", 7200))
        self._access_token = token
        self._token_expires_at = time.time() + expires_in
        logger.info("QQ Bot access_token 刷新成功，有效期 %d 秒", expires_in)
        return token

    # ------------------------------------------------------------------
    # 发送逻辑
    # ------------------------------------------------------------------

    def _send_c2c(
        self,
        token: str,
        openid: str,
        content: str,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """发送私聊（C2C）消息，自动分段。"""
        return self._send_chunked(self._send_single_c2c, token, openid, content, timeout_seconds)

    def _send_single_c2c(
        self,
        token: str,
        openid: str,
        content: str,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """发送单条私聊（C2C）消息。"""
        url = f"{API_BASE}{C2C_MESSAGE_PATH.format(openid=openid)}"
        headers = self._build_headers(token)

        body, is_markdown = self._build_message_body(content)
        body["msg_seq"] = int(time.time())

        try:
            resp = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=timeout_seconds or 30,
            )

            if resp.status_code < 400:
                logger.info("QQ 私聊消息发送成功 (openid=%s)", openid[:8] + "...")
                return True

            # Markdown 失败时降级为纯文本重试
            if is_markdown and resp.status_code in (400, 403, 415):
                logger.warning(
                    "QQ Markdown 发送失败 (HTTP %s)，降级为纯文本重试",
                    resp.status_code,
                )
                text_body = {
                    "content": self._strip_markdown(content),
                    "msg_type": MSG_TYPE_TEXT,
                    "msg_seq": int(time.time()),
                }
                resp = requests.post(
                    url,
                    headers=headers,
                    json=text_body,
                    timeout=timeout_seconds or 30,
                )
                if resp.status_code < 400:
                    logger.info("QQ 私聊纯文本发送成功 (降级)")
                    return True

            logger.error(
                "QQ 私聊发送失败: HTTP %s, body=%s",
                resp.status_code,
                resp.text[:500],
            )
            return False

        except requests.exceptions.Timeout:
            logger.error("QQ 私聊请求超时")
            return False
        except requests.exceptions.RequestException as exc:
            logger.error("QQ 私聊网络请求异常: %s", exc)
            return False

    def _send_group(
        self,
        token: str,
        group_openid: str,
        content: str,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """发送群聊消息，自动分段。"""
        return self._send_chunked(self._send_single_group, token, group_openid, content, timeout_seconds)

    def _send_single_group(
        self,
        token: str,
        group_openid: str,
        content: str,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """发送单条群聊消息。"""
        url = f"{API_BASE}{GROUP_MESSAGE_PATH.format(group_openid=group_openid)}"
        headers = self._build_headers(token)

        body, is_markdown = self._build_message_body(content)
        body["msg_seq"] = int(time.time())

        try:
            resp = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=timeout_seconds or 30,
            )

            if resp.status_code < 400:
                logger.info("QQ 群聊消息发送成功 (group=%s)", group_openid[:8] + "...")
                return True

            # Markdown 降级
            if is_markdown and resp.status_code in (400, 403, 415):
                logger.warning(
                    "QQ 群聊 Markdown 发送失败 (HTTP %s)，降级为纯文本重试",
                    resp.status_code,
                )
                text_body = {
                    "content": self._strip_markdown(content),
                    "msg_type": MSG_TYPE_TEXT,
                    "msg_seq": int(time.time()),
                }
                resp = requests.post(
                    url,
                    headers=headers,
                    json=text_body,
                    timeout=timeout_seconds or 30,
                )
                if resp.status_code < 400:
                    logger.info("QQ 群聊纯文本发送成功 (降级)")
                    return True

            logger.error(
                "QQ 群聊发送失败: HTTP %s, body=%s",
                resp.status_code,
                resp.text[:500],
            )
            return False

        except requests.exceptions.Timeout:
            logger.error("QQ 群聊请求超时")
            return False
        except requests.exceptions.RequestException as exc:
            logger.error("QQ 群聊网络请求异常: %s", exc)
            return False

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk_content(content: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
        """将长内容按 max_len 分段，尽量在 Markdown 标题或换行处断开。

        Returns:
            分段后的内容列表，每段不超过 max_len。
        """
        if len(content) <= max_len:
            return [content]

        chunks: list[str] = []
        start = 0

        while start < len(content):
            if len(content) - start <= max_len:
                chunks.append(content[start:])
                break

            # 尝试在 max_len 范围内找到合适的断点
            end = start + max_len
            # 优先找最近的 Markdown 二级/三级标题
            split_at = content.rfind("\n## ", start, end)
            if split_at <= start:
                split_at = content.rfind("\n### ", start, end)
            if split_at <= start:
                split_at = content.rfind("\n\n", start, end)
            if split_at <= start:
                split_at = content.rfind("\n", start, end)
            if split_at <= start:
                # 实在找不到，硬切
                split_at = end

            chunks.append(content[start:split_at].strip())
            start = split_at

        return chunks

    def _send_chunked(
        self,
        send_fn,
        token: str,
        target: str,
        content: str,
        timeout_seconds: Optional[float],
    ) -> bool:
        """将长内容分段发送，每段独立发送。

        Args:
            send_fn: 单条发送函数（_send_single_c2c 或 _send_single_group）
        """
        chunks = self._chunk_content(content)
        all_ok = True
        for i, chunk in enumerate(chunks):
            if not chunk:
                continue
            if i > 0 and len(chunks) > 1:
                logger.info("发送分段 %d/%d (%d 字符)", i + 1, len(chunks), len(chunk))
            ok = send_fn(token, target, chunk, timeout_seconds)
            if not ok:
                all_ok = False
        return all_ok

    def _is_configured(self) -> bool:
        """检查配置是否完整。"""
        if not self._app_id or not self._client_secret:
            logger.warning("QQ_APP_ID 或 QQ_CLIENT_SECRET 未配置")
            return False
        if not self._user_openid and not self._group_openid:
            logger.warning(
                "QQ_USER_OPENID 和 QQ_GROUP_OPENID 均未配置（至少需配置一个目标）"
            )
            return False
        return True

    def _build_headers(self, token: str) -> Dict[str, str]:
        """构建 API 请求头。"""
        return {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "QQBotAdapter/daily_stock_analysis",
        }

    def _build_message_body(self, content: str) -> tuple[Dict[str, Any], bool]:
        """构建消息体，优先使用 Markdown。

        Returns:
            (body_dict, is_markdown)
        """
        # 内容已在外层（_send_chunked）分段，此处无需再次截断
        if self._has_markdown(content):
            return (
                {
                    "markdown": {"content": content},
                    "msg_type": MSG_TYPE_MARKDOWN,
                },
                True,
            )
        else:
            return (
                {
                    "content": content,
                    "msg_type": MSG_TYPE_TEXT,
                },
                False,
            )

    @staticmethod
    def _has_markdown(text: str) -> bool:
        """简单判断文本是否包含 Markdown 标记。"""
        markers = [
            "#",       # 标题
            "**",      # 粗体
            "* ",      # 无序列表
            r"1. ",    # 有序列表
            "---",     # 分隔线
            "```",     # 代码块
            "> ",      # 引用
            "[",       # 链接
            "![",      # 图片
            "|",       # 表格
        ]
        return any(marker in text for marker in markers)

    @staticmethod
    def _strip_markdown(markdown_text: str) -> str:
        """清理 Markdown 标记为纯文本（降级发送用）。"""
        text = markdown_text
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)  # 粗体
        text = re.sub(r"\*([^*]+)\*", r"\1", text)      # 斜体
        text = re.sub(r"`([^`]+)`", r"\1", text)         # 行内代码
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)  # 标题
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)       # 图片
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)        # 链接
        text = re.sub(r"```[\s\S]*?```", "", text)                   # 代码块
        return text.strip()
