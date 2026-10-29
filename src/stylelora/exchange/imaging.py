"""Decode original bytes completely without resizing, re-encoding or stripping metadata."""

import warnings

from PIL import Image, UnidentifiedImageError

from .errors import ExchangeError
from .types import ImageInspection

EXTENSION_FORMAT = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP",
    ".bmp": "BMP", ".gif": "GIF", ".tif": "TIFF", ".tiff": "TIFF",
}


def inspect_image(path, options):
    if path.stat().st_size > options.max_image_bytes:
        raise ExchangeError("image:too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > options.max_pixels:
                    raise ExchangeError("image:pixel_limit")
                if image.format != EXTENSION_FORMAT.get(path.suffix.lower()):
                    raise ExchangeError("image:format_mismatch")
                if getattr(image, "n_frames", 1) != 1:
                    raise ExchangeError("image:multiframe_unsupported")
                image.verify()
            with Image.open(path) as image:
                image.load()
                metadata = None
                if image.format == "PNG":
                    metadata = {str(key): str(value) for key, value in image.info.items() if isinstance(value, str)}
                    if sum(len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in metadata.items()) > 1024 * 1024:
                        raise ExchangeError("image:metadata_limit")
                return ImageInspection(width, height, image.format, metadata)
    except Image.DecompressionBombError as exc:
        raise ExchangeError("image:pixel_limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ExchangeError("image:decode_failed") from exc
