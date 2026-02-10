# SITCON Marketing Bot

SITCON 行銷組 Review 管理機器人 - 用於追蹤信件/文件的審核狀態。

## 功能

### Review 管理

- `/review <名稱> : <連結>` - 新增 review 請求（支援批量，每行一個）
- `/review_approve` - 選擇待審核項目（並通知提交者）
- `/review_need_fix [評語]` - 選擇標記需要修改項目（可附帶評語，並立刻通知提交者）
- `/review_again` - 重新送審（待修改項目修改完成後）
- `/review_list` - 列出所有待處理項目
- `/review_notify` - 手動觸發通知 reviewers 審核

### Reviewer 管理

- `/reviewer_add <username>` - 新增 reviewer
- `/reviewer_remove <username>` - 移除 reviewer
- `/reviewer_list` - 列出所有 reviewers

### 自動提醒

- 週期性提醒 reviewers 審核待處理項目（預設每 60 分鐘）
- 週期性提醒提交者修改被標記為 need_fix 的項目（預設每 120 分鐘）

## 安裝

### 1. 安裝 uv（如果還沒有）

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# 或使用 Homebrew
brew install uv
```

### 2. 複製專案並安裝依賴

```bash
git clone https://github.com/moon-jam/sitcon-marketing-bot.git
cd sitcon-marketing-bot
uv sync
```

### 3. 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env` 檔案，填入：

- `BOT_TOKEN`：從 @BotFather 取得的 Bot Token
- `ALLOWED_CHAT_IDS`：允許使用的聊天室 ID（多個用逗號分隔）
- `REMINDER_INTERVAL_PENDING`：提醒 reviewers 的週期（分鐘，預設 60）
- `REMINDER_INTERVAL_NEED_FIX`：提醒提交者修改的週期（分鐘，預設 120）

### 4. 啟動 Bot

```bash
uv run python main.py
```

### 使用 Docker（建議）

```bash
# 建立並啟動
docker compose up -d

# 查看 logs
docker compose logs -f

# 停止
docker compose down
```

資料庫會持久化在 `./data/reviews.db`。

## 如何取得 Bot Token

1. 在 Telegram 搜尋 @BotFather
2. 傳送 `/newbot`
3. 依照指示設定 bot 名稱和 username
4. 取得 Bot Token（格式：`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`）

## 如何取得聊天室 ID

### 方法一：使用 Telegram API
1. 將 bot 加入群組
2. 在群組中傳送任意訊息
3. 在瀏覽器開啟：`https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
4. 在 JSON 回應中找到 `"chat":{"id": -100XXXXXXXXXX}`

### 方法二：使用 @RawDataBot
1. 在群組中邀請 @RawDataBot
2. Bot 會自動顯示群組資訊，包含 chat ID
3. 取得 ID 後可以把 @RawDataBot 移除

### 方法三：使用 @userinfobot
1. 在群組中邀請 @userinfobot
2. 傳送任意訊息，bot 會回覆包含 chat ID 的資訊

> **注意**：群組/超級群組 ID 通常是負數（如 `-1001234567890`）

## 使用範例

### 新增單一 review

```plaintext
/review 台積電贊助信 : https://docs.google.com/document/d/xxx
```

### 批量新增 review

```plaintext
/review 台積電贊助信 : https://docs.google.com/document/d/1
聯發科贊助信 : https://docs.google.com/document/d/2
Google 贊助信 : https://docs.google.com/document/d/3
```

### 審核通過

```plaintext
/review_approve 台積電贊助信
```

### 標記需要修改

```plaintext
/review_need_fix 聯發科贊助信
```

### 管理 Reviewers

```plaintext
/reviewer_add moonjam322
/reviewer_add smitug01
/reviewer_list
/reviewer_remove someone
```

### 手動通知 Reviewers

```plaintext
/review_notify
```

## 專案結構

```plaintext
sitcon-marketing-bot/
├── .env.example        # 環境變數範例
├── .gitignore
├── .dockerignore
├── .python-version
├── Dockerfile          # Docker 映像定義
├── docker-compose.yml  # Docker Compose 配置
├── pyproject.toml      # 專案設定與依賴
├── uv.lock
├── README.md
├── main.py             # 主程式進入點
├── database.py         # 資料庫操作
├── scheduler.py        # 排程提醒
├── data/               # SQLite 資料庫目錄（Docker 持久化）
└── handlers/
    ├── __init__.py
    ├── review.py       # Review 相關指令
    └── reviewer.py     # Reviewer 管理指令
```

## License

[MIT License](LICENSE)
