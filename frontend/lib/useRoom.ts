"use client";
// Room state: snapshot + live events over the websocket, with reconnect that reloads
// missed events after the last sequence. Also owns push-to-talk and assistant playback.
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, Creds, WS_URL } from "./api";
import { joinLiveKit, AudioState, LiveKitSession } from "./livekit";
import { FRAME_MS, Mic, startMic } from "./mic";

type Mode = "team" | "ask";
const TAG: Record<Mode, number> = { team: 1, ask: 2 };
const PREROLL_MS = 300; // audio kept from just before the press, so the first word isn't clipped
const TAIL_MS = 300; // keep capturing briefly after release, so the last word isn't clipped
const MAX_PENDING_FRAMES = (10_000 / FRAME_MS) | 0; // cap buffering while waiting for a turn
import type { ComponentState, Exchange, Message, Participant, Req, RequestEvent, ServerEvent, Snapshot, TurnState } from "./types";

export interface Notice { id: number; text: string; level: string }

interface State {
  snap: Snapshot | null;
  // Who is talking or waiting for Relay's summary (no words are shown, just a bubble).
  activity: Record<string, { phase: "speaking" | "processing"; at: number }>;
  askInterim: string;
  status: Record<"transcription" | "ai" | "speech", { state: ComponentState; detail?: string }>;
  notices: Notice[];
  openExchange: string | null;
  closed: boolean;
}

type Action =
  | { t: "snapshot"; snap: Snapshot }
  | { t: "event"; e: ServerEvent }
  | { t: "dismiss"; id: number }
  | { t: "notice"; text: string; level?: string }
  | { t: "openExchange"; id: string | null }
  | { t: "exchangeAudio"; id: string; audio: string };

let noticeId = 0;
const upsert = <T extends { id: string }>(list: T[], item: T) =>
  list.some((x) => x.id === item.id) ? list.map((x) => (x.id === item.id ? { ...x, ...item } : x)) : [...list, item];

function applyPersisted(snap: Snapshot, e: ServerEvent): [Snapshot, Partial<State>] {
  const p = e.payload;
  const extra: Partial<State> = {};
  const s = { ...snap, latest_sequence: Math.max(snap.latest_sequence, e.sequence ?? 0) };
  switch (e.type) {
    case "message.created":
      s.messages = upsert(s.messages, p.message as Message);
      break;
    case "message.linked":
      s.messages = s.messages.map((m) => (m.id === p.message_id ? { ...m, request_ids: p.request_ids } : m));
      break;
    case "request.changed": {
      const r = p.request as Req;
      s.requests = upsert(s.requests, r);
      const ev: RequestEvent = {
        request_id: r.id, actor_id: p.change.actor_id, origin: p.change.origin, type: p.change.type,
        timestamp: (e as any).timestamp ?? new Date().toISOString(), sequence: e.sequence ?? 0, // eslint-disable-line @typescript-eslint/no-explicit-any
        source_message_id: p.change.source_message_id,
      };
      s.request_events = [...s.request_events, ev];
      break;
    }
    case "participant.joined":
    case "participant.updated":
      s.participants = upsert(s.participants, p.participant as Participant);
      break;
    case "clarification.created":
      s.clarifications = upsert(s.clarifications, p as any); // eslint-disable-line @typescript-eslint/no-explicit-any
      break;
    case "clarification.resolved":
      s.clarifications = s.clarifications.filter((c) => !(p.ids as string[]).includes(c.id));
      break;
    case "ask.answer":
      s.exchanges = upsert(s.exchanges, p.exchange as Exchange);
      extra.openExchange = p.exchange.id;
      extra.askInterim = "";
      break;
    case "ask.shared":
      s.exchanges = s.exchanges.map((x) => (x.id === p.exchange_id ? { ...x, shared_message_id: p.message_id } : x));
      break;
  }
  return [s, extra];
}

