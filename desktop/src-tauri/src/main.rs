#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    fs,
    io::{Read, Write},
    net::{TcpStream, ToSocketAddrs},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, MutexGuard},
    time::{Duration, Instant},
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    AppHandle, Emitter, Manager, State, WindowEvent,
};
mod liveness;

use tauri_plugin_opener::OpenerExt;
use tauri_plugin_shell::{process::CommandEvent, ShellExt};

const SERVICE_NAME: &str = "codex-office";
const SERVICE_VERSION: &str = "0.1.0";
const PORT: u16 = 24242;
const OFFICE_URL: &str = "http://localhost:24242";
const MAX_HTTP_RESPONSE: usize = 128 * 1024;

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct Settings {
    project_path: Option<PathBuf>,
    codex_path: Option<PathBuf>,
    owner: Option<Owner>,
    #[serde(default)]
    preserved_child: Option<PreservedChild>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct Owner {
    project_path: PathBuf,
    pid: u32,
    instance_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct PreservedChild {
    project_path: PathBuf,
    pid: u32,
}

#[derive(Debug, Deserialize)]
struct Descriptor {
    protocol: u8,
    service: String,
    version: String,
    pid: u32,
    port: u16,
    #[serde(default)]
    parent_pid: u32,
    instance_id: String,
    token: String,
    state: String,
}

struct ChildState<T = tauri_plugin_shell::process::CommandChild> {
    child: Option<T>,
    pid: Option<u32>,
    launch_token: String,
    running: bool,
    starting: bool,
    safe_to_exit: bool,
    startup_error: Option<String>,
}

impl<T> Default for ChildState<T> {
    fn default() -> Self {
        Self {
            child: None,
            pid: None,
            launch_token: String::new(),
            running: false,
            starting: false,
            safe_to_exit: true,
            startup_error: None,
        }
    }
}

struct OfficeController {
    settings_file: PathBuf,
    settings: Mutex<Settings>,
    child: Mutex<ChildState>,
    action: Mutex<()>,
    #[cfg(test)]
    test_port: u16,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct ServiceStatus {
    state: String,
    safety: String,
    busy_count: u64,
    pending_count: u64,
    stop_pending: bool,
    reason: String,
    can_start: bool,
    can_stop: bool,
    can_restart: bool,
    can_open: bool,
    can_cancel_stop: bool,
    can_recover: bool,
    can_preserve: bool,
    can_clear_preserved: bool,
    can_quit_preserved: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct View {
    project_path: Option<String>,
    codex_path: Option<String>,
    status: ServiceStatus,
    logs: Vec<String>,
}

impl OfficeController {
    fn expected_port(&self) -> u16 {
        #[cfg(test)]
        { self.test_port }
        #[cfg(not(test))]
        { PORT }
    }

    fn load(app: &AppHandle) -> Result<Self, String> {
        let directory = app.path().app_local_data_dir().map_err(display_error)?;
        fs::create_dir_all(&directory).map_err(display_error)?;
        let settings_file = directory.join("controller.json");
        let mut settings = load_settings(&settings_file)?;
        if settings.codex_path.is_none() {
            settings.codex_path = find_codex();
        }
        let controller = Self {
            settings_file,
            settings: Mutex::new(settings),
            child: Mutex::new(ChildState { safe_to_exit: true, ..ChildState::default() }),
            action: Mutex::new(()),
            #[cfg(test)]
            test_port: PORT,
        };
        controller.save_settings()?;
        Ok(controller)
    }

    fn settings(&self) -> Result<Settings, String> {
        ensure_settings_settled(&self.settings_file)?;
        self.settings.lock().map(|value| value.clone()).map_err(display_error)
    }

    fn save_settings(&self) -> Result<(), String> {
        write_settings(&self.settings_file, &self.settings()?)
    }

    fn child_state(&self) -> Result<MutexGuard<'_, ChildState>, String> {
        self.child.lock().map_err(|_| "Controller lifecycle state is unavailable after an internal failure.".to_owned())
    }
}

// An unfinished journal is ambiguous even if controller.json still exists.
// Never turn an interrupted safety-marker rewrite into default settings.
fn ensure_settings_settled(settings_file: &Path) -> Result<(), String> {
    for extension in ["json.tmp", "json.bak"] {
        if settings_file.with_extension(extension).try_exists().map_err(display_error)? {
            return Err("Controller settings have an interrupted write; startup and controls are blocked. Keep controller.json, .tmp and .bak for recovery; do not reset them while a preserved child may be running.".to_owned());
        }
    }
    Ok(())
}

fn load_settings(settings_file: &Path) -> Result<Settings, String> {
    ensure_settings_settled(settings_file)?;
    match fs::read(settings_file) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map_err(|_| "Controller settings are unreadable; no values were replaced.".to_owned()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Settings::default()),
        Err(error) => Err(display_error(error)),
    }
}

fn write_settings(settings_file: &Path, settings: &Settings) -> Result<(), String> {
    ensure_settings_settled(settings_file)?;
    let bytes = serde_json::to_vec_pretty(settings).map_err(display_error)?;
    let temporary = settings_file.with_extension("json.tmp");
    let backup = settings_file.with_extension("json.bak");
    // Exclusive creation also prevents overlapping writers. On any error leave
    // the journal intact: this controller and the next launch must fail closed.
    let mut file = fs::OpenOptions::new().write(true).create_new(true)
        .open(&temporary).map_err(display_error)?;
    file.write_all(&bytes).map_err(display_error)?;
    file.sync_all().map_err(display_error)?;
    drop(file);
    let had_settings = settings_file.try_exists().map_err(display_error)?;
    if had_settings {
        fs::rename(settings_file, &backup).map_err(display_error)?;
    }
    fs::rename(&temporary, settings_file).map_err(display_error)?;
    if had_settings {
        fs::remove_file(&backup).map_err(display_error)?;
    }
    Ok(())
}

fn display_error(error: impl std::fmt::Display) -> String {
    error.to_string()
}

fn runtime_dir(project: &Path) -> PathBuf {
    project.join(".bot-mode").join("team-room-v1").join("desktop-controller")
}

fn descriptor_path(project: &Path) -> PathBuf {
    runtime_dir(project).join("desktop.json")
}

fn read_descriptor(project: &Path) -> Result<Descriptor, String> {
    read_descriptor_at_port(project, PORT)
}

fn read_descriptor_at_port(project: &Path, port: u16) -> Result<Descriptor, String> {
    let runtime = checked_existing_runtime(project)?;
    let bytes = fs::read(runtime.join("desktop.json")).map_err(|_| "No controller descriptor is available.".to_owned())?;
    let descriptor: Descriptor = serde_json::from_slice(&bytes)
        .map_err(|_| "Controller descriptor is invalid; the listener remains untouched.".to_owned())?;
    if descriptor.protocol != 1 || descriptor.service != SERVICE_NAME || descriptor.version != SERVICE_VERSION
        || descriptor.port != port || descriptor.pid == 0 || descriptor.token.is_empty()
        || descriptor.instance_id.is_empty()
    {
        return Err("Controller descriptor identity does not match this Office build.".to_owned());
    }
    Ok(descriptor)
}

fn owner_matches(settings: &Settings, project: &Path, descriptor: &Descriptor) -> bool {
    settings.owner.as_ref().is_some_and(|owner| {
        owner.project_path == project && owner.pid == descriptor.pid
            && owner.instance_id == descriptor.instance_id
    })
}

fn http_request(descriptor: &Descriptor, method: &str, endpoint: &str) -> Result<(u16, Value), String> {
    let address = ("127.0.0.1", descriptor.port).to_socket_addrs()
        .map_err(display_error)?.next().ok_or_else(|| "Loopback address is unavailable.".to_owned())?;
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2))
        .map_err(|_| "The authenticated Office listener did not respond.".to_owned())?;
    // Native Desktop safety inspection can outlast a health probe (e.g. stale relay).
    let wait = match endpoint { "/desktop/shutdown" => 65, "/desktop/status" => 10, _ => 4 };
    stream.set_read_timeout(Some(Duration::from_secs(wait))).map_err(display_error)?;
    stream.set_write_timeout(Some(Duration::from_secs(4))).map_err(display_error)?;
    write!(stream,
        "{method} {endpoint} HTTP/1.1\r\nHost: localhost:{}\r\nOrigin: http://tauri.localhost\r\nX-Office-Controller-Token: {}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
        descriptor.port, descriptor.token).map_err(display_error)?;
    let mut response = Vec::new();
    stream.take(MAX_HTTP_RESPONSE as u64).read_to_end(&mut response).map_err(display_error)?;
    let boundary = response.windows(4).position(|part| part == b"\r\n\r\n")
        .ok_or_else(|| "Controller returned an invalid HTTP response.".to_owned())?;
    let headers = String::from_utf8_lossy(&response[..boundary]);
    let status = headers.lines().next().and_then(|line| line.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or_else(|| "Controller returned an invalid HTTP status.".to_owned())?;
    let body = &response[boundary + 4..];
    let value = if body.is_empty() { Value::Null } else {
        serde_json::from_slice(body).map_err(|_| "Controller returned invalid status data.".to_owned())?
    };
    Ok((status, value))
}

fn identity_matches(descriptor: &Descriptor, response: &Value) -> bool {
    response.get("protocol").and_then(Value::as_u64) == Some(1)
        && response.get("service").and_then(Value::as_str) == Some(SERVICE_NAME)
        && response.get("version").and_then(Value::as_str) == Some(SERVICE_VERSION)
        && response.get("pid").and_then(Value::as_u64) == Some(descriptor.pid as u64)
        && response.get("parent_pid").and_then(Value::as_u64) == Some(descriptor.parent_pid as u64)
        && response.get("port").and_then(Value::as_u64) == Some(descriptor.port as u64)
        && response.get("instance_id").and_then(Value::as_str) == Some(&descriptor.instance_id)
}

fn status_template(state: &str, reason: &str) -> ServiceStatus {
    ServiceStatus {
        state: state.to_owned(), safety: "unknown".to_owned(), busy_count: 0,
        pending_count: 0, stop_pending: false, reason: reason.to_owned(),
        can_start: false, can_stop: false, can_restart: false, can_open: false,
        can_cancel_stop: false, can_recover: false, can_preserve: false,
        can_clear_preserved: false, can_quit_preserved: false,
    }
}

fn startup_recovery_available<T>(state: &ChildState<T>) -> bool {
    state.running && !state.starting && state.pid.is_some()
        && state.child.is_some() && state.startup_error.is_some()
}

fn preserved_child_for<T>(state: &ChildState<T>, project: &Path) -> Result<PreservedChild, String> {
    if !startup_recovery_available(state) {
        return Err("Only this controller's retained, unverified startup child can be preserved.".to_owned());
    }
    Ok(PreservedChild {
        project_path: project.to_path_buf(),
        pid: state.pid.expect("startup recovery requires a child PID"),
    })
}

fn preserved_child_status(
    preserved: &PreservedChild,
    can_clear: bool,
    can_preserve: bool,
    can_quit: bool,
) -> ServiceStatus {
    let reason = if can_preserve {
        format!("Office child PID {} remains untouched. Save the preservation lock before quitting the controller.", preserved.pid)
    } else {
        format!("Unverified Office child PID {} was left untouched. Verify it has stopped before clearing the safety lock.", preserved.pid)
    };
    let mut status = status_template("preserved", &reason);
    status.can_clear_preserved = can_clear;
    status.can_preserve = can_preserve;
    status.can_quit_preserved = can_quit;
    status
}

fn unverified_owned_child(reason: &str, can_recover: bool) -> ServiceStatus {
    let mut status = status_template("unverified", reason);
    status.can_recover = can_recover;
    status.can_preserve = can_recover;
    status
}

fn status_for(controller: &OfficeController, settings: &Settings) -> (ServiceStatus, Vec<String>) {
    let (child_running, child_starting, child_pid, child_present, can_recover, startup_error) = {
        let child = match controller.child_state() {
            Ok(child) => child,
            Err(reason) => return (status_template("unverified", &reason), Vec::new()),
        };
        (child.running, child.starting, child.pid, child.child.is_some(),
            startup_recovery_available(&child), child.startup_error.clone())
    };
    if let Some(preserved) = settings.preserved_child.as_ref() {
        let same_live_child = child_running && child_present && child_pid == Some(preserved.pid);
        let no_tracked_child = !child_running && !child_starting && !child_present;
        return (preserved_child_status(preserved,
            no_tracked_child, same_live_child && can_recover, no_tracked_child), Vec::new());
    }
    let Some(project) = settings.project_path.as_deref() else {
        return (status_template("notConfigured", "Choose the Office project folder."), Vec::new());
    };
    let descriptor_file = descriptor_path(project);
    if !descriptor_file.exists() {
        let mut status = if child_starting {
            status_template("starting", "Waiting for authenticated service identity.")
        } else if child_running {
            unverified_owned_child(startup_error.as_deref()
                .unwrap_or("The owned service has not confirmed its startup identity."), can_recover)
        } else {
            status_template("notStarted", "No managed Office service has been started for this project.")
        };
        status.can_start = !child_running && !child_starting && settings.codex_path.is_some();
        return (status, Vec::new());
    }
    if !child_running && !child_starting {
        // The previous controller's service is gone (forced stop, upgrade): archive its descriptor instead of staying Unverified.
        let archived = checked_existing_runtime(project).ok().and_then(|runtime| {
            liveness::archive_stale_descriptor(&runtime, std::time::SystemTime::now(),
                liveness::process_state, liveness::port_state).ok().flatten()
        });
        if archived.is_some() {
            let mut status = status_template("notStarted",
                "The previous Office service is no longer running; its stale descriptor was archived.");
            status.can_start = settings.codex_path.is_some();
            return (status, Vec::new());
        }
    }
    let descriptor = match read_descriptor_at_port(project, controller.expected_port()) {
        Ok(descriptor) => descriptor,
        Err(reason) => return (unverified_owned_child(&reason, can_recover), Vec::new()),
    };
    if descriptor.state == "stopped" && !child_running {
        let mut status = status_template("stopped", "Office recorded a clean shutdown.");
        status.safety = "safe".to_owned();
        status.can_start = settings.codex_path.is_some();
        return (status, read_logs(&descriptor));
    }
    if descriptor.state == "unhealthy" {
        return (unverified_owned_child("Office reported incomplete cleanup; no process was stopped.", can_recover), read_logs(&descriptor));
    }
    if !owner_matches(settings, project, &descriptor) {
        return (unverified_owned_child("The descriptor is not owned by this controller; the listener was not contacted.", can_recover), Vec::new());
    }
    let (health_code, health) = match http_request(&descriptor, "GET", "/desktop/health") {
        Ok(value) => value,
        Err(reason) => return (unverified_owned_child(&reason, can_recover), read_logs(&descriptor)),
    };
    if health_code != 200 || !identity_matches(&descriptor, &health) {
        return (unverified_owned_child("Authenticated health identity did not match; control is disabled.", can_recover), Vec::new());
    }
    let (status_code, details) = match http_request(&descriptor, "GET", "/desktop/status") {
        Ok(value) => value,
        Err(reason) => return (unverified_owned_child(&reason, can_recover), read_logs(&descriptor)),
    };
    if status_code != 200 || !identity_matches(&descriptor, &details) {
        return (unverified_owned_child("Authenticated status identity did not match; control is disabled.", can_recover), Vec::new());
    }
    (verified_status(&health, &details, settings.codex_path.is_some()), read_logs(&descriptor))
}

fn verified_status(health: &Value, details: &Value, configured: bool) -> ServiceStatus {
    let safety = details.get("safety").and_then(Value::as_str).unwrap_or("unknown");
    let stop_pending = details.get("stop_pending").and_then(Value::as_bool).unwrap_or(false);
    let state = if stop_pending || health.get("state").and_then(Value::as_str) == Some("stopping") {
        "stopping"
    } else { "running" };
    let mut status = ServiceStatus {
        state: state.to_owned(), safety: safety.to_owned(),
        busy_count: details.get("busy_count").and_then(Value::as_u64).unwrap_or(0),
        pending_count: details.get("pending_count").and_then(Value::as_u64).unwrap_or(0),
        stop_pending,
        reason: details.get("reason").and_then(Value::as_str).unwrap_or("").to_owned(),
        can_start: false,
        can_stop: !stop_pending && safety != "unknown",
        can_restart: !stop_pending && safety != "unknown" && configured,
        can_open: state == "running",
        can_cancel_stop: stop_pending,
        can_recover: false,
        can_preserve: false,
        can_clear_preserved: false,
        can_quit_preserved: false,
    };
    if safety == "unknown" && status.reason.is_empty() {
        status.reason = "Office is responding, but owned work is not fully verified.".to_owned();
    }
    status
}

fn read_logs(descriptor: &Descriptor) -> Vec<String> {
    match http_request(descriptor, "GET", "/desktop/logs") {
        Ok((200, value)) => value.get("lines").and_then(Value::as_array).map(|lines| {
            lines.iter().filter_map(Value::as_str).take(200).map(str::to_owned).collect()
        }).unwrap_or_default(),
        _ => Vec::new(),
    }
}

fn make_view(controller: &OfficeController) -> Result<View, String> {
    let settings = controller.settings()?;
    let (status, logs) = status_for(controller, &settings);
    Ok(View {
        project_path: settings.project_path.map(|path| path.display().to_string()),
        codex_path: settings.codex_path.map(|path| path.display().to_string()),
        status,
        logs,
    })
}

fn persist_owner(controller: &OfficeController, owner: Option<Owner>) -> Result<(), String> {
    controller.settings.lock().map_err(display_error)?.owner = owner;
    controller.save_settings()
}

fn runtime_components(project: &Path) -> Result<(PathBuf, [PathBuf; 3]), String> {
    let selected_project = project.to_path_buf();
    let project = project.canonicalize().map_err(display_error)?;
    if project != selected_project {
        return Err("The selected project folder no longer resolves to its saved path.".to_owned());
    }
    if !project.is_dir() {
        return Err("The selected project folder is unavailable.".to_owned());
    }
    let components = [
        project.join(".bot-mode"),
        project.join(".bot-mode").join("team-room-v1"),
        project.join(".bot-mode").join("team-room-v1").join("desktop-controller"),
    ];
    Ok((project, components))
}

fn checked_existing_runtime(project: &Path) -> Result<PathBuf, String> {
    let (project, components) = runtime_components(project)?;
    for component in &components {
        inspect_runtime_component(&project, component)?;
    }
    let runtime = components.last().expect("runtime component list is nonempty")
        .canonicalize().map_err(display_error)?;
    if !runtime.starts_with(&project) {
        return Err("Office runtime escaped the selected project.".to_owned());
    }
    Ok(runtime)
}

fn runtime_for(project: &Path) -> Result<PathBuf, String> {
    let (project, components) = runtime_components(project)?;

    // Inspect the whole existing chain before making any directory. This prevents
    // create_dir_all from following a pre-existing junction outside the project.
    for component in &components {
        inspect_runtime_component(&project, component)?;
    }
    for component in &components {
        match fs::create_dir(component) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(display_error(error)),
        }
        inspect_runtime_component(&project, component)?;
    }
    checked_existing_runtime(&project)
}

