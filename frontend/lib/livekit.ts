// LiveKit: everyone subscribes to the room; your mic track is published muted and is
// only unmuted while the server has granted you the team turn. Listeners can mute
// individual teammates locally (only affects what you hear).
import { LocalAudioTrack, Room, RoomEvent, Track } from "livekit-client";
import { api } from "./api";

export type AudioState = "off" | "connecting" | "ok" | "error";

export interface LiveKitSession {
  room: Room;
  track: LocalAudioTrack;
  setMuted: (participantId: string, muted: boolean) => void;
}

export async function joinLiveKit(
  sessionId: string,
  token: string,
  micTrack: MediaStreamTrack,
  onState: (s: AudioState, detail?: string) => void,
  isMuted: (participantId: string) => boolean,
): Promise<LiveKitSession> {
  onState("connecting");
  const { url, token: lkToken } = await api<{ url: string; token: string }>(
    `/sessions/${sessionId}/audio-token`,
    { method: "POST", token },
  );
  const room = new Room({ adaptiveStream: true, dynacast: true });
  const elements = new Map<string, Set<HTMLMediaElement>>(); // participant id -> audio elements

  room.on(RoomEvent.TrackSubscribed, (track, _pub, participant) => {
    if (track.kind !== Track.Kind.Audio) return;
    const el = track.attach();
    el.muted = isMuted(participant.identity);
    document.body.appendChild(el);
    if (!elements.has(participant.identity)) elements.set(participant.identity, new Set());
    elements.get(participant.identity)!.add(el);
  });
  room.on(RoomEvent.TrackUnsubscribed, (track, _pub, participant) => {
    track.detach().forEach((el) => {
      elements.get(participant.identity)?.delete(el);
      el.remove();
    });
  });
  room.on(RoomEvent.Disconnected, () => onState("error", "Audio transport disconnected"));
  room.on(RoomEvent.Reconnecting, () => onState("connecting"));
  room.on(RoomEvent.Reconnected, () => onState("ok"));
  await room.connect(url, lkToken);
  const track = new LocalAudioTrack(micTrack.clone(), undefined, false);
  await room.localParticipant.publishTrack(track, { source: Track.Source.Microphone });
  await track.mute();
  onState("ok");
  return {
    room,
    track,
    setMuted: (id, muted) => elements.get(id)?.forEach((el) => { el.muted = muted; }),
  };
}
