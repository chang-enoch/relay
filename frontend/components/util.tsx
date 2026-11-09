import type { Participant } from "@/lib/types";

const initials = (name?: string) => {
  const words = (name ?? "?").trim().split(/\s+/);
  return (words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2)).toUpperCase() || "?";
};

export const time = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const TINTS = ["#e1eadf", "#ece6dc", "#e1e8ee", "#ece1ec", "#e8eadb", "#e0ebe8"];
const tint = (id: string) => TINTS[[...id].reduce((a, c) => a + c.charCodeAt(0), 0) % TINTS.length];

export const byId = (people: Participant[]) => Object.fromEntries(people.map((p) => [p.id, p]));

export function Avatar({ id, name, size = 40 }: { id: string; name?: string; size?: number }) {
  return (
    <span className="grid shrink-0 place-items-center rounded-full text-sm font-bold text-ink"
      style={{ width: size, height: size, background: tint(id) }}>{initials(name)}</span>
  );
}
