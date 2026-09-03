import json
import re

from app.rag.models import (
    ChunkStrategy,
    DocumentChunk,
    RawDocument,
    content_hash_text,
)


# metadata 结构发生变化：
# Fixed chunk 新增 covered_sections，因此升级版本，
# 避免旧 chunk_id / 旧索引 metadata 被继续复用。
CHUNKER_VERSION = "token-strategy-v3"

MAX_STRUCTURED_TOKENS = 700

CHUNK_STRATEGIES = {
    "fixed_128": ChunkStrategy("fixed_128", 128, 16),
    "fixed_256": ChunkStrategy("fixed_256", 256, 32),
    "fixed_512": ChunkStrategy("fixed_512", 512, 64),
    "markdown": ChunkStrategy("markdown", 700, 0),
    "type_aware": ChunkStrategy("type_aware", 700, 0),
}


# 简单 token 规则：
# - 中文字符单独计 token
# - 英文单词作为 token
# - 数字/小数作为 token
# - 其他非空白字符单独计 token
TOKEN_RE = re.compile(
    r"[\u3400-\u9fff]"
    r"|[A-Za-z]+(?:['-][A-Za-z]+)*"
    r"|\d+(?:\.\d+)?"
    r"|[^\s]"
)


# 所有 Markdown heading，用于原有 Markdown / TypeAware 切分
HEADING_RE = re.compile(
    r"^(#{1,6})\s+(.+?)\s*$"
)


# Fixed chunk 的 covered_sections 不把一级标题 # 当作业务 section。
#
# 例如：
#
# # 退款政策                <- 文档标题，不作为业务 section
# ## 退款资格判断           <- section
# ### 特殊情况              <- section
#
SECTION_HEADING_RE = re.compile(
    r"^(#{2,6})\s+(.+?)\s*$",
    re.MULTILINE,
)


def token_spans(text):
    """
    返回 text 中所有 token 的字符位置。

    示例：
        [
            (0, 1),
            (1, 2),
            ...
        ]
    """
    return [
        (match.start(), match.end())
        for match in TOKEN_RE.finditer(text)
    ]


def token_count(text):
    """
    统计文本 token 数量。
    """
    return len(token_spans(text))


def _section_blocks(text, top_level_only=False):
    """
    根据 Markdown heading 将文档切成 section block。

    top_level_only=False:
        # / ## / ### / ... 都作为边界

    top_level_only=True:
        只使用 ## 作为业务级边界。

    注意：
        这个函数只服务于 markdown / type_aware。
        Fixed strategy 不允许经过这里。
    """
    lines = text.splitlines(keepends=True)

    headings = []
    position = 0

    for line in lines:
        match = HEADING_RE.match(
            line.rstrip("\r\n")
        )

        if match and (
            not top_level_only
            or len(match.group(1)) == 2
        ):
            headings.append(
                (
                    position,
                    match.group(2).strip(),
                )
            )

        position += len(line)

    starts = [0] + [
        pos
        for pos, _ in headings
    ]

    ends = [
        pos
        for pos, _ in headings
    ] + [len(text)]

    blocks = []

    for index, start in enumerate(starts):
        end = ends[index]

        raw_block = text[start:end]
        block = raw_block.strip()

        if not block:
            continue

        first = block.splitlines()[0].strip()
        match = HEADING_RE.match(first)

        if match:
            title = match.group(2).strip()

            # 如果这个 block 只有 heading，没有正文，
            # 则不生成空 chunk。
            remaining = "\n".join(
                block.splitlines()[1:]
            ).strip()

            if not remaining:
                continue
        else:
            title = "正文"

        # strip() 后修正实际字符起始位置
        actual_start = (
            start
            + len(raw_block)
            - len(raw_block.lstrip())
        )

        blocks.append(
            (
                title,
                block,
                actual_start,
                actual_start + len(block),
            )
        )

    return blocks


def split_markdown_sections(text):
    """
    对外保留原有 Markdown section split 接口。
    """
    return [
        (title, body)
        for title, body, _, _ in _section_blocks(text)
    ]


