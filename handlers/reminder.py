import html
import logging
from datetime import datetime, timedelta, time
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
)
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
    直接進入日期選擇
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

    # 第一步：直接顯示日期選擇
    keyboard = [
        [
            InlineKeyboardButton("今天", callback_data="remind_day:0"),
            InlineKeyboardButton("明天", callback_data="remind_day:1"),
        ],
        [
            InlineKeyboardButton("後天", callback_data="remind_day:2"),
            InlineKeyboardButton("下週一", callback_data="remind_day:mon"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🔔 正在為 @{target_user} 設定提醒：\n"
        f"📝 內容：{content}\n\n"
        "📅 請選擇提醒日期：",
        reply_markup=reply_markup
    )

async def remind_day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """第二步：選擇具體時段"""
    query = update.callback_query
    await query.answer()

    day_code = query.data.replace("remind_day:", "")
    
    # 計算日期
    now = datetime.now(TZ)
    if day_code == "mon":
        days_ahead = 7 - now.weekday()
        if days_ahead <= 0: days_ahead += 7
        target_date = now + timedelta(days=days_ahead)
    else:
        target_date = now + timedelta(days=int(day_code))
    
    context.user_data["remind_target_date"] = target_date.date().isoformat()

    # 時段選單
    keyboard = [
        [
            InlineKeyboardButton("早上 09:00", callback_data="remind_time:09:00"),
            InlineKeyboardButton("中午 12:00", callback_data="remind_time:12:00"),
        ],
        [
            InlineKeyboardButton("下午 15:00", callback_data="remind_time:15:00"),
            InlineKeyboardButton("晚上 18:00", callback_data="remind_time:18:00"),
        ],
        [
            InlineKeyboardButton("深夜 21:00", callback_data="remind_time:21:00"),
            InlineKeyboardButton("自訂 (1小時後)", callback_data="remind_time:relative_60"),
        ]
    ]
    
    date_str = target_date.strftime('%Y-%m-%d')
    day_name = "今天" if day_code == "0" else "明天" if day_code == "1" else "後天" if day_code == "2" else "下週一"
    text = f"⏰ 請選擇 {day_name} ({date_str}) 的提醒時間："
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)

async def remind_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理最終時間確認並執行（包含 GitLab Due Date）"""
    query = update.callback_query
    await query.answer()

    time_val = query.data.replace("remind_time:", "")
    target_user = context.user_data.pop("remind_target", None)
    content = context.user_data.pop("remind_content", None)

    if not target_user or not content:
        await query.edit_message_text("❌ 錯誤：找不到提醒資訊，請重新輸入指令")
        return

    now = datetime.now(TZ)
    next_at = None
    time_desc = ""

    if time_val.startswith("relative_"):
        minutes = int(time_val.replace("relative_", ""))
        next_at = now + timedelta(minutes=minutes)
        time_desc = f"{minutes} 分鐘後"
    else:
        date_str = context.user_data.pop("remind_target_date")
        target_date = datetime.fromisoformat(date_str).date()
        hour, minute = map(int, time_val.split(":"))
        next_at = datetime.combine(target_date, time(hour, minute)).replace(tzinfo=TZ)
        
        # 如果選的是今天但時間已經過了，自動加一天
        if next_at < now:
            next_at += timedelta(days=1)
        
        time_desc = next_at.strftime('%Y-%m-%d %H:%M')

    # GitLab 開卡（同步設定 Due Date）
    gitlab_issue_iid = None
    gitlab_issue_url = None
    due_date = next_at.strftime('%Y-%m-%d') # 使用提醒日期作為 Due Date

    try:
        assignee_id = await gitlab_client.get_gitlab_user_id(target_user)
        gitlab_user = await gitlab_client.get_gitlab_username(target_user)
        tag_str = f"@{gitlab_user}" if gitlab_user else f"@{target_user} (Telegram)"

        issue_title = f"[Remind] {content}"
        issue_desc = (
            f"提醒對象：{tag_str}\n"
            f"預定時間：{time_desc}\n"
            f"內容：{content}"
        )
        labels = ["Status::Inbox", "Category::Task"]

        issue = await gitlab_client.create_issue(
            title=issue_title,
            description=issue_desc,
            assignee_id=assignee_id,
            labels=labels,
            due_date=due_date
        )
        if issue:
            gitlab_issue_iid = issue.get("iid")
            gitlab_issue_url = issue.get("web_url")
    except Exception as e:
        logger.error(f"GitLab integration failed: {e}")

    # 存入資料庫
    reminder_id = await add_reminder(
        title=content[:50],
        content=content,
        assignee_tg_id=None,
        assignee_username=target_user,
        gitlab_issue_iid=gitlab_issue_iid,
        gitlab_issue_url=gitlab_issue_url,
        timing_type="once",
        next_remind_at=next_at
    )

    # 排程提醒
    from scheduler import schedule_reminder_job
    reminder = {
        "id": reminder_id,
        "assignee_username": target_user,
        "content": content,
        "timing_type": "once",
        "next_remind_at": next_at,
        "gitlab_issue_url": gitlab_issue_url,
        "gitlab_issue_iid": gitlab_issue_iid,
    }
    schedule_reminder_job(context.application, reminder)

    msg = f"✅ 已設定 @{target_user} 的提醒！\n"
    msg += f"⏰ 提醒時間：{next_at.strftime('%Y-%m-%d %H:%M')}\n"
    if gitlab_issue_url:
        msg += f"📅 GitLab Due Date: {due_date}\n"
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
        lines.append(f"⏳ ID: {r['id']} - {html.escape(r['content'])}")
        if r.get("next_remind_at"):
            next_at = r["next_remind_at"]
            if isinstance(next_at, str):
                try:
                    next_at = datetime.fromisoformat(next_at).strftime('%Y-%m-%d %H:%M')
                except: pass
            else:
                next_at = next_at.strftime('%Y-%m-%d %H:%M')
            lines.append(f"   提醒時間：{next_at}")
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

    from database import update_reminder_status
    success = await update_reminder_status(reminder_id, "done")
    if success:
        if reminder.get("gitlab_issue_iid"):
            try:
                await gitlab_client.close_issue(reminder["gitlab_issue_iid"])
            except Exception as e:
                logger.error(f"Failed to close GitLab issue: {e}")
        
        job_queue = context.application.job_queue
        if job_queue:
            for job in job_queue.get_jobs_by_name(f"remind_{reminder_id}"):
                job.schedule_removal()
        
        await update.message.reply_text(f"✅ 提醒 ID {reminder_id} 已標記為完成！")
    else:
        await update.message.reply_text(f"❌ 更新提醒狀態失敗")

def register_reminder_handlers(app, chat_filter=None):
    """註冊 reminder 相關的指令處理器"""
    app.add_handler(UnifiedCommandHandler("remind", remind_command, filters=chat_filter))
    app.add_handler(UnifiedCommandHandler("remind_list", remind_list_command, filters=chat_filter))
    app.add_handler(UnifiedCommandHandler("remind_done", remind_done_command, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(remind_day_callback, pattern=r"^remind_day:"))
    app.add_handler(CallbackQueryHandler(remind_time_callback, pattern=r"^remind_time:"))
