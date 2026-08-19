//! Layout and painting.
//!
//! This is the actual fix the rewrite exists for. The TypeScript TUI was a
//! single vertically-scrolling transcript: every panel it drew was a
//! `ToolPanel` appended to the bottom, so the pipeline's shape, the live
//! claim race and the decision ledger were all visible only at the instant
//! they were printed and then scrolled away forever. You could see the last
//! thing that happened; you could never see the current state.
//!
//! Here the five domains are persistent fixed regions, lazygit-style, all
//! on screen at once and all always current. Nothing below reformats state
//! into padded text lines the way `domain-panels.ts` had to — every panel
//! reads typed fields off `AppState` and lays them out itself.

use cairn_state::claims::{ClaimPhase, ClaimRace};
use cairn_state::ledger::DecisionEntry;
use cairn_state::memory::{MatchStrength, MemoryMatch};
use cairn_state::pipeline::{PipelineNode, StageStatus};
use cairn_state::transcript::TranscriptKind;
use cairn_state::ui::{InputMode, Panel, PANELS};
use cairn_state::{short, AppState};
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;

use crate::app::App;
use crate::theme::Theme;

/// Below either of these the six-region layout stops being readable and
/// collapses to one panel plus a numbered tab bar. Chosen from the layout's
/// own arithmetic, not guessed: the full layout needs one status row, one
/// command row, one footer row, and three panel rows that each need a border
/// pair plus at least two content rows; and the pipeline's five side-by-side
/// cards stop fitting their labels under roughly 80 columns.
pub const MIN_ROWS: u16 = 24;
pub const MIN_COLS: u16 = 80;

pub fn draw(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    let theme = app.theme;
    frame.render_widget(Block::default().style(theme.base()), area);

    let escalated = app.state.escalation.is_some();
    let constraints = if escalated {
        vec![
            Constraint::Length(1), // status bar
            Constraint::Length(1), // escalation banner
            Constraint::Min(3),    // panels
            Constraint::Length(1), // command line
            Constraint::Length(1), // footer
        ]
    } else {
        vec![
            Constraint::Length(1),
            Constraint::Min(3),
            Constraint::Length(1),
            Constraint::Length(1),
        ]
    };
    let rows = Layout::default().direction(Direction::Vertical).constraints(constraints).split(area);

    let (banner, body, command, footer) = if escalated {
        (Some(rows[1]), rows[2], rows[3], rows[4])
    } else {
        (None, rows[1], rows[2], rows[3])
    };

    draw_status_bar(frame, rows[0], app);
    if let Some(banner) = banner {
        draw_escalation_banner(frame, banner, app);
    }

    let cramped = area.height < MIN_ROWS || area.width < MIN_COLS;
    if app.state.ui.zoomed {
        draw_panel(frame, body, app, app.state.ui.focus, true);
    } else if cramped {
        draw_cramped(frame, body, app);
    } else {
        draw_full(frame, body, app);
    }

    draw_command_line(frame, command, app);
    draw_footer(frame, footer, app);

    if app.state.ui.show_explain {
        draw_explain_modal(frame, area, app);
    }
    if app.state.ui.show_help {
        draw_help_modal(frame, area, app);
    }
    if app.state.ui.input_mode == InputMode::ConfirmQuit {
        draw_quit_modal(frame, area, &theme);
    }
}

/// The spec layout: pipeline across the top, then two rows of a wider left
/// panel beside a narrower right one.
fn draw_full(frame: &mut Frame, area: Rect, app: &mut App) {
    // ~35% is the ceiling, not the fixed size: a freshly-planned pipeline
    // has three lines per card and would otherwise sit in a tall band of
    // empty rows while the panels below it are cramped. The band grows on
    // its own as stages acquire probes, runtimes and artifact ids.
    let tallest = app
        .state
        .pipeline
        .nodes
        .iter()
        // Measured from the card the panel will actually draw, so the two
        // can never disagree about how tall a stage is.
        .map(|node| pipeline_card(node, &app.theme, app.state.now_ms(), false, 24).len() as u16)
        .max()
        .unwrap_or(3);
    let ceiling = (area.height as u32 * 35 / 100) as u16;
    let pipeline_height = (tallest + 2).clamp(5, ceiling.max(5));

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(pipeline_height),
            Constraint::Percentage(50),
            Constraint::Percentage(50),
        ])
        .split(area);

    let split = |area: Rect| {
        Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Percentage(55), Constraint::Percentage(45)])
            .split(area)
    };

    draw_panel(frame, rows[0], app, Panel::Pipeline, false);
    let middle = split(rows[1]);
    draw_panel(frame, middle[0], app, Panel::Claims, false);
    draw_panel(frame, middle[1], app, Panel::Ledger, false);
    let bottom = split(rows[2]);
    draw_panel(frame, bottom[0], app, Panel::Memory, false);
    draw_panel(frame, bottom[1], app, Panel::Transcript, false);
}

/// Small-terminal degradation: identical `AppState`, different `Layout`.
/// One focused panel plus a numbered tab bar, so `1`-`5` and `Tab` still
/// reach everything — nothing becomes unreachable, it just becomes serial.
fn draw_cramped(frame: &mut Frame, area: Rect, app: &mut App) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Min(2)])
        .split(area);

    let theme = app.theme;
    let mut spans = Vec::new();
    for (index, panel) in PANELS.iter().enumerate() {
        let focused = *panel == app.state.ui.focus;
        let label = format!(" {}:{} ", index + 1, panel.title());
        spans.push(Span::styled(
            label,
            if focused { theme.selection() } else { theme.muted() },
        ));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)), rows[0]);
    draw_panel(frame, rows[1], app, app.state.ui.focus, false);
}

fn draw_panel(frame: &mut Frame, area: Rect, app: &mut App, panel: Panel, zoomed: bool) {
    let focused = app.state.ui.focus == panel;
    let theme = app.theme;
    let count = panel_count(&app.state, panel);
    let mut title = format!(" {} {} ", panel.index() + 1, panel.title());
    if count > 0 {
        title.push_str(&format!("({count}) "));
    }
    if zoomed {
        title.push_str("[zoomed] ");
    }
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(if focused { BorderType::Thick } else { BorderType::Plain })
        .border_style(theme.border(focused))
        .title(Span::styled(
            title,
            if focused { theme.fg(theme.colors.gold).add_modifier(Modifier::BOLD) } else { theme.muted() },
        ));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }

    match panel {
        Panel::Pipeline => draw_pipeline(frame, inner, app),
        Panel::Claims => draw_claims(frame, inner, app),
        Panel::Ledger => draw_ledger(frame, inner, app),
        Panel::Memory => draw_memory(frame, inner, app),
        Panel::Transcript => draw_transcript(frame, inner, app),
    }
}

fn panel_count(state: &AppState, panel: Panel) -> usize {
    match panel {
        Panel::Pipeline => state.pipeline.nodes.iter().filter(|n| n.status != StageStatus::Unknown).count(),
        Panel::Claims => state.claims.total(),
        Panel::Ledger => state.ledger.entries.len(),
        Panel::Memory => state.memory.len(),
        Panel::Transcript => state.transcript.len(),
    }
}

// --- status bar, banner, command line, footer -------------------------

