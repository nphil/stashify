/* One short message at a time, shown at the bottom, gone by itself. */

class Toast {
  message = $state<string | null>(null);
  #timer: ReturnType<typeof setTimeout> | undefined;

  show(message: string, ms = 2800): void {
    this.message = message;
    clearTimeout(this.#timer);
    this.#timer = setTimeout(() => { this.message = null; }, ms);
  }
  hide(): void {
    clearTimeout(this.#timer);
    this.message = null;
  }
}

export const toast = new Toast();
