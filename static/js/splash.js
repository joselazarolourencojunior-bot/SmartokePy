let socket = io();
let mouseTimer = null;
let cursorVisible = false;
let nowPlaying = {};
let octopusInstance = null;
let showMenu = false;
let menuButtonVisible = false;
let autoplayConfirmed = false;
let volume = 0.85;
const playbackStartTimeout = 90000;
const bgMediaResumeDelay = 2000;
const prematureEndThresholdSeconds = 120;
const maxPlaybackRecoveryAttempts = 4;
let isScoreShown = false;
const hasBgVideo = PikaraokeConfig.hasBgVideo;
let currentVideoUrl = null;
let hlsInstance = null;
let idleTime = 0;
let screensaverTimeoutSeconds = PikaraokeConfig.screensaverTimeout;
let bg_playlist = [];
let bgMediaResumeTimeout = null;
let scoreReviews = {
  low: ["Better luck next time!"],
  mid: ["Not bad!"],
  high: ["Great job!"],
};
let isMaster = false;
let uiScale = null;
let clockIntervalId = null;
let playbackRecoveryAttempts = 0;
let playbackResumeHint = null;
let playbackStartGuardTimer = null;
let startSongSent = false;

// Browser detection
const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
const isMobileSafari = isSafari && (/iPhone|iPad|iPod/i.test(navigator.userAgent) || navigator.maxTouchPoints > 1);
const isChrome = /chrome/i.test(navigator.userAgent) && !/edg/i.test(navigator.userAgent);
const isFirefox = /firefox/i.test(navigator.userAgent);
const isEdge = /edg/i.test(navigator.userAgent);
const isSupportedBrowser = isSafari || isChrome || isFirefox || isEdge;

const isMediaPlaying = (media) =>
  !!(
    media.currentTime > 0 &&
    !media.paused &&
    !media.ended &&
    media.readyState > 2
  );

const formatTime = (seconds) => {
  if (isNaN(seconds)) {
    return "00:00";
  }
  const totalSeconds = Math.floor(seconds);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const secs = totalSeconds % 60;
  const formattedMinutes = String(minutes).padStart(2, "0");
  const formattedSeconds = String(secs).padStart(2, "0");
  return `${formattedMinutes}:${formattedSeconds}`;
}

const testAutoplayCapability = async () => {
  try {
    const testVideo = document.createElement('video');
    testVideo.playsInline = true;
    testVideo.muted = true;
    let testSrc = "/static/video/test_autoplay.mp4";
    let probeOk = true;
    try {
      await new Promise((resolve, reject) => {
        const probe = new XMLHttpRequest();
        probe.open("HEAD", testSrc, true);
        probe.onreadystatechange = () => {
          if (probe.readyState === 4) {
            if (probe.status >= 200 && probe.status < 400) resolve();
            else reject();
          }
        };
        probe.onerror = reject;
        probe.timeout = 2000;
        probe.ontimeout = reject;
        probe.send();
      });
    } catch (_) {
      probeOk = false;
    }
    if (probeOk) {
      testVideo.src = testSrc;
      await new Promise((resolve, reject) => {
        testVideo.onloadeddata = resolve;
        testVideo.onerror = reject;
        setTimeout(() => reject(new Error("load-timeout")), 3500);
      });
      await testVideo.play();
      testVideo.muted = false;
      testVideo.volume = 0.01;
      await new Promise(resolve => setTimeout(resolve, 500));
      if (testVideo.muted || testVideo.paused) {
        testVideo.pause();
        $('#permissions-modal').addClass('is-active');
      } else {
        testVideo.pause();
        handleConfirmation();
      }
    } else {
      testVideo.muted = true;
      testVideo.playsInline = true;
      testVideo.srcObject = null;
      testVideo.setAttribute("autoplay", "true");
      const canSilentPlay = document.createElement("video");
      canSilentPlay.muted = true;
      canSilentPlay.playsInline = true;
      canSilentPlay.setAttribute("autoplay", "true");
      try {
        const playPromise = canSilentPlay.play();
        if (playPromise && typeof playPromise.catch === "function") {
          playPromise.catch(() => {});
        }
        handleConfirmation();
      } catch (_) {
        $('#permissions-modal').addClass('is-active');
      }
    }
  } catch (e) {
    console.log("Autoplay check fell back to modal:", e);
    $('#permissions-modal').addClass('is-active');
  }
};

const handleConfirmation = () => {
  $('#permissions-modal').removeClass('is-active');
  autoplayConfirmed = true;
  updateBackgroundMediaState(true);
  loadNowPlaying();
};

const hideVideo = () => {
  $("#video-container").hide();
}

