/**
 * The scrolling history of user turns, assistant narration, and tool
 * panels — a thin VStack wrapper (pi-tui already gives us differential
 * rendering and layout; Transcript's only job is "append a component,
 * keep a spacer between entries").
 */

import { VStack, Spacer } from "@earendil-works/pi-tui";
import type { Component } from "@earendil-works/pi-tui";

export class Transcript implements Component {
	private stack: VStack;

	constructor() {
		this.stack = new VStack([], { gap: 0 });
	}

	append(component: Component): void {
		if (this.stack.children.length > 0) {
			this.stack.addChild(new Spacer(1));
		}
		this.stack.addChild(component);
	}

	invalidate(): void {
		this.stack.invalidate();
	}

	render(width: number): string[] {
		return this.stack.render(width);
	}
}
