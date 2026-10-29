export class CoalescedInvalidation {
  private timer: ReturnType<typeof setTimeout> | undefined;
  private running = false;
  private dirty = false;
  private closed = false;
  constructor(
    private refresh: () => Promise<unknown>,
    private delay = 300,
  ) {}
  request() {
    if (this.closed) return;
    this.dirty = true;
    if (!this.running && !this.timer)
      this.timer = setTimeout(() => {
        this.timer = undefined;
        void this.flush();
      }, this.delay);
  }
  private async flush() {
    if (this.closed || !this.dirty) return;
    this.running = true;
    this.dirty = false;
    try {
      await this.refresh();
    } catch {
      /* GET errors remain in the query cache; reconnect/poll will retry. */
    } finally {
      this.running = false;
      if (this.dirty) this.request();
    }
  }
  close() {
    this.closed = true;
    clearTimeout(this.timer);
  }
}
