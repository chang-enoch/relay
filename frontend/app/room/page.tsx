"use client";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { Brand, RelayMark } from "@/components/Brand";
import { AskRelayPanel } from "@/components/AskRelayPanel";
import { MessageCard } from "@/components/MessageCard";
import { Avatar, byId } from "@/components/util";
import { clearCreds, Creds, loadCreds } from "@/lib/api";
import { OUTSTANDING } from "@/lib/types";
import { useRoom } from "@/lib/useRoom";

export default function RoomPage() {
  const router = useRouter();
  const [creds, setCreds] = useState<Creds | null>(null);
  useEffect(() => {
    const c = loadCreds();
    if (!c) router.replace("/");
    else setCreds(c); // eslint-disable-line react-hooks/set-state-in-effect
  }, [router]);
  const room = useRoom(creds);
  const { snap } = room;

  const [filter, setFilter] = useState<"all" | "outstanding">("all");
  const [flash, setFlash] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [nowIso, setNowIso] = useState(""); // ticks so expired clarifications drop off
  useEffect(() => {
    const tick = () => setNowIso(new Date().toISOString());
    tick();
    const t = setInterval(tick, 5000);
    return () => clearInterval(t);
  }, []);
  const feedEnd = useRef<HTMLDivElement>(null);

  const people = useMemo(() => byId(snap?.participants ?? []), [snap?.participants]);
  const me = snap ? people[snap.me] : undefined;
  const bySource = useMemo(() => {
    const map: Record<string, NonNullable<typeof snap>["requests"]> = {};
    for (const r of snap?.requests ?? []) if (r.source_message_id) (map[r.source_message_id] ??= []).push(r);
    return map;
  }, [snap?.requests]);
  const reqById = useMemo(() => Object.fromEntries((snap?.requests ?? []).map((r) => [r.id, r])), [snap?.requests]);
  const outstandingCount = (snap?.messages ?? []).filter((m) => (bySource[m.id] ?? []).some((r) => OUTSTANDING.includes(r.status))).length;
  const visible = (snap?.messages ?? []).filter((m) =>
    filter === "all" || (bySource[m.id] ?? []).some((r) => OUTSTANDING.includes(r.status)));

  type FeedItem = { kind: "message"; at: string; m: (typeof visible)[number] } | { kind: "clarification"; at: string; c: NonNullable<typeof snap>["clarifications"][number] };
  const feed: FeedItem[] = [
    ...visible.map((m) => ({ kind: "message" as const, at: m.timestamp, m })),
    ...(filter === "all" ? (snap?.clarifications ?? []).filter((c) => !nowIso || !c.expires_at || c.expires_at > nowIso)
      .map((c) => ({ kind: "clarification" as const, at: c.created_at, c })) : []),
  ].sort((a, b) => a.at.localeCompare(b.at));

  const activityCount = Object.keys(room.activity).length;
  useEffect(() => { feedEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [snap?.messages.length, snap?.clarifications.length, activityCount]);

  // Space = hold to talk (not while typing).
  const { pressTalk, releaseTalk } = room;
  useEffect(() => {
    const typing = (e: KeyboardEvent) => (e.target as HTMLElement)?.closest("input,textarea");
    const down = (e: KeyboardEvent) => { if (e.code === "Space" && !e.repeat && !typing(e)) { e.preventDefault(); pressTalk(); } };
    const up = (e: KeyboardEvent) => { if (e.code === "Space" && !typing(e)) { e.preventDefault(); releaseTalk(); } };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", releaseTalk);
    return () => { window.removeEventListener("keydown", down); window.removeEventListener("keyup", up); window.removeEventListener("blur", releaseTalk); };
  }, [pressTalk, releaseTalk]);

  // Inline status messages clear themselves.
  const { notices, dismiss } = room;
  useEffect(() => {
    if (!notices.length) return;
    const t = setTimeout(() => dismiss(notices[0].id), 5000);
    return () => clearTimeout(t);
  }, [notices, dismiss]);

  const jump = (id: string) => {
    setFilter("all");
    setTimeout(() => document.getElementById(`msg-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 50);
    setFlash(id);
    setTimeout(() => setFlash(null), 1600);
  };

  const leave = () => { clearCreds(); router.replace("/"); };

  if (room.closed) {
    return (
      <>
        <Brand />
        <main className="grid flex-1 place-items-center p-8 text-center">
          <div>
            <h1 className="text-2xl font-bold">This room has closed</h1>
            <p className="mt-2 text-muted">Its code no longer works.</p>
            <button className="btn btn-primary mt-6" onClick={leave}>Back to start</button>
          </div>
        </main>
      </>
    );
  }
  if (!snap || !me) {
    return (<><Brand /><main className="grid flex-1 place-items-center text-muted">Connecting…</main></>);
  }

  const speakerId = snap.turn.speaker?.participant_id;
  const speaker = speakerId ? people[speakerId] : undefined;
  const queuePos = snap.turn.queue.indexOf(snap.me);
  const askOpen = room.openExchange !== null;
  const exchange = room.openExchange && room.openExchange !== "new" && room.openExchange !== "idle"
    ? snap.exchanges.find((x) => x.id === room.openExchange) ?? null : null;
  const canManage = me.permissions.includes("manage_requests");
  const others = snap.participants.filter((p) => p.id !== snap.me);
  const allMuted = others.length > 0 && others.every((p) => room.muted.has(p.id));
  const lastWarning = room.notices.at(-1);

  const pills: [string, string, string | undefined][] = [
    ["Server", room.connected ? "ok" : "error", room.connected ? undefined : "Reconnecting to Relay"],
    ["Audio", !snap.providers.audio ? "off" : room.audio.state === "ok" ? "ok" : room.audio.state === "error" ? "error" : "unknown", room.audio.detail],
    ["Transcription", !snap.providers.transcription ? "off" : room.status.transcription.state, room.status.transcription.detail],
    ["AI", !snap.providers.ai ? "off" : room.status.ai.state, room.status.ai.detail],
    ["Speech", !snap.providers.speech ? "off" : room.status.speech.state, room.status.speech.detail],
  ];

  return (
    <div className="flex h-dvh flex-col">
      <Brand right={
        <div className="flex flex-wrap justify-end gap-1.5">
          {pills.map(([label, st, detail]) => (
            <span key={label} title={detail ?? (st === "off" ? "Not configured on the server" : st)}
              className="flex items-center gap-1.5 rounded-full border border-line bg-card px-2.5 py-1 text-xs text-muted">
              <span className={`h-2 w-2 rounded-full ${st === "ok" ? "bg-moss" : st === "error" ? "bg-danger" : st === "off" ? "bg-line" : "bg-amber-bar"}`} />
              <span className="hidden sm:inline">{label}</span>
            </span>
          ))}
        </div>
      } />

      <div className="flex min-h-0 flex-1">
        {/* Sidebar */}
        <aside className="hidden w-72 shrink-0 flex-col overflow-y-auto border-r border-line bg-sage px-7 py-8 lg:flex">
          <p className="label">Room code</p>
          <p className="mt-2 text-4xl font-bold tracking-wide">{snap.session.room_code}</p>
          <button className="btn mt-4 w-fit" onClick={() => navigator.clipboard?.writeText(snap.session.room_code)}>Copy code</button>

          <div className="mt-10 flex items-center justify-between">
            <p className="label">People in room</p>
            {others.length > 0 && (
              <button className="text-xs text-muted hover:text-ink" onClick={() => room.setMuted(others.map((p) => p.id), !allMuted)}>
                {allMuted ? "Unmute all" : "Mute all"}
              </button>
            )}
          </div>
          <ul className="mt-4 space-y-5">
            {snap.participants.map((p) => (
              <li key={p.id} className="flex items-center gap-3">
                <div className="relative">
                  <Avatar id={p.id} name={p.display_name} size={44} />
                  <span className={`absolute -bottom-0.5 -right-0.5 h-3 w-3 rounded-full border-2 border-sage ${p.online ? "bg-moss" : "bg-line"}`} />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate font-bold">{p.display_name}{p.id === snap.me && <span className="font-normal text-muted"> (you)</span>}</p>
                  <p className="truncate text-sm text-muted">
                    {p.id === speakerId ? (room.muted.has(p.id) ? "speaking (muted for you)" : "🎙 speaking…") : p.role}
                  </p>
                </div>
                {p.id !== snap.me && (
                  <button onClick={() => room.setMuted([p.id], !room.muted.has(p.id))}
                    title={room.muted.has(p.id) ? `Unmute ${p.display_name}` : `Mute ${p.display_name} for you`}
                    aria-label={room.muted.has(p.id) ? `Unmute ${p.display_name}` : `Mute ${p.display_name}`}
                    aria-pressed={room.muted.has(p.id)}
                    className={`grid h-8 w-8 shrink-0 place-items-center rounded-full border ${room.muted.has(p.id) ? "border-danger/40 bg-card text-danger" : "border-transparent text-muted hover:border-line hover:bg-card hover:text-ink"}`}>
                    <SpeakerIcon off={room.muted.has(p.id)} />
                  </button>
                )}
              </li>
            ))}
          </ul>

          {snap.session.setting && (<><p className="label mt-10">Room context</p><p className="mt-3 whitespace-pre-line text-muted">{snap.session.setting}</p></>)}

          <div className="mt-auto space-y-2 pt-10">
            <button className="btn w-full" onClick={leave}>Leave room</button>
            {me.permissions.includes("delete_session") && (
              <button className={`btn w-full ${confirmDelete ? "border-danger text-danger" : ""}`}
                onClick={() => (confirmDelete ? room.deleteRoom() : setConfirmDelete(true))} onBlur={() => setConfirmDelete(false)}>
                {confirmDelete ? "Click again to delete everything" : "Delete room"}
              </button>
            )}
          </div>
        </aside>

        {/* Main */}
        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex min-h-0 flex-1 gap-6 px-4 pt-6 sm:px-8 sm:pt-8">
            <section className="flex min-w-0 flex-1 flex-col">
              <div className="lg:hidden mb-2 text-sm text-muted">Room <strong className="text-ink">{snap.session.room_code}</strong> · {snap.participants.length} people</div>
              <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">{snap.session.name}</h1>
              <p className="mt-1 text-muted">A shared voice channel for the team</p>
              <div className="mt-5 flex flex-wrap items-center gap-3">
                <button className={`btn min-w-32 text-left ${filter === "all" ? "border-moss" : ""}`} onClick={() => setFilter("all")}>All messages</button>
                <button className={`btn min-w-36 text-left ${filter === "outstanding" ? "border-moss" : ""}`} onClick={() => setFilter("outstanding")}>
                  Outstanding <span className="ml-2">{outstandingCount}</span>
                </button>
                <span className="ml-auto hidden text-sm text-muted sm:inline">Messages condensed by AI</span>
              </div>

              <div className="mt-5 min-h-0 flex-1 space-y-2 overflow-y-auto pb-6 pr-1">
                {feed.length === 0 && (
                  <p className="py-16 text-center text-muted">
                    {filter === "outstanding" ? "Nothing outstanding." : "No messages yet. Hold to talk and speak naturally."}
                  </p>
                )}
                {feed.map((item) => item.kind === "message" ? (
                  <MessageCard key={item.m.id} message={item.m} people={people} me={snap.me} canManage={canManage}
                    sourced={bySource[item.m.id] ?? []}
                    linked={item.m.request_ids.map((id) => reqById[id]).filter((r) => r && r.source_message_id !== item.m.id)}
                    events={snap.request_events} onAction={room.action} onJump={jump} highlightFlash={flash === item.m.id} />
                ) : (
                  <div key={item.c.id} className="flex items-start gap-4 rounded-xl border border-dashed border-line px-4 py-3 sm:px-5">
                    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-sage">
                      <RelayMark width={14} height={18} />
                    </span>
                    <div className="min-w-0">
                      <p className="text-sm"><span className="font-bold">Relay</span> <span className="text-muted">asks {item.c.participant_id === snap.me ? "you" : people[item.c.participant_id]?.display_name}</span></p>
                      <p className="mt-0.5">{item.c.question}</p>
                      {item.c.participant_id === snap.me && <p className="mt-1 text-xs text-muted">Hold to talk and answer.</p>}
                    </div>
                  </div>
                ))}
                {Object.entries(room.activity)
                  .filter(([, a]) => !nowIso || a.at > Date.parse(nowIso) - 20000) // drop if no message ever came
                  .map(([pid, a]) => (
                  <div key={pid} className="flex items-center gap-4 px-4 py-3 sm:px-5" aria-live="polite">
                    <Avatar id={pid} name={people[pid]?.display_name} />
                    <div className="min-w-0">
                      <p className="text-sm">
                        <span className="font-bold">{people[pid]?.display_name}</span>{" "}
                        <span className="text-muted">{a.phase === "speaking" ? "is speaking" : "· Relay is summarising"}</span>
                      </p>
                      <div className={`speech-bubble mt-1.5 ${a.phase === "processing" ? "is-processing" : ""}`} aria-label={a.phase === "speaking" ? "Speaking" : "Summarising"}>
                        <span /><span /><span />
                      </div>
                    </div>
                  </div>
                ))}
                <div ref={feedEnd} />
              </div>
            </section>

            {askOpen && (
              <div className="fixed inset-x-3 bottom-28 top-20 z-20 lg:static lg:inset-auto lg:z-auto lg:w-96 lg:shrink-0 lg:pb-6">
                <AskRelayPanel exchange={exchange} pending={room.openExchange === "new"} interim={room.askInterim}
                  asking={room.asking} messages={snap.messages} people={people} history={snap.exchanges}
                  onClose={() => room.setOpenExchange(null)} onShare={room.share} onPlay={room.playAnswer} hasAudio={!!exchange && exchange.id in room.answerAudio} onJump={jump}
                  onToggleAsk={room.toggleAsk} onType={(t) => room.sendText(t, "ask")} onPick={room.setOpenExchange} micReady={room.micReady} />
              </div>
            )}
          </div>

          {/* Talk bar */}
          <footer className="border-t border-line bg-[#fafaf8] px-4 py-4 sm:px-8">
            <div className="flex flex-wrap items-center gap-3">
              <div className="min-w-0 flex-1">
                <p className="font-bold">
                  {!room.micReady ? "Enable your microphone to talk"
                    : room.talk === "talking" ? "You're on the channel"
                    : speaker ? `${speaker.display_name} is speaking`
                    : "The channel is clear"}
                </p>
                <p className={`truncate text-sm ${lastWarning ? "text-amber-ink" : "text-muted"}`}>
                  {lastWarning ? lastWarning.text : !room.micReady ? "Your mic only reaches the team and Relay while you hold the talk button."
                    : room.talk === "talking" ? "Release to send. Speak naturally; important details stay explicit."
                    : queuePos >= 0 ? `You're next in line${queuePos > 0 ? ` (#${queuePos + 1})` : ""}. Keep holding.`
                    : speaker ? "Hold to request the next turn." : "Speak naturally. Important details stay explicit. Hold Space to talk."}
                </p>
              </div>
              <form className="hidden min-w-48 flex-1 md:flex" onSubmit={(e) => { e.preventDefault(); room.sendText(typed, "team"); setTyped(""); }}>
                <input className="field py-2.5" placeholder="Type to the team instead" value={typed} onChange={(e) => setTyped(e.target.value)} />
              </form>
              <button className="btn min-w-32 px-5 py-3 text-left" onClick={() => (askOpen ? room.setOpenExchange(null) : room.setOpenExchange(snap.exchanges.at(-1)?.id ?? "idle"))}>
                Ask Relay
              </button>
              {!room.micReady ? (
                <button className="btn btn-primary min-w-40 px-5 py-3 text-left" onClick={room.enableMic}>Enable microphone</button>
              ) : (
                <button
                  className={`btn btn-primary relative min-w-40 select-none overflow-hidden px-5 py-3 text-left touch-none ${room.talk === "talking" ? "ring-4 ring-moss/25" : ""}`}
                  onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); room.pressTalk(); }}
                  onPointerUp={room.releaseTalk} onPointerCancel={room.releaseTalk} onContextMenu={(e) => e.preventDefault()}>
                  {room.talk === "talking" && (
                    <span className="absolute inset-y-0 left-0 bg-white/15 transition-[width]" style={{ width: `${Math.min(100, room.micLevel * 250)}%` }} />
                  )}
                  <span className="relative">{room.talk === "talking" ? "Talking…" : room.talk === "waiting" ? (queuePos >= 0 ? "Waiting…" : "Requesting…") : "Hold to talk"}</span>
                </button>
              )}
            </div>
          </footer>
        </main>
      </div>

    </div>
  );
}

function SpeakerIcon({ off }: { off: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M11 5 6 9H2v6h4l5 4V5z" />
      {off ? (<><line x1="23" y1="9" x2="17" y2="15" /><line x1="17" y1="9" x2="23" y2="15" /></>)
        : (<><path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M19 5a10 10 0 0 1 0 14" /></>)}
    </svg>
  );
}
