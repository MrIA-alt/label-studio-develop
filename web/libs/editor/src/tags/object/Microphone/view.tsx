import { observer } from "mobx-react";
import { type FC, useCallback, useEffect, useImperativeHandle, useRef, useState, forwardRef } from "react";
import { ErrorMessage } from "../../../components/ErrorMessage/ErrorMessage";
import { Hotkey } from "../../../core/Hotkey";
import { cn } from "../../../utils/bem";

import "./view.scss";

interface MicrophoneProps {
  item: any;
}

const formatTime = (secs: number) => {
  if (!Number.isFinite(secs) || secs < 0) return "0:00";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
};

const Kbd: FC<{ shortcut: string }> = ({ shortcut }) => (
  <kbd className={cn("microphone-tag").elem("kbd").toClassName()}>{shortcut}</kbd>
);

// ── Live scrolling waveform during recording ──────────────────────────
const LiveWaveform: FC<{ analyser: AnalyserNode | null }> = ({ analyser }) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rafRef = useRef<number>(0);
  const historyRef = useRef<number[]>([]);

  useEffect(() => {
    if (!analyser || !canvasRef.current) return;

    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);

    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);
    let frameCount = 0;

    const draw = () => {
      rafRef.current = requestAnimationFrame(draw);
      analyser.getByteTimeDomainData(dataArray);

      frameCount++;
      if (frameCount % 3 === 0) {
        let min = 255;
        let max = 0;
        for (let i = 0; i < bufferLength; i++) {
          if (dataArray[i] < min) min = dataArray[i];
          if (dataArray[i] > max) max = dataArray[i];
        }
        historyRef.current.push((max - min) / 255);
        const maxCols = Math.floor(rect.width / 3);
        if (historyRef.current.length > maxCols) {
          historyRef.current = historyRef.current.slice(-maxCols);
        }
      }

      const w = rect.width;
      const h = rect.height;
      const midY = h / 2;

      ctx.fillStyle = "#f9f8f6";
      ctx.fillRect(0, 0, w, h);

      ctx.strokeStyle = "#e1ded5";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, midY);
      ctx.lineTo(w, midY);
      ctx.stroke();

      const history = historyRef.current;
      const barW = 2;
      const gap = 1;
      const step = barW + gap;

      for (let i = 0; i < history.length; i++) {
        const x = w - (history.length - i) * step;
        if (x < 0) continue;
        const amplitude = history[i];
        const barH = Math.max(2, amplitude * (h - 12));
        const alpha = 0.4 + amplitude * 0.6;
        ctx.fillStyle = `rgba(204, 94, 70, ${alpha})`;
        ctx.beginPath();
        ctx.roundRect(x, midY - barH / 2, barW, barH, 1);
        ctx.fill();
      }
    };

    historyRef.current = [];
    draw();
    return () => cancelAnimationFrame(rafRef.current);
  }, [analyser]);

  return (
    <canvas
      ref={canvasRef}
      className={cn("microphone-tag").elem("live-waveform").toClassName()}
    />
  );
};

// ── Decode audio from blob or URL into ArrayBuffer ────────────────────
async function loadAudioBuffer(audioBlob: Blob | null, audioURL: string): Promise<AudioBuffer> {
  let arrayBuffer: ArrayBuffer;

  if (audioBlob) {
    arrayBuffer = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onloadend = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = reject;
      reader.readAsArrayBuffer(audioBlob);
    });
  } else {
    const resp = await fetch(audioURL);
    if (!resp.ok) throw new Error(`Failed to load audio (HTTP ${resp.status})`);
    arrayBuffer = await resp.arrayBuffer();
  }

  const audioCtx = new AudioContext();
  const buffer = await audioCtx.decodeAudioData(arrayBuffer);
  audioCtx.close();
  return buffer;
}

// ── Static waveform + playback ────────────────────────────────────────
export interface PlaybackWaveformHandle {
  togglePlay: () => void;
}