fn inspect_runtime_component(project: &Path, component: &Path) -> Result<(), String> {
    let metadata = match fs::symlink_metadata(component) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(display_error(error)),
    };
    if is_link_or_reparse(&metadata) {
        return Err("Office runtime path contains a symbolic link or reparse point.".to_owned());
    }
    if !metadata.is_dir() {
        return Err("An Office runtime path component is not a directory.".to_owned());
    }
    let canonical = component.canonicalize().map_err(display_error)?;
    if !canonical.starts_with(project) {
        return Err("Office runtime escaped the selected project.".to_owned());
    }
    Ok(())
}

#[cfg(windows)]
fn is_link_or_reparse(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    metadata.file_type().is_symlink() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(not(windows))]
fn is_link_or_reparse(metadata: &fs::Metadata) -> bool {
    metadata.file_type().is_symlink()
}

fn find_codex() -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        candidates.push(PathBuf::from(local).join("Programs/Codex/resources/codex.exe"));
    }
    if let Some(program_files) = std::env::var_os("ProgramFiles") {
        let packages = PathBuf::from(program_files).join("WindowsApps");
        if let Ok(entries) = fs::read_dir(packages) {
            for entry in entries.flatten() {
                if entry.file_name().to_string_lossy().starts_with("OpenAI.Codex_") {
                    candidates.push(entry.path().join("app/resources/codex.exe"));
                }
            }
        }
    }
    candidates.into_iter().find(|path| path.is_file())
        .and_then(|path| path.canonicalize().ok())
}

