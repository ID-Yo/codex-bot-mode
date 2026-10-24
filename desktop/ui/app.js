const { invoke } = window.__TAURI__.core;
const dialog = window.__TAURI__.dialog;
const byId = (id) => document.getElementById(id);
let busy = false;
let busyState;
let refreshTask;
let viewRevision = 0;
let lastView;

function notify(message) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.hidden = false;
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => { toast.hidden = true; }, 6000);
}

function actionMessage(message = '', kind = '') {
  const target = byId('action-message');
  target.textContent = message;
  target.dataset.kind = kind;
}

function setStatus(status) {
  const state = status.state || 'unverified';
  const labels = {
    running: 'Running', starting: 'Starting', stopping: 'Stopping',
    stopped: 'Stopped', unhealthy: 'Unverified', unverified: 'Unverified',
    preserved: 'Child preserved', notConfigured: 'Setup needed', notStarted: 'Not started'
  };
  byId('status-badge').dataset.state = state;
  byId('status-label').textContent = labels[state] || 'Unverified';

  let detail = status.reason || '';
  if (state === 'running') {
    detail = status.safety === 'safe'
      ? 'Authenticated health is responding. Office is ready in Chrome.'
      : status.safety === 'busy'
        ? `Office is healthy; ${status.busyCount || 0} active operation(s) are protected.`
        : 'Office responds, but owned work could not be fully verified.';
    if (status.pendingCount) detail += ` ${status.pendingCount} queued item(s) remain saved.`;
  } else if (state === 'starting') {
    detail ||= 'Waiting for authenticated service health…';
  } else if (state === 'stopping') {
    detail ||= 'Waiting for Office work to reach a safe stop boundary.';
  } else if (state === 'stopped') {
    detail ||= status.pendingCount ? `Stopped safely; ${status.pendingCount} queued item(s) remain saved.` : 'The owned service confirmed a clean stop.';
  } else if (state === 'notConfigured') {
    detail ||= 'Choose the project that contains the Office profiles you want to serve.';
  } else if (state === 'notStarted') {
    detail ||= 'No authenticated Office service has been started from this controller yet.';
  } else {
    detail ||= 'Service ownership or health is unverified. The controller will not attach to or stop an unknown listener.';
  }
  byId('status-detail').textContent = detail;
}

function render(view) {
  lastView = view;
  byId('project-path').textContent = view.projectPath || 'Choose the project that contains your Office profiles.';
  byId('project-path').title = view.projectPath || '';
  byId('codex-path').textContent = view.codexPath || 'Codex Desktop runtime not found. Install Codex or locate codex.exe.';
  byId('codex-path').title = view.codexPath || '';
  setStatus(busyState ? { state: busyState } : (view.status || {}));
  for (const id of ['choose-project', 'choose-codex', 'refresh', 'quit']) byId(id).disabled = busy;
  byId('logs').textContent = (view.logs || []).join('\n') || 'No verified service log is available.';
  byId('logs-last').textContent = (view.logs || []).at(-1) || 'No verified service log is available.';

  const ready = !busy && Boolean(view.projectPath) && Boolean(view.codexPath);
  byId('start').disabled = !ready || !view.status?.canStart;
  byId('stop').disabled = busy || !view.status?.canStop;
  byId('restart').disabled = !ready || !view.status?.canRestart;
  byId('open-office').disabled = busy || !view.status?.canOpen;
  byId('cancel-stop').hidden = !view.status?.canCancelStop;
  byId('cancel-stop').disabled = busy;
  byId('recover-startup').hidden = !view.status?.canRecover;
  byId('recover-startup').disabled = busy;
  byId('preserve-startup').hidden = !view.status?.canPreserve;
  byId('preserve-startup').disabled = busy;
  byId('clear-preserved').hidden = !view.status?.canClearPreserved;
  byId('clear-preserved').disabled = busy;
  byId('quit-preserved').hidden = !view.status?.canQuitPreserved;
  byId('quit-preserved').disabled = busy;
}

async function refresh() {
  if (refreshTask) return refreshTask;
  const revision = viewRevision;
  refreshTask = (async () => {
    try {
      const view = await invoke('get_view');
      if (revision === viewRevision) render(view);
    } catch (error) {
      if (revision === viewRevision) actionMessage(String(error), 'error');
    }
  })();
  try {
    await refreshTask;
  } finally {
    refreshTask = undefined;
  }
}

