import hashlib
from io import BytesIO

from PIL import Image, ImageOps, __version__ as pillow_version

from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import images
from ..project_db.storage import atomic_bytes
from ..project_db.validation import digest, relative_path
from ..exchange.imaging import EXTENSION_FORMAT
from ..exchange.common import read_bounded
from ..runtime.errors import ServiceError

MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "BMP": "image/bmp", "GIF": "image/gif", "TIFF": "image/tiff"}


def has_alpha(project, image):
    if image.file_missing:
        return None
    try:
        with Image.open(relative_path(project.root, "images/" + image.filename)) as decoded:
            return "A" in decoded.getbands() or "transparency" in decoded.info
    except (OSError, ValueError):
        return None


def read_media(context, image_id, *, size=None):
    from ..exchange.common import project_operation
    with project_operation(context.handle):
        return _read_media(context, image_id, size=size)


def _read_media(context, image_id, *, size=None):
    project = context.handle
    with read_snapshot(project) as db:
        image = images.get_image(db, image_id)
    path = relative_path(project.root, "images/" + image.filename)
    if image.file_missing or not path.is_file():
        if not image.file_missing:
            submit_write(project, lambda db: images.set_file_missing(db, image_id, missing=True)).result()
        raise ServiceError(410, "image:file_missing")
    if path.stat().st_size > 100*1024*1024:
        raise ServiceError(413, "image:too_large")
    try:
        data = read_bounded(path, 100*1024*1024)
    except FileNotFoundError as exc:
        submit_write(project, lambda db: images.set_file_missing(db, image_id, missing=True)).result()
        raise ServiceError(410, "image:file_missing") from exc
    if len(data) != image.file_size or hashlib.sha256(data).hexdigest() != image.sha256:
        raise ServiceError(409, "image:hash_mismatch")
    try:
        with Image.open(BytesIO(data)) as original:
            if original.format != EXTENSION_FORMAT.get(path.suffix) or original.width*original.height > 100_000_000:
                raise ValueError()
            original.verify()
        if size is None:
            return data, MIME[EXTENSION_FORMAT[path.suffix]], image.sha256
        if size not in (256, 512):
            raise ServiceError(422, "image:invalid_thumbnail_size")
        cache_key = digest({"sha256": image.sha256, "size": size, "transform": "exif-white-webp-v1", "pillow": pillow_version})
        cached = relative_path(project.root, f"cache/thumbnails/{cache_key}.webp")
        # Per-project serialization prevents simultaneous encodes and partial cache reads.
        with project._lifecycle:
            if not cached.exists():
                with Image.open(BytesIO(data)) as original:
                    original.load()
                    display = ImageOps.exif_transpose(original).convert("RGBA")
                    display.thumbnail((size, size), Image.Resampling.LANCZOS)
                    background = Image.new("RGB", display.size, "white")
                    background.paste(display, mask=display.getchannel("A"))
                    encoded = BytesIO()
                    background.save(encoded, format="WEBP", quality=85)
                cached.parent.mkdir(parents=True, exist_ok=True)
                atomic_bytes(cached, encoded.getvalue())
            output = cached.read_bytes()
        return output, "image/webp", cache_key
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ServiceError(422, "image:decode_failed") from exc
