"""
Review 相關指令處理器
"""

import os
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

from database import (
    add_review,
    get_review_by_name,
    update_review_status,
    get_pending_reviews,
    get_need_fix_reviews,
    get_all_active_reviews,
    ReviewStatus,
)
from scheduler import (
    send_pending_review_notification,
    notify_submitter_approved,
    notify_submitter_need_fix,
)


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


def is_valid_url(text: str) -> bool:
    """檢查是否為有效的 URL"""
    url_pattern = re.compile(
        r"^https?://"  # http:// or https://
        r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"  # domain
        r"localhost|"  # localhost
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # or ip
        r"(?::\d+)?"  # optional port
        r"(?:/?|[/?]\S+)$",
        re.IGNORECASE,
    )
    return url_pattern.match(text) is not None


def parse_review_line(line: str) -> tuple[str, str] | None:
    """
    解析單行 review 輸入
    格式：贊助商/文件名稱 : 連結
    回傳 (sponsor_name, link) 或 None（格式錯誤）
    """
    # 使用 " : " 作為分隔符（前後有空格）
    if " : " not in line:
        return None

    parts = line.split(" : ", 1)
    if len(parts) != 2:
        return None

    sponsor_name = parts[0].strip()
    link = parts[1].strip()

    if not sponsor_name or not link:
        return None

    if not is_valid_url(link):
        return None

    return (sponsor_name, link)


def format_review_list(reviews: list[dict], title: str) -> str:
    """格式化 review 清單"""
    if not reviews:
        return f"📋 {title}\n\n（無）"

    lines = [f"📋 {title}\n"]
    for r in reviews:
        status_emoji = {
            "pending": "⏳",
            "approved": "✅",
            "need_fix": "🔧",
        }.get(r["status"], "❓")

        lines.append(f"{status_emoji} {r['sponsor_name']}")
        lines.append(f"   連結：{r['link']}")
        lines.append(f"   提交者：@{r['submitter_username']}")
        lines.append("")

    return "\n".join(lines)


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    處理 /review 指令
    支援單行或多行輸入：
    /review 贊助商1 : https://link1
    贊助商2 : https://link2
    """
    if not update.message or not update.message.text:
        return

    # 取得指令後的所有文字
    text = update.message.text

    # 移除 /review 指令本身
    if text.startswith("/review@"):
        # 處理 /review@botname 的情況
        text = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
    elif text.startswith("/review"):
        text = text[7:].strip()

    if not text:
        await update.message.reply_text(
            "❌ 格式錯誤\n\n"
            "使用方式：\n"
            "/review 贊助商名稱 : 連結\n\n"
            "或批量新增：\n"
            "/review 贊助商1 : 連結1\n"
            "贊助商2 : 連結2\n"
            "贊助商3 : 連結3"
        )
        return

    # 分割多行
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    success_items = []
    failed_items = []

    user = update.message.from_user
    submitter_id = user.id
    submitter_username = user.username or user.first_name or str(user.id)

    for line in lines:
        parsed = parse_review_line(line)
        if parsed:
            sponsor_name, link = parsed
            await add_review(sponsor_name, link, submitter_id, submitter_username)
            success_items.append(f"✅ {sponsor_name}")
        else:
            failed_items.append(f"❌ {line}")

    # 組織回覆訊息
    response_parts = []

    if success_items:
        response_parts.append("📝 已新增 Review 請求：\n" + "\n".join(success_items))

    if failed_items:
        response_parts.append(
            "⚠️ 以下項目格式錯誤（應為「名稱 : 連結」）：\n" + "\n".join(failed_items)
        )

    # 顯示目前所有 pending 的 reviews
    pending_reviews = await get_pending_reviews()
    response_parts.append(format_review_list(pending_reviews, "目前待審核項目"))

    await update.message.reply_text("\n\n".join(response_parts))


async def review_approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /review_approve 指令 - 顯示待審核項目選單"""
    if not update.message:
        return

    # 如果有提供參數，直接審核該項目
    if context.args:
        sponsor_name = " ".join(context.args)
        await _do_approve(update, context, sponsor_name)
        return

    # 沒有參數時，顯示選單
    pending_reviews = await get_pending_reviews()

    if not pending_reviews:
        await update.message.reply_text("📋 目前沒有待審核的項目")
        return

    # 建立 InlineKeyboard
    keyboard = []
    for r in pending_reviews:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"✅ {r['sponsor_name']}",
                    callback_data=f"approve:{r['sponsor_name']}",
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📋 請選擇要審核通過的項目：", reply_markup=reply_markup
    )


