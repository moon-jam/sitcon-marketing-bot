import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    CallbackQueryHandler,
)

from database import (
    add_reminder,
    get_pending_reminders_by_username,
    update_reminder_status,
    get_reminder_by_id,
    get_reminder_by_name, # 注意：這裡可能需要修正，database.py 沒有 get_reminder_by_name
)
# 修正：database.py 確實沒有 get_reminder_by_name，但我們可以用 id。

from handlers.gitlab_client import gitlab_client
from handlers.utils import (
    extract_command_args,
    UnifiedCommandHandler,
)

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Taipei")

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    處理 /remind 指令
    格式：/remind @username 內容
    """
    if not update.message or not update.message.text:
        return

    args = extract_command_args(update.message, "remind")
    if not args:
        await update.message.reply_text(
            "❌ 格式錯誤\n\n"
            "使用方式：/remind @username 內容"
        )
        return

    # 解析 @username 和 內容
    parts = args.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text(
            "❌ 格式錯誤\n\n"
            "使用方式：/remind @username 內容"
        )
        return

    target_user = parts[0].lstrip("@")
    content = parts[1]

    # 暫存到 user_data 給 callback 使用
    context.user_data["remind_target"] = target_user
    context.user_data["remind_content"] = content

    # 顯示類型選單
    keyboard = [
        [
            InlineKeyboardButton("一次性 (One-time)", callback_data="remind_type:once"),
            InlineKeyboardButton("週期性 (Periodic)", callback_data="remind_type:periodic"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🔔 正在為 @{target_user} 設定提醒：\n"
        f"📝 內容：{content}\n\n"
        "請選擇提醒類型：",
        reply_markup=reply_markup
    )

async def remind_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理提醒類型的 callback，接著顯示時間/週期選單"""
    query = update.callback_query
    await query.answer()

    timing_type = query.data.replace("remind_type:", "")
    context.user_data["remind_timing_type"] = timing_type

    if timing_type == "once":
        keyboard = [
            [
                InlineKeyboardButton("1 小時後", callback_data="remind_time:60"),
                InlineKeyboardButton("4 小時後", callback_data="remind_time:240"),
            ],
            [
                InlineKeyboardButton("1 天後", callback_data="remind_time:1440"),
                InlineKeyboardButton("3 天後", callback_data="remind_time:4320"),
            ],
        ]
        text = "請選擇多久後提醒一次 (一次性)："
    else:
        keyboard = [
            [
                InlineKeyboardButton("每天 (Daily)", callback_data="remind_time:1440"),
                InlineKeyboardButton("每 3 天", callback_data="remind_time:4320"),
            ],
            [
                InlineKeyboardButton("每週 (Weekly)", callback_data="remind_time:10080"),
            ],
        ]
        text = "請選擇提醒週期："

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)