fn draw_status_bar(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let state = &app.state;
    let mut spans = vec![
        Span::styled(" cairn ", theme.fg(theme.colors.gold).add_modifier(Modifier::BOLD)),
        Span::styled("│ ", theme.dim()),
    ];

    let activity_style = if state.session.running {
        theme.fg(theme.colors.cyan).add_modifier(Modifier::BOLD)
    } else {
        theme.muted()
    };
    spans.push(Span::styled(state.activity.label.clone(), activity_style));

    let done = state.pipeline.nodes.iter().filter(|n| n.status == StageStatus::Succeeded).count();
    let planned = state
        .pipeline
        .nodes
        .iter()
        .filter(|n| n.status != StageStatus::Unknown)
        .count();
    if planned > 0 {
        spans.push(Span::styled(" │ ", theme.dim()));
        spans.push(Span::styled(format!("{done}/{planned} stages"), theme.fg(theme.colors.bone)));
    }
    if let Some(run_id) = &state.session.run_id {
        spans.push(Span::styled(" │ run ", theme.dim()));
        spans.push(Span::styled(short(run_id, 8), theme.fg(theme.colors.violet)));
    }
    if let Some(owner) = &state.session.owner {
        spans.push(Span::styled(" │ ", theme.dim()));
        spans.push(Span::styled(owner.clone(), theme.fg(theme.colors.stone)));
        if let Some(region) = &state.session.region {
            spans.push(Span::styled(format!(" @ {region}"), theme.dim()));
        }
    }
    let active_races = state.claims.active.len();
    if active_races > 0 {
        spans.push(Span::styled(" │ ", theme.dim()));
        spans.push(Span::styled(
            format!("{active_races} live claim{}", if active_races == 1 { "" } else { "s" }),
            theme.fg(theme.colors.warning),
        ));
    }
    if let Some(message) = &state.ui.status_message {
        spans.push(Span::styled(" │ ", theme.dim()));
        spans.push(Span::styled(message.clone(), theme.fg(theme.colors.error)));
    }

    let bar = if theme.color_enabled {
        Paragraph::new(Line::from(spans)).style(theme.bg(theme.colors.panel))
    } else {
        Paragraph::new(Line::from(spans))
    };
    frame.render_widget(bar, area);
}

/// Only drawn when the backend really did refuse to spend without a human.
/// The figures are the ones `agent/loop.py::estimated_cost_usd` produced.
fn draw_escalation_banner(frame: &mut Frame, area: Rect, app: &App) {
    let Some(escalation) = &app.state.escalation else { return };
    let theme = &app.theme;
    let text = format!(
        " ESCALATED · {} · projected {} exceeds approval {} · {} ",
        escalation.stage.clone().unwrap_or_else(|| "-".into()),
        cairn_state::fmt_usd(escalation.projected_cost_usd),
        cairn_state::fmt_usd(escalation.approval_usd),
        escalation.detail.clone().unwrap_or_else(|| "needs a human".into())
    );
    let style = if theme.color_enabled {
        Style::default().fg(theme.colors.background).bg(theme.colors.warning).add_modifier(Modifier::BOLD)
    } else {
        Style::default().add_modifier(Modifier::REVERSED)
    };
    frame.render_widget(Paragraph::new(Line::from(Span::styled(text, style))), area);
}

fn draw_command_line(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let ui = &app.state.ui;
    match ui.input_mode {
        InputMode::Command | InputMode::MemorySearch => {
            let prompt = if ui.input_mode == InputMode::MemorySearch {
                "memory search> "
            } else {
                ""
            };
            let mut spans = vec![
                Span::styled(prompt, theme.fg(theme.colors.violet).add_modifier(Modifier::BOLD)),
                Span::styled(ui.input.clone(), theme.fg(theme.colors.bone)),
                Span::styled("▏", theme.fg(theme.colors.gold)),
            ];
            if let Some(hint) = app.argument_hint() {
                if !ui.input.contains(' ') {
                    spans.push(Span::styled(format!(" {hint}"), theme.dim()));
                }
            }
            let hints = app.completion_hints();
            if hints.len() > 1 {
                spans.push(Span::styled(
                    format!("   {} ", hints.join(" ")),
                    theme.muted(),
                ));
            }
            frame.render_widget(Paragraph::new(Line::from(spans)), area);
            // A real caret, so the terminal's own cursor sits where typing
            // will land instead of the drawn "▏" having to stand in for it.
            let caret = prompt.chars().count() + ui.input[..ui.cursor].chars().count();
            frame.set_cursor_position((area.x + caret as u16, area.y));
        }
        _ => {
            let spans = vec![
                Span::styled(" / ", theme.fg(theme.colors.gold)),
                Span::styled("command  ", theme.muted()),
                Span::styled("? ", theme.fg(theme.colors.gold)),
                Span::styled("help  ", theme.muted()),
                Span::styled("m ", theme.fg(theme.colors.gold)),
                Span::styled("memory search  ", theme.muted()),
                Span::styled("q ", theme.fg(theme.colors.gold)),
                Span::styled("quit", theme.muted()),
            ];
            frame.render_widget(Paragraph::new(Line::from(spans)), area);
        }
    }
}