const PlaybackWaveform = forwardRef<PlaybackWaveformHandle, { audioURL: string; audioBlob: Blob | null }>(
  ({ audioURL, audioBlob }, ref) => {
    const canvasRef = useRef<HTMLCanvasElement | null>(null);
    const audioRef = useRef<HTMLAudioElement | null>(null);
    const rafRef = useRef<number>(0);
    const [playing, setPlaying] = useState(false);
    const [currentTime, setCurrentTime] = useState(0);
    const [duration, setDuration] = useState(0);
    const peaksRef = useRef<{ min: number; max: number }[]>([]);
    const canvasDims = useRef({ w: 0, h: 0 });

    const togglePlay = useCallback(() => {
      const audio = audioRef.current;
      if (!audio) return;
      if (playing) {
        audio.pause();
      } else {
        audio.play();
      }
    }, [playing]);

    useImperativeHandle(ref, () => ({ togglePlay }), [togglePlay]);

    useEffect(() => {
      if (!audioURL || !canvasRef.current) return;

      const canvas = canvasRef.current;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.scale(dpr, dpr);
      canvasDims.current = { w: rect.width, h: rect.height };

      loadAudioBuffer(audioBlob, audioURL)
        .then((buffer) => {
          setDuration(buffer.duration);

          const channelData = buffer.getChannelData(0);
          const w = rect.width;
          const samplesPerPixel = Math.max(1, Math.floor(channelData.length / w));
          const peaks: { min: number; max: number }[] = [];

          for (let i = 0; i < w; i++) {
            let lo = 1;
            let hi = -1;
            const start = i * samplesPerPixel;
            const end = Math.min(start + samplesPerPixel, channelData.length);
            for (let j = start; j < end; j++) {
              if (channelData[j] < lo) lo = channelData[j];
              if (channelData[j] > hi) hi = channelData[j];
            }
            peaks.push({ min: lo, max: hi });
          }
          peaksRef.current = peaks;
          drawWaveform(ctx, rect.width, rect.height, peaks, 0);
        })
        .catch((e) => console.error("Failed to decode audio for waveform", e));
    }, [audioURL, audioBlob]);

    useEffect(() => {
      if (!playing) return;

      const canvas = canvasRef.current;
      const ctx = canvas?.getContext("2d");
      if (!ctx) return;
      const { w, h } = canvasDims.current;

      const animate = () => {
        rafRef.current = requestAnimationFrame(animate);
        const audio = audioRef.current;
        if (!audio) return;
        setCurrentTime(audio.currentTime);
        const progress = duration > 0 ? audio.currentTime / duration : 0;
        drawWaveform(ctx, w, h, peaksRef.current, progress);
      };

      animate();
      return () => cancelAnimationFrame(rafRef.current);
    }, [playing, duration]);

    const drawWaveform = (
      ctx: CanvasRenderingContext2D,
      w: number, h: number,
      peaks: { min: number; max: number }[],
      progress: number,
    ) => {
      const midY = h / 2;
      const progressX = Math.floor(progress * w);

      ctx.clearRect(0, 0, w * 2, h * 2);
      ctx.fillStyle = "#f9f8f6";
      ctx.fillRect(0, 0, w, h);

      ctx.strokeStyle = "#e1ded5";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, midY);
      ctx.lineTo(w, midY);
      ctx.stroke();

      for (let i = 0; i < peaks.length; i++) {
        const { min, max } = peaks[i];
        const top = midY + min * (h / 2 - 4);
        const bottom = midY + max * (h / 2 - 4);
        const barH = Math.max(1, bottom - top);
        ctx.fillStyle = i < progressX ? "rgba(76, 95, 169, 0.85)" : "rgba(164, 159, 149, 0.55)";
        ctx.fillRect(i, top, 1, barH);
      }

      if (progress > 0 && progress < 1) {
        ctx.strokeStyle = "#4c5fa9";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(progressX, 2);
        ctx.lineTo(progressX, h - 2);
        ctx.stroke();

        ctx.fillStyle = "#4c5fa9";
        ctx.beginPath();
        ctx.arc(progressX, 2, 3, 0, Math.PI * 2);
        ctx.fill();
        ctx.beginPath();
        ctx.arc(progressX, h - 2, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    };

    const handleCanvasClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current;
      const audio = audioRef.current;
      if (!canvas || !audio || !duration) return;

      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const ratio = x / rect.width;
      audio.currentTime = ratio * duration;
      setCurrentTime(audio.currentTime);

      const ctx = canvas.getContext("2d");
      const { w, h } = canvasDims.current;
      if (ctx) drawWaveform(ctx, w, h, peaksRef.current, ratio);
    };

    return (
      <div className={cn("microphone-tag").elem("player").toClassName()}>
        <audio
          ref={audioRef}
          src={audioURL}
          preload="auto"
          onPlay={() => setPlaying(true)}
          onPause={() => {
            setPlaying(false);
            const audio = audioRef.current;
            const ctx = canvasRef.current?.getContext("2d");
            const { w, h } = canvasDims.current;
            if (audio && ctx) {
              drawWaveform(ctx, w, h, peaksRef.current, duration > 0 ? audio.currentTime / duration : 0);
            }
          }}
          onEnded={() => {
            setPlaying(false);
            setCurrentTime(0);
            const ctx = canvasRef.current?.getContext("2d");
            const { w, h } = canvasDims.current;
            if (ctx) drawWaveform(ctx, w, h, peaksRef.current, 0);
          }}
        />
        <canvas
          ref={canvasRef}
          className={cn("microphone-tag").elem("playback-waveform").toClassName()}
          onClick={handleCanvasClick}
        />
        <div className={cn("microphone-tag").elem("player-controls").toClassName()}>
          <button
            type="button"
            className={cn("microphone-tag").elem("play-btn").toClassName()}
            onClick={togglePlay}
            aria-label={playing ? "Pause" : "Play"}
          >
            {playing ? (
              <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                <rect x="3" y="2" width="4" height="12" rx="1" />
                <rect x="9" y="2" width="4" height="12" rx="1" />
              </svg>
            ) : (
              <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                <path d="M4 2.5v11l9-5.5z" />
              </svg>
            )}
          </button>
          <span className={cn("microphone-tag").elem("time").toClassName()}>
            {formatTime(currentTime)}
          </span>
          <div className={cn("microphone-tag").elem("progress-track").toClassName()}>
            <div
              className={cn("microphone-tag").elem("progress-fill").toClassName()}
              style={{ width: `${duration > 0 ? (currentTime / duration) * 100 : 0}%` }}
            />
          </div>
          <span className={cn("microphone-tag").elem("time").toClassName()}>
            {formatTime(duration)}
          </span>
        </div>
      </div>
    );
  },
);