const getPlaybackTelemetry = (video) => ({
  reason: null,
  position: Number.isFinite(video.currentTime) ? video.currentTime : null,
  duration: Number.isFinite(nowPlaying.now_playing_duration)
    ? nowPlaying.now_playing_duration
    : (Number.isFinite(video.duration) ? video.duration : null),
  readyState: video.readyState,
  networkState: video.networkState,
  error: video.error ? (video.error.message || `media-error-${video.error.code}`) : null,
  url: currentVideoUrl,
});

const isPrematureEnd = (video) => {
  const duration = Number.isFinite(nowPlaying.now_playing_duration)
    ? nowPlaying.now_playing_duration
    : (Number.isFinite(video.duration) ? video.duration : null);
  const position = Number.isFinite(video.currentTime) ? video.currentTime : 0;
  if (duration === null) {
    // V37: duration DESCONHECIDO (HLS sem metadata, Chrome/ffmpeg nao informou).
    // Tratar como "prematuro" se tocamos MENOS DE 120s (2min). Apenas apos 2min
    // seguidos tocando aceitamos que acabou mesmo sem saber duration total.
    return position < 120;
  }
  return position < (duration - prematureEndThresholdSeconds);
};

const tryPlaybackRecovery = (reason, mode = "combined") => {
  const video = getVideoPlayer();
  if (!hlsInstance || playbackRecoveryAttempts >= maxPlaybackRecoveryAttempts) {
    return false;
  }

  playbackRecoveryAttempts += 1;
  console.warn(`Attempting HLS playback recovery (${playbackRecoveryAttempts}/${maxPlaybackRecoveryAttempts}):`, reason);

  try {
    const restartAt = Math.max((video.currentTime || 0) - 1, 0);
    if (mode === "network" || mode === "combined") {
      hlsInstance.startLoad(restartAt);
    }
    if (mode === "media" || mode === "combined") {
      hlsInstance.recoverMediaError();
    }
  } catch (e) {
    console.error("HLS recovery failed to initialize", e);
    return false;
  }

  setTimeout(() => {
    if (!isMediaPlaying(video) && currentVideoUrl) {
      video.play().catch(err => console.error("Recovery play failed:", err));
    }
  }, 250);

  return true;
};

const endSong = async (reason = null, showScore = false) => {
  const video = getVideoPlayer();
  const payload = {
    ...getPlaybackTelemetry(video),
    reason,
  };
  if (showScore && !PikaraokeConfig.disableScore) {
    isScoreShown = true;
    await startScore("/static/");
    isScoreShown = false;
  }
  currentVideoUrl = null;
  if (hlsInstance) {
    hlsInstance.destroy();
    hlsInstance = null;
  }
  video.pause();
  $("#video-source").attr("src", "");
  video.load();
  hideVideo();
  if (isMaster) {
    socket.emit("end_song", payload);
  } else {
    console.log("Slave active (read-only): skipping end_song emission");
  }
}

const getBackgroundMusicPlayer = () => document.getElementById('background-music');
const getBackgroundVideoPlayer = () => document.getElementById('bg-video');
const getVideoPlayer = () => $("#video")[0]

const getNextBgMusicSong = () => {
  let currentSong = getBackgroundMusicPlayer().getAttribute('src');
  let nextSong = bg_playlist[0];
  if (currentSong) {
    let currentIndex = bg_playlist.indexOf(currentSong);
    if (currentIndex >= 0 && currentIndex < bg_playlist.length - 1) {
      nextSong = bg_playlist[currentIndex + 1];
    }
  }
  return nextSong;
}

const playBGMusic = async (play) => {
  const audio = getBackgroundMusicPlayer();
  if (play) {
    if (PikaraokeConfig.disableBgMusic) return;
    if (!autoplayConfirmed) return;
    if (bg_playlist.length === 0) return;

    if (!audio.getAttribute('src')) audio.setAttribute('src', getNextBgMusicSong());

    if (isMediaPlaying(audio)) return;
    audio.volume = 0;
    if (audio.readyState <= 2) await audio.load();
    await audio.play().catch(e => console.log("Autoplay blocked (music)"));
    $(audio).animate({ volume: PikaraokeConfig.bgMusicVolume }, 2000);
  } else {
    if (audio) {
      $(audio).animate({ volume: 0 }, 2000, () => audio.pause());
    }
  }
}

const playBGVideo = async (play) => {
  const bgVideo = getBackgroundVideoPlayer();
  const bgVideoContainer = $('#bg-video-container');

  if (play) {
    if (PikaraokeConfig.disableBgVideo) return;
    if (!autoplayConfirmed) return;

    if (isMediaPlaying(bgVideo)) return;
    $("#bg-video").attr("src", "/stream/bg_video");
    if (bgVideo.readyState <= 2) await bgVideo.load();
    bgVideo.play().catch(() => console.log("Autoplay blocked (video)"));
    bgVideoContainer.fadeIn(2000);
  } else {
    if (bgVideo && isMediaPlaying(bgVideo)) {
      bgVideo.pause();
      bgVideoContainer.fadeOut(2000);
    }
  }
}

