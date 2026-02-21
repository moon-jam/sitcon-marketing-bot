"""
排程提醒功能
- 提醒 reviewers 審核 pending reviews（週期由 .env 設定）
- 提醒 submitters 修改 need_fix reviews（週期由 .env 設定）
- 每日摘要通知（每天早上定時發送待處理事項）
"""

import html
import logging
import os
from datetime import datetime, time, timedelta

from zoneinfo import ZoneInfo
from telegram import Bot
from telegram.ext import Application

from database import (
    get_pending_reviews,
    get_need_fix_reviews,
    get_all_reviewers,
    get_active_reminders,
    get_all_pending_reminders,
    update_next_remind_at,
    update_reminder_status,
    update_review_status,
    get_reminder_by_id,
    ReviewStatus,
)

logger = logging.getLogger(__name__)

# 預設提醒週期（分鐘）
DEFAULT_INTERVAL_PENDING = 60  # 每小時
DEFAULT_INTERVAL_NEED_FIX = 120  # 每兩小時

# 時區
TZ = ZoneInfo("Asia/Taipei")


def get_reminder_interval(env_key: str, default: int) -> int:
    """從環境變數取得提醒週期（分鐘）"""
    interval_str = os.getenv(env_key, "")
    if not interval_str:
        return default

    try:
        interval = int(interval_str.strip())
        if interval > 0:
            return interval
        else:
            logger.warning(
                f"Invalid interval in {env_key}: {interval}, using default {default}"
            )
            return default
    except ValueError:
        logger.warning(
            f"Invalid interval in {env_key}: {interval_str}, using default {default}"
        )
        return default


def _parse_time(time_str: str) -> time | None:
    """解析 HH:MM 格式時間字串"""
    try:
        parts = time_str.strip().split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def is_quiet_hours() -> bool:
    """
    檢查現在是否在免打擾時段。
    讀取 QUIET_HOURS_START / QUIET_HOURS_END（HH:MM，Asia/Taipei）。
    支援跨午夜（例如 22:00-08:00）。
    """
    start_str = os.getenv("QUIET_HOURS_START", "")
    end_str = os.getenv("QUIET_HOURS_END", "")
    if not start_str or not end_str:
        return False

    start = _parse_time(start_str)
    end = _parse_time(end_str)
    if start is None or end is None:
        logger.warning(
            f"Invalid QUIET_HOURS format: start={start_str}, end={end_str}"
        )
        return False

    now = datetime.now(TZ).time()

    if start <= end:
        # 同一天內，例如 09:00-18:00
        return start <= now < end
    else:
        # 跨午夜，例如 22:00-08:00
        return now >= start or now < end


async def send_pending_review_notification(bot: Bot, chat_ids: list[int]) -> bool:
    """
    發送待審核通知給 reviewers
    回傳是否有發送（有 pending reviews 且有 reviewers）
    """
    pending_reviews = await get_pending_reviews()
    if not pending_reviews:
        logger.info("No pending reviews to notify")
        return False

    reviewers = await get_all_reviewers()
    if not reviewers:
        logger.warning("No reviewers configured, skipping notification")
        return False

    # 建立提醒訊息
    reviewer_mentions = " ".join([f"@{html.escape(u)}" for u in reviewers])
    review_lines = []
    for r in pending_reviews:
        line = f"• {html.escape(r['sponsor_name'])} - {html.escape(r['link'])}"
        if r.get("gitlab_issue_url"):
            line += f" (<a href=\"{r['gitlab_issue_url']}\">GitLab #{r['gitlab_issue_iid']}</a>)"
        review_lines.append(line)
    
    review_list = "\n".join(review_lines)

    message = (
        f"📢 Review 提醒\n\n"
        f"{reviewer_mentions}\n\n"
        f"以下項目等待審核：\n"
        f"<blockquote expandable>{review_list}</blockquote>\n"
        f"請使用 /review_list 查看詳細資訊"
    )

    # 發送到所有允許的聊天室
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
            logger.info(f"Sent pending review notification to chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send notification to chat {chat_id}: {e}")

    return True


