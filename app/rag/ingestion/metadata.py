import json
from dataclasses import replace

from app.rag.models import RawDocument, normalize_source


def _parse_front_matter_value(value: str) -> object:
    stripped = value.strip()

    if not stripped:
        return ""

    if stripped.lower() in {"true", "false", "null"}:
        return json.loads(stripped.lower())

    if stripped.startswith(("[", "{", '"')):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            if stripped.startswith("[") and stripped.endswith("]"):
                inner = stripped[1:-1].strip()
                return (
                    [_parse_front_matter_value(item) for item in inner.split(",")]
                    if inner
                    else []
                )

    try:
        return int(stripped)
    except ValueError:
        pass

    try:
        return float(stripped)
    except ValueError:
        return stripped.strip("'\"")


def parse_markdown_front_matter(text: str) -> tuple[str, dict]:
    lines = text.splitlines()

    if not lines or lines[0].strip() != "---":
        return text, {}

    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )

    if closing_index is None:
        raise ValueError("Markdown front matter is missing its closing delimiter")

    metadata = {}
    for line in lines[1:closing_index]:
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        if ":" not in stripped:
            raise ValueError(f"Invalid Markdown front matter line: {line!r}")

        key, value = stripped.split(":", 1)
        key = key.strip()

        if not key:
            raise ValueError(f"Invalid Markdown front matter key: {line!r}")

        metadata[key] = _parse_front_matter_value(value)

    body = "\n".join(lines[closing_index + 1:])
    return body.lstrip("\r\n"), metadata


def classify_document(source: str) -> dict:
    if any(keyword in source for keyword in ["退款", "退换货", "商品售后规则"]):
        return {
            "knowledge_category": "refund_policy",
            "business_domain": "after_sales",
            "source_type": "policy",
        }

    if any(keyword in source for keyword in ["物流", "配送"]):
        return {
            "knowledge_category": "logistics_policy",
            "business_domain": "fulfillment",
            "source_type": "policy",
        }

    if any(keyword in source for keyword in ["商品说明", "保修"]):
        return {
            "knowledge_category": "product_manual",
            "business_domain": "product_support",
            "source_type": "manual",
        }

    if "客服SOP" in source:
        return {
            "knowledge_category": "customer_sop",
            "business_domain": "customer_service",
            "source_type": "sop",
        }

    if any(keyword in source for keyword in ["FAQ", "历史问题", "案例"]):
        return {
            "knowledge_category": "historical_case",
            "business_domain": "customer_service",
            "source_type": "case",
        }

    return {
        "knowledge_category": "general_policy",
        "business_domain": "customer_service",
        "source_type": "knowledge",
    }


def metadata_for_path(source: str, rules: dict[str, dict]) -> dict:
    normalized_source = normalize_source(source)
    matches = []

    for prefix, metadata in rules.items():
        normalized_prefix = normalize_source(prefix)

        if (
            normalized_source == normalized_prefix
            or normalized_source.startswith(normalized_prefix + "/")
        ):
            matches.append((len(normalized_prefix), metadata))

    merged = {}
    for _, metadata in sorted(matches, key=lambda item: item[0]):
        merged.update(metadata)

    return merged


class MetadataEnricher:
    def __init__(
        self,
        *,
        explicit_metadata: dict[str, dict] | None = None,
        path_metadata: dict[str, dict] | None = None,
    ) -> None:
        self.explicit_metadata = {
            normalize_source(source): metadata
            for source, metadata in (explicit_metadata or {}).items()
        }
        self.path_metadata = path_metadata or {}

    def enrich(self, document: RawDocument) -> RawDocument:
        operational_metadata = dict(document.metadata)
        front_matter = operational_metadata.pop("front_matter", {})
        metadata = {
            **operational_metadata,
            **classify_document(document.source),
            **metadata_for_path(document.source, self.path_metadata),
            **front_matter,
            **self.explicit_metadata.get(normalize_source(document.source), {}),
        }

        return replace(document, metadata=metadata)