const shouldBackgroundMediaPlay = () => {
  return autoplayConfirmed &&
    !nowPlaying.now_playing &&
    !nowPlaying.up_next;
};

const updateBackgroundMediaState = (immediate = false) => {
  // Clear any pending resume
  if (bgMediaResumeTimeout) {
    clearTimeout(bgMediaResumeTimeout);
    bgMediaResumeTimeout = null;
  }

  if (shouldBackgroundMediaPlay()) {
    if (immediate) {
      playBGMusic(true);
      if (hasBgVideo) playBGVideo(true);
    } else {
      bgMediaResumeTimeout = setTimeout(() => {
        bgMediaResumeTimeout = null;
        if (shouldBackgroundMediaPlay()) {
          playBGMusic(true);
          if (hasBgVideo) playBGVideo(true);
        }
      }, bgMediaResumeDelay);
    }
  } else {
    playBGMusic(false);
    playBGVideo(false);
  }
};

const flashNotification = (message, categoryClass) => {
  const sn = $("#splash-notification");
  if (sn.html()) return;
  sn.html(message);
  sn.addClass(categoryClass);
  sn.fadeIn();
  setTimeout(() => {
    sn.fadeOut();
    setTimeout(() => {
      sn.html("");
      sn.removeClass(categoryClass);
    }, 450);
  }, 3000);
}

const setupScreensaver = () => {
  if (screensaverTimeoutSeconds > 0) {
    setInterval(() => {
      let screensaver = document.getElementById('screensaver');
      let video = getVideoPlayer();
      if (isMediaPlaying(video) || cursorVisible) {
        idleTime = 0;
      }
      if (idleTime >= screensaverTimeoutSeconds) {
        if (screensaver.style.visibility === 'hidden') {
          screensaver.style.visibility = 'visible';
          playBGVideo(false);
          startScreensaver(); // depends on upstream screensaver.js import
        }
        if (idleTime > screensaverTimeoutSeconds + 36000) idleTime = screensaverTimeoutSeconds;
      } else {
        if (screensaver.style.visibility === 'visible') {
          screensaver.style.visibility = 'hidden';
          stopScreensaver(); // depends on upstream screensaver.js import
          updateBackgroundMediaState(true);
        }
      }
      idleTime++;
    }, 1000)
  }
}

