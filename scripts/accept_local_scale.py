"""Opt-in real-file capacity acceptance; synthetic images, no remote services.

Creates a new fixture under .test-tmp, imports and exports through the production
HTTP application and retains the project for browser acceptance. No user data.
"""
import argparse
import json
import platform
from pathlib import Path
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from stylelora.api.app import create_app
from stylelora.runtime.instance import ProductLock
from stylelora.runtime.lifecycle import Runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=10000)
    args = parser.parse_args()
    if not 1 <= args.count <= 10000:
        parser.error('count must be 1..10000')
    parent = Path(__file__).resolve().parents[1]/'.test-tmp'
    parent.mkdir(exist_ok=True)
    base = parent/('local-scale-'+uuid4().hex[:12]); base.mkdir()
    inputs = base/'inputs'; inputs.mkdir()
    root = base/'scale.slproj'
    report = {'schema_version': 1, 'dataset': {'synthetic': True, 'count': args.count,
        'width': 512, 'height': 384, 'format': 'JPEG', 'caption': True},
        'platform': platform.platform(), 'python': platform.python_version(), 'project': str(root), 'metrics': {}}
    def save():
        (parent/'completion-scale.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    def mark(label, start):
        report['metrics'][label+'_seconds'] = round(time.perf_counter()-start, 4)
        save(); print(label+': '+str(report['metrics'][label+'_seconds'])+' s', flush=True)
    owner = ProductLock()
    runtime = None
    try:
        started = time.perf_counter()
        for index in range(args.count):
            image = Image.new('RGB', (512, 384))
            draw = ImageDraw.Draw(image)
            for y in range(0, 384, 48):
                for x in range(0, 512, 64):
                    draw.rectangle((x,y,x+63,y+47), fill=((index*17+x+y)%256,(index*31+x*3)%256,(index*7+y*5)%256))
            draw.text((8, 8), f'local fixture {index:05d}', fill='white')
            path = inputs/f'{index:05d}.jpg'; image.save(path, quality=90)
            path.with_suffix('.txt').write_text('landscape, tree\n\nA tree stands in a landscape.', encoding='utf-8')
        report['dataset']['input_bytes'] = sum(p.stat().st_size for p in inputs.iterdir())
        mark('generate_fixture', started)
        runtime = Runtime(product_dir=base/'product', llm_dir=base/'config', secret_dir=base/'private')
        runtime.open_now(root, create=True, name='Local scale acceptance', trigger_word='scale_style')
        pid = runtime.context.project_id; prefix = '/api/projects/'+pid
        with TestClient(create_app(runtime, manage_lifecycle=False), base_url=runtime.origin,
                        headers={'Authorization': 'Bearer '+runtime.token}) as client:
            def call(method, path, body=None):
                result = client.request(method, path, json=body, headers={'Idempotency-Key': str(uuid4())})
                if not result.is_success:
                    raise RuntimeError(f'{method} {path}: {result.status_code} {result.text[:1000]}')
                return result.json()
            def wait(value, label):
                url = value.get('status_url') or '/api/operations/'+value['operation_id']
                start, last = time.monotonic(), 0
                while time.monotonic()-start < 1800:
                    result = call('GET', url)
                    if result['status'] == 'done':
                        return result['result']
                    if result['status'] in ('failed','canceled','interrupted'):
                        raise RuntimeError(json.dumps(result.get('error')))
                    if time.monotonic()-last > 30:
                        print(label+' running '+str(round(time.monotonic()-start))+' s', flush=True); last=time.monotonic()
                    time.sleep(.15)
                raise RuntimeError(label+' timed out')
            grant = runtime.grant(inputs, 'import_root', pid)
            started = time.perf_counter()
            preview = wait(call('POST', prefix+'/imports/preview', {'root_handle': grant['grant_id']}), 'import_preview')
            assert preview['can_import'] and preview['selected'] == args.count
            mark('import_preview', started)
            started = time.perf_counter()
            imported = wait(call('POST', prefix+'/imports', {'preview_id': preview['preview_id'], 'digest': preview['digest']}), 'import')
            assert imported['imported'] == args.count, imported
            mark('import', started)
            latencies, seen, cursor = [], 0, None
            while True:
                started = time.perf_counter()
                page = call('GET', prefix+'/images?limit=1000'+('&cursor='+cursor if cursor else ''))
                latencies.append(time.perf_counter()-started); seen += len(page['items'])
                cursor = page['next_cursor']
                if not cursor:
                    break
            assert seen == args.count
            report['metrics']['list_pages'] = len(latencies)
            report['metrics']['list_page_p95_seconds'] = sorted(latencies)[max(0, int(len(latencies)*.95)-1)]
            for phase in ('cold', 'warm'):
                timings = []
                for index in range(1, min(args.count, 200)+1):
                    started = time.perf_counter()
                    result = client.get(prefix+f'/images/{index}/thumbnail?size=256')
                    assert result.status_code == 200
                    timings.append(time.perf_counter()-started)
                report['metrics']['thumbnail_'+phase+'_p95_seconds'] = sorted(timings)[int(len(timings)*.95)-1]
            save()
            started = time.perf_counter()
            preview = wait(call('POST', prefix+'/exports/preview', {'options': {'include_unreviewed': True}}), 'export_preview')
            mark('export_preview', started)
            started = time.perf_counter()
            accepted = call('POST', prefix+'/exports', {'preview_id': preview['preview_id'], 'digest': preview['digest']})
            exported = wait({'operation_id': accepted['operation_id']}, 'export')
            assert exported['exported'] == args.count and exported['failed'] == 0, exported
            mark('export', started)
            report['export_id'] = exported['export_id']
            started = time.perf_counter()
            wait(call('POST', prefix+'/storage/scans', {}), 'storage_scan')
            mark('storage_scan', started)
        # Gates describe this measured fixture, not arbitrary large photographs.
        gates = {'all_images_imported_and_exported': True,
                 'list_p95_under_1s': report['metrics']['list_page_p95_seconds'] < 1,
                 'thumbnail_cold_p95_under_250ms': report['metrics']['thumbnail_cold_p95_seconds'] < .25,
                 'import_under_15min': report['metrics']['import_seconds'] < 900,
                 'export_under_10min': report['metrics']['export_seconds'] < 600}
        report['gates'] = gates; report['passed'] = all(gates.values()); save()
        print(json.dumps({'passed': report['passed'], 'report': str(parent/'completion-scale.json')}), flush=True)
        return 0 if report['passed'] else 1
    finally:
        if runtime is not None:
            runtime.shutdown()
        owner.close()


if __name__ == '__main__':
    raise SystemExit(main())
