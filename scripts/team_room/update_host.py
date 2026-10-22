"""Connect the shared update engine to trusted local Office configuration."""
from pathlib import Path
import sys


def build(project):
    """Return None until an operator explicitly configures this host."""
    root = Path(project).resolve(strict=True)
    file = root / '.bot-mode/local/office-updates.json'
    if not file.exists():
        return None
    from .installation_inventory import _safe_file
    if not _safe_file(root, '.bot-mode/local/office-updates.json') or file.stat().st_size > 1024 * 1024:
        raise ValueError('Unsafe or oversized local update configuration')
    from ..component_release import read_json
    config = read_json(file)
    fields = {'schema', 'platform', 'source', 'cache', 'trust', 'projects', 'installed_versions'}
    if not isinstance(config, dict) or set(config) != fields or type(config['schema']) is not int or config['schema'] != 1:
        raise ValueError('Invalid local update configuration')
    projects = config['projects']
    if not isinstance(projects, dict) or not projects:
        raise ValueError('Explicit project identities are required')
    from . import registry
    configured = registry.load_registry(registry.registry_path(root))
    allowed = {item['id']: Path(item['root']).resolve(strict=True) for item in configured['projects']}
    if any(pid not in allowed or Path(path).resolve(strict=True) != allowed[pid] for pid, path in projects.items()):
        raise ValueError('Update projects must match configured Office identities')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from component_update_service import UpdateService
    from component_update_host import HostCache
    from component_update_status import read_history
    from profile_update import plan_update, apply_plan
    from .installation_inventory import discover
    for name in ('source', 'cache', 'trust'):
        if not isinstance(config[name], str) or not Path(config[name]).is_absolute():
            raise ValueError('Host update paths must be explicit absolute paths')
    trust = read_json(config['trust'])
    versions = config['installed_versions']
    if not isinstance(versions, dict):
        raise ValueError('Explicit installed release-version mapping is required')
    cache = HostCache(config['source'], config['cache'], config['platform'])

    class OfficeUpdates(UpdateService):
        available_components = {}
        def check(self):
            result = cache.check(trust=trust, project_map=versions)
            if result.get('state') == 'unavailable':
                raise ValueError(result.get('error', 'Release check failed'))
            return result

        def stage(self, release_id):
            result = cache.stage(release_id, trust=trust, project_map=versions)
            self._register_cached()
            return {'release_id': result.release_id, 'state': 'downloaded_and_verified', 'applied': False}

        def _register_cached(self):
            if not cache.status()['cached_releases']:
                return
            for entry in cache.verified_entries(trust=trust, project_map=versions):
                self.available_components[entry.release_id] = [
                    {key: item[key] for key in ('id', 'kind', 'version', 'requires')}
                    for item in entry.payload['components']]
                if entry.release_id not in self.releases:
                    self.register_download(entry.release_id, entry.envelope, entry.bundle_path, trust, config['platform'])

        def status(self):
            current = super().status()
            durable = read_history(self.projects)
            discovery = cache.status()
            current.update(durable)
            current['components'] = dict(self.available_components)
            current.update(configured=True, projects=list(self.projects), discovery=discovery,
                           discovery_freshness='recorded' if discovery.get('last_check') else 'not_checked')
            return current

    service = OfficeUpdates(projects, discover, plan_update, apply_plan)
    service.available_components = {}
    service._register_cached()
    return service
