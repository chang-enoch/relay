"use client";
import { useState } from "react";
import type { Exchange, Message, Participant } from "@/lib/types";
import { time } from "./util";
import type { TalkPhase } from "@/lib/useRoom";

export function AskRelayPanel({ exchange, pending, interim, asking, messages, people, onClose, onShare, onPlay, onJump, onToggleAsk, onType, history, onPick, micReady }: {
  exchange: Exchange | null; pending: boolean; interim: string; asking: TalkPhase;
  messages: Message[]; people: Record<string, Participant>; history: Exchange[];
  onClose: () => void; onShare: (id: string) => void; onPlay: (x: Exchange) => void; onJump: (id: string) => void;
  onToggleAsk: () => void; onType: (text: string) => void; onPick: (id: string) => void; micReady: boolean;
}) {
  const [text, setText] = useState("");
  const msgById = Object.fromEntries(messages.map((m) => [m.id, m]));
  return (
    <aside className="flex max-h-full flex-col rounded-2xl border border-line bg-sage p-5 sm:p-6">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-2xl font-bold">Ask Relay</h2>
          <p className="label mt-1">Only visible to you</p>
        </div>
        <button className="text-muted hover:text-ink" onClick={onClose} aria-label="Close">✕</button>
      </div>

      <div className="mt-4 min-h-0 flex-1 overflow-y-auto">
        {pending ? (
          <>
            <p className="font-bold">{interim || (asking === "talking" ? "Listening…" : asking === "waiting" ? "Starting…" : "Thinking…")}</p>
            {asking === "idle" && <p className="mt-3 animate-pulse text-muted">Checking the room…</p>}
          </>
        ) : exchange ? (
          <>
            <p className="text-lg font-bold leading-snug">{exchange.question_transcript}</p>
            <p className="mt-4 text-[1.05rem] leading-relaxed">{exchange.answer_text}</p>
            {exchange.source_message_ids.length > 0 && (
              <p className="mt-4 text-sm text-moss">
                Sources ·{" "}
                {exchange.source_message_ids.map((id, i) => {
                  const m = msgById[id];
                  const who = m?.participant_id ? people[m.participant_id]?.display_name : "message";
                  return (
                    <button key={id} className="hover:underline" onClick={() => onJump(id)}>
                      {i > 0 && ", "}{who} {m ? time(m.timestamp) : ""}
                    </button>
                  );
                })}
              </p>
            )}
            <div className="mt-5 flex flex-wrap gap-2">
              <button className="btn" disabled={!exchange.audio} onClick={() => onPlay(exchange)}>Play answer</button>
              <button className="btn" disabled={!!exchange.shared_message_id} onClick={() => onShare(exchange.id)}>
                {exchange.shared_message_id ? "Shared to room" : "Share to room"}
              </button>
            </div>
          </>
        ) : (
          <p className="text-muted">Ask about assignments, what&apos;s outstanding, the latest reports, or what changed while you were away. Questions never create requests.</p>
        )}

        {history.length > 1 && (
          <div className="mt-6 border-t border-line pt-3">
            <p className="label">Earlier questions</p>
            <ul className="mt-2 space-y-1 text-sm">
              {history.filter((x) => x.id !== exchange?.id).slice(-5).reverse().map((x) => (
                <li key={x.id}><button className="text-left text-muted hover:text-ink" onClick={() => onPick(x.id)}>{x.question_transcript}</button></li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="mt-4 flex gap-2">
        <button className={`btn shrink-0 ${asking !== "idle" ? "btn-primary" : ""}`} onClick={onToggleAsk} disabled={!micReady} title={micReady ? "" : "Enable your microphone first"}>
          {asking === "talking" ? "■ Stop" : asking === "waiting" ? "…" : "🎙 Speak"}
        </button>
        <form className="flex min-w-0 flex-1 gap-2" onSubmit={(e) => { e.preventDefault(); onType(text); setText(""); }}>
          <input className="field min-w-0 py-2" placeholder="or type a question" value={text} onChange={(e) => setText(e.target.value)} />
        </form>
      </div>
      <p className="mt-2 text-xs text-muted">A private question does not create a room message.</p>
    </aside>
  );
}
