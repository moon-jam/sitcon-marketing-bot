"""
Reviewer 管理指令處理器
"""

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from database import add_reviewer, remove_reviewer, get_all_reviewers


async def reviewer_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /reviewer_add 指令"""
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "❌ 請提供 username\n" "使用方式：/reviewer_add <username>"
        )
        return

    username = context.args[0].lstrip("@")
    success = await add_reviewer(username)

    if success:
        await update.message.reply_text(f"✅ 已新增 reviewer：@{username}")
    else:
        await update.message.reply_text(f"ℹ️ @{username} 已經是 reviewer")


async def reviewer_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /reviewer_remove 指令"""
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "❌ 請提供 username\n" "使用方式：/reviewer_remove <username>"
        )
        return

    username = context.args[0].lstrip("@")
    success = await remove_reviewer(username)

    if success:
        await update.message.reply_text(f"✅ 已移除 reviewer：@{username}")
    else:
        await update.message.reply_text(f"❌ 找不到 reviewer：@{username}")


async def reviewer_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /reviewer_list 指令"""
    if not update.message:
        return

    reviewers = await get_all_reviewers()

    if not reviewers:
        await update.message.reply_text(
            "📋 Reviewer 清單\n\n"
            "（尚無任何 reviewer）\n\n"
            "使用 /reviewer_add <username> 新增"
        )
        return

    reviewer_list = "\n".join([f"• @{username}" for username in reviewers])
    await update.message.reply_text(f"📋 Reviewer 清單\n\n{reviewer_list}")


def register_reviewer_handlers(app, chat_filter=None):
    """註冊 reviewer 相關的指令處理器"""
    app.add_handler(
        CommandHandler("reviewer_add", reviewer_add_command, filters=chat_filter)
    )
    app.add_handler(
        CommandHandler("reviewer_remove", reviewer_remove_command, filters=chat_filter)
    )
    app.add_handler(
        CommandHandler("reviewer_list", reviewer_list_command, filters=chat_filter)
    )