fn draw_footer(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let spans = vec![
        Span::styled(format!(" {} ", app.state.ui.focus.title()), theme.fg(theme.colors.gold)),
        Span::styled(app.state.ui.focus.hints(), theme.muted()),
        Span::styled("  │  ", theme.dim()),
        Span::styled("1-5/tab panel · z zoom · t theme · ? keys", theme.dim()),
    ];
    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

// --- the pipeline DAG -------------------------------------------------

/// Five stage cards side by side with real arrows between them, each card
/// carrying its own verdict/class/probe/runtime. This is the panel that
/// is *always current*: a stage that finished twenty events ago still
/// shows its verdict here.
fn draw_pipeline(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let state = &app.state;
    let selected = state.ui.selected(Panel::Pipeline);
    let focused = state.ui.focus == Panel::Pipeline;

    // Under roughly 12 columns per card the labels stop fitting, so fall
    // back to one stage per row rather than truncating everything to noise.
    let per_card = area.width / state.pipeline.nodes.len() as u16;
    if per_card < 12 {
        let items: Vec<Vec<Line>> = state
            .pipeline
            .nodes
            .iter()
            .map(|node| vec![pipeline_summary_line(node, theme, state.now_ms())])
            .collect();
        render_items(frame, area, theme, items, selected, focused);
        return;
    }

    let node_count = state.pipeline.nodes.len();
    let mut constraints = Vec::new();
    for index in 0..node_count {
        constraints.push(Constraint::Min(10));
        if index + 1 < node_count {
            constraints.push(Constraint::Length(3));
        }
    }
    let columns = Layout::default()
        .direction(Direction::Horizontal)
        .constraints(constraints)
        .split(area);

    for (index, node) in state.pipeline.nodes.iter().enumerate() {
        let cell = columns[index * 2];
        let is_selected = focused && index == selected;
        // The card's fill carries the stage's outcome even before you read
        // a word of it — the `panel*` roles the palette defines for exactly
        // this, which the TS TUI could only apply to a transcript block.
        let fill = if is_selected {
            theme.selection()
        } else {
            match node.status {
                StageStatus::Succeeded => theme.bg(theme.colors.panel_success),
                StageStatus::Failed => theme.bg(theme.colors.panel_error),
                StageStatus::Running | StageStatus::Planned => {
                    theme.bg(theme.colors.panel_pending)
                }
                StageStatus::Unknown => theme.bg(theme.colors.panel),
            }
        };
        frame.render_widget(
            Paragraph::new(pipeline_card(node, theme, state.now_ms(), is_selected, cell.width as usize)).style(fill),
            cell,
        );
        if index + 1 < node_count {
            let arrow = columns[index * 2 + 1];
            let reached = node.status == StageStatus::Succeeded;
            frame.render_widget(
                Paragraph::new(Line::from(Span::styled(
                    "→",
                    if reached { theme.fg(theme.colors.green) } else { theme.dim() },
                )))
                .alignment(Alignment::Center),
                arrow,
            );
        }
    }
}

fn status_style(status: StageStatus, theme: &Theme) -> Style {
    match status {
        StageStatus::Unknown => theme.dim(),
        StageStatus::Planned => theme.fg(theme.colors.stone),
        StageStatus::Running => theme.fg(theme.colors.cyan).add_modifier(Modifier::BOLD),
        StageStatus::Succeeded => theme.fg(theme.colors.green),
        StageStatus::Failed => theme.fg(theme.colors.error).add_modifier(Modifier::BOLD),
    }
}

fn pipeline_card<'a>(
    node: &PipelineNode,
    theme: &Theme,
    now_ms: u64,
    selected: bool,
    width: usize,
) -> Vec<Line<'a>> {
    let mut lines = Vec::new();
    let marker = if selected { "▸" } else { " " };
    lines.push(Line::from(vec![
        Span::styled(marker.to_string(), theme.fg(theme.colors.gold)),
        Span::styled(node.status.glyph().to_string(), status_style(node.status, theme)),
        Span::styled(format!(" {}", node.stage), theme.bold()),
    ]));
    lines.push(Line::from(Span::styled(
        format!("  {}", node.status.label()),
        status_style(node.status, theme),
    )));

    if let Some(action) = &node.action {
        lines.push(Line::from(Span::styled(
            format!("  {action}"),
            theme.fg(theme.colors.violet),
        )));
    }
    if let Some(class) = &node.change_class {
        lines.push(Line::from(Span::styled(format!("  {class}"), theme.muted())));
    }
    if node.structurally_sound == Some(false) {
        lines.push(Line::from(Span::styled("  unsound", theme.fg(theme.colors.warning))));
    }
    if let Some(probe) = &node.probe {
        let head = if probe.passed { "probe ok" } else { "probe FAIL" };
        lines.push(Line::from(Span::styled(
            format!("  {head}"),
            if probe.passed {
                theme.fg(theme.colors.green)
            } else {
                theme.fg(theme.colors.error)
            },
        )));
        if let Some(coverage) = probe.coverage() {
            lines.push(Line::from(vec![
                Span::styled("  ", Style::default()),
                Span::styled(bar(coverage, 6), theme.fg(theme.colors.cyan)),
                Span::styled(format!(" {}%", (coverage * 100.0).round() as i64), theme.muted()),
            ]));
        }
    }
    if let Some(elapsed) = node.elapsed_ms(now_ms) {
        lines.push(Line::from(Span::styled(
            format!("  {}", fmt_ms(elapsed as i64)),
            theme.muted(),
        )));
    }
    if let Some(size) = node.size_bytes {
        lines.push(Line::from(Span::styled(format!("  {}", fmt_bytes(size)), theme.dim())));
    }
    // Truncate to the card's real width rather than a fixed guess, so a
    // wide terminal shows more of the error instead of eliding text that
    // would have fitted. The full text is always in the log panel.
    let room = width.saturating_sub(2).max(8);
    if let Some(error) = &node.error {
        lines.push(Line::from(Span::styled(
            format!("  {}", short(error, room)),
            theme.fg(theme.colors.error),
        )));
    }
    if let Some(id) = &node.artifact_id {
        lines.push(Line::from(Span::styled(format!("  {}", short(id, room)), theme.dim())));
    }
    lines
}

fn pipeline_summary_line<'a>(node: &PipelineNode, theme: &Theme, now_ms: u64) -> Line<'a> {
    let mut spans = vec![
        Span::styled(node.status.glyph().to_string(), status_style(node.status, theme)),
        Span::styled(format!(" {:<11}", node.stage), theme.bold()),
        Span::styled(format!("{:<9}", node.status.label()), status_style(node.status, theme)),
    ];
    if let Some(action) = &node.action {
        spans.push(Span::styled(format!("{action} "), theme.fg(theme.colors.violet)));
    }
    if let Some(elapsed) = node.elapsed_ms(now_ms) {
        spans.push(Span::styled(fmt_ms(elapsed as i64), theme.muted()));
    }
    Line::from(spans)
}

// --- claims -----------------------------------------------------------

/// One card per work_key, with both sides of the race and a live countdown
/// against the real 45s lease / 10s heartbeat from `db/claims.py`.
fn draw_claims(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let state = &app.state;
    if state.claims.total() == 0 {
        render_empty(
            frame,
            area,
            theme,
            "No claims observed yet. Run a stage and this fills with live races.",
        );
        return;
    }
    let width = area.width.saturating_sub(2) as usize;
    let items: Vec<Vec<Line>> = state
        .claims
        .ordered()
        .map(|race| claim_card(race, theme, state.now_ms(), width))
        .collect();
    render_items(
        frame,
        area,
        theme,
        items,
        state.ui.selected(Panel::Claims),
        state.ui.focus == Panel::Claims,
    );
}

fn phase_style(phase: ClaimPhase, theme: &Theme) -> Style {
    match phase {
        ClaimPhase::Owned => theme.fg(theme.colors.green),
        ClaimPhase::TookOver => theme.fg(theme.colors.gold).add_modifier(Modifier::BOLD),
        ClaimPhase::Contended => theme.fg(theme.colors.warning),
        ClaimPhase::Subscribed => theme.fg(theme.colors.cyan),
        ClaimPhase::LeaseExpired => theme.fg(theme.colors.error).add_modifier(Modifier::BOLD),
        ClaimPhase::Completed => theme.fg(theme.colors.green),
        ClaimPhase::Failed => theme.fg(theme.colors.error),
        ClaimPhase::SubscribeCompleted => theme.fg(theme.colors.cyan),
    }
}