const handleNowPlayingUpdate = (np) => {
  nowPlaying = np;
  if (np.now_playing) {

    // Handle updating now playing HTML
    let nowPlayingHtml = `<span>${np.now_playing}</span> `;
    if (np.now_playing_transpose !== 0) {
      nowPlayingHtml += `<span class='is-size-6 has-text-success'><b>Key</b>: ${getSemitonesLabel(np.now_playing_transpose)} </span>`;
    }
    $("#now-playing-song").html(nowPlayingHtml);
    $("#now-playing-singer").html(np.now_playing_user);
    $("#now-playing").fadeIn();
  } else {
    $("#now-playing").fadeOut();
  }
  if (np.up_next) {
    $("#up-next-song").html(np.up_next);
    $("#up-next-singer").html(np.next_user);
    $("#up-next").fadeIn();
  } else {
    $("#up-next").fadeOut();
  }

  // Update bg music and video state
  if (np.now_playing || np.up_next) {
    idleTime = 0;
  }
  updateBackgroundMediaState();
  const video = getVideoPlayer();

  // Setup ASS subtitle file if found
  const subtitleUrl = np.now_playing_subtitle_url;
  if (octopusInstance) {
    octopusInstance.dispose();
    octopusInstance = null;
  }
  if (subtitleUrl && video) {
    const options = {
      video: video,
      subUrl: subtitleUrl,
      fonts: ["/static/fonts/Arial.ttf", "/static/fonts/DroidSansFallback.ttf"],
      debug: true,
      workerUrl: "/static/js/subtitles-octopus-worker.js"
    };
    try {
      octopusInstance = new SubtitlesOctopus(options);
      if (uiScale) {
        // Find the canvas created by SubtitlesOctopus (sibling of the video)
        const canvas = video.parentNode.querySelector('canvas');
        if (canvas) {
          canvas.style.transform = `scale(${uiScale})`;
          canvas.style.transformOrigin = 'bottom center';
        }
      }
    } catch (e) { console.error(e); }
  }

  if (np.now_playing_url && np.now_playing_url !== currentVideoUrl) {
    currentVideoUrl = np.now_playing_url;
    playbackRecoveryAttempts = 0;
    startSongSent = false;
    if (playbackStartGuardTimer) {
      clearTimeout(playbackStartGuardTimer);
      playbackStartGuardTimer = null;
    }
    const streamUrl = np.now_playing_url;
    $("#video-source").attr("src", "");
    video.load();
    $("#video-source").attr("src", streamUrl);

    video.muted = video.muted || false;
    video.setAttribute("muted", video.muted ? "muted" : "");
    video.setAttribute("playsinline", "");
    video.setAttribute("webkit-playsinline", "");

    if (streamUrl.endsWith('.m3u8')) {
      const useNativeHLS = video.canPlayType('application/vnd.apple.mpegurl') && !isChrome && !isEdge && !isMobileSafari;
      if (useNativeHLS) {
        video.src = streamUrl;
      } else {
        if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }
        hlsInstance = new Hls({
          startPosition: 0,
          manifestLoadingMaxRetry: 12,
          manifestLoadingMaxRetryTime: 30000,
          manifestLoadingRetryDelay: 800,
          levelLoadingMaxRetry: 10,
          levelLoadingMaxRetryTime: 25000,
          levelLoadingRetryDelay: 800,
          fragLoadingMaxRetry: 10,
          fragLoadingMaxRetryTime: 25000,
          fragLoadingRetryDelay: 600,
          lowLatencyMode: false,
          backBufferLength: 30,
          enableWorker: true,
          xhrSetup: (xhr, url) => {
            xhr.addEventListener('error', () => {});
            // Custom XHR wrapper: for 404/5xx responses, force them to act like
            // recoverable network errors (Hls.js will retry based on *LoadingMaxRetry settings).
            // Without this, Hls.js treats HTTP 404 for manifest as FATAL (non-retryable),
            // which is exactly the failure we see on transitions (FFmpeg hasn't written
            // the playlist yet when Splash first asks).
            const originalOnReadyStateChange = xhr.onreadystatechange;
            xhr.onreadystatechange = function (...args) {
              if (xhr.readyState === 4) {
                const status = xhr.status;
                if ((status === 404 || (status >= 500 && status < 600)) && !status._trae_forced) {
                  // Fake a network-level abort so Hls.js classifies this as
                  // ErrorDetails.{MANIFEST,LEVEL,FRAG}_LOAD_ERROR (retryable)
                  // instead of {MANIFEST,LEVEL,FRAG}_LOAD_ERROR_404 (fatal).
                  try { Object.defineProperty(xhr, 'status', { value: 0, writable: true, configurable: true }); } catch (_) { xhr.status = 0; }
                  try { Object.defineProperty(xhr, 'readyState', { value: 0, writable: true, configurable: true }); } catch (_) { }
                  return;
                }
              }
              if (typeof originalOnReadyStateChange === 'function') {
                return originalOnReadyStateChange.apply(this, args);
              }
            };
          }
        });
        hlsInstance.on(Hls.Events.ERROR, (_event, data) => {
          console.error("HLS error:", data);
          if (!data?.fatal) return;

          // Extra defensive layer: if still got a fatal for 404-style load error,
          // try to re-start the same source one more time instead of ending song.
          if (
            data.type === Hls.ErrorTypes.NETWORK_ERROR &&
            data.response &&
            (data.response.code === 404 || (data.response.code >= 500 && data.response.code < 600))
          ) {
            if (tryPlaybackRecovery(`hls-network-${data.details}-status-${data.response.code}`, "network")) {
              return;
            }
          }

          if (data.type === Hls.ErrorTypes.NETWORK_ERROR && tryPlaybackRecovery(`hls-network-${data.details}`, "network")) {
            return;
          }
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR && tryPlaybackRecovery(`hls-media-${data.details}`, "media")) {
            return;
          }

          endSong(`hls-fatal-${data.type || "unknown"}-${data.details || "unknown"}`);
        });
        hlsInstance.loadSource(streamUrl);
        hlsInstance.attachMedia(video);
      }
    }

    video.load();
    const duration = $("#duration");
    if (np.now_playing_duration) {
      duration.text(`/${formatTime(np.now_playing_duration)}`);
      duration.show();
    } else {
      duration.hide();
    }

    $("#video-container").show();

    const attemptPlay = () => {
      const playPromise = video.play();
      if (playPromise && typeof playPromise.catch === "function") {
        playPromise.catch((err) => {
          console.error('Play failed (1st attempt):', err);
          if (!video.muted) {
            video.muted = true;
            video.setAttribute("muted", "muted");
            const retry2 = video.play();
            if (retry2 && typeof retry2.catch === "function") {
              retry2.catch(err2 => {
                console.error('Play failed (muted fallback):', err2);
                setTimeout(() => {
                  video.play().catch(err3 => console.error("Final play retry failed:", err3));
                }, 1000);
              });
            }
          } else {
            setTimeout(() => video.play().catch(err2 => console.error("2s play retry failed:", err2)), 1000);
          }
        });
      }
    };
    attemptPlay();

    if (np.now_playing_position && isMediaPlaying(video)) {
      if (Math.abs(video.currentTime - np.now_playing_position) > 2) {
        console.log("Syncing to server position:", np.now_playing_position);
        video.currentTime = np.now_playing_position;
      }
    }

    playbackStartGuardTimer = setTimeout(() => {
      if (!isMediaPlaying(video) && (video.paused || video.readyState <= 1)) {
        console.warn("Playback guard timeout fired (90s) and nothing played — triggering endSong(\"failed to start\")");
        endSong("failed to start");
      } else {
        if (playbackStartGuardTimer) { clearTimeout(playbackStartGuardTimer); playbackStartGuardTimer = null; }
      }
    }, playbackStartTimeout);
  }
}

