from io import BytesIO
import math

from PIL import Image, ImageOps, ImageDraw

from .types import LlmFailure

TIERS = ((2_000_000, 92), (1_600_000, 88), (1_000_000, 84))


def encode_image(source, tier=0):
    try:
        if not isinstance(source, bytes) or len(source) > 32 * 1024 * 1024:
            raise ValueError()
        with Image.open(BytesIO(source)) as original:
            if original.width * original.height > 80_000_000:
                raise ValueError()
            picture = ImageOps.exif_transpose(original).convert('RGBA')
        base = Image.new('RGBA', picture.size, 'white')
        picture = Image.alpha_composite(base, picture).convert('RGB')
        pixels, quality = TIERS[tier]
        ratio = min(1, math.sqrt(pixels / (picture.width * picture.height)))
        if ratio < 1:
            picture = picture.resize((max(1, int(picture.width * ratio)), max(1, int(picture.height * ratio))), Image.Resampling.LANCZOS)
        output = BytesIO()
        picture.save(output, 'JPEG', quality=quality)
        return output.getvalue()
    except Exception:
        raise LlmFailure('invalid_image', outcome='not_sent') from None


def test_image():
    picture = Image.new('RGB', (256, 128), 'white')
    draw = ImageDraw.Draw(picture)
    draw.rectangle((20, 25, 95, 100), fill='red')
    draw.ellipse((155, 25, 230, 100), fill='blue')
    output = BytesIO()
    picture.save(output, 'PNG')
    return output.getvalue()