// ── Main Microphone component ─────────────────────────────────────────
const MicrophoneView: FC<MicrophoneProps> = observer(({ item }) => {
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const playbackRef = useRef<PlaybackWaveformHandle | null>(null);

  // Keep refs to latest callbacks so hotkeys always call the current version
  const startRecordingRef = useRef<() => void>(() => {});
  const stopRecordingRef = useRef<() => void>(() => {});
  const rerecordRef = useRef<() => void>(() => {});

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      if (mediaRecorderRef.current?.state === "recording") {
        mediaRecorderRef.current.stop();
      }
      audioCtxRef.current?.close();
    };
  }, []);

  const getMimeType = useCallback(() => {
    if (item.format === "wav") {
      return MediaRecorder.isTypeSupported("audio/wav") ? "audio/wav" : "audio/webm";
    }
    return "audio/webm";
  }, [item.format]);

  const startRecording = useCallback(async () => {
    if (item.recording || item.readonly) return;
    try {
      chunksRef.current = [];
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error(
          "Microphone access requires a secure context (HTTPS or localhost). " +
          "Please access Label Studio via HTTPS or http://localhost.",
        );
      }
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = getMimeType();
      const recorder = new MediaRecorder(stream, { mimeType });

      const audioCtx = new AudioContext();
      audioCtxRef.current = audioCtx;
      const source = audioCtx.createMediaStreamSource(stream);
      const analyserNode = audioCtx.createAnalyser();
      analyserNode.fftSize = 2048;
      source.connect(analyserNode);
      setAnalyser(analyserNode);

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) {
          chunksRef.current.push(e.data);
        }
      };

      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        audioCtx.close();
        audioCtxRef.current = null;
        setAnalyser(null);

        if (timerRef.current) {
          clearInterval(timerRef.current);
          timerRef.current = null;
        }

        const blob = new Blob(chunksRef.current, { type: mimeType });
        const url = URL.createObjectURL(blob);

        item.setAudioBlob(blob);
        item.setAudioURL(url);
        item.setRecording(false);

        // immediately upload to server
        item.uploadAudio();
      };

      mediaRecorderRef.current = recorder;
      recorder.start(100);
      item.setRecording(true);
      setElapsed(0);

      const startTime = Date.now();
      timerRef.current = setInterval(() => {
        const secs = Math.floor((Date.now() - startTime) / 1000);
        setElapsed(secs);
        if (secs >= item.maxDurationSeconds) {
          stopRecording();
        }
      }, 250);
    } catch (err: any) {
      item.onError(err);
    }
  }, [getMimeType, item]);

  const stopRecording = useCallback(() => {
    if (mediaRecorderRef.current?.state === "recording") {
      mediaRecorderRef.current.stop();
    }
  }, []);

  const rerecord = useCallback(() => {
    if (item.readonly) return;
    item.clearRecording();
    setElapsed(0);
  }, [item]);

  // Update refs so hotkeys always use the latest callbacks
  startRecordingRef.current = startRecording;
  stopRecordingRef.current = stopRecording;
  rerecordRef.current = rerecord;

  // Register keyboard shortcuts
  useEffect(() => {
    const hotkeys = Hotkey("Microphone", "Microphone Recording");

    hotkeys.addKey("r", () => {
      if (item.recording) return;
      if (item.hasRecording) {
        rerecordRef.current();
      } else {
        startRecordingRef.current();
      }
    }, "Record / Re-record audio");

    hotkeys.addKey("s", () => {
      stopRecordingRef.current();
    }, "Stop recording");

    hotkeys.addKey("space", () => {
      if (item.recording) {
        stopRecordingRef.current();
      } else if (item.hasRecording) {
        playbackRef.current?.togglePlay();
      } else {
        startRecordingRef.current();
      }
    }, "Play/Pause/Record toggle");

    return () => {
      hotkeys.unbindAll();
    };
  }, []);

  const isReadOnly = item.readonly;

  return (
    <div className={cn("microphone-tag").toClassName()}>
      {/* Errors */}
      {item.errors?.length > 0 && (
        <div className={cn("microphone-tag").elem("errors").toClassName()}>
          {item.errors.map((error: any, i: number) => (
            <ErrorMessage key={`err-${i}`} error={error} />
          ))}
        </div>
      )}

      {/* Uploading indicator */}
      {item.uploading && (
        <div className={cn("microphone-tag").elem("uploading").toClassName()}>
          <span className={cn("microphone-tag").elem("spinner").toClassName()} />
          Uploading recording...
        </div>
      )}

      {/* Idle — record button */}
      {!item.recording && !item.hasRecording && !item.uploading && !isReadOnly && (
        <button
          type="button"
          className={cn("microphone-tag").elem("record-btn").toClassName()}
          onClick={startRecording}
        >
          <span className={cn("microphone-tag").elem("record-dot").toClassName()} />
          Record <Kbd shortcut="R" />
        </button>
      )}

      {/* Recording — live waveform + timer + stop */}
      {item.recording && (
        <div className={cn("microphone-tag").elem("recording").toClassName()}>
          <LiveWaveform analyser={analyser} />
          <div className={cn("microphone-tag").elem("recording-bar").toClassName()}>
            <span className={cn("microphone-tag").elem("pulse").toClassName()} />
            <span className={cn("microphone-tag").elem("timer").toClassName()}>
              {formatTime(elapsed)} / {formatTime(item.maxDurationSeconds)}
            </span>
            <button
              type="button"
              className={cn("microphone-tag").elem("stop-btn").toClassName()}
              onClick={stopRecording}
            >
              <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
                <rect x="1" y="1" width="10" height="10" rx="2" />
              </svg>
              Stop <Kbd shortcut="S" />
            </button>
          </div>
        </div>
      )}

      {/* Playback — waveform + controls + re-record */}
      {item.hasRecording && !item.recording && !item.uploading && (
        <div className={cn("microphone-tag").elem("playback").toClassName()}>
          <PlaybackWaveform ref={playbackRef} audioURL={item.audioURL} audioBlob={item.audioBlob} />
          {!isReadOnly && (
            <div className={cn("microphone-tag").elem("playback-actions").toClassName()}>
              <span className={cn("microphone-tag").elem("hint").toClassName()}>
                <Kbd shortcut="Space" /> Play/Pause
              </span>
              <button
                type="button"
                className={cn("microphone-tag").elem("rerecord-btn").toClassName()}
                onClick={rerecord}
              >
                Re-record <Kbd shortcut="R" />
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
});

export const Microphone: FC<MicrophoneProps> = observer(({ item }) => {
  return <MicrophoneView item={item} />;
});
