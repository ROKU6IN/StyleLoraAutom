"""F09 rule evaluation. No database, filesystem, HTTP, or implicit alias expansion."""
from dataclasses import dataclass

from ..project_db.types import Tag
from ..project_db.validation import normalize_tag
from ..tagging.plain import validate_plain_tag


def normalize_rule(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'condition', 'delete', 'write'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('Invalid rule schema')
    result = {'schema_version': 1}
    for name in ('condition', 'delete', 'write'):
        group = value[name]
        if group is None:
            result[name] = None
            continue
        if not isinstance(group, dict) or set(group) != {'mode', 'tags'} or group['mode'] not in ('all', 'any') or not isinstance(group['tags'], list) or len(group['tags']) > 10000:
            raise ValueError('Invalid rule group')
        tags = list(dict.fromkeys(validate_plain_tag(tag) for tag in group['tags']))
        result[name] = {'mode': group['mode'], 'tags': tags} if tags else None
    if not result['delete'] and not result['write']:
        raise ValueError('Empty rule')
    return result


@dataclass(frozen=True)
class Delta:
    before: tuple[Tag, ...]
    after: tuple[Tag, ...]
    removed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    matched: bool = False
    reason: str | None = None


def evaluate_rule(rule, before, condition_tags):
    before = tuple(before)
    keys = {tag.tag for tag in before}
    condition = rule['condition']
    if condition:
        if condition_tags is None:
            return Delta(before, before, reason='condition:missing_original')
        condition_keys = {normalize_tag(tag.tag) for tag in condition_tags}
        hits = [tag in condition_keys for tag in condition['tags']]
        if not (all(hits) if condition['mode'] == 'all' else any(hits)):
            return Delta(before, before, reason='condition:not_matched')
    delete = rule['delete']
    removed = ()
    if delete:
        hits = tuple(tag for tag in delete['tags'] if tag in keys)
        if not hits or delete['mode'] == 'all' and len(hits) != len(delete['tags']):
            return Delta(before, before, reason='delete:not_matched')
        removed = hits
    after = tuple(tag for tag in before if tag.tag not in removed)
    remaining = {tag.tag for tag in after}
    write = rule['write']
    reason = None
    if write:
        missing = tuple(tag for tag in write['tags'] if tag not in remaining)
        # Groups run in order: a failed write gate does not reverse a successful
        # delete. Only a failed delete prevents evaluating the following write.
        if write['mode'] == 'all' and len(missing) != len(write['tags']):
            if not removed:
                return Delta(before, before, reason='write:not_matched')
            reason = 'write:not_matched'
        else:
            after += tuple(Tag(tag) for tag in missing)
    after_keys = {tag.tag for tag in after}
    removed = tuple(tag.tag for tag in before if tag.tag not in after_keys)
    added = tuple(tag.tag for tag in after if tag.tag not in keys)
    # Delete then re-add is no semantic change; preserve order and provenance on no-op.
    if not removed and not added:
        return Delta(before, before, matched=True, reason='unchanged')
    return Delta(before, after, removed, added, True, reason)
