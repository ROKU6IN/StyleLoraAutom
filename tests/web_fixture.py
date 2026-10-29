"""Owned browser-test process. Chooser substitution exists only in this test module."""
import json
import sys
from pathlib import Path

from stylelora.runtime.instance import ProductLock, pipe_request
from stylelora.runtime.picker import NativePicker
from stylelora.runtime.server import serve


def main():
    mode = sys.argv[1]
    if mode == 'credentials':
        request = json.loads(sys.stdin.read())
        hello = pipe_request({'action': 'hello'})
        if hello['pid'] != request['pid']:
            raise RuntimeError('Test does not own this server')
        if request.get('scale_project'):
            scale = Path(request['scale_project']).resolve()
            allowed = Path(__file__).resolve().parents[1]/'.test-tmp'
            if not scale.is_relative_to(allowed) or scale.name != 'scale.slproj':
                raise RuntimeError('Scale fixture must be an owned test project')
            value = pipe_request({'action': 'grant', 'purpose': 'project_open', 'path': str(scale),
                                  'instance_id': hello['instance_id']})
        else:
            value = pipe_request({'action': 'bootstrap'}) if request.get('bootstrap') else hello
        # Captured only in the Playwright parent memory, never written to logs/files.
        sys.stdout.write(json.dumps(value))
        return
    root = Path(sys.argv[2]).resolve()
    allowed = Path(__file__).resolve().parents[1]/'.test-tmp'
    if not root.is_relative_to(allowed) or not root.is_dir():
        raise RuntimeError('Fixture root must be an existing test directory')
    if mode == 'settings-lease':
        import asyncio
        from uuid import uuid4
        from stylelora.generation.settings import SettingsLeases
        from stylelora.runtime.errors import ServiceError
        from tests.settings_support import FakeSettings
        manager = SettingsLeases(root/'runtime'/'product'/'private'/'recovery'/'launcher')
        adapter = FakeSettings()
        async def change():
            if sys.argv[3] == 'conflict':
                lease = await manager.acquire(adapter, project_id=str(uuid4()), source_id=1, cycle_id=str(uuid4()),
                    changes={'steps':28, 'quality_toggle':False, 'uc_preset':None})
                adapter.values['steps'] = 99
                try:
                    await manager.restore(adapter, lease)
                except ServiceError as error:
                    if error.code != 'profile:restore_conflict':
                        raise
            elif sys.argv[3] == 'restore':
                row = manager.journal.load()['record']
                adapter.values.update(row['desired'])
                await manager.restore(adapter, row['lease_id'])
            else:
                raise ValueError('Unknown fixture action')
        asyncio.run(change())
        return
    if mode == 'encoding-input':
        from PIL import Image
        path = root/'inputs'/'encoding-example.png'
        Image.new('RGB', (33, 31), (17, 83, 159)).save(path)
        caption = b'\xef\xbb\xbf\xff' if sys.argv[3] == 'broken-bom' else 'smile\n\n一名女孩站在树旁。'.encode('gbk')
        path.with_suffix('.txt').write_bytes(caption)
        return
    if mode == 'extra-input':
        from PIL import Image
        path=root/'inputs'/'catalog-example.png'
        Image.new('RGB',(32,32),'cyan').save(path)
        caption='(alias_tag:1.2)\n\n新图段落。' if sys.argv[3]=='invalid' else 'alias_tag, 新图说明\n\n新图段落。'
        path.with_suffix('.txt').write_text(caption,encoding='utf-8')
        return
    probe = ProductLock()  # Refuse to attach to a user's running service.
    probe.close()
    from PIL import Image, ImageDraw
    inputs = root/'inputs'
    inputs.mkdir()
    for index, color in enumerate(['#B8C6B0', '#CEA38D', '#B2AEC3', '#9DBDC6', '#D0B879', '#9AA398']):
        image = Image.new('RGBA', (640, 480), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 90, 310, 410), fill='#EDE7DB')
        draw.ellipse((230+index*12, 65, 540, 375), fill='#57445E')
        draw.polygon([(80, 370), (310, 150), (555, 410)], fill='#EDC77B')
        if index == 0:
            draw.rectangle((0, 0, 90, 90), fill=(0, 0, 0, 0))
        path = inputs/f'study-{index+1:02}.png'
        image.save(path)
        path.with_suffix('.txt').write_text('geometric, still_life\n\n几何构图习作。', encoding='utf-8')
    def choose(self, purpose, cancel):
        return inputs if purpose == 'import_root' else root/'browser.slproj'
    NativePicker.choose = choose
    # The final browser case opts into this synthetic recovery fixture explicitly.
    # No equivalent hook or backend is registered by the shipped application.
    from stylelora.generation.sources import SourceService
    from stylelora.generation.runner import SerialDriver
    from stylelora.project_db import commands
    from stylelora.project_db.connection import submit_write
    from stylelora.project_db.types import ItemSeed
    from tests.generation_support import FakeCycleAdapter
    from tests.test_generation_handoff import draft
    (root/'recovery-draft.json').write_text(json.dumps(draft(2)), encoding='utf-8')
    from stylelora.backends.nai_direct.profile import DEFAULT_CONFIG
    direct_draft = {**draft(2), 'config': DEFAULT_CONFIG, 'cost_policy': 'allow'}
    (root/'direct-review-draft.json').write_text(json.dumps(direct_draft), encoding='utf-8')
    original_create = SourceService.create
    def recovery_source(self, context, body):
        result = original_create(self, context, body)
        if body['name'] == 'Recovery browser fixture' and (root/'enable-recovery-fixture').exists():
            adapter = FakeCycleAdapter()
            async def lost(row, grant):
                adapter.complete(row, grant)
                raise ConnectionError('synthetic response loss')
            adapter.submit_hook = lost
            self.driver = SerialDriver(self.runtime, adapter)
            submit_write(context.handle, lambda db: commands.start_source(db,result['source_id'],
                [ItemSeed(1,'forest'),ItemSeed(2,'river')])).result()
            self.driver.dispatch(context,result['source_id'])
        if body['name'] == 'Direct unknown browser fixture' and (root/'enable-direct-review-fixture').exists():
            service = self.runtime.nai_direct
            service.token = lambda: 'synthetic-only'
            service.available = lambda: True
            async def direct_lost(*args, **kwargs):
                count = root/'direct-review-request-count'
                count.write_text(str(int(count.read_text())+1 if count.exists() else 1))
                raise ServiceError(503, 'nai:request_unknown', outcome='unknown')
            service.backend.generate = direct_lost
            submit_write(context.handle, lambda db: commands.start_source(db, result['source_id'],
                [ItemSeed(1, 'forest'), ItemSeed(2, 'river')])).result()
            service.driver.dispatch(context, result['source_id'])
        return result
    SourceService.create = recovery_source
    # Local fault injection only in this owned test process, after the deletion
    # barrier was saved. The marker selects a single UUID, never a filesystem path.
    from stylelora.prompt_production.storage import ProductFiles
    from stylelora.runtime.errors import ServiceError
    original_remove = ProductFiles.remove_checked
    def remove_with_fault(self, group, identifier, expected_digest):
        marker = root/'fail-library-remove-once'
        if group in ('coverage', 'dictionaries/features', 'dictionaries/prompts') and marker.exists() and marker.read_text(encoding='utf-8') == identifier:
            marker.unlink()
            raise ServiceError(503, 'prompt:cleanup_incomplete', outcome='unknown')
        return original_remove(self, group, identifier, expected_digest)
    ProductFiles.remove_checked = remove_with_fault
    # Isolate Launcher discovery from the user's environment; fixture schemas stay untrusted.
    from functools import partial
    from .mcp_support import LocalMcpServer
    from stylelora.backends.nai_launcher.client import McpClient
    from stylelora.backends.nai_launcher.discovery import discover
    from stylelora.services.backends import BackendService
    launcher = LocalMcpServer(root/'launcher').start()
    original = BackendService.__init__
    def backend_init(self, runtime):
        original(self, runtime, client_factory=partial(McpClient, discovery=partial(discover, launcher.appdata, environ={})))
    BackendService.__init__ = backend_init
    # Synthetic ONNX data and contracts exist only in this owned test process.
    from .tagger_support import inference_fixture, DEVICE
    from stylelora.services.tagger import TaggerService
    from stylelora.tagging.models import ModelStore
    from stylelora.tagging.manifests import REVIEWED_MANIFESTS
    fast, content = inference_fixture()
    slow, _ = inference_fixture(model_id='synthetic-slow')
    def model_stream(manifest, file, cancel):
        if manifest.model_id not in ('synthetic-tagger', 'synthetic-slow'):
            raise RuntimeError('Browser fixture never downloads real weights')
        if manifest.model_id == 'synthetic-slow':
            while (root/'hold-model-download').exists():
                if cancel.wait(.02):
                    return
        yield content[file.name]
    tagger_original = TaggerService.__init__
    def tagger_init(self, runtime, model_dir=None):
        tagger_original(self, runtime, manifests=(fast, slow, *REVIEWED_MANIFESTS),
                        store=ModelStore(root/'models', download=model_stream), device_probe=lambda: dict(DEVICE))
    TaggerService.__init__ = tagger_init
    from stylelora.services.jobs import JobService
    from stylelora.tagging.predictor import OrtTagger
    class BrowserTagger(OrtTagger):
        def predict_one(self, content, cancel):
            failure = root/'fail-next-wd-inference'
            if failure.exists():
                failure.unlink()
                from stylelora.runtime.errors import ServiceError
                raise ServiceError(503, 'tagger:inference_failed')
            result = super().predict_one(content, cancel)
            hold = root/'hold-wd-inference'
            if hold.exists():
                (root/'wd-inference-entered').write_text('entered', encoding='utf-8')
                import time
                while hold.exists() and not cancel.is_set():
                    time.sleep(.02)
            return result
    job_original = JobService.__init__
    def jobs_init(self, runtime):
        job_original(self, runtime, engine_factory=BrowserTagger)
    JobService.__init__ = jobs_init
    # The browser fixture never contacts a real LLM or reads real credentials.
    import httpx
    from stylelora.llm.service import LlmService
    llm_original = LlmService.__init__
    def llm_handler(request):
        if request.url.host != '127.0.0.1' or request.url.port != 19333:
            return httpx.Response(503)
        if request.headers.get('authorization') == 'Bearer bad-browser-secret':
            return httpx.Response(401, text='never expose upstream bodies')
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'fixture/vision', 'input_modalities': ['text', 'image']}]})
        from .test_llm import reply
        body = json.loads(request.content)
        payload = json.loads(body['messages'][1]['content'][0]['text'])
        if 'features' in payload:
            with (root/'prompt-calls.log').open('a', encoding='utf-8') as log:
                log.write('call\n')
            tags = [tag for values in payload['features'].values() for tag in values]
            if (root/'fail-prompt-tree-zero').exists() and 'tree 0' in tags:
                return httpx.Response(200, json=reply(model='fixture/vision', text=json.dumps({
                    'status': 'invalid_input', 'tags': [], 'natural_language': None, 'aspect': None,
                    'added': [], 'dropped': [], 'out_of_frame': [], 'reason': 'fixture candidate rejected'})))
            import re
            tags.sort(key=lambda t: 0 if t == 'no humans' or re.fullmatch(r'\d+(?:girl|boy|girls|boys)', t) else 1)
            return httpx.Response(200, json=reply(model='fixture/vision', text=json.dumps({
                'status': 'ok', 'tags': tags, 'natural_language': (
                    'A dreamy forest surrounds the river.' if (root/'review-prompt-output').exists() else
                    'Trees stand beside a river.') if payload['nl_enabled'] else None,
                'aspect': payload.get('aspect_hint') or 'landscape', 'added': [], 'dropped': [], 'out_of_frame': []})))
        if 'dataset_mode_policy' in payload:
            return httpx.Response(200, json=reply(model='fixture/vision', text=json.dumps(
                {'vision_tags': ['geometric objects', 'table'], 'vision_description': 'Geometric objects sit on a table.'})))
        return httpx.Response(200, json=reply(model='fixture/vision', text=json.dumps(
            {'left_color': 'red', 'left_shape': 'square', 'right_color': 'blue', 'right_shape': 'circle'})))
    def llm_init(self, runtime, directory=None, secret_dir=None, **kwargs):
        llm_original(self, runtime, directory, secret_dir, transport=httpx.MockTransport(llm_handler))
    LlmService.__init__ = llm_init
    try:
        serve(no_browser=True, runtime_dir=root/'runtime')
    finally:
        launcher.close()


if __name__ == '__main__':
    main()
