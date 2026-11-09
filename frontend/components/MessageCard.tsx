"use client";
import { useState } from "react";
import type { Message, Participant, Req, RequestEvent } from "@/lib/types";
import { OUTSTANDING } from "@/lib/types";
import { Avatar, time } from "./util";

const STATUS_LABEL: Record<string, string> = {
  open: "Open", accepted: "Accepted", in_progress: "In progress", completed: "Completed", cancelled: "Cancelled",
};

const EVENT_LABEL: Record<string, string> = {
  create_request: "created", update_request: "changed", accept: "accepted", decline: "declined",
  report_progress: "reported progress", complete: "completed", cancel: "cancelled", reassign: "reassigned",
};

export function MessageCard(props: {
  message: Message;
  people: Record<string, Participant>;
  sourced: Req[]; // requests whose source is this message
  linked: Req[]; // other requests this message updated
  events: RequestEvent[];
  me: string;
  canManage: boolean;
  onAction: (body: Record<string, unknown>) => void;
  onJump: (id: string) => void;
  highlightFlash: boolean;
}) {
  const { message: m, people, sourced, linked, events, me } = props;
  const [open, setOpen] = useState(false);
  const speaker = m.participant_id ? people[m.participant_id] : undefined;
  const outstanding = sourced.some((r) => OUTSTANDING.includes(r.status));
  const shared = m.kind === "shared_answer";
  const corrected = sourced.some((r) => r.instruction_revision > 1);

  return (
    <article id={`msg-${m.id}`}
      className={`relative rounded-xl border px-4 py-4 transition-shadow sm:px-5 ${
        outstanding ? "border-amber-line bg-amber-bg" : shared ? "border-line bg-sage" : "border-transparent"
      } ${props.highlightFlash ? "ring-2 ring-moss/40" : ""}`}>
      {outstanding && <span className="absolute inset-y-3 left-0 w-1 rounded-full bg-amber-bar" />}
      <div className="flex gap-4">
        <Avatar id={m.participant_id ?? "relay"} name={speaker?.display_name} />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-3">
            <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              <span className="font-bold">{speaker?.display_name ?? "Someone"}</span>
              <span className="text-sm text-muted">{speaker?.role}</span>
            </div>
            <time className="shrink-0 text-sm text-muted">{time(m.timestamp)}</time>
          </div>

          {shared && (
            <p className="mt-1 text-xs font-semibold uppercase tracking-wide text-moss">
              Shared Relay answer · generated {m.generated_at ? time(m.generated_at) : ""}
            </p>
          )}
          {outstanding && (
            <p className="mt-1 flex items-center gap-1.5 text-sm text-amber-ink">
              <span className="h-2 w-2 rounded-full bg-amber-ink" /> Outstanding request{sourced.filter((r) => OUTSTANDING.includes(r.status)).length > 1 ? "s" : ""}
            </p>
          )}

          {corrected ? (
            <>
              {/* Show the request as it stands now; keep the original wording for reference. */}
              <p className="mt-1.5 leading-relaxed">{sourced.map((r) => (r.details.description ?? "").replace(/[.\s]+$/, "")).join("; ")}.</p>
              <p className="mt-0.5 text-sm text-muted">Originally: {m.summary}</p>
            </>
          ) : (
            <p className="mt-1.5 leading-relaxed">{m.summary}</p>
          )}

          {shared && m.sources && m.sources.length > 0 && (
            <p className="mt-2 text-sm text-moss">
              Sources ·{" "}
              {m.sources.map((id, i) => (
                <button key={id} className="underline-offset-2 hover:underline" onClick={() => props.onJump(id)}>
                  {i > 0 && ", "}message {i + 1}
                </button>
              ))}
            </p>
          )}

          {sourced.map((r) => (
            <RequestLine key={r.id} r={r} people={people} me={me} canManage={props.canManage} onAction={props.onAction}
              events={events.filter((e) => e.request_id === r.id)} expanded={open} />
          ))}
          {linked.length > 0 && (
            <p className="mt-2 text-sm text-muted">
              Updates:{" "}
              {linked.map((r, i) => (
                <button key={r.id} className="hover:underline" onClick={() => r.source_message_id && props.onJump(r.source_message_id)}>
                  {i > 0 && ", "}{r.details.description}
                </button>
              ))}
            </p>
          )}

          {(m.original_transcript || sourced.length > 0) && (
            <button className="mt-2 text-xs text-muted hover:text-ink" onClick={() => setOpen(!open)} aria-expanded={open}>
              {open ? "▾" : "▸"} {m.original_transcript ? "Original transcript" : "Details"}
              {sourced.length > 0 ? " & history" : ""}
            </button>
          )}
          {open && m.original_transcript && (
            <blockquote className="mt-2 border-l-2 border-line pl-3 text-sm italic text-muted">“{m.original_transcript}”</blockquote>
          )}
        </div>
      </div>
    </article>
  );
}

