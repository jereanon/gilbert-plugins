/**
 * useAndonFmApi — plugin-local WS RPC bindings for the Andon FM tuner.
 *
 * Components inside this plugin call ``const api = useAndonFmApi()`` and
 * get typed bindings for the ``andon_fm.*`` frame types implemented by
 * ``AndonFmService.get_ws_handlers``.
 */

import { useMemo } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import type {
  AndonFmPlayResult,
  AndonFmSpeakersResponse,
  AndonFmStationsResponse,
  AndonFmStopResult,
} from "./types";

export function useAndonFmApi() {
  const { rpc } = useWebSocket();

  return useMemo(
    () => ({
      listStations: () =>
        rpc<AndonFmStationsResponse>({
          type: "andon_fm.stations.list",
        }),

      listSpeakers: () =>
        rpc<AndonFmSpeakersResponse>({
          type: "andon_fm.speakers.list",
        }),

      playStation: (params: {
        station: string;
        speakers?: string[];
        volume?: number;
      }) =>
        rpc<AndonFmPlayResult>({
          type: "andon_fm.play",
          station: params.station,
          speakers: params.speakers,
          volume: params.volume,
        }),

      stopStation: (params: { speakers?: string[] } = {}) =>
        rpc<AndonFmStopResult>({
          type: "andon_fm.stop",
          speakers: params.speakers,
        }),

      refreshNowPlaying: () =>
        rpc<AndonFmStationsResponse>({
          type: "andon_fm.now_playing.get",
        }),
    }),
    [rpc],
  );
}
