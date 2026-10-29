import threading
from dataclasses import replace
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from ..exchange.common import input_root
from ..exchange.errors import ExchangeError, ExportInterrupted
from ..exchange.imaging import inspect_image
from ..exchange.sidecar import decode_sidecar, parse_sidecar
from ..exchange.exporter import ExportOptions, export_images, preview_export
from ..exchange.importer import SidecarIndex, _load_options, _read_inputs, _report, import_images, scan_import, scheme_options
from ..exchange.types import ImportOptions
from ..project_db import commands
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import schemes, sources
from ..project_db.validation import digest, primitive, relative_path
from ..runtime.contracts import Versions, strict_json
from ..runtime.errors import ServiceError
from ..tagging.trigger_validation import validate_trigger
from ..tagging.tokenizer import tokenize_prompt


class ExchangeService:
    def __init__(self, runtime, dataset):
        self.runtime, self.dataset = runtime, dataset
        self.exports = {}
        self.lock = threading.RLock()
        self.active_sources = {}

    def preview_import(self, context, value):
        self.runtime.grants.get(value["root_handle"], "import_root", context.project_id)
        with read_snapshot(context.handle) as db:
            scheme = schemes.get_import_scheme(db, value["scheme_id"]) if value["scheme_id"] else schemes.get_default_import_scheme(db)
        options = scheme_options(scheme)
        if value["options"] is not None:
            options = ImportOptions(**{**primitive(options), **value["options"]})
        catalog = self.runtime.catalog.freeze() if options.parser == 'catalog-comma-v1' else None
        if catalog is not None and not catalog.entries:
            raise ServiceError(422, 'catalog:required')
        def work(task, root):
            manifest = scan_import(context.handle, root, options=options)
            if not manifest.entries:
                raise ServiceError(422, "input:no_images")
            diagnostics, failures, fallback_count = [], 0, 0
            sidecars = SidecarIndex()
            for entry in manifest.entries:
                if task.cancel.is_set():
                    raise ServiceError(409, 'operation:canceled')
                result = {'path': entry.path, 'error': None}
                try:
                    if entry.preflight_error:
                        raise ExchangeError(entry.preflight_error)
                    path, data = _read_inputs(root, entry, options, sidecars)
                    inspect_image(path, options)
                    if catalog and data is not None and len(diagnostics)<100:
                        header=decode_sidecar(data,options.encoding)[0].split('\n')[0]
                        result['segments']=[{'raw':t.raw[:256],'lookup':t.lookup,'reason':t.reason} for t in tokenize_prompt(header,catalog=catalog)[:50]]
                    parsed = parse_sidecar(data, options=options, catalog=catalog)
                    result.update(originals=primitive(parsed.originals), warnings=parsed.warnings,
                                  encoding=parsed.encoding, encoding_fallback=parsed.encoding_fallback)
                    fallback_count += int(parsed.encoding_fallback)
                except (ExchangeError, OSError) as exc:
                    failures += 1
                    result['error'] = exc.code if isinstance(exc, ExchangeError) else 'input:unreadable'
                # Keep previews bounded; validation still covers the complete manifest.
                if len(diagnostics) < 100:
                    if 'originals' in result:
                        result['originals'] = [{'type':row['type'], 'content': row['content'][:2000] if isinstance(row['content'],str) else
                            [{**tag,'tag':tag['tag'][:256]} for tag in row['content'][:50]]} for row in result['originals']]
                    diagnostics.append(result)
                elif result['error']:
                    # Errors beyond the sample must remain actionable in the UI.
                    replace_at=next((i for i in range(len(diagnostics)-1,-1,-1) if not diagnostics[i]['error']),None)
                    if replace_at is not None:
                        diagnostics[replace_at]=result
            content = {"selected": len(manifest.entries), "manifest": primitive(manifest), "options": primitive(options), "scheme_id": scheme.scheme_id,
                       'diagnostics': diagnostics, 'blocking_errors': failures, 'can_import': failures == 0,
                       'encoding_fallback_count': fallback_count,
                       'catalog_revision': catalog.revision if catalog else None}
            return self.runtime.previews.add(context.project_id, "import", content,
                       {"root_handle": value["root_handle"], "options": options, "manifest_digest": digest(manifest), "scheme_id": scheme.scheme_id,
                        'scheme_digest': digest(scheme), 'catalog': catalog, 'can_import': failures == 0})
        return self._submit_with_grant(context, "import.preview", value["root_handle"], work)

    def _submit_with_grant(self, context, kind, grant_id, work):
        root, release = self.runtime.grants.acquire(grant_id, "import_root", context.project_id)
        try:
            return self.runtime.supervisor.submit(kind, lambda task: work(task, root), context=context, finalizer=release)
        except BaseException:
            release()
            raise

    def _import_progress(self, context, task, value):
        if value.get('outcome') == 'done' and value.get('image_id') is not None:
            self.runtime.vision.import_keywords(context, value['image_id'])
        task.source_id = value["source_id"]
        with self.lock:
            self.active_sources[(context.project_id, task.source_id)] = task.operation_id
        if value["item_id"] is None:
            self.runtime.versions.bump(self.dataset.key(context, "source", task.source_id))
        counts = value["counts"]
        self.runtime.supervisor.progress(task, completed=sum(counts[key] for key in ("done", "failed", "skipped")), total=sum(counts.values()))
        payload = {**value, "cycle_id": None, "in_flight": counts["sent"], "restore_pending": False}
        kind = "source.item_completed" if value["item_id"] is not None else "source." + ("started" if value["status"] == "running" else value["status"])
        self.runtime.emit(kind, payload, project_id=context.project_id, resource={"kind": "source", "id": value["source_id"]})

    def start_import(self, context, value):
        _, frozen = self.runtime.previews.get(context.project_id, "import", value["preview_id"], value["digest"])
        if not frozen['can_import']:
            raise ServiceError(422, 'preview:has_errors')
        def work(task, root):
            def progress(data):
                self.runtime.source_roots[(context.project_id, data["source_id"])] = frozen["root_handle"]
                self._import_progress(context, task, data)
            return import_images(context.handle, root, name=value["name"], options=frozen["options"], scheme_id=frozen["scheme_id"],
                                 expected_manifest=frozen["manifest_digest"], expected_scheme=frozen['scheme_digest'], catalog=frozen['catalog'],
                                 cancelled=task.cancel.is_set, progress=progress)
        return self._submit_with_grant(context, "import.run", frozen["root_handle"], work)

    def import_summary(self, context, source_id):
        with read_snapshot(context.handle) as db:
            source = sources.get_source(db, source_id)
        if source.type != "import":
            raise ServiceError(422, "source:not_import")
        _, entries = _load_options(source)
        return primitive(_report(context.handle, source_id, entries))

    def set_import_root(self, context, source_id, root_handle, expected):
        def work(task, root):
            view = self.dataset.get_source(context, source_id)
            Versions.require(expected, view["version"])
            if view["type"] != "import" or view["status"] not in ("paused", "done"):
                raise ServiceError(409, "state:conflict")
            with read_snapshot(context.handle) as db:
                source = sources.get_source(db, source_id)
                options, entries = _load_options(source)
            if options.parser == 'catalog-comma-v1':
                self.runtime.catalog.load_frozen(source.config_json['catalog_revision'])
            checked = input_root(context.handle, root)
            sidecars = SidecarIndex()
            for entry in entries:
                if task.cancel.is_set():
                    return {"verified": False}
                _read_inputs(checked, entry, options, sidecars)
            def assign(db):
                # Verification can be slow; recheck its input version in the
                # writer before publishing a new source-root binding.
                current = self.dataset.source_view(context, db, source_id)
                Versions.require(expected, current["version"])
                def committed():
                    self.runtime.source_roots[(context.project_id, source_id)] = root_handle
                    self.runtime.versions.bump(self.dataset.key(context, "source", source_id))
                db.after_commit.append(committed)
            submit_write(context.handle, assign).result()
            return {"source_id": source_id, "verified": True}
        return self._submit_with_grant(context, "import.verify_root", root_handle, work)

    def control_source(self, context, source_id, action, expected):
        with self.lock:
            operation_id = self.active_sources.get((context.project_id, source_id))
        if action == "resume":
            root_id = self.runtime.source_roots.get((context.project_id, source_id))
            if root_id is None:
                raise ServiceError(409, "import:root_unavailable")
            self.runtime.grants.get(root_id, "import_root", context.project_id)
            with self.lock:
                operation_id = self.active_sources.get((context.project_id, source_id))
                if operation_id and not self.runtime.supervisor.tasks[operation_id].finished.is_set():
                    raise ServiceError(409, "source:in_flight")
                def check(db):
                    view = self.dataset.source_view(context, db, source_id)
                    Versions.require(expected, view["version"])
                    if view["type"] != "import" or view["status"] not in ("paused", "done"):
                        raise ServiceError(409, "state:conflict")
                submit_write(context.handle, check).result()
                with read_snapshot(context.handle) as db:
                    source = sources.get_source(db, source_id)
                catalog = self.runtime.catalog.load_frozen(source.config_json['catalog_revision']) if source.config_json.get('catalog_revision') else None
                def work(task, root):
                    return import_images(context.handle, root, source_id=source_id, catalog=catalog, cancelled=task.cancel.is_set,
                                         progress=lambda data: self._import_progress(context, task, data))
                operation = self._submit_with_grant(context, "import.resume", root_id, work)
                self.active_sources[(context.project_id, source_id)] = operation["operation_id"]
                return operation
        def write(db):
            view = self.dataset.source_view(context, db, source_id)
            Versions.require(expected, view["version"])
            if view["type"] != "import":
                raise ServiceError(501, "feature:unsupported")
            target = "paused" if action == "pause" else "canceled"
            commands.transition_source(db, source_id, expected=view["status"], target=target, reason="http:"+action)
            db.after_commit.append(lambda: self.runtime.versions.bump(self.dataset.key(context, "source", source_id)))
        submit_write(context.handle, write).result()
        if operation_id:
            self.runtime.supervisor.tasks[operation_id].cancel.set()
        view = self.dataset.get_source(context, source_id)
        self.runtime.emit("source." + view["status"], {"source_id": source_id, "status": view["status"], "counts": view["counts"]},
                          project_id=context.project_id, resource={"kind": "source", "id": source_id}, version=view["version"])
        return view

    def preview_export(self, context, value):
        options = replace(ExportOptions(**value["options"]), allow_existing_trigger=True)
        catalog = self.runtime.catalog.freeze()
        def work(task):
            preview = preview_export(context.handle, scheme_id=value["scheme_id"], options=options, catalog=catalog)
            return self.runtime.previews.add(context.project_id, "export", preview,
                                            {"scheme_id": preview['scheme']['scheme_id'], "options": options, "plan_digest": preview["plan_digest"],
                                             'catalog': catalog, 'trigger_validation': preview['trigger_validation'],
                                             'hardlink_ack_digest': preview['hardlink_ack_digest']})
        return self.runtime.supervisor.submit("export.preview", work, context=context)

    def validate_trigger(self, context, value):
        prompts = value.get('style_prompts') or []
        if context:
            if value.get('style_prompts') is not None:
                raise ServiceError(422, 'input:conflicting_fields')
            with read_snapshot(context.handle) as db:
                prompts = [row[0] or '' for row in db.execute('SELECT style_prompt FROM sources ORDER BY source_id')]
        return validate_trigger(value['trigger'], style_prompts=prompts, catalog=self.runtime.catalog.freeze())

    def _export_view(self, context, record):
        from .storage import export_cleanup_state
        result = {key: record.get(key) for key in ("export_id", "status", "selected", "exported", "skipped", "failed", "report_url", "relative_dir", "operation_id", "image_mode")}
        cleanup_status = export_cleanup_state(context.handle, record['export_id'])
        if cleanup_status:
            result.update(status=cleanup_status, report_url=None)
        result["version"] = self.runtime.versions.get(self.dataset.key(context, "export", record["export_id"]), result["status"])
        return result

    def start_export(self, context, value):
        _, frozen = self.runtime.previews.get(context.project_id, "export", value["preview_id"], value["digest"])
        if value["scheme_id"] is not None and value["scheme_id"] != frozen["scheme_id"]:
            raise ServiceError(409, "preview:stale")
        if self.runtime.catalog.snapshot().revision != frozen['catalog'].revision:
            raise ServiceError(409, 'preview:stale')
        validation = frozen['trigger_validation']
        acknowledgement = value.get('trigger_override_digest')
        if validation['can_override'] and acknowledgement != validation['override_digest']:
            raise ServiceError(422, 'trigger:override_required', details=validation)
        if acknowledgement is not None and acknowledgement != validation['override_digest']:
            raise ServiceError(409, 'trigger:override_stale')
        link_ack = value.get('hardlink_ack_digest')
        if frozen['hardlink_ack_digest'] and link_ack is None:
            raise ServiceError(422, 'export:hardlink_confirmation_required')
        if link_ack != frozen['hardlink_ack_digest']:
            raise ServiceError(409, 'export:hardlink_confirmation_stale')
        export_id = str(uuid4())
        record = {"export_id": export_id, "status": "planned", "selected": 0, "exported": 0, "skipped": 0, "failed": 0,
                  "image_mode": 'hardlink' if frozen['hardlink_ack_digest'] else 'copy',
                  "report_url": None, "relative_dir": None, "operation_id": None}
        with self.lock:
            record["ordinal"] = len(self.exports)+1
            self.exports[(context.project_id, export_id)] = record
        def emit(kind):
            self.runtime.emit(kind, {key: record[key] for key in ("export_id", "status", "selected", "exported", "skipped", "failed", "report_url")},
                              project_id=context.project_id, resource={"kind": "export", "id": export_id})
        def work(task):
            with self.lock:
                record.update(status="running", operation_id=task.operation_id)
            emit("export.started")
            def progress(counts):
                with self.lock:
                    record.update(counts)
                self.runtime.supervisor.progress(task, completed=sum(counts[key] for key in ("exported", "failed", "skipped")), total=counts["selected"])
                emit("export.progress")
            try:
                report = export_images(context.handle, scheme_id=frozen["scheme_id"], options=frozen["options"],
                                       catalog=frozen['catalog'], trigger_override_digest=acknowledgement,
                                       hardlink_ack_digest=link_ack,
                                       cancelled=task.cancel.is_set, expected_digest=frozen["plan_digest"], export_id=export_id, progress=progress)
                with self.lock:
                    record.update(status="finished", relative_dir=report["directory"], **report["counts"])
                    record["report_url"] = f"/api/projects/{context.project_id}/exports/{export_id}/report"
                emit("export.finished")
                return self._export_view(context, record)
            except Exception as exc:
                with self.lock:
                    record["status"] = "canceled" if task.cancel.is_set() else "interrupted" if isinstance(exc, ExportInterrupted) else "failed"
                    record["relative_dir"] = getattr(exc, "relative_directory", None)
                emit("export.canceled" if task.cancel.is_set() else "export.failed")
                raise
        try:
            operation = self.runtime.supervisor.submit("export.run", work, context=context)
        except BaseException:
            with self.lock:
                del self.exports[(context.project_id, export_id)]
            raise
        with self.lock:
            record["operation_id"] = operation["operation_id"]
            return self._export_view(context, record)

    def _recover_exports(self, context):
        root = relative_path(context.handle.root, "exports")
        for scheme_dir in root.iterdir():
            if not scheme_dir.name.isdecimal():
                continue
            checked = relative_path(context.handle.root, scheme_dir.relative_to(context.handle.root).as_posix())
            if not checked.is_dir():
                continue
            for directory in checked.iterdir():
                relative = directory.relative_to(context.handle.root).as_posix()
                directory = relative_path(context.handle.root, relative)
                if not directory.is_dir():
                    continue
                partial = directory.name.startswith(".partial-")
                receipt = relative_path(context.handle.root, relative + ("/.export-state.json" if partial else "/export_report.json"))
                try:
                    if receipt.stat().st_size > 32*1024*1024:
                        continue
                    data = strict_json(receipt.read_bytes())
                    if not isinstance(data, dict) or data.get("schema_version") != 1:
                        continue
                    export_id = str(UUID(data["export_id"])) if data.get("export_id") else str(uuid5(NAMESPACE_URL, relative))
                    counts = data.get("counts", {})
                    output_config = data.get('output_config', {})
                    if not isinstance(output_config, dict):
                        continue
                    image_mode = data.get('image_mode', 'copy') if partial else output_config.get('image_mode', 'copy')
                    if image_mode not in ('copy', 'hardlink'):
                        continue
                    if not isinstance(counts, dict) or any(type(counts.get(key, 0)) is not int or counts.get(key, 0) < 0 for key in ("selected", "exported", "skipped", "failed")):
                        continue
                    record = {"export_id": export_id, "status": "interrupted" if partial else "finished", "relative_dir": relative,
                              "image_mode": image_mode,
                              "operation_id": None, "report_url": None if partial else f"/api/projects/{context.project_id}/exports/{export_id}/report",
                              **{key: counts.get(key, 0) for key in ("selected", "exported", "skipped", "failed")}}
                    with self.lock:
                        record["ordinal"] = len(self.exports)+1
                        self.exports.setdefault((context.project_id, export_id), record)
                except (OSError, ValueError, TypeError, KeyError, ServiceError):
                    continue

    def get_export(self, context, export_id):
        self._recover_exports(context)
        with self.lock:
            record = self.exports.get((context.project_id, export_id))
            if record is None:
                raise ServiceError(410, "export:session_expired")
            operation = self.runtime.supervisor.tasks.get(record["operation_id"])
            if record["status"] == "planned" and operation and operation.status == "canceled":
                record["status"] = "canceled"
            return self._export_view(context, record)

    def list_exports(self, context, *, limit=50, cursor=None):
        self._recover_exports(context)
        identity = digest([context.project_id, "exports"])
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        with self.lock:
            records = sorted((record for key, record in self.exports.items() if key[0] == context.project_id), key=lambda record: record["ordinal"])
            upper = state["upper"] if state else max((record["ordinal"] for record in records), default=0)
            records = [record for record in records if (state["last"] if state else 0) < record["ordinal"] <= upper]
            values = [self._export_view(context, record) for record in records[:limit]]
        return {"items": values, "limit": limit, "has_more": len(records)>limit, "upper_id": upper,
                "next_cursor": self.runtime.cursors.encode({"identity": identity, "last": records[limit-1]["ordinal"], "upper": upper}) if len(records)>limit else None}

    def report(self, context, export_id):
        from ..exchange.common import project_operation
        with project_operation(context.handle):
            return self._report_locked(context, export_id)

    def _report_locked(self, context, export_id):
        view = self.get_export(context, export_id)
        if view["status"] != "finished":
            raise ServiceError(409, "export:not_finished")
        path = relative_path(context.handle.root, view["relative_dir"] + "/export_report.json")
        return strict_json(path.read_bytes())

    def cancel_export(self, context, export_id, expected):
        view = self.get_export(context, export_id)
        Versions.require(expected, view["version"])
        if view["status"] not in ("planned", "running") or view["operation_id"] is None:
            raise ServiceError(409, "export:already_finished")
        task = self.runtime.supervisor.tasks[view["operation_id"]]
        task.cancel.set()
        return self.get_export(context, export_id)
