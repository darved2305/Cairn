/**
 * A minimal typed pub/sub bus. Used both for the real backend event stream
 * (subprocess.ts feeds CairnEvent objects into one of these) and for
 * purely local UI signals (activity-tracker.ts's derived activity state,
 * animation-clock.ts's frame ticks) — one small primitive instead of
 * pulling in Node's untyped EventEmitter everywhere.
 */
export class Emitter<T> {
	private listeners = new Set<(value: T) => void>();

	on(listener: (value: T) => void): () => void {
		this.listeners.add(listener);
		return () => this.listeners.delete(listener);
	}

	emit(value: T): void {
		for (const listener of this.listeners) listener(value);
	}

	clear(): void {
		this.listeners.clear();
	}
}
