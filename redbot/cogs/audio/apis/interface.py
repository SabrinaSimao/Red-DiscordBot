import asyncio
import datetime
import json
import logging
import random
import time
from collections import namedtuple
from pathlib import Path
from typing import Callable, List, Mapping, MutableMapping, Optional, Tuple, Union

import aiohttp
import discord
import lavalink
from lavalink.rest_api import LoadResult

from redbot.cogs.audio.apis.playlist_wrapper import PlaylistWrapper
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator
from redbot.core.utils.dbtools import APSWConnectionWrapper
from .global_db import GlobalCacheWrapper
from .local_db import LocalCacheWrapper
from .playlist_interface import get_playlist
from .spotify import SpotifyWrapper
from .youtube import YouTubeWrapper
from ..audio_dataclasses import Query
from ..audio_logging import IS_DEBUG, debug_exc_log
from ..errors import DatabaseError, SpotifyFetchError, TrackEnqueueError
from ..utils import CacheLevel, Notifier

_ = Translator("Audio", __file__)
log = logging.getLogger("red.cogs.Audio.api.AudioAPIInterface")
_TOP_100_US = "https://www.youtube.com/playlist?list=PL4fGSI1pDJn5rWitrRWFKdm-ulaFiIyoK"
# TODO: Get random from global Cache


