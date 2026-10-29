import asyncio
from copy import deepcopy
from pathlib import Path
import threading

from pydantic import Field

from ...api.contracts import StrictModel
from ...llm.secrets import SecretStore
from ...llm.types import LlmFailure
from ...project_db.validation import utc_now
from ...runtime.contracts import Versions
from ...runtime.errors import ServiceError
from ...runtime.supervisor import TERMINAL
from .client import NaiDirectBackend
from .profile import DEFAULT_CONFIG, capabilities


class TokenInput(StrictModel):
    value: str = Field(min_length=1, max_length=8192, pattern=r'^[\x21-\x7e]+$', repr=False)


class NaiSecrets(SecretStore):
    @staticmethod
    def env_name(ref):
        if ref != 'novelai':
            raise LlmFailure('invalid_token_ref')
        return 'STYLELORA_NAI_TOKEN'


class DirectService:
    def __init__(self, runtime, directory=None):
        from ...generation.direct import DirectDriver
        self.runtime = runtime
        self.secrets = NaiSecrets(Path(directory)/'nai-direct' if directory else None)
        self.backend = NaiDirectBackend()
        self.lock = threading.RLock()
        self.account = None
        self.probe_id = None
        self.revision = 0
        self.driver = DirectDriver(runtime, self)

    def available(self):
        return self.secrets.status('novelai')['effective_available']

    def token(self):
        try:
            value = self.secrets.read('novelai')
        except LlmFailure:
            raise ServiceError(503, 'nai:secret_store_unavailable') from None
        if not value:
            raise ServiceError(503, 'nai:token_missing')
        return value

    def view(self):
        with self.lock:
            status = self.secrets.status('novelai')
            return {'backend_id': 'nai_direct', 'name': 'NovelAI 直连', 'credentials': status,
                    'available': status['effective_available'], 'account': deepcopy(self.account),
                    'probe_operation_id': self.probe_id, 'default_config': deepcopy(DEFAULT_CONFIG),
                    'capabilities': capabilities(), 'estimated_anlas': None,
                    'version': self.runtime.versions.get('nai-direct:credentials', {'status': status, 'revision': self.revision})}

    def save_token(self, value, expected):
        with self.runtime.generation_sources.lock, self.lock, self.driver.lock:
            Versions.require(expected, self.view()['version'])
            if self.driver.active or self.probe_id and self.runtime.supervisor.get(self.probe_id)['status'] not in TERMINAL:
                raise ServiceError(409, 'nai:busy')
            try:
                self.secrets.put('novelai', value)
            except LlmFailure as error:
                if error.outcome == 'unknown':
                    # The atomic file replacement may have succeeded before its
                    # acknowledgement failed. Do not present the old CAS token
                    # or old account snapshot as authoritative after that.
                    self.revision += 1
                    self.account = None
                    self.runtime.emit('backend.changed', {'backend_id': 'nai_direct'})
                    raise ServiceError(503, 'nai:secret_write_unknown', outcome='unknown') from None
                raise ServiceError(503, 'nai:secret_store_unavailable') from None
            self.revision += 1
            self.account = None
            self.runtime.emit('backend.changed', {'backend_id': 'nai_direct'})
            return self.view()

    def probe(self):
        with self.lock:
            if self.probe_id and self.runtime.supervisor.get(self.probe_id)['status'] not in TERMINAL:
                return self.runtime.supervisor.get(self.probe_id)
            token = self.token()
            def work(task):
                try:
                    result = asyncio.run(self.backend.account(token))
                    if task.cancel.is_set():
                        raise ServiceError(409, 'operation:canceled')
                    with self.lock:
                        self.account = {**result, 'checked_at': utc_now()}
                    return self.view()
                except BaseException:
                    with self.lock:
                        self.account = None
                    raise
            operation = self.runtime.supervisor.submit('nai_direct.account', work)
            self.probe_id = operation['operation_id']
            return operation
