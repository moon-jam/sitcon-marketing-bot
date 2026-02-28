"""
Review 相關指令處理器
"""

import html
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    CallbackQueryHandler,
)

from database import (
    add_review,
    get_review_by_name,
    update_review_status,
    get_pending_reviews,
    get_all_active_reviews,
    get_all_reviewers,
    ReviewStatus,
    get_review_by_id,
    update_review_status_by_id,
)
from handlers.gitlab_client import gitlab_client
from scheduler import (
    send_pending_review_notification,
    notify_submitter_approved,
    notify_submitter_need_fix,
)
from handlers.utils import (
    get_allowed_chat_ids,
    extract_command_args,
    UnifiedCommandHandler,
    reply_and_track,
)


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
    格式：贊助商/文件名稱 : 連結（冒號前後可不加空格）
    回傳 (sponsor_name, link) 或 None（格式錯誤）
    """
    # 支援全形冒號，先將全形轉換為半形
    line = line.replace("：", ":")

    # 優先使用 " : " 作為分隔符（前後有空格）
    if " : " in line:
        parts = line.split(" : ", 1)
    else:
        # 容許冒號前後沒空格，但要避免拆到 URL 中的 "://"
        # Python 的 re 模組不支援變動長度的 lookbehind (?<!https?)
        # 因此改為使用固定長度的 (?<!http)(?<!https)
        match = re.match(r'^(.+?)(?<!/)(?<!http)(?<!https):(.*)', line)
        if not match:
            return None
        parts = [match.group(1), match.group(2)]

    if len(parts) != 2:
        return None

    sponsor_name = parts[0].strip()
    link = parts[1].strip()

    if not sponsor_name or not link:
        return None

    # if not is_valid_url(link):
    #     return None

    return (sponsor_name, link)


def format_review_list(reviews: list[dict], title: str) -> str:
    """格式化 review 清單（HTML 格式，支援摺疊）"""
    escaped_title = html.escape(title)
    if not reviews:
        return f"📋 {escaped_title}\n\n（無）"

    lines = []
    for r in reviews:
        status_emoji = {
            "pending": "⏳",
            "approved": "✅",
            "need_fix": "🔧",
        }.get(r["status"], "❓")

        lines.append(f"{status_emoji} {html.escape(r['sponsor_name'])}")
        lines.append(f"   連結：{html.escape(r['link'])}")
        if r.get("gitlab_issue_url"):
            lines.append(f"   GitLab：<a href=\"{r['gitlab_issue_url']}\">#{r['gitlab_issue_iid']}</a>")
        lines.append(f"   提交者：{html.escape(r['submitter_username'])}")
        if r.get("comment"):
            lines.append(f"   💬 評語：{html.escape(r['comment'])}")
        lines.append("")

    content = "\n".join(lines)
    return f"📋 {escaped_title}\n\n<blockquote expandable>{content}</blockquote>"


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
    text = extract_command_args(update.message, "review")

    error_msg = (
        "❌ 格式錯誤\n\n"
        "請確保使用冒號「:」分隔 名稱 與 連結。\n\n"
        "使用方式：\n"
        "/review 贊助商名稱 : 連結\n\n"
        "或批量新增：\n"
        "/review 贊助商1 : 連結1\n"
        "贊助商2 : 連結2\n"
        "贊助商3 : 連結3"
    )

    if not text:
        await reply_and_track(update, context, error_msg, "review_cmd")
        return

    # 先發送處理中訊息，加快回應速度
    processing_msg = await reply_and_track(update, context, "⏳ 處理中...正與 GitLab 同步資料", "review_cmd")

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

            # GitLab 開卡
            gitlab_issue_iid = None
            gitlab_issue_url = None
            try:
                # 嘗試從提交者名稱映射 GitLab ID 與 Username
                assignee_id = await gitlab_client.get_gitlab_user_id(submitter_username)
                gitlab_user = await gitlab_client.get_gitlab_username(submitter_username)

                # 如果有對應的 GitLab 使用者，則使用 @ 標記
                tag_str = f"@{gitlab_user}" if gitlab_user else f"@{submitter_username} (Telegram)"

                issue_title = f"[Review] {sponsor_name}"
                issue_desc = f"提交者：{tag_str}\\\n連結：{link}"
                labels = ["Status::Review", "Category::Task"]

                issue = await gitlab_client.create_issue(
                    title=issue_title,
                    description=issue_desc,
                    assignee_id=assignee_id,
                    labels=labels
                )
                if issue:
                    gitlab_issue_iid = issue.get("iid")
                    gitlab_issue_url = issue.get("web_url")
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"GitLab integration failed: {e}")

            await add_review(sponsor_name, link, submitter_id, submitter_username, gitlab_issue_iid, gitlab_issue_url)
            success_msg = f"✅ {html.escape(sponsor_name)}"
            if gitlab_issue_url:
                success_msg += f" (<a href=\"{gitlab_issue_url}\">GitLab Issue: #{gitlab_issue_iid}</a>)"
            elif gitlab_issue_iid:
                success_msg += f" (GitLab Issue: #{gitlab_issue_iid})"
            success_items.append(success_msg)
        else:
            failed_items.append(f"❌ {html.escape(line)}")

    if not success_items:
        if processing_msg:
            await processing_msg.edit_text(error_msg)
        else:
            await reply_and_track(update, context, error_msg, "review_cmd")
        return

    # 組織回覆訊息
    response_parts = []

    if success_items:
        # 取得所有 reviewer 並標記
        reviewers = await get_all_reviewers()
        reviewer_tags = " ".join([f"@{html.escape(r)}" for r in reviewers])

        msg = "📝 已新增 Review 請求：\n" + "\n".join(success_items)
        if reviewer_tags:
            msg += f"\n\n🔔 呼叫審核者：{reviewer_tags}"
        response_parts.append(msg)

    if failed_items:
        response_parts.append(
            "⚠️ 以下項目格式錯誤（應為「名稱 : 連結」）：\n" + "\n".join(failed_items)
        )

    # 顯示目前所有 pending 的 reviews
    pending_reviews = await get_pending_reviews()
    response_parts.append(format_review_list(pending_reviews, "目前待審核項目"))

    final_text = "\n\n".join(response_parts)
    if processing_msg:
        await processing_msg.edit_text(final_text, parse_mode="HTML")
    else:
        await reply_and_track(update, context, final_text, "review_cmd", parse_mode="HTML")


async def review_approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /review_approve 指令 - 顯示待審核項目選單"""
    if not update.message:
        return

    # 取得參數
    args_text = extract_command_args(update.message, "review_approve")
    args = args_text.split() if args_text else []

    # 如果有提供參數，直接審核該項目
    if args:
        sponsor_name = " ".join(args)
        await _do_approve(update, context, sponsor_name)
        return

    # 沒有參數時，顯示選單
    pending_reviews = await get_pending_reviews()

    if not pending_reviews:
        await reply_and_track(update, context, "📋 目前沒有待審核的項目", "review_cmd")
        return

    # 建立 InlineKeyboard
    keyboard = []
    for r in pending_reviews:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"✅ {r['sponsor_name']}",
                    callback_data=f"approve:{r['id']}",
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    await reply_and_track(
        update, context, "📋 請選擇要審核通過的項目：", "review_cmd", reply_markup=reply_markup
    )


