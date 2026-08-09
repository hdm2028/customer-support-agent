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
    "七天无理由",
    "质量问题",
    "物流",
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
    "缺货",
    "补发",
    "补货",
    "继续等待",
    "拆单",
    "预售",
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


# 把文本拆成可检索 token。这里不是生产级分词器，而是一个稳定可运行的教学版本：
# 关键词 + 英文数字 + 中文单字 + 中文二字组合，能覆盖中文客服常见问题。
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


# 本地 hash embedding：当没有开启真实 Embedding 时使用。
# 它的作用是保证项目离线也能完整跑通 RAG 流程。
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


# 关键词分数用于和向量分数混合排序。
# 好处是：用户输入“保修”“退款”这种明确业务词时，检索结果更稳定。
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
    """把 Embedding 缓存在本地，避免同一段文档反复请求模型接口。"""

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

    def get(self, provider: str, model: str, dimensions: int, text: str) -> list[float] | None:
        key = self.make_key(provider, model, dimensions, text)
        return self.data.get(key)

    def set(self, provider: str, model: str, dimensions: int, text: str, vector: list[float]) -> None:
        key = self.make_key(provider, model, dimensions, text)
        self.data[key] = vector
        self.save()


class EmbeddingProvider:
    """统一封装 Embedding 来源：本地 hash 或智谱 Embedding。"""

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
