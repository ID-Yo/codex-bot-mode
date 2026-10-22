"""Narrow update request boundary. Hosts supply the trusted update service."""


def dispatch(service, path, data):
    actions = {
        '/api/updates/check': ('check', set(), set()),
        '/api/updates/stage': ('stage', {'release_id'}, set()),
        '/api/updates/inventory': ('inventory', {'project_id'}, set()),
        '/api/updates/preview': ('preview', {'project_id', 'release_id', 'targets', 'components'}, {'resolutions', 'delete_paths'}),
        '/api/updates/apply': ('apply', {'handle', 'approved_digest'}, set()),
    }
    if path not in actions:
        raise ValueError('Unknown update action')
    method, required, optional = actions[path]
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
        raise ValueError('Invalid update request fields')
    if service is None:
        raise ValueError('Trusted update source is not configured; use the recovery CLI')
    return getattr(service, method)(**data)