fn validate_codex(path: &str) -> Result<PathBuf, String> {
    let path = PathBuf::from(path).canonicalize().map_err(|_| "Choose an installed Codex codex.exe.".to_owned())?;
    if !path.is_file() || !path.file_name().is_some_and(|name| name.eq_ignore_ascii_case("codex.exe")) {
        return Err("The selected file must be Codex Desktop's codex.exe.".to_owned());
    }
    Ok(path)
}

fn validate_project(path: &str) -> Result<PathBuf, String> {
    let path = PathBuf::from(path).canonicalize().map_err(display_error)?;
    if !path.is_dir() { return Err("Choose an existing project folder.".to_owned()); }
    Ok(path)
}

#[tauri::command]
async fn get_view(controller: State<'_, Arc<OfficeController>>) -> Result<View, String> {
    let controller = controller.inner().clone();
    tauri::async_runtime::spawn_blocking(move || make_view(&controller)).await.map_err(display_error)?
}

#[tauri::command]
fn configure_project(project_path: String, controller: State<'_, Arc<OfficeController>>) -> Result<View, String> {
    let project = validate_project(&project_path)?;
    let current = controller.settings()?;
    if current.project_path.as_ref() != Some(&project) {
        let child = controller.child_state()?;
        if child.running || child.starting || current.owner.is_some() || current.preserved_child.is_some() {
            return Err("Stop and verify the current Office instance before changing projects.".to_owned());
        }
        drop(child);
        let mut settings = controller.settings.lock().map_err(display_error)?;
        settings.project_path = Some(project);
        settings.owner = None;
    }
    controller.save_settings()?;
    make_view(controller.inner())
}

#[tauri::command]
fn configure_codex(codex_path: String, controller: State<'_, Arc<OfficeController>>) -> Result<View, String> {
    let codex = validate_codex(&codex_path)?;
    let mut settings = controller.settings.lock().map_err(display_error)?;
    let child = controller.child_state()?;
    if child.running || child.starting || settings.owner.is_some() || settings.preserved_child.is_some() {
        return Err("Stop and verify the current Office instance before changing Codex.".to_owned());
    }
    settings.codex_path = Some(codex);
    drop(child);
    drop(settings);
    controller.save_settings()?;
    make_view(controller.inner())
}

fn verify_owned_health(settings: &Settings, project: &Path, port: u16) -> Result<Descriptor, String> {
    let descriptor = read_descriptor_at_port(project, port)?;
    if !owner_matches(settings, project, &descriptor) {
        return Err("The service identity is not owned by this controller.".to_owned());
    }
    let (code, health) = http_request(&descriptor, "GET", "/desktop/health")?;
    if code != 200 || !identity_matches(&descriptor, &health) {
        return Err("Authenticated service health did not match; no stop was sent.".to_owned());
    }
    Ok(descriptor)
}

fn wait_for_child_exit(controller: &OfficeController, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        match controller.child_state() {
            Ok(child) if !child.running => return true,
            Err(_) => return false,
            _ => {}
        }
        if Instant::now() >= deadline { return false; }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn stop_inner(controller: &OfficeController) -> Result<ServiceStatus, String> {
    let settings = controller.settings()?;
    let project = settings.project_path.as_deref().ok_or("Choose an Office project first.")?;
    let (status, _) = status_for(controller, &settings);
    if status.state == "stopped" || status.state == "notStarted" { return Ok(status); }
    if status.stop_pending { return Ok(status); }
    if !status.can_stop { return Err("Office ownership or shutdown safety is unverified; stop was refused.".to_owned()); }
    let descriptor = verify_owned_health(&settings, project, controller.expected_port())?;
    let (code, response) = http_request(&descriptor, "POST", "/desktop/shutdown")?;
    if code == 202 {
        let (latest, _) = status_for(controller, &settings);
        return Ok(latest);
    }
    if code != 200 || response.get("state").and_then(Value::as_str) != Some("stopped")
        || response.get("safety").and_then(Value::as_str) != Some("safe")
        || response.get("stop_pending").and_then(Value::as_bool) != Some(false)
    {
        return Err(response.get("reason").and_then(Value::as_str)
            .unwrap_or("Office did not confirm a clean safe stop.").to_owned());
    }
    if !wait_for_child_exit(controller, Duration::from_secs(10)) {
        return Err("Office confirmed cleanup; waiting for its owned process to exit.".to_owned());
    }
    persist_owner(controller, None)?;
    let mut child = controller.child_state()?;
    child.safe_to_exit = true;
    Ok(status_template("stopped", "Office confirmed a clean shutdown."))
}

#[tauri::command]
async fn stop_service(controller: State<'_, Arc<OfficeController>>) -> Result<ServiceStatus, String> {
    let controller = controller.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _action = controller.action.lock().map_err(display_error)?;
        stop_inner(&controller)
    }).await.map_err(display_error)?
}

#[tauri::command]
async fn cancel_stop(controller: State<'_, Arc<OfficeController>>) -> Result<View, String> {
    let controller = controller.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _action = controller.action.lock().map_err(display_error)?;
        let settings = controller.settings()?;
        let project = settings.project_path.as_deref().ok_or("Choose an Office project first.")?;
        let descriptor = verify_owned_health(&settings, project, controller.expected_port())?;
        let (code, response) = http_request(&descriptor, "POST", "/desktop/cancel-stop")?;
        if code != 200 { return Err(response.get("reason").and_then(Value::as_str)
            .unwrap_or("The pending stop could not be cancelled safely.").to_owned()); }
        make_view(&controller)
    }).await.map_err(display_error)?
}

fn record_startup_failure_state<T>(state: &mut ChildState<T>, reason: String) {
    state.starting = false;
    if state.running && state.child.is_some() {
        state.startup_error = Some(reason);
        state.safe_to_exit = false;
        return;
    }
    state.child = None;
    state.pid = None;
    state.running = false;
    state.startup_error = None;
    state.safe_to_exit = true;
}

fn record_startup_failure(controller: &OfficeController, reason: String) {
    if let Ok(mut child) = controller.child_state() {
        record_startup_failure_state(&mut child, reason);
    }
}

fn verify_startup_probes(descriptor: &Descriptor) -> Result<(), String> {
    let (health_code, health) = http_request(descriptor, "GET", "/desktop/health")
        .map_err(|error| format!("Authenticated startup health is unavailable: {error}"))?;
    if health_code != 200 || !identity_matches(descriptor, &health)
        || health.get("state").and_then(Value::as_str) != Some("running")
    {
        return Err("Authenticated startup health did not confirm this running child.".to_owned());
    }
    let (status_code, status) = http_request(descriptor, "GET", "/desktop/status")
        .map_err(|error| format!("Authenticated startup status is unavailable: {error}"))?;
    if status_code != 200 || !identity_matches(descriptor, &status) {
        return Err("Authenticated startup status did not match the child identity.".to_owned());
    }
    Ok(())
}

fn startup_matches(descriptor: &Descriptor, pid: u32, launch_token: &str) -> bool {
    !launch_token.is_empty() && descriptor.token == launch_token
        && (descriptor.pid == pid || descriptor.parent_pid == pid)
        && descriptor.state == "running"
}

