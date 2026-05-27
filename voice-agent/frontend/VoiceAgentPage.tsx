/**
 * Voice-agent page — a single "Start Conversation" toggle.
 *
 * Captures the browser microphone via `getUserMedia` + an
 * `AudioContext` ScriptProcessor (the simple, broadly-supported
 * path — AudioWorklet would be slightly nicer but requires a
 * separate worklet module file). Downsamples to 16 kHz mono
 * PCM_S16LE and pumps base64'd chunks to the backend via the
 * `voice_agent.send_audio_chunk` WS RPC at 50fps.
 *
 * Outbound audio (Gilbert's TTS) goes through the existing
 * `useBrowserSpeaker` plumbing — the voice-agent service publishes
 * a `speaker.browser.play` event with the synthesized MP3 inlined
 * as a `data:` URL, and the browser-speaker hook already running in
 * the app shell plays it via its HTMLAudioElement.
 *
 * This is the v1 turn-taking experience: press button → talk →
 * Gilbert speaks → talk again → end. Real-time barge-in needs
 * raw-bytes-over-WS playback (different audio sink shape) and is a
 * future iteration.
 */

import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import { Mic, MicOff, Loader2, Ear } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useWebSocket } from "@/hooks/useWebSocket";

/** A single transcript turn rendered in the live feed. */
interface TranscriptTurn {
  who: string;       // "us" (Gilbert) | "them" (the user) | "system"
  text: string;
  ts: number;        // seconds since session start (from server)
  /** Wall-clock epoch millis captured at SPA receive time. Used to
   * render a HH:MM:SS column so we can spot turn-queue weirdness
   * (e.g. user repeating themselves because the first attempt
   * looked stuck, then both attempts processing back-to-back). */
  receivedAt: number;
  /** Local React-only id so we can render this without a key collision when
   * the same text repeats. The server doesn't issue ids; we mint per-row. */
  key: string;
}

