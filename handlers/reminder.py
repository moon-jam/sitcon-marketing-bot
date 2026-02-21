import html
import logging
import calendar
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
    get_and_clear_bot_messages,
    track_bot_message,
)
from handlers.gitlab_client import gitlab_client
from handlers.utils import (
    extract_command_args,
    UnifiedCommandHandler,
)

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Taipei")

# --- Helpers ---

async def _reply_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, msg_type: str, reply_markup=None, parse_mode=None):
    """發送訊息並追蹤，同時刪除舊訊息以防洗版"""
    chat_id = update.effective_chat.id if update.effective_chat else None
    
    if chat_id:
        old_msg_ids = await get_and_clear_bot_messages(chat_id, msg_type)
        for msg_id in old_msg_ids:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    
    if chat_id:
        await track_bot_message(chat_id, msg.message_id, msg_type)
    return msg

def _get_date_label(target_date: datetime) -> str:
    """取得日期的友好標籤"""
    now = datetime.now(TZ).date()
    diff = (target_date.date() - now).days
    if diff == 0: return "今天"
    if diff == 1: return "明天"
    if diff == 2: return "後天"
    weekday_names = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    return f"{target_date.strftime('%m/%d')} ({weekday_names[target_date.weekday()]})"

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

def _get_time_stepper_keyboard(hour: int, minute: int) -> InlineKeyboardMarkup:
    """產生時間微調器按鈕"""
    keyboard = [
        [
            InlineKeyboardButton("H +1", callback_data="remind_time:adj_h:1"),
            InlineKeyboardButton("H +4", callback_data="remind_time:adj_h:4"),
            InlineKeyboardButton("M +10", callback_data="remind_time:adj_m:10"),
            InlineKeyboardButton("M +30", callback_data="remind_time:adj_m:30"),
        ],
        [
            InlineKeyboardButton(f"⏰ {hour:02d}:{minute:02d}", callback_data="ignore"),
        ],
        [
            InlineKeyboardButton("H -1", callback_data="remind_time:adj_h:-1"),
            InlineKeyboardButton("H -4", callback_data="remind_time:adj_h:-4"),
            InlineKeyboardButton("M -10", callback_data="remind_time:adj_m:-10"),
            InlineKeyboardButton("M -30", callback_data="remind_time:adj_m:-30"),
        ],
        [
            InlineKeyboardButton("✅ 確認時間", callback_data="remind_time:stepper_confirm"),
            InlineKeyboardButton("⬅️ 返回預設", callback_data="remind_time:stepper_back"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def _parse_inline_datetime(text: str) -> tuple[datetime | None, str]:
    """
    嘗試從文字尾端解析日期時間。
    支援格式：
        - 2026-02-15 14:00  (完整)
        - 2/15 14:00        (月/日 時:分)
        - 2-15 14:00        (月-日 時:分)
        - 2/15              (只有日期，預設 09:00)
        - 14:00             (只有時間，預設今天)
    回傳 (解析後的 datetime, 剩餘的內容文字)
    找不到就回傳 (None, 原始文字)
    """
    import re
    now = datetime.now(TZ)

    patterns = [
        # 完整格式：2026-02-15 14:00 or 2026/02/15 14:00
        (r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})$',
         lambda m: datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)), tzinfo=TZ)),
        # 月/日 時:分：2/15 14:00
        (r'(\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})$',
         lambda m: datetime(now.year, int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), int(m.group(4)), tzinfo=TZ)),
        # 只有日期：2/15 or 2-15
        (r'(\d{1,2})[/-](\d{1,2})$',
         lambda m: datetime(now.year, int(m.group(1)), int(m.group(2)),
                            9, 0, tzinfo=TZ)),
        # 只有時間：14:00
        (r'(\d{1,2}):(\d{2})$',
         lambda m: datetime(now.year, now.month, now.day,
                            int(m.group(1)), int(m.group(2)), tzinfo=TZ)),
    ]

    stripped = text.rstrip()
    for pattern, builder in patterns:
        match = re.search(pattern, stripped)
        if match:
            try:
                dt = builder(match)
                # 如果時間已過且只指定了時間，改為明天
                if dt < now and pattern == patterns[-1][0]:
                    dt += timedelta(days=1)
                # 如果只指定日期且年份的月份已過，改為明年
                if dt < now and pattern == patterns[2][0]:
                    dt = dt.replace(year=dt.year + 1)
                content = stripped[:match.start()].rstrip()
                if content:  # 確保還有剩餘內容
                    return dt, content
            except (ValueError, OverflowError):
                continue

    return None, text


