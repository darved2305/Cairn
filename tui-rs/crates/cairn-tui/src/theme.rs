//! The three palettes from `tui/src/theme/{cairn-dark,cairn-light,mono}.json`,
//! carried over verbatim as 24-bit RGB.
//!
//! The TypeScript version emitted its own `\x1b[38;2;r;g;bm` sequences;
//! here ratatui owns the escape writing, so a palette is just a table of
//! `Color::Rgb`. The two behaviours that were policy rather than mechanism
//! are preserved exactly:
//!
//! * `mono` is not a grey palette that happens to look flat — it is the
//!   palette with colour *disabled*, so every role resolves to the
//!   terminal's own default foreground and the user's terminal theme wins.
//! * `NO_COLOR` (https://no-color.org: presence of the variable at any
//!   value, including empty) disables colour for every palette, not just
//!   `mono`.

use cairn_state::ledger::ActionTone;
use cairn_state::ui::ThemeName;
use ratatui::style::{Color, Modifier, Style};

#[derive(Debug, Clone, Copy)]
pub struct Palette {
    pub stone: Color,
    pub bone: Color,
    pub gold: Color,
    pub cyan: Color,
    pub violet: Color,
    pub green: Color,
    pub warning: Color,
    pub error: Color,
    pub panel: Color,
    pub panel_pending: Color,
    pub panel_success: Color,
    pub panel_error: Color,
    pub selected: Color,
    pub muted: Color,
    pub dim: Color,
    pub background: Color,
}

const fn rgb(hex: u32) -> Color {
    Color::Rgb((hex >> 16) as u8, ((hex >> 8) & 0xFF) as u8, (hex & 0xFF) as u8)
}

/// `tui/src/theme/cairn-dark.json`.
const CAIRN_DARK: Palette = Palette {
    stone: rgb(0x94908A),
    bone: rgb(0xD8D1C4),
    gold: rgb(0xD6A85F),
    cyan: rgb(0x63C5DA),
    violet: rgb(0xA88BE8),
    green: rgb(0x78C89B),
    warning: rgb(0xD8AE64),
    error: rgb(0xE26D75),
    panel: rgb(0x232329),
    panel_pending: rgb(0x282832),
    panel_success: rgb(0x263129),
    panel_error: rgb(0x382629),
    selected: rgb(0x34343E),
    muted: rgb(0x777780),
    dim: rgb(0x56565F),
    background: rgb(0x1A1A1F),
};

/// `tui/src/theme/cairn-light.json`.
const CAIRN_LIGHT: Palette = Palette {
    stone: rgb(0x6B675F),
    bone: rgb(0x2A2822),
    gold: rgb(0x96702F),
    cyan: rgb(0x1D7A94),
    violet: rgb(0x6A4FB0),
    green: rgb(0x357A54),
    warning: rgb(0x8A6412),
    error: rgb(0xB23A44),
    panel: rgb(0xEDEAE2),
    panel_pending: rgb(0xE6E1F0),
    panel_success: rgb(0xE1EFE6),
    panel_error: rgb(0xF3E1E2),
    selected: rgb(0xDCD6C8),
    muted: rgb(0x8A867D),
    dim: rgb(0xA6A296),
    background: rgb(0xFAF8F3),
};

/// Every role resolves to the terminal's own defaults. Used for `mono` and
/// whenever `NO_COLOR` is set.
const PLAIN: Palette = Palette {
    stone: Color::Reset,
    bone: Color::Reset,
    gold: Color::Reset,
    cyan: Color::Reset,
    violet: Color::Reset,
    green: Color::Reset,
    warning: Color::Reset,
    error: Color::Reset,
    panel: Color::Reset,
    panel_pending: Color::Reset,
    panel_success: Color::Reset,
    panel_error: Color::Reset,
    selected: Color::Reset,
    muted: Color::Reset,
    dim: Color::Reset,
    background: Color::Reset,
};

#[derive(Debug, Clone, Copy)]
pub struct Theme {
    pub name: ThemeName,
    pub colors: Palette,
    /// False for `mono` and whenever `NO_COLOR` is present. When false the
    /// panels lean on `BOLD`/`DIM`/`REVERSED` for the distinctions colour
    /// would otherwise carry, rather than silently losing them.
    pub color_enabled: bool,
}