/** Format epoch ms as "HH:MM:SS" in the user's local timezone. */
function formatWallClock(epochMs: number): string {
  const d = new Date(epochMs);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

type SessionState =
  | "idle"
  | "starting"
  | "active"      // engine listening + responding
  | "dormant"     // conversational-mode only: waiting for "Hey Gilbert"
  | "stopping";

type SessionMode = "turn_based" | "conversational";

const TARGET_SAMPLE_RATE = 16000;
// AudioWorkletNode emits one chunk per ~85 ms of target-rate audio
// (~1365 samples at 16 kHz). The worklet runs off the main thread on
// a dedicated audio-rendering thread, so its callback timing isn't
// affected by React renders / TTS playback / GC pauses on the main
// thread — fixing the 20-second mic-capture lag that the old
// ScriptProcessorNode setup hit under load.
const CHUNK_DURATION_MS = 85;

// Inline AudioWorklet processor. Lives as a string + Blob URL so the
// plugin doesn't need a separate worklet asset (Vite static-asset
// handling for plugins is awkward — Blob URL is universal and works
// regardless of the build's asset-resolution config).
//
// The worklet:
//   1. Accumulates 128-sample Float32 frames from the input (audio
//      thread's quantum)
//   2. Downsamples to 16 kHz by averaging blocks of `downsampleRatio`
//      source samples per output sample
//   3. Converts each averaged sample to Int16
//   4. postMessages a complete chunk back to the main thread once
//      we've accumulated ~85 ms of target-rate audio
//
// Critical: returns `true` from process() so the audio thread keeps
// scheduling it (returning false would have the worklet GC'd after
// one frame).
const MIC_WORKLET_CODE = /* js */ `
class MicDownsampleProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this._targetRate = opts.targetSampleRate || 16000;
    this._chunkDurationMs = opts.chunkDurationMs || 85;
    // sampleRate is a global in worklet scope (set by the AudioContext)
    this._sourceRate = sampleRate;
    this._downsampleRatio = this._sourceRate / this._targetRate;
    this._chunkSamples = Math.floor((this._targetRate * this._chunkDurationMs) / 1000);
    this._sourceSamplesPerChunk = Math.ceil(this._chunkSamples * this._downsampleRatio);
    // Rolling accumulator of source-rate samples until we have enough
    // for a chunk. Float32Array because that's what input arrives as.
    this._acc = new Float32Array(0);
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0 || !input[0]) {
      return true;
    }
    const channel = input[0]; // mono — we asked for 1 channel
    // Append to accumulator.
    const merged = new Float32Array(this._acc.length + channel.length);
    merged.set(this._acc);
    merged.set(channel, this._acc.length);
    this._acc = merged;

    while (this._acc.length >= this._sourceSamplesPerChunk) {
      const slice = this._acc.subarray(0, this._sourceSamplesPerChunk);
      // Copy the remainder forward (subarray is a view; we need to
      // own the new accumulator buffer so the next iteration's
      // \`merged\` allocation doesn't trample shared storage).
      this._acc = new Float32Array(this._acc.subarray(this._sourceSamplesPerChunk));

      const out = new Int16Array(this._chunkSamples);
      for (let i = 0; i < this._chunkSamples; i++) {
        const start = Math.floor(i * this._downsampleRatio);
        const end = Math.floor((i + 1) * this._downsampleRatio);
        let sum = 0;
        let n = 0;
        for (let j = start; j < end && j < slice.length; j++) {
          sum += slice[j];
          n++;
        }
        const v = n > 0 ? sum / n : 0;
        const clamped = Math.max(-1, Math.min(1, v));
        out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      }
      // Transfer the buffer to avoid copying it across the worklet
      // boundary — single bulk move instead of element-by-element.
      this.port.postMessage(out.buffer, [out.buffer]);
    }
    return true;
  }
}
registerProcessor('mic-downsample-processor', MicDownsampleProcessor);
`;

export function VoiceAgentPage(): ReactElement {
  const { connected, rpc, subscribe } = useWebSocket();
  const [state, setState] = useState<SessionState>("idle");
  const [mode, setMode] = useState<SessionMode>("turn_based");
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptTurn[]>([]);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);

  // Audio-graph refs + the teardown helper live ABOVE the useEffect
  // subscriptions because the ``session_ended`` subscription calls
  // teardownAudio in its handler — TypeScript's strict block-scope
  // check (``tsc -b`` is stricter than ``tsc --noEmit``) rejects
  // referencing it before declaration.
  const audioCtxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);

  const teardownAudio = useCallback(() => {
    if (workletNodeRef.current) {
      // Closing the port first stops the worklet's postMessage
      // delivery; disconnect cuts it out of the audio graph so the
      // audio thread stops scheduling its process().
      try {
        workletNodeRef.current.port.close();
      } catch {
        /* ignore */
      }
      workletNodeRef.current.disconnect();
      workletNodeRef.current = null;
    }
    if (sourceRef.current) {
      sourceRef.current.disconnect();
      sourceRef.current = null;
    }
    if (streamRef.current) {
      for (const t of streamRef.current.getTracks()) t.stop();
      streamRef.current = null;
    }
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => {
        /* already closed */
      });
      audioCtxRef.current = null;
    }
  }, []);

  // Subscribe to live transcript-turn events from the backend. The
  // server emits ``voice_agent.transcript_turn`` for every "them"
  // (user-side STT commit) and "us" (LLM reply) turn so the SPA can
  // render the conversation as it happens — useful even when the
  // audio-out path is misbehaving and the user can't hear Gilbert.
  useEffect(() => {
    const unsub = subscribe("voice_agent.transcript_turn", (event) => {
      const data = event.data ?? {};
      const who = String(data.who ?? "");
      const text = String(data.text ?? "");
      const ts =
        typeof data.ts === "number" ? data.ts : Number(data.ts ?? 0);
      if (!who || !text) return;
      const receivedAt = Date.now();
      // Diagnostic: log when each transcript event arrives in the
      // browser so we can cross-reference against the server's
      // ``voice_agent.transcript_turn`` publish timestamp in the
      // journal. If wall-clock delta is small, WS delivery is
      // healthy; if it's seconds, we have WS-side starvation that
      // the AudioWorkletNode migration won't fix on its own. Keep
      // the log lean — every transcript turn fires this, on every
      // active voice session, for every user.
      // eslint-disable-next-line no-console
      console.debug(
        `[voice_agent] transcript event who=${who} chars=${text.length} received=${receivedAt} perfNow=${Math.round(performance.now())}`
      );
      const newTurn: TranscriptTurn = {
        who,
        text,
        ts,
        receivedAt,
        key: `${ts}-${who}-${Math.random().toString(36).slice(2, 8)}`,
      };
      setTranscript((prev) => [...prev, newTurn]);
    });
    return unsub;
  }, [subscribe]);

  // Subscribe to "session ended" so the SPA can flip back to idle
  // when the brain decides the conversation is over (e.g. the user
  // said "talk to you later" and the LLM called end_conversation).
  // Without this the SPA stays in active mode, holding the mic open
  // and pumping audio that nothing's listening to. Defined below the
  // teardown helper so it can call it cleanly.
  useEffect(() => {
    const unsub = subscribe("voice_agent.session_ended", () => {
      teardownAudio();
      setSessionId(null);
      setState("idle");
    });
    return unsub;
  }, [subscribe, teardownAudio]);

  // Conversational mode emits ``voice_agent.state_changed`` when the
  // server transitions between ``active`` (engine listening) and
  // ``dormant`` (only the wake-word detector listening, waiting for
  // "Hey Gilbert" to resume). Reflect the state in the UI so the
  // user knows what's going on. Mic stays open in both states — only
  // the routing on the server side changes.
  useEffect(() => {
    const unsub = subscribe("voice_agent.state_changed", (event) => {
      const data = event.data ?? {};
      const newState = String(data.state ?? "");
      if (newState === "dormant") {
        setState("dormant");
      } else if (newState === "active") {
        setState("active");
      }
    });
    return unsub;
  }, [subscribe]);

  // Auto-scroll the transcript on every new turn.
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcript]);

  const stop = useCallback(async () => {
    // ``dormant`` is a perfectly valid state to stop from too —
    // conversational mode sits there waiting for "Hey Gilbert" but
    // the user might want to hard-end without doing the dance.
    if (state !== "active" && state !== "dormant") return;
    setState("stopping");
    teardownAudio();
    if (sessionId) {
      try {
        await rpc<{ ok: boolean }>({
          type: "voice_agent.end_session",
          session_id: sessionId,
        });
      } catch {
        /* server may already be torn down */
      }
    }
    setSessionId(null);
    setState("idle");
  }, [state, sessionId, rpc, teardownAudio]);

  const start = useCallback(async () => {
    if (state !== "idle") return;
    setError(null);
    setTranscript([]);
    setState("starting");

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
    } catch (err) {
      setError(
        err instanceof Error
          ? `Microphone permission denied: ${err.message}`
          : "Microphone permission denied"
      );
      setState("idle");
      return;
    }
    streamRef.current = stream;

    // Tell the backend to open a session. It returns a session_id we
    // tag every audio chunk with. ``mode`` selects the lifecycle:
    // - ``turn_based``: classic press-button-to-talk
    // - ``conversational``: wake-word fallback after 10s of silence
    let resp: { ok: boolean; session_id?: string; error?: string };
    try {
      resp = await rpc({ type: "voice_agent.start_session", mode });
    } catch (err) {
      setError(
        err instanceof Error
          ? `Failed to start session: ${err.message}`
          : "Failed to start session"
      );
      teardownAudio();
      setState("idle");
      return;
    }
    if (!resp.ok || !resp.session_id) {
      setError(resp.error ?? "Server refused the session");
      teardownAudio();
      setState("idle");
      return;
    }
    const newSessionId = resp.session_id;
    setSessionId(newSessionId);

    // Wire up the audio graph:
    //   MediaStreamSource → AudioWorkletNode (off-thread downsample
    //                       + Int16 conversion) → muted gain →
    //                       AudioContext destination
    //
    // The AudioWorkletNode runs on a dedicated audio-rendering thread.
    // Critically, its process() callback is scheduled by the audio
    // subsystem at fixed 128-sample boundaries regardless of what's
    // happening on the main JS thread — React renders, TTS clip
    // decoding, GC pauses, etc. can't starve it.
    //
    // The previous ScriptProcessorNode ran on the main thread; under
    // sustained load (e.g. long TTS playback decoding in parallel)
    // its onaudioprocess callbacks got delayed, the audio context
    // buffered the missed input, and the SPA's transcript display
    // lagged ~20s behind real speech. That's what this migration
    // fixes.
    const audioCtx = new AudioContext();
    audioCtxRef.current = audioCtx;

    // Register the worklet via a Blob URL — keeps the worklet code
    // co-located with this component without needing Vite static-
    // asset handling for plugin frontends.
    const blob = new Blob([MIC_WORKLET_CODE], {
      type: "application/javascript",
    });
    const workletUrl = URL.createObjectURL(blob);
    try {
      await audioCtx.audioWorklet.addModule(workletUrl);
    } catch (err) {
      URL.revokeObjectURL(workletUrl);
      setError(
        err instanceof Error
          ? `Failed to load audio worklet: ${err.message}`
          : "Failed to load audio worklet"
      );
      teardownAudio();
      setState("idle");
      return;
    }
    URL.revokeObjectURL(workletUrl);

    const sourceNode = audioCtx.createMediaStreamSource(stream);
    sourceRef.current = sourceNode;

    const workletNode = new AudioWorkletNode(
      audioCtx,
      "mic-downsample-processor",
      {
        processorOptions: {
          targetSampleRate: TARGET_SAMPLE_RATE,
          chunkDurationMs: CHUNK_DURATION_MS,
        },
      }
    );
    workletNodeRef.current = workletNode;

    // Worklet posts an Int16 PCM ArrayBuffer per ~85ms chunk back to
    // the main thread. We base64 + RPC on the main thread; that's
    // unavoidable because the underlying WebSocket lives here, but
    // the audio CAPTURE itself never gets blocked by main-thread
    // work. Fire-and-forget RPC so we don't await a server roundtrip
    // before the next chunk can be queued.
    workletNode.port.onmessage = (event) => {
      const arrayBuf = event.data as ArrayBuffer;
      const bytes = new Uint8Array(arrayBuf);
      let bin = "";
      for (let i = 0; i < bytes.byteLength; i++) {
        bin += String.fromCharCode(bytes[i]);
      }
      const b64 = btoa(bin);
      void rpc({
        type: "voice_agent.send_audio_chunk",
        session_id: newSessionId,
        audio_b64: b64,
      }).catch(() => {
        /* server will have already torn the session down */
      });
    };

    sourceNode.connect(workletNode);
    // Worklet's process() runs reliably as long as the node is
    // connected to a destination. Route to a muted gain so we don't
    // echo the mic back into the speakers.
    const muted = audioCtx.createGain();
    muted.gain.value = 0;
    workletNode.connect(muted);
    muted.connect(audioCtx.destination);

    setState("active");
  }, [state, mode, rpc, teardownAudio]);

  // Tear down on unmount.
  useEffect(() => {
    return () => {
      teardownAudio();
    };
  }, [teardownAudio]);

  return (
    <div className="container mx-auto max-w-2xl py-8">
      <h1 className="text-2xl font-semibold mb-2">Voice conversation</h1>
      <p className="text-muted-foreground mb-6">
        Start a real-time voice conversation with Gilbert. The mic
        captures locally; Gilbert speaks back through this tab.
      </p>

      <Card className="p-6 flex flex-col items-center gap-4">
        {state === "idle" && (
          <>
            <div className="flex gap-2" role="radiogroup" aria-label="Mode">
              <Button
                size="sm"
                variant={mode === "turn_based" ? "default" : "outline"}
                onClick={() => setMode("turn_based")}
                role="radio"
                aria-checked={mode === "turn_based"}
              >
                Turn-based
              </Button>
              <Button
                size="sm"
                variant={mode === "conversational" ? "default" : "outline"}
                onClick={() => setMode("conversational")}
                role="radio"
                aria-checked={mode === "conversational"}
              >
                Conversational
              </Button>
            </div>
            <p className="text-xs text-muted-foreground max-w-md text-center">
              {mode === "turn_based"
                ? "Press the button, talk; Gilbert speaks back. Stays open until you (or he) ends it."
                : "Like turn-based, but after 10 seconds of silence the session drops to wake-word mode. Say “Hey Gilbert” to wake him up again."}
            </p>
            <Button size="lg" onClick={start} disabled={!connected}>
              <Mic className="mr-2 h-5 w-5" />
              Start Conversation
            </Button>
          </>
        )}
        {state === "starting" && (
          <Button size="lg" disabled>
            <Loader2 className="mr-2 h-5 w-5 animate-spin" />
            Starting…
          </Button>
        )}
        {state === "active" && (
          <Button size="lg" variant="destructive" onClick={stop}>
            <MicOff className="mr-2 h-5 w-5" />
            End Conversation
          </Button>
        )}
        {state === "dormant" && (
          <Button size="lg" variant="destructive" onClick={stop}>
            <MicOff className="mr-2 h-5 w-5" />
            End Conversation
          </Button>
        )}
        {state === "stopping" && (
          <Button size="lg" disabled>
            <Loader2 className="mr-2 h-5 w-5 animate-spin" />
            Stopping…
          </Button>
        )}

        {state === "active" && (
          <p className="text-sm text-muted-foreground">
            Listening… speak naturally. Gilbert will respond by voice.
          </p>
        )}
        {state === "dormant" && (
          <p className="flex items-center gap-2 text-sm font-medium text-amber-600 dark:text-amber-400">
            <Ear className="h-4 w-4" />
            Waiting for &ldquo;Hey Gilbert&rdquo; — say the wake phrase to resume.
          </p>
        )}
        {error && (
          <p className="text-sm text-destructive font-medium">{error}</p>
        )}
        {!connected && (
          <p className="text-sm text-muted-foreground">
            Reconnecting to Gilbert…
          </p>
        )}
      </Card>

      {transcript.length > 0 && (
        <Card className="mt-6 p-4 max-h-[400px] overflow-y-auto">
          <h2 className="text-sm font-semibold text-muted-foreground mb-3">
            Live transcript
          </h2>
          <div className="space-y-2 text-sm">
            {transcript.map((t) => (
              <div
                key={t.key}
                className={
                  t.who === "us"
                    ? "flex items-start gap-3"
                    : t.who === "them"
                      ? "flex items-start gap-3"
                      : "flex items-start gap-3 text-muted-foreground italic"
                }
              >
                {/* Trailing spaces on the timestamp + label spans
                    are intentional — the visual ``flex gap-3``
                    doesn't make it into clipboard text, so a copy
                    would otherwise read "20:56:49GilbertHey there"
                    with everything mashed together. The string-
                    literal form ``{`…: `}`` ensures the trailing
                    whitespace survives JSX tokenization. */}
                <span
                  className="shrink-0 font-mono text-xs text-muted-foreground w-20 pt-0.5"
                  title={`Received ${new Date(t.receivedAt).toLocaleString()} · session t=${t.ts.toFixed(2)}s`}
                >
                  {`${formatWallClock(t.receivedAt)} `}
                </span>
                <span
                  className={
                    "shrink-0 font-semibold w-20 " +
                    (t.who === "us"
                      ? "text-primary"
                      : t.who === "them"
                        ? "text-foreground"
                        : "text-muted-foreground")
                  }
                >
                  {`${
                    t.who === "us"
                      ? "Gilbert"
                      : t.who === "them"
                        ? "You"
                        : "System"
                  }: `}
                </span>
                <span className="flex-1 break-words">{t.text}</span>
              </div>
            ))}
            <div ref={transcriptEndRef} />
          </div>
        </Card>
      )}

      <div className="mt-6 text-xs text-muted-foreground space-y-1">
        <p>
          Tips: keep the browser tab focused, allow microphone access
          when prompted, and use a headset for the cleanest pickup.
        </p>
        <p>
          This is v1 — turn-taking only. Real-time barge-in (cutting
          Gilbert off mid-sentence) requires a different audio path
          and is coming next.
        </p>
      </div>
    </div>
  );
}
