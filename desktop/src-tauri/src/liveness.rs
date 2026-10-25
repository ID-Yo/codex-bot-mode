//! Detects a leftover controller descriptor whose service is gone (#244).
//!
//! After a forced stop or an upgrade the runtime can keep a `desktop.json` that names a dead service. Such a
//! descriptor is stale only when the recorded process is missing or is not an Office service and nothing accepts
//! connections on the recorded port. A live listener, or an unknown process image, keeps the descriptor untouched so the
//! controller stays Unverified. This module does not depend on Tauri.

use serde_json::Value;
use std::{
    fs, io,
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const SERVICE_IMAGE_PREFIX: &str = "office-service";

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProcessState {
    Missing,
    /// `image` is the executable file name, or None when the system refused to disclose it.
    Running { image: Option<String> },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PortState {
    Free,
    InUse,
}

pub fn is_service_image(image: &str) -> bool {
    let name = image.rsplit(['\\', '/']).next().unwrap_or(image).to_ascii_lowercase();
    name.starts_with(SERVICE_IMAGE_PREFIX)
}

pub fn descriptor_is_stale(process: &ProcessState, port: PortState) -> bool {
    if port == PortState::InUse {
        return false;
    }
    match process {
        ProcessState::Missing => true,
        ProcessState::Running { image: Some(image) } => !is_service_image(image),
        ProcessState::Running { image: None } => false,
    }
}

pub fn port_state(port: u16) -> PortState {
    let addresses = [
        SocketAddr::from(([127, 0, 0, 1], port)),
        SocketAddr::from((std::net::Ipv6Addr::LOCALHOST, port)),
    ];
    for address in addresses {
        // Windows reports a refused loopback connection only after about 2 seconds of SYN retries.
        match TcpStream::connect_timeout(&address, Duration::from_secs(4)) {
            Ok(_) => return PortState::InUse,
            Err(error) if matches!(error.kind(), io::ErrorKind::ConnectionRefused | io::ErrorKind::AddrNotAvailable) => {}
            // A timeout or any other failure does not prove the port is free.
            Err(_) => return PortState::InUse,
        }
    }
    PortState::Free
}

#[cfg(windows)]
pub fn process_state(pid: u32) -> ProcessState {
    use std::ffi::c_void;
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn CloseHandle(handle: *mut c_void) -> i32;
        fn GetExitCodeProcess(handle: *mut c_void, code: *mut u32) -> i32;
        fn QueryFullProcessImageNameW(handle: *mut c_void, flags: u32, name: *mut u16, size: *mut u32) -> i32;
        fn GetLastError() -> u32;
    }
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const STILL_ACTIVE: u32 = 259;
    const ERROR_INVALID_PARAMETER: u32 = 87;
    // SAFETY: plain Win32 calls with a handle that is closed on every path and a buffer that outlives the call.
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return if GetLastError() == ERROR_INVALID_PARAMETER {
                ProcessState::Missing
            } else {
                ProcessState::Running { image: None }
            };
        }
        let mut code = 0u32;
        if GetExitCodeProcess(handle, &mut code) != 0 && code != STILL_ACTIVE {
            CloseHandle(handle);
            return ProcessState::Missing;
        }
        let mut buffer = vec![0u16; 32768];
        let mut size = buffer.len() as u32;
        let image = if QueryFullProcessImageNameW(handle, 0, buffer.as_mut_ptr(), &mut size) != 0 {
            Some(String::from_utf16_lossy(&buffer[..size as usize]))
        } else {
            None
        };
        CloseHandle(handle);
        ProcessState::Running { image }
    }
}

#[cfg(not(windows))]
pub fn process_state(pid: u32) -> ProcessState {
    let proc = PathBuf::from("/proc").join(pid.to_string());
    if !proc.exists() {
        return ProcessState::Missing;
    }
    ProcessState::Running { image: fs::read_link(proc.join("exe")).ok().map(|path| path.display().to_string()) }
}