function reducer(state: State, a: Action): State {
  switch (a.t) {
    case "snapshot":
      return { ...state, snap: a.snap };
    case "dismiss":
      return { ...state, notices: state.notices.filter((n) => n.id !== a.id) };
    case "notice":
      return { ...state, notices: [...state.notices, { id: ++noticeId, text: a.text, level: a.level ?? "warning" }] };
    case "openExchange":
      return { ...state, openExchange: a.id };
    case "exchangeAudio":
      if (!state.snap) return state;
      return { ...state, snap: { ...state.snap, exchanges: state.snap.exchanges.map((x) => (x.id === a.id ? { ...x, audio: a.audio } : x)) } };
    case "event": {
      const e = a.e;
      if (!state.snap) return state;
      if (!e.ephemeral) {
        if ((e.sequence ?? 0) <= state.snap.latest_sequence) return state; // already applied
        const [snap, extra] = applyPersisted(state.snap, e);
        let activity = state.activity;
        const author = e.type === "message.created" ? e.payload.message?.participant_id : null;
        if (author && activity[author]) { activity = { ...activity }; delete activity[author]; }
        return { ...state, ...extra, activity, notices: [...state.notices, ...(extra.notices ?? [])], snap };
      }
      const p = e.payload;
      switch (e.type) {
        case "turn.state": {
          const activity = { ...state.activity };
          const now = Date.now();
          for (const [pid, a] of Object.entries(activity)) {
            if (a.phase === "speaking" && p.speaker?.participant_id !== pid) activity[pid] = { phase: "processing", at: now };
          }
          const sp = p.speaker?.participant_id as string | undefined;
          if (sp && activity[sp]?.phase !== "speaking") activity[sp] = { phase: "speaking", at: now };
          return { ...state, activity, snap: { ...state.snap, turn: p as TurnState } };
        }
        case "transcript.interim":
          if (p.mode === "ask") return { ...state, askInterim: p.text };
          return state; // team words are not shown while speaking
        case "transcript.final": {
          if (p.mode === "ask") return state;
          const activity = { ...state.activity };
          if (!p.text) delete activity[p.participant_id]; // nothing heard: no message will follow
          else activity[p.participant_id] = { phase: "processing", at: Date.now() };
          return { ...state, activity };
        }
        case "status":
          return { ...state, status: { ...state.status, [p.component]: { state: p.state, detail: p.detail } } };
        case "notice":
          return { ...state, notices: [...state.notices, { id: ++noticeId, text: p.text, level: p.level }] };
        case "presence":
          return { ...state, snap: { ...state.snap, participants: state.snap.participants.map((x) => (x.id === p.participant_id ? { ...x, online: p.online } : x)) } };
        case "session.closed":
          return { ...state, closed: true };
      }
      return state;
    }
  }
}

const initial: State = {
  snap: null, activity: {}, askInterim: "", notices: [], openExchange: null, closed: false,
  status: { transcription: { state: "unknown" }, ai: { state: "unknown" }, speech: { state: "unknown" } },
};

export type TalkPhase = "idle" | "waiting" | "talking";