async function loadNowPlaying() {
  const data = await $.get("/now_playing");
  const parsed = JSON.parse(data);
  if (playbackResumeHint !== null && parsed.now_playing) {
    parsed.now_playing_position = playbackResumeHint;
  }
  playbackResumeHint = null;
  handleNowPlayingUpdate(parsed);
}

const setupOverlayMenus = () => {
  if (PikaraokeConfig.hideOverlay) {
    $('#bottom-container').hide();
    $('#top-container').hide();
  }
  $("#menu a").fadeOut(); // start hidden
  const triggerInactivity = () => {
    mouseTimer = null;
    document.body.style.cursor = 'none';
    cursorVisible = false;
    $("#menu a").fadeOut();
    if (PikaraokeConfig.showSplashClock) {
      setTimeout(() => {
        if (!cursorVisible) $("#clock").fadeIn();
      }, 1000);
    }
    menuButtonVisible = false;
  };

  document.onmousemove = function () {
    if (mouseTimer) window.clearTimeout(mouseTimer);
    if (!cursorVisible) {
      document.body.style.cursor = 'default';
      cursorVisible = true;
    }
    if (!menuButtonVisible) {
      $("#menu a").fadeIn();
      $("#clock").hide();
      menuButtonVisible = true;
    }
    mouseTimer = window.setTimeout(triggerInactivity, 5000);
  };

  // Set initial state to hidden
  triggerInactivity();
  $('#menu a').click(function () {
    if (showMenu) {
      $('#menu-container').hide();
      $('#menu-container iframe').attr('src', '');
      showMenu = false;
    } else {
      setUserCookie();
      $("#menu-container").show();
      $("#menu-container iframe").attr("src", "/");
      showMenu = true;
    }
  });
  $('#menu-background').click(function () {
    if (showMenu) {
      $(".navbar-burger").click();
    }
  });
}

const setupVideoPlayer = () => {
  $('#video-container').hide();
  const video = getVideoPlayer();

  video.addEventListener("play", () => {
    $("#video-container").show();
  });

  const confirmPlaybackStartedOnce = () => {
    if (startSongSent || !isMaster) return;
    if (video.currentTime > 0.5 && isMediaPlaying(video)) {
      startSongSent = true;
      if (playbackStartGuardTimer) {
        clearTimeout(playbackStartGuardTimer);
        playbackStartGuardTimer = null;
      }
      socket.emit("start_song");
    }
  };

  // Master reports playback position to server
  setInterval(() => {
    if (isMaster && isMediaPlaying(video)) {
      socket.emit("playback_position", video.currentTime);
    }
  }, 1000);

  video.addEventListener("ended", () => {
    startSongSent = false;
    if (playbackStartGuardTimer) { clearTimeout(playbackStartGuardTimer); playbackStartGuardTimer = null; }
    if (isPrematureEnd(video) && tryPlaybackRecovery("video-ended-early")) {
      return;
    }
    endSong(isPrematureEnd(video) ? "video-ended-early" : "complete", !isPrematureEnd(video));
  });
  video.addEventListener("timeupdate", () => {
    $("#current").text(formatTime(video.currentTime));
    confirmPlaybackStartedOnce();
    if (video.currentTime > 1.0 && playbackStartGuardTimer) {
      clearTimeout(playbackStartGuardTimer);
      playbackStartGuardTimer = null;
    }
  });
  $("#video source")[0].addEventListener("error", (e) => {
    startSongSent = false;
    if (playbackStartGuardTimer) { clearTimeout(playbackStartGuardTimer); playbackStartGuardTimer = null; }
    if (tryPlaybackRecovery("source-error")) {
      return;
    }
    if (isMediaPlaying(video) || currentVideoUrl) {
      endSong("error while playing");
    }
  });
  video.addEventListener("error", () => {
    startSongSent = false;
    if (playbackStartGuardTimer) { clearTimeout(playbackStartGuardTimer); playbackStartGuardTimer = null; }
    if (tryPlaybackRecovery("video-error")) {
      return;
    }
    if (isMediaPlaying(video) || currentVideoUrl) {
      endSong("video-error");
    }
  });
  window.addEventListener(
    'beforeunload',
    function (event) {
      startSongSent = false;
      if (playbackStartGuardTimer) { clearTimeout(playbackStartGuardTimer); playbackStartGuardTimer = null; }
      if (isMediaPlaying(video)) {
        endSong("splash screen closed");
      }
    },
    true
  );
}