fn wait_for_descriptor(
    project: &Path, pid: u32, launch_token: &str, port: u16, timeout: Duration,
    mut is_live: impl FnMut() -> bool,
) -> Result<Descriptor, String> {
    let deadline = Instant::now() + timeout;
    loop {
        if !is_live() {
            return Err("The owned service exited before startup health was confirmed.".to_owned());
        }
        let last_error = match read_descriptor_at_port(project, port) {
            Ok(descriptor) if startup_matches(&descriptor, pid, launch_token) => {
                match verify_startup_probes(&descriptor) {
                    Ok(()) if is_live() => return Ok(descriptor),
                    Ok(()) => return Err("The owned service exited during startup probes.".to_owned()),
                    Err(error) => error,
                }
            }
            Ok(_) => "The service descriptor does not match the bound launch identity.".to_owned(),
            Err(error) => error,
        };
        if Instant::now() >= deadline {
            return Err(format!("Startup is unverified; the owned process is retained for recheck. {last_error}"));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

fn wait_for_startup(controller: &OfficeController, project: &Path, pid: u32) -> Result<View, String> {
    let launch_token = controller.child_state()?.launch_token.clone();
    let is_live = || controller.child_state().is_ok_and(|child| {
        child.running && child.pid == Some(pid) && child.child.is_some()
    });
    let descriptor = wait_for_descriptor(project, pid, &launch_token, controller.expected_port(), Duration::from_secs(60), is_live)?;
    let owner = Owner { project_path: project.to_path_buf(), pid: descriptor.pid, instance_id: descriptor.instance_id.clone() };
    persist_owner(controller, Some(owner))?;
    if !is_live() {
        persist_owner(controller, None)?;
        return Err("The owned service exited before startup health was confirmed.".to_owned());
    }
    let view = make_view(controller)?;
    if view.status.state != "running" {
        return Err("The owned service did not remain verified and running after startup probes.".to_owned());
    }
    let mut child = controller.child_state()?;
    if !child.running || child.pid != Some(pid) || child.child.is_none() {
        drop(child);
        persist_owner(controller, None)?;
        return Err("The owned service exited before startup health was confirmed.".to_owned());
    }
    child.starting = false;
    child.startup_error = None;
    child.safe_to_exit = false;
    Ok(view)
}

fn new_launch_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    #[cfg(windows)]
    {
        #[link(name = "bcrypt")]
        extern "system" {
            fn BCryptGenRandom(algorithm: *mut std::ffi::c_void, buffer: *mut u8, size: u32, flags: u32) -> i32;
        }
        // System RNG, no provider handle. Never fall back to a predictable launch binding.
        if unsafe { BCryptGenRandom(std::ptr::null_mut(), bytes.as_mut_ptr(), 32, 2) } != 0 {
            return Err("A secure Office launch identity could not be created.".to_owned());
        }
    }
    #[cfg(not(windows))]
    fs::File::open("/dev/urandom").and_then(|mut file| file.read_exact(&mut bytes)).map_err(display_error)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn start_inner(app: &AppHandle, controller: &Arc<OfficeController>) -> Result<View, String> {
    let settings = controller.settings()?;
    let project = settings.project_path.as_deref().ok_or("Choose an Office project first.")?;
    let codex = settings.codex_path.as_deref().ok_or("Locate Codex Desktop's codex.exe first.")?;
    let (status, _) = status_for(controller, &settings);
    if !matches!(status.state.as_str(), "notStarted" | "stopped") || !status.can_start {
        return Err("Office is active or unverified; a second service was not started.".to_owned());
    }
    if !project.is_dir() || !codex.is_file() { return Err("The saved project or Codex path is unavailable.".to_owned()); }
    let runtime = runtime_for(project)?;
    let launch_token = new_launch_token()?;
    {
        let mut child = controller.child_state()?;
        if child.running || child.starting { return Err("The managed Office process is already starting or running.".to_owned()); }
        child.starting = true;
        child.safe_to_exit = false;
        child.startup_error = None;
        child.launch_token = launch_token.clone();
    }
    let mut args = vec![
        "--codex-path".to_owned(), codex.display().to_string(),
        "--runtime-root".to_owned(), runtime.display().to_string(),
        "--project".to_owned(), project.display().to_string(),
        "--port".to_owned(), PORT.to_string(),
        "--desktop-managed".to_owned(),
    ];
    if project.join(".bot-mode/desktop-relay/relay.json").is_file() {
        args.push("--desktop-relay".to_owned());
    }
    let sidecar = match app.shell().sidecar("office-service") {
        Ok(command) => command,
        Err(error) => {
            let message = display_error(error);
            record_startup_failure(controller, message.clone());
            return Err(message);
        }
    };
    let command = sidecar.args(args).current_dir(project).env("CODEX_OFFICE_LAUNCH_TOKEN", launch_token);
    let (events, child_handle) = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            let message = format!("Bundled Office service could not start: {error}");
            record_startup_failure(controller, message.clone());
            return Err(message);
        }
    };
    let pid = child_handle.pid();
    {
        let mut child = controller.child_state()?;
        child.pid = Some(pid);
        child.running = true;
        child.child = Some(child_handle);
        child.startup_error = None;
    }
    track_child_events(controller.clone(), events);
    match wait_for_startup(controller, project, pid) {
        Ok(view) => Ok(view),
        Err(error) => {
            record_startup_failure(controller, error.clone());
            Err(error)
        }
    }
}

fn track_child_events(event_controller: Arc<OfficeController>, mut events: tauri::async_runtime::Receiver<CommandEvent>) {
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            if matches!(event, CommandEvent::Terminated(_)) {
                let Ok(mut child) = event_controller.child_state() else { break; };
                if child.starting {
                    child.startup_error = Some("The owned service exited before startup health was confirmed.".to_owned());
                }
                child.running = false;
                child.starting = false;
                child.pid = None;
                child.child = None;
                child.launch_token.clear();
                break;
            }
        }
    });
}

fn recover_startup_inner(controller: &OfficeController) -> Result<View, String> {
    let project = controller.settings()?.project_path.ok_or("Choose an Office project first.")?;
    let pid = {
        let mut child = controller.child_state()?;
        if !startup_recovery_available(&child) {
            return Err("There is no idle, controller-owned startup process to recheck.".to_owned());
        }
        child.starting = true;
        child.startup_error = None;
        child.safe_to_exit = false;
        child.pid.expect("checked above")
    };
    match wait_for_startup(controller, &project, pid) {
        Ok(view) => Ok(view),
        Err(error) => {
            record_startup_failure(controller, error.clone());
            Err(error)
        }
    }
}

fn preserve_startup_child_inner(controller: &OfficeController, confirmed: bool) -> Result<(), String> {
    if !confirmed {
        return Err("Preserving the unverified child requires explicit confirmation.".to_owned());
    }
    let settings = controller.settings()?;
    let project = settings.project_path.as_deref().ok_or("Choose an Office project first.")?;
    let marker = {
        let child = controller.child_state()?;
        preserved_child_for(&child, project)?
    };
    {
        let mut settings = controller.settings.lock().map_err(display_error)?;
        if settings.project_path.as_ref() != Some(&marker.project_path) {
            return Err("The configured project changed; no child was preserved.".to_owned());
        }
        settings.owner = None;
        settings.preserved_child = Some(marker.clone());
    }
    controller.save_settings()?;
    {
        let mut child = controller.child_state()?;
        if child.running && child.pid == Some(marker.pid) && child.child.is_some() {
            if !startup_recovery_available(&child) {
                return Err("Startup state changed while saving the preservation lock; controller exit remains blocked.".to_owned());
            }
        } else if child.running || child.starting {
            return Err("The retained child identity changed; controller exit remains blocked.".to_owned());
        }
        // The persisted lock prevents a second start after the controller exits.
        // The exact child handle is dropped with the controller; no kill is sent.
        child.safe_to_exit = true;
    }
    Ok(())
}

fn clear_preserved_child_inner(controller: &OfficeController, confirmed_stopped: bool) -> Result<View, String> {
    if !confirmed_stopped {
        return Err("Verify the preserved child has stopped before clearing its safety lock.".to_owned());
    }
    let mut settings = controller.settings.lock().map_err(display_error)?;
    if settings.preserved_child.is_none() {
        return Err("There is no preserved-child safety lock to clear.".to_owned());
    }
    let child = controller.child_state()?;
    if child.running || child.starting || child.child.is_some() {
        return Err("A controller-owned child is still tracked; the safety lock was kept.".to_owned());
    }
    let previous_marker = settings.preserved_child.clone();
    settings.preserved_child = None;
    settings.owner = None;
    drop(child);
    drop(settings);
    if let Err(error) = controller.save_settings() {
        controller.settings.lock().map_err(display_error)?.preserved_child = previous_marker;
        return Err(error);
    }
    make_view(controller)
}

fn prepare_preserved_controller_exit<T>(
    child: &mut ChildState<T>,
    has_preserved_child: bool,
    confirmed: bool,
) -> Result<(), String> {
    if !confirmed {
        return Err("Quitting with a preserved child requires explicit confirmation.".to_owned());
    }
    if !has_preserved_child {
        return Err("There is no preserved-child safety lock to keep.".to_owned());
    }
    if child.running || child.starting || child.child.is_some() {
        return Err("A controller-owned child is still tracked; preserve it before exiting.".to_owned());
    }
    child.safe_to_exit = true;
    Ok(())
}

