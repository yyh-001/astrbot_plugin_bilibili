import json
from typing import Any

import aiohttp

BGM_API_BASE_URL = "https://api.bgm.tv"
DEFAULT_TIMEOUT_SECS = 10
DEFAULT_BANGUMI_USER_AGENT = (
    "soulter/astrbot_plugin_bilibili "
    "(https://github.com/Soulter/astrbot_plugin_bilibili)"
)
MAX_ERROR_BODY_LEN = 300


class BangumiApiClient:
    """bgm.tv API client."""

    def __init__(
        self,
        token: str = "",
        user_agent: str = DEFAULT_BANGUMI_USER_AGENT,
        *,
        base_url: str = BGM_API_BASE_URL,
        timeout_secs: int = DEFAULT_TIMEOUT_SECS,
    ) -> None:
        self._token = token.strip()
        self._user_agent = user_agent.strip()
        if not self._user_agent:
            raise ValueError("bangumi_user_agent 不能为空")
        self._base_url = base_url.rstrip("/")
        self._timeout_secs = timeout_secs

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _ensure_list_payload(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise ValueError("bgm.tv /calendar 响应格式错误：顶层不是数组")
        return [item for item in payload if isinstance(item, dict)]

    async def get_calendar(self) -> list[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=self._timeout_secs)
        url = f"{self._base_url}/calendar"
        async with aiohttp.ClientSession(
            timeout=timeout, headers=self._headers()
        ) as session:
            async with session.get(url) as resp:
                body = await resp.text()
                if resp.status != 200:
                    snippet = body[:MAX_ERROR_BODY_LEN]
                    raise RuntimeError(
                        f"bgm.tv /calendar 请求失败: HTTP {resp.status}, body={snippet}"
                    )
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise ValueError("bgm.tv /calendar 响应不是合法 JSON") from exc
                return self._ensure_list_payload(payload)
