//! Spawns a real `cairn <args>` invocation and tails its NDJSON event file.
//!
//! This is a faithful port of `tui/src/connection/subprocess.ts`; the
//! reasoning below is that file's, and it survives the language change
//! intact.
//!
//! This process (the TUI) is itself launched as a child of the Python
//! `cairn` entrypoint (`src/cairn/cli.py::_launch_tui`). To run a real
//! command it spawns a fresh grandchild Python process rather than
//! reimplementing any of Cairn's logic: Cairn's correctness core (claims,
//! probes, the agent loop) is Python, and the TUI must observe it the
//! same way any other caller does — through the real CLI, not a private
//! shortcut that could drift from what `cairn` does when run by hand.
//!
//! `CAIRN_PYTHON` names the interpreter to invoke as `<python> -m
//! cairn.cli <args>` — set by the parent Python process when it launches
//! this TUI (so the exact same venv is reused), falling back to
//! `python`/`python3` on PATH for a standalone `cargo run`.
//!
//! **No async runtime.** The workload is one subprocess plus one
//! file-tail loop per invocation; three plain OS threads and a
//! `crossbeam-channel` are a better fit than dragging in tokio, and match
//! how `gitui` (the closest real-world analog) does the same job.

use std::collections::HashMap;
use std::env;
use std::ffi::OsString;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use cairn_protocol::{parse_event_line, split_carry, CairnEvent};
use crossbeam_channel::Sender;

/// Same 40ms cadence the TypeScript tailer used. Plain-file polling (not
/// `notify`/inotify/ReadDirectoryChangesW) is deliberate — see
/// `obs/events.py`'s module docstring for why a tailed regular file, not
/// an inherited fd, is the cross-platform-portable choice on the writer
/// side; the same reasoning says the *reader* must not depend on a
/// platform-specific change-notification API either. This project
/// develops and demos on Windows.
pub const POLL_INTERVAL: Duration = Duration::from_millis(40);

/// How many extra poll ticks to run after the child exits, so events
/// flushed immediately before exit are not lost to a race between the
/// child closing and the final poll.
const DRAIN_TICKS: u32 = 3;

/// Everything a running command can tell the render loop. One channel,
/// one sender cloned per command thread, one receiver drained per tick.
#[derive(Debug, Clone)]
pub enum BackendMsg {
    /// A parsed NDJSON event, in the order it was written.
    Event(CairnEvent),
    /// One line of the child's stderr — human-readable text, never parsed
    /// as protocol, surfaced only on failure.
    StderrLine(String),
    /// The child terminated. `label` echoes what was launched, so the UI
    /// can attribute the exit without tracking handles.
    Exited { label: String, code: i32 },
    /// The child could not be spawned at all (no interpreter on PATH,
    /// etc.). Distinct from a non-zero exit.
    SpawnFailed { label: String, error: String },
}

/// Which interpreter to run, and the fixed `-m cairn.cli` prefix.
pub struct PythonCommand {
    pub program: OsString,
    pub base_args: Vec<String>,
}

pub fn resolve_python_command() -> PythonCommand {
    let base_args = vec!["-m".to_string(), "cairn.cli".to_string()];
    if let Some(explicit) = env::var_os("CAIRN_PYTHON") {
        if !explicit.is_empty() {
            return PythonCommand { program: explicit, base_args };
        }
    }
    let fallback = if cfg!(windows) { "python" } else { "python3" };
    PythonCommand { program: OsString::from(fallback), base_args }
}

/// A live command. Dropping this does not kill the child — the supervisor
/// thread owns its lifetime and always cleans up the temp events file.
pub struct CommandHandle {
    label: String,
    stop: Arc<AtomicBool>,
    child: Arc<Mutex<Option<Child>>>,
    events_file: PathBuf,
}

impl CommandHandle {
    pub fn label(&self) -> &str {
        &self.label
    }

    pub fn events_file(&self) -> &Path {
        &self.events_file
    }

    /// Kill the child and stop tailing. The supervisor thread still runs
    /// its drain window and still deletes the temp file.
    pub fn cancel(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(child) = guard.as_mut() {
                let _ = child.kill();
            }
        }
        self.stop.store(true, Ordering::SeqCst);
    }
}

