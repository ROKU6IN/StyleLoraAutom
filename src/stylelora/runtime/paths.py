"""Shared serve/doctor locations; resolving them never creates directories."""
from dataclasses import dataclass
import os
from pathlib import Path

from .errors import ServiceError


@dataclass(frozen=True)
class ProductPaths:
    runtime: Path
    product: Path
    config: Path
    private: Path
    models: Path


def locations(runtime_dir=None, product_dir=None, *, environ=None):
    env = os.environ if environ is None else environ
    def base(key):
        value = env[key]
        if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
            raise ValueError()
        return Path(value)/'StyleLoraAutom'
    try:
        state = Path(runtime_dir) if runtime_dir is not None else base('LOCALAPPDATA')/'runtime'
        product = Path(product_dir) if product_dir is not None else state/'product' if runtime_dir is not None else base('APPDATA')/'tagging'
        custom = runtime_dir is not None or product_dir is not None
        config = product/'llm' if custom else base('APPDATA')
        private = product/'private' if custom else base('LOCALAPPDATA')
    except (KeyError, TypeError, ValueError):
        raise ServiceError(503, 'config:locations_unavailable') from None
    return ProductPaths(*(path.absolute() for path in (state, product, config, private, state.parent/'models'/'wd')))
