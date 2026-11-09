export type Status = "open" | "accepted" | "in_progress" | "completed" | "cancelled";
export const OUTSTANDING: Status[] = ["open", "accepted", "in_progress"];

export interface Participant {
  id: string;
  display_name: string;
  role: string;
  permissions: string[];
  online?: boolean;
  connection_state?: string;
}

export interface Message {
  id: string;
  participant_id: string | null;
  original_transcript: string | null;
  summary: string;
  request_ids: string[];
  timestamp: string;
  kind: "team" | "shared_answer";
  shared_answer_id: string | null;
  sources?: string[];
  generated_at?: string;
}

export interface RequestDetails {
  description?: string;
  quantity?: string;
  destination?: string;
  instructions?: string;
  deadline?: string;
  estimate?: string;
  estimate_due_at?: string;
  notes?: string;
}

export interface Req {
  id: string;
  source_message_id: string | null;
  requester_id: string | null;
  assignee_id: string | null;
  target_role: string | null;
  details: RequestDetails;
  status: Status;
  version: number;
  instruction_revision: number;
  accepted_revision: number | null;
  created_at: string;
  updated_at: string;
}

export interface RequestEvent {
  request_id: string;
  actor_id: string | null;
  origin: "ai" | "manual";
  type: string;
  timestamp: string;
  sequence: number;
  source_message_id: string | null;
}

export interface Exchange {
  id: string;
  question_transcript: string;
  answer_text: string;
  source_message_ids: string[];
  created_at: string;
  shared_message_id: string | null;
  audio?: string;
}

export interface Clarification {
  id: string;
  participant_id: string;
  question: string;
  related_request_id: string | null;
  created_at: string;
  expires_at: string;
}

export interface TurnState {
  speaker: { participant_id: string; turn_id: string } | null;
  queue: string[];
}

export type ComponentState = "ok" | "error" | "off" | "unknown";

export interface Snapshot {
  session: { id: string; room_code: string; name: string; setting: string; created_by: string };
  me: string;
  participants: Participant[];
  messages: Message[];
  requests: Req[];
  request_events: RequestEvent[];
  exchanges: Exchange[];
  clarifications: Clarification[];
  turn: TurnState;
  providers: { audio: boolean; transcription: boolean; ai: boolean; speech: boolean };
  latest_sequence: number;
}

export interface ServerEvent {
  type: string;
  payload: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  sequence?: number;
  ephemeral?: boolean;
}
