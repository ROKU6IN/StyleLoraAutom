"""Generate the local API schema used by frontend request types; no running service needed."""
import json
from pathlib import Path

from stylelora.api.app import create_app
from stylelora.runtime.lifecycle import Runtime

root = Path(__file__).resolve().parents[1]
runtime = Runtime()
try:
    output = root/'.test-tmp/openapi.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(create_app(runtime).openapi(), ensure_ascii=False, indent=2), encoding='utf-8')
    print('Generated .test-tmp/openapi.json')
finally:
    runtime.shutdown()