fn quit_preserved_controller_inner(controller: &OfficeController, confirmed: bool) -> Result<(), String> {
    let settings = controller.settings()?;
    let mut child = controller.child_state()?;
    prepare_preserved_controller_exit(&mut child, settings.preserved_child.is_some(), confirmed)
}

#[tauri::command]
async fn start_service(app: AppHandle, controller: State<'_, Arc<OfficeController>>) -> Result<View, String> {
    let controller = controller.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _action = controller.action.lock().map_err(display_error)?;
        start_inner(&app, &controller)
    }).await.map_err(display_error)?
}

#[tauri::command]
async fn recover_startup(controller: State<'_, Arc<OfficeController>>) -> Result<View, String> {
    let controller = controller.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _action = controller.action.lock().map_err(display_error)?;
        recover_startup_inner(&controller)
    }).await.map_err(display_error)?
}

#[tauri::command]
async fn preserve_startup_child(
    app: AppHandle,
    controller: State<'_, Arc<OfficeController>>,
    confirmed: bool,
) -> Result<(), String> {
    let controller = controller.inner().clone();
    let app_exit = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _action = controller.action.lock().map_err(display_error)?;
        preserve_startup_child_inner(&controller, confirmed)?;
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(250));
            app_exit.exit(0);
        });
        Ok(())
    }).await.map_err(display_error)?
}

#[tauri::command]
async fn clear_preserved_child(
    controller: State<'_, Arc<OfficeController>>,
    confirmed_stopped: bool,
) -> Result<View, String> {
    let controller = controller.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _action = controller.action.lock().map_err(display_error)?;
        clear_preserved_child_inner(&controller, confirmed_stopped)
    }).await.map_err(display_error)?
}

#[tauri::command]
async fn quit_preserved_controller(
    app: AppHandle,
    controller: State<'_, Arc<OfficeController>>,
    confirmed: bool,
) -> Result<(), String> {
    let controller = controller.inner().clone();
    let app_exit = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _action = controller.action.lock().map_err(display_error)?;
        quit_preserved_controller_inner(&controller, confirmed)?;
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(250));
            app_exit.exit(0);
        });
        Ok(())
    }).await.map_err(display_error)?
}

#[tauri::command]
async fn restart_service(app: AppHandle, controller: State<'_, Arc<OfficeController>>) -> Result<View, String> {
    let controller = controller.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _action = controller.action.lock().map_err(display_error)?;
        let stopped = stop_inner(&controller)?;
        if stopped.state != "stopped" { return make_view(&controller); }
        start_inner(&app, &controller)
    }).await.map_err(display_error)?
}

fn is_verified_running(controller: &OfficeController) -> Result<bool, String> {
    let settings = controller.settings()?;
    let (status, _) = status_for(controller, &settings);
    Ok(status.can_open)
}

#[tauri::command]
fn open_chrome(app: AppHandle, controller: State<'_, Arc<OfficeController>>) -> Result<bool, String> {
    if !is_verified_running(controller.inner())? { return Err("Office is not verified as running.".to_owned()); }
    Ok(app.opener().open_url(OFFICE_URL, Some("chrome")).is_ok())
}

#[tauri::command]
fn open_default_browser(app: AppHandle, controller: State<'_, Arc<OfficeController>>) -> Result<(), String> {
    if !is_verified_running(controller.inner())? { return Err("Office is not verified as running.".to_owned()); }
    app.opener().open_url(OFFICE_URL, None::<&str>).map_err(display_error)
}

/// Idle states from which the controller may quit without stopping anything.
fn is_quit_ready(state: &str) -> bool {
    matches!(state, "notConfigured" | "notStarted" | "stopped")
}

/// Stops an owned service if there is one and marks the controller safe to exit. Fails while Office is busy or draining.
fn prepare_quit(controller: &OfficeController) -> Result<(), String> {
    let _action = controller.action.lock().map_err(display_error)?;
    let settings = controller.settings()?;
    let (status, _) = status_for(controller, &settings);
    let final_status = if is_quit_ready(&status.state) { status } else { stop_inner(controller)? };
    if !is_quit_ready(&final_status.state) {
        return Err("Office is busy or still draining; the controller remains in the tray.".to_owned());
    }
    let mut child = controller.child_state()?;
    child.safe_to_exit = true;
    Ok(())
}

#[tauri::command]
async fn quit_application(app: AppHandle, controller: State<'_, Arc<OfficeController>>) -> Result<(), String> {
    let controller = controller.inner().clone();
    let app_exit = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        prepare_quit(&controller)?;
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(250));
            app_exit.exit(0);
        });
        Ok(())
    }).await.map_err(display_error)?
}

fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_single_instance::init(|app, _, _| show_main(app)))
        .setup(|app| {
            let controller = Arc::new(OfficeController::load(app.handle())
                .map_err(std::io::Error::other)?);
            app.manage(controller);

            let show = MenuItem::with_id(app, "show", "Show Codex Office", true, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "Open Office in Chrome", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Office…", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &open, &quit])?;
            let icon = app.default_window_icon().cloned().ok_or_else(||
                std::io::Error::other("Tauri default window icon is missing"))?;
            TrayIconBuilder::new()
                .icon(icon)
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => show_main(app),
                    "open" => { let _ = app.emit("office-open-requested", ()); }
                    "quit" => { let _ = app.emit("office-quit-requested", ()); }
                    _ => {}
                })
                .build(app)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_view, configure_project, configure_codex, start_service, stop_service,
            restart_service, recover_startup, preserve_startup_child, clear_preserved_child,
            quit_preserved_controller, cancel_stop, open_chrome, open_default_browser, quit_application
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("Codex Office desktop failed to initialize");

    app.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            let safe = app.try_state::<Arc<OfficeController>>()
                .and_then(|controller| controller.child_state().ok().map(|child| child.safe_to_exit))
                .unwrap_or(false);
            if !safe {
                api.prevent_exit();
                show_main(app);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        path::{Path, PathBuf},
        sync::atomic::{AtomicU64, Ordering},
    };

    struct TestDir(PathBuf);

    impl TestDir {
        fn new(label: &str) -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "codex-office-{label}-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).expect("create test directory");
            Self(path)
        }
    }

    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[cfg(windows)]
    fn create_dir_link(link: &Path, target: &Path) -> std::io::Result<()> {
        let output = std::process::Command::new("cmd.exe")
            .arg("/C").arg("mklink").arg("/J").arg(link).arg(target).output()?;
        if output.status.success() {
            Ok(())
        } else {
            Err(std::io::Error::other(String::from_utf8_lossy(&output.stderr).into_owned()))
        }
    }

    #[cfg(unix)]
    fn create_dir_link(link: &Path, target: &Path) -> std::io::Result<()> {
        std::os::unix::fs::symlink(target, link)
    }

    #[test]
    fn frozen_worker_requires_bound_launch_and_exact_parent() {
        let descriptor: Descriptor = serde_json::from_value(serde_json::json!({
            "protocol": 1, "service": SERVICE_NAME, "version": SERVICE_VERSION,
            "pid": 5678, "parent_pid": 4321, "port": PORT,
            "instance_id": "fixture", "token": "launch-secret", "state": "running"
        })).unwrap();
        assert!(startup_matches(&descriptor, 4321, "launch-secret"));
        assert!(!startup_matches(&descriptor, 9999, "launch-secret"));
        assert!(!startup_matches(&descriptor, 4321, "foreign-secret"));
        assert!(!startup_matches(&descriptor, 4321, ""));
    }

    #[test]
    fn direct_binary_and_authenticated_identity_match_exactly() {
        let descriptor: Descriptor = serde_json::from_value(serde_json::json!({
            "protocol": 1, "service": SERVICE_NAME, "version": SERVICE_VERSION,
            "pid": 4321, "parent_pid": 1234, "port": PORT,
            "instance_id": "fixture", "token": "launch-secret", "state": "running"
        })).unwrap();
        assert!(startup_matches(&descriptor, 4321, "launch-secret"));
        assert!(!startup_matches(&descriptor, 4321, "foreign-secret"));
        let mut health = serde_json::json!({
            "protocol": 1, "service": SERVICE_NAME, "version": SERVICE_VERSION,
            "pid": 4321, "parent_pid": 1234, "port": PORT, "instance_id": "fixture"
        });
        assert!(identity_matches(&descriptor, &health));
        health["parent_pid"] = serde_json::json!(9999);
        assert!(!identity_matches(&descriptor, &health));
        health["parent_pid"] = serde_json::json!(1234);
        health["instance_id"] = serde_json::json!("foreign-instance");
        assert!(!identity_matches(&descriptor, &health));
        let first = new_launch_token().unwrap();
        assert_eq!(first.len(), 64);
        assert_ne!(first, new_launch_token().unwrap());
    }

    #[test]
    fn slow_unknown_status_passes_production_probes_but_cannot_stop() {
        use std::net::TcpListener;
        let root = TestDir::new("slow-status");
        let project = root.0.canonicalize().unwrap();
        let runtime = runtime_for(&project).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let data = serde_json::json!({
            "protocol": 1, "service": SERVICE_NAME, "version": SERVICE_VERSION,
            "pid": 5678, "parent_pid": 4321, "port": port,
            "instance_id": "fixture", "token": "owned", "state": "running"
        });
        fs::write(runtime.join("desktop.json"), data.to_string()).unwrap();
        let server = std::thread::spawn(move || {
            for index in 0..2 {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = Vec::new();
                while !request.ends_with(b"\r\n\r\n") {
                    let mut byte = [0];
                    stream.read_exact(&mut byte).unwrap();
                    request.push(byte[0]);
                }
                if index == 1 { std::thread::sleep(Duration::from_millis(4250)); }
                let mut response = data.clone();
                response.as_object_mut().unwrap().remove("token");
                response["safety"] = serde_json::json!("unknown");
                let body = response.to_string();
                write!(stream, "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body).unwrap();
            }
        });
        let started = Instant::now();
        let descriptor = wait_for_descriptor(&project, 4321, "owned", port, Duration::from_secs(15), || true).unwrap();
        assert!(started.elapsed() >= Duration::from_secs(4));
        assert_eq!(descriptor.pid, 5678);
        let status = verified_status(&serde_json::json!({"state":"running"}),
            &serde_json::json!({"safety":"unknown"}), true);
        assert_eq!(status.state, "running");
        assert!(status.can_open);
        assert!(!status.can_stop && !status.can_restart && !status.can_start);
        server.join().unwrap();
    }

    #[test]
    fn foreign_launch_is_refused_before_http_and_timeout_remains_recoverable() {
        use std::net::TcpListener;
        let root = TestDir::new("foreign-launch");
        let project = root.0.canonicalize().unwrap();
        let runtime = runtime_for(&project).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let port = listener.local_addr().unwrap().port();
        for (parent, token) in [(4321, "foreign"), (9999, "owned"), (4321, "")] {
            let data = serde_json::json!({
                "protocol": 1, "service": SERVICE_NAME, "version": SERVICE_VERSION,
                "pid": 5678, "parent_pid": parent, "port": port,
                "instance_id": "fixture", "token": token, "state": "running"
            });
            fs::write(runtime.join("desktop.json"), data.to_string()).unwrap();
            let error = wait_for_descriptor(&project, 4321, "owned", port, Duration::ZERO, || true)
                .err().expect("foreign launch must time out");
            assert!(error.contains("retained for recheck"));
            assert_eq!(listener.accept().unwrap_err().kind(), std::io::ErrorKind::WouldBlock);
            let mut state = fake_startup_child();
            record_startup_failure_state(&mut state, error);
            assert!(startup_recovery_available(&state));
            assert_eq!(preserved_child_for(&state, &root.0).unwrap().pid, 4321);
        }
        assert!(wait_for_descriptor(&project, 4321, "owned", port, Duration::ZERO, || false)
            .err().unwrap().contains("exited"));
    }

    // Run explicitly with synthetic fixture env paths; never uses the owner port or Codex.
    #[test]
    fn status_over_budget_fails_closed_and_retains_child() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let descriptor: Descriptor = serde_json::from_value(serde_json::json!({
            "protocol": 1, "service": SERVICE_NAME, "version": SERVICE_VERSION,
            "pid": 5678, "parent_pid": 4321, "port": port,
            "instance_id": "fixture", "token": "owned", "state": "running"
        })).unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            while !request.ends_with(b"\r\n\r\n") {
                let mut byte = [0];
                stream.read_exact(&mut byte).unwrap();
                request.push(byte[0]);
            }
            assert!(String::from_utf8(request).unwrap().starts_with("GET /desktop/status "));
            std::thread::sleep(Duration::from_millis(10500));
        });
        let started = Instant::now();
        let error = http_request(&descriptor, "GET", "/desktop/status").err().expect("bounded timeout");
        assert!(started.elapsed() < Duration::from_secs(13));
        let mut child = fake_startup_child();
        record_startup_failure_state(&mut child, error);
        assert!(startup_recovery_available(&child));
        assert_eq!(child.pid, Some(4321));
        assert!(!child.safe_to_exit);
        let controls = unverified_owned_child("timeout", true);
        assert!(!controls.can_start && !controls.can_stop && !controls.can_open);
        server.join().unwrap();
    }

    // Explicit synthetic integration; not a GUI or installed-owner acceptance test.
    #[test]
    #[ignore = "requires packaged service and isolated synthetic project"]
    fn packaged_service_passes_production_rust_startup_and_shutdown() {
        use std::process::{Command, Stdio};
        let project = PathBuf::from(std::env::var_os("OFFICE_TEST_PROJECT").expect("synthetic project")).canonicalize().unwrap();
        let fixture = PathBuf::from(std::env::var_os("OFFICE_TEST_CODEX").expect("synthetic Codex fixture"));
        assert_eq!(fixture.file_name().unwrap(), "fixture_codex.exe");
        let sidecar = PathBuf::from(std::env::var_os("OFFICE_TEST_SIDECAR").expect("packaged sidecar"));
        assert!(project.join(".synthetic-office-test").is_file());
        assert!(!project.join(".bot-mode/desktop-relay/relay.json").exists());
        let runtime = runtime_for(&project).unwrap();
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        assert_ne!(port, PORT);
        let app = tauri::test::mock_builder().plugin(tauri_plugin_shell::init())
            .build(tauri::test::mock_context(tauri::test::noop_assets())).unwrap();
        let controller = Arc::new(OfficeController {
            settings_file: project.join("controller-test.json"),
            settings: Mutex::new(Settings { project_path: Some(project.clone()),
                codex_path: Some(fixture.clone()), ..Settings::default() }),
            child: Mutex::new(ChildState::default()), action: Mutex::new(()), test_port: port,
        });
        controller.save_settings().unwrap();
        assert!(make_view(&controller).unwrap().status.can_start);
        for attempt in 0..2 {
            let token = new_launch_token().unwrap();
            let (events, handle) = app.shell().command(&sidecar)
                .args(["--project", project.to_str().unwrap(), "--runtime-root", runtime.to_str().unwrap(),
                    "--codex-path", fixture.to_str().unwrap(), "--port", &port.to_string(), "--desktop-managed"])
                .env("CODEX_OFFICE_LAUNCH_TOKEN", &token).spawn().unwrap();
            let pid = handle.pid();
            *controller.child_state().unwrap() = ChildState {
                child: Some(handle), pid: Some(pid), launch_token: token.clone(),
                running: true, starting: true, safe_to_exit: false, startup_error: None,
            };
            track_child_events(controller.clone(), events);
            let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let view = if attempt == 0 {
                    wait_for_startup(&controller, &project, pid).unwrap()
                } else {
                    record_startup_failure(&controller, "synthetic interrupted startup".to_owned());
                    assert!(startup_recovery_available(&controller.child_state().unwrap()));
                    recover_startup_inner(&controller).unwrap()
                };
                assert_eq!(view.status.state, "running");
                assert!(view.status.can_open && view.status.can_stop && view.status.can_restart);
                assert!(!view.status.can_start);
                let descriptor = read_descriptor_at_port(&project, port).unwrap();
                assert_ne!(descriptor.pid, pid, "onefile worker must differ from launcher");
                assert_eq!(descriptor.parent_pid, pid);
                let settings = controller.settings().unwrap();
                assert!(owner_matches(&settings, &project, &descriptor));
                assert_eq!(settings.owner.unwrap().pid, descriptor.pid);
                assert_eq!(controller.child_state().unwrap().pid, Some(pid));
                let stopped = stop_inner(&controller).unwrap();
                assert_eq!(stopped.state, "stopped");
                assert!(controller.settings().unwrap().owner.is_none());
                assert!(!controller.child_state().unwrap().running);
                assert!(controller.child_state().unwrap().safe_to_exit);
                assert!(make_view(&controller).unwrap().status.can_start);
                assert_eq!(read_descriptor_at_port(&project, port).unwrap().state, "stopped");
            }));
            if controller.child_state().unwrap().running {
                // Only this exact synthetic child tree if assertions failed.
                let _ = Command::new("taskkill").args(["/PID", &pid.to_string(), "/T", "/F"])
                    .stdout(Stdio::null()).stderr(Stdio::null()).status();
                assert!(wait_for_child_exit(&controller, Duration::from_secs(10)));
            }
            if let Err(error) = outcome { std::panic::resume_unwind(error); }
        }
    }

    fn controller_for(project: &Path, codex: PathBuf, port: u16) -> OfficeController {
        OfficeController {
            settings_file: project.join("controller-test.json"),
            settings: Mutex::new(Settings { project_path: Some(project.to_path_buf()),
                codex_path: Some(codex), ..Settings::default() }),
            child: Mutex::new(ChildState::default()), action: Mutex::new(()), test_port: port,
        }
    }

    fn bare_controller(root: &TestDir, settings: Settings, port: u16) -> OfficeController {
        OfficeController {
            settings_file: root.0.join("controller-test.json"), settings: Mutex::new(settings),
            child: Mutex::new(ChildState::default()), action: Mutex::new(()), test_port: port,
        }
    }

    fn write_service_descriptor(runtime: &Path, pid: u32, port: u16) {
        let data = serde_json::json!({
            "protocol": 1, "service": SERVICE_NAME, "version": SERVICE_VERSION,
            "pid": pid, "parent_pid": 1, "port": port,
            "instance_id": "fixture", "token": "t", "state": "running"
        });
        fs::write(runtime.join("desktop.json"), data.to_string()).unwrap();
        fs::write(runtime.join("running.json"), "{}").unwrap();
    }

    #[test]
    fn descriptor_of_a_dead_service_is_archived_and_start_is_enabled() {
        let root = TestDir::new("stale-descriptor");
        let project = root.0.canonicalize().unwrap();
        let runtime = runtime_for(&project).unwrap();
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let codex = project.join("codex.exe");
        fs::write(&codex, b"").unwrap();
        write_service_descriptor(&runtime, 4_000_000_000, port);
        let controller = controller_for(&project, codex, port);
        let status = make_view(&controller).unwrap().status;
        assert_eq!(status.state, "notStarted");
        assert!(status.can_start && !status.can_stop && !status.can_open);
        assert!(!runtime.join("desktop.json").exists() && !runtime.join("running.json").exists());
        let names: Vec<String> = fs::read_dir(&runtime).unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned()).collect();
        assert!(names.iter().any(|name| name.starts_with("desktop.stale-")), "{names:?}");
        assert!(names.iter().any(|name| name.starts_with("running.stale-")), "{names:?}");
    }

    #[test]
    fn live_unresponsive_listener_keeps_the_controller_unverified() {
        let root = TestDir::new("live-unresponsive");
        let project = root.0.canonicalize().unwrap();
        let runtime = runtime_for(&project).unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let codex = project.join("codex.exe");
        fs::write(&codex, b"").unwrap();
        write_service_descriptor(&runtime, 4_000_000_000, port);
        let controller = controller_for(&project, codex, port);
        let status = make_view(&controller).unwrap().status;
        assert_eq!(status.state, "unverified");
        assert!(!status.can_start);
        assert!(runtime.join("desktop.json").exists() && runtime.join("running.json").exists());
        drop(listener);
    }

    #[test]
    fn quit_with_no_project_configured_is_allowed_immediately() {
        let root = TestDir::new("quit-not-configured");
        let controller = bare_controller(&root, Settings::default(), PORT);
        assert_eq!(status_for(&controller, &controller.settings().unwrap()).0.state, "notConfigured");
        controller.child_state().unwrap().safe_to_exit = false;
        prepare_quit(&controller).expect("setup needed must not block quitting");
        assert!(controller.child_state().unwrap().safe_to_exit);
    }

    #[test]
    fn quit_stays_refused_while_the_service_is_unverified() {
        use std::net::TcpListener;
        let root = TestDir::new("quit-unverified");
        let project = root.0.canonicalize().unwrap();
        let runtime = runtime_for(&project).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let data = serde_json::json!({
            "protocol": 1, "service": SERVICE_NAME, "version": SERVICE_VERSION,
            "pid": 4_000_000_000u32, "parent_pid": 1, "port": port,
            "instance_id": "fixture", "token": "t", "state": "running"
        });
        fs::write(runtime.join("desktop.json"), data.to_string()).unwrap();
        let settings = Settings { project_path: Some(project), ..Settings::default() };
        let controller = bare_controller(&root, settings, port);
        controller.child_state().unwrap().safe_to_exit = false;
        assert_eq!(status_for(&controller, &controller.settings().unwrap()).0.state, "unverified");
        assert!(prepare_quit(&controller).is_err());
        assert!(!controller.child_state().unwrap().safe_to_exit);
        drop(listener);
    }

    fn fake_startup_child() -> ChildState<&'static str> {
        ChildState {
            child: Some("fake-owned-child"),
            pid: Some(4321),
            launch_token: "fixture-launch".to_owned(),
            running: true,
            starting: true,
            safe_to_exit: false,
            startup_error: None,
        }
    }

    #[test]
    fn existing_settings_without_preserved_child_still_load() {
        let settings: Settings = serde_json::from_str(
            r#"{"projectPath":null,"codexPath":null,"owner":null}"#,
        ).expect("load settings written before preservation marker existed");

        assert!(settings.preserved_child.is_none());
    }

    #[test]
    fn runtime_refuses_a_link_at_each_existing_component_before_creating_children() {
        let root = TestDir::new("runtime-links");
        let names = [".bot-mode", "team-room-v1", "desktop-controller"];
        for link_index in 0..names.len() {
            let project = root.0.join(format!("project-{link_index}"));
            let outside = root.0.join(format!("outside-{link_index}"));
            fs::create_dir_all(&project).expect("create project");
            fs::create_dir_all(&outside).expect("create link target");
            let sentinel = outside.join("sentinel.txt");
            fs::write(&sentinel, b"external runtime data").expect("write external sentinel");
            let project = project.canonicalize().expect("canonical project");
            let link = names[..=link_index].iter().fold(project.clone(), |path, name| path.join(name));
            if link_index > 0 {
                fs::create_dir_all(link.parent().expect("link parent")).expect("create safe ancestors");
            }
            create_dir_link(&link, &outside).expect("create directory reparse fixture");

            assert!(runtime_for(&project).is_err(), "accepted linked component {}", link.display());
            let external_child = names[link_index + 1..].iter().fold(outside.clone(), |path, name| path.join(name));
            if external_child != outside {
                assert!(!external_child.exists(), "created runtime outside project at {}", external_child.display());
            }
            assert_eq!(fs::read(&sentinel).expect("read external sentinel"), b"external runtime data".to_vec(),
                "external sentinel changed through component {}", link.display());
        }
    }

    #[test]
    fn descriptor_reads_refuse_a_junction_before_reading_outside_project() {
        let root = TestDir::new("descriptor-link");
        let project = root.0.join("project");
        let outside = root.0.join("outside");
        fs::create_dir_all(&project).expect("create project");
        fs::create_dir_all(&outside).expect("create external target");
        fs::write(outside.join("desktop.json"), "{}").expect("write external descriptor fixture");
        let project = project.canonicalize().expect("canonical project");
        create_dir_link(&project.join(".bot-mode"), &outside).expect("create directory reparse fixture");

        let error = read_descriptor(&project).expect_err("refuse external descriptor path");

        assert!(error.contains("symbolic link or reparse point"));
    }

    #[test]
    fn runtime_is_created_only_inside_the_canonical_project() {
        let root = TestDir::new("runtime-contained");
        let project = root.0.join("project");
        fs::create_dir_all(&project).expect("create project");
        let project = project.canonicalize().expect("canonical project");

        let runtime = runtime_for(&project).expect("create contained runtime");
        let expected = project.join(".bot-mode/team-room-v1/desktop-controller");

        assert_eq!(runtime, expected);
    }

    #[test]
    fn startup_failure_retains_the_same_owned_child_for_recheck() {
        let mut state = ChildState::<&str> {
            child: Some("fake-owned-child"),
            pid: Some(4321),
            launch_token: "fixture-launch".to_owned(),
            running: true,
            starting: true,
            safe_to_exit: false,
            startup_error: None,
        };

        record_startup_failure_state(&mut state, "health probe timed out".to_owned());

        assert_eq!(state.child, Some("fake-owned-child"));
        assert_eq!(state.pid, Some(4321));
        assert!(state.running);
        assert!(!state.starting);
        assert!(!state.safe_to_exit);
        assert_eq!(state.startup_error.as_deref(), Some("health probe timed out"));
        assert!(startup_recovery_available(&state));
    }

    #[test]
    fn persistent_missing_descriptor_can_be_preserved() {
        let root = TestDir::new("startup-missing-descriptor");
        let project = root.0.join("project");
        fs::create_dir_all(&project).expect("create project");
        let project = project.canonicalize().expect("canonical project");
        runtime_for(&project).expect("create runtime without descriptor");
        let mut state = fake_startup_child();

        for _ in 0..3 {
            let error = read_descriptor(&project).expect_err("descriptor remains missing");
            record_startup_failure_state(&mut state, error);
            assert!(startup_recovery_available(&state));
        }
        let preserved = preserved_child_for(&state, &project).expect("preserve after persistent missing descriptor");
        assert_eq!(preserved.pid, 4321);
        assert_eq!(state.child, Some("fake-owned-child"));
    }

    #[test]
    fn persistent_malformed_descriptor_can_be_preserved() {
        let root = TestDir::new("startup-malformed-descriptor");
        let project = root.0.join("project");
        fs::create_dir_all(&project).expect("create project");
        let project = project.canonicalize().expect("canonical project");
        let runtime = runtime_for(&project).expect("create runtime");
        fs::write(runtime.join("desktop.json"), b"{ malformed").expect("write malformed descriptor");
        let mut state = fake_startup_child();

        for _ in 0..3 {
            let error = read_descriptor(&project).expect_err("descriptor remains malformed");
            record_startup_failure_state(&mut state, error);
            assert!(startup_recovery_available(&state));
        }
        let preserved = preserved_child_for(&state, &project).expect("preserve after persistent malformed descriptor");
        assert_eq!(preserved.pid, 4321);
        assert_eq!(state.child, Some("fake-owned-child"));
    }

    #[test]
    fn persistent_http_failure_can_be_preserved() {
        use std::net::TcpListener;

        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind loopback failure fixture");
        let port = listener.local_addr().expect("fixture address").port();
        let fixture = std::thread::spawn(move || {
            for _ in 0..3 {
                let (stream, _) = listener.accept().expect("accept local fixture request");
                drop(stream); // Deliberately return no HTTP response.
            }
        });
        let descriptor = Descriptor {
            protocol: 1,
            service: SERVICE_NAME.to_owned(),
            version: SERVICE_VERSION.to_owned(),
            pid: 4321,
            parent_pid: 1234,
            port,
            instance_id: "fixture-instance".to_owned(),
            token: "fixture-token".to_owned(),
            state: "running".to_owned(),
        };
        let mut state = fake_startup_child();

        for _ in 0..3 {
            let error = http_request(&descriptor, "GET", "/desktop/health")
                .expect_err("fixture closes the authenticated health response");
            record_startup_failure_state(&mut state, error);
            assert!(startup_recovery_available(&state));
        }
        fixture.join().expect("local HTTP failure fixture exits");
        let preserved = preserved_child_for(&state, Path::new("E:/fixture/project"))
            .expect("preserve after persistent HTTP failure");
        assert_eq!(preserved.pid, 4321);
        assert_eq!(state.child, Some("fake-owned-child"));
    }

    fn preserved_settings(project: &Path) -> Settings {
        Settings {
            project_path: Some(project.to_path_buf()),
            preserved_child: Some(PreservedChild { project_path: project.to_path_buf(), pid: 4321 }),
            ..Settings::default()
        }
    }

    #[test]
    fn existing_preserved_marker_survives_settings_rewrite() {
        let root = TestDir::new("marker-rewrite");
        let path = root.0.join("controller.json");
        let mut settings = preserved_settings(&root.0);
        write_settings(&path, &settings).expect("initial marker");
        settings.codex_path = Some(root.0.join("codex.exe"));
        write_settings(&path, &settings).expect("rewrite existing marker");
        let saved = load_settings(&path).expect("load settled rewrite");
        assert_eq!(saved.preserved_child.expect("marker retained").pid, 4321);
        assert_eq!(saved.codex_path, settings.codex_path);
        assert!(!path.with_extension("json.tmp").exists());
        assert!(!path.with_extension("json.bak").exists());
    }

    #[test]
    fn interrupted_marker_rewrite_refuses_reload_at_every_stage() {
        for stage in 0..4 {
            let root = TestDir::new("interrupted-marker-rewrite");
            let path = root.0.join("controller.json");
            let temporary = path.with_extension("json.tmp");
            let backup = path.with_extension("json.bak");
            let settings = preserved_settings(&root.0);
            write_settings(&path, &settings).expect("existing marker");
            let original = fs::read(&path).expect("original marker bytes");
            // Snapshot the actual journal transitions, including partial staging
            // and a promoted marker-clear whose backup has not been removed.
            fs::write(&temporary, if stage == 0 { b"{".to_vec() } else {
                serde_json::to_vec(&Settings::default()).expect("serialize next settings")
            }).expect("stage interrupted write");
            if stage >= 2 {
                fs::rename(&path, &backup).expect("retain original as backup");
            }
            if stage == 3 {
                fs::rename(&temporary, &path).expect("promote next settings");
            }
            assert!(load_settings(&path).is_err(), "stage {stage} must not load defaults or a cleared marker");
            assert!(write_settings(&path, &Settings::default()).is_err(), "stage {stage} must not overwrite recovery evidence");
            let retained = if stage >= 2 { &backup } else { &path };
            assert_eq!(fs::read(retained).expect("old marker survives"), original);
        }
    }

    #[test]
    fn failed_marker_clear_retains_memory_and_disk_lock() {
        let root = TestDir::new("failed-marker-clear");
        let path = root.0.join("controller.json");
        let settings = preserved_settings(&root.0);
        write_settings(&path, &settings).expect("existing marker");
        let original = fs::read(&path).expect("original marker");
        let controller = OfficeController {
            settings_file: path.clone(),
            settings: Mutex::new(settings),
            child: Mutex::new(ChildState::default()),
            action: Mutex::new(()),
            #[cfg(test)]
            test_port: PORT,
        };
        fs::write(path.with_extension("json.tmp"), b"partial interrupted rewrite")
            .expect("force unresolved write failure");
        assert!(write_settings(&path, &Settings::default()).is_err(), "existing marker rewrite must refuse unresolved staging");
        assert!(clear_preserved_child_inner(&controller, true).is_err());
        assert_eq!(controller.settings.lock().expect("in-memory settings").preserved_child
            .as_ref().expect("in-memory lock restored").pid, 4321);
        assert_eq!(fs::read(&path).expect("disk marker retained"), original);
        assert!(controller.settings().is_err(), "live controls fail closed too");
        assert!(load_settings(&path).is_err(), "relaunch cannot silently discard the lock");
    }

    #[test]
    fn explicit_preserve_persists_lock_and_does_not_terminate_owned_child() {
        let root = TestDir::new("preserve-owned-child");
        let project = root.0.join("project");
        fs::create_dir_all(&project).expect("create project");
        let project = project.canonicalize().expect("canonical project");
        let settings_file = root.0.join("controller.json");
        let mut child = fake_startup_child();
        record_startup_failure_state(&mut child, "persistent HTTP failure".to_owned());
        // Fake handles exercise state helpers only; the production controller
        // below retains its concrete Tauri CommandChild type.
        let marker = preserved_child_for(&child, &project).expect("exact startup child");
        let settings = Settings {
            project_path: Some(project.clone()),
            preserved_child: Some(marker),
            ..Settings::default()
        };
        write_settings(&settings_file, &settings).expect("persist safety marker");
        let saved = load_settings(&settings_file).expect("reload safety marker");
        assert_eq!(saved.preserved_child.as_ref().expect("persisted child").pid, 4321);
        assert_eq!(child.child, Some("fake-owned-child"));
        assert!(child.running);
        assert!(!child.safe_to_exit);

        let resumed_controller = OfficeController {
            settings_file,
            settings: Mutex::new(saved),
            child: Mutex::new(ChildState::default()),
            action: Mutex::new(()),
            #[cfg(test)]
            test_port: PORT,
        };
        let resumed_view = make_view(&resumed_controller).expect("show post-relaunch safety lock");
        assert_eq!(resumed_view.status.state, "preserved");
        assert!(!resumed_view.status.can_start);
        assert!(resumed_view.status.can_clear_preserved);
        assert!(resumed_view.status.can_quit_preserved);
        assert!(!resumed_view.status.can_recover);
    }

    #[test]
    fn preserved_child_safety_lock_can_be_cleared_only_after_restart_confirmation() {
        let root = TestDir::new("clear-preserved-child");
        let project = root.0.join("project");
        fs::create_dir_all(&project).expect("create project");
        let project = project.canonicalize().expect("canonical project");
        let settings_file = root.0.join("controller.json");
        let settings = Settings {
            project_path: Some(project),
            preserved_child: Some(PreservedChild { project_path: root.0.clone(), pid: 4321 }),
            ..Settings::default()
        };
        let controller = OfficeController {
            settings_file: settings_file.clone(),
            settings: Mutex::new(settings.clone()),
            child: Mutex::new(ChildState::default()),
            action: Mutex::new(()),
            #[cfg(test)]
            test_port: PORT,
        };
        fs::write(&settings_file, serde_json::to_vec(&settings).expect("serialize safety marker"))
            .expect("write saved settings");

        assert!(clear_preserved_child_inner(&controller, false).is_err());
        assert!(controller.settings().expect("settings retained").preserved_child.is_some());

        let active_child = ChildState {
            running: true,
            starting: true,
            safe_to_exit: false,
            ..ChildState::default()
        };
        let active_controller = OfficeController {
            settings_file: settings_file.clone(),
            settings: Mutex::new(settings.clone()),
            child: Mutex::new(active_child),
            action: Mutex::new(()),
            #[cfg(test)]
            test_port: PORT,
        };
        assert!(clear_preserved_child_inner(&active_controller, true).is_err());
        assert!(active_controller.settings().expect("active marker retained").preserved_child.is_some());

        let view = clear_preserved_child_inner(&controller, true).expect("explicitly clear verified stale marker");

        assert_ne!(view.status.state, "preserved");
        assert!(controller.settings().expect("settings cleared").preserved_child.is_none());
    }

    #[test]
    fn preserved_controller_exit_requires_confirmation_and_keeps_the_marker() {
        let mut child = ChildState::<&str> { safe_to_exit: false, ..ChildState::default() };

        assert!(prepare_preserved_controller_exit(&mut child, true, false).is_err());
        assert!(!child.safe_to_exit);
        assert!(prepare_preserved_controller_exit(&mut child, false, true).is_err());
        assert!(!child.safe_to_exit);
        prepare_preserved_controller_exit(&mut child, true, true).expect("explicit controller-only exit");

        assert!(child.safe_to_exit);
        assert!(!child.running);
        assert!(prepare_preserved_controller_exit(&mut child, false, true).is_err());
    }

    #[test]
    fn failure_before_spawn_resets_start_state_without_a_child() {
        let mut state = ChildState::<&str> {
            child: None,
            pid: None,
            launch_token: String::new(),
            running: false,
            starting: true,
            safe_to_exit: false,
            startup_error: None,
        };

        record_startup_failure_state(&mut state, "spawn failed".to_owned());

        assert!(!state.running);
        assert!(!state.starting);
        assert!(state.safe_to_exit);
        assert!(state.child.is_none());
        assert!(state.pid.is_none());
        assert!(!startup_recovery_available(&state));
    }
}