const setupBackgroundMusicPlayer = () => {
  $.get("/bg_playlist", function (data) {
    if (data) bg_playlist = data;
  });
  const bgMusic = getBackgroundMusicPlayer();
  bgMusic.addEventListener("ended", async () => {
    bgMusic.setAttribute('src', getNextBgMusicSong());
    await bgMusic.load();
    await bgMusic.play();
  });
}

const handleUnsupportedBrowser = () => {
  if (!isSupportedBrowser) {
    let modalContents = document.getElementById("permissions-modal-content");
    let warningMessage = document.createElement("p");
    warningMessage.classList.add("notification", "is-warning");
    warningMessage.innerHTML =
      PikaraokeConfig.translations.unsupportedBrowser;
    modalContents.prepend(warningMessage);
  }
}

const startClock = () => {
  if (clockIntervalId) return;
  const update = () => {
    const el = document.getElementById('clock');
    if (el) el.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: true });
  };
  update();
  clockIntervalId = setInterval(update, 1000);
}

const stopClock = () => {
  if (!clockIntervalId) return;
  clearInterval(clockIntervalId);
  clockIntervalId = null;
}

const toggleBGMedia = (configKey, playFn, disabled) => {
  PikaraokeConfig[configKey] = disabled;
  disabled ? playFn(false) : shouldBackgroundMediaPlay() && playFn(true);
};

const PREFERENCE_EFFECTS = {
  disable_bg_video:    (v) => toggleBGMedia("disableBgVideo", playBGVideo, v),
  disable_bg_music:    (v) => toggleBGMedia("disableBgMusic", playBGMusic, v),
  disable_score:       (v) => { PikaraokeConfig.disableScore = v; },
  show_splash_clock:   (v) => {
    PikaraokeConfig.showSplashClock = v;
    v ? startClock() : (stopClock(), $("#clock").hide());
  },
  hide_overlay:        (v) => {
    PikaraokeConfig.hideOverlay = v;
    $("#bottom-container, #top-container").toggle(!v);
  },
  hide_url:            (v) => { $("#qr-code, #screensaver-qr").toggle(!v); },
  bg_music_volume:     (v) => {
    PikaraokeConfig.bgMusicVolume = v;
    const player = getBackgroundMusicPlayer();
    if (isMediaPlaying(player)) $(player).animate({ volume: v }, 1000);
  },
  screensaver_timeout: (v) => {
    screensaverTimeoutSeconds = v;
    PikaraokeConfig.screensaverTimeout = v;
  },
};

const parsePreferenceValue = (value) => {
  if (typeof value !== "string") return value;
  if (value === "True") return true;
  if (value === "False") return false;
  const num = Number(value);
  return !isNaN(num) && value.trim() !== "" ? num : value;
};

const applyPreferenceUpdate = (data) => {
  const effect = PREFERENCE_EFFECTS[data.key];
  if (effect) effect(parsePreferenceValue(data.value));
};

const applyPreferencesReset = (defaults) => {
  Object.entries(defaults).forEach(([key, value]) => applyPreferenceUpdate({ key, value }));
};

