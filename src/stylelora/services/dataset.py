from dataclasses import replace

from ..annotations.editing import _edit, _view
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import annotations, images, schemes, sources
from ..project_db.rules import validate_read_set
from ..project_db.storage import atomic_json
from ..project_db.types import CurrentRef, Tag
from ..project_db.validation import LAYERS, digest, primitive, text
from ..tagging.plain import validate_plain_tag
from ..runtime.contracts import Versions
from ..runtime.errors import ServiceError
from .media import has_alpha, read_media


class DatasetService:
    def __init__(self, runtime):
        self.runtime = runtime
        from .exchange import ExchangeService
        self.exchange = ExchangeService(runtime, self)

    def key(self, context, kind, identifier):
        return f"{context.project_id}:{kind}:{identifier}"

    def manual_rating(self, context, image_id, rating, expected):
        from ..project_db.types import RatingValue
        from ..project_db.validation import utc_now
        def write(db):
            row = images.get_image(db, image_id)
            Versions.require(expected, self.image_version(context, row))
            if row.file_missing:
                raise ServiceError(409, 'image:file_missing')
            images.set_rating(db, image_id, expected=images.rating_value(row), value=RatingValue(rating, 'manual', True, utc_now()))
        submit_write(context.handle, write).result()
        self.runtime.emit('image.changed', {'image_id': image_id}, project_id=context.project_id, resource={'kind': 'image', 'id': image_id})
        return self.get_image(context, image_id)

    def image_version(self, context, image):
        return self.runtime.versions.get(self.key(context, "image", image.image_id),
                                        [image.review_status, image.file_missing, image.rating, image.rating_source, image.rating_verified, image.rated_at])

    def image_view(self, context, db, image, *, metadata=False):
        value = primitive(image)
        if not metadata:
            value.pop("png_meta_json", None)
        coverage = {}
        for layer in LAYERS:
            row = annotations.get_current(db, image.image_id, layer, "tags")
            coverage[layer] = bool(row and row.content)
        value.update(version=self.image_version(context, image),
                     annotation_coverage=coverage, original_url=f"/api/projects/{context.project_id}/images/{image.image_id}/content",
                     thumbnail_url=f"/api/projects/{context.project_id}/images/{image.image_id}/thumbnail")
        return value

    def enrich_image(self, context, value):
        # Image decoding happens after the short SQLite snapshot has been released.
        from types import SimpleNamespace
        value["has_alpha"] = has_alpha(context.handle, SimpleNamespace(**value))
        return value

    def get_image(self, context, image_id, metadata=False):
        with read_snapshot(context.handle) as db:
            value = self.image_view(context, db, images.get_image(db, image_id), metadata=metadata)
        return self.enrich_image(context, value)

    def _filter(self, query):
        return images.workbench_where(query)

    def list_images(self, context, query, *, limit=50, cursor=None, sort="id", around_id=None):
        if not 1 <= limit <= 1000 or sort not in ("id", "-id") or cursor and around_id is not None:
            raise ServiceError(422, "input:invalid")
        where, args = self._filter(query)
        identity = digest([context.project_id, "images", query, sort])
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        direction = "ASC" if sort == "id" else "DESC"
        with read_snapshot(context.handle) as db:
            upper = state["upper"] if state else db.execute("SELECT COALESCE(MAX(image_id),0) FROM images").fetchone()[0]
            # ID list and page share this short snapshot; rows and media are not held for client scrolling.
            ids = [row[0] for row in db.execute(f"SELECT image_id FROM images WHERE ({where}) AND image_id<=? ORDER BY image_id {direction}", (*args, upper))]
            anchor = None
            if around_id is not None:
                if around_id not in ids:
                    raise ServiceError(404, "resource:not_found")
                anchor = ids.index(around_id)
                start = anchor // limit * limit
            elif state:
                start = next((index for index, value in enumerate(ids) if (value > state["last"] if sort == "id" else value < state["last"])), len(ids))
            else:
                start = 0
            chosen = ids[start:start+limit]
            values = [self.image_view(context, db, images.get_image(db, image_id)) for image_id in chosen]
        more = start+len(values) < len(ids)
        next_cursor = self.runtime.cursors.encode({"identity": identity, "upper": upper, "last": chosen[-1]}) if more else None
        return {"items": [self.enrich_image(context, value) for value in values], "next_cursor": next_cursor, "has_more": more, "limit": limit, "upper_id": upper,
                "total": len(ids), "start_index": start, "anchor_index": anchor}

    def annotation_view(self, context, db, image_id):
        view = _view(db, image_id)
        result = primitive(view)
        result["tags"] = [{"tag": value, "conf": None} for value in view.tags]
        result["version"] = self.runtime.versions.get(self.key(context, "annotations", image_id), view.refs)
        return result

    @staticmethod
    def normalize_query(query):
        return {"source_ids": sorted(set(query.get("source_ids") or [])),
                "review_statuses": sorted(set(query.get("review_statuses") or [])),
                "rating": sorted(set(query.get("rating") or [])),
                "file_missing": query.get("file_missing"), "q": query.get("q") or None}

    def query_ids(self, db, query):
        where, args = self._filter(query)
        return [row[0] for row in db.execute(f"SELECT image_id FROM images WHERE {where} ORDER BY image_id LIMIT 5001", args)]

    def image_stats(self, context, query):
        where, args = self._filter(query)
        with read_snapshot(context.handle) as db:
            total = db.execute(f"SELECT COUNT(*) FROM images WHERE {where}", args).fetchone()[0]
            groups = [{"by": column, "buckets": [{"key": row[0], "count": row[1]} for row in
                db.execute(f"SELECT {column},COUNT(*) FROM images WHERE {where} GROUP BY {column} ORDER BY {column}", args)]}
                for column in ("review_status", "rating", "source_id")]
        return {"total": total, "groups": groups}

    def preview_review(self, context, query):
        query = self.normalize_query(query)
        with read_snapshot(context.handle) as db:
            ids = self.query_ids(db, query)
            if len(ids) > 5000:
                raise ServiceError(413, "batch:too_large")
            expected = {str(i): self.image_version(context, images.get_image(db, i)) for i in ids}
        return self.runtime.previews.add(context.project_id, "review", {"total": len(ids), "query": query},
                                         {"ids": ids, "expected_versions": expected, "query": query})

    def get_annotations(self, context, image_id):
        with read_snapshot(context.handle) as db:
            return self.annotation_view(context, db, image_id)

    def edit(self, context, image_id, value, expected):
        refs = tuple(CurrentRef(**ref) for ref in value["read_set"])
        if value["tags"] is None and not value["write_nl"]:
            raise ServiceError(422, "input:no_changes")
        def write(db):
            Versions.require(expected, self.annotation_view(context, db, image_id)["version"])
            desired = tuple(Tag(**tag) for tag in value["tags"]) if value["tags"] is not None else None
            result = _edit(db, image_id, desired, value["natural_language"], value["write_nl"], refs)
            view = self.annotation_view(context, db, image_id)
            if result.changed:
                def committed():
                    self.runtime.versions.bump(self.key(context, "annotations", image_id))
                    view["version"] = self.runtime.versions.get(self.key(context, "annotations", image_id), view["refs"])
                db.after_commit.append(committed)
            return result, view
        result, view = submit_write(context.handle, write).result()
        if result.changed:
            self.runtime.emit("annotations.changed", {"image_ids": [image_id], "fields": ["manual"], "annotation_ids": list(result.annotation_ids)},
                              project_id=context.project_id, resource={"kind": "image", "id": image_id}, version=view["version"])
        return {**primitive(result), "annotation": view, "version": view["version"]}

    def bulk_edit(self, context, value):
        ids = sorted(set(value["ids"]))
        if len(ids) > 5000:
            raise ServiceError(413, "batch:too_large")
        expected = value["expected_versions"]
        if set(expected) != {str(i) for i in ids}:
            raise ServiceError(428, "concurrency:precondition_required")
        refs = tuple(CurrentRef(**ref) for ref in value["read_set"])
        add = tuple(dict.fromkeys(validate_plain_tag(tag) for tag in value["add"]))
        remove = {validate_plain_tag(tag) for tag in value["remove"]}
        if set(add) & remove:
            raise ServiceError(422, "input:overlapping_changes")
        def write(db):
            validate_read_set(db, refs, ids)
            views = {i: self.annotation_view(context, db, i) for i in ids}
            for i in ids:
                Versions.require(expected[str(i)], views[i]["version"])
            results, updated = [], []
            for i in ids:
                tags = [tag["tag"] for tag in views[i]["tags"] if tag["tag"] not in remove]
                tags.extend(tag for tag in add if tag not in tags)
                result = _edit(db, i, tuple(Tag(tag) for tag in tags), None, False, tuple(ref for ref in refs if ref.image_id == i))
                view = self.annotation_view(context, db, i)
                if result.changed:
                    def committed(i=i, view=view):
                        self.runtime.versions.bump(self.key(context, "annotations", i))
                        view["version"] = self.runtime.versions.get(self.key(context, "annotations", i), view["refs"])
                    db.after_commit.append(committed)
                results.append(result)
                updated.append(view)
            return results, updated
        results, updated = submit_write(context.handle, write).result()
        changed = [result.image_id for result in results if result.changed]
        if changed:
            self.runtime.emit("annotations.changed", {"image_ids": changed[:500], "invalidate_scope": len(changed)>500, "fields": ["manual"], "annotation_ids": []}, project_id=context.project_id)
        return {"items": updated}

    def review(self, context, value):
        selected_query = value.get("query")
        if selected_query is not None:
            if value.get("ids") is not None or value.get("expected_versions") is not None or value.get("targets") is not None or value.get("target") is None:
                raise ServiceError(422, "input:invalid_review_scope")
            if any(value.get(key) is None for key in ("preview_id", "digest", "expected_total")):
                raise ServiceError(428, "concurrency:precondition_required")
            preview, frozen = self.runtime.previews.get(context.project_id, "review", value["preview_id"], value["digest"])
            selected_query = self.normalize_query(selected_query)
            if selected_query != frozen["query"] or value["expected_total"] != preview["total"]:
                raise ServiceError(409, "concurrency:version_conflict")
            ids, expected = frozen["ids"], frozen["expected_versions"]
        else:
            if not value.get("ids") or any(value.get(key) is not None for key in ("preview_id", "digest", "expected_total")):
                raise ServiceError(422, "input:invalid_review_scope")
            ids = sorted(set(value["ids"]))
            expected = value["expected_versions"] or {}
        if len(ids) > 5000:
            raise ServiceError(413, "batch:too_large")
        if set(expected) != {str(i) for i in ids}:
            raise ServiceError(428, "concurrency:precondition_required")
        target, targets = value.get("target"), value.get("targets")
        if (target is None) == (targets is None) or targets is not None and set(targets) != set(expected):
            raise ServiceError(422, "input:invalid_review_targets")
        def write(db):
            if selected_query is not None and self.query_ids(db, selected_query) != ids:
                raise ServiceError(409, "concurrency:version_conflict")
            before = {i: images.get_image(db, i) for i in ids}
            before_versions = {i: self.image_version(context, image) for i, image in before.items()}
            for i, image in before.items():
                Versions.require(expected[str(i)], self.image_version(context, image))
            changed, updated = [], []
            for i, image in before.items():
                result = images.set_review(db, i, expected=image.review_status, target=target or targets[str(i)])
                if result.changed:
                    changed.append(i)
                current = images.get_image(db, i)
                view = self.image_view(context, db, current)
                if result.changed:
                    def committed(i=i, view=view, current=current):
                        self.runtime.versions.bump(self.key(context, "image", i))
                        view["version"] = self.image_version(context, current)
                    db.after_commit.append(committed)
                updated.append({"image": view, "old_review_status": image.review_status, "old_version": before_versions[i]})
            return changed, updated
        changed, updated = submit_write(context.handle, write).result()
        for item in updated:
            self.enrich_image(context, item["image"])
        result = {"items": updated}
        if changed:
            self.runtime.emit("image.updated", {"image_ids": changed[:500], "invalidate_scope": len(changed)>500, "fields": ["review_status"], "annotation_ids": []}, project_id=context.project_id)
        return result

    def update_project(self, context, value, expected):
        def write(db):
            Versions.require(expected, self.runtime.project_view(context)["version"])
            metadata = replace(context.handle.metadata, **{key: val for key, val in value.items() if val is not None})
            text(metadata.name, "project name", nonempty=True)
            text(metadata.trigger_word, "trigger word")
            if metadata != context.handle.metadata:
                atomic_json(context.handle.root / "project.json", metadata)
                context.handle.metadata = metadata
                db.after_commit.append(lambda: self.runtime.versions.bump(context.project_id + ":project"))
            result = self.runtime.project_view(context)
            db.after_commit.append(lambda: result.update(version=self.runtime.versions.get(context.project_id + ":project", metadata)))
            return result
        result = submit_write(context.handle, write).result()
        self.runtime.remember_project(context, touch=False)
        result.update(known_id=context.known_id, recent_warning=context.recent_warning)
        return result

    def source_view(self, context, db, source_id):
        source = sources.get_source(db, source_id)
        value = primitive(source)
        value["counts"] = sources.count_item_states(db, source_id)
        value["version"] = self.runtime.versions.get(self.key(context, "source", source_id), [source.status, source.config_json, source.name, source.cost_policy, source.locked,
                                                    source.style_prompt, source.negative_prompt, source.target_count])
        value["runtime"] = {"cycle_id": None, "pause_reason": None, "restore_pending": False, "in_flight": value["counts"]["sent"]}
        if source.type == 'nai':
            value['runtime']['pause_reason'] = self.runtime.generation_sources.reasons.get((context.project_id, source_id))
        return value

    def get_source(self, context, source_id):
        with read_snapshot(context.handle) as db:
            return self.source_view(context, db, source_id)

    def item_view(self, context, item):
        value = primitive(item)
        value['version'] = self.runtime.versions.get(self.key(context, 'items', item.item_id), value)
        return value

    def get_item(self, context, item_id):
        with read_snapshot(context.handle) as db:
            return self.item_view(context, sources.get_item(db, item_id))

    def retry_item(self, context, item_id, expected_attempt, expected):
        from ..project_db import commands
        committed_result = []
        def write(db):
            item = sources.get_item(db, item_id)
            source = sources.get_source(db, item.source_id)
            Versions.require(expected, self.item_view(context, item)['version'])
            if source.status == 'canceled':
                raise ServiceError(409, 'source:canceled')
            replay = item.status == 'pending' and item.attempts == expected_attempt + 1
            if not replay and item.status != 'failed':
                raise ServiceError(409, 'item:not_failed')
            if not replay and item.attempts != expected_attempt:
                raise ServiceError(409, 'item:attempt_conflict')
            result = commands.retry_failed_item(db, item_id, expected_attempt=expected_attempt)
            value = primitive(result.value)
            def committed():
                committed_result.append(True)
                if result.changed:
                    self.runtime.versions.bump(self.key(context, 'items', item_id))
                    self.runtime.versions.bump(self.key(context, 'source', item.source_id))
                value['version'] = self.runtime.versions.get(self.key(context, 'items', item_id), primitive(result.value))
            db.after_commit.append(committed)
            return value
        try:
            value = submit_write(context.handle, write).result()
            source = self.get_source(context, value['source_id'])
            self.runtime.emit('source.changed', {'source_id': source['source_id'], 'status': source['status'], 'counts': source['counts']},
                project_id=context.project_id, resource={'kind': 'source', 'id': source['source_id']}, version=source['version'])
        except Exception:
            if committed_result:
                raise ServiceError(503, 'source:retry_outcome_unknown', outcome='unknown') from None
            raise
        return value

    def list_resource(self, context, kind, *, limit=50, cursor=None, source_id=None):
        if not 1 <= limit <= 1000:
            raise ServiceError(422, "input:invalid")
        identity = digest([context.project_id, kind, source_id])
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        table, column = {"sources": ("sources", "source_id"), "items": ("source_items", "item_id"),
                         "import-schemes": ("import_schemes", "scheme_id"), "export-schemes": ("export_schemes", "scheme_id")}[kind]
        where, args = ("source_id=?", [source_id]) if kind == "items" else ("1=1", [])
        with read_snapshot(context.handle) as db:
            upper = state["upper"] if state else db.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table} WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"SELECT {column} FROM {table} WHERE {where} AND {column}>? AND {column}<=? ORDER BY {column} LIMIT ?", (*args, state["last"] if state else 0, upper, limit+1)).fetchall()
            values = []
            for row in rows[:limit]:
                if kind == "sources":
                    value = self.source_view(context, db, row[0])
                elif kind == "items":
                    value = self.item_view(context, sources.get_item(db, row[0]))
                else:
                    from .schemes import view as scheme_view
                    value = scheme_view(self.runtime,context,(schemes.get_import_scheme if kind == "import-schemes" else schemes.get_export_scheme)(db, row[0]),kind=='import-schemes')
                value.setdefault("version", self.runtime.versions.get(self.key(context, kind, row[0]), value))
                values.append(value)
        more = len(rows) > limit
        return {"items": values, "limit": limit, "has_more": more, "upper_id": upper,
                "next_cursor": self.runtime.cursors.encode({"identity": identity, "upper": upper, "last": rows[limit-1][0]}) if more else None}

    def media(self, context, image_id, size=None):
        return read_media(context, image_id, size=size)
