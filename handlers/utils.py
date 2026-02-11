"""
Handler 工具與通用函數
"""

import os
from urllib.parse import parse_qs, urlparse
from telegram import Update
from telegram.ext import CommandHandler


def get_allowed_chat_ids() -> list[int]:
    """從環境變數取得允許的聊天室 ID 清單"""
    chat_ids_str = os.getenv("ALLOWED_CHAT_IDS", "")
    if not chat_ids_str:
        return []

    chat_ids = []
    for id_str in chat_ids_str.split(","):
        id_str = id_str.strip()
        if id_str:
            try:
                chat_ids.append(int(id_str))
            except ValueError:
                pass
    return chat_ids


def extract_command_args(message, command: str = None) -> str:
    """
    從訊息中提取指令後的文字參數。
    支援一般指令和超連結形式指令。
    """
    if not message or not message.text:
        return ""

    text = message.text
    entities = message.entities or []

    for entity in entities:
        if entity.type == "bot_command":
            entity_text = text[entity.offset : entity.offset + entity.length]
            if command:
                expected = f"/{command}"
                if not (
                    entity_text == expected or entity_text.startswith(f"{expected}@")
                ):
                    continue
            return text[entity.offset + entity.length :].strip()

        elif (
            entity.type == "text_link"
            and entity.url
            and entity.url.startswith("tg://bot_command")
        ):
            parsed = urlparse(entity.url)
            qs = parse_qs(parsed.query)
            entity_cmd = qs.get("command", [None])[0]

            if command and entity_cmd != command:
                continue

            return text[entity.offset + entity.length :].strip()

    # Fallback: 手動分割
    if text.startswith("/"):
        parts = text.split(None, 1)
        return parts[1] if len(parts) > 1 else ""

    return ""


class UnifiedCommandHandler(CommandHandler):
    """
    統一指令處理器，同時支援標準指令與超連結指令 (tg://bot_command)
    """

    def check_update(self, update: Update):
        if not update.message or not update.message.text:
            return None

        # 1. 檢查標準 CommandHandler 邏輯
        res = super().check_update(update)
        if res:
            return res

        # 2. 檢查超連結形式
        message = update.message
        for entity in message.entities or []:
            if (
                entity.type == "text_link"
                and entity.url
                and entity.url.startswith("tg://bot_command")
            ):
                parsed = urlparse(entity.url)
                qs = parse_qs(parsed.query)
                cmd_in_url = qs.get("command", [None])[0]

                if cmd_in_url in self.commands:
                    return True
        return None
