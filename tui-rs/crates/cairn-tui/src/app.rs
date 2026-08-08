//! The part of the TUI that is neither pure state nor pure drawing: which
//! key does what, which slash command becomes which real `cairn` argv, and
//! the plumbing between the backend channel and `AppState::apply_event`.
//!
//! Command dispatch is a port of `tui/src/app.ts::dispatchSlashCommand`
//! and the argv construction is a port of
//! `tui/src/connection/cairn-connection.ts`. Both are exercised by the
//! tests at the bottom of this file, because the argv contract is the one
//! place where a silent drift would make the TUI *look* like it ran what
//! you asked while running something else.

use std::collections::HashMap;
use std::time::Instant;

use cairn_backend::{run_cairn_command, BackendMsg, CommandHandle};
use cairn_state::commands::{
    complete, help_lines, lookup, match_free_text_intent, narration_for_run, parse_slash_input,
};
use cairn_state::pipeline::PIPELINE_STAGES;
use cairn_state::transcript::TranscriptKind;
use cairn_state::ui::{InputMode, Panel, ThemeName, PANELS, THEME_NAMES};
use cairn_state::AppState;
use crossbeam_channel::{unbounded, Receiver, Sender};
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};

use crate::theme::Theme;

pub struct App {
    pub state: AppState,
    pub theme: Theme,
    started: Instant,
    tx: Sender<BackendMsg>,
    rx: Receiver<BackendMsg>,
    /// The command currently in flight, if any. Kept so `q` during a run
    /// can kill it rather than orphaning a grandchild Python process.
    handle: Option<CommandHandle>,
    /// Whether the in-flight command has produced any protocol event yet.
    /// A non-zero exit with no events is the only case where stderr is the
    /// honest thing to show.
    saw_events: bool,
    /// Test seam: when set, `spawn` records the argv instead of launching a
    /// real subprocess, so the argv contract can be asserted without a
    /// Python interpreter.
    dry_run: bool,
    pub last_spawn: Option<Vec<String>>,
}

impl App {
    pub fn new(theme_name: ThemeName) -> Self {
        let (tx, rx) = unbounded();
        Self {
            state: AppState::new(),
            theme: Theme::load(theme_name),
            started: Instant::now(),
            tx,
            rx,
            handle: None,
            saw_events: false,
            dry_run: false,
            last_spawn: None,
        }
    }

    #[cfg(test)]
    fn for_test() -> Self {
        let mut app = Self::new(ThemeName::CairnDark);
        app.dry_run = true;
        app
    }

    /// Advance the injected clock. The reducer and every countdown read
    /// this, so the lease bar is driven by real elapsed time without any
    /// part of `cairn-state` ever calling a clock itself.
    pub fn tick(&mut self) {
        self.state.set_now(self.started.elapsed().as_millis() as u64);
    }

    pub fn should_quit(&self) -> bool {
        self.state.ui.should_quit
    }

    /// Drain everything the backend threads have queued. Returns true if
    /// anything arrived, so the render loop can skip a redraw when nothing
    /// changed.
    pub fn drain_backend(&mut self) -> bool {
        let mut changed = false;
        while let Ok(message) = self.rx.try_recv() {
            changed = true;
            match message {
                BackendMsg::Event(event) => {
                    self.saw_events = true;
                    self.state.apply_event(event);
                }
                BackendMsg::StderrLine(line) => self.state.push_stderr(line),
                BackendMsg::Exited { label, code } => {
                    let saw_events = self.saw_events;
                    self.state.command_exited(&label, code, saw_events);
                    self.handle = None;
                }
                BackendMsg::SpawnFailed { label, error } => {
                    self.state.session.running = false;
                    self.handle = None;
                    let message = format!("could not start `{label}`: {error}");
                    self.state.transcript.push(TranscriptKind::Error, message.clone(), "");
                    self.state.note_status(message);
                }
            }
        }
        changed
    }

    // --- command dispatch ---------------------------------------------

    fn spawn(&mut self, args: Vec<String>) {
        if self.state.session.running {
            self.state.note_status("a command is already running — Esc then q to cancel it");
            return;
        }
        let label = format!("cairn {}", args.join(" "));
        self.state.command_started(label);
        self.saw_events = false;
        self.last_spawn = Some(args.clone());
        if self.dry_run {
            return;
        }
        self.handle = Some(run_cairn_command(&args, self.tx.clone(), &HashMap::new()));
    }

    fn info(&mut self, text: impl Into<String>) {
        self.state.transcript.push(TranscriptKind::Info, text, "");
    }

    fn narrate(&mut self, text: impl Into<String>) {
        self.state.transcript.push(TranscriptKind::Narration, text, "");
    }