async def _do_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sponsor_name: str = None, review_id: int = None
):
    """執行審核通過"""
    if review_id:
        review = await get_review_by_id(review_id)
        if review:
            sponsor_name = review["sponsor_name"]
    else:
        # 檢查是否存在
        review = await get_review_by_name(sponsor_name)

    if not review:
        if update.message:
            name_str = sponsor_name if sponsor_name else f"ID {review_id}"
            await update.message.reply_text(
                f"❌ 找不到「{name_str}」的 review 請求"
            )
        return False

    if review["status"] == ReviewStatus.APPROVED.value:
        if update.message:
            await update.message.reply_text(f"ℹ️ 「{sponsor_name}」已經是審核通過狀態")
        return False

    if review_id:
        success = await update_review_status_by_id(review_id, ReviewStatus.APPROVED)
    else:
        success = await update_review_status(sponsor_name, ReviewStatus.APPROVED)
        
    if success:
        # 關閉 GitLab Issue
        if review.get("gitlab_issue_iid"):
            try:
                await gitlab_client.close_issue(review["gitlab_issue_iid"])
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to close GitLab issue: {e}")

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

    # 解析 callback_data
    review_id_str = query.data.replace("approve:", "")
    try:
        review_id = int(review_id_str)
    except ValueError:
        await query.answer(text="❌ 無效的操作")
        return

    # 取得名稱用於顯示
    review = await get_review_by_id(review_id)
    sponsor_name = review["sponsor_name"] if review else f"ID:{review_id}"

    # 先回答 callback 避免 query 過期 (Query is too old)
    await query.answer(text=f"⏳ 正在審核「{sponsor_name}」...")

    success = await _do_approve(update, context, review_id=review_id)

    if success:
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_text(f"✅ 「{sponsor_name}」已審核通過！")
    else:
        await query.edit_message_text(
            f"❌ 審核「{sponsor_name}」失敗（可能已審核或不存在）"
        )


