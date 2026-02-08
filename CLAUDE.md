# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Discord music bot that downloads and plays audio from YouTube. Single-file architecture (`main.py`) using discord.py with yt-dlp for audio extraction.

## Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
# Create .env file with:
#   TOKEN=<discord_bot_token>
#   COMMAND_PREFIX=<prefix>

# Run the bot
python main.py
```

The bot automatically updates yt-dlp on startup via `update_yt_dlp()`.

## Architecture

### Core Classes

**Audio Pipeline:**
- `YTDLSource`: Handles yt-dlp extraction and local file downloads
  - Downloads audio files to `downloads/` directory (created on startup)
  - Uses yt-dlp with iOS/Android player clients to avoid 403 errors
  - FFmpeg post-processes to MP3 format
  - Plays from local files (not streaming URLs)
- `Song`: Wrapper around YTDLSource with embed creation
- `SongQueue`: asyncio.Queue subclass with shuffle/remove operations

**State Management:**
- `VoiceState`: Per-guild state tracking current song, queue, volume, voice connection
  - Owns the audio player task (`audio_player_task()`)
  - Auto-disconnects after 36 hours of inactivity
- `Music` Cog: Command interface, maintains `voice_states` dict keyed by guild ID

**UI:**
- `MyView`: Discord UI buttons for pause/resume/skip with 5-minute timeout

### Key Patterns

**File Downloads:**
The bot downloads files locally before playback:
1. `create_source()` calls `ytdl.extract_info(search, download=True)`
2. Gets filename from `ytdl.prepare_filename()` or `info['requested_downloads'][0]['filepath']`
3. Validates file exists, falls back to `.mp3` extension
4. Passes local file path to `FFmpegPCMAudio`
5. Downloaded files are kept permanently in `downloads/`

**yt-dlp Configuration:**
- Uses YouTube iOS/Android/web player clients via `extractor_args` (not browser impersonation)
- Downloads in `downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s` format
- FFmpeg extracts audio to MP3 at 192kbps quality
- Verbose logging enabled for debugging

**Voice Connection:**
- `VoiceState.voice` stores the guild's voice client
- Commands check `ctx.voice_state.voice` and auto-connect if missing
- Voice state cleanup handled in `stop()` method and cog unload

**Guild Isolation:**
Each guild has independent state via `Music.voice_states[guild.id]`, including:
- Separate song queue
- Independent volume settings
- Isolated voice connection
- Per-guild audio player task

### Logging

- Bot logs to `bot.log` and stdout
- YTDL operations logged at INFO level (download start, file location, size)
- Debug logs track filename resolution and header injection
- Voice connection changes logged with guild names

### Commands

All commands are hybrid (slash + prefix):
- `come/join` - Join user's voice channel
- `play/p <search>` - Download and queue audio
- `pause/resume/skip/stop` - Playback controls
- `queue/list` - Show queue (paginated, 10 per page)
- `volume` - Get/set volume (0-100)
- `player/gui/buttons` - Show control buttons
- `fix` - Reset voice state if broken
- `leave/l` - Clear queue and disconnect

Owner-only: `sync`, `forcerestart`, `listservers`, `opusloaded`, `loadopus`, etc.

### Environment Requirements

- Discord bot token in `.env`
- FFmpeg installed and in PATH
- Opus library for voice (auto-loads from common paths on startup)
- Python 3.7+ with discord.py 2.2.2
- yt-dlp 2023.7.6+ (auto-updates on startup)

### Common Issues

**403 Forbidden errors:**
- Ensure `extractor_args` uses iOS/Android player clients
- Check yt-dlp is up to date
- Verify downloads are happening (check `downloads/` directory)

**Opus not loaded:**
Bot tries multiple opus library paths on startup. If all fail, voice won't work.

**Voice state stuck:**
Use `/fix` command to reset guild's voice state and reconnect.
