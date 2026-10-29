"""Verify packaged UI, SQL and runtime rules; run regressions from an isolated wheel."""
import sys
import tempfile
import unittest
import tomllib
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
wheel = root/f'.test-tmp/wheels/stylelora-{version}-py3-none-any.whl'
target = Path(tempfile.mkdtemp(prefix='f14-wheel-package-', dir=root/'.test-tmp')).resolve()
if not target.is_relative_to((root/'.test-tmp').resolve()):
    raise RuntimeError('Unexpected package-check directory')
with ZipFile(wheel) as archive:
    metadata = BytesParser().parsebytes(archive.read(f'stylelora-{version}.dist-info/METADATA'))
    if 'PyYAML==6.0.3' not in metadata.get_all('Requires-Dist', []):
        raise RuntimeError('Packaged YAML dependency missing')
    for member in archive.namelist():
        if not (target/member).resolve().is_relative_to(target):
            raise RuntimeError('Unsafe wheel member')
    assets = {path.relative_to(root/'src').as_posix(): path.read_bytes()
              for path in (root/'src/stylelora/api/web_dist').rglob('*') if path.is_file()}
    actual = {name for name in archive.namelist() if name.startswith('stylelora/api/web_dist/')}
    if set(assets) != actual:
        raise RuntimeError('Packaged web assets differ from the current build')
    for name, content in assets.items():
        if archive.read(name) != content:
            raise RuntimeError('Packaged asset content mismatch')
    sources = {path.relative_to(root/'src').as_posix(): path.read_bytes()
               for path in (root/'src/stylelora').rglob('*.py')}
    packaged_sources = {name for name in archive.namelist() if name.startswith('stylelora/') and name.endswith('.py')}
    if set(sources) != packaged_sources or any(archive.read(name) != content for name, content in sources.items()):
        raise RuntimeError('Packaged Python sources differ from the current source tree')
    for path in (root/'src/stylelora/llm/data').glob('*'):
        if path.is_file() and path.suffix in ('.json', '.md'):
            if archive.read(path.relative_to(root/'src').as_posix()) != path.read_bytes():
                raise RuntimeError('Packaged LLM template content mismatch')
    for path in (root/'src/stylelora/caption/data').glob('*'):
        if path.is_file() and archive.read(path.relative_to(root/'src').as_posix()) != path.read_bytes():
            raise RuntimeError('Packaged sorting contract/fixture mismatch')
    for name, packaged in (('vision-captioner', 'r2.md'), ('rating-classifier', 'r5.md')):
        if (root/'skills'/name/'SKILL.md').read_bytes() != archive.read('stylelora/llm/data/'+packaged):
            raise RuntimeError('Runtime template differs from its reviewed source')
    if not any(name.startswith('stylelora/project_db/migrations/') and name.endswith('.sql')
               for name in archive.namelist()):
        raise RuntimeError('Packaged database migration missing')
    archive.extractall(target)
sys.path[:0] = [str(target), str(root)]
import stylelora
assert Path(stylelora.__file__).resolve().is_relative_to(target)
assert stylelora.__version__ == version
print(f'Package verified: {len(sources)} Python sources, {len(assets)} web assets, SQL, runtime rules, version and isolated import', flush=True)
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'tests.test_api_loop', 'tests.test_api_security', 'tests.test_workbench_api',
    'tests.test_cleaning_api', 'tests.test_catalog_api',
    'tests.test_exchange_schemes_api', 'tests.test_tokenizer',
    'tests.test_import_encoding', 'tests.test_sidecar',
    'tests.test_importer',
    'tests.test_hardlink_export',
    'tests.test_pyramid_sorter',
    'tests.test_mcp_client', 'tests.test_api_backends',
    'tests.test_launcher_readonly',
    'tests.test_launcher_check',
    'tests.test_nai_direct',
    'tests.test_nai_manual_review',
    'tests.test_generation_parameters',
    'tests.test_startup',
    'tests.test_standalone_cli',
    'tests.test_session_cli',
    'tests.test_native_picker_process',
    'tests.test_phash', 'tests.test_api_duplicates',
    'tests.test_tagger_models', 'tests.test_api_tagger',
    'tests.test_tagger_predictor', 'tests.test_api_jobs',
    'tests.test_wd_acceptance',
    'tests.test_tagger_batch',
    'tests.test_llm', 'tests.test_api_llm',
    'tests.test_api_vision', 'tests.test_api_job_batches', 'tests.test_executor_workbench',
    'tests.test_prompt_production', 'tests.test_prompt_composer', 'tests.test_generation_handoff',
    'tests.test_prompt_management',
    'tests.test_dictionary_exchange',
    'tests.test_generation_presets',
    'tests.test_generation_artifacts',
    'tests.test_settings_leases',
    'tests.test_generation_costs',
    'tests.test_generation_runner',
    'tests.test_generation_recovery',
    'tests.test_storage',
    'tests.test_recent_projects',
    'tests.test_storage_config',
    'tests.test_product_defaults',
    'tests.test_export_recovery',
    'tests.test_storage_cli',
    'tests.test_project_cli',
    'tests.test_job_cli',
    'tests.test_prompt_cli',
    'tests.test_source_cli',
    'tests.test_source_mutations',
    'tests.test_import_cli',
    'tests.test_export_cli',
    'tests.test_image_verification',
    'tests.test_doctor',
    'tests.test_doctor_content',
    'tests.test_doctor_network',
    'tests.test_coverage_plans',
    'tests.test_coverage_editor',
    'tests.test_library_deletion',
    'tests.test_prompt_review',
    'tests.test_coverage_matrix',
])
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
