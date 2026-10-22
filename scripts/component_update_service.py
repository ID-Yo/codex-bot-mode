"""Local update workflow adapter for CLI/Office, with explicit installation scope.

No HTTP server, dispatch, credentials, network or arbitrary command execution.
The Office handler supplies authentication and maps requests to configured IDs.
"""
from copy import deepcopy
from pathlib import Path
import secrets
import threading

from component_release import verify_bundle


class UpdateServiceError(ValueError):
    pass


class UpdateService:
    def __init__(self, projects, inventory_reader, planner, applier):
        self.projects = {name: Path(path).resolve(strict=True) for name, path in projects.items()}
        self.inventory_reader = inventory_reader
        self.planner = planner
        self.applier = applier
        self.lock = threading.RLock()
        self.releases = {}
        self.plans = {}
        self.history = []

    def _project(self, identity):
        if not isinstance(identity, str) or identity not in self.projects:
            raise UpdateServiceError('Select a configured project identity')
        return self.projects[identity]

    def inventory(self, project_id):
        # Reader already enforces no-link metadata and protected-state boundaries.
        return deepcopy(self.inventory_reader(self._project(project_id)))

    def register_download(self, release_id, envelope, bundle, trust, platform):
        """Host-only registration of a bounded verified local download.

        The UI receives only the opaque release ID, never a client-controlled
        trust policy, key, URL or arbitrary local path. Verification is repeated
        by the transaction engine before apply; this step does not authorize it.
        """
        payload = verify_bundle(envelope, bundle, trust, platform)
        if release_id != payload['release_id']:
            raise UpdateServiceError('Release identity does not match verified payload')
        with self.lock:
            if release_id in self.releases:
                raise UpdateServiceError('Release identity already registered')
            self.releases[release_id] = {'envelope': deepcopy(envelope), 'bundle': Path(bundle),
                                         'trust': deepcopy(trust), 'platform': platform,
                                         'state': 'downloaded_and_verified'}
        return {'release_id': release_id, 'state': 'downloaded_and_verified', 'applied': False}

    def preview(self, project_id, release_id, targets, components, resolutions=None, delete_paths=()):
        project = self._project(project_id)
        with self.lock:
            if not isinstance(release_id, str) or not release_id:
                raise UpdateServiceError('Select a verified release identity')
            release = self.releases.get(release_id)
            if release is None:
                raise UpdateServiceError('Verified release is unavailable')
            if not isinstance(components, list) or not components:
                raise UpdateServiceError('Select explicit components')
            # Mapping is to inventory IDs, not arbitrary paths; the transaction
            # engine validates it against the current local inventory and roster.
            plan = self.planner(project, deepcopy(release['envelope']), release['bundle'],
                                deepcopy(release['trust']), release['platform'], deepcopy(targets),
                                list(components), resolutions=deepcopy(resolutions), delete_paths=delete_paths)
            digest = plan.get('plan_sha256')
            if not isinstance(digest, str) or len(digest) != 64:
                raise UpdateServiceError('Planner did not return an exact approval digest')
            handle = secrets.token_urlsafe(24)
            self.plans[handle] = {'project_id': project_id, 'release_id': release_id,
                                  'plan': deepcopy(plan), 'targets': deepcopy(targets), 'state': 'preview'}
            return {'handle': handle, 'project_id': project_id, 'release_id': release_id,
                    'state': 'conflict' if plan.get('conflicts') else 'preview', 'plan': deepcopy(plan)}

    def apply(self, handle, approved_digest):
        with self.lock:
            if not isinstance(handle, str) or handle not in self.plans:
                raise UpdateServiceError('Preview is unavailable; create a fresh plan')
            entry = self.plans[handle]
            plan = entry['plan']
            if not isinstance(approved_digest, str) or not secrets.compare_digest(plan['plan_sha256'], approved_digest):
                raise UpdateServiceError('Approval does not match the exact preview')
            if plan.get('conflicts'):
                raise UpdateServiceError('Resolve conflicts and preview again before applying')
            if entry['state'] == 'applied':
                return deepcopy(entry['result'])
            release = self.releases[entry['release_id']]
            # Hash/context revalidation and durable recovery remain the engine's
            # contract, not optimistic UI state. Errors never advance this status.
            result = self.applier(deepcopy(plan), approved_digest, deepcopy(release['envelope']),
                                  release['bundle'], deepcopy(release['trust']), release['platform'],
                                  deepcopy(entry['targets']))
            if result.get('status') not in ('applied', 'no_op'):
                raise UpdateServiceError('Transaction did not confirm application')
            entry['state'] = 'applied'
            public = {'project_id': entry['project_id'], 'release_id': entry['release_id'],
                      'state': 'applied_pending_activation', 'plan_sha256': approved_digest,
                      'transaction': result.get('transaction'), 'verified_active': False}
            entry['result'] = public
            self.history.append(deepcopy(public))
            return public

    def status(self):
        with self.lock:
            return {'releases': [{'release_id': key, 'state': value['state']} for key, value in self.releases.items()],
                    'history': deepcopy(self.history), 'durability': 'engine_journal',
                    'discovery_freshness': 'not_checked'}
