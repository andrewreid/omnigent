// Replays a busy session-updates stream against the real sidebar queries and
// counts how often each session list is re-read. Membership changes are rare
// (a create, rename and archive every couple of minutes), so the lists should
// be re-read at about that rate plus the once-a-minute activity round, not on
// every running session's tick.

import { act, cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { useEffect, useMemo } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const setWatched = vi.fn();
const subscribe = vi.fn((_listener: (frame: unknown) => void) => () => {});
vi.mock("@/lib/sessionUpdatesSocket", () => ({
  sessionUpdatesSocket: {
    start: vi.fn(),
    stop: vi.fn(),
    setWatched: (ids: string[]) => setWatched(ids),
    subscribe: (fn: (frame: unknown) => void) => subscribe(fn),
    subscribeStatus: () => () => {},
    isConnected: () => true,
  },
}));

import { clearSessionTombstones, useProjectSessions, type Conversation } from "./useConversations";
import { clearRecentlyCreated } from "@/lib/sessionListCache";
import { SidebarDataProvider, useSidebarData } from "./useSidebarData";
import { SessionUpdatesProvider } from "./SessionUpdatesProvider";

type Row = Conversation & { parent_session_id?: string | null; archived?: boolean };

const PROJECTS = ["alpha", "beta", "gamma", "delta"];
const SIM_MINUTES = 10;
const TICK_MS = 2_000;

/** In-memory `GET /v1/sessions` plus the stream's per-tick diff. */
class FakeSessionServer {
  rows = new Map<string, Row>();
  fetches = new Map<string, number>();
  private watched: string[] = [];
  private lastSent = new Map<string, string>();
  private nextId = 0;

  add(fields: Partial<Row>): Row {
    const id = `s${String(this.nextId++).padStart(4, "0")}`;
    const row: Row = {
      id,
      object: "conversation",
      title: `session ${id}`,
      created_at: this.now(),
      updated_at: this.now(),
      labels: {},
      permission_level: null,
      owner: null,
      status: "idle",
      archived: false,
      parent_session_id: null,
      pending_elicitations_count: 0,
      background_work_running: false,
      blocked_on: null,
      shared_with_others: null,
      runner_online: null,
      host_online: null,
      ...fields,
    };
    this.rows.set(id, row);
    return row;
  }

  now(): number {
    return Math.floor(Date.now() / 1000);
  }

  bump(id: string, fields: Partial<Row> = {}): void {
    const row = this.rows.get(id);
    if (row) this.rows.set(id, { ...row, ...fields, updated_at: this.now() });
  }

  list(url: URL): object {
    const params = url.searchParams;
    const visibility = params.get("visibility");
    const project = params.get("project");
    const limit = Number(params.get("limit"));
    const shape = params.get("pinned")
      ? "pinned"
      : project
        ? `project:${project}`
        : `${visibility}:${limit}`;
    this.fetches.set(shape, (this.fetches.get(shape) ?? 0) + 1);
    const rows = [...this.rows.values()]
      .filter((row) => !row.parent_session_id && !row.archived)
      .filter((row) => {
        if (params.get("pinned")) return "omnigent.pinned" in row.labels;
        const mine = row.permission_level == null;
        if (visibility === "mine" && !mine) return false;
        if (visibility === "shared" && mine) return false;
        return !project || row.labels.omni_project === project;
      })
      .sort((a, b) => b.updated_at - a.updated_at || b.id.localeCompare(a.id));
    const after = params.get("after");
    const start = after ? rows.findIndex((row) => row.id === after) + 1 : 0;
    // The list drops null fields (exclude_none) and computes no liveness.
    const data = rows
      .slice(start, start + limit)
      .map(
        (row) =>
          Object.fromEntries(Object.entries(row).filter(([, value]) => value != null)) as Row,
      );
    const last = data.at(-1);
    return {
      object: "list",
      data,
      first_id: data[0]?.id ?? null,
      last_id: last?.id ?? null,
      has_more: start + limit < rows.length,
      page_boundary: last
        ? { id: last.id, created_at: last.created_at, updated_at: last.updated_at }
        : null,
    };
  }

  setWatched(ids: string[]): void {
    // The socket only sends a watch (and gets a snapshot) when the set changes.
    if ([...ids].sort().join() === [...this.watched].sort().join()) return;
    this.watched = ids;
    this.lastSent.clear();
    const items = ids.flatMap((id) => (this.rows.has(id) ? [this.wire(id)] : []));
    for (const item of items) this.lastSent.set(item.id, JSON.stringify(item));
    this.send({ type: "snapshot", items });
  }

  /** One server rescan: send the watched rows that changed since the last frame. */
  tick(): void {
    const changed = this.watched.flatMap((id) => {
      if (!this.rows.has(id)) return [];
      const item = this.wire(id);
      const dump = JSON.stringify(item);
      if (this.lastSent.get(id) === dump) return [];
      this.lastSent.set(id, dump);
      return [item];
    });
    if (changed.length) this.send({ type: "changed", items: changed });
  }

  /** A create elsewhere: the server pushes the new row before it is watched. */
  announce(id: string): void {
    this.send({ type: "changed", items: [this.wire(id)] });
  }

  private wire(id: string) {
    const row = this.rows.get(id)!;
    // Stream rows are full dumps, nulls included, with liveness filled in.
    return { ...row, runner_online: true, host_online: true };
  }

  private send(frame: unknown): void {
    const handler = subscribe.mock.calls.at(-1)?.[0];
    if (handler) act(() => handler(frame));
  }
}

function Folder({ name }: { name: string }) {
  const { registerFolder } = useSidebarData();
  const query = useProjectSessions(name, true);
  const rows = useMemo(() => query.data?.pages.flatMap((page) => page.data) ?? [], [query.data]);
  useEffect(() => registerFolder(name, rows), [name, rows, registerFolder]);
  return null;
}

afterEach(() => {
  cleanup();
  clearSessionTombstones();
  clearRecentlyCreated();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function simulate({ running }: { running: number }) {
  vi.useFakeTimers({ now: new Date("2026-11-07T12:00:00Z") });
  const server = new FakeSessionServer();
  // History: owned sessions spread over the folders and unfiled, plus some
  // shared ones; the newest `running` owned roots keep working throughout.
  for (let i = 0; i < 160; i += 1) {
    vi.setSystemTime(Date.now() + 60_000);
    server.add({ labels: i % 3 === 0 ? {} : { omni_project: PROJECTS[i % PROJECTS.length] } });
  }
  for (let i = 0; i < 40; i += 1) {
    vi.setSystemTime(Date.now() + 60_000);
    server.add({ owner: "someone-else", permission_level: 1 });
  }
  const owned = [...server.rows.values()].filter((row) => row.permission_level == null);
  const runningIds = owned.slice(-running).map((row) => row.id);

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://test");
      if (url.pathname === "/v1/sessions") {
        const body = server.list(url);
        return new Response(JSON.stringify(body), { status: 200 });
      }
      return new Response(JSON.stringify({ data: [], has_more: false }), { status: 200 });
    }),
  );
  setWatched.mockImplementation((ids: string[]) => server.setWatched(ids));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SidebarDataProvider>
          <SessionUpdatesProvider>
            {PROJECTS.map((name) => (
              <Folder key={name} name={name} />
            ))}
          </SessionUpdatesProvider>
        </SidebarDataProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await act(() => vi.advanceTimersByTimeAsync(5_000));
  server.fetches.clear();

  let children = 0;
  const ticks = (SIM_MINUTES * 60_000) / TICK_MS;
  for (let tick = 1; tick <= ticks; tick += 1) {
    // Each running root (and its sub-agent) reports progress every other tick.
    for (const [index, id] of runningIds.entries()) {
      if ((tick + index) % 2 === 0) server.bump(id, { status: "running" });
    }
    if (tick % 5 === 0) {
      const parent = runningIds[tick % runningIds.length];
      const child = server.add({ parent_session_id: parent, title: `sub-agent ${children++}` });
      server.announce(child.id);
    }
    // About once a minute each: a new root in a folder, a rename, an archive.
    if (tick % 30 === 0) {
      const created = server.add({ labels: { omni_project: PROJECTS[0] } });
      server.announce(created.id);
    }
    if (tick % 30 === 10) server.bump(runningIds[tick % runningIds.length], { title: `t${tick}` });
    if (tick % 60 === 20) {
      const oldest = owned.find((row) => !server.rows.get(row.id)?.archived);
      if (oldest) server.bump(oldest.id, { archived: true });
    }
    server.tick();
    // Small steps let React render between timers, as a browser would.
    for (let elapsed = 0; elapsed < TICK_MS; elapsed += 100) {
      // oxlint-disable-next-line no-await-in-loop
      await act(() => vi.advanceTimersByTimeAsync(100));
    }
  }
  const perMinute = Object.fromEntries(
    [...server.fetches].map(([shape, count]) => [shape, count / SIM_MINUTES]),
  );
  const mineRows = client
    .getQueryData<{ pages: { data: unknown[] }[] }>(["conversations", "", false, null, "mine"])!
    .pages.flatMap((page) => page.data).length;
  return { perMinute, mineRows };
}

describe("sidebar list reads under a busy session-updates stream", () => {
  // 5 running roots all sit inside every loaded window; 40 outnumber Mine's
  // 30-row window, so one of them is always the row Mine's cursor names.
  it.each([5, 40])(
    "re-reads each list about once a minute with %i running roots",
    async (running) => {
      const { perMinute, mineRows } = await simulate({ running });

      // One activity round a minute, which the minute's new session joins.
      expect(Object.keys(perMinute).sort()).toEqual(
        ["mine:30", "shared:30", ...PROJECTS.map((name) => `project:${name}`)].sort(),
      );
      for (const rate of Object.values(perMinute)) expect(rate).toBeLessThanOrEqual(1.5);
      // Folder rows outside Mine's window stay out of Mine's cache.
      expect(mineRows).toBeLessThanOrEqual(40);
    },
  );
});