async def review_need_fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /review_need_fix 指令 - 顯示待審核項目選單，可選帶評語"""
    if not update.message:
        return

    # 解析評語（如果有的話）
    comment = extract_command_args(update.message, "review_need_fix")

    # 儲存評語到 user_data（給 callback 使用）
    if comment:
        context.user_data["need_fix_comment"] = comment

    # 顯示選單（顯示 pending 狀態的項目）
    pending_reviews = await get_pending_reviews()

    if not pending_reviews:
        await reply_and_track(update, context, "📋 目前沒有待審核的項目", "review_cmd")
        return

    # 建立 InlineKeyboard
    keyboard = []
    for r in pending_reviews:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"🔧 {r['sponsor_name']}",
                    callback_data=f"needfix:{r['id']}",
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)

    prompt = "📋 請選擇要標記為需要修改的項目："
    if comment:
        prompt += f"\n💬 評語：{comment}"

    await reply_and_track(update, context, prompt, "review_cmd", reply_markup=reply_markup)


async def _do_need_fix(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    sponsor_name: str = None,
    comment: str = None,
    review_id: int = None,
):
    """執行標記需要修改"""
    if review_id:
        review = await get_review_by_id(review_id)
        if review:
            sponsor_name = review["sponsor_name"]
    else:
        # 檢查是否存在
        review = await get_review_by_name(sponsor_name)
        
    if not review:
        if update.message:
            name_str = sponsor_name if sponsor_name else f"ID {review_id}"
            await update.message.reply_text(
                f"❌ 找不到「{name_str}」的 review 請求"
            )
        return False

    if review["status"] == ReviewStatus.APPROVED.value:
        if update.message:
            await update.message.reply_text(
                f"ℹ️ 「{sponsor_name}」已經審核通過，無法標記為需要修改"
            )
        return False

    if review_id:
        success = await update_review_status_by_id(review_id, ReviewStatus.NEED_FIX, comment)
    else:
        success = await update_review_status(sponsor_name, ReviewStatus.NEED_FIX, comment)
        
    if success:
        submitter = review.get("submitter_username", "未知")
        link = review.get("link", "")
        gitlab_url = review.get("gitlab_issue_url")
        gitlab_iid = review.get("gitlab_issue_iid")

        # 立刻通知提交者
        if submitter != "未知" and update.effective_chat:
            await notify_submitter_need_fix(
                context.bot,
                update.effective_chat.id,
                sponsor_name,
                submitter,
                link,
                comment,
                gitlab_url,
                gitlab_iid,
            )
        return True
    return False


async def need_fix_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理標記需要修改的 callback"""
    query = update.callback_query

    # 解析 callback_data
    review_id_str = query.data.replace("needfix:", "")
    try:
        review_id = int(review_id_str)
    except ValueError:
        await query.answer(text="❌ 無效的操作")
        return

    # 取得名稱用於顯示
    review = await get_review_by_id(review_id)
    sponsor_name = review["sponsor_name"] if review else f"ID:{review_id}"

    # 先回答 callback 避免 query 過期
    await query.answer(text=f"⏳ 正在標記「{sponsor_name}」為需修改...")

    # 取得評語（從 user_data）
    comment = context.user_data.pop("need_fix_comment", None)

    success = await _do_need_fix(update, context, comment=comment, review_id=review_id)

    if success:
        msg = f"🔧 「{sponsor_name}」已標記為需要修改"
        try:
            await query.message.delete()
        except Exception:
            if comment:
                msg += f"\n💬 評語：{comment}"
            await query.edit_message_text(msg)
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
        await reply_and_track(update, context, "📋 目前沒有待處理的 review 項目", "review_cmd")
        return

    # 分類顯示
    pending = [r for r in reviews if r["status"] == "pending"]
    need_fix = [r for r in reviews if r["status"] == "need_fix"]

    response_parts = []

    if pending:
        response_parts.append(format_review_list(pending, "待審核項目"))

    if need_fix:
        response_parts.append(format_review_list(need_fix, "待修改項目"))

    await reply_and_track(update, context, "\n".join(response_parts), "review_cmd", parse_mode="HTML")


