"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Brand } from "@/components/Brand";
import { api, Creds, saveCreds } from "@/lib/api";

export default function Home() {
  const router = useRouter();
  const [create, setCreate] = useState({ name: "", role: "", room_name: "", setting: "" });
  const [join, setJoin] = useState({ room_code: "", name: "", role: "" });
  const [busy, setBusy] = useState<"create" | "join" | null>(null);
  const [error, setError] = useState<{ which: "create" | "join"; text: string } | null>(null);

  async function go(which: "create" | "join") {
    setBusy(which);
    setError(null);
    try {
      const r = which === "create"
        ? await api<Creds>("/sessions", { body: { ...create, room_name: create.room_name || `${create.name}'s room` } })
        : await api<Creds>("/sessions/join", { body: join });
      saveCreds(r);
      router.push("/room");
    } catch (err) {
      const msg = err instanceof TypeError ? "Can't reach the Relay server. Is the backend running?" : (err as Error).message;
      setError({ which, text: msg });
      setBusy(null);
    }
  }

  const canCreate = create.name.trim() && create.role.trim();
  const canJoin = join.room_code.trim().length >= 4 && join.name.trim() && join.role.trim();

  return (
    <>
      <Brand />
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-10 sm:px-8 sm:py-14">
        <p className="label">A shared channel. A shared understanding.</p>
        <h1 className="mt-3 text-4xl font-bold tracking-tight sm:text-5xl">Keep your team on the same wavelength.</h1>
        <p className="mt-3 text-lg text-muted">Talk through the work. Keep requests, changes, and handoffs together in one room.</p>

        <div className="mt-10 grid gap-6 md:grid-cols-2">
          <form className="rounded-2xl border border-line bg-card p-6 sm:p-8" onSubmit={(e) => { e.preventDefault(); if (canCreate) go("create"); }}>
            <h2 className="text-2xl font-bold">Start a room</h2>
            <p className="mt-1 text-muted">Create a code. Bring your team together.</p>
            <div className="mt-6 grid gap-4 sm:grid-cols-2">
              <Field label="Your name" value={create.name} onChange={(v) => setCreate({ ...create, name: v })} placeholder="Your name" max={60} />
              <Field label="Your role" value={create.role} onChange={(v) => setCreate({ ...create, role: v })} placeholder="Describe your role" max={60} />
            </div>
            <div className="mt-4">
              <Field label="Room name · optional" value={create.room_name} onChange={(v) => setCreate({ ...create, room_name: v })} placeholder="Evening production" max={80} />
            </div>
            <div className="mt-4">
              <Field label="Room setting · optional" value={create.setting} onChange={(v) => setCreate({ ...create, setting: v })} placeholder="What is your team working on?" max={500} />
            </div>
            <button className="btn btn-primary mt-6 w-full py-3 text-left" disabled={!canCreate || busy !== null}>
              {busy === "create" ? "Creating…" : "Create room →"}
            </button>
            {error?.which === "create" && <p className="mt-3 text-sm text-danger">{error.text}</p>}
          </form>

          <form className="rounded-2xl border border-line bg-card p-6 sm:p-8" onSubmit={(e) => { e.preventDefault(); if (canJoin) go("join"); }}>
            <h2 className="text-2xl font-bold">Join your team</h2>
            <p className="mt-1 text-muted">Have a room code? You&apos;re almost there.</p>
            <div className="mt-6">
              <Field label="Room code" value={join.room_code} onChange={(v) => setJoin({ ...join, room_code: v.toUpperCase() })} placeholder="ABC123" max={12} mono />
            </div>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <Field label="Your name" value={join.name} onChange={(v) => setJoin({ ...join, name: v })} placeholder="Your name" max={60} />
              <Field label="Your role" value={join.role} onChange={(v) => setJoin({ ...join, role: v })} placeholder="Describe your role" max={60} />
            </div>
            <button className="btn mt-6 w-full py-3 text-left" disabled={!canJoin || busy !== null}>
              {busy === "join" ? "Joining…" : "Join room →"}
            </button>
            {error?.which === "join" && <p className="mt-3 text-sm text-danger">{error.text}</p>}
          </form>
        </div>

        <div className="mt-8 space-y-2 text-sm text-muted">
          <p>No account needed. Share the generated code with your team. Names and roles are free-form.</p>
          <p className="rounded-lg border border-line bg-sage px-4 py-3 text-ink">
            <strong>Before you join:</strong> while you hold the talk button, your speech is sent to external AI services
            (Deepgram for transcription, an AI model chosen by the host (Gemini, OpenAI or Claude) for summaries and requests, ElevenLabs for spoken replies).
            Transcripts and summaries are kept for 7 days; raw audio is not stored. Headphones are recommended.
          </p>
        </div>
      </main>
    </>
  );
}

// max mirrors the backend's field limits, so a value the server would reject can't be typed.
function Field({ label, value, onChange, placeholder, max, mono }: {
  label: string; value: string; onChange: (v: string) => void; placeholder: string; max: number; mono?: boolean;
}) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      <input className={`field mt-2 ${mono ? "tracking-[0.3em] uppercase" : ""}`} value={value}
        onChange={(e) => onChange(e.target.value)} placeholder={placeholder} maxLength={max} />
    </label>
  );
}