    /// Port of `app.ts::dispatchSlashCommand`. Every branch either spawns a
    /// real `cairn` subcommand or renders state this session actually
    /// observed — nothing here fabricates a result.
    pub fn dispatch(&mut self, name: &str, args: &str) {
        let args = args.trim();
        match name {
            "run" => {
                self.narrate(narration_for_run(args));
                let mut argv = vec!["run".to_string()];
                if args.is_empty() || args == "--all" {
                    argv.push("--all".to_string());
                } else {
                    // Only a real stage is passed through as a positional;
                    // anything else would reach the CLI as a bad argument
                    // and fail in a way the panels could not explain.
                    let stage = args.split_whitespace().next().unwrap_or("");
                    if !PIPELINE_STAGES.contains(&stage) {
                        self.state.transcript.push(
                            TranscriptKind::Error,
                            format!(
                                "unknown stage `{stage}` — try one of: {}",
                                PIPELINE_STAGES.join(", ")
                            ),
                            "",
                        );
                        self.state.session.running = false;
                        self.state.session.current_command = None;
                        return;
                    }
                    argv.push(stage.to_string());
                }
                self.spawn(argv);
            }
            "plan" => self.spawn(vec!["plan".into(), "--output".into(), "json".into()]),
            "explain" => {
                if args.is_empty() {
                    self.narrate("Usage: /explain <artifact_id>");
                    return;
                }
                let id = args.split_whitespace().next().unwrap_or("").to_string();
                self.spawn(vec!["explain".into(), id, "--output".into(), "json".into()]);
            }
            "memory" => {
                if args.is_empty() {
                    self.narrate("Usage: /memory <text>  ·  /memory why-blocked");
                    return;
                }
                if args == "why-blocked" {
                    self.spawn(vec![
                        "memory".into(),
                        "why-blocked".into(),
                        "--output".into(),
                        "json".into(),
                    ]);
                } else {
                    self.spawn(vec!["memory".into(), "search".into(), args.to_string()]);
                }
                self.state.ui.focus = Panel::Memory;
            }
            "doctor" => self.spawn(vec!["doctor".into()]),
            "model" => {
                // The fixed catalog from `classify/llm.py` and
                // `embeddings.py`. Presented as fixed precisely because the
                // backend has no runtime model-switching path — offering a
                // selector would imply a capability that does not exist.
                for line in [
                    "Models",
                    "  anthropic.claude-sonnet-5      [bedrock]  reasoning · current",
                    "  amazon.titan-embed-text-v2:0   [bedrock]  embeddings · current",
                    "  Fixed catalog from src/cairn/classify/llm.py and embeddings.py —",
                    "  the backend has no runtime model-switching path yet.",
                ] {
                    self.info(line);
                }
            }
            "status" => {
                let mut lines = vec![
                    "Status".to_string(),
                    format!("  activity     {}", self.state.activity.label),
                    format!("  theme        {}", self.theme.name.as_str()),
                    format!("  colour       {}", if self.theme.color_enabled { "on" } else { "off" }),
                    format!(
                        "  run_id       {}",
                        self.state.session.run_id.clone().unwrap_or_else(|| "-".into())
                    ),
                    format!(
                        "  worker       {}",
                        self.state.session.owner.clone().unwrap_or_else(|| "-".into())
                    ),
                    format!("  events seen  {}", self.state.events_seen),
                ];
                match self.state.doctor.gating_ok {
                    Some(_) => {
                        lines.push(format!(
                            "  database     {}",
                            health(self.state.doctor.database_ok, "healthy", "unreachable")
                        ));
                        lines.push(format!(
                            "  schema       {}",
                            health(self.state.doctor.schema_ok, "up to date", "pending")
                        ));
                        lines.push(format!(
                            "  aws          {}",
                            health(self.state.doctor.aws_ok, "authenticated", "not verified")
                        ));
                    }
                    // Never guess at health we have not measured.
                    None => lines.push("  database     unknown — run /doctor".to_string()),
                }
                if !self.state.unhandled_types.is_empty() {
                    lines.push(format!(
                        "  unhandled    {}",
                        self.state.unhandled_types.join(", ")
                    ));
                }
                for line in lines {
                    self.info(line);
                }
            }
            "usage" => {
                let usage = self.state.ledger.usage;
                for line in [
                    "Usage — decisions observed this session".to_string(),
                    format!("  reused                {}", usage.reused),
                    format!("  recomputed            {}", usage.recomputed),
                    format!("  duplicates prevented  {}", usage.duplicates_prevented),
                    format!("  failures avoided      {}", usage.failures_avoided),
                    format!("  resumed               {}", usage.resumed),
                    format!("  remediated            {}", usage.remediated),
                    format!("  escalated             {}", usage.escalated),
                    // The honest caveat the TS panel also carried: these
                    // are this session's observations, not a persisted
                    // lifetime total.
                    "  Counts of decisions this session saw — not a stored total.".to_string(),
                ] {
                    self.info(line);
                }
            }
            "theme" => match ThemeName::parse(args) {
                Some(name) => {
                    self.set_theme(name);
                }
                None => {
                    let joined = THEME_NAMES.join(", ");
                    self.narrate(format!("Unknown theme `{args}`. Try: {joined}"));
                }
            },
            "settings" => {
                let theme_name = self.theme.name.as_str();
                let color = if self.theme.color_enabled { "on" } else { "off" };
                let no_color = if crate::theme::no_color_requested() {
                    "  NO_COLOR is set in the environment, so colour stays off."
                } else {
                    "  NO_COLOR is not set."
                };
                for line in [
                    "Settings".to_string(),
                    format!("  theme    {theme_name}    change: /theme <{}>", THEME_NAMES.join("|")),
                    format!("  colour   {color}"),
                    no_color.to_string(),
                ] {
                    self.info(line);
                }
            }
            "help" => {
                self.state.ui.show_help = true;
                for line in std::iter::once("Commands".to_string()).chain(help_lines()) {
                    self.info(line);
                }
            }
            "clear" => {
                self.state.transcript.clear();
                self.state.ui.transcript_scroll = None;
            }
            other => {
                self.state.transcript.push(
                    TranscriptKind::Error,
                    format!("Unknown command /{other}. Try /help."),
                    "",
                );
            }
        }
    }