async def _create_reminder_direct(update: Update, context, target_user: str, content: str, next_at: datetime):
    """直接建立提醒（跳過互動式選擇）"""
    time_desc = next_at.strftime('%Y-%m-%d %H:%M')
    due_date = next_at.strftime('%Y-%m-%d')

    # GitLab 開卡
    gitlab_issue_iid = None
    gitlab_issue_url = None
    try:
        assignee_id = await gitlab_client.get_gitlab_user_id(target_user)
        gitlab_user = await gitlab_client.get_gitlab_username(target_user)
        tag_str = f"@{gitlab_user}" if gitlab_user else f"@{target_user} (Telegram)"
        issue_desc = f"提醒對象：{tag_str}\\\\\\n預定時間：{time_desc}\\\\\\n內容：{content}"
        issue = await gitlab_client.create_issue(
            title=f"[Remind] {content}", description=issue_desc,
            assignee_id=assignee_id, labels=["Status::Inbox", "Category::Task"], due_date=due_date
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

    msg = f"✅ 已設定 @{target_user} 的提醒！\n📝 內容：{content}\n⏰ 提醒時間：{time_desc}\n"
    if gitlab_issue_url:
        msg += f"📅 GitLab Due Date: {due_date}\n<a href=\"{gitlab_issue_url}\">GitLab Issue: #{gitlab_issue_iid}</a>"
    await _reply_and_track(update, context, msg, "remind_cmd", parse_mode="HTML")

# --- Handlers ---

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /remind 指令 - 第一步：選擇日期（或直接指定時間）"""
    if not update.message or not update.message.text: return

    args = extract_command_args(update.message, "remind")
    if not args:
        await _reply_and_track(
            update, context,
            "❌ 格式錯誤\n\n"
            "使用方式：\n"
            "• /remind 內容（提醒自己）\n"
            "• /remind @username 內容\n"
            "• /remind 內容 2/15 14:00\n"
            "• /remind @username 內容 14:00",
            "remind_cmd"
        )
        return

    parts = args.split(None, 1)

    if parts[0].startswith("@"):
        if len(parts) < 2:
            await _reply_and_track(
                update, context,
                "❌ 格式錯誤\n\n"
                "使用方式：/remind @username 內容",
                "remind_cmd"
            )
            return
        target_user = parts[0].lstrip("@")
        raw_content = parts[1]
    else:
        # 沒有 @ → 提醒自己
        user = update.message.from_user
        target_user = user.username or str(user.id)
        raw_content = args

    # 嘗試從內容尾端解析日期時間
    parsed_time, content = _parse_inline_datetime(raw_content)

    if parsed_time:
        # 直接建立提醒，跳過互動式選擇
        context.user_data["remind_target"] = target_user
        context.user_data["remind_content"] = content
        await _create_reminder_direct(update, context, target_user, content, parsed_time)
        return

    # 沒有指定時間 → 走互動式日曆流程
    context.user_data["remind_target"] = target_user
    context.user_data["remind_content"] = raw_content

    # 日期選單：快捷按鈕 + 自訂日期
    keyboard = [
        [
            InlineKeyboardButton("今天", callback_data="remind_day:0"),
            InlineKeyboardButton("明天", callback_data="remind_day:1"),
            InlineKeyboardButton("後天", callback_data="remind_day:2"),
        ],
        [
            InlineKeyboardButton("📅 選擇其它日期 (月份)", callback_data="remind_month_picker"),
        ]
    ]

    await _reply_and_track(
        update, context,
        f"🔔 正在為 @{target_user} 設定提醒：\n📝 內容：{raw_content}\n\n📅 請選擇提醒日期：",
        "remind_cmd",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def remind_month_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示月份選擇器"""
    query = update.callback_query
    await query.answer()
    
    now = datetime.now(TZ)
    keyboard = []
    # 顯示目前月份及接下來的五個月 (共六個月)
    for i in range(6):
        target_month = now.replace(day=1) + timedelta(days=i*31) # 粗略估計
        # 修正為正確的月初
        month_start = datetime(target_month.year, target_month.month, 1, tzinfo=TZ)
        label = month_start.strftime("%Y年 %m月")
        keyboard.append([InlineKeyboardButton(label, callback_data=f"remind_month:{month_start.strftime('%Y-%m')} ")])
    
    keyboard.append([InlineKeyboardButton("⬅️ 返回快捷日期", callback_data="remind_day_back")])
    await query.edit_message_text("📅 請選擇月份：", reply_markup=InlineKeyboardMarkup(keyboard))

async def remind_day_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """返回快捷日期選單"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [
            InlineKeyboardButton("今天", callback_data="remind_day:0"),
            InlineKeyboardButton("明天", callback_data="remind_day:1"),
            InlineKeyboardButton("後天", callback_data="remind_day:2"),
        ],
        [
            InlineKeyboardButton("📅 選擇其它日期 (月份)", callback_data="remind_month_picker"),
        ]
    ]
    await query.edit_message_text(f"📅 請選擇提醒日期：", reply_markup=InlineKeyboardMarkup(keyboard))

async def remind_month_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示特定月份的日期選擇器 (對齊星期幾)"""
    query = update.callback_query
    await query.answer()
    
    year_month = query.data.replace("remind_month:", "").strip()
    year, month = map(int, year_month.split("-"))
    
    # monthrange 回傳 (該月第一天是星期幾, 該月天數)
    # 注意：0=週一, ..., 6=週日
    first_weekday_mon, num_days = calendar.monthrange(year, month)
    
    # 轉換為 0=週日, 1=週一... 模式
    first_weekday_sun = (first_weekday_mon + 1) % 7
    
    keyboard = []
    # 星期標籤 (週日開始)
    keyboard.append([
        InlineKeyboardButton(w, callback_data="ignore") 
        for w in ["日", "一", "二", "三", "四", "五", "六"]
    ])

    row = []
    # 補足第一週前面的空白 (以週日為起始)
    for _ in range(first_weekday_sun):
        row.append(InlineKeyboardButton(" ", callback_data="ignore"))

    # 填入日期
    for day in range(1, num_days + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        row.append(InlineKeyboardButton(str(day), callback_data=f"remind_day:date:{date_str}"))
        if len(row) == 7:
            keyboard.append(row)
            row = []
    
    # 補足最後一週後面的空白
    if row:
        while len(row) < 7:
            row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("⬅️ 返回選擇月份", callback_data="remind_month_picker")])
    await query.edit_message_text(f"📅 請選擇 {year}年{month}月 的日期：", reply_markup=InlineKeyboardMarkup(keyboard))

