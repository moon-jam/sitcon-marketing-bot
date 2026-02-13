import os
import json
import logging
import httpx
import urllib.parse
from typing import Optional, List

logger = logging.getLogger(__name__)

class GitLabClient:
    def __init__(self):
        self._mapping = None

    @property
    def mapping_file(self):
        return os.getenv("GITLAB_MAPPING_PATH", "telegramID2gitlabID.json")

    @property
    def url(self):
        base_url = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
        return f"{base_url}/api/v4"

    @property
    def headers(self):
        token = os.getenv("GITLAB_TOKEN")
        return {"Private-Token": token} if token else {}

    @property
    def project_id(self):
        pid = os.getenv("GITLAB_PROJECT_ID")
        if pid and "/" in str(pid):
            return urllib.parse.quote(str(pid), safe="")
        return pid

    def _load_mapping(self):
        if self._mapping is not None:
            return self._mapping
        
        path = self.mapping_file
        if not os.path.exists(path):
            logger.warning(f"Mapping file {path} not found.")
            return {}
        
        try:
            with open(path, "r") as f:
                self._mapping = json.load(f)
        except Exception as e:
            logger.error(f"Error loading mapping file {path}: {e}")
            self._mapping = {}
        return self._mapping

    async def get_gitlab_username(self, telegram_username: str) -> Optional[str]:
        """Maps Telegram username to GitLab username."""
        mapping = self._load_mapping()
        return mapping.get(telegram_username.lstrip("@"))

    def get_telegram_username(self, username: str) -> str:
        """反向映射：從任意 username 找到對應的 Telegram username。
        如果 username 本身就是 Telegram username 則直接回傳，
        如果是 GitLab username 則反查回 Telegram username。"""
        username = username.lstrip("@")
        mapping = self._load_mapping()  # tg_user -> gitlab_user

        # 如果 username 本身就在 mapping 的 key 裡，那它就是 TG username
        if username in mapping:
            return username

        # 反查：看看是否是某個 Telegram user 對應的 GitLab username
        for tg_user, gitlab_user in mapping.items():
            if isinstance(gitlab_user, str) and gitlab_user == username:
                return tg_user

        # 找不到映射就原樣回傳
        return username

    async def get_gitlab_user_id(self, telegram_username: str) -> Optional[int]:
        """Maps Telegram username to GitLab user ID."""
        gitlab_username = await self.get_gitlab_username(telegram_username)
        
        if not gitlab_username:
            logger.warning(f"No GitLab mapping for Telegram user {telegram_username}")
            return None

        # If it's already an integer string, return it
        if isinstance(gitlab_username, int):
            return gitlab_username
        if isinstance(gitlab_username, str) and gitlab_username.isdigit():
            return int(gitlab_username)

        # Otherwise, look up the user by username to get the ID
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.url}/users",
                    params={"username": gitlab_username},
                    headers=self.headers,
                    timeout=10.0
                )
                response.raise_for_status()
                users = response.json()
                if users:
                    return users[0]["id"]
        except Exception as e:
            logger.error(f"Error looking up GitLab user {gitlab_username}: {e}")
        
        return None

    async def create_issue(
        self, 
        title: str, 
        description: str, 
        assignee_id: Optional[int] = None,
        labels: Optional[List[str]] = None,
        due_date: Optional[str] = None
    ) -> Optional[dict]:
        """Creates an issue on GitLab."""
        if not self.project_id or not self.headers:
            logger.error("GitLab project ID or token not configured.")
            return None

        data = {
            "title": title,
            "description": description,
        }
        if assignee_id:
            data["assignee_ids"] = [assignee_id]
        if labels:
            data["labels"] = ",".join(labels)
        if due_date:
            data["due_date"] = due_date

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.url}/projects/{self.project_id}/issues",
                    json=data,
                    headers=self.headers,
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"Error creating GitLab issue: {e}")
            return None

    async def close_issue(self, issue_iid: int) -> bool:
        """Closes an issue on GitLab."""
        if not self.project_id or not self.headers:
            return False

        try:
            async with httpx.AsyncClient() as client:
                response = await client.put(
                    f"{self.url}/projects/{self.project_id}/issues/{issue_iid}",
                    json={"state_event": "close"},
                    headers=self.headers,
                    timeout=10.0
                )
                response.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Error closing GitLab issue {issue_iid}: {e}")
            return False

# Global instance
gitlab_client = GitLabClient()
