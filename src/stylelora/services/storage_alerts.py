"""Read only the last in-memory observations; never initiate an inventory."""
from copy import deepcopy


def summary(runtime):
    config = runtime.product_config.get()
    with runtime.condition:
        current = runtime.context
        pid = current.project_id if current and not current.closing else None
    with runtime.storage.lock:
        scan = deepcopy(runtime.storage.summaries.get(pid)) if pid else None
    project = None
    if scan is not None:
        keys = ('scan_id', 'as_of', 'total_bytes', 'partial', 'exceeds_threshold', 'project_threshold_bytes', 'config_revision')
        project = {key: scan[key] for key in keys}
        project.update(project_id=pid, stale=True, config_changed=scan['config_revision'] != config['revision'])
    known = runtime.known_storage.latest_summary()
    if known is not None:
        known.update(stale=True, config_changed=known['config_revision'] != config['revision'])
    return {'current_project': project, 'known_projects': known, 'config_revision': config['revision'],
            'storage_thresholds': config['storage_thresholds']}
