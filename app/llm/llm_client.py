import json
import urllib.error
import urllib.request

from app.core.config import Settings, get_settings
from app.observability.usage import begin_call, provider_usage, end_call


def call_zhipu_chat(messages: list[dict], settings: Settings | None = None) -> str:
    """非流式调用智谱 Chat Completions，适合普通 API 和自动化测试。"""

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

    call = begin_call(settings.zhipu_model, messages)
    try:
        with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        provider_usage(call, data.get("usage"))
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("智谱接口没有返回 choices")
        content = choices[0]["message"]["content"]
    except urllib.error.HTTPError as error:
        end_call(call, "", success=False, error_type="HTTPError")
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"智谱接口请求失败：HTTP {error.code} {body}") from error
    except urllib.error.URLError as error:
        end_call(call, "", success=False, error_type="URLError")
        raise RuntimeError(f"智谱接口连接失败：{error}") from error
    except BaseException as error:
        end_call(call, "", success=False, error_type=type(error).__name__)
        raise
    end_call(call, content, success=True)
    return content


def call_zhipu_chat_stream(messages: list[dict], settings: Settings | None = None, *, usage_trace=None):
    """流式调用智谱 Chat Completions，逐段产出模型生成的文本。"""

    settings = settings or get_settings()

    if not settings.has_llm_key:
        raise RuntimeError("没有读取到智谱 API Key，请在 .env 中配置 ZHIPUAI_API_KEY。")

    payload = {
        "model": settings.zhipu_model,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
    }

    request = urllib.request.Request(
        url=settings.zhipu_base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.zhipu_api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    call = begin_call(settings.zhipu_model, messages, trace=usage_trace)
    parts = []
    completed = False
    error_type = None
    try:
        with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()

                if not line or not line.startswith("data:"):
                    continue

                payload_text = line.removeprefix("data:").strip()

                if payload_text == "[DONE]":
                    completed = True
                    break

                try:
                    chunk = json.loads(payload_text)
                except json.JSONDecodeError:
                    continue

                provider_usage(call, chunk.get("usage"))

                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    content = delta.get("content")

                    if content:
                        parts.append(content)
                        yield content
    except urllib.error.HTTPError as error:
        error_type = "HTTPError"
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"智谱流式接口请求失败：HTTP {error.code} {body}") from error
    except urllib.error.URLError as error:
        error_type = "URLError"
        raise RuntimeError(f"智谱流式接口连接失败：{error}") from error
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        end_call(call, "".join(parts), success=completed, error_type=error_type or (None if completed else "IncompleteStream"))