def _fixed_token_chunks(text, size, overlap):
    """
    真正的 Fixed Token Window Splitter。

    重要：
        这里直接针对原始 document.text。
        不经过 Markdown section split。

    返回：
        [
            (start_char, end_char, chunk_text),
            ...
        ]
    """
    spans = token_spans(text)

    if not spans:
        return []

    if size <= 0:
        raise ValueError("chunk size must be > 0")

    if overlap < 0 or overlap >= size:
        raise ValueError(
            "overlap must satisfy 0 <= overlap < size"
        )

    result = []

    step = size - overlap

    for first in range(
        0,
        len(spans),
        step,
    ):
        last = min(
            first + size,
            len(spans),
        )

        start = spans[first][0]
        end = spans[last - 1][1]

        value = text[start:end].strip()

        if value:
            result.append(
                (
                    start,
                    end,
                    value,
                )
            )

        if last == len(spans):
            break

    return result


def _fixed_chunk_section_title(
    document,
    start_char,
):
    """
    保留旧 section 字段的行为。

    对 Fixed chunk：
        section 表示 chunk 起始位置所属的 Markdown section。

    注意：
        section 只用于 metadata / 兼容旧代码，
        不参与 Fixed chunk 边界生成。
    """
    if document.section:
        return document.section

    if document.file_type != "md":
        return "正文"

    title = "正文"
    position = 0

    for line in document.text.splitlines(
        keepends=True
    ):
        if position > start_char:
            break

        match = HEADING_RE.match(
            line.rstrip("\r\n")
        )

        if match:
            title = match.group(2).strip()

        position += len(line)

    return title


def _markdown_section_positions(text):
    """
    找出 Markdown 中所有业务 section 的字符位置。

    只记录 ## ~ ######，
    不记录 # 一级文档标题。

    返回示例：

        [
            (120, "退款资格判断"),
            (350, "退款申请"),
            (620, "退款人工审核"),
        ]
    """
    return [
        (
            match.start(),
            match.group(2).strip(),
        )
        for match in SECTION_HEADING_RE.finditer(text)
    ]


def _fixed_chunk_covered_sections(
    document,
    start_char,
    end_char,
    section_positions,
):
    """
    计算一个 Fixed chunk 实际覆盖到哪些 Markdown section。

    Fixed chunk 可以跨 heading，因此不能只保存一个 section。

    例如：

        ## 退款资格判断
        ...

        ## 退款申请
        ...

        ## 退款人工审核
        ...

    一个 Fixed256 chunk 可能：

        start_char 位于“退款资格判断”
        end_char 位于“退款人工审核”

    则：

        covered_sections = [
            "退款资格判断",
            "退款申请",
            "退款人工审核",
        ]

    注意：
        这个函数只生成 metadata。
        绝对不能改变 chunk start/end。
    """

    # 如果上游 RawDocument 本身已经明确指定 section，
    # 则直接保留。
    if document.section:
        return [document.section]

    if document.file_type != "md":
        return []

    covered = []

    # --------------------------------------------------
    # 1. 找 chunk 起始字符所在的 section
    # --------------------------------------------------

    current_section = None

    for section_start, title in section_positions:
        if section_start <= start_char:
            current_section = title
        else:
            break

    if current_section:
        covered.append(current_section)

    # --------------------------------------------------
    # 2. 找 chunk 范围内部新进入的 section
    # --------------------------------------------------

    for section_start, title in section_positions:
        if start_char < section_start < end_char:
            if title not in covered:
                covered.append(title)

    return covered


def split_text_with_overlap(
    text,
    max_chars,
    overlap,
):
    """
    保留旧接口名称。

    注意：
        参数名虽然叫 max_chars，
        实际现在按 token 数量切分。
    """
    if (
        max_chars <= 0
        or overlap < 0
        or overlap >= max_chars
    ):
        raise ValueError(
            "invalid token chunk size/overlap"
        )

    return _fixed_token_chunks(
        text,
        max_chars,
        overlap,
    )


