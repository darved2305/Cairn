//! The scrollback. In the old TypeScript TUI this *was* the whole
//! application; here it is one panel of six, holding the narration and the
//! raw event/stderr log that the structured panels deliberately do not try
//! to squeeze in.

/// Capped ring buffer bound — a long `run --all` against real infra emits
/// thousands of lines and none of them should grow the process without
/// limit.
const TRANSCRIPT_CAP: usize = 2000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TranscriptKind {
    /// Something the operator typed.
    User,
    /// Fixed copy attached to a real domain transition. Never a token
    /// stream — there is no LLM in this process.
    Narration,
    /// A one-line rendering of a real backend event.
    Event,
    /// A line of the child's stderr.
    Stderr,
    /// A local failure (spawn failed, unknown command, bad usage).
    Error,
    /// Local informational output (`/status`, `/usage`, `/model`).
    Info,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TranscriptEntry {
    pub kind: TranscriptKind,
    pub text: String,
    /// ISO8601 from the event envelope when there is one, else empty —
    /// this crate does no I/O, so it never reads a clock itself.
    pub timestamp: String,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct TranscriptState {
    /// Oldest first, so the panel reads top-to-bottom like a log.
    pub entries: Vec<TranscriptEntry>,
}

impl TranscriptState {
    pub fn push(&mut self, kind: TranscriptKind, text: impl Into<String>, timestamp: &str) {
        self.entries.push(TranscriptEntry {
            kind,
            text: text.into(),
            timestamp: timestamp.to_string(),
        });
        if self.entries.len() > TRANSCRIPT_CAP {
            let excess = self.entries.len() - TRANSCRIPT_CAP;
            self.entries.drain(0..excess);
        }
    }

    pub fn clear(&mut self) {
        self.entries.clear();
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}
