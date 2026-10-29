"""Current-project storage accounting and explicit, evidence-bound cleanup."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import PurePosixPath
import threading

from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import images
from ..project_db.storage import atomic_json
from ..project_db.validation import digest, relative_path, utc_now
from ..runtime.contracts import strict_json
from ..runtime.errors import ServiceError
from .storage_files import inventory, verify, unlink_verified

KINDS = ('thumbnails', 'exports', 'rejected_files')
MAX_CANDIDATES = 5000


@contextmanager
def exclusive(project):
    if not project._file_operations.acquire(blocking=False):
        raise ServiceError(409, 'storage:leased')
    try:
        yield
    finally:
        project._file_operations.release()


def rows_for(project):
    with read_snapshot(project) as db:
        return {r['filename']: dict(r) for r in db.execute('SELECT image_id,source_id,filename,sha256,file_size,review_status,file_missing FROM images')}


def export_cleanup_state(project, export_id):
    path = relative_path(project.root, '.cleanup/exports/'+digest(export_id)+'.json')
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        value = strict_json(path.read_bytes())
        if value['export_id'] != export_id or value['schema_version'] != 1:
            raise ValueError()
        return 'cleaned' if value['status'] == 'complete' else 'cleanup_partial'
    except (OSError, ValueError, KeyError, TypeError):
        return 'cleanup_partial'


def bucket(path, registered):
    parts = PurePosixPath(path).parts
    if parts[0] in ('project.db', 'project.db-wal', 'project.db-shm'):
        return 'database'
    if parts[0] == 'images':
        if len(parts) == 2 and parts[1] in registered:
            return 'images'
        if len(parts) > 1 and parts[1] == '.pending':
            return 'pending'
        if len(parts) > 1 and parts[1] in ('.checks', '.costs', '.operations'):
            return 'evidence'
        return 'image_other'
    if parts[0] == '.cleanup':
        return 'evidence'
    if parts[0] in ('cache', 'exports'):
        return parts[0]
    if 'backup' in parts[0].lower() or parts[0] == '.migrations':
        return 'backups'
    return 'other'


class StorageService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.summaries, self.operations = {}, {}

    def summary(self, context):
        with self.lock:
            value = deepcopy(self.summaries.get(context.project_id))
        # A scan is a point-in-time inventory, not a continuously updated size.
        config = self.runtime.product_config.get()
        return {'scan': value, 'stale': value is not None,
                'config_changed': value is not None and value['config_revision'] != config['revision'],
                'project_threshold_bytes': config['storage_thresholds']['project_bytes']}

    def scan(self, context):
        config = self.runtime.product_config.get()
        threshold = config['storage_thresholds']['project_bytes']
        def work(task):
            registered = rows_for(context.handle)
            files, errors, _ = inventory(context.handle.root, canceled=task.cancel.is_set)
            totals = {key: 0 for key in ('images', 'image_other', 'pending', 'evidence', 'cache', 'exports', 'database', 'backups', 'other')}
            reviews = {key: {'bytes': 0, 'files': 0, 'missing_records': 0} for key in ('keep', 'unreviewed', 'reject')}
            sources, present = {}, set()
            for row in registered.values():
                source = sources.setdefault(row['source_id'], {'source_id': row['source_id'], 'bytes': 0, 'files': 0, 'missing_records': 0})
                if row['file_missing']:
                    reviews[row['review_status']]['missing_records'] += 1
                    source['missing_records'] += 1
            for file in files:
                category = bucket(file['path'], registered)
                totals[category] += file['bytes']
                if category == 'images':
                    row = registered[PurePosixPath(file['path']).name]
                    present.add(row['image_id'])
                    for target in (reviews[row['review_status']], sources[row['source_id']]):
                        target['bytes'] += file['bytes']; target['files'] += 1
            result = {'scan_id': task.operation_id, 'config_revision': config['revision'],
                      'as_of': utc_now(), 'total_bytes': sum(totals.values()), 'categories': totals, 'reviews': reviews,
                      'sources': list(sources.values()), 'missing_records': sum(r['file_missing'] for r in registered.values()),
                      'not_observed_records': sum(r['image_id'] not in present for r in registered.values()),
                      'partial': bool(errors), 'errors': errors[:200], 'error_count': len(errors),
                      'file_count': len(files), 'hardlinked_files': sum(f['links'] > 1 for f in files),
                      'exceeds_threshold': sum(totals.values()) > threshold,
                      'project_threshold_bytes': threshold, 'accounting': 'logical_file_bytes'}
            if task.cancel.is_set():
                raise ServiceError(409, 'operation:canceled')
            with self.lock:
                self.summaries[context.project_id] = result
            self.runtime.emit('storage.changed', {'scan_id': task.operation_id})
            return result
        return self.runtime.supervisor.submit('storage.scan', work, context=context)

    def candidates(self, context, kind, canceled=lambda: False):
        project = context.handle
        registered = rows_for(project)
        base = {'thumbnails': 'cache/thumbnails', 'exports': 'exports', 'rejected_files': 'images'}[kind]
        if not relative_path(project.root, base).exists():
            return [], {}, []
        files, errors, dirs = inventory(project.root, base, canceled=canceled)
        if errors:
            raise ServiceError(409, 'cleanup:inventory_incomplete', details={'errors': errors[:100]})
        candidates, skipped = [], []
        if kind == 'thumbnails':
            candidates = [f for f in files if len(PurePosixPath(f['path']).parts) == 3 and
                          PurePosixPath(f['path']).suffix == '.webp' and
                          len(PurePosixPath(f['path']).stem) == 64 and
                          all(c in '0123456789abcdef' for c in PurePosixPath(f['path']).stem)]
        elif kind == 'rejected_files':
            for f in files:
                parts = PurePosixPath(f['path']).parts
                row = registered.get(parts[-1]) if len(parts) == 2 else None
                if row and row['review_status'] == 'reject' and not row['file_missing']:
                    candidates.append({**f, 'image': row})
        else:
            groups = {}
            for f in files:
                parts = PurePosixPath(f['path']).parts
                if len(parts) >= 4:
                    groups.setdefault('/'.join(parts[:3]), []).append(f)
            for directory, values in sorted(groups.items()):
                try:
                    if PurePosixPath(directory).name.startswith('.'):
                        raise ValueError()
                    state_path = relative_path(project.root, directory+'/.export-state.json')
                    report_path = relative_path(project.root, directory+'/export_report.json')
                    if state_path.stat().st_size > 65536 or report_path.stat().st_size > 32*1024*1024:
                        raise ValueError()
                    state, report = strict_json(state_path.read_bytes()), strict_json(report_path.read_bytes())
                    if (state.get('schema_version') != 1 or report.get('schema_version') != 1 or
                        state.get('status') != 'complete' or report.get('status') != 'complete' or
                        state.get('directory') != directory or report.get('directory') != directory or
                        not state.get('export_id') or state.get('export_id') != report.get('export_id')):
                        raise ValueError()
                    candidates.extend({**f, 'export_id': state['export_id'], 'directory': directory} for f in values)
                except (OSError, ValueError, TypeError, AttributeError):
                    skipped.append({'path': directory, 'code': 'cleanup:unsettled_export'})
            # Keep completion evidence until all payloads in that export are deleted.
            candidates.sort(key=lambda f: (f['directory'], PurePosixPath(f['path']).name in ('.export-state.json', 'export_report.json'), f['path']))
        if len(candidates) > MAX_CANDIDATES:
            raise ServiceError(413, 'cleanup:too_many_files')
        # Freeze only directories involved in deletion, including their ancestors.
        dirs = {p: value for p, value in dirs.items() if any(f['path'].startswith(p+'/') for f in candidates)}
        dirs[''] = [project.root.stat().st_dev, project.root.stat().st_ino]
        return candidates, dirs, skipped

    def preview(self, context, kind):
        def work(task):
            with exclusive(context.handle):
                files, dirs, skipped = self.candidates(context, kind, task.cancel.is_set)
                content = {'kind': kind, 'count': len(files), 'bytes': sum(f['bytes'] for f in files),
                           'hardlinked_files': sum(f['links'] > 1 for f in files),
                           'candidates': [{'path': f['path'], 'bytes': f['bytes'], 'image_id': f.get('image', {}).get('image_id')} for f in files],
                           'skipped': skipped, 'irreversible': kind == 'rejected_files',
                           'evidence_digest': digest([files, dirs]), 'generation': context.generation}
                return self.runtime.previews.add(context.project_id, 'storage.cleanup', content,
                                                 {'files': files, 'directories': dirs, 'kind': kind})
        return self.runtime.supervisor.submit('storage.preview', work, context=context)

    def cleanup(self, context, value):
        if value['confirm'] is not True:
            raise ServiceError(422, 'cleanup:confirmation_required')
        preview, frozen = self.runtime.previews.get(context.project_id, 'storage.cleanup', value['preview_id'], value['digest'])
        with self.lock:
            if preview['preview_id'] in self.operations:
                return self.runtime.supervisor.get(self.operations[preview['preview_id']])
            operation = self.runtime.supervisor.submit('storage.cleanup', lambda task: self._cleanup(context, task, preview, frozen),
                                                        context=context, cancelable=False)
            self.operations[preview['preview_id']] = operation['operation_id']
            return operation

    def _cleanup(self, context, task, preview, frozen):
        project = context.handle
        with exclusive(project):
            files, dirs, _ = self.candidates(context, frozen['kind'])
            if digest([files, dirs]) != preview['evidence_digest'] or context.generation != preview['generation']:
                raise ServiceError(409, 'cleanup:preview_stale')
            # Reject the entire plan before deletion if any candidate changed.
            for file in files:
                verify(project.root, file, dirs)
            audit_path = relative_path(project.root, '.cleanup/'+preview['preview_id']+'.json')
            if audit_path.exists():
                raise ServiceError(409, 'cleanup:receipt_exists')
            audit_path.parent.mkdir(exist_ok=True)
            audit = {'schema_version': 1, 'preview_id': preview['preview_id'], 'digest': preview['digest'],
                     'kind': frozen['kind'], 'as_of': utc_now(), 'status': 'running', 'candidates': files, 'results': []}
            atomic_json(audit_path, audit)
            export_markers = {}
            for file in files:
                if file.get('export_id') and file['export_id'] not in export_markers:
                    marker = relative_path(project.root, '.cleanup/exports/'+digest(file['export_id'])+'.json')
                    marker.parent.mkdir(exist_ok=True)
                    value = {'schema_version': 1, 'export_id': file['export_id'], 'preview_id': preview['preview_id'], 'status': 'running'}
                    atomic_json(marker, value)
                    export_markers[file['export_id']] = (marker, value)
            failed_groups = set()
            for index, file in enumerate(files):
                result = {'path': file['path'], 'image_id': file.get('image', {}).get('image_id'), 'status': 'failed', 'bytes': 0}
                try:
                    if task.cancel.is_set() or context.closing:
                        raise ServiceError(409, 'cleanup:stopped')
                    if file.get('directory') in failed_groups:
                        raise ServiceError(409, 'cleanup:export_incomplete')
                    def remove(db):
                        if file.get('image'):
                            row = images.get_image(db, file['image']['image_id'])
                            if row.review_status != 'reject' or row.file_missing or row.filename != file['image']['filename'] or row.sha256 != file['image']['sha256']:
                                raise ServiceError(409, 'cleanup:preview_stale')
                            if db.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running','paused') LIMIT 1").fetchone():
                                raise ServiceError(409, 'storage:leased')
                        unlink_verified(project.root, file, dirs)
                        project._fault('cleanup_after_unlink')
                        if file.get('image'):
                            images.set_file_missing(db, file['image']['image_id'], missing=True)
                    # Review changes and deletion/missing publication share the single writer.
                    submit_write(project, remove).result()
                    result.update(status='deleted', bytes=file['bytes'])
                except Exception as error:
                    result['code'] = error.code if isinstance(error, ServiceError) else 'cleanup:file_failed'
                    if file.get('directory'):
                        failed_groups.add(file['directory'])
                audit['results'].append(result)
                atomic_json(audit_path, audit)
                self.runtime.supervisor.progress(task, completed=index+1, total=len(files))
            audit['status'] = 'partial' if any(r['status'] != 'deleted' for r in audit['results']) else 'complete'
            atomic_json(audit_path, audit)
            deleted_exports = {f['export_id'] for f in files if f.get('directory') and f['directory'] not in failed_groups}
            for eid, (marker, value) in export_markers.items():
                atomic_json(marker, {**value, 'status': 'complete' if eid in deleted_exports else 'partial'})
            exchange = self.runtime.dataset.exchange
            with exchange.lock:
                for (pid, eid), record in exchange.exports.items():
                    if pid == context.project_id and eid in deleted_exports:
                        record.update(status='cleaned', report_url=None)
            self.runtime.emit('image.updated', {'image_ids': [], 'invalidate_scope': True, 'fields': ['file_missing']}, project_id=context.project_id)
            return {'kind': frozen['kind'], 'status': audit['status'], 'results': audit['results'],
                    'deleted': sum(r['status'] == 'deleted' for r in audit['results']),
                    'failed': sum(r['status'] != 'deleted' for r in audit['results']),
                    'unlinked_bytes': sum(r['bytes'] for r in audit['results']), 'audit': '.cleanup/'+audit_path.name}
