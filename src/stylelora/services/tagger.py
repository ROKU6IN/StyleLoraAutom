"""Product-scoped model operations, deliberately separate from persistent jobs."""
import threading
import time

from ..project_db.validation import utc_now
from ..runtime.errors import ServiceError, safe_error
from ..runtime.supervisor import TERMINAL
from ..tagging.device import probe_device
from ..tagging.manifests import REVIEWED_MANIFESTS, parse_manifest, reviewed_provenance
from ..tagging.model_runtime import runtime_status
from ..tagging.gpu_profiles import matching_profile
from ..tagging.models import ModelStore


class TaggerService:
    def __init__(self, runtime, model_dir=None, *, manifests=REVIEWED_MANIFESTS, store=None,
                 device_probe=probe_device, status=runtime_status, clock=time.monotonic):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.store = store or ModelStore(model_dir)
        checked = [parse_manifest(m.model_dump_json().encode()) for m in manifests]
        if len({(m.model_id, m.revision) for m in checked}) != len(checked):
            raise ValueError('Duplicate model revision')
        self.manifests = {m.digest: m for m in checked}
        self.device_probe, self.status, self.clock = device_probe, status, clock
        self.device, self.device_time = None, 0
        self.active, self.installations, self.verified, self.errors = {}, {}, {}, {}

    def can_execute(self):
        with self.lock:
            return bool(self.manifests) and self.status()['available'] and any(self._view(m)['state'] == 'ready' for m in self.manifests.values())

    def capabilities(self):
        with self.lock:
            if self.device is None or self.clock() - self.device_time > 30:
                self.device, self.device_time = self.device_probe(), self.clock()
            status = self.status()
            ready = any(self._view(m)['state'] == 'ready' for m in self.manifests.values())
            profiles = []
            if self.device.get('cuda_available'):
                for m in self.manifests.values():
                    profile = matching_profile(self.device, m.digest, status['version'])
                    if profile:
                        profiles.append({'manifest_id': m.digest, 'max_batch_size': max(map(int, profile['required_bytes'])),
                                         'profile_id': profile['profile_id']})
            reason = status['reason'] or ('tagger:manifest_unconfigured' if not self.manifests else 'tagger:model_missing' if not ready else None)
            return {'available': reason is None, 'reason': reason, 'executor_implemented': True,
                    'max_images': 5000, 'cpu_batch_size': 1, 'max_batch_size': 4,
                    'cuda_profiles': profiles, 'providers': ['cpu', 'auto'],
                    'model_management': True, 'trusted_manifest_count': len(self.manifests),
                    'manifest_reason': None if self.manifests else 'tagger:manifest_unconfigured',
                    'runtime': status, 'device': dict(self.device),
                    'launcher': {'available': False, 'reason': 'tagger:launcher_unavailable'}}

    def models(self, limit=50, cursor=None):
        identity = self.runtime.instance_id + ':tagger-models'
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        start = state['last'] if state else 0
        with self.lock:
            manifests = sorted(self.manifests.values(), key=lambda m: (m.model_id, m.revision))
            items = [self._view(m) for m in manifests[start:start+limit]]
        more = start+len(items) < len(manifests)
        return {'items': items, 'limit': limit, 'has_more': more,
                'next_cursor': self.runtime.cursors.encode({'identity': identity, 'last': start+limit}) if more else None}

    def _view(self, manifest):
        mid = manifest.digest
        state, reason = 'missing', None
        try:
            directory = self.store.directory(manifest)
            if directory.exists():
                state = 'unverified'
                stamp = self.store.footprint(manifest)
                if mid in self.verified and stamp == self.verified[mid][0]:
                    state = 'ready'
                elif mid in self.verified:
                    state, reason = 'invalid', 'tagger:model_changed'
                    self.verified.pop(mid, None)
                    self.errors[mid] = reason
        except FileNotFoundError:
            state, reason = 'invalid', 'tagger:model_invalid'
        except Exception as error:
            state, reason = 'invalid', safe_error(error).code
        active = self.active.get(mid)
        if active and self.runtime.supervisor.get(active)['status'] not in TERMINAL:
            state = 'installing'
        elif mid in self.errors:
            reason = self.errors[mid]
            if state != 'missing':
                state = 'invalid'
        return {'manifest_id': mid, 'model_id': manifest.model_id, 'repo_id': manifest.repo_id,
                'provenance': reviewed_provenance(manifest),
                'revision': manifest.revision, 'files': [f.model_dump() for f in manifest.files],
                'total_bytes': sum(f.size for f in manifest.files), 'state': state, 'reason': reason,
                'installation_id': active, 'preprocess_version': manifest.preprocess_version,
                'verified_at': self.verified.get(mid, (None, None))[1] if state == 'ready' else None}

    def install(self, manifest_id):
        with self.lock:
            manifest = self.manifests.get(manifest_id)
            if manifest is None:
                raise ServiceError(422, 'tagger:manifest_invalid')
            current = self.active.get(manifest_id)
            if current and self.runtime.supervisor.get(current)['status'] not in TERMINAL:
                return self.get(current)
            status = self.status()
            if not status['available']:
                raise ServiceError(503, status['reason'])
            self.store.directory(manifest)
            operation = self.runtime.supervisor.submit('tagger.install', lambda task: self._run(task, manifest))
            oid = operation['operation_id']
            self.active[manifest_id] = oid
            self.installations[oid] = {'manifest_id': manifest_id, 'phase': 'queued'}
            self.errors.pop(manifest_id, None)
            return self.get(oid)

    def get(self, installation_id):
        with self.lock:
            installation = self.installations.get(installation_id)
            if installation is None:
                raise ServiceError(410, 'session:expired')
            operation = self.runtime.supervisor.get(installation_id)
            return {**operation, **installation, 'installation_id': installation_id,
                    'phase': operation['status'] if operation['status'] in ('failed', 'canceled', 'interrupted') else installation['phase']}

    def cancel(self, installation_id, expected):
        self.get(installation_id)
        self.runtime.supervisor.cancel_task(installation_id, expected)

    def _run(self, task, manifest):
        def phase(value):
            with self.lock:
                self.installations[task.operation_id]['phase'] = value
        try:
            result = self.store.install(manifest, task.cancel, phase=phase,
                progress=lambda done, total: self.runtime.supervisor.progress(task, completed=done, total=total),
                publish_lock=self.runtime.supervisor.lock)
            with self.lock:
                self.verified[manifest.digest] = (result['stamp'], utc_now())
                self.errors.pop(manifest.digest, None)
                phase('ready')
            self.runtime.emit('tagger.model_ready', {'manifest_id': manifest.digest},
                              resource={'kind': 'model', 'id': manifest.digest})
            return {'manifest_id': manifest.digest, 'state': 'ready', 'reused': result['reused']}
        except Exception as error:
            with self.lock:
                if not task.cancel.is_set():
                    self.errors[manifest.digest] = safe_error(error).code
            raise
