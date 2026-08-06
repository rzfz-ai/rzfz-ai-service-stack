"""
GPUStack 2.x bus.py runtime backport — fixes the high-QPS memory leak documented
in upstream issues #5121, #5073, and PR #5255 (open against gpustack/gpustack@main,
not yet released in v2.1.x).

This is a SURGICAL patch — applies only the 3 leak fixes from PR #5255 onto
v2.1.2's Subscriber + EventBus classes via runtime monkey-patch. Does NOT pull
in the unrelated refactors that come with the main-branch version of bus.py.

Three leaks fixed:

  1. ``Subscriber.latest_by_key`` strands entries on QueueFull. v2.1.2 sets
     the dict entry BEFORE ``put_nowait``; if the put raises QueueFull the
     entry stays. Subsequent UPDATED events for the same id short-circuit on
     ``if event.id in latest_by_key`` and never put. Permanent entry per id.
     Fix: pop the latest_by_key entry on QueueFull.

  2. Publisher tasks pin ghost subscribers. ``EventBus.unsubscribe`` only
     ``.remove(subscriber)``; it doesn't drain the queue or cancel parked
     putters. Tasks blocked on ``queue.put`` (full queue) keep the
     subscriber + 1024 queued events alive. Fix: ``Subscriber.close()``
     drains the queue and cancels parked putters; ``unsubscribe`` calls it.

  3. ``Subscriber.enqueue`` doesn't check if subscriber is closed. After
     unsubscribe, in-flight publish tasks can still push events into the
     dead subscriber's queue, retaining payloads. Fix: short-circuit on
     ``_closed``.

Mount this file the same way as usercustomize.py — auto-loaded at process start.

NOTE: This is a STOPGAP. The right long-term answer is one of:
  (a) Upstream merges PR #5255 and ships v2.1.3 / v2.2.0; we bump.
  (b) Pin gpustack to v2.0.x where these leaks don't exist.
  (c) Ship the legacy llm-legacy profile (gpustack 0.7.1) as fallback.
"""

from __future__ import annotations
import asyncio
import sys


def _install_bus_leak_fix() -> None:
    try:
        from gpustack.server import bus as _bus
    except Exception as e:
        print(
            f"[usercustomize_bus_fix] skipped — gpustack.server.bus not importable ({e!r})",
            file=sys.stderr,
        )
        return

    Subscriber = _bus.Subscriber
    EventBus = _bus.EventBus

    # ── 1. Patch Subscriber.__init__ to add _closed flag ──────────────────
    original_init = Subscriber.__init__

    def patched_init(self):
        original_init(self)
        self._closed = False

    Subscriber.__init__ = patched_init

    # ── 2. Replace Subscriber.enqueue with leak-fixed version ─────────────
    EventType = _bus.EventType

    async def patched_enqueue(self, event):
        # Short-circuit on closed (Leak fix #3): drop without taking the
        # lock or touching latest_by_key so we don't strand entries that
        # nobody will ever pop.
        if getattr(self, "_closed", False):
            return

        # Squash UPDATED events by keeping only the latest per key
        if event.type == EventType.UPDATED and event.id is not None:
            async with self.lock:
                if event.id in self.latest_by_key:
                    self.latest_by_key[event.id] = event
                    return
                self.latest_by_key[event.id] = event

            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Leak fix #1: the dict entry was set ABOVE; if the put fails,
                # we MUST pop it back out so subsequent events for this id
                # don't short-circuit on the dict membership check.
                async with self.lock:
                    self.latest_by_key.pop(event.id, None)
                # Logger is in the original module's namespace; use its name.
                _bus.logger.warning(
                    "Subscriber:%s queue full, dropping UPDATED event for id=%s "
                    "(patched: latest_by_key entry popped to prevent leak)",
                    id(self),
                    event.id,
                )
            return

        # For other event types, enqueue directly
        await self.queue.put(event)

    Subscriber.enqueue = patched_enqueue

    # ── 3. Add Subscriber.close (Leak fix #2) ─────────────────────────────
    def patched_close(self):
        """Mark closed and release every event still tied to this subscriber.

        Two distinct holders to clear:
          (1) Events already enqueued — sitting in self.queue.
          (2) Events still waiting to enqueue — held by ``put`` callers
              parked on Queue._putters because the queue was full.

        Both passes are sync and O(N), so unsubscribe never blocks.
        Idempotent.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        # (1) Drain queue
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # (2) Cancel parked putters. _putters is private but stable since 3.4.
        putters = getattr(self.queue, "_putters", None)
        if putters is not None:
            for putter in list(putters):
                if not putter.done():
                    putter.cancel()
            putters.clear()

    Subscriber.close = patched_close

    # ── 4. Patch EventBus.unsubscribe to call close() ─────────────────────
    original_unsubscribe = EventBus.unsubscribe

    def patched_unsubscribe(self, topic, subscriber):
        if topic in self.subscribers:
            try:
                self.subscribers[topic].remove(subscriber)
            except ValueError:
                pass
            if not self.subscribers[topic]:
                del self.subscribers[topic]
        try:
            subscriber.close()
        except Exception as e:
            print(
                f"[usercustomize_bus_fix] subscriber.close() failed during "
                f"unsubscribe ({e!r}); leaking is preferable to crashing",
                file=sys.stderr,
            )

    EventBus.unsubscribe = patched_unsubscribe

    print(
        "[usercustomize_bus_fix] active: backported gpustack PR #5255 leak fixes "
        "(Subscriber.enqueue pops latest_by_key on QueueFull; Subscriber.close "
        "drains queue + cancels parked putters; EventBus.unsubscribe calls close)",
        file=sys.stderr,
    )


_install_bus_leak_fix()
