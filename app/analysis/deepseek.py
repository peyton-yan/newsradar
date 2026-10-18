"""DeepSeek chat-completions client used by the batch analyzer."""

from __future__ import annotations

import time
from typing import Any

import httpx


class DeepSeekError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4096,
    timeout: int = 120,
    provider: str = "deepseek",
) -> tuple[str, dict[str, Any]]:
    if not api_key and provider != "ollama":
        raise DeepSeekError("LLM API Key 未配置")
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    if provider in {"deepseek", "moonshot", "zhipu"}:
        # Batch classification does not need chain-of-thought.
        payload["thinking"] = {"type": "disabled"}
    elif provider in {"qwen", "siliconflow"}:
        payload["enable_thinking"] = False
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    last_error: DeepSeekError | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            last_error = DeepSeekError("DeepSeek 请求超时")
            if attempt == 0:
                time.sleep(2)
                continue
            raise last_error from exc
        except httpx.HTTPError as exc:
            last_error = DeepSeekError(f"DeepSeek 网络错误: {exc}")
            if attempt == 0:
                time.sleep(2)
                continue
            raise last_error from exc
        if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
            last_error = DeepSeekError(f"DeepSeek HTTP {response.status_code}", status=response.status_code)
            time.sleep(3)
            continue
        if response.status_code >= 400:
            message = f"DeepSeek HTTP {response.status_code}"
            try:
                body = response.json()
                message = str((body.get("error") or {}).get("message") or body.get("message") or message)
            except ValueError:
                pass
            raise DeepSeekError(message, status=response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise DeepSeekError("DeepSeek 返回非 JSON") from exc
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("DeepSeek 响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekError("DeepSeek 返回空内容")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return content, usage
    raise last_error or DeepSeekError("DeepSeek 请求失败")
