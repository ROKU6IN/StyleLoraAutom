import copy
from pathlib import Path
import threading
from uuid import UUID, uuid4

from ..project_db.storage import atomic_json
from ..runtime.contracts import strict_json, Versions
from ..runtime.errors import ServiceError
from .secrets import SecretStore, safe_path
from .types import EndpointInput, LlmFailure, ROLES, RolesInput


class ConfigStore:
    def __init__(self, runtime, directory=None, secret_dir=None):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.path = safe_path(Path(directory) / 'llm.json') if directory is not None else None
        self.secrets = SecretStore(secret_dir)
        self.state = {'format': 1, 'revision': 0, 'endpoints': {}, 'roles': dict.fromkeys(ROLES)}
        if self.path and self.path.exists():
            try:
                if self.path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError()
                data = strict_json(self.path.read_bytes())
                if set(data) != set(self.state) or data['format'] != 1 or not isinstance(data['revision'], int):
                    raise ValueError()
                RolesInput.model_validate({'roles': data['roles']})
                for eid, ep in data['endpoints'].items():
                    if str(UUID(eid)) != eid or set(ep) != {'endpoint_id', 'revision', 'token_refs', 'config'}:
                        raise ValueError()
                    EndpointInput.model_validate(ep['config'])
                    if eid != ep['endpoint_id'] or not isinstance(ep['revision'], int) or len(ep['token_refs']) > 32:
                        raise ValueError()
                    for ref in ep['token_refs']:
                        self.secrets.env_name(ref)
                self.state = data
                self._validate_roles(data['roles'], data)
            except Exception:
                raise LlmFailure('config_invalid') from None

    def _commit(self, state):
        if state == self.state:
            return
        state['revision'] = self.state['revision'] + 1
        if self.path:
            try:
                safe_path(self.path)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                atomic_json(self.path, state)
            except Exception:
                # Reload before reporting an unknown outcome; GET remains authoritative.
                if self.path.exists():
                    self.state = strict_json(self.path.read_bytes())
                raise LlmFailure('config_write_failed', outcome='unknown') from None
        self.state = state
        self.runtime.emit('llm.changed', {'revision': state['revision']})

    def _endpoint(self, eid):
        if eid not in self.state['endpoints']:
            raise ServiceError(404, 'llm:endpoint_not_found')
        return self.state['endpoints'][eid]

    def version(self, ep):
        return self.runtime.versions.get('llm:' + ep['endpoint_id'], {'endpoint': ep,
            'encrypted_entries': {ref: self.secrets.entries.get(ref) for ref in ep['token_refs']}})

    def view(self, eid):
        with self.lock:
            ep = self._endpoint(eid)
            return {**copy.deepcopy(ep['config']), 'endpoint_id': eid, 'revision': ep['revision'],
                    'tokens': [self.secrets.status(ref) for ref in ep['token_refs']], 'version': self.version(ep)}

    def snapshot(self, eid):
        with self.lock:
            return copy.deepcopy(self._endpoint(eid))

    def list(self, limit=50, cursor=None):
        with self.lock:
            identity = 'llm:endpoints'
            revision = self.state['revision']
            start = 0
            if cursor:
                page = self.runtime.cursors.decode(cursor, identity)
                if page['revision'] != revision:
                    raise ServiceError(409, 'page:stale_cursor')
                start = page['last']
            ids = sorted(self.state['endpoints'])
            chosen = ids[start:start+limit]
            more = start + len(chosen) < len(ids)
            return {'items': [self.view(eid) for eid in chosen], 'limit': limit, 'has_more': more,
                    'next_cursor': self.runtime.cursors.encode({'identity': identity, 'revision': revision,
                        'last': start+len(chosen)}) if more else None, 'upper_id': len(ids), 'revision': revision}

    def save(self, value: EndpointInput, eid=None, expected=None, *, trusted_probe=False):
        with self.lock:
            self._writable()
            state = copy.deepcopy(self.state)
            incoming = value.model_dump()
            old = {m['name']: m for m in self._endpoint(eid)['config']['models']} if eid else {}
            if not trusted_probe:
                for model in incoming['models']:
                    if model != old.get(model['name']):
                        model['capability_source'], model['tested_at'] = 'manual', None
            if eid:
                ep = self._endpoint(eid)
                Versions.require(expected, self.version(ep))
                if ep['config'] == incoming:
                    return self.view(eid)
                state['endpoints'][eid]['config'] = incoming
                state['endpoints'][eid]['revision'] += 1
            else:
                if len(state['endpoints']) >= 100:
                    raise ServiceError(422, 'llm:endpoint_limit')
                eid = str(uuid4())
                state['endpoints'][eid] = {'endpoint_id': eid, 'config': incoming, 'token_refs': [], 'revision': 1}
            self._validate_roles(state['roles'], state)
            self._commit(state)
            return self.view(eid)

    def _writable(self):
        if self.runtime.draining:
            raise ServiceError(503, 'process:draining')

    def delete(self, eid, expected):
        with self.lock:
            self._writable()
            ep = self._endpoint(eid)
            Versions.require(expected, self.version(ep))
            if any(v and v['endpoint_id'] == eid for v in self.state['roles'].values()):
                raise ServiceError(409, 'llm:endpoint_bound')
            # Require explicit secret deletion first so failures cannot silently orphan secrets.
            if ep['token_refs']:
                raise ServiceError(409, 'llm:endpoint_has_tokens')
            state = copy.deepcopy(self.state)
            del state['endpoints'][eid]
            self._commit(state)

    def token(self, eid, value, expected=None, ref=None):
        with self.lock:
            self._writable()
            ep = self._endpoint(eid)
            if ref is not None:
                Versions.require(expected, self.version(ep))
                if ref not in ep['token_refs']:
                    raise ServiceError(404, 'llm:token_not_found')
            else:
                if len(ep['token_refs']) >= 32:
                    raise ServiceError(422, 'llm:token_limit')
                ref = str(uuid4())
            # A successful secret write must invalidate endpoint CAS, even when the
            # environment currently overrides it. No plaintext enters llm.json.
            changed = self.secrets.put(ref, value)
            if not changed and value is not None and ref in ep['token_refs']:
                return {**self.secrets.status(ref), 'version': self.version(ep)}
            state = copy.deepcopy(self.state)
            target = state['endpoints'][eid]
            if value is None:
                target['token_refs'].remove(ref)
            elif ref not in target['token_refs']:
                target['token_refs'].append(ref)
            target['revision'] += 1
            self._commit(state)
            return {**self.secrets.status(ref), 'version': self.version(target)}

    @staticmethod
    def _validate_roles(roles, state):
        for role, binding in roles.items():
            if not binding:
                continue
            if role.startswith('clean'):
                raise ServiceError(422, 'llm:clean_reserved')
            ep = state['endpoints'].get(binding['endpoint_id'])
            model = next((m for m in ep['config']['models'] if m['name'] == binding['model']), None) if ep else None
            if model is None:
                raise ServiceError(422, 'llm:model_not_configured')
            if role in ('vision', 'vision_nsfw') and not model['supports_images']:
                raise ServiceError(422, 'llm:image_capability_required')

    def roles(self):
        with self.lock:
            roles = copy.deepcopy(self.state['roles'])
            return {'roles': roles, 'revision': self.state['revision'],
                    'version': self.runtime.versions.get('llm:roles', self.state),
                    'availability': {role: bool(binding and any(self.secrets.status(ref)['effective_available']
                        for ref in self._endpoint(binding['endpoint_id'])['token_refs'])) for role, binding in roles.items()}}

    def save_roles(self, value: RolesInput, expected):
        with self.lock:
            self._writable()
            Versions.require(expected, self.roles()['version'])
            state = copy.deepcopy(self.state)
            state['roles'] = {**dict.fromkeys(ROLES), **value.model_dump()['roles']}
            self._validate_roles(state['roles'], state)
            self._commit(state)
            return self.roles()
