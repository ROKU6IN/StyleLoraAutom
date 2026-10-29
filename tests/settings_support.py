"""Synthetic settings adapter, never registered in production compatibility."""
from copy import deepcopy

from stylelora.generation.settings import SettingsIdentity, SettingsWriteConflict
from stylelora.project_db.validation import digest


class FakeSettings:
    def __init__(self):
        self.identity = SettingsIdentity(digest('fixture-launcher-instance'), digest('fixture-settings-contract-v2'),
            ('steps', 'sampler', 'quality_toggle', 'uc_preset'))
        self.values = {'steps': 14, 'sampler': 'fixture-original', 'quality_toggle': True, 'uc_preset': 3,
                       'prompt': 'private prompt', 'token': 'private-token', 'readonly': 'do-not-write'}
        self.writes, self.reads, self.probes = [], 0, []
        self.settled, self.generations = {}, {}
        self.on_write, self.on_read, self.before_write = None, None, None

    async def read_settings(self):
        self.reads += 1
        if self.on_read:
            await self.on_read()
        return deepcopy(self.values)

    async def update_settings(self, values, operation_id, *, expected):
        self.writes.append((deepcopy(values), operation_id))
        if self.before_write:
            await self.before_write()
        if set(expected) != set(values) or any(type(self.values[k]) is not type(v) or self.values[k] != v for k,v in expected.items()):
            self.settled[operation_id] = True
            raise SettingsWriteConflict()
        if self.on_write:
            return await self.on_write(values, operation_id)
        self.values.update(values)
        self.settled[operation_id] = True

    async def settings_write_settled(self, operation_id):
        self.probes.append(operation_id)
        return self.settled.get(operation_id, False)

    async def generation_settled(self, operation_key):
        return self.generations.get(operation_key, False)