/// `YYYYMMDDTHHMMSSZ` for a point in time (UTC), used in archive names.
pub fn utc_stamp(time: SystemTime) -> String {
    let seconds = time.duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) as i64;
    let (days, rest) = (seconds.div_euclid(86_400), seconds.rem_euclid(86_400));
    // Civil-from-days (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}{month:02}{day:02}T{:02}{:02}{:02}Z", rest / 3600, rest % 3600 / 60, rest % 60)
}

fn archive_name(runtime: &Path, stem: &str, stamp: &str) -> PathBuf {
    let mut candidate = runtime.join(format!("{stem}.stale-{stamp}.json"));
    let mut counter = 1;
    while candidate.exists() {
        candidate = runtime.join(format!("{stem}.stale-{stamp}-{counter}.json"));
        counter += 1;
    }
    candidate
}

/// If `desktop.json` in `runtime` names a service that is gone, moves it and `running.json` aside
/// (never deletes them) and returns the archived paths. Returns None when the descriptor is absent, unreadable, records
/// a clean stop, or may still belong to a live service.
pub fn archive_stale_descriptor(
    runtime: &Path,
    now: SystemTime,
    process_of: impl Fn(u32) -> ProcessState,
    port_of: impl Fn(u16) -> PortState,
) -> io::Result<Option<Vec<PathBuf>>> {
    let descriptor = runtime.join("desktop.json");
    let Ok(bytes) = fs::read(&descriptor) else { return Ok(None) };
    let observed_running = fs::read(runtime.join("running.json")).ok();
    let Ok(value) = serde_json::from_slice::<Value>(&bytes) else { return Ok(None) };
    if value.get("state").and_then(Value::as_str) == Some("stopped") {
        return Ok(None);
    }
    let (Some(pid), Some(port)) = (
        value.get("pid").and_then(Value::as_u64).and_then(|pid| u32::try_from(pid).ok()).filter(|pid| *pid != 0),
        value.get("port").and_then(Value::as_u64).and_then(|port| u16::try_from(port).ok()).filter(|port| *port != 0),
    ) else {
        return Ok(None);
    };
    // The port is probed only for a process that could be gone, so a healthy service is never connected to.
    if !descriptor_is_stale(&process_of(pid), PortState::Free) || port_of(port) == PortState::InUse {
        return Ok(None);
    }
    // The probes can take seconds. Archive only what was observed: if either file changed meanwhile, or the service is
    // no longer gone, a new launch published its own descriptor and must keep it.
    if fs::read(&descriptor).ok().as_deref() != Some(bytes.as_slice())
        || fs::read(runtime.join("running.json")).ok() != observed_running
        || !descriptor_is_stale(&process_of(pid), PortState::Free)
    {
        return Ok(None);
    }
    let stamp = utc_stamp(now);
    let mut moved = Vec::new();
    // desktop.json moves last: if anything fails midway the descriptor is still there and the controller stays Unverified.
    for (file, stem) in [("running.json", "running"), ("desktop.json", "desktop")] {
        let source = runtime.join(file);
        if source.is_file() {
            let target = archive_name(runtime, stem, &stamp);
            fs::rename(&source, &target)?;
            moved.push(target);
        }
    }
    Ok(Some(moved))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        net::TcpListener,
        sync::atomic::{AtomicU64, Ordering},
    };

    struct Scratch(PathBuf);
    impl Scratch {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!("office-liveness-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
        fn descriptor(&self, body: &str) {
            fs::write(self.0.join("desktop.json"), body).unwrap();
            fs::write(self.0.join("running.json"), r#"{"pid": 52692}"#).unwrap();
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    const NOW: u64 = 1_761_248_836; // 2025-10-23T19:47:16Z
    fn at(seconds: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(seconds)
    }
    fn running(image: &str) -> ProcessState {
        ProcessState::Running { image: Some(image.to_owned()) }
    }
    const DESCRIPTOR: &str = r#"{"protocol":1,"service":"codex-office","version":"0.1.0","pid":52692,"port":24242,"instance_id":"i","token":"t","state":"running"}"#;

    #[test]
    fn stale_only_when_the_process_is_gone_or_foreign_and_the_port_is_free() {
        use PortState::{Free, InUse};
        assert!(descriptor_is_stale(&ProcessState::Missing, Free));
        assert!(descriptor_is_stale(&running(r"C:\Windows\System32\notepad.exe"), Free), "a reused pid with another image");
        assert!(!descriptor_is_stale(&ProcessState::Missing, InUse), "a live unresponsive listener stays Unverified");
        assert!(!descriptor_is_stale(&running(r"C:\Users\x\Codex Office\office-service.exe"), Free), "an Office service that is merely not listening yet");
        assert!(!descriptor_is_stale(&running("notepad.exe"), InUse));
        assert!(!descriptor_is_stale(&ProcessState::Running { image: None }, Free), "an unreadable image proves nothing");
        for image in ["OFFICE-SERVICE.EXE", "office-service-x86_64-pc-windows-msvc.exe", "/opt/office/office-service"] {
            assert!(is_service_image(image), "{image}");
        }
        assert!(!is_service_image(r"C:\office-service\python.exe"), "only the file name counts");
    }

    #[test]
    fn dead_pid_with_a_free_port_is_archived_and_nothing_is_deleted() {
        let dir = Scratch::new();
        dir.descriptor(DESCRIPTOR);
        let moved = archive_stale_descriptor(&dir.0, at(NOW), |_| ProcessState::Missing, |_| PortState::Free).unwrap().expect("archived");
        assert_eq!(moved.len(), 2);
        assert!(!dir.0.join("desktop.json").exists() && !dir.0.join("running.json").exists());
        let archived = dir.0.join("desktop.stale-20251023T194716Z.json");
        assert_eq!(fs::read_to_string(archived).unwrap(), DESCRIPTOR, "contents are kept as evidence");
        assert_eq!(fs::read_to_string(dir.0.join("running.stale-20251023T194716Z.json")).unwrap(), r#"{"pid": 52692}"#);
    }

    #[test]
    fn descriptor_replaced_during_the_probe_is_left_alone() {
        let dir = Scratch::new();
        dir.descriptor(DESCRIPTOR);
        let replacement = DESCRIPTOR.replace("52692", "60001").replace("\"t\"", "\"new-token\"");
        // The old endpoint looks free, but while it is probed a new service publishes its descriptor and launch record.
        let port_of = |_| {
            fs::write(dir.0.join("desktop.json"), &replacement).unwrap();
            fs::write(dir.0.join("running.json"), r#"{"pid": 60001}"#).unwrap();
            PortState::Free
        };
        assert!(archive_stale_descriptor(&dir.0, at(NOW), |_| ProcessState::Missing, port_of).unwrap().is_none());
        assert_eq!(fs::read_to_string(dir.0.join("desktop.json")).unwrap(), replacement);
        assert_eq!(fs::read_to_string(dir.0.join("running.json")).unwrap(), r#"{"pid": 60001}"#);
        let archived = fs::read_dir(&dir.0).unwrap().filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().contains(".stale-")).count();
        assert_eq!(archived, 0, "nothing of the new launch is archived");
    }

    #[test]
    fn service_that_appears_before_the_move_keeps_its_descriptor() {
        let dir = Scratch::new();
        dir.descriptor(DESCRIPTOR);
        let calls = std::cell::Cell::new(0);
        // Gone at the first look, alive (same files) at the re-check.
        let process_of = |_| {
            calls.set(calls.get() + 1);
            if calls.get() == 1 { ProcessState::Missing } else { running("office-service.exe") }
        };
        assert!(archive_stale_descriptor(&dir.0, at(NOW), process_of, |_| PortState::Free).unwrap().is_none());
        assert!(dir.0.join("desktop.json").exists() && dir.0.join("running.json").exists());
    }

    #[test]
    fn reused_pid_with_a_different_image_is_archived() {
        let dir = Scratch::new();
        dir.descriptor(DESCRIPTOR);
        assert!(archive_stale_descriptor(&dir.0, at(NOW), |_| running("chrome.exe"), |_| PortState::Free).unwrap().is_some());
    }

    #[test]
    fn live_unresponsive_listener_keeps_the_descriptor() {
        let dir = Scratch::new();
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        dir.descriptor(&DESCRIPTOR.replace("24242", &port.to_string()));
        // Dead pid, but something still accepts connections on the port: use the real probe.
        assert!(archive_stale_descriptor(&dir.0, at(NOW), |_| ProcessState::Missing, port_state).unwrap().is_none());
        assert!(dir.0.join("desktop.json").exists() && dir.0.join("running.json").exists());
    }

    #[test]
    fn healthy_service_keeps_the_descriptor() {
        let dir = Scratch::new();
        dir.descriptor(DESCRIPTOR);
        assert!(archive_stale_descriptor(&dir.0, at(NOW), |_| running("office-service.exe"), |_| PortState::InUse).unwrap().is_none());
        assert!(dir.0.join("desktop.json").exists());
    }

    #[test]
    fn unreadable_clean_or_incomplete_descriptors_are_left_alone() {
        let dir = Scratch::new();
        for body in ["{ malformed", &DESCRIPTOR.replace("running", "stopped"), r#"{"state":"running"}"#, r#"{"pid":0,"port":24242}"#, r#"{"pid":5,"port":70000}"#] {
            dir.descriptor(body);
            assert!(archive_stale_descriptor(&dir.0, at(NOW), |_| ProcessState::Missing, |_| PortState::Free).unwrap().is_none(), "{body}");
            assert!(dir.0.join("desktop.json").exists());
        }
        let empty = Scratch::new();
        assert!(archive_stale_descriptor(&empty.0, at(NOW), |_| ProcessState::Missing, |_| PortState::Free).unwrap().is_none());
    }

    #[test]
    fn a_second_archive_in_the_same_second_does_not_overwrite_the_first() {
        let dir = Scratch::new();
        for _ in 0..2 {
            dir.descriptor(DESCRIPTOR);
            assert!(archive_stale_descriptor(&dir.0, at(NOW), |_| ProcessState::Missing, |_| PortState::Free).unwrap().is_some());
        }
        assert!(dir.0.join("desktop.stale-20251023T194716Z.json").exists());
        assert!(dir.0.join("desktop.stale-20251023T194716Z-1.json").exists());
    }

    #[test]
    fn stamps_are_utc_and_sortable() {
        assert_eq!(utc_stamp(at(0)), "19700101T000000Z");
        assert_eq!(utc_stamp(at(1_000_000_000)), "20010909T014640Z");
        assert_eq!(utc_stamp(at(1_709_251_199)), "20240229T235959Z", "leap day");
        assert_eq!(utc_stamp(at(NOW)), "20251023T194716Z");
    }

    #[test]
    fn a_closed_loopback_port_is_free_and_an_open_one_is_in_use() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        assert_eq!(port_state(port), PortState::InUse);
        drop(listener);
        assert_eq!(port_state(port), PortState::Free);
    }

    #[test]
    fn real_process_state_follows_a_child_process() {
        let me = process_state(std::process::id());
        assert!(matches!(me, ProcessState::Running { .. }));
        #[cfg(windows)]
        let mut command = std::process::Command::new("cmd.exe");
        #[cfg(windows)]
        command.args(["/C", "ping", "-n", "30", "127.0.0.1"]);
        #[cfg(not(windows))]
        let mut command = std::process::Command::new("sleep");
        #[cfg(not(windows))]
        command.arg("30");
        let mut child = command.spawn().unwrap();
        assert!(matches!(process_state(child.id()), ProcessState::Running { .. }));
        child.kill().unwrap();
        child.wait().unwrap();
        assert_eq!(process_state(child.id()), ProcessState::Missing);
    }
}