async def send_need_fix_notification(bot: Bot, chat_ids: list[int]) -> bool:
    """
    發送待修改通知給 submitters
    回傳是否有發送（有 need_fix reviews）
    """
    need_fix_reviews = await get_need_fix_reviews()
    if not need_fix_reviews:
        logger.info("No need-fix reviews to notify")
        return False

    # 按 submitter 分組
    by_submitter = {}
    for r in need_fix_reviews:
        submitter = r.get("submitter_username", "unknown")
        if submitter not in by_submitter:
            by_submitter[submitter] = []
        by_submitter[submitter].append(r)

    # 建立提醒訊息
    detail_lines = []
    for submitter, reviews in by_submitter.items():
        detail_lines.append(f"@{html.escape(submitter)} 請修改：")
        for r in reviews:
            line = f"  • {html.escape(r['sponsor_name'])} - {html.escape(r['link'])}"
            if r.get("gitlab_issue_url"):
                line += f" (<a href=\"{r['gitlab_issue_url']}\">GitLab #{r['gitlab_issue_iid']}</a>)"
            detail_lines.append(line)
            if r.get("comment"):
                detail_lines.append(f"    💬 {html.escape(r['comment'])}")
        detail_lines.append("")

    details = "\n".join(detail_lines)
    message = (
        f"📢 修改提醒\n\n"
        f"<blockquote expandable>{details}</blockquote>\n"
        f"修改完成後請使用 /review_again 重新送審"
    )

    # 發送到所有允許的聊天室
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
            logger.info(f"Sent need-fix notification to chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send notification to chat {chat_id}: {e}")

    return True


async def notify_submitter_approved(
    bot: Bot, chat_id: int, sponsor_name: str, submitter_username: str
):
    """通知提交者審核已通過"""
    message = (
        f"✅ 審核通過通知\n\n"
        f"@{submitter_username} 您提交的「{sponsor_name}」已審核通過！"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=message)
    except Exception as e:
        logger.error(f"Failed to notify submitter: {e}")


async def notify_submitter_need_fix(
    bot: Bot,
    chat_id: int,
    sponsor_name: str,
    submitter_username: str,
    link: str,
    comment: str = None,
    gitlab_issue_url: str = None,
    gitlab_issue_iid: int = None,
):
    """通知提交者需要修改"""
    message = (
        f"🔧 修改通知\n\n"
        f"@{html.escape(submitter_username)} 您提交的「{html.escape(sponsor_name)}」需要修改\n"
        f"連結：{html.escape(link)}"
    )
    if gitlab_issue_url:
        message += f"\nGitLab：<a href=\"{gitlab_issue_url}\">#{gitlab_issue_iid}</a>"
    
    if comment:
        message += f"\n💬 評語：{html.escape(comment)}"
    message += "\n\n修改完成後請使用 /review_again 重新送審"
    try:
        await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to notify submitter: {e}")


async def remind_pending_reviews(context):
    """排程任務：提醒 reviewers 審核待處理的 reviews"""
    if is_quiet_hours():
        logger.info("Skipping pending review reminder (quiet hours)")
        return
    chat_ids = context.job.data.get("chat_ids", [])
    await send_pending_review_notification(context.bot, chat_ids)


async def remind_need_fix_reviews(context):
    """排程任務：提醒 submitters 修改需要修改的 reviews"""
    if is_quiet_hours():
        logger.info("Skipping need-fix review reminder (quiet hours)")
        return
    chat_ids = context.job.data.get("chat_ids", [])
    await send_need_fix_notification(context.bot, chat_ids)


