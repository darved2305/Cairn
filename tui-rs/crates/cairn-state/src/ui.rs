//! Which panel is focused, what is selected inside it, whether it is
//! zoomed, and what the command line currently holds. Pure data — the
//! renderer reads it, the key handler writes it, and both are testable
//! without a terminal.

/// The five focusable panels, in `1`..`5` order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Panel {
    Pipeline,
    Claims,
    Ledger,
    Memory,
    Transcript,
}

pub const PANELS: [Panel; 5] =
    [Panel::Pipeline, Panel::Claims, Panel::Ledger, Panel::Memory, Panel::Transcript];

impl Panel {
    pub fn title(self) -> &'static str {
        match self {
            Panel::Pipeline => "Pipeline",
            Panel::Claims => "Claims",
            Panel::Ledger => "Ledger",
            Panel::Memory => "Memory",
            Panel::Transcript => "Log",
        }
    }

    pub fn index(self) -> usize {
        PANELS.iter().position(|p| *p == self).unwrap_or(0)
    }

    pub fn from_index(index: usize) -> Panel {
        PANELS[index % PANELS.len()]
    }

    pub fn next(self) -> Panel {
        Panel::from_index(self.index() + 1)
    }

    pub fn prev(self) -> Panel {
        Panel::from_index(self.index() + PANELS.len() - 1)
    }

    /// The keybinding legend the footer shows for this panel.
    pub fn hints(self) -> &'static str {
        match self {
            Panel::Pipeline => "j/k stage · enter explain artifact · r run stage · R run all · p plan",
            Panel::Claims => "j/k race · enter explain artifact · lease/heartbeat count down live",
            Panel::Ledger => "j/k decision · enter explain candidate artifact",
            Panel::Memory => "j/k match · m new search · enter why-blocked",
            Panel::Transcript => "j/k scroll · g/G top/bottom · /clear to empty",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputMode {
    /// Keys are commands.
    Normal,
    /// `/` or `:` — typing a slash command.
    Command,
    /// `m` — typing memory search text directly.
    MemorySearch,
    /// `q` with a run in flight.
    ConfirmQuit,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ThemeName {
    CairnDark,
    CairnLight,
    Mono,
}

pub const THEME_NAMES: [&str; 3] = ["cairn-dark", "cairn-light", "mono"];

impl ThemeName {
    pub fn parse(raw: &str) -> Option<ThemeName> {
        match raw {
            "cairn-dark" => Some(ThemeName::CairnDark),
            "cairn-light" => Some(ThemeName::CairnLight),
            "mono" => Some(ThemeName::Mono),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            ThemeName::CairnDark => "cairn-dark",
            ThemeName::CairnLight => "cairn-light",
            ThemeName::Mono => "mono",
        }
    }

    pub fn next(self) -> ThemeName {
        match self {
            ThemeName::CairnDark => ThemeName::CairnLight,
            ThemeName::CairnLight => ThemeName::Mono,
            ThemeName::Mono => ThemeName::CairnDark,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct UiState {
    pub focus: Panel,
    pub zoomed: bool,
    pub show_help: bool,
    pub input_mode: InputMode,
    pub input: String,
    /// Byte offset of the caret within `input`. Kept in bytes because the
    /// editor only ever inserts/removes whole `char`s at the caret.
    pub cursor: usize,
    pub history: Vec<String>,
    pub history_index: Option<usize>,
    /// Completion candidates for the current input, and which one is
    /// highlighted.
    pub completions: Vec<String>,
    pub completion_index: usize,
    pub selection: [usize; 5],
    /// Line offset of the transcript panel; `usize::MAX` means "pinned to
    /// the bottom", which is the default and what a live run wants.
    pub transcript_scroll: Option<usize>,
    pub theme: ThemeName,
    pub status_message: Option<String>,
    pub should_quit: bool,
}

impl Default for UiState {
    fn default() -> Self {
        Self {
            focus: Panel::Pipeline,
            zoomed: false,
            show_help: false,
            input_mode: InputMode::Normal,
            input: String::new(),
            cursor: 0,
            history: Vec::new(),
            history_index: None,
            completions: Vec::new(),
            completion_index: 0,
            selection: [0; 5],
            transcript_scroll: None,
            theme: ThemeName::CairnDark,
            status_message: None,
            should_quit: false,
        }
    }
}

impl UiState {
    pub fn selected(&self, panel: Panel) -> usize {
        self.selection[panel.index()]
    }

    pub fn set_selected(&mut self, panel: Panel, index: usize) {
        self.selection[panel.index()] = index;
    }

    /// Move the focused panel's selection by `delta`, clamped to `len`.
    pub fn move_selection(&mut self, len: usize, delta: isize) {
        if len == 0 {
            self.set_selected(self.focus, 0);
            return;
        }
        let current = self.selected(self.focus).min(len - 1) as isize;
        let next = (current + delta).clamp(0, len as isize - 1) as usize;
        self.set_selected(self.focus, next);
    }

    pub fn begin_input(&mut self, mode: InputMode, seed: &str) {
        self.input_mode = mode;
        self.input = seed.to_string();
        self.cursor = self.input.len();
        self.completions.clear();
        self.completion_index = 0;
        self.history_index = None;
    }

    pub fn cancel_input(&mut self) {
        self.input_mode = InputMode::Normal;
        self.input.clear();
        self.cursor = 0;
        self.completions.clear();
        self.completion_index = 0;
        self.history_index = None;
    }

    pub fn insert_char(&mut self, ch: char) {
        self.input.insert(self.cursor, ch);
        self.cursor += ch.len_utf8();
    }

    pub fn backspace(&mut self) {
        if self.cursor == 0 {
            return;
        }
        let prev = self.input[..self.cursor]
            .chars()
            .next_back()
            .map(char::len_utf8)
            .unwrap_or(0);
        let start = self.cursor - prev;
        self.input.replace_range(start..self.cursor, "");
        self.cursor = start;
    }

    pub fn move_cursor(&mut self, delta: isize) {
        if delta < 0 {
            if let Some(prev) = self.input[..self.cursor].chars().next_back() {
                self.cursor -= prev.len_utf8();
            }
        } else if let Some(next) = self.input[self.cursor..].chars().next() {
            self.cursor += next.len_utf8();
        }
    }

    /// `Esc` priority, in the order the spec fixes: close the modal, then
    /// leave input mode, then unzoom. Returns true if anything happened.
    pub fn escape(&mut self) -> bool {
        if self.show_help {
            self.show_help = false;
            return true;
        }
        if self.input_mode != InputMode::Normal {
            self.cancel_input();
            return true;
        }
        if self.zoomed {
            self.zoomed = false;
            return true;
        }
        false
    }
}
