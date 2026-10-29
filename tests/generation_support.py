"""Synthetic operation association only; never loaded by the application."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import time

from stylelora.generation.runner import Preparation, Outcome
from stylelora.project_db.validation import digest
from .settings_support import FakeSettings
from .test_generation_handoff import FakeDriver
from .test_generation_artifacts import png


class FakeCycleAdapter(FakeSettings):
    def __init__(self):
        super().__init__()
        self.identity = replace(self.identity, contract_digest='a' * 64)
        self.estimates = [0]
        self.prepared, self.submitted, self.canceled, self.queried = [], [], [], []
        self.requests, self.paths, self.results = {}, {}, {}
        self.prepare_hook = self.submit_hook = self.cancel_hook = self.probe_hook = None

    def capabilities(self):
        return FakeDriver().capabilities()

    def outcome(self, row, grant, kind, sha=None):
        return Outcome(**{k: row[k] for k in ('operation_key', 'instance_id', 'contract_digest', 'request_digest', 'config_digest')},
            cost_operation_key=grant['operation_key'] if grant else None, kind=kind,
            evidence_digest=digest((row['operation_key'], kind)), artifact_sha256=sha)

    async def prepare(self, row, request, save_path):
        key = row['operation_key']
        self.prepared.append(row)
        self.requests[key], self.paths[key] = request, Path(save_path)
        if self.prepare_hook:
            await self.prepare_hook(row)
        return Preparation(self.estimates[min(len(self.prepared)-1, len(self.estimates)-1)],
                           {k: deepcopy(self.values[k]) for k in self.identity.writable_keys})

    async def submit(self, row, grant):
        self.submitted.append((row, grant, time.monotonic()))
        key = row['operation_key']
        if self.submit_hook:
            return await self.submit_hook(row, grant)
        return self.complete(row, grant)

    def complete(self, row, grant, *, prompt=None):
        key = row['operation_key']
        request, path = self.requests[key], self.paths[key]
        png(path, {'prompt': request['positive'] if prompt is None else prompt}, size=(request['width'], request['height']))
        result = self.outcome(row, grant, 'complete', hashlib.sha256(path.read_bytes()).hexdigest())
        self.results[key], self.generations[key] = result, True
        return result

    async def cancel_preparation(self, row):
        self.canceled.append(row)
        if self.cancel_hook:
            return await self.cancel_hook(row)
        result = self.outcome(row, None, 'canceled')
        self.results[row['operation_key']], self.generations[row['operation_key']] = result, True
        return result

    async def probe(self, row, grant):
        self.queried.append(row)
        if self.probe_hook:
            return await self.probe_hook(row, grant)
        return self.results.get(row['operation_key'], self.outcome(row, grant, 'unknown'))