async def build_daily_summary_message() -> str | None:
    """建構每日摘要訊息，按時間區間與負責人分組"""
    from handlers.gitlab_client import gitlab_client

    now = datetime.now(TZ)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    # 本週結束：本週日 23:59
    days_until_sunday = 6 - today.weekday()  # weekday: 0=Mon, 6=Sun
    week_end = today + timedelta(days=days_until_sunday)

    def _resolve_tg(username: str) -> str:
        """將 username 反向映射為 Telegram username"""
        return gitlab_client.get_telegram_username(username)

    # --- 收集提醒 ---
    reminders = await get_all_pending_reminders()
    buckets = {
        "overdue": [],   # 已過期
        "today": [],     # 今天
        "tomorrow": [],  # 明天
        "week": [],      # 本週其餘
        "later": [],     # 更之後
    }

    for r in reminders:
        next_at = r.get("next_remind_at")
        if not next_at:
            buckets["later"].append(r)
            continue
        if isinstance(next_at, str):
            try:
                next_at = datetime.fromisoformat(next_at)
            except ValueError:
                buckets["later"].append(r)
                continue
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=TZ)
        remind_date = next_at.date()

        if next_at < now:
            buckets["overdue"].append(r)
        elif remind_date == today:
            buckets["today"].append(r)
        elif remind_date == tomorrow:
            buckets["tomorrow"].append(r)
        elif remind_date <= week_end:
            buckets["week"].append(r)
        else:
            buckets["later"].append(r)

    # --- 收集 Reviews ---
    pending_reviews = await get_pending_reviews()
    need_fix_reviews = await get_need_fix_reviews()

    # 如果完全沒事項就不發
    has_reminders = any(buckets[k] for k in ["overdue", "today", "tomorrow", "week"])
    if not has_reminders and not pending_reviews and not need_fix_reviews:
        return None

    # --- 格式化函式 ---
    def _format_reminder_section(title: str, items: list) -> str:
        if not items:
            return ""
        # 按負責人分組
        by_user = {}
        for r in items:
            user = _resolve_tg(r.get("assignee_username") or "未指定")
            by_user.setdefault(user, []).append(r)

        lines = []
        for user, user_items in by_user.items():
            for item in user_items:
                content = html.escape(item.get("content") or item.get("title") or "（無內容）")
                next_at = item.get("next_remind_at")
                time_str = ""
                if next_at:
                    if isinstance(next_at, str):
                        try:
                            next_at = datetime.fromisoformat(next_at)
                        except ValueError:
                            pass
                    if isinstance(next_at, datetime):
                        time_str = f" 🕐{next_at.strftime('%m/%d %H:%M')}"
                line = f"• @{html.escape(user)}: {content}{time_str}"
                if item.get("gitlab_issue_url"):
                    line += f' (<a href="{item["gitlab_issue_url"]}">#{item["gitlab_issue_iid"]}</a>)'
                lines.append(line)

        content_text = "\n".join(lines)
        return f"\n<b>{title}</b>\n<blockquote expandable>{content_text}</blockquote>"

    # --- 組合訊息 ---
    weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
    header = f"☀️ <b>每日摘要</b> — {now.strftime('%m/%d')} 週{weekday_names[today.weekday()]}\n"

    parts = [header]
    parts.append(_format_reminder_section("🚨 已過期", buckets["overdue"]))
    parts.append(_format_reminder_section("📌 今天", buckets["today"]))
    parts.append(_format_reminder_section("📅 明天", buckets["tomorrow"]))
    parts.append(_format_reminder_section("🗓️ 本週", buckets["week"]))

    # Reviews
    if pending_reviews:
        review_lines = []
        for r in pending_reviews:
            tg_user = html.escape(_resolve_tg(r.get('submitter_username', '?')))
            line = f"• {html.escape(r['sponsor_name'])} (@{tg_user})"
            if r.get("gitlab_issue_url"):
                line += f' (<a href="{r["gitlab_issue_url"]}">#{r["gitlab_issue_iid"]}</a>)'
            review_lines.append(line)
        parts.append(f'\n<b>📝 待審核 Review ({len(pending_reviews)})</b>\n<blockquote expandable>{chr(10).join(review_lines)}</blockquote>')

    if need_fix_reviews:
        fix_lines = []
        for r in need_fix_reviews:
            tg_user = html.escape(_resolve_tg(r.get('submitter_username', '?')))
            line = f"• {html.escape(r['sponsor_name'])} (@{tg_user})"
            if r.get("comment"):
                line += f" 💬 {html.escape(r['comment'])}"
            fix_lines.append(line)
        parts.append(f'\n<b>🔧 待修改 Review ({len(need_fix_reviews)})</b>\n<blockquote expandable>{chr(10).join(fix_lines)}</blockquote>')

    return "\n".join(p for p in parts if p)


