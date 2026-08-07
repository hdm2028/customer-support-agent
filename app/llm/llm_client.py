import json
import urllib.error
import urllib.request

from app.core.config import Settings, get_settings


def call_zhipu_chat(messages: list[dict], settings: Settings | None = None) -> str:
    settings = settings or get_settings()

    if not settings.has_llm_key:
        raise RuntimeError("没有读取到智谱 API Key，请在 .env 中配置 ZHIPUAI_API_KEY。")

    payload = {
        "model": settings.zhipu_model,
        "messages": messages,
        "temperature": 0.2,
    }

    request = urllib.request.Request(
        url=settings.zhipu_base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.zhipu_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"智谱接口请求失败：HTTP {error.code} {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"智谱接口连接失败：{error}") from error

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"智谱接口没有返回 choices：{data}")

    return choices[0]["message"]["content"]