async def _do_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sponsor_name: str
):
    """執行審核通過"""
    # 檢查是否存在
    review = await get_review_by_name(sponsor_name)
    if not review:
        if update.message:
            await update.message.reply_text(
                f"❌ 找不到「{sponsor_name}」的 review 請求"
            )
        return False

    if review["status"] == ReviewStatus.APPROVED.value:
        if update.message:
            await update.message.reply_text(f"ℹ️ 「{sponsor_name}」已經是審核通過狀態")
        return False

    success = await update_review_status(sponsor_name, ReviewStatus.APPROVED)
    if success:
        # 通知提交者
        submitter = review.get("submitter_username", "")
        if submitter and update.effective_chat:
            await notify_submitter_approved(
                context.bot, update.effective_chat.id, sponsor_name, submitter
            )
        return True
    return False


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理審核通過的 callback"""
    query = update.callback_query
    await query.answer()

    # 解析 callback_data
    sponsor_name = query.data.replace("approve:", "")

    success = await _do_approve(update, context, sponsor_name)

    if success:
        await query.edit_message_text(f"✅ 「{sponsor_name}」已審核通過！")
    else:
        await query.edit_message_text(
            f"❌ 審核「{sponsor_name}」失敗（可能已審核或不存在）"
        )


async def review_need_fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /review_need_fix 指令 - 顯示待審核項目選單"""
    if not update.message:
        return

    # 如果有提供參數，直接標記該項目
    if context.args:
        sponsor_name = " ".join(context.args)
        await _do_need_fix(update, context, sponsor_name)
        return

    # 沒有參數時，顯示選單（顯示 pending 狀態的項目）
    pending_reviews = await get_pending_reviews()

    if not pending_reviews:
        await update.message.reply_text("📋 目前沒有待審核的項目")
        return

    # 建立 InlineKeyboard
    keyboard = []
    for r in pending_reviews:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"🔧 {r['sponsor_name']}",
                    callback_data=f"needfix:{r['sponsor_name']}",
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📋 請選擇要標記為需要修改的項目：", reply_markup=reply_markup
    )


async def _do_need_fix(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sponsor_name: str
):
    """執行標記需要修改"""
    # 檢查是否存在
    review = await get_review_by_name(sponsor_name)
    if not review:
        if update.message:
            await update.message.reply_text(
                f"❌ 找不到「{sponsor_name}」的 review 請求"
            )
        return False

    if review["status"] == ReviewStatus.APPROVED.value:
        if update.message:
            await update.message.reply_text(
                f"ℹ️ 「{sponsor_name}」已經審核通過，無法標記為需要修改"
            )
        return False

    success = await update_review_status(sponsor_name, ReviewStatus.NEED_FIX)
    if success:
        submitter = review.get("submitter_username", "未知")
        link = review.get("link", "")

        # 立刻通知提交者
        if submitter != "未知" and update.effective_chat:
            await notify_submitter_need_fix(
                context.bot, update.effective_chat.id, sponsor_name, submitter, link
            )
        return True
    return False