    fn set_theme(&mut self, name: ThemeName) {
        self.state.ui.theme = name;
        self.theme = Theme::load(name);
        if !self.theme.color_enabled && name != ThemeName::Mono {
            self.narrate(format!(
                "Switched to {}. NO_COLOR is set, so it renders without colour.",
                name.as_str()
            ));
        } else {
            self.narrate(format!("Switched to {}.", name.as_str()));
        }
    }

    /// What the command line does on Enter. Free text is only ever matched
    /// against the two fixed patterns in `commands::match_free_text_intent`
    /// — there is no LLM in this process, so anything else is refused
    /// rather than guessed at.
    pub fn submit_input(&mut self) {
        let input = self.state.ui.input.trim().to_string();
        let mode = self.state.ui.input_mode;
        self.state.ui.cancel_input();
        if input.is_empty() {
            return;
        }
        if mode == InputMode::MemorySearch {
            self.dispatch("memory", &input);
            return;
        }
        self.state.ui.history.push(input.clone());
        match parse_slash_input(&input) {
            Some(parsed) => self.dispatch(&parsed.name, &parsed.args),
            None => match match_free_text_intent(&input) {
                Some(parsed) => self.dispatch(&parsed.name, &parsed.args),
                None => self.narrate(
                    "I only act on the slash commands — press ? for the list, or / to type one.",
                ),
            },
        }
    }

    /// Tab cycles through the candidates rather than committing to the
    /// first: with several stages sharing a prefix, silently picking one
    /// would run the wrong stage.
    ///
    /// The cycle walks the *stored* candidate list, not a freshly computed
    /// one. Recomputing would use the text the previous Tab just wrote,
    /// whose prefix matches only itself — so the list would collapse to a
    /// single entry and Tab would appear to jam on the first candidate.
    fn apply_completion(&mut self) {
        let mid_cycle = self
            .state
            .ui
            .completions
            .get(self.state.ui.completion_index)
            .is_some_and(|current| *current == self.state.ui.input);

        let (candidates, index) = if mid_cycle {
            let candidates = self.state.ui.completions.clone();
            let index = (self.state.ui.completion_index + 1) % candidates.len();
            (candidates, index)
        } else {
            (complete(&self.state.ui.input, &self.state.knowledge), 0)
        };
        if candidates.is_empty() {
            return;
        }
        self.state.ui.input = candidates[index].clone();
        self.state.ui.cursor = self.state.ui.input.len();
        self.state.ui.completions = candidates;
        self.state.ui.completion_index = index;
    }

