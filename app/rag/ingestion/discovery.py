from hashlib import sha256
from pathlib import Path

from app.rag.models import (
    FileDiscoveryResult,
    KnowledgeSource,
    UnsupportedKnowledgeSource,
    normalize_source,
)


SUPPORTED_TEXT_SUFFIXES = {".md", ".txt"}
SUPPORTED_PDF_SUFFIXES = {".pdf"}
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SUPPORTED_SUFFIXES = (
    SUPPORTED_TEXT_SUFFIXES
    | SUPPORTED_PDF_SUFFIXES
    | SUPPORTED_IMAGE_SUFFIXES
)


def hash_file(file_path: Path) -> str:
    digest = sha256()

    with file_path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def discover_files(
    directory: Path,
    *,
    recursive: bool = True,
) -> FileDiscoveryResult:
    if not directory.exists():
        return FileDiscoveryResult()

    candidates = directory.rglob("*") if recursive else directory.iterdir()
    files = sorted(
        (path for path in candidates if path.is_file()),
        key=lambda path: path.relative_to(directory).as_posix(),
    )
    result = FileDiscoveryResult()

    for file_path in files:
        source = normalize_source(file_path.relative_to(directory).as_posix())
        suffix = file_path.suffix.lower()

        if suffix not in SUPPORTED_SUFFIXES:
            result.unsupported.append(
                UnsupportedKnowledgeSource(
                    path=file_path,
                    source=source,
                    file_type=suffix.lstrip("."),
                )
            )
            continue

        result.sources.append(
            KnowledgeSource(
                path=file_path,
                source=source,
                file_type=suffix.lstrip("."),
                content_hash=hash_file(file_path),
                size=file_path.stat().st_size,
            )
        )

    return result
