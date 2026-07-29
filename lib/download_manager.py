"""Download queue manager for serialized video downloads."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import time
import uuid
from queue import Empty, Queue
from threading import Thread

from pikaraoke.lib.events import EventSystem
from pikaraoke.lib.preference_manager import PreferenceManager
from pikaraoke.lib.queue_manager import QueueManager
from pikaraoke.lib.song_manager import SongManager
from pikaraoke.lib.ffmpeg import validate_media_file
from pikaraoke.lib.metadata_parser import normalize_for_comparison, regex_tidy
from pikaraoke.lib.youtube_dl import (
    build_ytdl_download_command,
    get_video_metadata,
    get_youtube_id_from_url,
    is_likely_karaoke_result,
)


class DownloadManager:
    """Manages a queue of video downloads, processing them serially.

    This prevents rate limiting from download sources and reduces CPU load
    by ensuring only one download runs at a time.

    Attributes:
        download_queue: Queue holding pending download requests.
    """

    def __init__(
        self,
        events: EventSystem,
        preferences: PreferenceManager,
        song_manager: SongManager,
        queue_manager: QueueManager,
        download_path: str,
        youtubedl_proxy: str | None = None,
        additional_ytdl_args: str | None = None,
    ) -> None:
        """Initialize the download manager.

        Args:
            events: Event system for notifications and broadcasts.
            preferences: Configuration manager for persistent settings.
            song_manager: Manager for song library operations.
            queue_manager: Manager for playback queue.
            download_path: Directory where downloads are saved.
            youtubedl_proxy: Optional proxy URL for yt-dlp.
            additional_ytdl_args: Optional additional arguments for yt-dlp.
        """
        self._events = events
        self._preferences = preferences
        self._song_manager = song_manager
        self._queue_manager = queue_manager
        self._download_path = download_path
        self._youtubedl_proxy = youtubedl_proxy
        self._additional_ytdl_args = additional_ytdl_args
        self.download_queue: Queue = Queue()
        self.pending_downloads: list[dict] = []  # Shadow queue for visibility
        self.download_errors: list[dict] = []  # Track failed downloads
        self.active_download: dict | None = None
        self._worker_thread: Thread | None = None
        self._is_downloading: bool = False  # Track if a download is currently in progress
        self._download_stall_timeout: int = 180  # Abort downloads that stop producing output

    def start(self) -> None:
        """Start the download worker thread."""
        self._worker_thread = Thread(target=self._process_queue, daemon=True)
        self._worker_thread.start()
        logging.debug("Download queue worker started")

    def get_downloads_status(self) -> dict:
        """Get the status of active and pending downloads.

        Returns:
            Dict containing 'active' download info and list of 'pending' downloads.
        """
        return {
            "active": self.active_download,
            "pending": self.pending_downloads,
            "errors": self.download_errors,
        }

    def remove_error(self, error_id: str) -> bool:
        """Remove an error from the list by ID.

        Args:
            error_id: The ID of the error to remove.

        Returns:
            True if removed, False if not found.
        """
        initial_len = len(self.download_errors)
        self.download_errors = [e for e in self.download_errors if e["id"] != error_id]
        return len(self.download_errors) < initial_len

    def _normalized_song_key(self, name: str) -> str:
        tidied = regex_tidy(name) or name
        return normalize_for_comparison(tidied)

    def _find_existing_library_match(self, video_url: str, title: str | None) -> str | None:
        video_id = get_youtube_id_from_url(video_url)
        if video_id:
            existing_by_id = self._song_manager.songs.find_by_id(self._download_path, video_id)
            if existing_by_id:
                return existing_by_id

        if not title:
            return None

        wanted_key = self._normalized_song_key(title)
        if not wanted_key:
            return None

        for song_path in self._song_manager.songs:
            existing_name = self._song_manager.display_name_from_path(song_path)
            if self._normalized_song_key(existing_name) == wanted_key:
                return song_path

        return None

    def _classify_download_failure(self, raw_error: str, stalled: bool) -> str:
        error_text = (raw_error or "").casefold()
        if stalled:
            return "Download timed out and was cancelled"
        if any(
            token in error_text
            for token in (
                "video unavailable",
                "private video",
                "this video is private",
                "sign in to confirm your age",
                "not available in your country",
                "members-only",
                "copyright",
                "unavailable",
            )
        ):
            return "Video unavailable, private, blocked, or restricted"
        if any(token in error_text for token in ("http error 403", "http error 429", "too many requests")):
            return "Video source denied access or rate-limited the download"
        if any(token in error_text for token in ("timed out", "timeout", "network is unreachable", "temporary failure in name resolution")):
            return "Network error while downloading"
        if any(token in error_text for token in ("permission denied", "read-only file system", "no space left on device")):
            return "Storage error while saving the file"
        if any(token in error_text for token in ("unsupported url", "unsupported url scheme")):
            return "Unsupported or invalid video link"
        return "Download failed"

    def queue_download(
        self,
        video_url: str,
        enqueue: bool = False,
        user: str = "Pikaraoke",
        title: str | None = None,
    ) -> None:
        """Queue a video for download.

        Downloads are processed serially to prevent rate limiting and CPU overload.

        Args:
            video_url: YouTube video URL.
            enqueue: Whether to add to playback queue after download.
            user: Username to attribute the download to.
            title: Display title (defaults to URL if not provided).
        """
        from flask_babel import _

        # Strip playlist parameter to avoid downloading entire playlists
        if "&list=" in video_url:
            video_url = video_url.split("&list=")[0]

        fetched_metadata = None
        if not title:
            fetched_metadata = get_video_metadata(video_url)
            title = fetched_metadata.get("title") if fetched_metadata else title

        displayed_title = title if title else video_url

        existing_match = self._find_existing_library_match(video_url, title)
        if existing_match:
            existing_name = self._song_manager.display_name_from_path(existing_match)
            logging.info("Rejected duplicate download already in library: %s", displayed_title)
            self.download_errors.append(
                {
                    "id": str(uuid.uuid4()),
                    "title": displayed_title,
                    "url": video_url,
                    "user": user,
                    "error": f"Already exists in library: {existing_name}",
                }
            )
            self._events.emit(
                "notification",
                _("Song already exists in library: %s") % existing_name,
                "warning",
            )
            return

        karaoke_channel = fetched_metadata.get("channel", "") if fetched_metadata else ""
        if title and not is_likely_karaoke_result(title, karaoke_channel):
            logging.warning("Rejected non-karaoke candidate before download: %s", displayed_title)
            self.download_errors.append(
                {
                    "id": str(uuid.uuid4()),
                    "title": displayed_title,
                    "url": video_url,
                    "user": user,
                    "error": "Rejected because title does not look like karaoke/playback",
                }
            )
            self._events.emit(
                "notification",
                _("Rejected non-karaoke result: %s") % displayed_title,
                "warning",
            )
            return

        # Check how many items are ahead (in queue + currently downloading)
        pending_count = self.download_queue.qsize() + (1 if self._is_downloading else 0)

        if pending_count > 0:
            # MSG: Message shown when download is added to queue (not first in line)
            self._events.emit(
                "notification",
                _("Download queued (#%d): %s") % (pending_count + 1, displayed_title),
            )
        else:
            # MSG: Message shown when download is added and will start immediately
            self._events.emit("notification", _("Download starting: %s") % displayed_title)

        # If queue was just started (was not downloading before), emit event
        if not self._is_downloading and self.download_queue.empty():
            self._events.emit("download_started")

        download_data = {
            "video_url": video_url,
            "enqueue": enqueue,
            "user": user,
            "title": title,
            "display_title": displayed_title,
        }

        # Add to the download queue and shadow list
        self.download_queue.put(download_data)
        self.pending_downloads.append(download_data)

    def _process_queue(self) -> None:
        """Worker thread that processes downloads from the queue serially.

        Runs indefinitely, blocking on queue.get() until items are available.
        Each download is processed completely before the next one starts.
        """
        while True:
            download_request = self.download_queue.get()

            # Remove from shadow queue
            # Note: Since this is a single worker thread and append happens on main thread,
            # we simply pop the first item as it corresponds to FIFO queue.
            # In a multi-worker scenario, this would need a lock.
            if self.pending_downloads:
                self.pending_downloads.pop(0)

            self._is_downloading = True

            # Initialize active download state
            self.active_download = {
                "title": download_request.get("display_title", download_request["video_url"]),
                "url": download_request["video_url"],
                "user": download_request["user"],
                "progress": 0.0,
                "status": "starting",
                "eta": "--:--",
                "speed": "---",
            }

            try:
                self._execute_download(
                    download_request["video_url"],
                    download_request["enqueue"],
                    download_request["user"],
                    download_request["title"],
                )
            except Exception as e:
                logging.error(f"Error processing download: {e}")
            finally:
                self._is_downloading = False
                self.active_download = None
                self.download_queue.task_done()

                # Check if we are done with all downloads
                if self.download_queue.empty():
                    self._events.emit("download_stopped")

    def _execute_download(
        self,
        video_url: str,
        enqueue: bool,
        user: str,
        title: str | None,
    ) -> int:
        """Execute a video download.

        Args:
            video_url: YouTube video URL.
            enqueue: Whether to add to queue after download.
            user: Username to attribute the download to.
            title: Display title (defaults to URL if not provided).

        Returns:
            Return code from the download process (0 = success).
        """
        from flask_babel import _

        displayed_title = title if title else video_url

        # MSG: Message shown when download actually starts (after waiting in queue)
        self._events.emit("notification", _("Downloading video: %s") % displayed_title)

        cmd = build_ytdl_download_command(
            video_url,
            self._download_path,
            self._preferences.get_or_default("high_quality"),
            self._youtubedl_proxy,
            self._additional_ytdl_args,
        )
        logging.debug("yt-dlp command: " + " ".join(cmd))

        # Use Popen to capture output in real-time
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
            universal_newlines=True,
        )

        output_buffer = []
        output_queue: Queue[str | None] = Queue()

        # Regex to parse progress from yt-dlp stdout
        # Example: [download]   0.0% of    4.62MiB at  396.66KiB/s ETA 00:12
        progress_regex = re.compile(
            r"\[download\]\s+(\d+\.?\d*)%\s+of\s+.*?\s+at\s+([^\s]+)\s+ETA\s+([^\s]+)"
        )
        video_id = get_youtube_id_from_url(video_url)
        last_output_at = time.monotonic()
        terminated_for_stall = False

        def _read_stdout() -> None:
            if process.stdout is None:
                output_queue.put(None)
                return
            try:
                for line in iter(process.stdout.readline, ""):
                    if not line:
                        break
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        Thread(target=_read_stdout, daemon=True).start()

        while True:
            try:
                line = output_queue.get(timeout=1)
            except Empty:
                if process.poll() is not None:
                    break
                if time.monotonic() - last_output_at > self._download_stall_timeout:
                    terminated_for_stall = True
                    logging.error(
                        "yt-dlp stalled for over %ss while downloading %s; terminating process",
                        self._download_stall_timeout,
                        displayed_title,
                    )
                    process.kill()
                    break
                continue

            if line is None:
                if process.poll() is not None:
                    break
                continue

            last_output_at = time.monotonic()
            output_buffer.append(line)
            match = progress_regex.search(line)
            if match and self.active_download:
                percent = float(match.group(1))
                speed = match.group(2)
                eta = match.group(3)

                self.active_download["progress"] = percent
                self.active_download["status"] = "downloading"
                self.active_download["speed"] = speed
                self.active_download["eta"] = eta
            elif self.active_download and (
                "[Merger]" in line or "Destination:" in line or "Post-process" in line
            ):
                # yt-dlp sometimes pauses progress updates while finalizing the file.
                self.active_download["status"] = "processing"
                self.active_download["eta"] = "--:--"
            # Log only non-progress lines to avoid spamming logs, or log everything at debug
            # logging.debug(line.strip())

        try:
            rc = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            rc = process.wait(timeout=5)
        output = "".join(output_buffer)
        if terminated_for_stall:
            output += (
                f"\nyt-dlp stalled for more than {self._download_stall_timeout} seconds "
                "without producing output and was terminated.\n"
            )

        if rc != 0:
            user_message = self._classify_download_failure(output, terminated_for_stall)
            # Logic removed: We no longer retry synchronously as it blocks the queue.
            # Failed downloads are now failed fast and logged.

            self._events.emit(
                "notification", f"{user_message}: {displayed_title}", "danger"
            )
            logging.error(f"yt-dlp stderr: {output}")
            self.download_errors.append(
                {
                    "id": str(uuid.uuid4()),
                    "title": displayed_title,
                    "url": video_url,
                    "user": user,
                    "error": f"{user_message}: {output or 'Unknown error'}",
                }
            )
        else:
            if self.active_download:
                self.active_download["progress"] = 100
                self.active_download["status"] = "complete"

            if enqueue:
                # MSG: Message shown after the download is completed and queued
                self._events.emit(
                    "notification", _("Downloaded and queued: %s") % displayed_title, "success"
                )
            else:
                # MSG: Message shown after the download is completed but not queued
                self._events.emit("notification", _("Downloaded: %s") % displayed_title, "success")

            # After download, find the file path by ID
            song_path = None
            if video_id:
                logging.debug(f"Searching for downloaded file by ID: {video_id}")
                song_path = self._song_manager.songs.find_by_id(self._download_path, video_id)
            else:
                logging.warning("No video ID available to find downloaded song")

            if song_path:
                is_valid, validation_error = validate_media_file(song_path)
                if not is_valid:
                    logging.error(
                        "Downloaded file failed integrity check and will be removed: %s (%s)",
                        song_path,
                        validation_error,
                    )
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(song_path)
                    self.download_errors.append(
                        {
                            "id": str(uuid.uuid4()),
                            "title": displayed_title,
                            "url": video_url,
                            "user": user,
                            "error": f"Corrupted or invalid file removed: {validation_error}",
                        }
                    )
                    self._events.emit(
                        "notification",
                        _("Corrupted download removed: %s") % displayed_title,
                        "danger",
                    )
                    song_path = None
                else:
                    self._events.emit("song_downloaded", song_path)
            else:
                logging.warning(
                    f"Could not find downloaded song in {self._download_path} matching ID: {video_id}"
                )
                self.download_errors.append(
                    {
                        "id": str(uuid.uuid4()),
                        "title": displayed_title,
                        "url": video_url,
                        "user": user,
                        "error": "Download finished but the saved file could not be found",
                    }
                )
                self._events.emit(
                    "notification",
                    _("Downloaded file could not be located: %s") % displayed_title,
                    "danger",
                )

            if enqueue:
                if song_path:
                    self._queue_manager.enqueue(song_path, user, log_action=False)
                else:
                    # MSG: Message shown after the download is completed but the adding to queue fails
                    self._events.emit(
                        "notification", _("Error queueing song: ") + displayed_title, "danger"
                    )

        return rc