async def remind_day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """第二步：選擇時間 (快速選項)"""
    query = update.callback_query
    await query.answer()

    data = query.data.replace("remind_day:", "")
    now = datetime.now(TZ)
    
    if data.startswith("date:"):
        date_str = data.replace("date:", "")
        target_date = datetime.fromisoformat(date_str).replace(tzinfo=TZ)
    else:
        day_code = int(data)
        target_date = now + timedelta(days=day_code)
    
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
        ],
        [
            InlineKeyboardButton("✨ 自訂精確時間 (步進器)", callback_data="remind_time:stepper_init"),
        ]
    ]
    
    date_display = target_date.strftime('%Y-%m-%d')
    text = f"⏰ 請選擇 {date_display} 的提醒時間："
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def remind_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理各種時間選擇（快速、相對、步進器）"""
    query = update.callback_query
    data = query.data.replace("remind_time:", "")

    # 1. 初始化步進器
    if data == "stepper_init":
        await query.answer()
        context.user_data["remind_h"] = 9
        context.user_data["remind_m"] = 0
        await query.edit_message_reply_markup(reply_markup=_get_time_stepper_keyboard(9, 0))
        return

    # 2. 返回預設選單
    if data == "stepper_back":
        await query.answer()
        date_str = context.user_data["remind_target_date"]
        target_date = datetime.fromisoformat(date_str)
        keyboard = [
            [InlineKeyboardButton("早上 09:00", callback_data="remind_time:09:00"), InlineKeyboardButton("中午 12:00", callback_data="remind_time:12:00")],
            [InlineKeyboardButton("下午 15:00", callback_data="remind_time:15:00"), InlineKeyboardButton("晚上 18:00", callback_data="remind_time:18:00")],
            [InlineKeyboardButton("深夜 21:00", callback_data="remind_time:21:00"), InlineKeyboardButton("自訂 (1小時後)", callback_data="remind_time:relative_60")],
            [InlineKeyboardButton("✨ 自訂精確時間 (步進器)", callback_data="remind_time:stepper_init")]
        ]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # 3. 步進器調整
    if data.startswith("adj_"):
        await query.answer()
        adj_part, val = data.split(":")
        unit = adj_part.split("_")[1]  # "adj_h" -> "h", "adj_m" -> "m"
        val = int(val)
        if unit == "h":
            context.user_data["remind_h"] = (context.user_data.get("remind_h", 9) + val) % 24
        else:
            context.user_data["remind_m"] = (context.user_data.get("remind_m", 0) + val) % 60
        
        await query.edit_message_reply_markup(
            reply_markup=_get_time_stepper_keyboard(context.user_data["remind_h"], context.user_data["remind_m"])
        )
        return

    # 4. 確認時間
    await query.answer()
    now = datetime.now(TZ)
    target_date_str = context.user_data.get("remind_target_date")
    target_user = context.user_data.pop("remind_target", None)
    content = context.user_data.pop("remind_content", None)

    if not target_user or not content:
        await query.edit_message_text("❌ 錯誤：找不到提醒資訊，請重新輸入指令")
        return

    next_at = None
    if data == "stepper_confirm":
        h = context.user_data.pop("remind_h", 9)
        m = context.user_data.pop("remind_m", 0)
        target_date = datetime.fromisoformat(target_date_str).date()
        next_at = datetime.combine(target_date, time(h, m)).replace(tzinfo=TZ)
    elif data.startswith("relative_"):
        minutes = int(data.replace("relative_", ""))
        next_at = now + timedelta(minutes=minutes)
    else:
        h, m = map(int, data.split(":"))
        target_date = datetime.fromisoformat(target_date_str).date()
        next_at = datetime.combine(target_date, time(h, m)).replace(tzinfo=TZ)

    if next_at < now: next_at += timedelta(days=1)
    time_desc = next_at.strftime('%Y-%m-%d %H:%M')
    due_date = next_at.strftime('%Y-%m-%d')

    # GitLab 開卡
    gitlab_issue_iid = None
    gitlab_issue_url = None
    try:
        assignee_id = await gitlab_client.get_gitlab_user_id(target_user)
        gitlab_user = await gitlab_client.get_gitlab_username(target_user)
        tag_str = f"@{gitlab_user}" if gitlab_user else f"@{target_user} (Telegram)"
        issue_desc = f"提醒對象：{tag_str}\\\n預定時間：{time_desc}\\\n內容：{content}"
        issue = await gitlab_client.create_issue(
            title=f"[Remind] {content}", description=issue_desc, 
            assignee_id=assignee_id, labels=["Status::Inbox", "Category::Task"], due_date=due_date
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

    msg = f"✅ 已設定 @{target_user} 的提醒！\n⏰ 提醒時間：{time_desc}\n"
    if gitlab_issue_url:
        msg += f"📅 GitLab Due Date: {due_date}\n<a href=\"{gitlab_issue_url}\">GitLab Issue: #{gitlab_issue_iid}</a>"
    await query.edit_message_text(msg, parse_mode="HTML")

async def remind_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /remind_list 指令"""
    if not update.message: return
    user = update.message.from_user
    username = user.username or str(user.id)
    reminders = await get_pending_reminders_by_username(username)
    text = await _format_remind_list_text(reminders, "只有我")
    await _reply_and_track(
        update, context, text, "remind_list_cmd",
        reply_markup=InlineKeyboardMarkup(_get_filter_keyboard("remind_list", "me")),
        parse_mode="HTML"
    )