    fn history_step(&mut self, delta: isize) {
        if self.state.ui.history.is_empty() {
            return;
        }
        let len = self.state.ui.history.len();
        let next = match (self.state.ui.history_index, delta) {
            (None, -1) => Some(len - 1),
            (Some(index), -1) => Some(index.saturating_sub(1)),
            (Some(index), 1) if index + 1 < len => Some(index + 1),
            (Some(_), 1) => None,
            (None, _) => None,
            (Some(index), _) => Some(index),
        };
        self.state.ui.history_index = next;
        self.state.ui.input = match next {
            Some(index) => self.state.ui.history[index].clone(),
            None => String::new(),
        };
        self.state.ui.cursor = self.state.ui.input.len();
        self.state.ui.completions.clear();
    }

    // --- keys ----------------------------------------------------------

    pub fn handle_key(&mut self, key: KeyEvent) {
        // Windows delivers both Press and Release; acting on both would run
        // every command twice.
        if key.kind == KeyEventKind::Release {
            return;
        }
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        if ctrl && matches!(key.code, KeyCode::Char('c') | KeyCode::Char('C')) {
            self.request_quit();
            return;
        }

        match self.state.ui.input_mode {
            InputMode::Command | InputMode::MemorySearch => self.handle_input_key(key),
            InputMode::ConfirmQuit => self.handle_confirm_key(key),
            InputMode::Normal => self.handle_normal_key(key),
        }
    }

    fn handle_input_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc => {
                self.state.ui.cancel_input();
            }
            KeyCode::Enter => self.submit_input(),
            KeyCode::Tab => {
                if self.state.ui.input_mode == InputMode::Command {
                    self.apply_completion();
                }
            }
            KeyCode::Backspace => {
                self.state.ui.backspace();
                self.state.ui.completions.clear();
            }
            KeyCode::Left => self.state.ui.move_cursor(-1),
            KeyCode::Right => self.state.ui.move_cursor(1),
            KeyCode::Up => self.history_step(-1),
            KeyCode::Down => self.history_step(1),
            KeyCode::Char(ch) => {
                self.state.ui.insert_char(ch);
                self.state.ui.completions.clear();
            }
            _ => {}
        }
    }

    fn handle_confirm_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Char('y') | KeyCode::Char('Y') => {
                if let Some(handle) = self.handle.take() {
                    handle.cancel();
                }
                self.state.ui.should_quit = true;
            }
            _ => {
                self.state.ui.input_mode = InputMode::Normal;
            }
        }
    }

    fn handle_normal_key(&mut self, key: KeyEvent) {
        let focused_len = self.state.focused_len();
        match key.code {
            KeyCode::Esc => {
                if !self.state.ui.escape() {
                    self.state.ui.status_message = None;
                }
            }
            KeyCode::Char('q') => self.request_quit(),
            KeyCode::Char('?') => self.state.ui.show_help = !self.state.ui.show_help,
            KeyCode::Char('/') | KeyCode::Char(':') => {
                let seed = if key.code == KeyCode::Char(':') { ":" } else { "/" };
                self.state.ui.begin_input(InputMode::Command, seed);
            }
            KeyCode::Char(c @ '1'..='5') => {
                let index = c as usize - '1' as usize;
                self.state.ui.focus = PANELS[index];
            }
            KeyCode::Tab => self.state.ui.focus = self.state.ui.focus.next(),
            KeyCode::BackTab => self.state.ui.focus = self.state.ui.focus.prev(),
            KeyCode::Char('j') | KeyCode::Down => self.move_selection(focused_len, 1),
            KeyCode::Char('k') | KeyCode::Up => self.move_selection(focused_len, -1),
            KeyCode::PageDown => self.move_selection(focused_len, 10),
            KeyCode::PageUp => self.move_selection(focused_len, -10),
            KeyCode::Char('g') => self.move_selection(focused_len, -(focused_len as isize)),
            KeyCode::Char('G') => self.move_selection(focused_len, focused_len as isize),
            KeyCode::Char('+') | KeyCode::Char('z') => {
                self.state.ui.zoomed = !self.state.ui.zoomed;
            }
            KeyCode::Enter | KeyCode::Char('e') => self.explain_selection(),
            KeyCode::Char('r') => match self.state.selected_stage() {
                Some(stage) => self.dispatch("run", &stage),
                None => self.state.note_status("no stage selected"),
            },
            KeyCode::Char('R') => self.dispatch("run", "--all"),
            KeyCode::Char('p') => self.dispatch("plan", ""),
            KeyCode::Char('d') => self.dispatch("doctor", ""),
            KeyCode::Char('m') => {
                self.state.ui.focus = Panel::Memory;
                self.state.ui.begin_input(InputMode::MemorySearch, "");
            }
            KeyCode::Char('t') => {
                let next = self.state.ui.theme.next();
                self.set_theme(next);
            }
            _ => {}
        }
    }

    fn move_selection(&mut self, len: usize, delta: isize) {
        self.state.ui.move_selection(len, delta);
        if self.state.ui.focus == Panel::Transcript {
            // Scrolling the log detaches it from the bottom; `G` re-pins it,
            // so a live run does not keep yanking the view away mid-read.
            let index = self.state.ui.selected(Panel::Transcript);
            self.state.ui.transcript_scroll =
                if index + 1 >= len { None } else { Some(index) };
        }
    }

    /// `Enter`/`e`: drill into whatever the focused panel has selected. On
    /// the Memory panel there is no artifact to explain, so it asks the
    /// backend the one question that panel *can* answer.
    fn explain_selection(&mut self) {
        if self.state.ui.focus == Panel::Memory {
            self.dispatch("memory", "why-blocked");
            return;
        }
        match self.state.selected_artifact_id() {
            Some(id) => self.dispatch("explain", &id),
            None => self
                .state
                .note_status("nothing to explain here — this row has no artifact id yet"),
        }
    }

    fn request_quit(&mut self) {
        if self.state.session.running {
            self.state.ui.input_mode = InputMode::ConfirmQuit;
        } else {
            self.state.ui.should_quit = true;
        }
    }

    /// Completion candidates for the current input, for the hint row.
    pub fn completion_hints(&self) -> Vec<String> {
        if self.state.ui.input_mode != InputMode::Command {
            return Vec::new();
        }
        complete(&self.state.ui.input, &self.state.knowledge)
    }

    /// The argument hint for the command being typed, if it is a real one.
    pub fn argument_hint(&self) -> Option<&'static str> {
        let parsed = parse_slash_input(&self.state.ui.input)?;
        lookup(&parsed.name)?.argument_hint
    }
}