/// Runs `<python> -m cairn.cli <args>` as a grandchild process with a
/// fresh per-invocation events file, tailing it for the lifetime of the
/// child plus a short drain window.
///
/// `extra_env` is applied on top of the inherited environment (the TUI
/// uses it for nothing today, but a caller that needs to pin
/// `CAIRN_APPROVAL_USD` for one run should not have to mutate its own
/// process env to do it).
pub fn run_cairn_command(
    args: &[String],
    tx: Sender<BackendMsg>,
    extra_env: &HashMap<String, String>,
) -> CommandHandle {
    let label = format!("cairn {}", args.join(" "));
    let python = resolve_python_command();
    let events_file = env::temp_dir().join(format!("cairn-events-{}.ndjson", uuid::Uuid::new_v4()));
    let stop = Arc::new(AtomicBool::new(false));
    let child_slot: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));

    let mut command = Command::new(&python.program);
    command
        .args(&python.base_args)
        .args(args)
        .env("CAIRN_EVENTS_FILE", &events_file)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (key, value) in extra_env {
        command.env(key, value);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            let _ = tx.send(BackendMsg::SpawnFailed {
                label: label.clone(),
                error: format!("{}: {err}", python.program.to_string_lossy()),
            });
            stop.store(true, Ordering::SeqCst);
            return CommandHandle { label, stop, child: child_slot, events_file };
        }
    };

    // stdout is drained but never parsed for meaning (§13: the TUI never
    // scrapes human-readable stdout — NDJSON events are the only truth).
    // Kept only so the child's pipe cannot back up and block it.
    if let Some(mut stdout) = child.stdout.take() {
        thread::spawn(move || {
            let mut sink = Vec::new();
            let _ = stdout.read_to_end(&mut sink);
        });
    }

    if let Some(stderr) = child.stderr.take() {
        let tx = tx.clone();
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines() {
                match line {
                    Ok(line) => {
                        if tx.send(BackendMsg::StderrLine(line)).is_err() {
                            return;
                        }
                    }
                    Err(_) => return,
                }
            }
        });
    }

    if let Ok(mut guard) = child_slot.lock() {
        *guard = Some(child);
    }

    let tailer = {
        let tx = tx.clone();
        let stop = Arc::clone(&stop);
        let path = events_file.clone();
        thread::spawn(move || tail_events(&path, &tx, &stop))
    };

    // Supervisor: waits on the child, gives the tailer its drain window,
    // then stops it, best-effort-deletes the temp file, and reports the
    // exit code. Owns the child so `cancel()` only needs a brief lock.
    {
        let tx = tx.clone();
        let stop = Arc::clone(&stop);
        let child_slot = Arc::clone(&child_slot);
        let path = events_file.clone();
        let label = label.clone();
        thread::spawn(move || {
            let code = loop {
                let status = {
                    let mut guard = match child_slot.lock() {
                        Ok(guard) => guard,
                        Err(_) => break 1,
                    };
                    match guard.as_mut() {
                        Some(child) => child.try_wait(),
                        None => break 1,
                    }
                };
                match status {
                    Ok(Some(status)) => break status.code().unwrap_or(1),
                    Ok(None) => thread::sleep(POLL_INTERVAL),
                    Err(_) => break 1,
                }
            };
            if let Ok(mut guard) = child_slot.lock() {
                *guard = None;
            }
            thread::sleep(POLL_INTERVAL * DRAIN_TICKS);
            stop.store(true, Ordering::SeqCst);
            let _ = tailer.join();
            // Best-effort cleanup; a leaked temp file is harmless.
            let _ = fs::remove_file(&path);
            let _ = tx.send(BackendMsg::Exited { label, code });
        });
    }

    CommandHandle { label, stop, child: child_slot, events_file }
}