fn claim_card<'a>(race: &ClaimRace, theme: &Theme, now_ms: u64, width: usize) -> Vec<Line<'a>> {
    let mut lines = Vec::new();
    let label = race.stage.clone().unwrap_or_else(|| short(&race.work_key, 18));
    lines.push(Line::from(vec![
        Span::styled(format!("{label}  "), theme.bold()),
        Span::styled(format!("[{}]", race.phase.label()), phase_style(race.phase, theme)),
        Span::styled(format!("  {}", short(&race.work_key, 20)), theme.dim()),
    ]));
    lines.push(Line::from(vec![
        Span::styled("  owner  ", theme.muted()),
        Span::styled(race.owner.describe(), theme.fg(theme.colors.green)),
    ]));
    if let Some(challenger) = &race.challenger {
        let role = if race.phase == ClaimPhase::TookOver { "lost   " } else { "waiting" };
        lines.push(Line::from(vec![
            Span::styled(format!("  {role} "), theme.muted()),
            Span::styled(challenger.describe(), theme.fg(theme.colors.stone)),
        ]));
    }

    match race.lease_remaining_ms(now_ms) {
        Some(remaining) => {
            let fraction = race.lease_fraction(now_ms);
            let bar_width = width.saturating_sub(30).clamp(6, 24);
            let lease_style = if fraction < 0.25 {
                theme.fg(theme.colors.error)
            } else if fraction < 0.5 {
                theme.fg(theme.colors.warning)
            } else {
                theme.fg(theme.colors.green)
            };
            let mut spans = vec![
                Span::styled("  lease  ", theme.muted()),
                Span::styled(bar(fraction, bar_width), lease_style),
                Span::styled(format!(" {}", fmt_ms(remaining as i64)), lease_style),
            ];
            // An inferred deadline must not be drawn as if it were measured:
            // for a claim we did not watch start, 45s is an upper bound.
            if !race.lease_is_exact {
                spans.push(Span::styled(" (≤, not watched from the start)", theme.dim()));
            }
            lines.push(Line::from(spans));

            let mut beat = vec![Span::styled("  beat   ", theme.muted())];
            match race.heartbeat_remaining_ms(now_ms) {
                Some(due) => {
                    let overdue = race.heartbeat_overdue(now_ms);
                    beat.push(Span::styled(
                        format!("next in {}", fmt_ms(due as i64)),
                        if overdue { theme.fg(theme.colors.error) } else { theme.muted() },
                    ));
                    if overdue {
                        beat.push(Span::styled(
                            "  OVERDUE — takeover window",
                            theme.fg(theme.colors.error).add_modifier(Modifier::BOLD),
                        ));
                    }
                }
                None => beat.push(Span::styled("no heartbeat seen yet", theme.dim())),
            }
            beat.push(Span::styled(format!("   {} beats", race.heartbeats), theme.dim()));
            lines.push(Line::from(beat));
        }
        None => {
            let mut spans = vec![Span::styled("  result ", theme.muted())];
            // A failed claim carries no artifact, duration or size, so
            // without this the line renders as a bare "result" and says
            // nothing. What it means is worth a sentence: the key is free
            // again, which is exactly what the next contender acts on.
            if race.phase == ClaimPhase::Failed {
                spans.push(Span::styled(
                    "failed — the work_key is free for the next contender",
                    theme.fg(theme.colors.error),
                ));
            }
            if let Some(state) = &race.terminal_state {
                spans.push(Span::styled(format!("{state} "), phase_style(race.phase, theme)));
            }
            if let Some(id) = &race.artifact_id {
                spans.push(Span::styled(format!("{} ", short(id, 16)), theme.fg(theme.colors.violet)));
            }
            if let Some(duration) = race.duration_ms {
                spans.push(Span::styled(format!("{} ", fmt_ms(duration)), theme.muted()));
            }
            if let Some(size) = race.size_bytes {
                spans.push(Span::styled(fmt_bytes(size), theme.dim()));
            }
            if let Some(waited) = race.waited_s {
                spans.push(Span::styled(format!("  waited {waited:.1}s"), theme.muted()));
            }
            lines.push(Line::from(spans));
        }
    }
    lines.push(Line::from(""));
    lines
}

// --- ledger -----------------------------------------------------------

fn draw_ledger(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let state = &app.state;
    if state.ledger.entries.is_empty() {
        render_empty(frame, area, theme, "No decisions recorded yet.");
        return;
    }
    let items: Vec<Vec<Line>> =
        state.ledger.entries.iter().map(|entry| ledger_card(entry, theme)).collect();
    render_items(
        frame,
        area,
        theme,
        items,
        state.ui.selected(Panel::Ledger),
        state.ui.focus == Panel::Ledger,
    );
}

fn ledger_card<'a>(entry: &DecisionEntry, theme: &Theme) -> Vec<Line<'a>> {
    let mut lines = vec![Line::from(vec![
        Span::styled(
            format!("{:<21}", entry.action.as_str()),
            theme.tone(entry.action.tone()).add_modifier(Modifier::BOLD),
        ),
        Span::styled(format!("{:<11}", entry.stage), theme.fg(theme.colors.bone)),
        Span::styled(
            entry.verdict.clone().unwrap_or_default(),
            if entry.is_refusal() {
                theme.fg(theme.colors.error)
            } else {
                theme.muted()
            },
        ),
    ])];

    let mut meta = vec![
        Span::styled("  by ", theme.dim()),
        // When authorization is absent, authority() names the actual proposer
        // and explicitly labels it as proposal-only.
        Span::styled(entry.authority(), theme.muted()),
    ];
    if let Some(class) = &entry.change_class {
        meta.push(Span::styled(format!("  {class}"), theme.dim()));
    }
    if let Some(latency) = entry.latency_ms {
        meta.push(Span::styled(format!("  {}", fmt_ms(latency)), theme.dim()));
    }
    lines.push(Line::from(meta));

    if let Some(explanation) = &entry.explanation {
        if !explanation.is_empty() {
            lines.push(Line::from(Span::styled(
                format!("  {explanation}"),
                theme.fg(theme.colors.stone),
            )));
        }
    }
    lines.push(Line::from(""));
    lines
}

// --- memory -----------------------------------------------------------

fn draw_memory(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let state = &app.state;

    if !state.memory.searched && state.memory.why_blocked.is_none() {
        render_empty(frame, area, theme, "Press m to search negative memory.");
        return;
    }

    let mut items: Vec<Vec<Line>> = Vec::new();
    if let Some(refusal) = &state.memory.why_blocked {
        items.push(vec![
            Line::from(vec![
                Span::styled("why-blocked  ", theme.fg(theme.colors.error).add_modifier(Modifier::BOLD)),
                Span::styled(refusal.action.as_str().to_string(), theme.tone(refusal.action.tone())),
                Span::styled(format!("  {}", refusal.stage), theme.muted()),
            ]),
            Line::from(Span::styled(
                format!("  {}", refusal.explanation.clone().unwrap_or_default()),
                theme.fg(theme.colors.stone),
            )),
            Line::from(""),
        ]);
    } else if state.memory.why_blocked_checked {
        items.push(vec![
            Line::from(Span::styled("why-blocked  no refusal on record", theme.muted())),
            Line::from(""),
        ]);
    }

    if state.memory.searched {
        let header = format!(
            "\"{}\"  {} match(es), {} verified{}",
            state.memory.query.clone().unwrap_or_default(),
            state.memory.len(),
            state.memory.verified_count(),
            state
                .memory
                .stage_filter
                .clone()
                .map(|s| format!("  stage {s}"))
                .unwrap_or_default()
        );
        items.push(vec![Line::from(Span::styled(header, theme.muted())), Line::from("")]);
    }

    for entry in &state.memory.matches {
        items.push(memory_card(entry, theme));
    }

    if state.memory.searched && state.memory.is_empty() {
        items.push(vec![Line::from(Span::styled(
            "Nothing in memory resembles this — not a guarantee it will work.",
            theme.muted(),
        ))]);
    }

    // The selection index counts matches, so the header cards above are
    // offset out of it.
    let offset = items.len() - state.memory.len();
    let selected = state.ui.selected(Panel::Memory) + offset;
    render_items(frame, area, theme, items, selected, state.ui.focus == Panel::Memory);
}