const setupSocketEvents = () => {
  socket.on('connect', () => {
    console.log('Socket connected');
    socket.emit("register_splash");
  });
  socket.on('splash_role', (role) => {
    isMaster = (role === "master");
    console.log("Splash role assigned:", role, isMaster ? "(Master active)" : "(Slave active - read-only)");
  });
  socket.on('connect_error', (error) => {
    console.error('Connection error:', error);
    flashNotification(PikaraokeConfig.translations.socketConnectionLost, "is-danger");
  });
  socket.on('disconnect', (reason) => {
    console.warn('Socket disconnected:', reason);
    flashNotification(PikaraokeConfig.translations.socketConnectionLost, "is-danger");
  });
  socket.on('pause', () => {
    const video = getVideoPlayer();
    const currVolume = video.volume;
    if (!video.paused) {
      $(video).animate({ volume: 0 }, 1000, () => {
        video.pause();
        video.volume = currVolume;
      });
    }
  });
  socket.on('play', () => {
    const video = getVideoPlayer();
    const currVolume = video.volume;
    if (video.paused) {
      video.play();
      video.volume = 0;
      $(video).animate({ volume: currVolume }, 1000);
    }
  });
  socket.on('skip', (reason) => {
    const video = getVideoPlayer();
    const currVolume = video.volume;
    if (isMediaPlaying(video)) {
      $(video).animate({ volume: 0 }, 1000, () => {
        video.pause();
        video.volume = currVolume;
        hideVideo();
      });
    } else {
      video.pause();
      hideVideo();
    }
  });
  socket.on('volume', (val) => {
    const video = getVideoPlayer();
    if (val === "up") {
      video.volume = Math.min(1, video.volume + 0.1);
    } else if (val === "down") {
      video.volume = Math.max(0, video.volume - 0.1);
    } else {
      video.volume = val;
    }
  });
  socket.on('restart', () => {
    const video = getVideoPlayer();
    video.currentTime = 0;
    if (video.paused) video.play();
  });
  socket.on("notification", (data) => {
    const notification = data.split("::");
    const message = notification[0];
    const categoryClass = notification.length > 1 ? notification[1] : "is-primary";
    flashNotification(message, categoryClass);
    if (isMaster) {
      socket.emit("clear_notification");
    }
  });
  socket.on("now_playing", handleNowPlayingUpdate);
  socket.on("playback_stream_url_changed", (payload) => {
    // [V11 FIX-B BUG2 SEM SOM] Mesma musica, NOVA URL stream (retry do ffmpeg).
    // Trocar source do HLS imediatamente, NAO ESPERAR now_playing_update
    // (que pode demorar ou ate mesmo nao trocar se o title do now_playing
    //  for igual). Preserva posicao atual do video para nao voltar ao comeco.
    const newUrl = payload && payload.new_url;
    if (!newUrl) return;
    const newSubtitleUrl = payload && payload.new_subtitle_url;
    console.warn("[V11] playback_stream_url_changed! newUrl=", newUrl, "old=", currentVideoUrl);
    const video = getVideoPlayer();
    const savedPos = video && isFinite(video.currentTime) ? Math.max(0, video.currentTime - 0.5) : 0;
    currentVideoUrl = newUrl;
    playbackRecoveryAttempts = 0;
    startSongSent = false;
    if (playbackStartGuardTimer) { clearTimeout(playbackStartGuardTimer); playbackStartGuardTimer = null; }

    // Atualizar subtitle (se mudou)
    if (newSubtitleUrl) {
      if (octopusInstance) { try { octopusInstance.dispose(); } catch (_) {} octopusInstance = null; }
      try {
        octopusInstance = new SubtitlesOctopus({
          video: video, subUrl: newSubtitleUrl,
          fonts: ["/static/fonts/Arial.ttf", "/static/fonts/DroidSansFallback.ttf"],
          debug: true, workerUrl: "/static/js/subtitles-octopus-worker.js"
        });
      } catch (_) {}
    }

    $("#video-source").attr("src", "");
    video.load();
    $("#video-source").attr("src", newUrl);
    video.muted = video.muted || false;
    video.setAttribute("muted", video.muted ? "muted" : "");

    if (newUrl.endsWith('.m3u8')) {
      const useNativeHLS = video.canPlayType('application/vnd.apple.mpegurl') && !isChrome && !isEdge && !isMobileSafari;
      if (useNativeHLS) {
        video.src = newUrl;
      } else {
        if (hlsInstance) { try { hlsInstance.destroy(); } catch (_) {} hlsInstance = null; }
        hlsInstance = new Hls({
          startPosition: savedPos || 0,
          manifestLoadingMaxRetry: 12, manifestLoadingMaxRetryTime: 30000, manifestLoadingRetryDelay: 800,
          levelLoadingMaxRetry: 10, levelLoadingMaxRetryTime: 25000, levelLoadingRetryDelay: 800,
          fragLoadingMaxRetry: 10, fragLoadingMaxRetryTime: 25000, fragLoadingRetryDelay: 600,
          lowLatencyMode: false, backBufferLength: 30, enableWorker: true,
          xhrSetup: (xhr, url) => {
            xhr.addEventListener('error', () => {});
            const originalOnReadyStateChange = xhr.onreadystatechange;
            xhr.onreadystatechange = function (...args) {
              if (xhr.readyState === 4) {
                const status = xhr.status;
                if ((status === 404 || (status >= 500 && status < 600)) && !status._trae_forced) {
                  try { Object.defineProperty(xhr, 'status', { value: 0, writable: true, configurable: true }); } catch (_) { xhr.status = 0; }
                  try { Object.defineProperty(xhr, 'readyState', { value: 0, writable: true, configurable: true }); } catch (_) {}
                  return;
                }
              }
              if (typeof originalOnReadyStateChange === 'function') return originalOnReadyStateChange.apply(this, args);
            };
          }
        });
        hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
          if (savedPos > 1) {
            try { video.currentTime = savedPos; } catch (_) {}
          }
          const p = video.play();
          if (p && typeof p.catch === "function") p.catch(e => {
            if (!video.muted) { video.muted = true; video.play().catch(()=>{}); }
          });
        });
        hlsInstance.on(Hls.Events.ERROR, (_event, data) => {
          if (!data?.fatal) return;
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR &&
              data.response && (data.response.code === 404 || (data.response.code >= 500 && data.response.code < 600))) {
            if (tryPlaybackRecovery(`hls-network-${data.details}-status-${data.response.code}`,"network")) return;
          }
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR && tryPlaybackRecovery(`hls-network-${data.details}`,"network")) return;
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR   && tryPlaybackRecovery(`hls-media-${data.details}`,"media"))   return;
          endSong(`hls-fatal-urlchange-${data.type||"unknown"}-${data.details||"unknown"}`);
        });
        hlsInstance.loadSource(newUrl);
        hlsInstance.attachMedia(video);
      }
    }

    video.load();
    if (!newUrl.endsWith('.m3u8')) {
      setTimeout(() => {
        if (savedPos > 1 && video) { try { video.currentTime = savedPos; } catch (_) {} }
        video.play().catch(e => {
          if (!video.muted) { video.muted = true; video.play().catch(()=>{}); }
        });
      }, 180);
    }

    playbackStartGuardTimer = setTimeout(() => {
      if (!isMediaPlaying(video) && (video.paused || video.readyState <= 1)) {
        console.warn("[V11-urlchange] Playback guard timeout — firing end_song failed to start");
        endSong("failed to start (after url change)");
      } else {
        if (playbackStartGuardTimer) { clearTimeout(playbackStartGuardTimer); playbackStartGuardTimer = null; }
      }
    }, playbackStartTimeout);
  });
  socket.on("preferences_update", applyPreferenceUpdate);
  socket.on("preferences_reset", applyPreferencesReset);
  socket.on("score_phrases_update", (phrases) => { scoreReviews = phrases; });
  socket.on("retry_current_song", async (payload = {}) => {
    console.warn("Server requested current song retry:", payload);
    const resumePosition = Number(payload.position);
    playbackResumeHint = Number.isFinite(resumePosition) ? resumePosition : null;
    currentVideoUrl = null;
    if (hlsInstance) {
      hlsInstance.destroy();
      hlsInstance = null;
    }
    await loadNowPlaying();
  });

  socket.on("playback_position", (position) => {
    if (!isMaster) {
      const video = getVideoPlayer();
      if (isMediaPlaying(video)) {
        if (Math.abs(video.currentTime - position) > 2) {
          console.log("Slave drifting, syncing position to:", position);
          video.currentTime = position;
        }
      }
    }
  });
}

