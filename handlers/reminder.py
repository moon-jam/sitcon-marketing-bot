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
    get_all_pending_reminders,
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

# --- Helpers ---

async def _format_remind_list_text(reminders: list, title_suffix: str) -> str:
    """格式化提醒清單文字內容"""
    if not reminders:
        return f"📋 <b>目前的提醒 ({title_suffix})：</b>\n\n（無待處理項目）"

    lines = []
    for r in reminders:
        lines.append(f"⏳ <b>@{html.escape(r['assignee_username'])}</b>: {html.escape(r['content'])}")
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
        lines.append("")
    
    content = "\n".join(lines)
    return f"📋 <b>目前的提醒 ({title_suffix})：</b>\n\n<blockquote expandable>{content}</blockquote>"

def _get_filter_keyboard(prefix: str, current_filter: str) -> list:
    """產生所有人/只有我的篩選按鈕列"""
    all_label = "👥 所有人" + (" ✅" if current_filter == "all" else "")
    me_label = "👤 只有我" + (" ✅" if current_filter == "me" else "")
    return [
        [
            InlineKeyboardButton(all_label, callback_data=f"{prefix}_filter:all"),
            InlineKeyboardButton(me_label, callback_data=f"{prefix}_filter:me"),
        ]
    ]

# --- Handlers ---

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /remind 指令"""
    if not update.message or not update.message.text:
        return

    args = extract_command_args(update.message, "remind")
    if not args:
        await update.message.reply_text("❌ 格式錯誤\n\n使用方式：/remind @username 內容")
        return

    parts = args.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text("❌ 格式錯誤\n\n使用方式：/remind @username 內容")
        return

    target_user = parts[0].lstrip("@")
    content = parts[1]

    context.user_data["remind_target"] = target_user
    context.user_data["remind_content"] = content

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
        f"🔔 正在為 @{target_user} 設定提醒：\n📝 內容：{content}\n\n📅 請選擇提醒日期：",
        reply_markup=reply_markup
    )

async def remind_day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    day_code = query.data.replace("remind_day:", "")
    now = datetime.now(TZ)
    if day_code == "mon":
        days_ahead = 7 - now.weekday()
        if days_ahead <= 0: days_ahead += 7
        target_date = now + timedelta(days=days_ahead)
    else:
        target_date = now + timedelta(days=int(day_code))
    
    context.user_data["remind_target_date"] = target_date.date().isoformat()

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
        if next_at < now:
            next_at += timedelta(days=1)
        time_desc = next_at.strftime('%Y-%m-%d %H:%M')

    gitlab_issue_iid = None
    gitlab_issue_url = None
    due_date = next_at.strftime('%Y-%m-%d')

    try:
        assignee_id = await gitlab_client.get_gitlab_user_id(target_user)
        gitlab_user = await gitlab_client.get_gitlab_username(target_user)
        tag_str = f"@{gitlab_user}" if gitlab_user else f"@{target_user} (Telegram)"

        issue_title = f"[Remind] {content}"
        issue_desc = f"提醒對象：{tag_str}\\\n預定時間：{time_desc}\\\n內容：{content}"
        labels = ["Status::Inbox", "Category::Task"]

        issue = await gitlab_client.create_issue(
            title=issue_title, description=issue_desc, assignee_id=assignee_id, labels=labels, due_date=due_date
        )
        if issue:
            gitlab_issue_iid = issue.get("iid")
            gitlab_issue_url = issue.get("web_url")
    except Exception as e:
        logger.error(f"GitLab integration failed: {e}")

    reminder_id = await add_reminder(
        title=content[:50], content=content, assignee_tg_id=None, assignee_username=target_user,
        gitlab_issue_iid=gitlab_issue_iid, gitlab_issue_url=gitlab_issue_url, timing_type="once", next_remind_at=next_at
    )

    from scheduler import schedule_reminder_job
    schedule_reminder_job(context.application, {
        "id": reminder_id, "assignee_username": target_user, "content": content,
        "timing_type": "once", "next_remind_at": next_at, "gitlab_issue_url": gitlab_issue_url, "gitlab_issue_iid": gitlab_issue_iid,
    })

    msg = f"✅ 已設定 @{target_user} 的提醒！\n⏰ 提醒時間：{next_at.strftime('%Y-%m-%d %H:%M')}\n"
    if gitlab_issue_url:
        msg += f"📅 GitLab Due Date: {due_date}\n<a href=\"{gitlab_issue_url}\">GitLab Issue: #{gitlab_issue_iid}</a>"
    
    await query.edit_message_text(msg, parse_mode="HTML")

async def remind_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /remind_list 指令"""
    if not update.message: return
    
    # 預設顯示只有我
    user = update.message.from_user
    username = user.username or str(user.id)
    reminders = await get_pending_reminders_by_username(username)
    
    text = await _format_remind_list_text(reminders, "只有我")
    reply_markup = InlineKeyboardMarkup(_get_filter_keyboard("remind_list", "me"))
    
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")

