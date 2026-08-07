/**
 * Domain-aware argument sources for slash-command autocomplete (Part B
 * §29). `PIPELINE_STAGES` is Cairn's real, fixed five-stage DAG
 * (PROJECT.md §4.3 / src/cairn/planner.py's STAGES) — stable and safe to
 * hardcode, not something that needs a live query. `SessionKnowledge`
 * tracks artifact ids this *session* has actually seen via real events,
 * so `/explain` can suggest them — never a fabricated id.
 */

export const PIPELINE_STAGES = ["env", "dataset", "features", "checkpoint", "eval"] as const;

export const THEME_NAMES = ["cairn-dark", "cairn-light", "mono"] as const;

export class SessionKnowledge {
	private artifactIds = new Set<string>();

	noteArtifactId(artifactId: string | undefined | null): void {
		if (artifactId) this.artifactIds.add(artifactId);
	}

	knownArtifactIds(): string[] {
		return [...this.artifactIds];
	}
}
