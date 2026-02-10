"""
SITCON Marketing Bot - Review 管理機器人

功能：
- /review：新增 review 請求（支援批量）
- /review_approve：選擇待審核項目審核通過（並通知提交者）
- /review_need_fix：選擇標記需要修改（並立刻通知提交者）
- /review_again：重新送審（待修改項目修改完成後）
- /review_list：列出所有待處理項目
- /review_notify：手動觸發通知 reviewers
- /reviewer_add：新增 reviewer
- /reviewer_remove：移除 reviewer
- /reviewer_list：列出所有 reviewers
"""

import logging
import os
import sys

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from database import init_db
from handlers import register_review_handlers, register_reviewer_handlers
from scheduler import setup_scheduler

# 載入環境變數
load_dotenv()

# 設定 logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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
                logger.warning(f"Invalid chat ID: {id_str}")

    return chat_ids


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /start 指令"""
    if not update.message:
        return

    await update.message.reply_text(
        "👋 你好！我是 SITCON Review 管理機器人\n\n"
        "📝 Review 管理：\n"
        "• /review <名稱> : <連結> - 新增 review 請求\n"
        "• /review_approve - 選擇審核通過項目\n"
        "• /review_need_fix - 選擇標記需要修改項目\n"
        "• /review_again - 重新送審（待修改項目修改完成）\n"
        "• /review_list - 列出待處理項目\n"
        "• /review_notify - 手動通知 reviewers\n\n"
        "👥 Reviewer 管理：\n"
        "• /reviewer_add <username> - 新增 reviewer\n"
        "• /reviewer_remove <username> - 移除 reviewer\n"
        "• /reviewer_list - 列出 reviewers\n\n"
        "⏰ 提醒：我會依照設定週期自動通知 reviewers\n"
        "💡 提示：可以批量新增 review，每行一個"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /help 指令"""
    await start_command(update, context)


async def post_init(application: Application) -> None:
    """Bot 啟動後設定指令清單（讓 / 能自動補完）"""
    commands = [
        BotCommand("review", "新增 review 請求（名稱 : 連結）"),
        BotCommand("review_approve", "選擇審核通過項目"),
        BotCommand("review_need_fix", "選擇標記需要修改項目"),
        BotCommand("review_again", "重新送審（修改完成）"),
        BotCommand("review_list", "列出待處理項目"),
        BotCommand("review_notify", "手動通知 reviewers"),
        BotCommand("reviewer_add", "新增 reviewer"),
        BotCommand("reviewer_remove", "移除 reviewer"),
        BotCommand("reviewer_list", "列出 reviewers"),
        BotCommand("help", "顯示使用說明"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands registered")


def main():
    """主程式進入點"""
    # 取得 Bot Token
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("BOT_TOKEN 環境變數未設定")
        sys.exit(1)

    # 取得允許的聊天室 ID
    allowed_chat_ids = get_allowed_chat_ids()
    if allowed_chat_ids:
        logger.info(f"Allowed chat IDs: {allowed_chat_ids}")
    else:
        logger.warning("ALLOWED_CHAT_IDS 未設定，所有聊天室都可以使用指令")

    # 建立 Application（加入 post_init 設定指令補完）
    app = Application.builder().token(bot_token).post_init(post_init).build()

    # 建立聊天室過濾器
    chat_filter = filters.Chat(allowed_chat_ids) if allowed_chat_ids else None

    # 註冊基本指令（受聊天室限制）
    app.add_handler(CommandHandler("start", start_command, filters=chat_filter))
    app.add_handler(CommandHandler("help", help_command, filters=chat_filter))

    # 註冊 review 和 reviewer 相關指令
    register_review_handlers(app, chat_filter)
    register_reviewer_handlers(app, chat_filter)

    # 初始化資料庫
    import asyncio

    asyncio.get_event_loop().run_until_complete(init_db())
    logger.info("Database initialized")

    # 設定排程提醒（只有設定了聊天室 ID 才啟用）
    if allowed_chat_ids:
        setup_scheduler(app, allowed_chat_ids)
    else:
        logger.warning("Scheduled reminders disabled (no ALLOWED_CHAT_IDS configured)")

    # 啟動 Bot
    logger.info("Starting bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