async def remind_list_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /remind_list 的篩選切換"""
    query = update.callback_query
    await query.answer()
    
    filter_type = query.data.replace("remind_list_filter:", "")
    user = query.from_user
    username = user.username or str(user.id)
    
    if filter_type == "me":
        reminders = await get_pending_reminders_by_username(username)
        text = await _format_remind_list_text(reminders, "只有我")
    else:
        reminders = await get_all_pending_reminders()
        text = await _format_remind_list_text(reminders, "所有人")
        
    reply_markup = InlineKeyboardMarkup(_get_filter_keyboard("remind_list", filter_type))
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")

async def remind_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /remind_done 指令"""
    if not update.message: return
    
    # 預設顯示只有我
    user = update.message.from_user
    username = user.username or str(user.id)
    reminders = await get_pending_reminders_by_username(username)
    
    keyboard = _get_filter_keyboard("remind_done", "me")
    
    for r in reminders:
        label = f"✅ @{r['assignee_username']}: {r['content'][:20]}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"remind_done_act:{r['id']}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📋 請選擇要完成的提醒 (只有我)：", reply_markup=reply_markup)

async def remind_done_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /remind_done 的篩選切換"""
    query = update.callback_query
    await query.answer()
    
    filter_type = query.data.replace("remind_done_filter:", "")
    user = query.from_user
    username = user.username or str(user.id)
    
    if filter_type == "me":
        reminders = await get_pending_reminders_by_username(username)
        title = "📋 請選擇要完成的提醒 (只有我)："
    else:
        reminders = await get_all_pending_reminders()
        title = "📋 請選擇要完成的提醒 (所有人)："
        
    keyboard = _get_filter_keyboard("remind_done", filter_type)
    for r in reminders:
        label = f"✅ @{r['assignee_username']}: {r['content'][:20]}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"remind_done_act:{r['id']}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(title, reply_markup=reply_markup)

async def remind_done_act_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理標記為完成的實際動作"""
    query = update.callback_query
    await query.answer()

    reminder_id = int(query.data.replace("remind_done_act:", ""))
    reminder = await get_reminder_by_id(reminder_id)

    if not reminder:
        await query.edit_message_text("❌ 找不到該提醒")
        return

    success = await update_reminder_status(reminder_id, "done")
    if success:
        if reminder.get("gitlab_issue_iid"):
            try: await gitlab_client.close_issue(reminder["gitlab_issue_iid"])
            except Exception as e: logger.error(f"Failed to close GitLab issue: {e}")
        
        if context.application.job_queue:
            for job in context.application.job_queue.get_jobs_by_name(f"remind_{reminder_id}"):
                job.schedule_removal()
        
        await query.edit_message_text(f"✅ 提醒「{reminder['content'][:20]}...」已標記為完成！")
    else:
        await query.edit_message_text("❌ 更新提醒狀態失敗")

def register_reminder_handlers(app, chat_filter=None):
    """註冊 reminder 相關的指令處理器"""
    app.add_handler(UnifiedCommandHandler("remind", remind_command, filters=chat_filter))
    app.add_handler(UnifiedCommandHandler("remind_list", remind_list_command, filters=chat_filter))
    app.add_handler(UnifiedCommandHandler("remind_done", remind_done_command, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(remind_day_callback, pattern=r"^remind_day:"))
    app.add_handler(CallbackQueryHandler(remind_time_callback, pattern=r"^remind_time:"))
    app.add_handler(CallbackQueryHandler(remind_list_filter_callback, pattern=r"^remind_list_filter:"))
    app.add_handler(CallbackQueryHandler(remind_done_filter_callback, pattern=r"^remind_done_filter:"))
    app.add_handler(CallbackQueryHandler(remind_done_act_callback, pattern=r"^remind_done_act:"))