async def review_notify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /review_notify 指令 - 手動觸發通知 reviewers"""
    if not update.message:
        return

    chat_ids = get_allowed_chat_ids()
    if not chat_ids and update.effective_chat:
        chat_ids = [update.effective_chat.id]

    sent = await send_pending_review_notification(context.bot, chat_ids)

    if not sent:
        await reply_and_track(update, context, "📋 目前沒有待審核的項目，或尚未設定 reviewers", "review_cmd")


async def review_again_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /review_again 指令 - 顯示待修改項目選單，選擇後改回待審核"""
    if not update.message:
        return

    reviews = await get_need_fix_reviews()

    if not reviews:
        await reply_and_track(update, context, "📋 目前沒有待修改的項目", "review_cmd")
        return

    # 建立 inline keyboard
    keyboard = []
    for review in reviews:
        name = review["sponsor_name"]
        keyboard.append([InlineKeyboardButton(name, callback_data=f"again:{review['id']}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await reply_and_track(
        update, context, "🔄 請選擇要重新送審的項目：", "review_cmd", reply_markup=reply_markup
    )


async def again_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 review_again inline keyboard 的 callback"""
    query = update.callback_query
    if not query or not query.data:
        return

    # 解析 callback_data: "again:review_id"
    review_id_str = query.data.replace("again:", "", 1)
    try:
        review_id = int(review_id_str)
    except ValueError:
        await query.answer(text="❌ 無效的操作")
        return

    # 檢查是否存在
    review = await get_review_by_id(review_id)
    if not review:
        await query.answer()
        await query.edit_message_text(f"❌ 找不到 ID {review_id} 的 review 請求")
        return

    sponsor_name = review["sponsor_name"]

    # 先回答 callback 避免 query 過期
    await query.answer(text=f"⏳ 正在重新送審「{sponsor_name}」...")

    if review["status"] != ReviewStatus.NEED_FIX.value:
        await query.edit_message_text(f"ℹ️ 「{sponsor_name}」不在待修改狀態")
        return

    # 改回 pending 狀態
    success = await update_review_status_by_id(review_id, ReviewStatus.PENDING)
    if success:
        link = review.get("link", "")
        result_text = f"🔄 「{sponsor_name}」已重新送審"
        if link:
            result_text += f"\n📎 連結：{link}"
        try:
            await query.message.delete()
            await context.bot.send_message(
                chat_id=query.message.chat_id, text=result_text
            )
        except Exception:
            await query.edit_message_text(result_text)
    else:
        await query.edit_message_text(f"❌ 更新「{sponsor_name}」狀態失敗")


def register_review_handlers(app, chat_filter=None):
    """註冊 review 相關的指令處理器"""
    handlers = [
        ("review", review_command),
        ("review_approve", review_approve_command),
        ("review_need_fix", review_need_fix_command),
        ("review_list", review_list_command),
        ("review_notify", review_notify_command),
        ("review_again", review_again_command),
    ]

    for cmd, callback in handlers:
        app.add_handler(UnifiedCommandHandler(cmd, callback, filters=chat_filter))

    # Callback handlers for inline keyboards
    app.add_handler(CallbackQueryHandler(approve_callback, pattern=r"^approve:"))
    app.add_handler(CallbackQueryHandler(need_fix_callback, pattern=r"^needfix:"))
    app.add_handler(CallbackQueryHandler(again_callback, pattern=r"^again:"))