async def remind_list_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    filter_type = query.data.replace("remind_list_filter:", "")
    user = query.from_user
    username = user.username or str(user.id)
    reminders = await (get_pending_reminders_by_username(username) if filter_type == "me" else get_all_pending_reminders())
    text = await _format_remind_list_text(reminders, "只有我" if filter_type == "me" else "所有人")
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(_get_filter_keyboard("remind_list", filter_type)), parse_mode="HTML")

async def remind_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user = update.message.from_user
    username = user.username or str(user.id)
    reminders = await get_pending_reminders_by_username(username)
    keyboard = _get_filter_keyboard("remind_done", "me")
    for r in reminders:
        keyboard.append([InlineKeyboardButton(f"✅ @{r['assignee_username']}: {r['content'][:20]}", callback_data=f"remind_done_act:{r['id']}")])
    await _reply_and_track(
        update, context, "📋 請選擇要完成的提醒 (只有我)：", "remind_done_cmd",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def remind_done_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    filter_type = query.data.replace("remind_done_filter:", "")
    user = query.from_user
    username = user.username or str(user.id)
    reminders = await (get_pending_reminders_by_username(username) if filter_type == "me" else get_all_pending_reminders())
    keyboard = _get_filter_keyboard("remind_done", filter_type)
    for r in reminders:
        keyboard.append([InlineKeyboardButton(f"✅ @{r['assignee_username']}: {r['content'][:20]}", callback_data=f"remind_done_act:{r['id']}")])
    await query.edit_message_text(f"📋 請選擇要完成的提醒 ({'只有我' if filter_type == 'me' else '所有人'})：", reply_markup=InlineKeyboardMarkup(keyboard))

async def remind_done_act_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    reminder_id = int(query.data.replace("remind_done_act:", ""))
    reminder = await get_reminder_by_id(reminder_id)
    if not reminder: return
    if await update_reminder_status(reminder_id, "done"):
        if reminder.get("gitlab_issue_iid"):
            try: await gitlab_client.close_issue(reminder["gitlab_issue_iid"])
            except Exception as e: logger.error(f"Failed to close GitLab issue: {e}")
        if context.application.job_queue:
            for job in context.application.job_queue.get_jobs_by_name(f"remind_{reminder_id}"): job.schedule_removal()
        await query.edit_message_text(f"✅ 提醒「{reminder['content'][:20]}...」已標記為完成！")

async def daily_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /daily_summary 指令 - 手動觸發每日摘要"""
    if not update.message: return
    from scheduler import send_daily_summary
    from handlers.utils import get_allowed_chat_ids

    chat_ids = get_allowed_chat_ids()
    if not chat_ids and update.effective_chat:
        chat_ids = [update.effective_chat.id]

    sent = await send_daily_summary(context.bot, chat_ids)
    if not sent:
        await _reply_and_track(update, context, "📋 目前沒有任何待處理事項！", "daily_summary_cmd")

def register_reminder_handlers(app, chat_filter=None):
    app.add_handler(UnifiedCommandHandler("remind", remind_command, filters=chat_filter))
    app.add_handler(UnifiedCommandHandler("remind_list", remind_list_command, filters=chat_filter))
    app.add_handler(UnifiedCommandHandler("remind_done", remind_done_command, filters=chat_filter))
    app.add_handler(UnifiedCommandHandler("daily_summary", daily_summary_command, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(remind_day_callback, pattern=r"^remind_day:"))
    app.add_handler(CallbackQueryHandler(remind_day_back_callback, pattern=r"^remind_day_back$"))
    app.add_handler(CallbackQueryHandler(remind_month_picker_callback, pattern=r"^remind_month_picker$"))
    app.add_handler(CallbackQueryHandler(remind_month_callback, pattern=r"^remind_month:"))
    app.add_handler(CallbackQueryHandler(remind_time_callback, pattern=r"^remind_time:"))
    app.add_handler(CallbackQueryHandler(remind_list_filter_callback, pattern=r"^remind_list_filter:"))
    app.add_handler(CallbackQueryHandler(remind_done_filter_callback, pattern=r"^remind_done_filter:"))
    app.add_handler(CallbackQueryHandler(remind_done_act_callback, pattern=r"^remind_done_act:"))