function RequestLine({ r, people, me, canManage, onAction, events, expanded }: {
  r: Req; people: Record<string, Participant>; me: string; canManage: boolean;
  onAction: (body: Record<string, unknown>) => void; events: RequestEvent[]; expanded: boolean;
}) {
  const assignee = r.assignee_id ? people[r.assignee_id] : undefined;
  const isAssignee = r.assignee_id === me;
  const isRequester = r.requester_id === me;
  const claimable = !r.assignee_id && (!r.target_role || r.target_role.toLowerCase() === people[me]?.role.toLowerCase());
  const active = OUTSTANDING.includes(r.status);
  const needsConfirm = active && r.instruction_revision > 1 && r.accepted_revision === null;
  const pending = active && r.status === "open" && assignee && !assignee.online;
  const overdue = active && r.details.estimate_due_at && new Date(r.details.estimate_due_at) < new Date();
  const act = (type: string, extra: Record<string, unknown> = {}) =>
    onAction({ type, request_id: r.id, expected_version: r.version, ...extra });

  const d = r.details;
  const facts = [
    d.quantity && `Qty ${d.quantity}`, d.destination && `→ ${d.destination}`,
    d.deadline && `Deadline: ${d.deadline}`, d.estimate && `Estimate: ${d.estimate}`,
  ].filter(Boolean);

  return (
    <div className={`mt-3 rounded-lg border px-3 py-2 text-sm ${active ? "border-amber-line bg-white/60" : "border-line bg-white/40 text-muted"}`}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium text-ink">{d.description}</span>
        <span className="rounded-full bg-sage px-2 py-0.5 text-xs">{STATUS_LABEL[r.status]}</span>
        {needsConfirm && <span className="rounded-full bg-amber-bg px-2 py-0.5 text-xs text-amber-ink">Changed · awaiting confirmation</span>}
        {!needsConfirm && r.instruction_revision > 1 && <span className="rounded-full bg-sage px-2 py-0.5 text-xs text-moss">Updated</span>}
        {pending && <span className="rounded-full bg-amber-bg px-2 py-0.5 text-xs text-amber-ink">Delivery pending</span>}
        {overdue && <span className="rounded-full bg-amber-bg px-2 py-0.5 text-xs text-amber-ink">Estimate overdue</span>}
      </div>
      <p className="mt-0.5 text-muted">
        {assignee ? `For ${assignee.display_name}` : r.target_role ? `For any ${r.target_role}` : "Unassigned"}
        {facts.length > 0 && ` · ${facts.join(" · ")}`}
        {d.notes && ` · ${d.notes}`}
      </p>
      {active && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {r.status === "open" ? (
            <>
              {(isAssignee || claimable) && <Small onClick={() => act("accept")}>Accept</Small>}
              {isAssignee && <Small onClick={() => act("decline")}>Decline</Small>}
            </>
          ) : (
            // Once taken up, only Complete and Cancel remain.
            (isAssignee || canManage) && <Small onClick={() => act("complete")}>Complete</Small>
          )}
          {(isRequester || canManage) && <Small onClick={() => act("cancel")}>Cancel</Small>}
        </div>
      )}
      {expanded && events.length > 0 && (
        <ol className="mt-2 space-y-0.5 border-t border-line pt-2 text-xs text-muted">
          {events.map((e) => (
            <li key={e.sequence}>
              {time(e.timestamp)} · {people[e.actor_id ?? ""]?.display_name ?? "Someone"} {EVENT_LABEL[e.type] ?? e.type}
              {e.origin === "manual" ? " (manual)" : " (by voice)"}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function Small({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return <button className="btn px-2.5 py-1 text-xs" onClick={onClick}>{children}</button>;
}
