//! The slash-command ecosystem, ported as-is from
//! `tui/src/commands/registry.ts` and `completions.ts`.
//!
//! Every command here maps to a real backend capability — a `cairn`
//! subcommand a human could type themselves — or to a purely local view of
//! state this session actually observed. `/sessions`, `/claims`,
//! `/resume` and `/tree` remain deliberately absent: none of them has a
//! real read API behind it yet (there is still no "list active claims" or
//! "list runs" query in `src/cairn/db/*.py`), and an exposed command must
//! be backed by real data, not a placeholder. That discipline is why the
//! Claims panel in this TUI is populated by *observed events* rather than
//! by a query that does not exist.

use std::collections::BTreeSet;

use crate::pipeline::PIPELINE_STAGES;
use crate::ui::THEME_NAMES;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArgSource {
    None,
    Stages,
    ArtifactIds,
    Themes,
    FreeText,
}

#[derive(Debug, Clone, Copy)]
pub struct SlashCommand {
    pub name: &'static str,
    pub description: &'static str,
    pub argument_hint: Option<&'static str>,
    pub arg_source: ArgSource,
}

/// Exactly the twelve commands the TypeScript registry exposed.
pub const SLASH_COMMANDS: [SlashCommand; 12] = [
    SlashCommand {
        name: "run",
        description: "execute stage or pipeline",
        argument_hint: Some("[stage|--all]"),
        arg_source: ArgSource::Stages,
    },
    SlashCommand {
        name: "plan",
        description: "inspect decisions without execution",
        argument_hint: None,
        arg_source: ArgSource::None,
    },
    SlashCommand {
        name: "explain",
        description: "explain an artifact or verdict",
        argument_hint: Some("<artifact_id>"),
        arg_source: ArgSource::ArtifactIds,
    },
    SlashCommand {
        name: "memory",
        description: "search causal memory",
        argument_hint: Some("<text>"),
        arg_source: ArgSource::FreeText,
    },
    SlashCommand {
        name: "doctor",
        description: "diagnose environment",
        argument_hint: None,
        arg_source: ArgSource::None,
    },
    SlashCommand {
        name: "model",
        description: "show the configured model catalog",
        argument_hint: None,
        arg_source: ArgSource::None,
    },
    SlashCommand {
        name: "status",
        description: "current session status",
        argument_hint: None,
        arg_source: ArgSource::None,
    },
    SlashCommand {
        name: "usage",
        description: "compute reused/recomputed this session",
        argument_hint: None,
        arg_source: ArgSource::None,
    },
    SlashCommand {
        name: "theme",
        description: "switch theme",
        argument_hint: Some("<cairn-dark|cairn-light|mono>"),
        arg_source: ArgSource::Themes,
    },
    SlashCommand {
        name: "settings",
        description: "configure Cairn",
        argument_hint: None,
        arg_source: ArgSource::None,
    },
    SlashCommand {
        name: "help",
        description: "list commands",
        argument_hint: None,
        arg_source: ArgSource::None,
    },
    SlashCommand {
        name: "clear",
        description: "clear the transcript",
        argument_hint: None,
        arg_source: ArgSource::None,
    },
];

pub fn lookup(name: &str) -> Option<&'static SlashCommand> {
    SLASH_COMMANDS.iter().find(|command| command.name == name)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedSlash {
    pub name: String,
    pub args: String,
}

/// Port of `registry.ts::parseSlashInput`. Accepts a leading `:` too,
/// because this TUI also enters command mode with `:` (vim muscle memory);
/// the command names themselves are unchanged.
pub fn parse_slash_input(input: &str) -> Option<ParsedSlash> {
    let trimmed = input.trim();
    let body = trimmed.strip_prefix('/').or_else(|| trimmed.strip_prefix(':'))?;
    let (name, args) = match body.find(char::is_whitespace) {
        Some(index) => (&body[..index], body[index..].trim()),
        None => (body, ""),
    };
    if name.is_empty() {
        return None;
    }
    Some(ParsedSlash { name: name.to_string(), args: args.to_string() })
}

/// Artifact ids this *session* has actually seen in real events, so
/// `/explain` can suggest them — never a fabricated id. Port of
/// `completions.ts::SessionKnowledge`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SessionKnowledge {
    artifact_ids: BTreeSet<String>,
}

impl SessionKnowledge {
    pub fn note_artifact_id(&mut self, artifact_id: Option<&str>) {
        if let Some(id) = artifact_id {
            if !id.is_empty() {
                self.artifact_ids.insert(id.to_string());
            }
        }
    }

    pub fn known_artifact_ids(&self) -> Vec<String> {
        self.artifact_ids.iter().cloned().collect()
    }

    pub fn len(&self) -> usize {
        self.artifact_ids.len()
    }

    pub fn is_empty(&self) -> bool {
        self.artifact_ids.is_empty()
    }
}