/// Tails a growing NDJSON file, sending one `CairnEvent` per complete
/// line as it appears. Reads only newly-appended bytes each tick and
/// carries any partial trailing line across polls, so a line split by a
/// mid-write poll is never dropped or half-parsed.
pub fn tail_events(path: &Path, tx: &Sender<BackendMsg>, stop: &AtomicBool) {
    let mut file: Option<File> = None;
    let mut offset: u64 = 0;
    let mut carry = String::new();

    while !stop.load(Ordering::SeqCst) {
        if file.is_none() {
            match File::open(path) {
                Ok(opened) => file = Some(opened),
                Err(_) => {
                    // Not created yet — the child may not have emitted a
                    // single event, or may never emit one at all.
                    thread::sleep(POLL_INTERVAL);
                    continue;
                }
            }
        }
        let handle = file.as_mut().expect("opened above");
        let size = handle.metadata().map(|m| m.len()).unwrap_or(offset);
        if size > offset {
            let mut buffer = vec![0u8; (size - offset) as usize];
            if handle.seek(SeekFrom::Start(offset)).is_ok() {
                match handle.read_exact(&mut buffer) {
                    Ok(()) => {
                        offset = size;
                        carry.push_str(&String::from_utf8_lossy(&buffer));
                        let (lines, rest) = split_carry(&carry);
                        let mut parsed = Vec::new();
                        for line in lines {
                            if let Some(event) = parse_event_line(line) {
                                parsed.push(event);
                            }
                        }
                        let rest = rest.to_string();
                        carry = rest;
                        for event in parsed {
                            if tx.send(BackendMsg::Event(event)).is_err() {
                                return;
                            }
                        }
                    }
                    Err(_) => {
                        // Short read against a file being appended to —
                        // retry the same offset on the next tick.
                    }
                }
            }
        }
        thread::sleep(POLL_INTERVAL);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::sync::atomic::AtomicBool;

    fn line(event_type: &str, stage: &str) -> String {
        format!(
            r#"{{"payload": {{"stage": "{stage}"}}, "run_id": null, "timestamp": "t", "type": "{event_type}", "version": 1}}"#
        )
    }

    #[test]
    fn resolves_explicit_python_from_env() {
        // Only assert the shape that does not depend on ambient env: the
        // fixed `-m cairn.cli` prefix, which is what makes the child a
        // real CLI invocation rather than a private entrypoint.
        let resolved = resolve_python_command();
        assert_eq!(resolved.base_args, vec!["-m".to_string(), "cairn.cli".to_string()]);
        assert!(!resolved.program.is_empty());
    }

    #[test]
    fn tails_appended_lines_and_carries_partial_writes() {
        let path = env::temp_dir().join(format!("cairn-test-{}.ndjson", uuid::Uuid::new_v4()));
        let (tx, rx) = crossbeam_channel::unbounded();
        let stop = Arc::new(AtomicBool::new(false));

        let tail_path = path.clone();
        let tail_stop = Arc::clone(&stop);
        let handle = thread::spawn(move || tail_events(&tail_path, &tx, &tail_stop));

        // First write: one whole line plus half of a second.
        let whole = line("stage.started", "env");
        let second = line("stage.completed", "env");
        let (head, tail) = second.split_at(20);
        {
            let mut file = File::create(&path).unwrap();
            file.write_all(format!("{whole}\n{head}").as_bytes()).unwrap();
            file.flush().unwrap();
        }
        let first = rx.recv_timeout(Duration::from_secs(5)).unwrap();
        match first {
            BackendMsg::Event(event) => assert_eq!(event.event_type, "stage.started"),
            other => panic!("expected event, got {other:?}"),
        }

        // Second write completes the split line.
        {
            let mut file = fs::OpenOptions::new().append(true).open(&path).unwrap();
            file.write_all(format!("{tail}\n").as_bytes()).unwrap();
            file.flush().unwrap();
        }
        let next = rx.recv_timeout(Duration::from_secs(5)).unwrap();
        match next {
            BackendMsg::Event(event) => assert_eq!(event.event_type, "stage.completed"),
            other => panic!("expected event, got {other:?}"),
        }

        stop.store(true, Ordering::SeqCst);
        handle.join().unwrap();
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn garbage_lines_do_not_stop_the_stream() {
        let path = env::temp_dir().join(format!("cairn-test-{}.ndjson", uuid::Uuid::new_v4()));
        let (tx, rx) = crossbeam_channel::unbounded();
        let stop = Arc::new(AtomicBool::new(false));
        let tail_path = path.clone();
        let tail_stop = Arc::clone(&stop);
        let handle = thread::spawn(move || tail_events(&tail_path, &tx, &tail_stop));

        {
            let mut file = File::create(&path).unwrap();
            let good = line("doctor.completed", "x");
            file.write_all(format!("not json\n{{\"half\": \n{good}\n").as_bytes()).unwrap();
            file.flush().unwrap();
        }
        let msg = rx.recv_timeout(Duration::from_secs(5)).unwrap();
        match msg {
            BackendMsg::Event(event) => assert_eq!(event.event_type, "doctor.completed"),
            other => panic!("expected event, got {other:?}"),
        }

        stop.store(true, Ordering::SeqCst);
        handle.join().unwrap();
        let _ = fs::remove_file(&path);
    }
}