fn health(flag: Option<bool>, yes: &str, no: &str) -> String {
    match flag {
        Some(true) => yes.to_string(),
        Some(false) => no.to_string(),
        None => "unknown".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use cairn_state::ui::Panel;

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn shift(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::SHIFT)
    }

    // --- the argv contract ports faithfully ---------------------------

    #[test]
    fn each_command_builds_the_exact_argv_the_typescript_connection_built() {
        let mut app = App::for_test();

        app.dispatch("plan", "");
        assert_eq!(app.last_spawn.clone().unwrap(), ["plan", "--output", "json"]);

        app.state.session.running = false;
        app.dispatch("run", "--all");
        assert_eq!(app.last_spawn.clone().unwrap(), ["run", "--all"]);

        app.state.session.running = false;
        app.dispatch("run", "");
        assert_eq!(app.last_spawn.clone().unwrap(), ["run", "--all"], "bare /run means --all");

        app.state.session.running = false;
        app.dispatch("run", "features");
        assert_eq!(app.last_spawn.clone().unwrap(), ["run", "features"]);

        app.state.session.running = false;
        app.dispatch("explain", "art-123");
        assert_eq!(
            app.last_spawn.clone().unwrap(),
            ["explain", "art-123", "--output", "json"]
        );

        app.state.session.running = false;
        app.dispatch("memory", "cuda out of memory");
        assert_eq!(
            app.last_spawn.clone().unwrap(),
            ["memory", "search", "cuda out of memory"],
            "the whole query is one argv element, not split on spaces"
        );

        app.state.session.running = false;
        app.dispatch("memory", "why-blocked");
        assert_eq!(
            app.last_spawn.clone().unwrap(),
            ["memory", "why-blocked", "--output", "json"]
        );

        app.state.session.running = false;
        app.dispatch("doctor", "");
        assert_eq!(app.last_spawn.clone().unwrap(), ["doctor"]);
    }

    #[test]
    fn an_unknown_stage_is_refused_rather_than_passed_to_the_cli() {
        let mut app = App::for_test();
        app.dispatch("run", "not_a_stage");
        assert!(app.last_spawn.is_none(), "a bad stage must never reach the CLI");
        assert!(!app.state.session.running);
        let last = app.state.transcript.entries.last().unwrap();
        assert_eq!(last.kind, TranscriptKind::Error);
        assert!(last.text.contains("unknown stage"));
    }

    #[test]
    fn local_commands_never_spawn_a_subprocess() {
        for name in ["model", "status", "usage", "settings", "help", "clear", "theme"] {
            let mut app = App::for_test();
            app.dispatch(name, if name == "theme" { "mono" } else { "" });
            assert!(app.last_spawn.is_none(), "/{name} must stay local");
        }
    }

    #[test]
    fn status_never_claims_health_it_has_not_measured() {
        let mut app = App::for_test();
        app.dispatch("status", "");
        let text: String =
            app.state.transcript.entries.iter().map(|e| e.text.clone()).collect::<Vec<_>>().join("\n");
        assert!(text.contains("unknown — run /doctor"));
        assert!(!text.contains("healthy"), "no doctor run means no health claim");
    }

    #[test]
    fn a_second_command_is_refused_while_one_is_in_flight() {
        let mut app = App::for_test();
        app.dispatch("doctor", "");
        assert!(app.state.session.running);
        app.last_spawn = None;
        app.dispatch("plan", "");
        assert!(app.last_spawn.is_none(), "one child at a time");
        assert!(app.state.ui.status_message.is_some());
    }

    // --- keys ----------------------------------------------------------

    #[test]
    fn number_keys_and_tab_move_panel_focus() {
        let mut app = App::for_test();
        app.handle_key(key(KeyCode::Char('3')));
        assert_eq!(app.state.ui.focus, Panel::Ledger);
        app.handle_key(key(KeyCode::Tab));
        assert_eq!(app.state.ui.focus, Panel::Memory);
        app.handle_key(shift(KeyCode::BackTab));
        assert_eq!(app.state.ui.focus, Panel::Ledger);
        app.handle_key(key(KeyCode::Char('5')));
        assert_eq!(app.state.ui.focus, Panel::Transcript);
    }

    #[test]
    fn key_release_events_are_ignored_so_nothing_runs_twice_on_windows() {
        let mut app = App::for_test();
        let mut release = key(KeyCode::Char('d'));
        release.kind = KeyEventKind::Release;
        app.handle_key(release);
        assert!(app.last_spawn.is_none(), "a key release must not launch a command");
        app.handle_key(key(KeyCode::Char('d')));
        assert_eq!(app.last_spawn.clone().unwrap(), ["doctor"]);
    }

    #[test]
    fn shortcut_keys_map_to_the_commands_the_footer_advertises() {
        let mut app = App::for_test();
        app.handle_key(key(KeyCode::Char('p')));
        assert_eq!(app.last_spawn.clone().unwrap(), ["plan", "--output", "json"]);

        let mut app = App::for_test();
        app.handle_key(key(KeyCode::Char('d')));
        assert_eq!(app.last_spawn.clone().unwrap(), ["doctor"]);

        let mut app = App::for_test();
        app.handle_key(shift(KeyCode::Char('R')));
        assert_eq!(app.last_spawn.clone().unwrap(), ["run", "--all"]);

        // `r` runs whichever stage the pipeline panel has selected.
        let mut app = App::for_test();
        app.state.ui.focus = Panel::Pipeline;
        app.state.ui.set_selected(Panel::Pipeline, 2);
        app.handle_key(key(KeyCode::Char('r')));
        assert_eq!(app.last_spawn.clone().unwrap(), ["run", "features"]);
    }

    #[test]
    fn m_focuses_memory_and_opens_a_search_prompt_that_spawns_a_real_search() {
        let mut app = App::for_test();
        app.handle_key(key(KeyCode::Char('m')));
        assert_eq!(app.state.ui.focus, Panel::Memory);
        assert_eq!(app.state.ui.input_mode, InputMode::MemorySearch);
        for ch in "oom".chars() {
            app.handle_key(key(KeyCode::Char(ch)));
        }
        app.handle_key(key(KeyCode::Enter));
        assert_eq!(app.last_spawn.clone().unwrap(), ["memory", "search", "oom"]);
        assert_eq!(app.state.ui.input_mode, InputMode::Normal);
        // A memory search typed at the prompt is not a slash command and
        // must not pollute the command history.
        assert!(app.state.ui.history.is_empty());
    }

    #[test]
    fn slash_mode_completes_with_tab_and_runs_on_enter() {
        let mut app = App::for_test();
        app.handle_key(key(KeyCode::Char('/')));
        assert_eq!(app.state.ui.input_mode, InputMode::Command);
        assert_eq!(app.state.ui.input, "/");
        for ch in "doc".chars() {
            app.handle_key(key(KeyCode::Char(ch)));
        }
        app.handle_key(key(KeyCode::Tab));
        assert_eq!(app.state.ui.input, "/doctor");
        app.handle_key(key(KeyCode::Enter));
        assert_eq!(app.last_spawn.clone().unwrap(), ["doctor"]);
        assert_eq!(app.state.ui.history, vec!["/doctor"]);
    }

    #[test]
    fn tab_cycles_through_several_completion_candidates() {
        let mut app = App::for_test();
        app.state.ui.begin_input(InputMode::Command, "/run ");
        app.handle_key(key(KeyCode::Tab));
        let first = app.state.ui.input.clone();
        app.handle_key(key(KeyCode::Tab));
        let second = app.state.ui.input.clone();
        assert_ne!(first, second, "Tab must cycle, not lock onto one candidate");
        assert!(first.starts_with("/run "));

        // Six candidates (five stages plus --all), so cycling wraps back.
        let total = app.state.ui.completions.len();
        assert_eq!(total, 6);
        for _ in 2..total {
            app.handle_key(key(KeyCode::Tab));
        }
        app.handle_key(key(KeyCode::Tab));
        assert_eq!(app.state.ui.input, first, "the cycle wraps to the start");

        // Typing after a cycle restarts completion from the new text.
        app.state.ui.begin_input(InputMode::Command, "/doc");
        app.handle_key(key(KeyCode::Tab));
        assert_eq!(app.state.ui.input, "/doctor");
    }

    #[test]
    fn up_arrow_recalls_command_history() {
        let mut app = App::for_test();
        app.state.ui.history = vec!["/plan".into(), "/doctor".into()];
        app.state.ui.begin_input(InputMode::Command, "/");
        app.handle_key(key(KeyCode::Up));
        assert_eq!(app.state.ui.input, "/doctor");
        app.handle_key(key(KeyCode::Up));
        assert_eq!(app.state.ui.input, "/plan");
        app.handle_key(key(KeyCode::Down));
        assert_eq!(app.state.ui.input, "/doctor");
    }

    #[test]
    fn free_text_is_refused_rather_than_guessed_at() {
        let mut app = App::for_test();
        app.state.ui.begin_input(InputMode::Command, "");
        for ch in "why did the eval stage regress".chars() {
            app.handle_key(key(KeyCode::Char(ch)));
        }
        app.handle_key(key(KeyCode::Enter));
        assert!(app.last_spawn.is_none(), "no LLM in this process — do not guess");
        assert!(app
            .state
            .transcript
            .entries
            .last()
            .unwrap()
            .text
            .contains("only act on the slash commands"));

        // The two fixed patterns still work.
        let mut app = App::for_test();
        app.state.ui.begin_input(InputMode::Command, "run everything");
        app.handle_key(key(KeyCode::Enter));
        assert_eq!(app.last_spawn.clone().unwrap(), ["run", "--all"]);
    }

    #[test]
    fn enter_drills_into_the_selection_and_says_so_when_there_is_nothing_to_drill_into() {
        let mut app = App::for_test();
        app.state.ui.focus = Panel::Pipeline;
        app.handle_key(key(KeyCode::Enter));
        assert!(app.last_spawn.is_none());
        assert!(app.state.ui.status_message.clone().unwrap().contains("nothing to explain"));

        // Give the selected node a real artifact id and try again.
        app.state.pipeline.node_mut("env").unwrap().artifact_id = Some("art-env".into());
        app.state.ui.set_selected(Panel::Pipeline, 0);
        app.handle_key(key(KeyCode::Char('e')));
        assert_eq!(app.last_spawn.clone().unwrap(), ["explain", "art-env", "--output", "json"]);
    }

    #[test]
    fn enter_on_the_memory_panel_asks_why_blocked() {
        let mut app = App::for_test();
        app.state.ui.focus = Panel::Memory;
        app.handle_key(key(KeyCode::Enter));
        assert_eq!(app.last_spawn.clone().unwrap(), ["memory", "why-blocked", "--output", "json"]);
    }

    #[test]
    fn zoom_and_help_and_escape_unwind_in_the_specified_order() {
        let mut app = App::for_test();
        app.handle_key(key(KeyCode::Char('z')));
        assert!(app.state.ui.zoomed);
        app.handle_key(key(KeyCode::Char('?')));
        assert!(app.state.ui.show_help);
        app.handle_key(key(KeyCode::Esc));
        assert!(!app.state.ui.show_help);
        assert!(app.state.ui.zoomed, "the modal closes before the zoom");
        app.handle_key(key(KeyCode::Esc));
        assert!(!app.state.ui.zoomed);
        app.handle_key(key(KeyCode::Char('+')));
        assert!(app.state.ui.zoomed, "+ zooms as well as z");
    }

    #[test]
    fn t_cycles_the_theme_through_all_three_palettes() {
        let mut app = App::for_test();
        assert_eq!(app.state.ui.theme, ThemeName::CairnDark);
        app.handle_key(key(KeyCode::Char('t')));
        assert_eq!(app.state.ui.theme, ThemeName::CairnLight);
        assert_eq!(app.theme.name, ThemeName::CairnLight);
        app.handle_key(key(KeyCode::Char('t')));
        assert_eq!(app.state.ui.theme, ThemeName::Mono);
        assert!(!app.theme.color_enabled, "mono is colour-off, not a grey palette");
        app.handle_key(key(KeyCode::Char('t')));
        assert_eq!(app.state.ui.theme, ThemeName::CairnDark);
    }

    #[test]
    fn quitting_mid_run_asks_first() {
        let mut app = App::for_test();
        app.handle_key(key(KeyCode::Char('q')));
        assert!(app.should_quit(), "idle quit is immediate");

        let mut app = App::for_test();
        app.dispatch("run", "--all");
        assert!(app.state.session.running);
        app.handle_key(key(KeyCode::Char('q')));
        assert!(!app.should_quit());
        assert_eq!(app.state.ui.input_mode, InputMode::ConfirmQuit);
        app.handle_key(key(KeyCode::Char('n')));
        assert!(!app.should_quit());
        assert_eq!(app.state.ui.input_mode, InputMode::Normal);

        app.handle_key(key(KeyCode::Char('q')));
        app.handle_key(key(KeyCode::Char('y')));
        assert!(app.should_quit());
    }

    #[test]
    fn ctrl_c_quits_from_inside_the_command_line_too() {
        let mut app = App::for_test();
        app.state.ui.begin_input(InputMode::Command, "/pl");
        app.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
        assert!(app.should_quit());
    }

    #[test]
    fn clear_empties_the_log_without_touching_the_structured_panels() {
        let mut app = App::for_test();
        app.dispatch("status", "");
        assert!(!app.state.transcript.is_empty());
        app.state.pipeline.node_mut("env").unwrap().artifact_id = Some("art-env".into());
        app.dispatch("clear", "");
        assert!(app.state.transcript.is_empty());
        assert_eq!(
            app.state.pipeline.node("env").unwrap().artifact_id.as_deref(),
            Some("art-env"),
            "/clear empties the log, not the state the panels show"
        );
    }

    #[test]
    fn backend_messages_reach_the_reducer_and_a_failure_surfaces_stderr() {
        let mut app = App::for_test();
        app.dispatch("doctor", "");
        app.tx
            .send(BackendMsg::StderrLine("psycopg.OperationalError: timeout".into()))
            .unwrap();
        app.tx.send(BackendMsg::Exited { label: "cairn doctor".into(), code: 1 }).unwrap();
        assert!(app.drain_backend());
        assert!(!app.state.session.running);
        let message = app.state.ui.status_message.clone().unwrap();
        assert!(message.contains("exited 1"));
        assert!(message.contains("OperationalError"));
    }

    /// End-to-end against a real cluster: spawns a genuine
    /// `<CAIRN_PYTHON> -m cairn.cli doctor`, tails the NDJSON file it
    /// writes, and asserts the reducer received a real `doctor.completed`.
    ///
    /// Ignored by default because it needs `CAIRN_DATABASE_URL` pointing at
    /// a live CockroachDB and a Python environment with `cairn` importable.
    /// Run it with:
    ///
    /// ```text
    /// cargo test -p cairn-tui --bin cairn-tui -- --ignored --nocapture
    /// ```
    #[test]
    #[ignore = "needs a live CockroachDB cluster and a cairn-importable Python"]
    fn live_doctor_round_trips_through_the_real_backend() {
        let mut app = App::new(ThemeName::CairnDark);
        app.dispatch("doctor", "");
        assert!(app.state.session.running);

        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(120);
        while std::time::Instant::now() < deadline {
            app.drain_backend();
            if !app.state.session.running {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        app.drain_backend();

        assert!(!app.state.session.running, "the child never exited within 120s");
        assert_eq!(app.state.session.last_exit_code, Some(0), "cairn doctor failed");
        assert!(
            app.state.doctor.gating_ok.is_some(),
            "no doctor.completed event reached the reducer — stderr: {:?}",
            app.state.session.stderr_tail
        );
        assert_eq!(
            app.state.doctor.database_ok,
            Some(true),
            "the live cluster reported unhealthy: {:?}",
            app.state.doctor.database_detail
        );
        println!("database: {:?}", app.state.doctor.database_detail);
        println!("schema:   {:?}", app.state.doctor.schema_detail);
    }

    #[test]
    fn a_spawn_failure_is_reported_and_does_not_wedge_the_running_flag() {
        let mut app = App::for_test();
        app.dispatch("doctor", "");
        app.tx
            .send(BackendMsg::SpawnFailed {
                label: "cairn doctor".into(),
                error: "python: not found".into(),
            })
            .unwrap();
        app.drain_backend();
        assert!(!app.state.session.running, "a failed spawn must not block the next command");
        assert!(app.state.ui.status_message.clone().unwrap().contains("python: not found"));
    }
}
