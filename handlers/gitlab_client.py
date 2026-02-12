import os
import json
import logging
import httpx
import urllib.parse
from typing import Optional, List

logger = logging.getLogger(__name__)

MAPPING_FILE = "telegramID2gitlabID.json"

class GitLabClient:
    def __init__(self):
        self._mapping = None

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
        
        if not os.path.exists(MAPPING_FILE):
            logger.warning(f"Mapping file {MAPPING_FILE} not found.")
            return {}
        
        try:
            with open(MAPPING_FILE, "r") as f:
                self._mapping = json.load(f)
        except Exception as e:
            logger.error(f"Error loading mapping file: {e}")
            self._mapping = {}
        return self._mapping

    async def get_gitlab_username(self, telegram_username: str) -> Optional[str]:
        """Maps Telegram username to GitLab username."""
        mapping = self._load_mapping()
        return mapping.get(telegram_username.lstrip("@"))

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
        labels: Optional[List[str]] = None
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