fn memory_card<'a>(entry: &MemoryMatch, theme: &Theme) -> Vec<Line<'a>> {
    let strength_style = match entry.strength {
        MatchStrength::Verified => theme.fg(theme.colors.green).add_modifier(Modifier::BOLD),
        MatchStrength::Advisory => theme.muted(),
    };
    let mut lines = vec![Line::from(vec![
        Span::styled(format!("{} ", entry.strength.glyph()), strength_style),
        Span::styled(
            entry.error_class.clone().unwrap_or_else(|| "unknown".into()),
            theme.fg(theme.colors.bone).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!("  {}", entry.stage.clone().unwrap_or_default()),
            theme.muted(),
        ),
        Span::styled(
            entry
                .cosine_distance
                .map(|d| format!("   d={d:.3}"))
                .unwrap_or_default(),
            theme.dim(),
        ),
    ])];
    if let Some(summary) = &entry.summary_text {
        lines.push(Line::from(Span::styled(
            format!("  {summary}"),
            theme.fg(theme.colors.stone),
        )));
    }
    // docs/project/PROJECT.md §7.2: a weak match is advisory and must say so — it never
    // blocks anything, and the panel must not let it look like it does.
    let mut tail = vec![Span::styled(
        format!("  {}", entry.strength.label()),
        strength_style,
    )];
    if let Some(wasted) = entry.wasted_ms {
        tail.push(Span::styled(format!("   wasted {}", fmt_ms(wasted)), theme.dim()));
    }
    lines.push(Line::from(tail));
    lines.push(Line::from(""));
    lines
}

// --- transcript -------------------------------------------------------

fn draw_transcript(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let state = &app.state;
    if state.transcript.is_empty() {
        render_empty(frame, area, theme, "Narration and raw events appear here.");
        return;
    }
    let lines: Vec<Line> = state
        .transcript
        .entries
        .iter()
        .map(|entry| {
            let style = match entry.kind {
                TranscriptKind::User => theme.fg(theme.colors.cyan).add_modifier(Modifier::BOLD),
                TranscriptKind::Narration => theme.fg(theme.colors.bone),
                TranscriptKind::Event => theme.fg(theme.colors.stone),
                TranscriptKind::Stderr => theme.fg(theme.colors.warning),
                TranscriptKind::Error => theme.fg(theme.colors.error),
                TranscriptKind::Info => theme.fg(theme.colors.violet),
            };
            let prefix = match entry.kind {
                TranscriptKind::User => "› ",
                TranscriptKind::Error => "✗ ",
                TranscriptKind::Stderr => "‹ ",
                _ => "  ",
            };
            Line::from(vec![
                Span::styled(clock(&entry.timestamp), theme.dim()),
                Span::styled(prefix, style),
                Span::styled(entry.text.clone(), style),
            ])
        })
        .collect();

    // Pinned to the bottom unless the operator scrolled up, so a live run
    // does not keep yanking the view away from something being read.
    let height = area.height as usize;
    let offset = match state.ui.transcript_scroll {
        Some(index) => index.min(lines.len().saturating_sub(1)),
        None => lines.len().saturating_sub(height),
    };
    frame.render_widget(
        Paragraph::new(lines).scroll((offset as u16, 0)),
        area,
    );
}

/// `12:04:31` out of an ISO8601 timestamp, or blank padding when the entry
/// carries no timestamp (a locally-generated line).
fn clock(timestamp: &str) -> String {
    match timestamp.split('T').nth(1) {
        Some(rest) => {
            let hhmmss: String = rest.chars().take(8).collect();
            format!("{hhmmss} ")
        }
        None => "         ".to_string(),
    }
}

// --- modals -----------------------------------------------------------

fn modal_area(area: Rect, width_pct: u16, height_pct: u16) -> Rect {
    let vertical = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Percentage((100 - height_pct) / 2),
            Constraint::Percentage(height_pct),
            Constraint::Percentage((100 - height_pct) / 2),
        ])
        .split(area);
    Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage((100 - width_pct) / 2),
            Constraint::Percentage(width_pct),
            Constraint::Percentage((100 - width_pct) / 2),
        ])
        .split(vertical[1])[1]
}

fn floating<'a>(
    frame: &mut Frame,
    area: Rect,
    theme: &Theme,
    title: &'a str,
    lines: Vec<Line<'a>>,
    width_pct: u16,
    height_pct: u16,
) {
    let region = modal_area(area, width_pct, height_pct);
    // `Clear` first: without it the panels underneath bleed through the
    // gaps between the overlay's own glyphs.
    frame.render_widget(Clear, region);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Double)
        .border_style(theme.fg(theme.colors.gold))
        .title(Span::styled(format!(" {title} "), theme.bold()))
        .style(theme.base());
    let inner = block.inner(region);
    frame.render_widget(block, region);
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

fn draw_help_modal(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let key = |k: &str, what: &str| {
        Line::from(vec![
            Span::styled(format!("  {k:<14}"), theme.fg(theme.colors.gold)),
            Span::styled(what.to_string(), theme.fg(theme.colors.bone)),
        ])
    };
    let mut lines = vec![
        Line::from(Span::styled("  Panels", theme.bold())),
        key("1 – 5", "focus pipeline / claims / ledger / memory / log"),
        key("Tab, S-Tab", "cycle focus forwards and backwards"),
        key("z or +", "zoom the focused panel full-screen"),
        Line::from(""),
        Line::from(Span::styled("  Selection", theme.bold())),
        key("j / k, ↓ / ↑", "move the selection"),
        key("g / G", "first / last row"),
        key("Enter or e", "explain the selected artifact (why-blocked on memory)"),
        Line::from(""),
        Line::from(Span::styled("  Commands", theme.bold())),
        key("r", "run the selected stage"),
        key("R", "run the whole pipeline"),
        key("p", "plan"),
        key("d", "doctor"),
        key("m", "focus memory and search it"),
        key("t", "cycle theme (cairn-dark / cairn-light / mono)"),
        key("/ or :", "command line, Tab completes"),
        Line::from(""),
        Line::from(Span::styled("  Leaving", theme.bold())),
        key("Esc", "close modal, then cancel input, then unzoom"),
        key("q, Ctrl-C", "quit (confirms while a run is in flight)"),
        Line::from(""),
        Line::from(Span::styled("  Slash commands", theme.bold())),
    ];
    for line in cairn_state::commands::help_lines() {
        lines.push(Line::from(Span::styled(format!("  {line}"), theme.muted())));
    }
    floating(frame, area, theme, "Keys", lines, 72, 84);
}

