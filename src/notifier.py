"""
推送通知模块

支持渠道：
- Server酱（微信，推荐，sct.ftqq.com）
- PushPlus（微信，备选，pushplus.plus）
- 邮件（SMTP）

Token 从环境变量读取：
- SERVERCHAN_KEY
- PUSHPLUS_TOKEN
- EMAIL_HOST, EMAIL_PORT, EMAIL_USER, EMAIL_PASSWORD, EMAIL_TO
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class Notifier:
    """统一推送入口"""

    def __init__(self, config: dict):
        self.config = config or {}

    def send(self, title: str, content: str, content_html: Optional[str] = None):
        """
        Args:
            title: 标题（短）
            content: 正文（Markdown 格式，微信和邮件都能接受）
            content_html: 邮件用的 HTML 版本（可选，没有就用 content 转）
        """
        results = []
        if self.config.get("serverchan", {}).get("enabled"):
            results.append(("ServerChan", self._send_serverchan(title, content)))
        if self.config.get("pushplus", {}).get("enabled"):
            results.append(("PushPlus", self._send_pushplus(title, content)))
        if self.config.get("email", {}).get("enabled"):
            to = self.config.get("email", {}).get("to") or os.getenv("EMAIL_TO", "")
            results.append(("Email", self._send_email(title, content, content_html, to)))

        success = [name for name, ok in results if ok]
        failed = [name for name, ok in results if not ok]
        if success:
            logger.info(f"[Notifier] 推送成功: {', '.join(success)}")
        if failed:
            logger.warning(f"[Notifier] 推送失败: {', '.join(failed)}")
        return results

    # ---------------- Server酱 ----------------
    def _send_serverchan(self, title: str, content: str) -> bool:
        key = os.getenv("SERVERCHAN_KEY")
        if not key:
            logger.warning("[Notifier] SERVERCHAN_KEY 未配置，跳过")
            return False
        url = f"https://sctapi.ftqq.com/{key}.send"
        try:
            resp = httpx.post(
                url,
                data={"title": title[:32], "desp": content[:32000]},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                return True
            logger.error(f"[ServerChan] 失败: {data}")
            return False
        except Exception as e:
            logger.error(f"[ServerChan] 异常: {e}")
            return False

    # ---------------- PushPlus ----------------
    def _send_pushplus(self, title: str, content: str) -> bool:
        token = os.getenv("PUSHPLUS_TOKEN")
        if not token:
            logger.warning("[Notifier] PUSHPLUS_TOKEN 未配置，跳过")
            return False
        try:
            resp = httpx.post(
                "https://www.pushplus.plus/send",
                json={
                    "token": token,
                    "title": title,
                    "content": content,
                    "template": "markdown",
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 200:
                return True
            logger.error(f"[PushPlus] 失败: {data}")
            return False
        except Exception as e:
            logger.error(f"[PushPlus] 异常: {e}")
            return False

    # ---------------- 邮件 ----------------
    def _send_email(self, title: str, content: str, content_html: Optional[str], to: str) -> bool:
        host = os.getenv("EMAIL_HOST")
        user = os.getenv("EMAIL_USER")
        password = os.getenv("EMAIL_PASSWORD")
        if not all([host, user, password, to]):
            logger.warning("[Notifier] 邮件配置不完整，跳过")
            return False
        port = int(os.getenv("EMAIL_PORT") or "465")

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = title
            msg["From"] = user
            msg["To"] = to

            # 纯文本版本
            msg.attach(MIMEText(content, "plain", "utf-8"))
            # HTML 版本（如果没有就用 markdown 转一下）
            if not content_html:
                content_html = self._md_to_html(content)
            msg.attach(MIMEText(content_html, "html", "utf-8"))

            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(user, password)
                server.send_message(msg)
            return True
        except Exception as e:
            logger.error(f"[Email] 发送失败: {e}")
            return False

    @staticmethod
    def _md_to_html(text: str) -> str:
        """简单的 markdown -> html，不引入额外依赖"""
        lines = text.split("\n")
        html_lines = []
        for line in lines:
            line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if line.startswith("# "):
                html_lines.append(f"<h2>{line[2:]}</h2>")
            elif line.startswith("## "):
                html_lines.append(f"<h3>{line[3:]}</h3>")
            elif line.startswith("- "):
                html_lines.append(f"<li>{line[2:]}</li>")
            elif line.strip() == "":
                html_lines.append("<br>")
            else:
                # 加粗 **xxx**
                import re
                line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
                html_lines.append(f"<p>{line}</p>")
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
        <style>
          body{{font-family:-apple-system,sans-serif;max-width:600px;margin:20px auto;padding:20px;color:#222}}
          h2{{color:#fbbf24;border-bottom:2px solid #fbbf24;padding-bottom:6px}}
          strong{{color:#dc2626}}
        </style></head><body>{''.join(html_lines)}</body></html>"""
