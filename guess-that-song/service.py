"""Guess That Song game service — AI tool provider with UI blocks."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.events import EventBusProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import SpeakerProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.ui import ToolOutput, UIBlock, UIElement, UIOption

from .game import GameConfig, GameState, PlayerGuess, RoundResult, SongInfo
from .scoring import score_round

logger = logging.getLogger(__name__)

CORRECT_PHRASES = [
    "Nailed it!",
    "That's it!",
    "You got it!",
    "Sharp ears!",
    "Music pro!",
    "Crushed it!",
]
WRONG_PHRASES = [
    "Not quite!",
    "Close but no cigar!",
    "Nope!",
    "Nice try!",
    "Swing and a miss!",
    "Not this time!",
]


class GuessGameService(Service):
    """Guess That Song multiplayer game, exposed as AI tools with UI blocks."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._plugin_config: dict[str, Any] = config or {}
        self._enabled: bool = False
        self._games: dict[str, GameState] = {}

        # Dependencies (resolved in start)
        self._music_svc: Any = None
        self._speaker_svc: Any = None
        self._ai_svc: Any = None
        self._event_bus: Any = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="guess_game",
            capabilities=frozenset({"guess_game", "ai_tools"}),
            requires=frozenset({"music", "speaker_control"}),
            optional=frozenset({"event_bus", "text_to_speech", "ai_chat", "configuration"}),
            ai_calls=frozenset({"guess_song_validate"}),
            toggleable=True,
            toggle_description="Guess That Song multiplayer game",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        # Load config from configuration capability if available
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)
            if not section.get("enabled", False):
                logger.info("Guess That Song service disabled")
                return
            # Merge stored config over plugin defaults
            for key in (
                "default_clip_seconds",
                "default_num_rounds",
                "default_volume",
                "max_rounds",
                "max_concurrent_games",
            ):
                if key in section:
                    self._plugin_config[key] = section[key]

        self._enabled = True
        self._music_svc = resolver.require_capability("music")
        self._speaker_svc = resolver.require_capability("speaker_control")
        self._ai_svc = resolver.get_capability("ai_chat")
        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc is not None and isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus
        logger.info("Guess That Song service started")

    async def stop(self) -> None:
        self._games.clear()
        self._enabled = False

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "guess_game"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="default_clip_seconds",
                type=ToolParameterType.INTEGER,
                description="Default clip length in seconds (2-10).",
                default=3,
            ),
            ConfigParam(
                key="default_num_rounds",
                type=ToolParameterType.INTEGER,
                description="Default number of rounds per game.",
                default=5,
            ),
            ConfigParam(
                key="default_volume",
                type=ToolParameterType.INTEGER,
                description="Default playback volume (0-100).",
                default=70,
            ),
            ConfigParam(
                key="max_rounds",
                type=ToolParameterType.INTEGER,
                description="Maximum rounds allowed per game.",
                default=20,
            ),
            ConfigParam(
                key="max_concurrent_games",
                type=ToolParameterType.INTEGER,
                description="Maximum number of simultaneous games.",
                default=3,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        for key in (
            "default_clip_seconds",
            "default_num_rounds",
            "default_volume",
            "max_rounds",
            "max_concurrent_games",
        ):
            if key in config:
                self._plugin_config[key] = config[key]

    # ── ToolProvider protocol ────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "guess_game"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="guess_song_setup",
                description=(
                    "Show the Guess That Song game setup form so the user can "
                    "pick speakers, genre, rounds, clip length, and volume. "
                    "Call this when a user wants to play Guess That Song."
                ),
                parameters=[],
                required_role="everyone",
            ),
            ToolDefinition(
                name="guess_song_create",
                description=(
                    "Create a new Guess That Song game with the given settings. "
                    "Usually called after the user submits the setup form."
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Genre or artist to search for songs.",
                    ),
                    ToolParameter(
                        name="num_rounds",
                        type=ToolParameterType.INTEGER,
                        description="Number of rounds (1-20).",
                        required=False,
                    ),
                    ToolParameter(
                        name="clip_seconds",
                        type=ToolParameterType.INTEGER,
                        description="Clip length in seconds (2-10).",
                        required=False,
                    ),
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names to play on.",
                        required=False,
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Playback volume (0-100).",
                        required=False,
                    ),
                ],
            ),
            ToolDefinition(
                name="guess_song_join",
                description="Join an existing Guess That Song game as a player.",
                parameters=[
                    ToolParameter(
                        name="game_id",
                        type=ToolParameterType.STRING,
                        description="The game ID to join.",
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="guess_song_start",
                description=(
                    "Start the game or advance to the next round. Only the host can do this. "
                    "Plays a music clip on the speakers."
                ),
                parameters=[
                    ToolParameter(
                        name="game_id",
                        type=ToolParameterType.STRING,
                        description="The game ID.",
                    ),
                ],
            ),
            ToolDefinition(
                name="guess_song_submit_guess",
                description=(
                    "Submit a player's guess for the current round. "
                    "IMPORTANT: Do NOT reveal whether the guess is correct. "
                    "Just confirm it was received. Results are shown when all "
                    "players have guessed or the host reveals."
                ),
                parameters=[
                    ToolParameter(
                        name="game_id",
                        type=ToolParameterType.STRING,
                        description="The game ID.",
                    ),
                    ToolParameter(
                        name="guess",
                        type=ToolParameterType.STRING,
                        description="The player's guess (song title and/or artist).",
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="guess_song_action",
                description=(
                    "Perform a game action: 'reveal' (force-reveal round), "
                    "'replay' (replay the clip), 'end' (end the game)."
                ),
                parameters=[
                    ToolParameter(
                        name="game_id",
                        type=ToolParameterType.STRING,
                        description="The game ID.",
                    ),
                    ToolParameter(
                        name="action",
                        type=ToolParameterType.STRING,
                        description="Action to perform.",
                        enum=["reveal", "replay", "end"],
                    ),
                ],
            ),
            ToolDefinition(
                name="guess_song_status",
                description=(
                    "Get the current status, scores, and round info for a game. "
                    "If no game_id, lists all active games."
                ),
                parameters=[
                    ToolParameter(
                        name="game_id",
                        type=ToolParameterType.STRING,
                        description="The game ID (omit to list all).",
                        required=False,
                    ),
                ],
                required_role="everyone",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str | ToolOutput:
        try:
            match name:
                case "guess_song_setup":
                    return await self._tool_setup(arguments)
                case "guess_song_create":
                    return await self._tool_create(arguments)
                case "guess_song_join":
                    return await self._tool_join(arguments)
                case "guess_song_start":
                    return await self._tool_start(arguments)
                case "guess_song_submit_guess":
                    return await self._tool_submit_guess(arguments)
                case "guess_song_action":
                    return await self._tool_action(arguments)
                case "guess_song_status":
                    return await self._tool_status(arguments)
                case _:
                    raise KeyError(f"Unknown tool: {name}")
        except Exception as e:
            logger.exception("guess_game tool error: %s", name)
            return f"Error: {e}"

    # ── Conversation state sync ────────────────────────────────────────

    _STATE_KEY = "guess_game"

    async def _sync_state(self, game: GameState) -> None:
        """Write the AI-visible game summary to conversation state."""
        if self._ai_svc is None:
            return
        try:
            await self._ai_svc.set_conversation_state(
                self._STATE_KEY,
                game.to_ai_summary(),
            )
        except Exception:
            logger.debug("Failed to sync game state to conversation", exc_info=True)

    async def _clear_state(self) -> None:
        """Remove game state from conversation state."""
        if self._ai_svc is None:
            return
        try:
            await self._ai_svc.clear_conversation_state(self._STATE_KEY)
        except Exception:
            logger.debug("Failed to clear game state from conversation", exc_info=True)

    # ── Tool implementations ─────────────────────────────────────────

    async def _tool_setup(self, args: dict[str, Any]) -> ToolOutput:
        """Return a setup form for creating a new game."""
        user_id = args.get("_user_id", "")

        # Build speaker options from the speaker service
        speaker_options: list[UIOption] = []
        try:
            if not isinstance(self._speaker_svc, SpeakerProvider):
                raise RuntimeError("Speaker service does not provide SpeakerProvider")
            for backend in self._speaker_svc.backends.values():
                for s in await backend.list_speakers():
                    speaker_options.append(UIOption(value=s.name, label=s.name))
        except Exception:
            logger.warning("Could not list speakers for setup form")

        defaults = self._plugin_config
        form = UIBlock(
            title="Guess That Song — New Game",
            for_user=user_id,
            elements=[
                UIElement(
                    type="text",
                    name="query",
                    label="Genre or Artist",
                    placeholder="e.g. 80s rock, Taylor Swift, jazz",
                    default="popular hits",
                    required=True,
                ),
                UIElement(
                    type="range",
                    name="num_rounds",
                    label="Rounds",
                    min_val=1,
                    max_val=defaults.get("max_rounds", 20),
                    step=1,
                    default=defaults.get("default_num_rounds", 5),
                ),
                UIElement(
                    type="range",
                    name="clip_seconds",
                    label="Clip Length (seconds)",
                    min_val=2,
                    max_val=10,
                    step=1,
                    default=defaults.get("default_clip_seconds", 3),
                ),
                UIElement(
                    type="range",
                    name="volume",
                    label="Volume",
                    min_val=0,
                    max_val=100,
                    step=5,
                    default=defaults.get("default_volume", 70),
                ),
                *(
                    [
                        UIElement(
                            type="checkbox",
                            name="speakers",
                            label="Speakers",
                            options=speaker_options,
                        )
                    ]
                    if speaker_options
                    else []
                ),
            ],
            submit_label="Create Game",
        )
        return ToolOutput(
            text="Here's the setup form — pick your settings and hit Create Game!",
            ui_blocks=[form],
        )

    async def _tool_create(self, args: dict[str, Any]) -> ToolOutput:
        """Create a game, fetch songs, return lobby info."""
        max_concurrent = self._plugin_config.get("max_concurrent_games", 3)
        active = sum(1 for g in self._games.values() if g.status != "ended")
        if active >= max_concurrent:
            return ToolOutput(text=f"Too many active games ({active}). End one first.")

        query = args.get("query", "popular hits")
        max_rounds = self._plugin_config.get("max_rounds", 20)
        num_rounds = min(max(args.get("num_rounds", 5), 1), max_rounds)
        clip_seconds = min(max(args.get("clip_seconds", 3), 2), 10)
        speakers = args.get("speakers", [])
        volume = args.get("volume")

        # Fetch songs
        songs = await self._fetch_songs(query)
        if not songs:
            return ToolOutput(
                text=f"Couldn't find any songs for '{query}'. Try a different search."
            )
        if len(songs) < num_rounds:
            num_rounds = len(songs)

        random.shuffle(songs)

        # Get user context from tool arguments
        user_id = args.get("_user_id", "host")
        user_name = args.get("_user_name", "Host")

        config = GameConfig(
            query=query,
            num_rounds=num_rounds,
            clip_seconds=clip_seconds,
            speakers=speakers if isinstance(speakers, list) else [],
            volume=int(volume) if volume is not None else None,
        )
        game = GameState(
            host_id=user_id,
            host_name=user_name,
            config=config,
            songs=songs[:num_rounds],
        )
        game.add_player(user_id, user_name)
        self._games[game.game_id] = game
        await self._sync_state(game)

        speaker_note = f" on {', '.join(speakers)}" if speakers else ""
        vol_note = f" at {volume}%" if volume else ""

        config_label = f"{num_rounds} rounds, {clip_seconds}s clips{speaker_note}{vol_note}"
        lobby_block = UIBlock(
            title="Guess That Song",
            elements=[
                UIElement(
                    type="label",
                    name="info",
                    label=f"**{query}**\n{config_label}",
                ),
                UIElement(
                    type="buttons",
                    name="lobby_action",
                    options=[UIOption("start", "Start Game")],
                ),
            ],
            for_user=user_id,
        )
        # Create a join block per room member (excluding host)
        room_members = args.get("_room_members", [])
        join_blocks = []
        for member in room_members:
            mid = member.get("user_id", "")
            if mid and mid != user_id:
                join_blocks.append(
                    UIBlock(
                        title="Guess That Song",
                        elements=[
                            UIElement(
                                type="label",
                                name="join_info",
                                label=(
                                    f"**{user_name}** started a game!\n**{query}** — {config_label}"
                                ),
                            ),
                            UIElement(
                                type="buttons",
                                name="join_action",
                                options=[
                                    UIOption("join", "Join Game"),
                                    UIOption("decline", "Decline"),
                                ],
                            ),
                        ],
                        for_user=mid,
                    )
                )

        return ToolOutput(
            text=(f"Game created! {query} — {config_label}. Hit Start when ready!"),
            ui_blocks=[lobby_block, *join_blocks],
        )

    async def _tool_join(self, args: dict[str, Any]) -> str:
        """Add a player to an existing game."""
        game_id = args["game_id"]
        game = self._games.get(game_id)
        if not game:
            return f"No game found with ID '{game_id}'."
        if game.status == "ended":
            return "That game has already ended."

        user_id = args.get("_user_id", "player")
        user_name = args.get("_user_name", "Player")

        if user_id in game.players:
            return f"You're already in game {game_id}!"

        game.add_player(user_id, user_name)
        await self._sync_state(game)
        return (
            f"Joined game {game_id}! {len(game.players)} player(s) ready. "
            f"Theme: {game.config.query}. Waiting for the host to start."
        )

    async def _tool_start(self, args: dict[str, Any]) -> str | ToolOutput:
        """Start a round — play a clip and return a guess form."""
        game_id = args["game_id"]
        game = self._games.get(game_id)
        if not game:
            return f"No game found with ID '{game_id}'."

        user_id = args.get("_user_id", "")
        if user_id and user_id != game.host_id:
            return "Only the host can start the game or advance rounds."

        if game.status not in ("lobby", "between_rounds"):
            return f"Can't start — game is currently '{game.status}'."

        if game.current_round >= game.config.num_rounds:
            return await self._end_game(game)

        # Advance round
        game.current_round += 1
        game.guesses.clear()
        game.status = "playing"
        song = game.current_song
        if not song:
            return "No more songs available."

        await self._sync_state(game)

        # Create a guess form per player (sent before clip plays
        # so players can answer as soon as they hear it)
        speakers = game.config.speakers or []
        speakers_msg = f" on {', '.join(speakers)}" if speakers else ""
        round_label = f"Round {game.current_round} of {game.config.num_rounds}"
        clip_label = f"Playing {game.config.clip_seconds}s clip{speakers_msg}... What song is this?"

        # Announce and play clip in the background after forms are returned
        async def _announce_and_play() -> None:
            try:
                await self._announce_round_start(game)
                await self._play_clip(game, song)
            except Exception:
                logger.exception("Failed to announce/play clip for round %d", game.current_round)

        asyncio.create_task(_announce_and_play())
        guess_blocks = [
            UIBlock(
                title=round_label,
                elements=[
                    UIElement(
                        type="label",
                        name="prompt",
                        label=clip_label,
                    ),
                    UIElement(
                        type="text",
                        name="guess",
                        label="Your Guess",
                        placeholder="Song title and/or artist",
                        required=True,
                    ),
                ],
                submit_label="Submit Guess",
                for_user=pid,
            )
            for pid in game.players
        ]

        return ToolOutput(
            text=(
                f"{round_label} — "
                f"playing a {game.config.clip_seconds}s clip"
                f"{speakers_msg}. Type your guess!"
            ),
            ui_blocks=guess_blocks,
        )

    async def _tool_submit_guess(self, args: dict[str, Any]) -> str | ToolOutput:
        """Record a player's guess. Auto-reveal when all have guessed."""
        game_id = args["game_id"]
        game = self._games.get(game_id)
        if not game:
            return f"No game found with ID '{game_id}'."
        if game.status != "playing":
            return "No round in progress — nothing to guess."

        user_id = args.get("_user_id", "player")
        user_name = args.get("_user_name", "Player")

        if user_id not in game.players:
            return "You're not in this game. Join first!"
        if user_id in game.guesses:
            return "You already guessed this round!"

        guess_text = args.get("guess", "").strip()
        if not guess_text:
            return "Please provide a guess."

        game.guesses[user_id] = PlayerGuess(
            player_id=user_id,
            player_name=user_name,
            guess_text=guess_text,
        )
        await self._sync_state(game)

        remaining = len(game.players) - len(game.guesses)
        if remaining > 0:
            return f"Got your guess! Waiting on {remaining} more player(s)..."

        # All guesses in — reveal
        return await self._reveal_round(game)

    async def _tool_action(self, args: dict[str, Any]) -> str | ToolOutput:
        """Handle host actions: reveal, replay, end."""
        game_id = args["game_id"]
        game = self._games.get(game_id)
        if not game:
            return f"No game found with ID '{game_id}'."

        action = args.get("action", "")

        match action:
            case "reveal":
                if game.status != "playing":
                    return "No round in progress to reveal."
                return await self._reveal_round(game)
            case "replay":
                if game.current_song is None:
                    return "No clip to replay."
                await self._play_clip(game, game.current_song)
                return "Replaying the clip!"
            case "end":
                return await self._end_game(game)
            case _:
                return f"Unknown action: {action}"

    async def _tool_status(self, args: dict[str, Any]) -> str:
        """Return game status / scoreboard."""
        game_id = args.get("game_id")

        if not game_id:
            active = [g for g in self._games.values() if g.status != "ended"]
            if not active:
                return "No active games."
            lines = ["**Active Games:**"]
            for g in active:
                players = ", ".join(g.players.values())
                lines.append(
                    f"- **{g.game_id}** ({g.status}) — {g.config.query}, "
                    f"round {g.current_round}/{g.config.num_rounds}, "
                    f"players: {players}"
                )
            return "\n".join(lines)

        game = self._games.get(game_id)
        if not game:
            return f"No game found with ID '{game_id}'."

        lines = [
            f"**Game {game.game_id}** — {game.status}",
            f"Theme: {game.config.query}",
            f"Round: {game.current_round}/{game.config.num_rounds}",
            f"Players: {', '.join(game.players.values())}",
            "",
            game.format_scores(),
        ]
        if game.status == "playing":
            guessed = list(game.guesses.keys())
            waiting = [n for uid, n in game.players.items() if uid not in guessed]
            if waiting:
                lines.append(f"\nWaiting on: {', '.join(waiting)}")
        return "\n".join(lines)

    # ── Game logic ───────────────────────────────────────────────────

    async def _reveal_round(self, game: GameState) -> ToolOutput:
        """Score guesses, update standings, return results with action buttons."""
        song = game.current_song
        if not song:
            return ToolOutput(text="No song to reveal.")

        guesses = list(game.guesses.values())
        results = await score_round(guesses, song, ai_svc=self._ai_svc)

        # Update scores
        for r in results:
            game.scores[r["player_id"]] = game.scores.get(r["player_id"], 0) + r["points"]

        # Store round result
        game.round_results.append(
            RoundResult(
                round_number=game.current_round,
                song=song,
            )
        )

        game.status = "between_rounds"
        await self._sync_state(game)

        # Build reveal text
        lines = []
        if song.album_art_url:
            lines.append(f"![{song.title}]({song.album_art_url})")
        lines.append(f"**{song.title}** by {song.artist}\n")
        lines.append("*Scoring: 1pt title, +1pt artist, +1pt fastest correct*\n")

        correct_players = []
        for r in results:
            if r["got_title"]:
                bonuses = []
                if r["got_artist"]:
                    bonuses.append("+1 artist")
                if r["is_fastest"]:
                    bonuses.append("+1 fastest")
                bonus_str = f" ({', '.join(bonuses)})" if bonuses else ""
                lines.append(
                    f'- {r["player_name"]}: "{r["guess_text"]}" — '
                    f"{random.choice(CORRECT_PHRASES)} **+{r['points']}pt{'s' if r['points'] != 1 else ''}**{bonus_str}"
                )
                correct_players.append(r["player_name"])
            else:
                lines.append(
                    f'- {r["player_name"]}: "{r["guess_text"]}" — {random.choice(WRONG_PHRASES)}'
                )

        if guesses and not correct_players:
            lines.append("\nStumped everyone! That was a tough one.")
        elif not guesses:
            lines.append("No guesses this round!")

        lines.append(f"\n{game.format_scores()}")
        remaining = game.rounds_remaining
        if remaining > 0:
            lines.append(f"\n{remaining} round{'s' if remaining != 1 else ''} left!")
        else:
            lines.append("\nThat was the last round!")

        reveal_text = "\n".join(lines)

        # TTS announce round results
        await self._announce_results(game, song, correct_players)

        # Last round — auto-end the game
        if remaining == 0:
            return await self._end_game(game)

        # Action buttons for mid-game rounds
        action_block = UIBlock(
            title="",
            elements=[
                UIElement(
                    type="buttons",
                    name="round_action",
                    options=[
                        UIOption("next", "Next Round"),
                        UIOption("replay", "Replay Clip"),
                        UIOption("scores", "Scores"),
                        UIOption("end", "End Game"),
                    ],
                ),
            ],
            for_user=game.host_id,
        )

        return ToolOutput(text=reveal_text, ui_blocks=[action_block])

    async def _end_game(self, game: GameState) -> ToolOutput:
        """End the game, show final scores, clean up."""
        game.status = "ended"
        final_text = game.format_final_scores()

        # TTS final scores
        await self._announce_final(game)

        # Clean up
        self._games.pop(game.game_id, None)
        await self._clear_state()

        return ToolOutput(text=final_text, ui_blocks=[])

    # ── Song fetching ────────────────────────────────────────────────

    async def _fetch_songs(self, query: str) -> list[SongInfo]:
        """Search for songs using multiple query variations."""
        songs: list[SongInfo] = []
        seen: set[str] = set()

        queries = [query, f"{query} hits", f"{query} top", f"best {query}"]
        for q in queries:
            if len(songs) >= 15:
                break
            try:
                results = await self._music_svc.search(q, limit=10)
                for t in results.tracks:
                    if t.track_id in seen:
                        continue
                    seen.add(t.track_id)
                    artists = ", ".join(a.name for a in t.artists)
                    art_url = t.album.album_art_url if t.album else ""
                    songs.append(
                        SongInfo(
                            track_id=t.track_id,
                            title=t.name,
                            artist=artists,
                            uri=t.uri,
                            duration_seconds=t.duration_seconds,
                            album_art_url=art_url,
                        )
                    )
            except Exception:
                logger.exception("Song search failed for query: %s", q)

        # Fallback: try playlists
        if len(songs) < 10:
            try:
                results = await self._music_svc.search(query, limit=5)
                if results.playlists:
                    detail = await self._music_svc.get_playlist(results.playlists[0].playlist_id)
                    if detail:
                        for t in detail.tracks:
                            if t.track_id in seen:
                                continue
                            seen.add(t.track_id)
                            artists = ", ".join(a.name for a in t.artists)
                            art_url = t.album.album_art_url if t.album else ""
                            songs.append(
                                SongInfo(
                                    track_id=t.track_id,
                                    title=t.name,
                                    artist=artists,
                                    uri=t.uri,
                                    duration_seconds=t.duration_seconds,
                                    album_art_url=art_url,
                                )
                            )
            except Exception:
                logger.exception("Playlist fallback search failed")

        return songs

    # ── Clip playback ────────────────────────────────────────────────

    async def _play_clip(self, game: GameState, song: SongInfo) -> list[str]:
        """Play a random clip of a song. Returns speaker names played on."""
        try:
            # Calculate random start position
            duration = song.duration_seconds or 180.0
            clip_len = game.config.clip_seconds
            max_start = max(10, duration - clip_len - 10)
            start_pos = random.uniform(10, max_start) if max_start > 10 else 10.0

            # Play via MusicService
            await self._music_svc.play_track(
                track_id=song.track_id,
                speaker_names=game.config.speakers or None,
                volume=game.config.volume,
                position_seconds=start_pos,
            )

            # Schedule stop after clip_seconds
            speakers = game.config.speakers or None

            async def stop_after() -> None:
                await asyncio.sleep(clip_len)
                try:
                    await self._speaker_svc.stop_speakers(speakers)
                except Exception:
                    logger.warning("Failed to stop speakers after clip")

            asyncio.create_task(stop_after())
            return game.config.speakers or []

        except Exception:
            logger.exception("Failed to play clip for %s", song.title)
            return []

    # ── TTS announcements ────────────────────────────────────────────

    async def _announce(self, game: GameState, text: str) -> None:
        """Announce text on the game's speakers via TTS."""
        await self._speaker_svc.announce(
            text,
            speaker_names=game.config.speakers or None,
            volume=game.config.volume,
            context="Lively game-show host hosting a music guessing game",
        )

    async def _announce_round_start(self, game: GameState) -> None:
        """Announce round start on speakers via TTS."""
        try:
            total = game.config.num_rounds
            if game.current_round == 1:
                player_names = list(game.players.values())
                if len(player_names) == 1:
                    players_str = player_names[0]
                elif len(player_names) == 2:
                    players_str = f"{player_names[0]} and {player_names[1]}"
                else:
                    players_str = ", ".join(player_names[:-1]) + f", and {player_names[-1]}"
                speak = (
                    f"Welcome to Guess That Song! "
                    f"Today's theme is {game.config.query}. "
                    f"{total} rounds, "
                    f"{game.config.clip_seconds} second clips. "
                    f"Playing today: {players_str}. "
                    f"Here comes round 1!"
                )
            else:
                speak = f"Round {game.current_round} of {total}. Listen closely!"
            await self._announce(game, speak)
        except Exception:
            logger.exception("TTS round announcement failed")

    async def _announce_results(
        self,
        game: GameState,
        song: SongInfo,
        correct_players: list[str],
    ) -> None:
        """Announce round results on speakers via TTS."""
        try:
            speak = f"The answer is {song.title} by {song.artist}! "
            if correct_players:
                if len(correct_players) == 1:
                    speak += f"{correct_players[0]} got it!"
                else:
                    speak += f"{', '.join(correct_players[:-1])} and {correct_players[-1]} got it!"
            else:
                speak += "Nobody got it! That was a tough one."
            await self._announce(game, speak)
        except Exception:
            logger.exception("TTS round results announcement failed")

    async def _announce_final(self, game: GameState) -> None:
        """Announce final scores on speakers via TTS."""
        try:
            sorted_scores = sorted(
                game.scores.items(),
                key=lambda x: x[1],
                reverse=True,
            )
            speak = "Game over! Final scores: "
            for uid, score in sorted_scores:
                name = game.players.get(uid, uid)
                pts = "point" if score == 1 else "points"
                speak += f"{name} with {score} {pts}. "
            if sorted_scores:
                top = sorted_scores[0][1]
                winners = [game.players.get(uid, uid) for uid, s in sorted_scores if s == top]
                if len(winners) > 1:
                    speak += f"It's a tie between {' and '.join(winners)}!"
                else:
                    speak += f"{winners[0]} wins! Congratulations!"
            speak += " Thanks for playing!"
            await self._announce(game, speak)
        except Exception:
            logger.exception("TTS final announcement failed")