async def send_daily_summary(bot: Bot, chat_ids: list[int]) -> bool:
    """發送每日摘要通知，回傳是否有發送"""
    message = await build_daily_summary_message()
    if not message:
        logger.info("No items for daily summary, skipping")
        return False

    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
            logger.info(f"Sent daily summary to chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send daily summary to chat {chat_id}: {e}")
    return True


async def _daily_summary_job(context):
    """排程任務：每日摘要"""
    chat_ids = context.job.data.get("chat_ids", [])
    await send_daily_summary(context.bot, chat_ids)


async def sync_gitlab_issues(bot: Bot = None, chat_ids: list[int] = None):
    """同步 GitLab issue 狀態：如果 issue 已關閉，就更新資料庫"""
    from handlers.gitlab_client import gitlab_client

    # 收集所有有 GitLab IID 的待處理項目
    reminders = await get_all_pending_reminders()
    pending_reviews = await get_pending_reviews()
    need_fix_reviews = await get_need_fix_reviews()

    # 建立 IID → 資料的對應
    reminder_by_iid = {}
    for r in reminders:
        iid = r.get("gitlab_issue_iid")
        if iid:
            reminder_by_iid[int(iid)] = r

    review_by_iid = {}
    for r in pending_reviews + need_fix_reviews:
        iid = r.get("gitlab_issue_iid")
        if iid:
            review_by_iid[int(iid)] = r

    all_iids = list(set(list(reminder_by_iid.keys()) + list(review_by_iid.keys())))
    if not all_iids:
        logger.debug("GitLab sync: no issues to check")
        return

    # 批次查詢 GitLab
    issues = await gitlab_client.get_issues_by_iids(all_iids)
    closed_iids = {issue["iid"] for issue in issues if issue.get("state") == "closed"}

    if not closed_iids:
        logger.debug(f"GitLab sync: checked {len(all_iids)} issues, none closed")
        return

    synced_reminders = 0
    synced_reviews = 0
    notify_lines = []

    # 更新已關閉的 reminders
    for iid in closed_iids:
        if iid in reminder_by_iid:
            r = reminder_by_iid[iid]
            await update_reminder_status(r["id"], "done")
            synced_reminders += 1
            notify_lines.append(f"✅ 提醒「{r.get('content', '')[:20]}」(@{r.get('assignee_username', '?')}) 已完成")
            logger.info(f"GitLab sync: reminder #{r['id']} (issue #{iid}) marked as done")

    # 更新已關閉的 reviews
    for iid in closed_iids:
        if iid in review_by_iid:
            r = review_by_iid[iid]
            await update_review_status(r["sponsor_name"], ReviewStatus.APPROVED)
            synced_reviews += 1
            notify_lines.append(f"✅ Review「{r['sponsor_name']}」已審核通過")
            logger.info(f"GitLab sync: review '{r['sponsor_name']}' (issue #{iid}) marked as approved")

    logger.info(f"GitLab sync complete: {synced_reminders} reminders, {synced_reviews} reviews updated")

    # 發送通知
    if notify_lines and bot and chat_ids:
        msg = f"🔄 <b>GitLab 同步更新</b>\n\n" + "\n".join(notify_lines)
        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to send sync notification to {chat_id}: {e}")


async def _gitlab_sync_job(context):
    """排程任務：GitLab issue 同步"""
    chat_ids = context.job.data.get("chat_ids", [])
    await sync_gitlab_issues(context.bot, chat_ids)