/// Completion candidates for the current command-line contents. Returns
/// full replacement strings (including the leading `/`), so applying one
/// is a straight assignment.
pub fn complete(input: &str, knowledge: &SessionKnowledge) -> Vec<String> {
    let trimmed = input.trim_start();
    let lead = if trimmed.starts_with(':') { ':' } else { '/' };
    let Some(body) = trimmed.strip_prefix('/').or_else(|| trimmed.strip_prefix(':')) else {
        return Vec::new();
    };

    match body.find(char::is_whitespace) {
        // Still typing the command name.
        None => SLASH_COMMANDS
            .iter()
            .filter(|command| command.name.starts_with(body))
            .map(|command| format!("{lead}{}", command.name))
            .collect(),
        // Typing an argument.
        Some(index) => {
            let name = &body[..index];
            let prefix = body[index..].trim_start();
            let Some(command) = lookup(name) else { return Vec::new() };
            let candidates: Vec<String> = match command.arg_source {
                ArgSource::None | ArgSource::FreeText => Vec::new(),
                ArgSource::Stages => PIPELINE_STAGES
                    .iter()
                    .map(|s| s.to_string())
                    .chain(std::iter::once("--all".to_string()))
                    .collect(),
                ArgSource::ArtifactIds => knowledge.known_artifact_ids(),
                ArgSource::Themes => THEME_NAMES.iter().map(|t| t.to_string()).collect(),
            };
            candidates
                .into_iter()
                .filter(|candidate| candidate.starts_with(prefix))
                .map(|candidate| format!("{lead}{name} {candidate}"))
                .collect()
        }
    }
}

/// The `/help` body, generated from the registry so the two can never
/// drift.
pub fn help_lines() -> Vec<String> {
    SLASH_COMMANDS
        .iter()
        .map(|command| {
            let hint = command.argument_hint.unwrap_or("");
            format!("/{:<9} {:<28} {}", command.name, hint, command.description)
        })
        .collect()
}

/// A small, deterministic, clearly-non-LLM heuristic, ported verbatim in
/// spirit from `app.ts::matchFreeTextIntent`: two fixed patterns only.
/// There is no token stream and no LLM turn loop in this process, so
/// free text must never pretend to be understood.
pub fn match_free_text_intent(text: &str) -> Option<ParsedSlash> {
    let lower = text.to_lowercase();
    let has_word = |word: &str| {
        lower
            .split(|c: char| !c.is_alphanumeric())
            .any(|token| token == word)
    };
    if has_word("run")
        && (lower.contains("everything") || has_word("all") || lower.contains("pipeline"))
    {
        return Some(ParsedSlash { name: "run".into(), args: "--all".into() });
    }
    if has_word("doctor") || lower.contains("diagnos") {
        return Some(ParsedSlash { name: "doctor".into(), args: String::new() });
    }
    None
}

/// Fixed narration for `/run`, ported from `app.ts::narrationForRun`.
pub fn narration_for_run(args: &str) -> String {
    let target = args.trim();
    if target.is_empty() || target == "--all" {
        "I'll check what can survive first.".to_string()
    } else {
        format!("I'll check what can survive for {target} first.")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_slash_and_colon_forms() {
        assert_eq!(
            parse_slash_input("/run --all"),
            Some(ParsedSlash { name: "run".into(), args: "--all".into() })
        );
        assert_eq!(
            parse_slash_input(":doctor"),
            Some(ParsedSlash { name: "doctor".into(), args: String::new() })
        );
        assert_eq!(
            parse_slash_input("  /memory  cuda oom  "),
            Some(ParsedSlash { name: "memory".into(), args: "cuda oom".into() })
        );
        assert_eq!(parse_slash_input("run --all"), None);
        assert_eq!(parse_slash_input("/"), None);
    }

    #[test]
    fn completes_command_names_and_arguments() {
        let mut knowledge = SessionKnowledge::default();
        knowledge.note_artifact_id(Some("art-abc"));
        knowledge.note_artifact_id(Some("art-xyz"));
        knowledge.note_artifact_id(None);

        assert_eq!(complete("/ru", &knowledge), vec!["/run"]);
        assert!(complete("/", &knowledge).len() == SLASH_COMMANDS.len());
        assert_eq!(complete("/run fea", &knowledge), vec!["/run features"]);
        assert_eq!(complete("/run --", &knowledge), vec!["/run --all"]);
        assert_eq!(complete("/explain art-a", &knowledge), vec!["/explain art-abc"]);
        assert_eq!(complete("/theme mo", &knowledge), vec!["/theme mono"]);
        // A free-text argument has nothing real to suggest, so it suggests
        // nothing — rather than inventing candidates.
        assert!(complete("/memory cuda", &knowledge).is_empty());
        assert!(complete("/nosuch thing", &knowledge).is_empty());
    }

    #[test]
    fn no_command_exists_without_a_backend_capability() {
        for forbidden in ["sessions", "claims", "resume", "tree"] {
            assert!(
                lookup(forbidden).is_none(),
                "/{forbidden} has no real read API behind it and must stay absent"
            );
        }
    }

    #[test]
    fn free_text_matches_only_two_fixed_patterns() {
        assert_eq!(
            match_free_text_intent("run everything"),
            Some(ParsedSlash { name: "run".into(), args: "--all".into() })
        );
        assert_eq!(
            match_free_text_intent("please diagnose my setup"),
            Some(ParsedSlash { name: "doctor".into(), args: String::new() })
        );
        assert_eq!(match_free_text_intent("what happened to features?"), None);
        assert_eq!(match_free_text_intent("tell me about reuse"), None);
    }
}
