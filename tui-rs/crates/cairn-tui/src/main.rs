//! `cairn-tui` — the interactive terminal a bare `cairn` launches.
//!
//! Replaces the Node/pi-tui TUI at `tui/`. The old one was a single
//! vertically-scrolling transcript, so the pipeline, the live claim race
//! and the decision ledger were only ever visible for the instant they
//! were printed. This one keeps all five domains on screen as persistent
//! panels, lazygit-style, always showing current state.
//!
//! Deliberately absent: any equivalent of the TypeScript
//! `causal-field.ts` startup animation. It was ~1.9 seconds of unskippable
//! splash before the operator could do anything. This binary launches
//! straight into the layout.
//!
//! Threading is three plain OS threads per command plus this render loop,
//! with a `crossbeam-channel` between them — no async runtime, matching
//! how `gitui` does the same job.

mod app;
mod draw;
mod theme;

use std::io::{self, Stdout};
use std::panic;
use std::time::Duration;

use cairn_state::ui::ThemeName;
use ratatui::backend::CrosstermBackend;
use ratatui::crossterm::event::{self, Event};
use ratatui::crossterm::execute;
use ratatui::crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::Terminal;

use app::App;

/// How long to block waiting for a key before looping. Also the redraw
/// cadence for the lease countdown, which has to keep ticking even when
/// nothing arrives from the backend and nobody touches the keyboard.
const TICK: Duration = Duration::from_millis(100);

type Backend = CrosstermBackend<Stdout>;

fn main() {
    let theme_name = std::env::var("CAIRN_THEME")
        .ok()
        .and_then(|raw| ThemeName::parse(&raw))
        .unwrap_or(ThemeName::CairnDark);

    if let Err(error) = run(theme_name) {
        // The restore in `run` already ran, or the panic hook did it, so
        // this reaches a sane terminal.
        eprintln!("cairn-tui: {error}");
        std::process::exit(1);
    }
}

fn run(theme_name: ThemeName) -> io::Result<()> {
    let mut terminal = setup()?;
    install_panic_hook();
    let result = event_loop(&mut terminal, theme_name);
    restore(&mut terminal)?;
    result
}

fn setup() -> io::Result<Terminal<Backend>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    Terminal::new(CrosstermBackend::new(stdout))
}

fn restore(terminal: &mut Terminal<Backend>) -> io::Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()
}

/// Without this, a panic inside the render loop leaves the terminal in raw
/// mode on the alternate screen — the user's shell comes back with no echo
/// and no visible prompt, and the backtrace is invisible.
fn install_panic_hook() {
    let previous = panic::take_hook();
    panic::set_hook(Box::new(move |info| {
        let _ = disable_raw_mode();
        let _ = execute!(io::stdout(), LeaveAlternateScreen);
        previous(info);
    }));
}

fn event_loop(terminal: &mut Terminal<Backend>, theme_name: ThemeName) -> io::Result<()> {
    let mut app = App::new(theme_name);
    let mut dirty = true;

    while !app.should_quit() {
        app.tick();
        if app.drain_backend() {
            dirty = true;
        }
        if dirty {
            terminal.draw(|frame| draw::draw(frame, &mut app))?;
            dirty = false;
        }

        if event::poll(TICK)? {
            match event::read()? {
                Event::Key(key) => {
                    app.handle_key(key);
                    dirty = true;
                }
                Event::Resize(_, _) => dirty = true,
                // Mouse and paste are not subscribed to; anything else that
                // arrives is a focus change, which does not alter state.
                _ => {}
            }
        } else {
            // The countdown bars advance with wall time, so a quiet tick is
            // still a reason to redraw.
            dirty = !app.state.claims.active.is_empty()
                || app.state.pipeline.current_stage.is_some();
        }
    }
    Ok(())
}