async function act(label, command, state) {
  if (busy) return;
  busy = true;
  viewRevision += 1;
  busyState = state;
  actionMessage(label);
  render(lastView || { status: { state: 'unverified' } });
  try {
    const result = await command();
    if (result?.state === 'stopping' || result?.stopPending) {
      actionMessage('Stop is pending. Active or unverified work remains protected.', 'pending');
    } else {
      actionMessage('');
    }
  } catch (error) {
    actionMessage(String(error), 'error');
  } finally {
    busy = false;
    busyState = undefined;
    if (refreshTask) await refreshTask;
    await refresh();
  }
}

byId('choose-project').addEventListener('click', async () => {
  try {
    const path = await dialog.open({ directory: true, multiple: false, title: 'Choose an Office project' });
    if (typeof path === 'string') {
      const view = await invoke('configure_project', { projectPath: path });
      actionMessage('');
      render(view);
    }
  } catch (error) { notify(String(error)); }
});

byId('choose-codex').addEventListener('click', async () => {
  try {
    const path = await dialog.open({ directory: false, multiple: false, title: 'Locate Codex Desktop codex.exe',
      filters: [{ name: 'Codex Desktop', extensions: ['exe'] }] });
    if (typeof path === 'string') render(await invoke('configure_codex', { codexPath: path }));
  } catch (error) { notify(String(error)); }
});

byId('start').addEventListener('click', () => act('Starting the bundled Office service…', () => invoke('start_service'), 'starting'));
byId('stop').addEventListener('click', () => act('Requesting a safe drain…', () => invoke('stop_service'), 'stopping'));
byId('restart').addEventListener('click', () => act('Stopping safely before restart…', () => invoke('restart_service'), 'stopping'));
byId('cancel-stop').addEventListener('click', () => act('Requesting cancellation of the pending stop…', () => invoke('cancel_stop')));
byId('recover-startup').addEventListener('click', () => act('Rechecking the existing owned service; no new process will start…', () => invoke('recover_startup'), 'starting'));
byId('preserve-startup').addEventListener('click', async () => {
  const confirmed = await dialog.ask(
    'The controller will quit without stopping or killing this exact startup child. Office work may still be active and will no longer be monitored here. A safety lock will block another start next time until you verify this process has stopped and clear the lock.',
    { title: 'Leave the Office child running', kind: 'warning' }
  );
  if (confirmed) await act('Saving the preserved-child safety lock and exiting the controller…', () =>
    invoke('preserve_startup_child', { confirmed: true }));
});
byId('clear-preserved').addEventListener('click', async () => {
  const confirmed = await dialog.ask(
    'Only continue after you have verified that the previously preserved Office process has stopped. Clearing this lock permits Start to launch a new service. The controller will not terminate any process.',
    { title: 'Clear preserved-child safety lock', kind: 'warning' }
  );
  if (confirmed) await act('Clearing the manually verified preserved-child safety lock…', () =>
    invoke('clear_preserved_child', { confirmedStopped: true }));
});
byId('quit-preserved').addEventListener('click', async () => {
  const confirmed = await dialog.ask(
    'The controller will exit and keep its safety lock. The previously preserved Office process may still be active and will not be monitored or stopped.',
    { title: 'Quit controller and keep safety lock', kind: 'warning' }
  );
  if (confirmed) await act('Quitting the controller and retaining the safety lock…', () =>
    invoke('quit_preserved_controller', { confirmed: true }));
});
byId('refresh').addEventListener('click', refresh);

byId('open-office').addEventListener('click', () => act('Opening Office in Chrome…', async () => {
  const opened = await invoke('open_chrome');
  if (opened) return;
  const fallback = await dialog.ask('Chrome was not found. Open the local Office page in your default browser instead?', {
    title: 'Chrome is unavailable', kind: 'info'
  });
  if (fallback) await invoke('open_default_browser');
}));

byId('quit').addEventListener('click', () => act('Checking the safe quit boundary…', () => invoke('quit_application')));

window.__TAURI__.event.listen('office-open-requested', () => {
  if (lastView?.status?.canOpen) byId('open-office').click();
  else notify('Office is not verified as running. Start it and confirm its health before opening Chrome.');
});
window.__TAURI__.event.listen('office-quit-requested', () => byId('quit').click());

refresh();
window.setInterval(() => { if (!busy) refresh(); }, 3500);