/// The `explain` payload deserves more room than a panel row: it is the
/// full provenance chain of one artifact.
fn draw_explain_modal(frame: &mut Frame, area: Rect, app: &App) {
    let theme = &app.theme;
    let explain = &app.state.explain;
    let mut lines = Vec::new();

    if let Some(missing) = &explain.not_found {
        lines.push(Line::from(Span::styled(
            format!("  No artifact with id {missing}."),
            theme.fg(theme.colors.error),
        )));
        floating(frame, area, theme, "Explain", lines, 70, 30);
        return;
    }

    let field = |label: &str, value: String| {
        Line::from(vec![
            Span::styled(format!("  {label:<12}"), theme.muted()),
            Span::styled(value, theme.fg(theme.colors.bone)),
        ])
    };
    lines.push(field("artifact", explain.artifact_id.clone().unwrap_or_default()));
    lines.push(field("stage", explain.stage.clone().unwrap_or_default()));
    lines.push(field("work_key", explain.work_key.clone().unwrap_or_default()));
    if let Some(uri) = &explain.s3_uri {
        lines.push(field("s3", uri.clone()));
    }
    if let Some(region) = &explain.region {
        lines.push(field("region", region.clone()));
    }
    if let Some(fingerprint) = &explain.env_fingerprint {
        lines.push(field("env", fingerprint.clone()));
    }
    if let Some(size) = explain.size_bytes {
        lines.push(field("size", fmt_bytes(size)));
    }
    if let Some(duration) = explain.duration_ms {
        lines.push(field("duration", fmt_ms(duration)));
    }
    if let Some(created) = &explain.created_at {
        lines.push(field("created", created.clone()));
    }
    if let Some(quarantined) = &explain.quarantined_at {
        lines.push(Line::from(Span::styled(
            format!("  QUARANTINED at {quarantined}"),
            theme.fg(theme.colors.error).add_modifier(Modifier::BOLD),
        )));
    }

    if !explain.inputs.is_empty() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled("  Inputs", theme.bold())));
        for input in &explain.inputs {
            lines.push(Line::from(Span::styled(
                format!(
                    "    {} {} {}",
                    input.input_kind.clone().unwrap_or_default(),
                    input.input_ref.clone().unwrap_or_default(),
                    short(input.input_digest.as_deref().unwrap_or(""), 20)
                ),
                theme.fg(theme.colors.stone),
            )));
        }
    }
    if !explain.downstream.is_empty() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            format!("  Downstream ({})", explain.downstream.len()),
            theme.bold(),
        )));
        for id in &explain.downstream {
            lines.push(Line::from(Span::styled(format!("    {id}"), theme.fg(theme.colors.stone))));
        }
    }
    if !explain.decisions.is_empty() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled("  Decisions", theme.bold())));
        for decision in &explain.decisions {
            lines.push(Line::from(vec![
                Span::styled(
                    format!("    {:<21}", decision.action.as_str()),
                    theme.tone(decision.action.tone()),
                ),
                Span::styled(
                    decision.explanation.clone().unwrap_or_default(),
                    theme.fg(theme.colors.stone),
                ),
            ]));
        }
    }
    if !explain.contradictions.is_empty() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled("  Contradictions", theme.bold())));
        for contradiction in &explain.contradictions {
            lines.push(Line::from(Span::styled(
                format!(
                    "    {} {}",
                    if contradiction.quarantined { "quarantined" } else { "recorded  " },
                    contradiction.evidence.clone().unwrap_or_default()
                ),
                theme.fg(theme.colors.error),
            )));
        }
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("  Esc to close", theme.dim())));
    floating(frame, area, theme, "Explain", lines, 78, 80);
}

fn draw_quit_modal(frame: &mut Frame, area: Rect, theme: &Theme) {
    let lines = vec![
        Line::from(Span::styled(
            "  A command is still running.",
            theme.fg(theme.colors.warning).add_modifier(Modifier::BOLD),
        )),
        Line::from(""),
        Line::from(Span::styled(
            "  Quitting kills it. Any claim it holds keeps its 45s lease",
            theme.fg(theme.colors.bone),
        )),
        Line::from(Span::styled(
            "  until it expires, then another worker may take it over.",
            theme.fg(theme.colors.bone),
        )),
        Line::from(""),
        Line::from(vec![
            Span::styled("  y", theme.fg(theme.colors.error).add_modifier(Modifier::BOLD)),
            Span::styled(" quit    ", theme.fg(theme.colors.bone)),
            Span::styled("any other key", theme.fg(theme.colors.gold)),
            Span::styled(" stay", theme.fg(theme.colors.bone)),
        ]),
    ];
    floating(frame, area, theme, "Quit?", lines, 60, 34);
}

// --- shared helpers ---------------------------------------------------

/// Render a list of multi-line items, scrolled so the selected item stays
/// visible, with the selected item painted in the selection style.
fn render_items(
    frame: &mut Frame,
    area: Rect,
    theme: &Theme,
    items: Vec<Vec<Line<'_>>>,
    selected: usize,
    focused: bool,
) {
    let height = area.height as usize;
    if height == 0 || items.is_empty() {
        return;
    }
    let selected = selected.min(items.len() - 1);
    let starts: Vec<usize> = items
        .iter()
        .scan(0usize, |acc, item| {
            let start = *acc;
            *acc += item.len();
            Some(start)
        })
        .collect();
    let selected_start = starts[selected];
    let selected_end = selected_start + items[selected].len();
    let total: usize = items.iter().map(Vec::len).sum();

    // Scroll the minimum needed to bring the selection fully into view.
    let offset = if selected_end > height {
        (selected_end - height).min(total.saturating_sub(height))
    } else {
        0
    }
    .min(selected_start);

    let mut lines: Vec<Line> = Vec::with_capacity(total);
    for (index, item) in items.into_iter().enumerate() {
        let highlight = focused && index == selected;
        for line in item {
            lines.push(if highlight { line.patch_style(theme.selection()) } else { line });
        }
    }
    frame.render_widget(Paragraph::new(lines).scroll((offset as u16, 0)), area);
}

fn render_empty(frame: &mut Frame, area: Rect, theme: &Theme, message: &str) {
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(message.to_string(), theme.dim())))
            .wrap(Wrap { trim: true }),
        area,
    );
}

/// A proportional bar in eighth-blocks, so a 30%-full six-cell bar reads as
/// 30% rather than rounding to a whole cell.
fn bar(fraction: f64, width: usize) -> String {
    let fraction = fraction.clamp(0.0, 1.0);
    let eighths = (fraction * width as f64 * 8.0).round() as usize;
    let full = eighths / 8;
    let remainder = eighths % 8;
    let mut out = "█".repeat(full.min(width));
    if full < width && remainder > 0 {
        out.push(['▏', '▏', '▎', '▍', '▌', '▋', '▊', '▉'][remainder]);
    }
    while out.chars().count() < width {
        out.push('░');
    }
    out
}

fn fmt_ms(ms: i64) -> String {
    if ms < 1_000 {
        format!("{ms}ms")
    } else if ms < 60_000 {
        format!("{:.1}s", ms as f64 / 1_000.0)
    } else {
        format!("{}m{:02}s", ms / 60_000, (ms % 60_000) / 1_000)
    }
}

