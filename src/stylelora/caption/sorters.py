"""Conservative local R4 ordering with frozen, inspectable classification inputs."""
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import json
from types import MappingProxyType
from typing import Mapping

from ..exchange.errors import ExchangeError
from ..project_db.errors import UnsupportedSorter
from ..project_db.validation import digest
from ..tagging.plain import validate_plain_tag

GROUPS = ('subject', 'action_expression', 'clothing', 'composition', 'scene', 'lighting')
VERSION = 'pyramid-local-v1'
CATEGORY_GROUPS = {f'pyramid:{name}': name for name in GROUPS}
GROUP_ORDER = {name: index for index, name in enumerate((*GROUPS, 'unclassified'))}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate sorting field')
        result[key] = value
    return result


@dataclass(frozen=True)
class PyramidSorter:
    ranks: Mapping[str, int]
    aliases: Mapping[str, str]
    categories: Mapping[str, str]
    rules_sha256: str
    skill_hash: str
    catalog_revision: str | None
    catalog_sha256: str | None

    def category(self, tag):
        canonical = self.aliases.get(tag, tag)
        category = self.categories.get(canonical)
        if category == 'character':
            return 'subject'
        if category in CATEGORY_GROUPS:
            return CATEGORY_GROUPS[category]
        if category not in (None, 'general'):
            return 'unclassified'
        rank = self.ranks.get(canonical)
        return GROUPS[rank] if rank is not None else 'unclassified'

    def sort(self, tags):
        return tuple(sorted(tags, key=lambda tag: GROUP_ORDER[self.category(tag)]))

    def counts(self, tags):
        counts = Counter(self.category(tag) for tag in tags)
        return {name: counts[name] for name in (*GROUPS, 'unclassified')}

    def metadata(self):
        return {'name': 'prompt_pyramid', 'version': VERSION, 'rules_sha256': self.rules_sha256,
                'skill_hash': self.skill_hash, 'catalog_revision': self.catalog_revision,
                'catalog_sha256': self.catalog_sha256, 'category_order': [*GROUPS, 'unclassified']}


def load_pyramid(catalog=None):
    """No cache: a resource change must invalidate a previously previewed plan."""
    try:
        root = files('stylelora.caption').joinpath('data')
        raw = root.joinpath('prompt_pyramid_v1.json').read_bytes()
        contract = root.joinpath('prompt_pyramid_v1.md').read_bytes()
        if len(raw) > 64 * 1024 or not contract or len(contract) > 64 * 1024:
            raise ValueError('Sorting resource size')
        contract.decode('utf-8')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'version', 'groups'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['version'] != VERSION or not isinstance(value['groups'], dict)
                or tuple(value['groups']) != GROUPS):
            raise ValueError('Sorting resource schema')
        ranks = {}
        for index, name in enumerate(GROUPS):
            tags = value['groups'][name]
            if not isinstance(tags, list) or not 1 <= len(tags) <= 512:
                raise ValueError('Sorting resource group')
            for tag in tags:
                if not isinstance(tag, str) or len(tag) > 256 or validate_plain_tag(tag) != tag or tag in ranks:
                    raise ValueError('Sorting tag must be unique and normalized')
                ranks[tag] = index
        aliases = dict(catalog.entries) if catalog else {}
        categories = dict(catalog.categories) if catalog else {}
        return PyramidSorter(MappingProxyType(ranks), MappingProxyType(aliases), MappingProxyType(categories),
                             sha256(raw).hexdigest(), sha256(contract).hexdigest(), catalog.revision if catalog else None,
                             digest({'aliases': aliases, 'categories': categories}) if catalog else None)
    except (OSError, ValueError, TypeError, ExchangeError) as exc:
        raise UnsupportedSorter('Local pyramid sorting resources are unavailable or invalid') from exc


def pyramid_capability():
    try:
        return {'available': True, **load_pyramid().metadata()}
    except UnsupportedSorter:
        return {'available': False, 'reason': 'sorter:unsupported'}