export function useRoom(creds: Creds | null) {
  const [state, dispatch] = useReducer(reducer, initial);
  const [connected, setConnected] = useState(false);
  const [audio, setAudio] = useState<{ state: AudioState; detail?: string }>({ state: "off" });
  const [micReady, setMicReady] = useState(false);
  const [micLevel, setMicLevel] = useState(0);
  const [talk, setTalk] = useState<TalkPhase>("idle");
  const [asking, setAsking] = useState<TalkPhase>("idle");

  const ws = useRef<WebSocket | null>(null);
  const seqRef = useRef(0);
  const mic = useRef<Mic | null>(null);
  const lk = useRef<LiveKitSession | null>(null);
  // Teammates this listener has muted (local only). Remembered per room in this browser.
  const mutedKey = creds ? `relay.muted.${creds.session_id}` : "";
  const [muted, setMutedState] = useState<Set<string>>(new Set());
  const mutedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!mutedKey) return;
    let saved: string[] = [];
    try { saved = JSON.parse(localStorage.getItem(mutedKey) ?? "[]"); } catch {}
    mutedRef.current = new Set(saved);
    setMutedState(new Set(saved)); // eslint-disable-line react-hooks/set-state-in-effect
  }, [mutedKey]);
  const capture = useRef<{ team: boolean; ask: boolean }>({ team: false, ask: false });
  const holding = useRef<{ team: boolean; ask: boolean }>({ team: false, ask: false });
  const player = useRef<HTMLAudioElement | null>(null);
  const playing = useRef<{ kind: "announcement" | "answer"; id: string } | null>(null);
  const announceQueue = useRef<{ id: string; audio: string }[]>([]);
  const speakerRef = useRef<string | null>(null);
  const pendingAnswer = useRef<{ id: string; audio: string } | null>(null);
  const answerAudio = useRef<Record<string, string>>({});
  const preroll = useRef<ArrayBuffer[]>([]);
  const pending = useRef<Record<Mode, ArrayBuffer[]>>({ team: [], ask: [] });
  const credsRef = useRef<Creds | null>(creds);
  const replaying = useRef(false);
  const replayAgain = useRef(false);
  useEffect(() => { credsRef.current = creds; }, [creds]);
  const playRef = useRef<(kind: "announcement" | "answer", id: string, b64: string) => void>(() => {});

  useEffect(() => {
    speakerRef.current = state.snap?.turn.speaker?.participant_id ?? null;
  }, [state.snap]);

  const send = useCallback((msg: object) => {
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify(msg));
  }, []);

  const sendFrame = useCallback((mode: Mode, pcm: ArrayBuffer) => {
    const s = ws.current;
    if (!s || s.readyState !== WebSocket.OPEN) return;
    const frame = new Uint8Array(pcm.byteLength + 1);
    frame[0] = TAG[mode];
    frame.set(new Uint8Array(pcm), 1);
    s.send(frame);
  }, []);

  // ---------------------------------------------------------------- playback
  const stopPlayback = useCallback((interrupted: boolean) => {
    const cur = playing.current;
    if (!cur || !player.current) return;
    player.current.pause();
    if (cur.kind === "announcement") send({ type: "announcement.state", id: cur.id, state: interrupted ? "interrupted" : "played" });
    playing.current = null;
  }, [send]);

  const play = useCallback((kind: "announcement" | "answer", id: string, b64: string) => {
    if (!player.current) player.current = new Audio();
    stopPlayback(true);
    const el = player.current;
    el.src = `data:audio/mpeg;base64,${b64}`;
    playing.current = { kind, id };
    el.onended = () => {
      if (playing.current?.id === id) {
        if (kind === "announcement") send({ type: "announcement.state", id, state: "played" });
        playing.current = null;
        const next = announceQueue.current.shift();
        if (next) playRef.current("announcement", next.id, next.audio);
      }
    };
    el.play().catch(() => {
      if (kind === "announcement") send({ type: "announcement.state", id, state: "interrupted" });
      playing.current = null;
    });
  }, [send, stopPlayback]);
  useEffect(() => { playRef.current = play; }, [play]);

  // ---------------------------------------------------------------- events & socket
  // Fetch every event after our last sequence. Global sequences include other people's
  // private events, so gaps are normal; this fills them. Uses refs so it is never stale.
  const replay = useCallback(async () => {
    const c = credsRef.current;
    if (!c) return;
    if (replaying.current) { replayAgain.current = true; return; }
    replaying.current = true;
    try {
      do {
        replayAgain.current = false;
        const { events } = await api<{ events: ServerEvent[] }>(
          `/sessions/${c.session_id}/events?after=${seqRef.current}`, { token: c.token });
        for (const e of events) {
          dispatch({ t: "event", e });
          if (e.sequence) seqRef.current = Math.max(seqRef.current, e.sequence);
        }
      } while (replayAgain.current);
    } catch {
      // Next event or reconnect will retry.
    } finally {
      replaying.current = false;
    }
  }, []);

  const handle = useCallback((e: ServerEvent) => {
    if (!e.ephemeral && e.sequence) {
      if (e.sequence > seqRef.current + 1) {
        // Gap: replay everything after our last sequence (it includes this event).
        void replay();
        return;
      }
      seqRef.current = Math.max(seqRef.current, e.sequence);
    }
    if (e.type === "turn.state") {
      if (e.payload.speaker && playing.current) {
        // Answer speech yields to team audio; replay it once the channel clears.
        if (playing.current.kind === "answer") {
          const id = playing.current.id;
          const audio = answerAudio.current[id];
          if (audio) pendingAnswer.current = { id, audio };
        }
        stopPlayback(true);
      } else if (!e.payload.speaker && pendingAnswer.current) {
        const { id, audio } = pendingAnswer.current;
        pendingAnswer.current = null;
        play("answer", id, audio);
      }
    }
    if (e.type === "turn.granted") {
      const mode = e.payload.mode as "team" | "ask";
      if (!holding.current[mode]) {
        send({ type: "turn.release", mode });
      } else {
        // Send what was said while waiting for the grant (plus the pre-roll), then go live.
        for (const pcm of pending.current[mode]) sendFrame(mode, pcm);
        pending.current[mode] = [];
        capture.current[mode] = true;
        if (mode === "team") {
          setTalk("talking");
          lk.current?.track.unmute();
        } else setAsking("talking");
      }
    }
    if (e.type === "turn.ended") {
      const mode = e.payload.mode as "team" | "ask";
      capture.current[mode] = false;
      if (mode === "team") { setTalk("idle"); lk.current?.track.mute(); } else setAsking("idle");
    }
    if (e.type === "announcement" && e.payload.audio) {
      // Only arrives if the server has spoken announcements enabled.
      if (speakerRef.current) send({ type: "announcement.state", id: e.payload.notification_id, state: "interrupted" });
      else if (playing.current) announceQueue.current.push({ id: e.payload.notification_id, audio: e.payload.audio });
      else play("announcement", e.payload.notification_id, e.payload.audio);
    }
    if (e.type === "ask.audio") {
      // Ask Relay answers speak automatically, but never over a teammate.
      const { exchange_id: id, audio } = e.payload;
      answerAudio.current[id] = audio;
      dispatch({ t: "exchangeAudio", id, audio });
      if (speakerRef.current) pendingAnswer.current = { id, audio };
      else play("answer", id, audio);
    }
    dispatch({ t: "event", e });
  }, [play, send, sendFrame, stopPlayback, replay]);


  useEffect(() => {
    if (!creds) return;
    let stopped = false;
    let retry: ReturnType<typeof setTimeout>;
    let attempt = 0;
    let loaded = false;

    const connect = async () => {
      try {
        if (!loaded) {
          const snap = await api<Snapshot>(`/sessions/${creds.session_id}/snapshot`, { token: creds.token });
          dispatch({ t: "snapshot", snap });
          seqRef.current = snap.latest_sequence;
          loaded = true;
        } else {
          await replay();
        }
      } catch (err) {
        if ((err as { status?: number }).status === 401) { dispatch({ t: "event", e: { type: "session.closed", payload: {}, ephemeral: true } }); return; }
      }
      if (stopped) return; // effect was cleaned up while we were loading
      const sock = new WebSocket(`${WS_URL}/sessions/${creds.session_id}/stream?token=${encodeURIComponent(creds.token)}`);
      sock.binaryType = "arraybuffer";
      ws.current = sock;
      sock.onopen = () => {
        attempt = 0;
        setConnected(true);
        void replay(); // catch anything committed between the snapshot and this socket opening
      };
      sock.onmessage = (m) => { if (typeof m.data === "string") handle(JSON.parse(m.data)); };
      sock.onclose = (ev) => {
        setConnected(false);
        capture.current = { team: false, ask: false };
        setTalk("idle"); setAsking("idle");
        lk.current?.track.mute();
        if (stopped || ev.code === 4401) return;
        retry = setTimeout(connect, Math.min(8000, 500 * 2 ** attempt++));
      };
    };
    connect();
    const ping = setInterval(() => send({ type: "ping" }), 20000);
    return () => { stopped = true; clearTimeout(retry); clearInterval(ping); ws.current?.close(); };
  }, [creds]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- microphone
  const enableMic = useCallback(async () => {
    if (!creds || mic.current) return;
    try {
      player.current = player.current ?? new Audio(); // unlock playback with this gesture
      mic.current = await startMic((pcm, level) => {
        setMicLevel(level);
        preroll.current.push(pcm);
        if (preroll.current.length > PREROLL_MS / FRAME_MS) preroll.current.shift();
        for (const mode of ["team", "ask"] as const) {
          if (capture.current[mode]) sendFrame(mode, pcm);
          else if (holding.current[mode] && pending.current[mode].length < MAX_PENDING_FRAMES) pending.current[mode].push(pcm);
        }
      });
      setMicReady(true);
    } catch (err) {
      dispatch({ t: "notice", text: `Microphone unavailable: ${(err as Error).message}` });
      return;
    }
    try {
      lk.current = await joinLiveKit(creds.session_id, creds.token, mic.current.track,
        (s, detail) => setAudio({ state: s, detail }), (id) => mutedRef.current.has(id));
    } catch (err) {
      setAudio({ state: "error", detail: (err as Error).message });
    }
  }, [creds, sendFrame]);

  useEffect(() => () => { mic.current?.stop(); lk.current?.room.disconnect(); }, []);

  // ---------------------------------------------------------------- talk controls
  const begin = useCallback((mode: Mode) => {
    holding.current[mode] = true;
    pending.current[mode] = [...preroll.current];
    send({ type: "turn.request", mode });
  }, [send]);

  const end = useCallback((mode: Mode) => {
    holding.current[mode] = false;
    if (capture.current[mode]) {
      // Keep streaming a moment so a word cut off by the release still reaches transcription.
      setTimeout(() => {
        capture.current[mode] = false;
        if (mode === "team") lk.current?.track.mute();
        send({ type: "turn.release", mode });
      }, TAIL_MS);
    } else {
      pending.current[mode] = [];
      if (mode === "team") send({ type: "turn.cancel" });
      else send({ type: "turn.release", mode });
    }
  }, [send]);

  const pressTalk = useCallback(() => {
    if (holding.current.team || !micReady) return;
    setTalk("waiting");
    begin("team");
  }, [micReady, begin]);

  const releaseTalk = useCallback(() => {
    if (!holding.current.team) return;
    end("team");
    setTalk("idle");
  }, [end]);

  const toggleAsk = useCallback(() => {
    if (!micReady) return;
    if (!holding.current.ask) {
      setAsking("waiting");
      dispatch({ t: "openExchange", id: "new" });
      begin("ask");
    } else {
      end("ask");
      setAsking("idle");
    }
  }, [micReady, begin, end]);

  const setMuted = useCallback((ids: string[], mute: boolean) => {
    const next = new Set(mutedRef.current);
    for (const id of ids) {
      if (mute) next.add(id); else next.delete(id);
      lk.current?.setMuted(id, mute);
    }
    mutedRef.current = next;
    setMutedState(next);
    try { localStorage.setItem(mutedKey, JSON.stringify([...next])); } catch {}
  }, [mutedKey]);

  const sendText = useCallback((text: string, mode: "team" | "ask") => {
    if (!text.trim()) return;
    if (mode === "ask") dispatch({ t: "openExchange", id: "new" });
    send({ type: "utterance.text", text, mode });
  }, [send]);

  const action = useCallback(async (body: Record<string, unknown>) => {
    if (!creds) return;
    try {
      await api(`/sessions/${creds.session_id}/actions`, {
        token: creds.token, body: { ...body, idempotency_key: crypto.randomUUID() },
      });
    } catch (err) {
      dispatch({ t: "notice", text: (err as Error).message });
    }
  }, [creds]);

  const share = useCallback(async (exchangeId: string) => {
    if (!creds) return;
    try {
      await api(`/sessions/${creds.session_id}/answers/${exchangeId}/share`, { method: "POST", token: creds.token });
      dispatch({ t: "notice", text: "Answer shared to the room.", level: "info" });
    } catch (err) {
      dispatch({ t: "notice", text: (err as Error).message });
    }
  }, [creds]);

  const deleteRoom = useCallback(async () => {
    if (!creds) return;
    await api(`/sessions/${creds.session_id}`, { method: "DELETE", token: creds.token }).catch((err) =>
      dispatch({ t: "notice", text: (err as Error).message }));
  }, [creds]);

  return {
    ...state, connected, audio, micReady, micLevel, talk, asking, muted, setMuted,
    enableMic, pressTalk, releaseTalk, toggleAsk, sendText, action, share, deleteRoom,
    playAnswer: (x: Exchange) => x.audio && play("answer", x.id, x.audio),
    dismiss: (id: number) => dispatch({ t: "dismiss", id }),
    setOpenExchange: (id: string | null) => dispatch({ t: "openExchange", id }),
  };
}