const handleSocketRecovery = () => {
  // A socket may disconnect if the tab is backgrounded for a while
  // Reconnect and configure event listeners when tab becomes visible again
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === 'visible') {
      autoplayConfirmed && loadNowPlaying();
      if (!socket.connected) {
        socket = io();
        setupSocketEvents();
      }
    }
  });
}

const setupUIScaling = () => {
  const urlParams = new URLSearchParams(window.location.search);
  const rawScale = urlParams.get('scale');
  if (!rawScale) return;
  uiScale = parseFloat(rawScale) || 1;

  const scaleTargets = [
    { selector: '#logo-container img.logo', origin: null },
    { selector: '#top-container', origin: 'top right' },
    { selector: '#ap-container', origin: 'top left' },
    { selector: '#qr-code', origin: 'bottom left' },
    { selector: '#up-next', origin: 'bottom right' },
    { selector: '#dvd', origin: null },
    { selector: '#your-score-text', origin: null },
    { selector: '#score-number-text', origin: null },
    { selector: '#score-review-text', origin: null },
    { selector: '#splash-notification', origin: 'top left' },
    { selector: '#clock', origin: 'top left' },
  ];

  scaleTargets.forEach(({ selector, origin }) => {
    const el = document.querySelector(selector);
    if (el) {
      el.style.transform = `scale(${uiScale})`;
      if (origin) el.style.transformOrigin = origin;
    }
  });
}

// Document ready procedures

$(function () {
  // Setup various features and listeners
  setupUIScaling();
  if (PikaraokeConfig.showSplashClock) startClock();
  setupScreensaver();
  setupOverlayMenus();
  setupVideoPlayer();
  setupBackgroundMusicPlayer();

  // Handle browser compatibility
  handleUnsupportedBrowser();
  testAutoplayCapability();
});


// Setup sockets and recovery outside of document ready to prevent race conditions
setupSocketEvents();
handleSocketRecovery();

// Fallback: if socket connected before listeners were attached, register now
if (socket.connected) {
  console.log('Socket already connected, registering splash...');
  socket.emit("register_splash");
}
