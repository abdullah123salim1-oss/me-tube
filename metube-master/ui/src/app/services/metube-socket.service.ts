import { Injectable, NgZone, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, Subject, firstValueFrom } from 'rxjs';

// The stock MeTube UI talks to its server over Socket.IO. This deployment runs
// on Vercel serverless functions, which cannot hold WebSocket connections, so
// this service keeps the exact same `fromEvent(...)` interface the app expects
// and feeds it from a lightweight `GET /api/snapshot` poll: each snapshot is
// diffed against the previous one and replayed as the same events the Socket.IO
// server emitted (all / added / updated / completed / canceled / cleared / ...).
//
// It also acts as the queue heartbeat: whenever the snapshot shows work in
// 'queued' state, it fires POST /api/tick — the request that actually runs the
// yt-dlp downloads on the server.

interface Snapshot {
  queue: [string, unknown][];
  done: [string, unknown][];
  formats: unknown;
  configuration: unknown;
  custom_dirs: unknown;
  subscriptions: unknown[];
}

@Injectable({ providedIn: 'root' })
export class MeTubeSocket {
  private http = inject(HttpClient);
  private zone = inject(NgZone);

  private listeners = new Map<string, Subject<string>>();
  private prevQueue = new Map<string, string>();
  private prevDone = new Map<string, string>();
  private prevSubsJson = '[]';
  private initialized = false;
  private polling = false;
  private lastTickAt = 0;

  private static readonly POLL_INTERVAL_MS = 2500;
  private static readonly TICK_THROTTLE_MS = 10000;

  constructor() {
    window.setTimeout(() => void this.poll());
    window.setInterval(() => void this.poll(), MeTubeSocket.POLL_INTERVAL_MS);
  }

  fromEvent(event: string): Observable<string> {
    let subject = this.listeners.get(event);
    if (!subject) {
      subject = new Subject<string>();
      this.listeners.set(event, subject);
    }
    return subject.asObservable();
  }

  private emit(event: string, data: string): void {
    this.listeners.get(event)?.next(data);
  }

  private async poll(): Promise<void> {
    if (this.polling) {
      return;
    }
    this.polling = true;
    try {
      const snapshot = await firstValueFrom(
        this.http.get<Snapshot>('/api/snapshot'),
      );
      if (snapshot) {
        this.zone.run(() => this.apply(snapshot));
      }
    } catch {
      // Transient network/server failure: keep polling; the UI stays as-is.
    } finally {
      this.polling = false;
    }
  }

  private apply(snapshot: Snapshot): void {
    const queuePairs = snapshot.queue || [];
    const donePairs = snapshot.done || [];
    const queue = new Map<string, string>(
      queuePairs.map(([k, v]) => [k, JSON.stringify(v)]),
    );
    const done = new Map<string, string>(
      donePairs.map(([k, v]) => [k, JSON.stringify(v)]),
    );

    if (!this.initialized) {
      this.initialized = true;
      this.emit('formats', JSON.stringify(snapshot.formats ?? {}));
      this.emit('configuration', JSON.stringify(snapshot.configuration ?? {}));
      this.emit('custom_dirs', JSON.stringify(snapshot.custom_dirs ?? {}));
      this.emit('subscriptions_all', JSON.stringify(snapshot.subscriptions ?? []));
      this.emit('all', JSON.stringify([queuePairs, donePairs]));
      this.prevQueue = queue;
      this.prevDone = done;
      this.prevSubsJson = JSON.stringify(snapshot.subscriptions ?? []);
      this.emit('connect', '');
      this.maybeTick(queuePairs);
      return;
    }

    // Diff queue rows -> added / updated / completed / canceled.
    for (const [key, value] of queue) {
      const prev = this.prevQueue.get(key);
      if (prev === undefined) {
        this.emit('added', value);
      } else if (prev !== value) {
        this.emit('updated', value);
      }
    }
    for (const key of this.prevQueue.keys()) {
      if (queue.has(key)) {
        continue;
      }
      const doneValue = done.get(key);
      if (doneValue !== undefined) {
        this.emit('completed', doneValue);
      } else {
        this.emit('canceled', JSON.stringify(key));
      }
    }
    // Diff done rows -> cleared.
    for (const key of this.prevDone.keys()) {
      if (!done.has(key)) {
        this.emit('cleared', JSON.stringify(key));
      }
    }

    const subsJson = JSON.stringify(snapshot.subscriptions ?? []);
    if (subsJson !== this.prevSubsJson) {
      this.prevSubsJson = subsJson;
      this.emit('subscriptions_all', subsJson);
    }

    this.prevQueue = queue;
    this.prevDone = done;
    this.maybeTick(queuePairs);
  }

  private maybeTick(queuePairs: [string, unknown][]): void {
    const hasWork = queuePairs.some(([, info]) => {
      const status = (info as { status?: string } | null)?.status;
      return status === 'queued' || status === 'preparing';
    });
    if (!hasWork) {
      return;
    }
    const now = Date.now();
    if (now - this.lastTickAt < MeTubeSocket.TICK_THROTTLE_MS) {
      return;
    }
    this.lastTickAt = now;
    // Fire-and-forget: this call blocks on the server while downloads run;
    // polling continues in parallel and surfaces the progress.
    this.http.post('/api/tick', {}).subscribe({ error: () => undefined });
  }
}