async def need_fix_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理標記需要修改的 callback"""
    query = update.callback_query
    await query.answer()

    # 解析 callback_data
    sponsor_name = query.data.replace("needfix:", "")

    success = await _do_need_fix(update, context, sponsor_name)

    if success:
        await query.edit_message_text(f"🔧 「{sponsor_name}」已標記為需要修改")
    else:
        await query.edit_message_text(
            f"❌ 標記「{sponsor_name}」失敗（可能已審核或不存在）"
        )


async def review_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /review-list 指令"""
    if not update.message:
        return

    reviews = await get_all_active_reviews()

    if not reviews:
        await update.message.reply_text("📋 目前沒有待處理的 review 項目")
        return

    # 分類顯示
    pending = [r for r in reviews if r["status"] == "pending"]
    need_fix = [r for r in reviews if r["status"] == "need_fix"]

    response_parts = []

    if pending:
        response_parts.append(format_review_list(pending, "待審核項目"))

    if need_fix:
        response_parts.append(format_review_list(need_fix, "待修改項目"))

    await update.message.reply_text("\n".join(response_parts))


async def review_notify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /review_notify 指令 - 手動觸發通知 reviewers"""
    if not update.message:
        return

    chat_ids = get_allowed_chat_ids()
    if not chat_ids and update.effective_chat:
        chat_ids = [update.effective_chat.id]

    sent = await send_pending_review_notification(context.bot, chat_ids)

    if not sent:
        await update.message.reply_text("📋 目前沒有待審核的項目，或尚未設定 reviewers")


async def review_again_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /review_again 指令 - 顯示待修改項目選單，選擇後改回待審核"""
    if not update.message:
        return

    reviews = await get_need_fix_reviews()

    if not reviews:
        await update.message.reply_text("📋 目前沒有待修改的項目")
        return

    # 建立 inline keyboard
    keyboard = []
    for review in reviews:
        name = review["sponsor_name"]
        keyboard.append([InlineKeyboardButton(name, callback_data=f"again:{name}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🔄 請選擇要重新送審的項目：", reply_markup=reply_markup
    )


async def again_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 review_again inline keyboard 的 callback"""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    # 解析 callback_data: "again:贊助商名稱"
    sponsor_name = query.data.replace("again:", "", 1)

    # 檢查是否存在
    review = await get_review_by_name(sponsor_name)
    if not review:
        await query.edit_message_text(f"❌ 找不到「{sponsor_name}」的 review 請求")
        return

    if review["status"] != ReviewStatus.NEED_FIX.value:
        await query.edit_message_text(f"ℹ️ 「{sponsor_name}」不在待修改狀態")
        return

    # 改回 pending 狀態
    success = await update_review_status(sponsor_name, ReviewStatus.PENDING)
    if success:
        link = review.get("link", "")
        if link:
            await query.edit_message_text(
                f"🔄 「{sponsor_name}」已重新送審\n📎 連結：{link}"
            )
        else:
            await query.edit_message_text(f"🔄 「{sponsor_name}」已重新送審")
    else:
        await query.edit_message_text(f"❌ 更新「{sponsor_name}」狀態失敗")


def register_review_handlers(app, chat_filter=None):
    """註冊 review 相關的指令處理器"""
    app.add_handler(CommandHandler("review", review_command, filters=chat_filter))
    app.add_handler(
        CommandHandler("review_approve", review_approve_command, filters=chat_filter)
    )
    app.add_handler(
        CommandHandler("review_need_fix", review_need_fix_command, filters=chat_filter)
    )
    app.add_handler(
        CommandHandler("review_list", review_list_command, filters=chat_filter)
    )
    app.add_handler(
        CommandHandler("review_notify", review_notify_command, filters=chat_filter)
    )
    app.add_handler(
        CommandHandler("review_again", review_again_command, filters=chat_filter)
    )

    # Callback handlers for inline keyboards
    app.add_handler(CallbackQueryHandler(approve_callback, pattern=r"^approve:"))
    app.add_handler(CallbackQueryHandler(need_fix_callback, pattern=r"^needfix:"))
    app.add_handler(CallbackQueryHandler(again_callback, pattern=r"^again:"))