fn fmt_bytes(bytes: i64) -> String {
    const UNITS: [&str; 5] = ["B", "KB", "MB", "GB", "TB"];
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit + 1 < UNITS.len() {
        value /= 1024.0;
        unit += 1;
    }
    if unit == 0 {
        format!("{bytes}{}", UNITS[0])
    } else {
        format!("{:.1}{}", value, UNITS[unit])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use cairn_state::ui::ThemeName;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    use serde_json::json;

    fn event(event_type: &str, payload: serde_json::Value) -> cairn_protocol::CairnEvent {
        let envelope = json!({
            "version": 1,
            "type": event_type,
            "timestamp": "2026-08-08T12:04:31.000000+00:00",
            "run_id": "11111111-2222-4333-8444-555555555555",
            "payload": payload,
        });
        cairn_protocol::parse_event_line(&serde_json::to_string(&envelope).unwrap()).unwrap()
    }

    /// Drive a realistic run into the state and render it, so the panels are
    /// exercised against the same payload shapes the backend really emits.
    fn populated(width: u16, height: u16) -> (App, Terminal<TestBackend>) {
        let mut app = App::new(ThemeName::CairnDark);
        app.state.apply_event(event(
            "run.started",
            json!({"target_stage": "eval", "owner": "worker-a", "region": "us-east-1", "bucket": "b"}),
        ));
        app.state.apply_event(event(
            "plan.completed",
            json!({"stages": [
                {"stage": "env", "work_key": "wk-env", "structurally_sound": true},
                {"stage": "dataset", "work_key": "wk-dataset", "structurally_sound": true},
                {"stage": "features", "work_key": "wk-features", "structurally_sound": false},
                {"stage": "checkpoint", "work_key": "wk-checkpoint", "structurally_sound": true},
                {"stage": "eval", "work_key": "wk-eval", "structurally_sound": true}
            ]}),
        ));
        app.state.apply_event(event("stage.started", json!({"stage": "env", "work_key": "wk-env"})));
        app.state.apply_event(event(
            "probe.completed",
            json!({"probe_type": "sampled_equivalence", "population_size": 128, "sample_size": 88,
                   "tolerance": 0.001, "runtime_ms": 412, "passed": true}),
        ));
        app.state.apply_event(event(
            "decision.recorded",
            json!({"decision_id": "d-1", "work_key": "wk-env", "stage": "env", "action": "REUSE",
                   "verdict": "reused", "change_class": "cosmetic", "proposed_by": "rule",
                   "authorized_by": "probe", "candidate_artifact_id": "art-env",
                   "latency_ms": 12, "explanation": "probe passed at tolerance"}),
        ));
        app.state.apply_event(event(
            "stage.completed",
            json!({"stage": "env", "action": "REUSE", "verdict": "reused", "artifact_id": "art-env"}),
        ));
        app.state.apply_event(event(
            "claim.contended",
            json!({"work_key": "wk-features", "stage": "features", "owner": "worker-b",
                   "owner_host": "ip-10-0-1-7", "owner_region": "eu-west-1", "owner_fence": 3}),
        ));
        app.state.apply_event(event(
            "claim.heartbeat",
            json!({"work_key": "wk-features", "owner": "worker-b", "fence": 3}),
        ));
        app.state.apply_event(event(
            "memory.search_completed",
            json!({"query": "cuda oom", "stage": "checkpoint", "provider": "TitanEmbeddingProvider",
                   "matches": [
                     {"signature_id": "sig-1", "stage": "checkpoint", "error_class": "OutOfMemoryError",
                      "summary_text": "batch 64 OOMs on g5.xlarge", "cosine_distance": 0.0412,
                      "wasted_ms": 91000, "has_verified_remediation": true},
                     {"signature_id": "sig-2", "stage": "checkpoint", "error_class": "ValueError",
                      "summary_text": "shape mismatch", "cosine_distance": 0.3117,
                      "wasted_ms": 400, "has_verified_remediation": false}]}),
        ));
        let terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        (app, terminal)
    }

    fn rendered(terminal: &Terminal<TestBackend>) -> String {
        let buffer = terminal.backend().buffer();
        let width = buffer.area.width as usize;
        buffer
            .content()
            .chunks(width)
            .map(|row| row.iter().map(|cell| cell.symbol()).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn the_full_layout_shows_every_domain_at_once() {
        let (mut app, mut terminal) = populated(140, 40);
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let screen = rendered(&terminal);

        // All five panel titles are on screen simultaneously — the whole
        // point of the rewrite.
        for title in ["Pipeline", "Claims", "Ledger", "Memory", "Log"] {
            assert!(screen.contains(title), "{title} panel missing from the layout");
        }
        // Every stage of the DAG is present, not just the current one.
        for stage in ["env", "dataset", "features", "checkpoint", "eval"] {
            assert!(screen.contains(stage), "stage {stage} not shown");
        }
        // A stage that finished several events ago still shows its verdict.
        assert!(screen.contains("REUSE"), "the env verdict scrolled away");
        // The live race and its two workers are both visible.
        assert!(screen.contains("worker-b"), "claim owner missing");
        assert!(screen.contains("worker-a"), "the challenger (us) is missing");
        assert!(screen.contains("lease"), "no lease countdown");
        // The advisory label is on screen, per docs/project/PROJECT.md §7.2.
        assert!(screen.contains("advisory"), "a weak memory match must be labelled advisory");
    }

    #[test]
    fn a_small_terminal_collapses_to_one_panel_and_a_tab_bar() {
        let (mut app, mut terminal) = populated(60, 18);
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let screen = rendered(&terminal);
        // The numbered tab bar keeps every panel reachable.
        assert!(screen.contains("1:Pipeline"), "no numbered tab bar in the cramped layout");
        assert!(screen.contains("4:Memory"));
        // Only the focused panel's body is drawn.
        assert!(!screen.contains("advisory"), "memory body should not render while pipeline is focused");

        app.state.ui.focus = Panel::Memory;
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        assert!(rendered(&terminal).contains("advisory"), "focusing memory must show it");
    }

    #[test]
    fn zoom_gives_the_focused_panel_the_whole_body() {
        let (mut app, mut terminal) = populated(140, 40);
        app.state.ui.focus = Panel::Ledger;
        app.state.ui.zoomed = true;
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let screen = rendered(&terminal);
        assert!(screen.contains("[zoomed]"));
        assert!(screen.contains("probe passed at tolerance"));
        assert!(!screen.contains("Pipeline"), "a zoomed panel is alone in the body");
    }

    #[test]
    fn the_help_modal_floats_over_the_panels() {
        let (mut app, mut terminal) = populated(140, 40);
        app.state.ui.show_help = true;
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let screen = rendered(&terminal);
        assert!(screen.contains("Keys"));
        assert!(screen.contains("zoom the focused panel"));
        assert!(screen.contains("/explain"), "the slash registry is in the help modal");
    }

    #[test]
    fn an_escalation_gets_its_own_banner_with_the_real_figures() {
        let (mut app, mut terminal) = populated(140, 40);
        app.state.apply_event(event(
            "approval.requested",
            json!({"work_key": "wk-eval", "stage": "eval", "projected_cost_usd": 0.812345,
                   "approval_usd": 0.5, "detail": "12 vCPU-min"}),
        ));
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let screen = rendered(&terminal);
        assert!(screen.contains("ESCALATED"));
        // Six decimals: these are sub-cent figures and $0.81 would be a lie
        // about precision the backend actually reported.
        assert!(screen.contains("$0.812345"), "the real projected cost must be shown exactly");
        assert!(screen.contains("$0.500000"));
    }

    #[test]
    fn rendering_never_panics_at_absurd_sizes() {
        for (width, height) in [(1, 1), (2, 3), (20, 6), (79, 23), (80, 24), (300, 100)] {
            let (mut app, mut terminal) = populated(width, height);
            app.state.ui.show_help = true;
            terminal.draw(|frame| draw(frame, &mut app)).unwrap();
            app.state.ui.show_help = false;
            app.state.ui.zoomed = true;
            for panel in PANELS {
                app.state.ui.focus = panel;
                terminal.draw(|frame| draw(frame, &mut app)).unwrap();
            }
        }
    }

    #[test]
    fn the_lease_bar_drains_as_real_time_passes() {
        let (mut app, mut terminal) = populated(140, 40);
        app.state.set_now(0);
        app.state.apply_event(event(
            "claim.acquired",
            json!({"work_key": "wk-eval", "stage": "eval", "owner": "worker-a", "fence": 1,
                   "region": "us-east-1"}),
        ));
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        assert!(rendered(&terminal).contains("45.0s"), "a fresh lease shows its full 45s");

        app.state.set_now(40_000);
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let screen = rendered(&terminal);
        assert!(screen.contains("5.0s"), "the countdown must track real elapsed time");
        assert!(screen.contains("OVERDUE"), "a heartbeat 40s late is inside the takeover window");
    }

    #[test]
    fn a_not_found_explain_says_so_rather_than_showing_stale_detail() {
        let (mut app, mut terminal) = populated(140, 40);
        app.state.apply_event(event("explain.not_found", json!({"artifact_id": "art-nope"})));
        app.state.ui.show_explain = true;
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        assert!(rendered(&terminal).contains("No artifact with id art-nope"));
    }

    /// The whole stack against a real cluster: a genuine `cairn plan`
    /// subprocess, its real NDJSON, the real reducer, and these real
    /// panels. Prints the frame so the layout can be eyeballed without a
    /// TTY. Ignored by default — needs `CAIRN_DATABASE_URL` and a
    /// `cairn`-importable Python.
    ///
    /// ```text
    /// cargo test -p cairn-tui --bin cairn-tui -- --ignored --nocapture
    /// ```
    #[test]
    #[ignore = "needs a live CockroachDB cluster and a cairn-importable Python"]
    fn live_plan_renders_the_real_pipeline() {
        let mut app = App::new(ThemeName::CairnDark);
        app.dispatch("plan", "");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(180);
        while std::time::Instant::now() < deadline {
            app.drain_backend();
            if !app.state.session.running {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        app.drain_backend();

        assert_eq!(
            app.state.session.last_exit_code,
            Some(0),
            "cairn plan failed — stderr: {:?}",
            app.state.session.stderr_tail
        );
        // A real plan names every stage with a real deterministic work key.
        for node in &app.state.pipeline.nodes {
            assert!(
                node.work_key.is_some(),
                "stage {} got no work_key from the real planner",
                node.stage
            );
            assert_eq!(node.status, StageStatus::Planned);
        }

        let mut terminal = Terminal::new(TestBackend::new(150, 44)).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        println!("{}", rendered(&terminal));
    }

    /// A real `cairn run --all` against the live cluster, rendered. This is
    /// the case the old transcript UI handled worst: by the end of a run
    /// every stage's verdict had scrolled away. Here the finished DAG, the
    /// claims it took and the decisions it recorded are all still on screen
    /// together.
    #[test]
    #[ignore = "needs a live CockroachDB cluster and a cairn-importable Python"]
    fn live_run_all_leaves_every_stage_visible_at_once() {
        let mut app = App::new(ThemeName::CairnDark);
        app.dispatch("run", "--all");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(900);
        while std::time::Instant::now() < deadline {
            app.tick();
            app.drain_backend();
            if !app.state.session.running {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        app.drain_backend();
        app.tick();

        assert!(!app.state.session.running, "the run never finished");
        let mut terminal = Terminal::new(TestBackend::new(150, 44)).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        println!("{}", rendered(&terminal));
        println!(
            "exit={:?} events={} decisions={} claims={}",
            app.state.session.last_exit_code,
            app.state.events_seen,
            app.state.ledger.entries.len(),
            app.state.claims.total()
        );

        assert!(app.state.events_seen > 0, "a real run emitted no events at all");
        // Whatever the outcome, no stage may be left mid-flight: the panel
        // showing a permanently-spinning stage would be a lie about state.
        assert!(
            app.state.pipeline.current_stage.is_none(),
            "a finished run left {:?} still marked running",
            app.state.pipeline.current_stage
        );
    }

    /// Render an NDJSON stream captured from a genuine backend run. This is
    /// useful for scenarios that must execute inside the Linux workload
    /// image (real S3/Bedrock libraries) while still proving the native TUI
    /// reducer and panels tell the same story from the exact emitted events.
    #[test]
    #[ignore = "needs CAIRN_REMEDIATION_EVENTS_FILE from a real backend run"]
    fn live_event_stream_renders_refuse_remediate_and_downstream_replan() {
        let path = std::env::var("CAIRN_REMEDIATION_EVENTS_FILE")
            .expect("set CAIRN_REMEDIATION_EVENTS_FILE to real captured NDJSON");
        let raw = std::fs::read_to_string(path).expect("read real NDJSON stream");
        let mut app = App::new(ThemeName::CairnDark);
        for line in raw.lines() {
            if let Some(event) = cairn_protocol::parse_event_line(line) {
                app.state.apply_event(event);
            }
        }

        let actions: Vec<&str> = app
            .state
            .ledger
            .entries
            .iter()
            .map(|entry| entry.action.as_str())
            .collect();
        assert!(actions.contains(&"REFUSE_DOOMED"));
        assert!(actions.contains(&"REMEDIATE_AND_REPLAN"));

        let checkpoint = app.state.pipeline.node("checkpoint").unwrap();
        assert_eq!(checkpoint.status, StageStatus::Succeeded);
        assert_eq!(checkpoint.action.as_deref(), Some("REMEDIATE_AND_REPLAN"));
        let refused_key = app
            .state
            .ledger
            .entries
            .iter()
            .find(|entry| entry.action.as_str() == "REFUSE_DOOMED")
            .map(|entry| entry.work_key.as_str())
            .unwrap();
        assert_ne!(checkpoint.work_key.as_deref(), Some(refused_key));
        let eval = app.state.pipeline.node("eval").unwrap();
        assert_eq!(eval.status, StageStatus::Succeeded);
        assert_eq!(eval.action.as_deref(), Some("REUSE"));

        let mut terminal = Terminal::new(TestBackend::new(150, 44)).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let screen = rendered(&terminal);
        println!("{screen}");
        assert!(screen.contains("REMEDIATE_AND_REPLAN"));
        assert!(screen.contains("REFUSE_DOOMED"));
        assert!(screen.contains("by rule (proposal only)"));
        assert!(!screen.contains("by model (proposal only)"));
    }

    #[test]
    fn the_proportional_bar_is_honest_at_the_edges() {
        assert_eq!(bar(0.0, 4).chars().filter(|c| *c == '░').count(), 4);
        assert_eq!(bar(1.0, 4), "████");
        assert_eq!(bar(0.5, 4).chars().count(), 4);
        assert_eq!(bar(2.0, 3), "███", "an out-of-range fraction clamps rather than overflowing");
    }

    #[test]
    fn durations_and_sizes_read_the_way_an_operator_expects() {
        assert_eq!(fmt_ms(412), "412ms");
        assert_eq!(fmt_ms(8_700), "8.7s");
        assert_eq!(fmt_ms(125_000), "2m05s");
        assert_eq!(fmt_bytes(512), "512B");
        assert_eq!(fmt_bytes(4_194_304), "4.0MB");
    }

    #[test]
    fn the_clock_column_degrades_to_padding_for_local_lines() {
        assert_eq!(clock("2026-08-08T12:04:31.000000+00:00"), "12:04:31 ");
        assert_eq!(clock("").len(), 9, "a local line still aligns with timestamped ones");
    }

    /// Regenerates `docs/assets/tui/tui-overview.txt` — the block README.md
    /// embeds. It goes through the real renderer over `populated()`, the same
    /// event payloads `src/cairn/obs/events.py` emits, so the README shows this
    /// binary's actual output rather than a drawing of it.
    ///
    /// `#[ignore]` because its only job is to produce that file:
    ///
    ///     cargo test -p cairn-tui -- --ignored --nocapture layout_snapshot \
    ///       > ../docs/assets/tui/raw.txt
    #[test]
    #[ignore]
    fn layout_snapshot() {
        let (mut app, mut terminal) = populated(132, 38);
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        println!("--8<-- SNAPSHOT BEGIN");
        println!("{}", rendered(&terminal));
        println!("--8<-- SNAPSHOT END");
    }
}