/// https://no-color.org — *presence* disables colour, whatever the value.
pub fn no_color_requested() -> bool {
    std::env::var_os("NO_COLOR").is_some()
}

impl Theme {
    pub fn load(name: ThemeName) -> Self {
        let color_enabled = name != ThemeName::Mono && !no_color_requested();
        let colors = if !color_enabled {
            PLAIN
        } else {
            match name {
                ThemeName::CairnDark => CAIRN_DARK,
                ThemeName::CairnLight => CAIRN_LIGHT,
                ThemeName::Mono => PLAIN,
            }
        };
        Self { name, colors, color_enabled }
    }

    pub fn fg(&self, color: Color) -> Style {
        if self.color_enabled {
            Style::default().fg(color)
        } else {
            Style::default()
        }
    }

    pub fn bg(&self, color: Color) -> Style {
        if self.color_enabled {
            Style::default().bg(color)
        } else {
            Style::default()
        }
    }

    pub fn bold(&self) -> Style {
        Style::default().add_modifier(Modifier::BOLD)
    }

    pub fn dim(&self) -> Style {
        if self.color_enabled {
            Style::default().fg(self.colors.dim)
        } else {
            Style::default().add_modifier(Modifier::DIM)
        }
    }

    pub fn muted(&self) -> Style {
        if self.color_enabled {
            Style::default().fg(self.colors.muted)
        } else {
            Style::default().add_modifier(Modifier::DIM)
        }
    }

    /// The base style for the whole frame. Painting an explicit background
    /// matters on terminals whose own background differs from the palette's
    /// — without it the panels' `panel*` fills would sit on a mismatched
    /// ground.
    pub fn base(&self) -> Style {
        if self.color_enabled {
            Style::default().fg(self.colors.bone).bg(self.colors.background)
        } else {
            Style::default()
        }
    }

    /// The style a selected row is painted in. With colour off this becomes
    /// `REVERSED`, which is the only way left to show selection.
    pub fn selection(&self) -> Style {
        if self.color_enabled {
            Style::default().bg(self.colors.selected).add_modifier(Modifier::BOLD)
        } else {
            Style::default().add_modifier(Modifier::REVERSED)
        }
    }

    /// Border style, brighter for the focused panel — the lazygit cue for
    /// where the keys will land.
    pub fn border(&self, focused: bool) -> Style {
        if focused {
            self.fg(self.colors.gold).add_modifier(Modifier::BOLD)
        } else {
            self.dim()
        }
    }

    /// Colour for one of the five decision-action roles. With colour off
    /// the ledger falls back to the action name itself carrying the
    /// meaning, which it always does anyway.
    pub fn tone(&self, tone: ActionTone) -> Style {
        let color = match tone {
            ActionTone::Saved => self.colors.green,
            ActionTone::Spent => self.colors.cyan,
            ActionTone::Avoided => self.colors.violet,
            ActionTone::Recovered => self.colors.gold,
            ActionTone::Escalated => self.colors.warning,
            ActionTone::Neutral => self.colors.stone,
        };
        self.fg(color)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mono_disables_colour_entirely() {
        let theme = Theme::load(ThemeName::Mono);
        assert!(!theme.color_enabled);
        assert_eq!(theme.fg(Color::Red), Style::default(), "mono must not paint");
    }

    #[test]
    fn a_palette_carries_the_exact_hex_from_the_typescript_json() {
        // Only meaningful when the ambient env is not forcing NO_COLOR.
        if no_color_requested() {
            return;
        }
        let dark = Theme::load(ThemeName::CairnDark);
        assert_eq!(dark.colors.gold, Color::Rgb(0xD6, 0xA8, 0x5F));
        assert_eq!(dark.colors.background, Color::Rgb(0x1A, 0x1A, 0x1F));
        let light = Theme::load(ThemeName::CairnLight);
        assert_eq!(light.colors.error, Color::Rgb(0xB2, 0x3A, 0x44));
    }

    #[test]
    fn selection_survives_colour_being_off() {
        let mono = Theme::load(ThemeName::Mono);
        assert!(mono.selection().add_modifier.contains(Modifier::REVERSED));
    }
}