def setup_scheduler(app: Application, chat_ids: list[int]):
    """設定排程任務"""
    job_queue = app.job_queue

    if not job_queue:
        logger.error("Job queue is not available")
        return

    job_data = {"chat_ids": chat_ids}

    # 從環境變數取得提醒週期（分鐘）
    interval_pending = get_reminder_interval(
        "REMINDER_INTERVAL_PENDING", DEFAULT_INTERVAL_PENDING
    )
    interval_need_fix = get_reminder_interval(
        "REMINDER_INTERVAL_NEED_FIX", DEFAULT_INTERVAL_NEED_FIX
    )

    # 設定週期性提醒 pending reviews
    job_queue.run_repeating(
        remind_pending_reviews,
        interval=interval_pending * 60,  # 轉換為秒
        first=interval_pending * 60,  # 第一次執行延遲一個週期
        data=job_data,
        name="pending_reminder",
    )
    logger.info(f"Scheduled pending review reminder every {interval_pending} minutes")

    # 設定週期性提醒 need_fix reviews
    job_queue.run_repeating(
        remind_need_fix_reviews,
        interval=interval_need_fix * 60,  # 轉換為秒
        first=interval_need_fix * 60,  # 第一次執行延遲一個週期
        data=job_data,
        name="need_fix_reminder",
    )
    logger.info(f"Scheduled need-fix reminder every {interval_need_fix} minutes")

    # 設定每日摘要通知（預設 09:00 Asia/Taipei）
    daily_time_str = os.getenv("DAILY_SUMMARY_TIME", "09:00")
    daily_time = _parse_time(daily_time_str) or time(9, 0)
    daily_time = daily_time.replace(tzinfo=TZ)
    job_queue.run_daily(
        _daily_summary_job,
        time=daily_time,
        data=job_data,
        name="daily_summary",
    )
    logger.info(f"Scheduled daily summary at {daily_time.strftime('%H:%M')} (Asia/Taipei)")

    # 設定 GitLab issue 同步（預設每 10 分鐘）
    sync_interval = int(os.getenv("GITLAB_SYNC_INTERVAL", "10"))
    job_queue.run_repeating(
        _gitlab_sync_job,
        interval=sync_interval * 60,
        first=60,  # 啟動後 1 分鐘執行第一次
        data=job_data,
        name="gitlab_sync",
    )
    logger.info(f"Scheduled GitLab issue sync every {sync_interval} minutes")

    logger.info(
        f"Scheduler setup complete. Reminders will be sent to chat IDs: {chat_ids}"
    )

    # 載入現有的個人提醒
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        loop.create_task(load_custom_reminders(app))
    except RuntimeError:
        # 如果真的沒 loop (不應發生在 post_init)，則回退
        logger.warning("No running loop found in setup_scheduler, reminder loading may be delayed")


async def load_custom_reminders(app: Application):
    """從資料庫載入並設定所有待處理的個人提醒"""
    reminders = await get_active_reminders()
    for r in reminders:
        schedule_reminder_job(app, r)
    logger.info(f"Loaded {len(reminders)} custom reminders from database")


def schedule_reminder_job(app: Application, reminder: dict):
    """設定單個個人提醒的 Job"""
    job_queue = app.job_queue
    if not job_queue:
        return

    reminder_id = reminder["id"]
    next_at = reminder["next_remind_at"]

    # 轉換 next_at 字串為 datetime (如果從 sqlite 讀出來是字串)
    if isinstance(next_at, str):
        try:
            next_at = datetime.fromisoformat(next_at)
        except ValueError:
            logger.error(f"Invalid next_remind_at format for reminder {reminder_id}: {next_at}")
            return

    if not next_at:
        return

    # 如果已經過期且是一次性的，就不排程
    now = datetime.now(TZ)
    if next_at < now and reminder["timing_type"] == "once":
        return

    # 為了解決時區問題，如果 next_at 沒有時區，加上本地時區
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=TZ)

    job_queue.run_once(
        execute_reminder_job,
        when=next_at,
        data=reminder_id,
        name=f"remind_{reminder_id}",
    )


async def execute_reminder_job(context):
    """執行個人提醒 Job：發送通知並更新下次時間"""
    reminder_id = context.job.data
    reminder = await get_reminder_by_id(reminder_id)

    if not reminder or reminder["status"] != "pending":
        return

    # 發送通知
    username = reminder["assignee_username"]
    content = reminder["content"]
    msg = f"🔔 提醒 @{html.escape(username)}\n\n📝 內容：{html.escape(content)}"
    if reminder.get("gitlab_issue_url"):
        msg += f"\n🔗 GitLab: <a href=\"{reminder['gitlab_issue_url']}\">#{reminder['gitlab_issue_iid']}</a>"

    from handlers.utils import get_allowed_chat_ids
    chat_ids = get_allowed_chat_ids()
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send custom reminder {reminder_id} to {chat_id}: {e}")

    # 發送後清除 next_remind_at (不論是否成功)
    await update_next_remind_at(reminder_id, None)
