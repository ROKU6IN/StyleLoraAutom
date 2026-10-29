from collections import Counter
import threading

from ..annotations.editing import _view
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import annotations, images, operations
from ..project_db.rules import make_rule_plan, apply_rule, undo_rule, MAX_RULE_BYTES
from ..project_db.types import RuleImageChange, Scope, Tag
from ..project_db.validation import digest, encode, primitive
from ..rules.engine import evaluate_rule, normalize_rule
from ..runtime.contracts import Versions
from ..runtime.errors import ServiceError
from ..tagging.plain import validate_plain_tag


class CleaningService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.applied = {}

    def resolve(self, context, db, scope, *, rule=False):
        selected, query = scope.get('selected_ids'), scope.get('filter')
        if (selected is None) == (query is None):
            raise ServiceError(422, 'scope:invalid')
        if selected is not None:
            ids = sorted(set(selected))
            if len(ids) > 5000:
                raise ServiceError(413, 'batch:too_large')
            for image_id in ids:
                images.get_image(db, image_id)
        else:
            query = self.runtime.dataset.normalize_query(query)
            if rule and not query['review_statuses']:
                query['review_statuses'] = ['keep', 'unreviewed']
            ids = self.runtime.dataset.query_ids(db, query)
        if len(ids) > 5000:
            raise ServiceError(413, 'batch:too_large')
        return ids

    def changed(self, context, ids, field):
        if ids:
            self.runtime.emit('annotations.changed', {'image_ids': ids[:500], 'invalidate_scope': len(ids)>500,
                'fields': [field], 'annotation_ids': []}, project_id=context.project_id)

    def preview(self, context, value):
        rule = normalize_rule(value['rule'])
        catalog_revision = self.runtime.catalog.revision()
        with read_snapshot(context.handle) as db:
            ids = self.resolve(context, db, value['scope'], rule=True)
            changes, examples, reasons = [], [], Counter()
            matched, fallback, missing = 0, 0, 0
            for image_id in ids:
                view = _view(db, image_id)
                original = annotations.get_current(db, image_id, 'original', 'tags')
                wd = annotations.get_current(db, image_id, 'wd', 'tags') if original is None else None
                condition = original or wd
                fallback += int(original is None and wd is not None)
                missing += int(original is None)
                delta = evaluate_rule(rule, tuple(Tag(t) for t in view.tags), condition.content if condition else None)
                matched += int(delta.matched)
                if delta.reason:
                    reasons[delta.reason] += 1
                if delta.removed or delta.added:
                    previous = annotations.get_current(db, image_id, 'rule', 'tags')
                    changes.append(RuleImageChange(image_id, delta.after, previous.annotation_id if previous else None))
                if len(examples) < 100:
                    examples.append({'image_id': image_id, **primitive(delta), 'condition_source': 'original' if original else 'wd' if wd else None})
            plan = make_rule_plan(db, rule=rule, scope=Scope(selected_ids=tuple(ids)), changes=changes)
        if len(encode(plan).encode()) > MAX_RULE_BYTES:
            raise ServiceError(413, 'batch:too_large')
        content = {'rule': rule, 'total': len(ids), 'matched_count': matched, 'changed_count': len(changes),
                   'examples': examples, 'reasons': dict(reasons), 'wd_fallback_count': fallback, 'missing_original_count': missing,
                   'plan_digest': digest([plan, catalog_revision]), 'catalog_revision': catalog_revision}
        return self.runtime.previews.add(context.project_id, 'rule', content, {'plan': plan, 'catalog_revision': catalog_revision})

    def apply(self, context, value, expected):
        preview, frozen = self.runtime.previews.get(context.project_id, 'rule', value['preview_id'], value['digest'])
        Versions.require(expected, preview['version'])
        with self.lock, self.runtime.catalog.lock:
            key = (context.project_id, value['preview_id'])
            if key in self.applied:
                return self.applied[key]
            if frozen['catalog_revision'] != self.runtime.catalog.revision():
                raise ServiceError(409, 'rule:preview_stale')
            def write(db):
                result = apply_rule(db, frozen['plan'])
                if result.affected_count:
                    for change in frozen['plan'].changes:
                        db.after_commit.append(lambda i=change.image_id: self.runtime.versions.bump(self.runtime.dataset.key(context, 'annotations', i)))
                return primitive(result)
            result = submit_write(context.handle, write).result()
            self.applied[key] = result
            with self.runtime.previews.lock:
                self.applied = {k:v for k,v in self.applied.items() if k[1] in self.runtime.previews.values}
        self.changed(context, [change.image_id for change in frozen['plan'].changes] if result['affected_count'] else [], 'rule')
        return result

    def operation_view(self, context, row):
        value = primitive(row)
        value.pop('changes_json')
        value['version'] = self.runtime.versions.get(self.runtime.dataset.key(context, 'operation', row.op_id), [row.status, row.undone_at])
        return value

    def log(self, context, *, limit=50, cursor=None, status=None):
        if status not in (None, 'applied', 'undone'):
            raise ServiceError(422, 'input:invalid')
        identity = digest([context.project_id, 'rule-log', status])
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        with read_snapshot(context.handle) as db:
            upper = state['upper'] if state else db.execute('SELECT COALESCE(MAX(op_id),0) FROM operation_log').fetchone()[0]
            rows = db.execute('SELECT op_id FROM operation_log WHERE op_id>? AND op_id<=? AND (? IS NULL OR status=?) ORDER BY op_id LIMIT ?',
                              (state['last'] if state else 0, upper, status, status, limit+1)).fetchall()
            values = [self.operation_view(context, operations.get_operation(db, row[0])) for row in rows[:limit]]
        more = len(rows)>limit
        return {'items': values, 'limit': limit, 'upper_id': upper, 'has_more': more,
                'next_cursor': self.runtime.cursors.encode({'identity': identity, 'upper': upper, 'last': values[-1]['op_id']}) if more else None}

    def detail(self, context, op_id, *, limit=50, cursor=None):
        with read_snapshot(context.handle) as db:
            row = operations.get_operation(db, op_id)
            view = self.operation_view(context, row)
        identity = digest([context.project_id, 'rule-changes', op_id, view['version']])
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        start = state['last'] if state else 0
        values = primitive(row.changes_json[start:start+limit])
        more = start+len(values)<len(row.changes_json)
        return {**view, 'items': values, 'limit': limit, 'has_more': more, 'upper_id': len(row.changes_json),
                'next_cursor': self.runtime.cursors.encode({'identity': identity, 'upper': len(row.changes_json), 'last': start+limit}) if more else None}

    def undo(self, context, op_id, expected):
        def write(db):
            row = operations.get_operation(db, op_id)
            Versions.require(expected, self.operation_view(context, row)['version'])
            result = undo_rule(db, op_id)
            ids = [change['image_id'] for change in row.changes_json] if result.changed else []
            for image_id in ids:
                db.after_commit.append(lambda i=image_id: self.runtime.versions.bump(self.runtime.dataset.key(context, 'annotations', i)))
            return self.operation_view(context, result.value), ids
        result, ids = submit_write(context.handle, write).result()
        self.changed(context, ids, 'rule.undo')
        return result

    def bulk_preview(self, context, value):
        add = list(dict.fromkeys(validate_plain_tag(tag) for tag in value['add']))
        remove = list(dict.fromkeys(validate_plain_tag(tag) for tag in value['remove']))
        if set(add)&set(remove) or not add and not remove:
            raise ServiceError(422, 'input:overlapping_changes')
        with read_snapshot(context.handle) as db:
            ids = self.resolve(context, db, value['scope'])
            views = [self.runtime.dataset.annotation_view(context, db, i) for i in ids]
        payload = {'ids': ids, 'add': add, 'remove': remove, 'read_set': [r for view in views for r in view['refs']],
                   'expected_versions': {str(view['image_id']):view['version'] for view in views}}
        if len(encode(payload).encode()) > MAX_RULE_BYTES:
            raise ServiceError(413, 'batch:too_large')
        examples, changed = [], 0
        for view in views:
            before = [tag['tag'] for tag in view['tags']]
            after = [tag for tag in before if tag not in remove]
            after.extend(tag for tag in add if tag not in after)
            changed += int(before != after)
            if len(examples)<100:
                examples.append({'image_id': view['image_id'], 'before': before, 'after': after})
        return self.runtime.previews.add(context.project_id, 'bulk-manual', {'total': len(ids), 'changed_count': changed,
            'examples': examples, 'plan_digest': digest(payload)}, payload)

    def bulk_apply(self, context, value, expected):
        preview, payload = self.runtime.previews.get(context.project_id, 'bulk-manual', value['preview_id'], value['digest'])
        Versions.require(expected, preview['version'])
        with self.lock:
            key = (context.project_id, value['preview_id'])
            if key in self.applied:
                return self.applied[key]
            result = self.runtime.dataset.bulk_edit(context, payload) if payload['ids'] else {'items': []}
            self.applied[key] = {'total': preview['total'], 'changed_count': preview['changed_count'], 'image_ids': [row['image_id'] for row in result['items']]}
            with self.runtime.previews.lock:
                self.applied = {k:v for k,v in self.applied.items() if k[1] in self.runtime.previews.values}
            return self.applied[key]

    def stats(self, context, value):
        catalog = self.runtime.catalog.snapshot()
        with read_snapshot(context.handle) as db:
            ids = self.resolve(context, db, value['scope'])
            counts = Counter(tag for image_id in ids for tag in set(_view(db, image_id).tags))
        items = [{'tag': tag, 'count': count, **catalog.hit(tag)} for tag, count in sorted(counts.items(), key=lambda pair:(-pair[1],pair[0]))]
        return {'total_images': len(ids), 'unique_tags': len(items), 'catalog_revision': catalog.revision,
                'items': items[:1000], 'truncated': len(items)>1000}