def choose_chunk_strategy(document):
    """
    从 document.metadata 中读取 chunk_strategy。
    """
    name = document.metadata.get(
        "chunk_strategy",
        "fixed_256",
    )

    if name not in CHUNK_STRATEGIES:
        raise ValueError(
            f"Unknown chunk strategy: {name}"
        )

    return CHUNK_STRATEGIES[name]


def _split_oversized(text):
    """
    Markdown / TypeAware 的超长 section fallback。

    正常结构化 section <= 700 tokens 时保持完整。
    只有超长时才继续切。
    """
    if token_count(text) <= MAX_STRUCTURED_TOKENS:
        return [
            (
                0,
                len(text),
                text.strip(),
            )
        ]

    heading_match = HEADING_RE.match(
        text.splitlines()[0].strip()
    )

    heading_prefix = ""
    body_text = text

    if heading_match:
        first_line = text.splitlines(
            keepends=True
        )[0]

        heading_prefix = first_line

        body_text = text[
            len(first_line):
        ].lstrip("\r\n")

    # 优先按 Markdown 段落切
    units = [
        part
        for part in re.split(
            r"\n\s*\n",
            body_text,
        )
        if part.strip()
    ]

    # 如果没有自然段，则退化到句子级
    if len(units) == 1:
        units = [
            part
            for part in re.split(
                r"(?<=[。！？.!?])\s*",
                body_text,
            )
            if part.strip()
        ]

    result = []
    cursor = 0

    for unit_index, raw_unit in enumerate(units):
        unit = raw_unit.strip()

        start = text.find(
            unit,
            cursor,
        )

        # 极端情况下 find 失败，防止产生 -1 offset
        if start < 0:
            start = cursor

        end = start + len(unit)

        output_unit = (
            heading_prefix.rstrip()
            + "\n"
            + unit
            if (
                unit_index == 0
                and heading_prefix
            )
            else unit
        )

        if (
            token_count(output_unit)
            <= MAX_STRUCTURED_TOKENS
        ):
            result.append(
                (
                    start,
                    end,
                    output_unit,
                )
            )

        else:
            result.extend(
                (
                    start + sub_start,
                    start + sub_end,
                    sub_text,
                )
                for (
                    sub_start,
                    sub_end,
                    sub_text,
                ) in _fixed_token_chunks(
                    output_unit,
                    MAX_STRUCTURED_TOKENS,
                    0,
                )
            )

        cursor = end

    return result