class AudioAPIInterface:
    """Handles music queries.

    Always tries the Local cache first, then Global cache before making API calls.
    """

    def __init__(
        self, bot: Red, config: Config, session: aiohttp.ClientSession, conn: APSWConnectionWrapper
    ):
        self.bot = bot
        self.config = config
        self.conn = conn
        self.spotify_api: SpotifyWrapper = SpotifyWrapper(self.bot, self.config, session)
        self.youtube_api: YouTubeWrapper = YouTubeWrapper(self.bot, self.config, session)
        self.local_cache_api = LocalCacheWrapper(self.bot, self.config, self.conn)
        self.global_cache_api = GlobalCacheWrapper(self.bot, self.config, session)
        self._session: aiohttp.ClientSession = session
        self._tasks: MutableMapping = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialises the Local Cache connection"""
        await self.local_cache_api.lavalink.init()

    def close(self) -> None:
        """Closes the Local Cache connection"""
        self.local_cache_api.lavalink.close()

    async def get_random_track_from_db(self) -> Optional[MutableMapping]:
        """Get a random track from the local database and return it"""
        track = {}
        try:
            query_data = {}
            date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
            date = int(date.timestamp())
            query_data["day"] = date
            max_age = await self.config.cache_age()
            maxage = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(
                days=max_age
            )
            maxage_int = int(time.mktime(maxage.timetuple()))
            query_data["maxage"] = maxage_int
            track = await self.local_cache_api.lavalink.fetch_random(query_data)
            if track:
                if track.get("loadType") == "V2_COMPACT":
                    track["loadType"] = "V2_COMPAT"
                results = LoadResult(track)
                track = random.choice(list(results.tracks))
        except Exception as exc:
            debug_exc_log(log, exc, "Failed to fetch a random track from database")
            track = {}

        if not track:
            return None

        return track

    async def route_tasks(
        self,
        action_type: str = None,
        table: str = None,
        data: Union[List[MutableMapping], MutableMapping] = None,
    ) -> None:
        """Separate the tasks and run them in the appropriate functions"""

        if not data:
            return
        if action_type == "insert" and isinstance(data, list):
            if table == "lavalink":
                await self.local_cache_api.lavalink.insert(data)
            elif table == "youtube":
                await self.local_cache_api.youtube.insert(data)
            elif table == "spotify":
                await self.local_cache_api.spotify.insert(data)
        elif action_type == "update":
            if table == "lavalink":
                await self.local_cache_api.lavalink.update(data)
            elif table == "youtube":
                await self.local_cache_api.youtube.update(data)
            elif table == "spotify":
                await self.local_cache_api.spotify.update(data)

    async def run_tasks(self, ctx: Optional[commands.Context] = None, message_id=None) -> None:
        """Run tasks for a specific context"""
        lock_id = message_id if message_id is not None else ctx.message.id
        lock_author = ctx.author if ctx else None
        async with self._lock:
            if lock_id in self._tasks:
                if IS_DEBUG:
                    log.debug(f"Running database writes for {lock_id} ({lock_author})")
                try:
                    tasks = self._tasks[ctx.message.id]
                    del self._tasks[ctx.message.id]
                    await asyncio.gather(
                        *[self.route_tasks(*tasks[a]) for a in tasks], return_exceptions=True
                    )
                except Exception as exc:
                    debug_exc_log(
                        log, exc, f"Failed database writes for {lock_id} ({lock_author})"
                    )
                else:
                    if IS_DEBUG:
                        log.debug(f"Completed database writes for {lock_id} ({lock_author})")

    async def run_all_pending_tasks(self) -> None:
        """Run all pending tasks left in the cache, called on cog_unload"""
        async with self._lock:
            if IS_DEBUG:
                log.debug("Running pending writes to database")
            try:
                tasks = {"update": [], "insert": [], "global": []}
                for (k, task) in self._tasks.items():
                    for t, args in task.items():
                        tasks[t].append(args)
                self._tasks = {}
                await asyncio.gather(
                    *[self.route_tasks(*tasks[a]) for a in tasks], return_exceptions=True
                )

            except Exception as exc:
                debug_exc_log(log, exc, "Failed database writes")
            else:
                if IS_DEBUG:
                    log.debug("Completed pending writes to database have finished")

    def append_task(self, ctx: commands.Context, event: str, task: tuple, _id=None) -> None:
        """Add a task to the cache to be run later"""
        lock_id = _id or ctx.message.id
        if lock_id not in self._tasks:
            self._tasks[lock_id] = {"update": [], "insert": [], "global": []}
        self._tasks[lock_id][event].append(task)

    async def _spotify_first_time_query(
        self,
        ctx: commands.Context,
        query_type: str,
        uri: str,
        notifier: Notifier,
        skip_youtube: bool = False,
        current_cache_level: CacheLevel = CacheLevel.none(),
    ) -> List[str]:
        """Return youtube URLS for the spotify URL provided"""
        youtube_urls = []
        tracks = await self._spotify_fetch_tracks(query_type, uri, params=None, notifier=notifier)
        total_tracks = len(tracks)
        database_entries = []
        track_count = 0
        time_now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        youtube_cache = CacheLevel.set_youtube().is_subset(current_cache_level)
        for track in tracks:
            if track.get("error", {}).get("message") == "invalid id":
                continue
            (
                song_url,
                track_info,
                uri,
                artist_name,
                track_name,
                _id,
                _type,
            ) = self.spotify_api.get_spotify_track_info(track)

            database_entries.append(
                {
                    "id": _id,
                    "type": _type,
                    "uri": uri,
                    "track_name": track_name,
                    "artist_name": artist_name,
                    "song_url": song_url,
                    "track_info": track_info,
                    "last_updated": time_now,
                    "last_fetched": time_now,
                }
            )
            if skip_youtube is False:
                val = None
                if youtube_cache:
                    try:
                        (val, last_update) = await self.local_cache_api.youtube.fetch_one(
                            {"track": track_info}
                        )
                    except Exception as exc:
                        debug_exc_log(log, exc, f"Failed to fetch {track_info} from YouTube table")

                if val is None:
                    val = await self._youtube_first_time_query(
                        ctx, track_info, current_cache_level=current_cache_level
                    )
                if youtube_cache and val:
                    task = ("update", ("youtube", {"track": track_info}))
                    self.append_task(ctx, *task)
                if val:
                    youtube_urls.append(val)
            else:
                youtube_urls.append(track_info)
            await asyncio.sleep(0)
            track_count += 1
            if notifier and ((track_count % 2 == 0) or (track_count == total_tracks)):
                await notifier.notify_user(current=track_count, total=total_tracks, key="youtube")
        if CacheLevel.set_spotify().is_subset(current_cache_level):
            task = ("insert", ("spotify", database_entries))
            self.append_task(ctx, *task)
        return youtube_urls

    async def _spotify_fetch_tracks(
        self,
        query_type: str,
        uri: str,
        recursive: Union[str, bool] = False,
        params: MutableMapping = None,
        notifier: Optional[Notifier] = None,
    ) -> Union[MutableMapping, List[str]]:
        """Gets track info from spotify API"""

        if recursive is False:
            (call, params) = self.spotify_api.spotify_format_call(query_type, uri)
            results = await self.spotify_api.get_call(call, params)
        else:
            results = await self.spotify_api.get_call(recursive, params)
        try:
            if results["error"]["status"] == 401 and not recursive:
                raise SpotifyFetchError(
                    (
                        "The Spotify API key or client secret has not been set properly. "
                        "\nUse `{prefix}audioset spotifyapi` for instructions."
                    )
                )
            elif recursive:
                return {"next": None}
        except KeyError:
            pass
        if recursive:
            return results
        tracks = []
        track_count = 0
        total_tracks = results.get("tracks", results).get("total", 1)
        while True:
            new_tracks = []
            if query_type == "track":
                new_tracks = results
                tracks.append(new_tracks)
            elif query_type == "album":
                tracks_raw = results.get("tracks", results).get("items", [])
                if tracks_raw:
                    new_tracks = tracks_raw
                    tracks.extend(new_tracks)
            else:
                tracks_raw = results.get("tracks", results).get("items", [])
                if tracks_raw:
                    new_tracks = [k["track"] for k in tracks_raw if k.get("track")]
                    tracks.extend(new_tracks)
            track_count += len(new_tracks)
            if notifier:
                await notifier.notify_user(current=track_count, total=total_tracks, key="spotify")
            try:
                if results.get("next") is not None:
                    results = await self._spotify_fetch_tracks(
                        query_type, uri, results["next"], params, notifier=notifier
                    )
                    continue
                else:
                    break
            except KeyError:
                raise SpotifyFetchError(
                    "This doesn't seem to be a valid Spotify playlist/album URL or code."
                )
        return tracks

    async def spotify_query(
        self,
        ctx: commands.Context,
        query_type: str,
        uri: str,
        skip_youtube: bool = False,
        notifier: Optional[Notifier] = None,
    ) -> List[str]:
        """Queries the Database then falls back to Spotify and YouTube APIs.

        Parameters
        ----------
        ctx: commands.Context
            The context this method is being called under.
        query_type : str
            Type of query to perform (Pl
        uri: str
            Spotify URL ID.
        skip_youtube:bool
            Whether or not to skip YouTube API Calls.
        notifier: Notifier
            A Notifier object to handle the user UI notifications while tracks are loaded.
        Returns
        -------
        List[str]
            List of Youtube URLs.
        """
        current_cache_level = CacheLevel(await self.config.cache_level())
        cache_enabled = CacheLevel.set_spotify().is_subset(current_cache_level)
        if query_type == "track" and cache_enabled:
            try:
                (val, last_update) = await self.local_cache_api.spotify.fetch_one(
                    {"uri": f"spotify:track:{uri}"}
                )
            except Exception as exc:
                debug_exc_log(
                    log, exc, f"Failed to fetch 'spotify:track:{uri}' from Spotify table"
                )
                val = None
        else:
            val = None
        youtube_urls = []
        if val is None:
            urls = await self._spotify_first_time_query(
                ctx,
                query_type,
                uri,
                notifier,
                skip_youtube,
                current_cache_level=current_cache_level,
            )
            youtube_urls.extend(urls)
        else:
            if query_type == "track" and cache_enabled:
                task = ("update", ("spotify", {"uri": f"spotify:track:{uri}"}))
                self.append_task(ctx, *task)
            youtube_urls.append(val)
        return youtube_urls

    async def spotify_enqueue(
        self,
        ctx: commands.Context,
        query_type: str,
        uri: str,
        enqueue: bool,
        player: lavalink.Player,
        lock: Callable,
        notifier: Optional[Notifier] = None,
        query_global: bool = True,
    ) -> List[lavalink.Track]:
        """Queries the Database then falls back to Spotify and YouTube APIs then Enqueued matched tracks.

        Parameters
        ----------
        ctx: commands.Context
            The context this method is being called under.
        query_type : str
            Type of query to perform (Pl
        uri: str
            Spotify URL ID.
        enqueue:bool
            Whether or not to enqueue the tracks
        player: lavalink.Player
            The current Player.
        notifier: Notifier
            A Notifier object to handle the user UI notifications while tracks are loaded.
        lock: Callable
            A callable handling the Track enqueue lock while spotify tracks are being added.
        query_global: bool
            A place holder
        Returns
        -------
        List[str]
            List of Youtube URLs.
        """
        # globaldb_toggle = await self.config.global_db_enabled()
        track_list = []
        has_not_allowed = False
        try:
            current_cache_level = CacheLevel(await self.config.cache_level())
            guild_data = await self.config.guild(ctx.guild).all()
            enqueued_tracks = 0
            consecutive_fails = 0
            queue_dur = await self.queue_duration(ctx)
            queue_total_duration = lavalink.utils.format_time(queue_dur)
            before_queue_length = len(player.queue)
            tracks_from_spotify = await self._spotify_fetch_tracks(
                query_type, uri, params=None, notifier=notifier
            )
            total_tracks = len(tracks_from_spotify)
            if total_tracks < 1:
                lock(ctx, False)
                embed3 = discord.Embed(
                    colour=await ctx.embed_colour(),
                    title=_("This doesn't seem to be a supported Spotify URL or code."),
                )
                await notifier.update_embed(embed3)

                return track_list
            database_entries = []
            time_now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

            youtube_cache = CacheLevel.set_youtube().is_subset(current_cache_level)
            spotify_cache = CacheLevel.set_spotify().is_subset(current_cache_level)
            for track_count, track in enumerate(tracks_from_spotify):
                (
                    song_url,
                    track_info,
                    uri,
                    artist_name,
                    track_name,
                    _id,
                    _type,
                ) = self.spotify_api.get_spotify_track_info(track)

                database_entries.append(
                    {
                        "id": _id,
                        "type": _type,
                        "uri": uri,
                        "track_name": track_name,
                        "artist_name": artist_name,
                        "song_url": song_url,
                        "track_info": track_info,
                        "last_updated": time_now,
                        "last_fetched": time_now,
                    }
                )
                val = None
                llresponse = None
                if youtube_cache:
                    try:
                        (val, last_updated) = await self.local_cache_api.youtube.fetch_one(
                            {"track": track_info}
                        )
                    except Exception as exc:
                        debug_exc_log(log, exc, f"Failed to fetch {track_info} from YouTube table")

                if val is None:
                    val = await self._youtube_first_time_query(
                        ctx, track_info, current_cache_level=current_cache_level
                    )
                if youtube_cache and val and llresponse is None:
                    task = ("update", ("youtube", {"track": track_info}))
                    self.append_task(ctx, *task)

                if llresponse:
                    track_object = llresponse.tracks
                elif val:
                    try:
                        (result, called_api) = await self.fetch_track(
                            ctx,
                            player,
                            Query.process_input(
                                val, Path(await self.config.localpath()).absolute()
                            ),
                        )
                    except (RuntimeError, aiohttp.ServerDisconnectedError):
                        lock(ctx, False)
                        error_embed = discord.Embed(
                            colour=await ctx.embed_colour(),
                            title=_("The connection was reset while loading the playlist."),
                        )
                        await notifier.update_embed(error_embed)
                        break
                    except asyncio.TimeoutError:
                        lock(ctx, False)
                        error_embed = discord.Embed(
                            colour=await ctx.embed_colour(),
                            title=_("Player timeout, skipping remaining tracks."),
                        )
                        await notifier.update_embed(error_embed)
                        break
                    track_object = result.tracks
                else:
                    track_object = []
                if (track_count % 2 == 0) or (track_count == total_tracks):
                    key = "lavalink"
                    seconds = "???"
                    second_key = None
                    await notifier.notify_user(
                        current=track_count,
                        total=total_tracks,
                        key=key,
                        seconds_key=second_key,
                        seconds=seconds,
                    )

                if consecutive_fails >= 10:
                    error_embed = discord.Embed(
                        colour=await ctx.embed_colour(),
                        title=_("Failing to get tracks, skipping remaining."),
                    )
                    await notifier.update_embed(error_embed)
                    break
                if not track_object:
                    consecutive_fails += 1
                    continue
                consecutive_fails = 0
                single_track = track_object[0]
                if not await self.is_allowed(
                    self.config,
                    ctx.guild,
                    (
                        f"{single_track.title} {single_track.author} {single_track.uri} "
                        f"{str(Query.process_input(single_track, Path(await self.config.localpath()).absolute()))}"
                    ),
                ):
                    has_not_allowed = True
                    if IS_DEBUG:
                        log.debug(f"Query is not allowed in {ctx.guild} ({ctx.guild.id})")
                    continue
                track_list.append(single_track)
                if enqueue:
                    if len(player.queue) >= 10000:
                        continue
                    if guild_data["maxlength"] > 0:
                        if self.track_limit(single_track, guild_data["maxlength"]):
                            enqueued_tracks += 1
                            player.add(ctx.author, single_track)
                            self.bot.dispatch(
                                "red_audio_track_enqueue",
                                player.channel.guild,
                                single_track,
                                ctx.author,
                            )
                    else:
                        enqueued_tracks += 1
                        player.add(ctx.author, single_track)
                        self.bot.dispatch(
                            "red_audio_track_enqueue",
                            player.channel.guild,
                            single_track,
                            ctx.author,
                        )

                    if not player.current:
                        await player.play()
            if not track_list and not has_not_allowed:
                raise SpotifyFetchError(
                    message=_(
                        "Nothing found.\nThe YouTube API key may be invalid "
                        "or you may be rate limited on YouTube's search service.\n"
                        "Check the YouTube API key again and follow the instructions "
                        "at `{prefix}audioset youtubeapi`."
                    ).format(prefix=ctx.prefix)
                )
            player.maybe_shuffle()
            if enqueue and tracks_from_spotify:
                if total_tracks > enqueued_tracks:
                    maxlength_msg = " {bad_tracks} tracks cannot be queued.".format(
                        bad_tracks=(total_tracks - enqueued_tracks)
                    )
                else:
                    maxlength_msg = ""

                embed = discord.Embed(
                    colour=await ctx.embed_colour(),
                    title=_("Playlist Enqueued"),
                    description=_("Added {num} tracks to the queue.{maxlength_msg}").format(
                        num=enqueued_tracks, maxlength_msg=maxlength_msg
                    ),
                )
                if not guild_data["shuffle"] and queue_dur > 0:
                    embed.set_footer(
                        text=_(
                            "{time} until start of playlist"
                            " playback: starts at #{position} in queue"
                        ).format(time=queue_total_duration, position=before_queue_length + 1)
                    )

                await notifier.update_embed(embed)
            lock(ctx, False)

            if spotify_cache:
                task = ("insert", ("spotify", database_entries))
                self.append_task(ctx, *task)
        except Exception as exc:
            lock(ctx, False)
            raise exc
        finally:
            lock(ctx, False)
        return track_list

    async def _youtube_first_time_query(
        self,
        ctx: commands.Context,
        track_info: str,
        current_cache_level: CacheLevel = CacheLevel.none(),
    ) -> str:
        """
        Call the Youtube API and returns the youtube URL that the query matched
        """
        track_url = await self.youtube_api.get_call(track_info)
        if CacheLevel.set_youtube().is_subset(current_cache_level) and track_url:
            time_now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            task = (
                "insert",
                (
                    "youtube",
                    [
                        {
                            "track_info": track_info,
                            "track_url": track_url,
                            "last_updated": time_now,
                            "last_fetched": time_now,
                        }
                    ],
                ),
            )
            self.append_task(ctx, *task)
        return track_url

    async def youtube_query(self, ctx: commands.Context, track_info: str) -> str:
        """
        Gets an YouTube URL from for the query
        """
        current_cache_level = CacheLevel(await self.config.cache_level())
        cache_enabled = CacheLevel.set_youtube().is_subset(current_cache_level)
        val = None
        if cache_enabled:
            try:
                (val, update) = await self.local_cache_api.youtube.fetch_one({"track": track_info})
            except Exception as exc:
                debug_exc_log(log, exc, f"Failed to fetch {track_info} from YouTube table")
        if val is None:
            youtube_url = await self._youtube_first_time_query(
                ctx, track_info, current_cache_level=current_cache_level
            )
        else:
            if cache_enabled:
                task = ("update", ("youtube", {"track": track_info}))
                self.append_task(ctx, *task)
            youtube_url = val
        return youtube_url

    async def fetch_track(
        self,
        ctx: commands.Context,
        player: lavalink.Player,
        query: Query,
        forced: bool = False,
        lazy: bool = False,
        should_query_global: bool = True,
    ) -> Tuple[LoadResult, bool]:
        """A replacement for :code:`lavalink.Player.load_tracks`. This will try to get a valid
        cached entry first if not found or if in valid it will then call the lavalink API.

        Parameters
        ----------
        ctx: commands.Context
            The context this method is being called under.
        player : lavalink.Player
            The player who's requesting the query.
        query: audio_dataclasses.Query
            The Query object for the query in question.
        forced:bool
            Whether or not to skip cache and call API first.
        lazy:bool
            If set to True, it will not call the api if a track is not found.
        should_query_global:bool
            If the method should query the global database.

        Returns
        -------
        Tuple[lavalink.LoadResult, bool]
            Tuple with the Load result and whether or not the API was called.
        """
        current_cache_level = CacheLevel(await self.config.cache_level())
        cache_enabled = CacheLevel.set_lavalink().is_subset(current_cache_level)
        val = None
        _raw_query = Query.process_input(query, Path(await self.config.localpath()).absolute())
        query = str(_raw_query)
        valid_global_entry = False
        results = None
        called_api = False

        if cache_enabled and not forced and not _raw_query.is_local:
            update = True
            try:
                (val, update) = await self.local_cache_api.lavalink.fetch_one({"query": query})
            except Exception as exc:
                debug_exc_log(log, exc, f"Failed to fetch '{query}' from Lavalink table")

            if val and isinstance(val, dict):
                if IS_DEBUG:
                    log.debug(f"Updating Local Database with {query}")
                task = ("update", ("lavalink", {"query": query}))
                self.append_task(ctx, *task)
            else:
                val = None

            if val and not forced and isinstance(val, dict):
                valid_global_entry = False
                called_api = False
            else:
                val = None

        if valid_global_entry:
            pass
        elif lazy is True:
            called_api = False
        elif val and not forced:
            data = val
            data["query"] = query
            if data.get("loadType") == "V2_COMPACT":
                data["loadType"] = "V2_COMPAT"
            results = LoadResult(data)
            called_api = False
            if results.has_error:
                # If cached value has an invalid entry make a new call so that it gets updated
                results, called_api = await self.fetch_track(ctx, player, _raw_query, forced=True)
            valid_global_entry = False
        else:
            if IS_DEBUG:
                log.debug(f"Querying Lavalink api for {query}")
            called_api = True
            results = None
            try:
                results = await player.load_tracks(query)
            except KeyError:
                results = None
            except RuntimeError:
                raise TrackEnqueueError
        if results is None:
            results = LoadResult({"loadType": "LOAD_FAILED", "playlistInfo": {}, "tracks": []})
            valid_global_entry = False

        if (
            cache_enabled
            and results.load_type
            and not results.has_error
            and not _raw_query.is_local
            and results.tracks
        ):
            try:
                time_now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
                data = json.dumps(results._raw)
                if all(k in data for k in ["loadType", "playlistInfo", "isSeekable", "isStream"]):
                    task = (
                        "insert",
                        (
                            "lavalink",
                            [
                                {
                                    "query": query,
                                    "data": data,
                                    "last_updated": time_now,
                                    "last_fetched": time_now,
                                }
                            ],
                        ),
                    )
                    self.append_task(ctx, *task)
            except Exception as exc:
                debug_exc_log(
                    log, exc, f"Failed to enqueue write task for '{query}' to Lavalink table"
                )
        return results, called_api

    async def autoplay(self, player: lavalink.Player, playlist_api: PlaylistWrapper):
        """
        Enqueue a random track
        """
        autoplaylist = await self.config.guild(player.channel.guild).autoplaylist()
        current_cache_level = CacheLevel(await self.config.cache_level())
        cache_enabled = CacheLevel.set_lavalink().is_subset(current_cache_level)
        playlist = None
        tracks = None
        if autoplaylist["enabled"]:
            try:
                playlist = await get_playlist(
                    autoplaylist["id"],
                    autoplaylist["scope"],
                    self.bot,
                    playlist_api,
                    player.channel.guild,
                    player.channel.guild.me,
                )
                tracks = playlist.tracks_obj
            except Exception as exc:
                debug_exc_log(log, exc, "Failed to fetch playlist for autoplay")

        if not tracks or not getattr(playlist, "tracks", None):
            if cache_enabled:
                tracks = await self.get_random_track_from_db()
            if not tracks:
                ctx = namedtuple("Context", "message")
                (results, called_api) = await self.fetch_track(
                    ctx(player.channel.guild),
                    player,
                    Query.process_input(
                        _TOP_100_US, Path(await self.config.localpath()).absolute()
                    ),
                )
                tracks = list(results.tracks)
        if tracks:
            multiple = len(tracks) > 1
            track = tracks[0]

            valid = not multiple
            tries = len(tracks)
            while valid is False and multiple:
                tries -= 1
                if tries <= 0:
                    raise DatabaseError("No valid entry found")
                track = random.choice(tracks)
                query = Query.process_input(track, Path(await self.config.localpath()).absolute())
                await asyncio.sleep(0.001)
                if not query.valid:
                    continue
                if query.is_local and not query.local_track_path.exists():
                    continue
                if not await self.is_allowed(
                    self.config,
                    player.channel.guild,
                    (
                        f"{track.title} {track.author} {track.uri} "
                        f"{str(Query.process_input(track, Path(await self.config.localpath()).absolute()))}"
                    ),
                ):
                    if IS_DEBUG:
                        log.debug(
                            "Query is not allowed in "
                            f"{player.channel.guild} ({player.channel.guild.id})"
                        )
                    continue
                valid = True

            track.extras["autoplay"] = True
            player.add(player.channel.guild.me, track)
            self.bot.dispatch(
                "red_audio_track_auto_play", player.channel.guild, track, player.channel.guild.me
            )
            if not player.current:
                await player.play()
