import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from pathlib import Path

from app.core.config import BASE_DIR, Settings, get_settings


CACHE_PATH = BASE_DIR / "data" / "cache" / "embedding_cache.json"
LOCAL_VECTOR_DIM = 256

CUSTOMER_KEYWORDS = [
    "退货",
    "换货",
    "退款",
    "退钱",
    "退款申请",
    "人工审核",
    "MQ",
    "七天无理由",
    "质量问题",
    "质量",
    "黑屏",
    "物流",
    "快递",
    "未收到",
    "没收到",
    "三天没动",
    "超过48",
    "发货",
    "签收",
    "保修",
    "维修",
    "检测",
    "耳机",
    "手环",
    "定制",
    "会员",
    "优惠券",
    "投诉",
    "赔付",
    "修改地址",
    "改收货地址",
    "修改收货地址",
    "待发货",
    "出库前",
    "改派",
    "工单",
    "人工",
    "人工客服",
    "投诉升级",
    "升级工单",
    "记录用户诉求",
    "支付",
    "扣款",
    "银行卡",
    "发票",
    "电子发票",
    "发票抬头",
    "税号",
    "邮箱",
]


# Keyword, alphanumeric, unigram and bigram tokens for Chinese support queries.
def tokenize(text: str) -> list[str]:
    tokens = []
    text = text.strip()

    for keyword in CUSTOMER_KEYWORDS:
        if keyword in text:
            tokens.append(keyword.lower())

    for word in re.findall(r"[a-zA-Z0-9]+", text):
        tokens.append(word.lower())

    chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(chinese_chars)

    for index in range(len(chinese_chars) - 1):
        tokens.append(chinese_chars[index] + chinese_chars[index + 1])

    return tokens


# Local embedding fallback when remote embeddings are disabled.
def local_hash_embedding(text: str, dimensions: int = LOCAL_VECTOR_DIM) -> list[float]:
    vector = [0.0] * dimensions

    for token in tokenize(text):
        digest = hashlib.md5(token.encode("utf-8")).hexdigest()
        index = int(digest[:8], 16) % dimensions
        vector[index] += 1.0

    norm = math.sqrt(sum(value * value for value in vector))

    if norm == 0:
        return vector

    return [value / norm for value in vector]


# Keyword score is combined with vector and BM25 scores.
def keyword_score(query: str, source: str, text: str) -> int:
    score = 0
    source_lower = source.lower()
    text_lower = text.lower()

    for keyword in CUSTOMER_KEYWORDS:
        if keyword not in query:
            continue

        keyword_lower = keyword.lower()

        if keyword_lower in source_lower:
            score += 3

        if keyword_lower in text_lower:
            score += 1

    return score


class EmbeddingCache:
    def __init__(self, path: Path = CACHE_PATH) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}

        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def make_key(self, provider: str, model: str, dimensions: int, text: str) -> str:
        raw = f"{provider}|{model}|{dimensions}|{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def make_cache_key(self, provider: str, model: str, dimensions: int, text: str) -> str:
        return "embedding:" + self.make_key(provider, model, dimensions, text)

    def get(self, provider: str, model: str, dimensions: int, text: str) -> list[float] | None:
        key = self.make_key(provider, model, dimensions, text)
        cache_key = self.make_cache_key(provider, model, dimensions, text)

        try:
            from app.storage.cache import get_json_cache, set_json_cache

            cached = get_json_cache(cache_key)
            if cached is not None:
                return cached
        except Exception:
            pass

        vector = self.data.get(key)

        if vector is not None:
            try:
                from app.storage.cache import set_json_cache

                set_json_cache(
                    cache_key,
                    vector,
                    ttl_seconds=get_settings().embedding_cache_ttl_seconds,
                )
            except Exception:
                pass

        return vector

    def set(self, provider: str, model: str, dimensions: int, text: str, vector: list[float]) -> None:
        key = self.make_key(provider, model, dimensions, text)
        self.data[key] = vector

        try:
            from app.storage.cache import set_json_cache

            set_json_cache(
                self.make_cache_key(provider, model, dimensions, text),
                vector,
                ttl_seconds=get_settings().embedding_cache_ttl_seconds,
            )
        except Exception:
            pass

        self.save()


class EmbeddingProvider:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cache = EmbeddingCache()

    def embed_text(self, text: str) -> list[float]:
        if self.settings.rag_embedding_provider == "zhipu":
            return self._embed_with_zhipu(text)

        return local_hash_embedding(text)

    def _embed_with_zhipu(self, text: str) -> list[float]:
        provider = "zhipu"
        model = self.settings.zhipu_embedding_model
        dimensions = self.settings.embedding_dimensions

        cached = self.cache.get(provider, model, dimensions, text)
        if cached is not None:
            return cached

        if not self.settings.has_llm_key:
            raise RuntimeError("没有读取到智谱 API Key，无法调用真实 Embedding。")

        payload = {
            "model": model,
            "input": text,
            "dimensions": dimensions,
        }

        request = urllib.request.Request(
            url=self.settings.zhipu_embedding_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.zhipu_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.settings.llm_timeout_seconds,
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"智谱 Embedding 请求失败：HTTP {error.code} {body}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"智谱 Embedding 连接失败：{error}") from error

        vector = data["data"][0]["embedding"]
        self.cache.set(provider, model, dimensions, text, vector)

        return vector


def get_embedding_provider() -> EmbeddingProvider:
    return EmbeddingProvider()
