"""
Handler 工具與通用函數
"""

import os
from urllib.parse import parse_qs, urlparse
from telegram import Update, Bot
from telegram.ext import CommandHandler, ContextTypes

from database import track_bot_message, get_and_clear_bot_messages

async def reply_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, msg_type: str, reply_markup=None, parse_mode=None):
    """發送訊息並追蹤，同時刪除舊訊息以防洗版"""
    chat_id = update.effective_chat.id if update.effective_chat else None
    
    if chat_id:
        old_msg_ids = await get_and_clear_bot_messages(chat_id, msg_type)
        for msg_id in old_msg_ids:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    try:
        msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        if chat_id:
            await track_bot_message(chat_id, msg.message_id, msg_type)
        return msg
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to reply_and_track: {e}")
        return None

async def send_and_track(bot: Bot, chat_id: int, text: str, msg_type: str, reply_markup=None, parse_mode=None):
    """用 bot.send_message 發送訊息並追蹤，同時刪除舊訊息以防洗版"""
    old_msg_ids = await get_and_clear_bot_messages(chat_id, msg_type)
    for msg_id in old_msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        await track_bot_message(chat_id, msg.message_id, msg_type)
        return msg
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to send_and_track to {chat_id}: {e}")
        return None


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
