/**
 * AndonFmPage — full-page Andon FM tuner, mounted at /media/andon-fm.
 *
 * Four AI-hosted Andon Labs stations rendered as cards with live
 * now-playing data scraped by the backend. Pressing Play opens a
 * speaker-picker dialog (multi-select speakers + volume) so the user
 * decides where each station plays each time, instead of the old
 * "always falls back to config defaults" behavior.
 *
 * Backend wiring:
 *   - ``andon_fm.stations.list`` for the card grid + defaults.
 *   - ``andon_fm.speakers.list`` for the picker dialog (lazy — only
 *     fetched the first time the dialog opens).
 *   - ``andon_fm.play`` / ``andon_fm.stop`` for the play/stop dispatch.
 *   - ``andon_fm.now_playing.changed`` event for live block updates.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useBrowserSpeaker } from "@/hooks/useBrowserSpeaker";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  RadioIcon,
  PlayIcon,
  SquareIcon,
  Volume2Icon,
  UsersIcon,
  WifiOffIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useAndonFmApi } from "./api";
import type {
  AndonFmNowPlayingChangedEvent,
  AndonFmSpeakerOption,
  AndonFmStation,
  AndonFmStationsResponse,
} from "./types";

const REFRESH_EVERY_MS = 90_000;

const HOST_THEMES: Record<
  string,
  { ring: string; chip: string; gradient: string }
> = {
  Claude: {
    ring: "ring-amber-400/60",
    chip: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
    gradient: "from-amber-500/30 via-orange-400/20 to-rose-500/30",
  },
  GPT: {
    ring: "ring-emerald-400/60",
    chip: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
    gradient: "from-emerald-500/30 via-teal-400/20 to-sky-500/30",
  },
  Gemini: {
    ring: "ring-sky-400/60",
    chip: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
    gradient: "from-sky-500/30 via-indigo-400/20 to-violet-500/30",
  },
  Grok: {
    ring: "ring-fuchsia-400/60",
    chip: "bg-fuchsia-500/15 text-fuchsia-700 dark:text-fuchsia-300",
    gradient: "from-fuchsia-500/30 via-pink-400/20 to-rose-500/30",
  },
};

const DEFAULT_THEME = HOST_THEMES.Claude;

function themeFor(host: string) {
  return HOST_THEMES[host] ?? DEFAULT_THEME;
}

function timeAgo(unix: number): string {
  if (!unix) return "—";
  const delta = Math.max(0, Date.now() / 1000 - unix);
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

export function AndonFmPage() {
  const api = useAndonFmApi();
  const { connected, subscribe } = useWebSocket();
  const queryClient = useQueryClient();
  const browser = useBrowserSpeaker();

  const [playingId, setPlayingId] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [errorId, setErrorId] = useState<string | null>(null);

  // Picker dialog state. ``pickerStation`` is non-null while open.
  // ``pickerSpeakers`` / ``pickerVolume`` mirror the user's selection
  // so we can carry it across stations within one session — pressing
  // Play on a second station starts from the previous picks rather
  // than reverting to config defaults each time.
  const [pickerStation, setPickerStation] = useState<AndonFmStation | null>(
    null,
  );
  const [pickerSpeakers, setPickerSpeakers] = useState<string[] | null>(null);
  const [pickerVolume, setPickerVolume] = useState<number | null>(null);

  const { data, isLoading, isError } = useQuery<AndonFmStationsResponse>({
    queryKey: ["andon_fm", "stations"],
    queryFn: api.listStations,
    enabled: connected,
    refetchInterval: REFRESH_EVERY_MS,
    staleTime: REFRESH_EVERY_MS / 2,
  });

  useEffect(() => {
    return subscribe("andon_fm.now_playing.changed", (event) => {
      const payload = event.data as unknown as AndonFmNowPlayingChangedEvent;
      if (!payload?.station_id) return;
      queryClient.setQueryData<AndonFmStationsResponse>(
        ["andon_fm", "stations"],
        (prev) => {
          if (!prev) return prev;
          return {
            ...prev,
            stations: prev.stations.map((s) =>
              s.id === payload.station_id
                ? {
                    ...s,
                    block: payload.block,
                    listeners: payload.listeners,
                    fetched_at: payload.fetched_at,
                    stale: false,
                  }
                : s,
            ),
          };
        },
      );
    });
  }, [subscribe, queryClient]);

  const defaults = data?.defaults ?? { speakers: ["my browser"], volume: 60 };

  const openPicker = useCallback(
    (station: AndonFmStation) => {
      setErrorId(null);
      setPickerStation(station);
      if (pickerSpeakers === null) {
        setPickerSpeakers(
          defaults.speakers.length ? [...defaults.speakers] : ["my browser"],
        );
      }
      if (pickerVolume === null) {
        setPickerVolume(defaults.volume);
      }
    },
    [defaults.speakers, defaults.volume, pickerSpeakers, pickerVolume],
  );

  const handlePlay = useCallback(
    async (
      station: AndonFmStation,
      speakers: string[],
      volume: number,
    ) => {
      setErrorId(null);
      setPendingId(station.id);
      try {
        const usesBrowser = speakers.some(
          (s) => s.trim().toLowerCase() === "my browser",
        );
        if (usesBrowser && !browser.enabled) {
          browser.setEnabled(true);
        }
        const result = await api.playStation({
          station: station.id,
          speakers: speakers.length ? speakers : undefined,
          volume,
        });
        if (!result.ok) {
          setErrorId(station.id);
          return;
        }
        setPlayingId(station.id);
      } catch {
        setErrorId(station.id);
      } finally {
        setPendingId((cur) => (cur === station.id ? null : cur));
      }
    },
    [api, browser],
  );

  const handleStop = useCallback(async () => {
    setErrorId(null);
    setPendingId("__stop__");
    try {
      await api.stopStation({
        speakers:
          pickerSpeakers && pickerSpeakers.length
            ? pickerSpeakers
            : defaults.speakers.length
              ? defaults.speakers
              : undefined,
      });
    } finally {
      setPendingId(null);
      setPlayingId(null);
    }
  }, [api, pickerSpeakers, defaults.speakers]);

  return (
    <div className="container mx-auto px-4 py-6 max-w-6xl">
      <header className="flex items-center justify-between gap-3 mb-5">
        <div className="flex items-center gap-3 min-w-0">
          <div className="rounded-md bg-foreground/5 p-2">
            <RadioIcon className="h-5 w-5 text-foreground/70" />
          </div>
          <div className="min-w-0">
            <h1 className="text-xl font-semibold leading-tight">Andon FM</h1>
            <p className="text-xs text-muted-foreground leading-tight">
              Four radio stations, each hosted by a different AI. Pick a
              station, pick which speakers play it.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {playingId && (
            <span className="hidden sm:inline-flex items-center gap-1 text-[10px] uppercase tracking-wider text-emerald-600 dark:text-emerald-400 font-semibold">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
              </span>
              On Air
            </span>
          )}
          {playingId && (
            <Button
              size="sm"
              variant="destructive"
              disabled={pendingId === "__stop__"}
              onClick={handleStop}
            >
              <SquareIcon className="h-3 w-3 mr-1" /> Stop
            </Button>
          )}
        </div>
      </header>

      {!connected ? (
        <ConnectionPlaceholder />
      ) : isLoading ? (
        <LoadingPlaceholder />
      ) : isError || !data ? (
        <ErrorPlaceholder />
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {data.stations.map((station) => (
              <StationCard
                key={station.id}
                station={station}
                isPlaying={playingId === station.id}
                isPending={pendingId === station.id}
                isError={errorId === station.id}
                onPlay={() => openPicker(station)}
                onStop={handleStop}
              />
            ))}
          </div>

          <p className="mt-4 text-[11px] text-muted-foreground flex items-center gap-1.5 flex-wrap">
            <span>Stream powered by</span>
            <a
              href="https://andonlabs.com/radio"
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2 hover:text-foreground"
            >
              Andon Labs
            </a>
            {data.last_fetch_ok ? (
              <span>· refreshed {timeAgo(data.last_fetch_ok)}</span>
            ) : data.last_fetch_error ? (
              <span className="text-rose-600">· {data.last_fetch_error}</span>
            ) : null}
          </p>
        </>
      )}

      <SpeakerPickerDialog
        station={pickerStation}
        speakers={pickerSpeakers ?? defaults.speakers}
        volume={pickerVolume ?? defaults.volume}
        onSpeakersChange={setPickerSpeakers}
        onVolumeChange={setPickerVolume}
        onClose={() => setPickerStation(null)}
        onConfirm={(station, speakers, volume) => {
          setPickerStation(null);
          handlePlay(station, speakers, volume);
        }}
      />
    </div>
  );
}

function ConnectionPlaceholder() {
  return (
    <div className="rounded-lg border border-dashed bg-card/50 px-4 py-3 text-sm text-muted-foreground flex items-center gap-2">
      <WifiOffIcon className="h-4 w-4" /> Connecting to Andon FM…
    </div>
  );
}

function LoadingPlaceholder() {
  return (
    <div className="rounded-lg border bg-card px-4 py-3 text-sm text-muted-foreground">
      Loading Andon FM…
    </div>
  );
}

function ErrorPlaceholder() {
  return (
    <div className="rounded-lg border bg-card px-4 py-3 text-sm text-rose-600">
      Couldn&apos;t load Andon FM stations.
    </div>
  );
}

interface StationCardProps {
  station: AndonFmStation;
  isPlaying: boolean;
  isPending: boolean;
  isError: boolean;
  onPlay: () => void;
  onStop: () => void;
}

function StationCard({
  station,
  isPlaying,
  isPending,
  isError,
  onPlay,
  onStop,
}: StationCardProps) {
  const theme = themeFor(station.host);
  const [imgFailed, setImgFailed] = useState(false);

  return (
    <article
      className={cn(
        "group relative rounded-lg border bg-card overflow-hidden transition",
        "hover:border-foreground/20 hover:shadow-md",
        isPlaying && `ring-2 ${theme.ring} shadow-md`,
      )}
    >
      <div className="relative aspect-square w-full bg-muted">
        {!imgFailed && station.image_url ? (
          <img
            src={station.image_url}
            alt={`${station.name} cover`}
            className="absolute inset-0 h-full w-full object-cover"
            onError={() => setImgFailed(true)}
            loading="lazy"
          />
        ) : (
          <div
            className={cn(
              "absolute inset-0 bg-gradient-to-br",
              theme.gradient,
            )}
          />
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-black/60 via-black/0 to-black/0" />
        <div className="absolute top-2 left-2 right-2 flex items-start justify-between gap-2">
          <span
            className={cn(
              "rounded-full px-2 py-0.5 text-[10px] font-medium",
              theme.chip,
            )}
          >
            {station.host}
          </span>
          {isPlaying && (
            <span className="rounded-full bg-emerald-500/90 text-white text-[9px] font-bold uppercase px-1.5 py-0.5 tracking-wider shadow">
              Live
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={isPlaying ? onStop : onPlay}
          disabled={isPending}
          aria-label={isPlaying ? `Stop ${station.name}` : `Play ${station.name}`}
          className={cn(
            "absolute bottom-2 right-2 h-10 w-10 rounded-full flex items-center justify-center",
            "bg-white/95 text-slate-900 shadow-lg backdrop-blur transition",
            "hover:scale-110 hover:bg-white",
            "disabled:opacity-60 disabled:hover:scale-100",
            isPlaying && "bg-emerald-500 text-white hover:bg-emerald-400",
          )}
        >
          {isPending ? (
            <span className="block h-3 w-3 rounded-full border-2 border-current border-t-transparent animate-spin" />
          ) : isPlaying ? (
            <SquareIcon className="h-4 w-4 fill-current" />
          ) : (
            <PlayIcon className="h-4 w-4 fill-current ml-0.5" />
          )}
        </button>
      </div>

      <div className="p-3 space-y-1.5">
        <div>
          <h3 className="text-sm font-semibold leading-tight truncate">
            {station.name}
          </h3>
          {station.block?.name ? (
            <p className="text-xs text-foreground/80 leading-tight truncate">
              {station.block.name}
            </p>
          ) : (
            <p className="text-xs text-muted-foreground italic">
              {station.stale ? "Tuning in…" : "Off-air block"}
            </p>
          )}
        </div>
        <div className="flex items-center justify-between text-[10px] text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <UsersIcon className="h-3 w-3" />
            {station.listeners > 0 ? `${station.listeners} listening` : "—"}
          </span>
          {isError ? (
            <span className="text-rose-600">Failed</span>
          ) : station.block?.description ? (
            <span
              className="truncate max-w-[60%] text-right opacity-80"
              title={station.block.description}
            >
              {station.block.description}
            </span>
          ) : (
            <span className="inline-flex items-center gap-0.5">
              <Volume2Icon className="h-3 w-3" />
              live
            </span>
          )}
        </div>
      </div>
    </article>
  );
}

interface SpeakerPickerDialogProps {
  station: AndonFmStation | null;
  speakers: string[];
  volume: number;
  onSpeakersChange: (next: string[]) => void;
  onVolumeChange: (next: number) => void;
  onClose: () => void;
  onConfirm: (station: AndonFmStation, speakers: string[], volume: number) => void;
}

function SpeakerPickerDialog({
  station,
  speakers,
  volume,
  onSpeakersChange,
  onVolumeChange,
  onClose,
  onConfirm,
}: SpeakerPickerDialogProps) {
  const api = useAndonFmApi();
  const { connected } = useWebSocket();
  const open = station !== null;

  // Lazy-load the speaker list — only fetch when the dialog first
  // becomes open. ``enabled`` flips off when closed so a stale-time
  // refetch doesn't run on hidden dialogs.
  const speakersQuery = useQuery({
    queryKey: ["andon_fm", "speakers"],
    queryFn: api.listSpeakers,
    enabled: connected && open,
    staleTime: 30_000,
  });

  const options: AndonFmSpeakerOption[] = useMemo(() => {
    return speakersQuery.data?.speakers ?? [];
  }, [speakersQuery.data]);

  const selectedSet = useMemo(
    () => new Set(speakers.map((s) => s.toLowerCase())),
    [speakers],
  );

  const toggleSpeaker = (id: string) => {
    const lower = id.toLowerCase();
    if (selectedSet.has(lower)) {
      onSpeakersChange(speakers.filter((s) => s.toLowerCase() !== lower));
    } else {
      onSpeakersChange([...speakers, id]);
    }
  };

  const handleConfirm = () => {
    if (!station) return;
    onConfirm(station, speakers, volume);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      {/*
        Cap at 50% of viewport height so the Play / Cancel buttons
        always sit above the fold, and shrink to content when there
        are few speakers — no fixed min-height (a 384px min was
        overflowing on smaller laptop viewports). ``flex flex-col``
        overrides DialogContent's default grid so ``flex-1 min-h-0``
        works on the inner body and the speakers list takes whatever
        space the header + slider + footer leave behind.
      */}
      <DialogContent
        className={cn(
          "sm:max-w-md flex flex-col",
          "max-h-[50vh]",
        )}
      >
        <DialogHeader className="shrink-0">
          <DialogTitle>
            Play {station ? station.name : "station"}
          </DialogTitle>
          <DialogDescription>
            Pick which speakers should play this station and set the
            volume.
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 min-h-0 flex flex-col gap-4 py-2">
          <fieldset className="flex-1 min-h-0 flex flex-col">
            <legend className="text-xs font-medium text-foreground/80 mb-2">
              Speakers
            </legend>
            {speakersQuery.isLoading ? (
              <p className="text-xs text-muted-foreground">
                Loading speakers…
              </p>
            ) : speakersQuery.isError ? (
              <p className="text-xs text-rose-600">
                Couldn&apos;t load speakers.
              </p>
            ) : options.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No speakers available. Enable a speaker backend in
                Settings.
              </p>
            ) : (
              <ul className="flex-1 min-h-0 space-y-1.5 overflow-y-auto pr-1">
                {options.map((opt) => {
                  const checked = selectedSet.has(opt.id.toLowerCase());
                  return (
                    <li key={`${opt.backend}:${opt.id}`}>
                      <label
                        className={cn(
                          "flex items-center gap-2.5 rounded-md border px-2.5 py-1.5 text-sm cursor-pointer transition-colors",
                          checked
                            ? "border-foreground/30 bg-foreground/5"
                            : "border-border hover:bg-muted/50",
                        )}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleSpeaker(opt.id)}
                          className="h-4 w-4"
                        />
                        <span className="flex-1 min-w-0">
                          <span className="block truncate">{opt.name}</span>
                          {opt.group_name && opt.group_name !== opt.name ? (
                            <span className="block text-[10px] text-muted-foreground truncate">
                              {opt.group_name}
                            </span>
                          ) : opt.model ? (
                            <span className="block text-[10px] text-muted-foreground truncate">
                              {opt.model}
                            </span>
                          ) : null}
                        </span>
                        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                          {opt.backend === "browser_tab"
                            ? "this tab"
                            : opt.backend || "speaker"}
                        </span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
          </fieldset>

          <div className="shrink-0">
            <label
              htmlFor="andon-fm-volume"
              className="flex items-center justify-between text-xs font-medium text-foreground/80 mb-1.5"
            >
              <span>Volume</span>
              <span className="font-mono text-muted-foreground">
                {volume}%
              </span>
            </label>
            <input
              id="andon-fm-volume"
              type="range"
              min={0}
              max={100}
              step={1}
              value={volume}
              onChange={(e) => onVolumeChange(Number(e.target.value))}
              className="w-full"
            />
          </div>
        </div>

        <DialogFooter className="shrink-0">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={handleConfirm}
            disabled={speakers.length === 0}
          >
            <PlayIcon className="h-4 w-4 mr-1" /> Play
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
