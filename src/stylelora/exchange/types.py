from dataclasses import dataclass

from ..project_db.types import JsonObject, OriginalInput


@dataclass(frozen=True)
class ImportOptions:
    encoding: str = "utf-8-sig"
    recursive: bool = True
    parser: str = "literal-tags-v1"
    max_image_bytes: int = 100 * 1024 * 1024
    max_pixels: int = 100_000_000
    max_sidecar_bytes: int = 1024 * 1024
    max_files: int = 10000


@dataclass(frozen=True)
class ManifestEntry:
    seq: int
    path: str
    image_sha256: str | None
    file_size: int
    sidecar_path: str | None
    sidecar_sha256: str | None
    preflight_error: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportManifest:
    entries: tuple[ManifestEntry, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SidecarResult:
    originals: tuple[OriginalInput, ...]
    warnings: tuple[str, ...] = ()
    encoding: str | None = None
    encoding_fallback: bool = False


@dataclass(frozen=True)
class ImageInspection:
    width: int
    height: int
    format: str
    png_meta: JsonObject | None


@dataclass(frozen=True)
class ImportItemResult:
    item_id: int
    path: str
    status: str
    image_id: int | None
    error: str | None
    warnings: tuple[str, ...] = ()
    encoding: str | None = None
    encoding_fallback: bool = False


@dataclass(frozen=True)
class ImportReport:
    source_id: int
    status: str
    selected: int
    imported: int
    skipped: int
    failed: int
    pending: int
    unresolved: int
    items: tuple[ImportItemResult, ...]
    warnings: tuple[str, ...] = ()