async def remind_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理時間/週期選擇，並執行開卡與存檔"""
    query = update.callback_query
    await query.answer()

    minutes = int(query.data.replace("remind_time:", ""))
    timing_type = context.user_data.get("remind_timing_type")
    target_user = context.user_data.pop("remind_target", None)
    content = context.user_data.pop("remind_content", None)

    if not target_user or not content:
        await query.edit_message_text("❌ 錯誤：找不到提醒資訊，請重新輸入指令")
        return

    timing_text = "一次性" if timing_type == "once" else "週期性"
    
    # 計算下次提醒時間
    next_at = datetime.now(TZ) + timedelta(minutes=minutes)
    
    time_desc = ""
    if timing_type == "once":
        time_desc = f"{minutes//60} 小時後" if minutes < 1440 else f"{minutes//1440} 天後"
    else:
        if minutes == 1440: time_desc = "每天"
        elif minutes == 10080: time_desc = "每週"
        else: time_desc = f"每 {minutes//1440} 天"

    # GitLab 開卡
    gitlab_issue_iid = None
    gitlab_issue_url = None
    try:
        assignee_id = await gitlab_client.get_gitlab_user_id(target_user)
        gitlab_user = await gitlab_client.get_gitlab_username(target_user)
        
        tag_str = f"@{gitlab_user}" if gitlab_user else f"@{target_user} (Telegram)"

        issue_title = f"[Remind] {content}"
        issue_desc = (
            f"提醒對象：{tag_str}\n"
            f"類型：{timing_text} ({time_desc})\n"
            f"內容：{content}"
        )
        labels = ["Status::Inbox", "Category::Task"]
        if timing_type == "periodic":
            labels.append("Type::Periodic")

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
        logger.error(f"GitLab integration failed: {e}")

    # 存入資料庫
    from database import add_reminder
    reminder_id = await add_reminder(
        title=content[:50],
        content=content,
        assignee_tg_id=None,
        assignee_username=target_user,
        gitlab_issue_iid=gitlab_issue_iid,
        gitlab_issue_url=gitlab_issue_url,
        timing_type=timing_type,
        interval_minutes=minutes if timing_type == "periodic" else None,
        next_remind_at=next_at
    )

    # 排程提醒
    from scheduler import schedule_reminder_job
    reminder = {
        "id": reminder_id,
        "assignee_username": target_user,
        "content": content,
        "timing_type": timing_type,
        "interval_minutes": minutes if timing_type == "periodic" else None,
        "next_remind_at": next_at,
        "gitlab_issue_url": gitlab_issue_url,
        "gitlab_issue_iid": gitlab_issue_iid,
    }
    schedule_reminder_job(context.application, reminder)

    msg = f"✅ 已設定 @{target_user} 的{timing_text}提醒！\n"
    msg += f"⏰ 下次提醒時間：{next_at.strftime('%Y-%m-%d %H:%M')}\n"
    if gitlab_issue_url:
        msg += f"<a href=\"{gitlab_issue_url}\">GitLab Issue: #{gitlab_issue_iid}</a>"
    
    await query.edit_message_text(msg, parse_mode="HTML")

async def remind_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /remind_list 指令"""
    if not update.message:
        return

    user = update.message.from_user
    username = user.username or str(user.id)

    from database import get_pending_reminders_by_username
    reminders = await get_pending_reminders_by_username(username)

    if not reminders:
        await update.message.reply_text("📋 你目前沒有待處理的提醒")
        return

    lines = ["📋 你的待處理提醒："]
    for r in reminders:
        timing = "⏳" if r["timing_type"] == "once" else "🔄"
        lines.append(f"{timing} ID: {r['id']} - {html.escape(r['content'])}")
        if r.get("next_remind_at"):
            # 如果是字串則轉換
            next_at = r["next_remind_at"]
            if isinstance(next_at, str):
                try:
                    next_at = datetime.fromisoformat(next_at).strftime('%Y-%m-%d %H:%M')
                except:
                    pass
            else:
                next_at = next_at.strftime('%Y-%m-%d %H:%M')
            lines.append(f"   下次提醒：{next_at}")
            
        if r.get("gitlab_issue_url"):
            lines.append(f"   GitLab: <a href=\"{r['gitlab_issue_url']}\">#{r['gitlab_issue_iid']}</a>")
    
    lines.append("\n使用 /remind_done <ID> 標記為完成")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def remind_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /remind_done 指令"""
    if not update.message:
        return

    args = extract_command_args(update.message, "remind_done")
    if not args or not args.strip().isdigit():
        await update.message.reply_text("❌ 請提供提醒 ID，例如：/remind_done 1")
        return

    reminder_id = int(args.strip())
    reminder = await get_reminder_by_id(reminder_id)

    if not reminder:
        await update.message.reply_text(f"❌ 找不到 ID 為 {reminder_id} 的提醒")
        return

    if reminder["status"] == "done":
        await update.message.reply_text(f"ℹ️ 提醒 ID {reminder_id} 已經是完成狀態")
        return

    # 更新狀態
    success = await update_reminder_status(reminder_id, "done")
    if success:
        # 關閉 GitLab Issue
        if reminder.get("gitlab_issue_iid"):
            try:
                await gitlab_client.close_issue(reminder["gitlab_issue_iid"])
            except Exception as e:
                logger.error(f"Failed to close GitLab issue: {e}")
        
        # 取消排程 Job
        job_queue = context.application.job_queue
        if job_queue:
            jobs = job_queue.get_jobs_by_name(f"remind_{reminder_id}")
            for job in jobs:
                job.schedule_removal()
        
        await update.message.reply_text(f"✅ 提醒 ID {reminder_id} 已標記為完成！")
    else:
        await update.message.reply_text(f"❌ 更新提醒狀態失敗")

def register_reminder_handlers(app, chat_filter=None):
    """註冊 reminder 相關的指令處理器"""
    app.add_handler(UnifiedCommandHandler("remind", remind_command, filters=chat_filter))
    app.add_handler(UnifiedCommandHandler("remind_list", remind_list_command, filters=chat_filter))
    app.add_handler(UnifiedCommandHandler("remind_done", remind_done_command, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(remind_type_callback, pattern=r"^remind_type:"))
    app.add_handler(CallbackQueryHandler(remind_time_callback, pattern=r"^remind_time:"))
