from dataclasses import asdict, dataclass
from importlib.metadata import version
from typing import Protocol


DEFAULT_RERANKER_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
DEFAULT_RERANKER_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
RERANKER_INPUT_VERSION = "candidate-representation-v1"


class SemanticRerankerError(RuntimeError):
    pass


@dataclass(frozen=True)
class SemanticRerankerIdentity:
    provider: str
    model: str
    revision: str
    library: str
    library_version: str
    device: str
    batch_size: int
    max_length: int
    input_version: str = RERANKER_INPUT_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


class SemanticReranker(Protocol):
    @property
    def identity(self) -> SemanticRerankerIdentity:
        ...

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        ...


def build_reranker_text(candidate: dict) -> str:
    """Build the deterministic candidate representation for pair scoring."""

    metadata = candidate.get("metadata") or {}
    metadata_lines = [
        f"knowledge_category: {metadata.get('knowledge_category', '')}",
        f"business_domain: {metadata.get('business_domain', '')}",
        f"source_type: {metadata.get('source_type', '')}",
    ]
    return "\n".join(
        [
            f"source: {candidate.get('source', '')}",
            f"section: {candidate.get('section', '')}",
            *metadata_lines,
            f"content: {candidate.get('text', '')}",
        ]
    )


class CrossEncoderSemanticReranker:
    """Local multilingual Cross-Encoder reranker with lazy model loading."""

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_RERANKER_MODEL,
        revision: str = DEFAULT_RERANKER_REVISION,
        device: str = "cpu",
        batch_size: int = 8,
        max_length: int = 512,
    ) -> None:
        self.model_name = model_name
        self.revision = revision
        self.device = device
        self.batch_size = batch_size
        self.max_length = max_length
        self._model = None

    @property
    def identity(self) -> SemanticRerankerIdentity:
        try:
            library_version = version("sentence-transformers")
        except Exception:
            library_version = "not-installed"

        return SemanticRerankerIdentity(
            provider="sentence_transformers_cross_encoder",
            model=self.model_name,
            revision=self.revision,
            library="sentence-transformers",
            library_version=library_version,
            device=self.device,
            batch_size=self.batch_size,
            max_length=self.max_length,
        )

    def _load_model(self):
        if self._model is not None:
            return self._model

        try:
            import torch
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(
                self.model_name,
                revision=self.revision,
                device=self.device,
                max_length=self.max_length,
                activation_fn=torch.nn.Sigmoid(),
            )
            model.model.eval()
        except Exception as error:
            raise SemanticRerankerError(
                "Failed to load local semantic reranker "
                f"{self.model_name}@{self.revision}: "
                f"{type(error).__name__}: {error}"
            ) from error

        self._model = model
        return model

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        if not query.strip():
            raise ValueError("Semantic reranker query must not be empty")

        if not candidates:
            return []

        pairs = [
            (query, build_reranker_text(candidate))
            for candidate in candidates
        ]
        model = self._load_model()

        try:
            scores = model.predict(
                pairs,
                batch_size=self.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception as error:
            raise SemanticRerankerError(
                "Local semantic reranker inference failed: "
                f"{type(error).__name__}: {error}"
            ) from error

        if len(scores) != len(candidates):
            raise SemanticRerankerError(
                "Semantic reranker returned a different number of scores "
                "than input candidates"
            )

        reranked = []
        for index, (candidate, score) in enumerate(zip(candidates, scores), start=1):
            retrieval_score = float(
                candidate.get("retrieval_score", candidate.get("score", 0))
                or 0
            )
            reranked.append(
                {
                    **candidate,
                    "retrieval_rank": int(candidate.get("retrieval_rank", index)),
                    "retrieval_score": round(retrieval_score, 6),
                    "semantic_rerank_score": round(float(score), 6),
                    "semantic_reranker": self.identity.to_dict(),
                }
            )

        reranked.sort(
            key=lambda item: (
                item["semantic_rerank_score"],
                -item["retrieval_rank"],
            ),
            reverse=True,
        )

        return [
            {
                **candidate,
                "semantic_rank": rank,
            }
            for rank, candidate in enumerate(reranked, start=1)
        ]


def build_semantic_reranker(settings=None) -> CrossEncoderSemanticReranker:
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()

    if settings.rag_semantic_reranker_provider != "cross_encoder":
        raise SemanticRerankerError(
            "Unsupported semantic reranker provider: "
            f"{settings.rag_semantic_reranker_provider}"
        )

    return CrossEncoderSemanticReranker(
        model_name=settings.rag_semantic_reranker_model,
        revision=settings.rag_semantic_reranker_revision,
        device=settings.rag_semantic_reranker_device,
        batch_size=settings.rag_semantic_reranker_batch_size,
        max_length=settings.rag_semantic_reranker_max_length,
    )
