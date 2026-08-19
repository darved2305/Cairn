# Cairn documentation

Start with the [repository README](../README.md) for what Cairn is and how to
run it. Everything below is depth.

## Architecture

| Document | What it covers |
| --- | --- |
| [architecture/OVERVIEW.md](architecture/OVERVIEW.md) | System design, component boundaries, the invariants that hold across them, and what one `cairn run` actually does |
| [architecture/SUBSTRATES.md](architecture/SUBSTRATES.md) | Every CockroachDB and AWS capability Cairn depends on, what each one does, and what breaks without it |

## Internals

| Document | What it covers |
| --- | --- |
| [internals/PROBES.md](internals/PROBES.md) | The six probes: each one's guarantee **and** its explicit non-guarantee |

## Operations

| Document | What it covers |
| --- | --- |
| [operations/COST.md](operations/COST.md) | Spend guardrails, why there is no NAT Gateway anywhere, and the emergency stop |

## Security

| Document | What it covers |
| --- | --- |
| [security/SECURITY_MODEL.md](security/SECURITY_MODEL.md) | Where each boundary is enforced, and what Cairn deliberately does not defend against |
| [../SECURITY.md](../SECURITY.md) | Reporting a vulnerability; credential handling in this repository |

## Project record

Design rationale and build history. These are the documents the implementation
was written against, not a description of it — where they disagree with the
code, the code wins.

| Document | What it covers |
| --- | --- |
| [project/PROJECT.md](project/PROJECT.md) | The authoritative design, including the full data model in §11 |
| [project/PLAN.md](project/PLAN.md) | The original day-by-day build plan and its exit criteria |
| [project/WINNING_PLAN_9_DAY.md](project/WINNING_PLAN_9_DAY.md) | The Flight Recorder redesign: scope decisions, identity model, claim state machine, schema changes |
| [project/SKILLS_USAGE.md](project/SKILLS_USAGE.md) | Which CockroachDB Agent Skills informed which files, and the concrete changes they caused |
| [project/VALIDATION_2026-08-09.md](project/VALIDATION_2026-08-09.md) | An adversarial end-to-end validation log against real infrastructure. Observed results only |

## Assets

`assets/diagrams/` holds every README diagram as an `.excalidraw` source
alongside the exported `.svg`. Open the `.excalidraw` file in excalidraw.com,
edit, save it back over the source, then re-export and commit both:

```bash
uv run python scripts/excalidraw_to_svg.py docs/assets/diagrams/*.excalidraw
```

`assets/tui/tui-overview.txt` is the terminal frame the README embeds. It comes
from the TUI's own renderer; regenerate it with the `layout_snapshot` test:

```bash
cd tui-rs && cargo test -p cairn-tui -- --ignored --nocapture layout_snapshot
```