def _build_chunk_id(
    document,
    *,
    section_index,
    chunk_index,
    section,
    start_char,
    end_char,
    content_hash,
    strategy,
    chunker_version,
):
    """
    构造稳定 chunk_id。
    """
    identity = json.dumps(
        {
            "document_id": document.document_id,
            "page": document.page,
            "section_index": section_index,
            "section": section,
            "chunk_index": chunk_index,
            "start_char": start_char,
            "end_char": end_char,
            "content_hash": content_hash,
            "strategy": strategy.name,
            "size": strategy.max_chars,
            "overlap": strategy.overlap,
            "chunker_version": chunker_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return (
        f"{document.document_id}:chunk:"
        f"{content_hash_text(identity)[:24]}"
    )


def chunk_document(
    document,
    *,
    chunker_version=CHUNKER_VERSION,
    chunk_strategy=None,
):
    """
    对单个 RawDocument 进行 chunk。

    Fixed:
        raw document
        -> fixed token windows

    Markdown:
        raw document
        -> Markdown heading sections
        -> oversized fallback

    TypeAware:
        raw document
        -> ## business section
        -> oversized fallback
    """
    if not document.text.strip():
        return []

    metadata = dict(document.metadata)

    if chunk_strategy is not None:
        metadata["chunk_strategy"] = (
            chunk_strategy
        )

    strategy = choose_chunk_strategy(
        type(
            "ConfiguredDocument",
            (),
            {
                "metadata": metadata,
            },
        )()
    )

    is_fixed_strategy = (
        strategy.name.startswith("fixed_")
    )

    # --------------------------------------------------
    # Fixed strategy
    # --------------------------------------------------

    if is_fixed_strategy:

        # Fixed 必须把整个原文看成一个整体，
        # 不能提前按照 Markdown section 切。
        sections = [
            (
                document.section or "正文",
                document.text,
                0,
            )
        ]

        pieces = [
            _fixed_token_chunks(
                document.text,
                strategy.max_chars,
                strategy.overlap,
            )
        ]

        # 只用于 metadata：
        # 记录每个 Fixed chunk 覆盖到哪些业务 section。
        fixed_section_positions = (
            _markdown_section_positions(
                document.text
            )
            if document.file_type == "md"
            else []
        )

    # --------------------------------------------------
    # Markdown / TypeAware strategy
    # --------------------------------------------------

    else:
        sections = [
            (
                title,
                block,
                start,
            )
            for (
                title,
                block,
                start,
                _,
            ) in _section_blocks(
                document.text,
                top_level_only=(
                    strategy.name
                    == "type_aware"
                ),
            )
        ]

        pieces = [
            _split_oversized(block)
            for _, block, _ in sections
        ]

        fixed_section_positions = []

    chunks = []

    # --------------------------------------------------
    # Build DocumentChunk
    # --------------------------------------------------

    for section_index, (
        (
            section_title,
            _,
            section_start,
        ),
        section_pieces,
    ) in enumerate(
        zip(sections, pieces),
        1,
    ):

        for local_index, (
            start,
            end,
            text,
        ) in enumerate(
            section_pieces,
            1,
        ):

            absolute_start = (
                section_start + start
            )

            absolute_end = (
                section_start + end
            )

            # --------------------------------------------------
            # section title
            # --------------------------------------------------

            if is_fixed_strategy:
                title = (
                    _fixed_chunk_section_title(
                        document,
                        absolute_start,
                    )
                )
            else:
                title = section_title

            # --------------------------------------------------
            # covered_sections
            # --------------------------------------------------

            if is_fixed_strategy:
                covered_sections = (
                    _fixed_chunk_covered_sections(
                        document=document,
                        start_char=absolute_start,
                        end_char=absolute_end,
                        section_positions=(
                            fixed_section_positions
                        ),
                    )
                )
            else:
                covered_sections = []

            digest = content_hash_text(text)

            chunk_metadata = {
                **metadata,
                "chunk_strategy": (
                    strategy.name
                ),
                "token_count": (
                    token_count(text)
                ),
                "chunk_index": (
                    len(chunks) + 1
                ),
                "section_title": title,
            }

            # 只给 Fixed chunk 写入 covered_sections。
            #
            # Markdown / TypeAware 保持原来的 metadata 行为，
            # 防止无关实验变量发生变化。
            if is_fixed_strategy:
                chunk_metadata[
                    "covered_sections"
                ] = covered_sections

            chunk = DocumentChunk(
                chunk_id=_build_chunk_id(
                    document,
                    section_index=section_index,
                    chunk_index=local_index,
                    section=title,
                    start_char=absolute_start,
                    end_char=absolute_end,
                    content_hash=digest,
                    strategy=strategy,
                    chunker_version=(
                        chunker_version
                    ),
                ),
                document_id=document.document_id,
                source=document.source,
                text=text,
                file_type=document.file_type,
                page=document.page,
                section=title,
                start_char=absolute_start,
                end_char=absolute_end,
                content_hash=digest,
                chunker_version=chunker_version,
                metadata=chunk_metadata,
            )

            chunks.append(chunk)

    return chunks


def chunk_documents(
    documents,
    *,
    chunker_version=CHUNKER_VERSION,
    chunk_strategy=None,
):
    """
    批量 chunk。
    """
    return [
        chunk
        for document in documents
        for chunk in chunk_document(
            document,
            chunker_version=chunker_version,
            chunk_strategy=chunk_strategy,
        )
    ]