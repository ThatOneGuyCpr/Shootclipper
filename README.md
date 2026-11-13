# Shoot Clipper

Press **Make clips** and it finds a timely wrestling interview, transcribes it, has Claude pick the best moments, and renders finished 9:16 TikToks into a `Ready to post` folder.

## Install

1. Download the newest `ShootClipper-Setup-….exe` from the [Releases page](https://github.com/ThatOneGuyCpr/Shootclipper/releases/latest) and run it. Windows may say "Windows protected your PC" because the installer isn't code-signed: press **More info**, then **Run anyway**.
2. Open Shoot Clipper from the Start menu or the desktop shortcut. ffmpeg comes built in, so there's nothing else to install.
3. In the app, open **Settings** (changes save as you make them):
   - Paste your Claude API key and press **Save** (create one at platform.claude.com under API keys, and add a few dollars of credit under Billing).
   - Point **2K captures folder** at your recordings and check **2K speed** (2x by default).
4. Optional: under **Daily run**, pick a time like 16:00 and press **Turn on** so clips are waiting when you get home.

The window uses Microsoft Edge WebView2, which is already on Windows 11 and current Windows 10. If the window can't open, the app opens in your browser instead and works the same way.

The first transcription downloads the Whisper model (about 0.5 GB on CPU, 1.6 GB for the GPU model). After that it works offline.

## Daily use

**Make clips** finishes anything you've queued first (including an interview that stopped with an error; it gets two more tries before it waits for you). With nothing queued, it checks this week's wrestling news, searches YouTube for interviews full of stories, and lists them under **Found interviews** on Home. Each one shows a thumbnail (click it to watch on YouTube), the channel, views, and what stories it likely holds. Press **Make clips** on the one you want; **Queue** saves it for later, and **Not this** hides it for good. **Find more** searches again.

Once an interview is chosen, it downloads, transcribes, has Claude pick the best stories, renders the top clips (2 by default) and opens the folder. Then you watch them, tweak anything in **Review** if needed, and upload.

Prefer the old behavior? Settings > Make clips > **Choosing interviews** > Pick the best one automatically. The daily run always picks on its own.

**Stop**: while anything runs, Make clips turns into **Stop** (there's one in the bottom bar too). Whatever finished is kept, so a stopped transcription starts over but a finished download doesn't, and Make clips picks up from there.

To use a specific video, paste its link on **Home** (or **Add files…**), choose its source preset if it has a burned-in watermark, and press Make clips.

## What it looks for

Stories come first: anecdotes told by someone who was there, with a setup and a payoff (backstage fights, ribs, firings, road stories, how a famous moment really happened). Any era counts. Timeliness gives a good story a boost but doesn't rescue a weak one, and opinions, hot takes and news reactions rank below a decent story.

## Storage

Settings > **Storage** shows how much space interview videos and finished clips take, with two rules:

- **Delete interview videos** (default: a week after clips are made). Transcripts stay, and if you re-render one of its clips later, the video downloads again by itself.
- **Delete finished clips** (default: a month after rendering). Scheduled clips are never deleted; posted clips keep their view counts in History.

Rules run when the app opens and after each Make clips; **Clean up now** runs them immediately. **Delete** on an interview in Home removes it with its download and transcript (finished clips stay in Library). Videos you added from your own folders are never deleted.

## The screens

- **Home**: the Make clips button with live steps (Download, Transcribe, Analyze, Render), your interviews, this week's topics and suggested interviews. **Log** in the bottom bar opens the full log.
- **Review**: the source plays in the app. Drag the red handles on the waveform to move the start and end, or click a word in the transcript to start there (Shift-click to end there). Words light up as they're spoken. **Snap to words** tidies the edges. Captions, hashtags and layout save as you edit. **Render clip** makes the video; **Finished** shows it in a phone frame.
- **Library**: thumbnails of finished clips; hover to play. **Copy caption** copies caption and hashtags. **Schedule** assigns the next posting slot (set the same time in TikTok Studio). **Mark posted** moves it to history, where you can log views.
- **Settings**: everything adjustable, grouped by job. The moon/sun button at the bottom left switches light and dark; by default the app follows Windows.

Review shortcuts:

| Key | Does |
| --- | --- |
| Space | Play or pause (plays the selection, stops at the end) |
| I / O | Set the start / end at the playhead |
| J / L | Step back / forward half a second (hold Shift for a tenth) |

## What the clip looks like

Matched to your edits:

- 1080x1920. Your 2K match fills the top 47%; the interview, face-tracked and zoomed in, fills the bottom.
- Captions sit on the seam, three words at a time, current word in yellow, heavy italic, sized to match your CapCut exports (Poppins Black ships with the app; type Komika Axis in Settings if you install CapCut's font).
- The 2K match plays at one fixed speed (2x by default) and is timed so its finish and Winner screen land as the clip ends. Gameplay audio is always muted.
- A name plate (speaker and source, like your HBK clip) slides in for the first few seconds.
- If a clip has no matching 2K capture, it falls back to a full-screen face-tracked crop with the same captions.
- Burned-in lower-third watermarks are kept out of the zoomed interview crop when the source preset knows about them.
- Hook text and context cards are available in Settings but off, since your edits don't use them.

Every video gets a matching `.txt` file with its caption and hashtags, handy if you upload from your phone.

## 2K footage

Point **Settings > 2K footage folder** at your captures. Name them after the matchup, or use a folder per wrestler:

```
2K\Ventura vs Hogan.mp4
2K\HBK vs Cornette.mp4
2K\Enzo Amore\vs Gotch NXT.mp4
```

Claude names the two people who should fight for each clip (usually the speaker and who they're talking about), and a capture with both of them beats one with only one. Nicknames work if they're in your name list (`Shawn Michaels = HBK`, `Rob Van Dam = RVD`), and so do last names like "Cornette."

**2K speed** is one setting for every capture, since you record at normal speed. The finish always lands on the clip's last frame:

- Match longer than the clip needs: it starts partway in.
- Match too short: other captures of the same matchup play first (name them alike, such as `Enzo vs Gotch 1.mp4` and `Enzo vs Gotch 2.mp4`).
- Only one short capture: it loops from the start.

At 2x, a 90-second clip uses 3 minutes of capture.

**Layout for Make clips**: **split when matched** (your current format, default), **alternate** (every other clip, so the Library header can compare average views with and without 2K), or **face only**.

In **Review** you can switch any clip's layout, pick a specific capture with **Choose…**, or press **Auto-match**.

## Costs

Pro plans don't include API credits, so API mode uses prepaid credit. Current Sonnet 5.5 rates:

| Step | Typical cost |
| --- | --- |
| Picking clips from 1 hour of interview | about $0.08 |
| News scout (web search) | about $0.25 to $0.50 per refresh |
| Ranking YouTube candidates | about $0.03 |

At three posts a week that's roughly **$4 to $8 a month**, most of it the news scout. Changing **Check the news every** in Settings from 2 days to 4 roughly halves that part. Every call logs its cost. **Paste** mode (Settings) costs nothing: the app copies a packet you paste into your Claude Project and you paste the reply back. Paste mode can't search for interviews on its own, so you'd queue links yourself.

## Fixing transcript names

Whisper mishears wrestler names. Two text files fix most of it:

- **Name hints**: names sent to Whisper as spelling hints. Add anyone who keeps getting mangled.
- **Fixes**: `what it wrote => what it should say`. Applied to every transcript automatically.

Both are under Settings > Files & schedule.

## Sources

The scout favors interview and podcast channels (Chris Van Vliet, Inside the Ropes, Busted Open and others in **Edit Preferred channels**) and never pulls from official promotion channels (**Edit Blocked channels**), since match footage gets Content ID claims fast.

## If something goes wrong

- **Install fails mentioning onnxruntime or "ResolutionImpossible"**: the environment was built with a Python that's too new. The current launcher detects this, deletes the `.venv` folder and rebuilds it with 3.12; if it still fails, delete `.venv` yourself and relaunch.
- **"ffmpeg not found"** (source copies only; the installer includes it): run `winget install Gyan.FFmpeg` in a terminal, then relaunch.
- **Transcription is slow**: the CPU model takes roughly a third of the video's length. An NVIDIA GPU makes it several times faster; if the log says the GPU failed to load, it falls back to CPU automatically.
- **A clip starts or ends mid-word**: open it in Review, press Snap to words or drag the handles, then Render clip.
- **Wrong person in frame on a two-shot**: switch Layout to Wide and render again.
- **Download fails**: YouTube changes often, and a new Shoot Clipper version usually brings a fixed yt-dlp. Check for updates in Settings.
- **A source won't play in Review** (some MKV or AVI files): press **Make a preview copy** on the player. Rendering works either way.
- Everything the app does is written to `%APPDATA%\ShootClipper\app.log` (Settings > Open app log). Scheduled runs also write `autopilot.log` in the same folder. Send both if something breaks.

## Updating

When a new version is out, **Update to …** appears in the bottom bar a few seconds after the app opens (or press **Check for updates** in Settings > Updates). Click it and Shoot Clipper downloads the new version, closes, installs it and reopens. Your settings, library, API key and text files live in `%APPDATA%\ShootClipper`, so updates never touch them. Uninstalling leaves them in place too.

## Moving from the zip version

Install as above. Your settings, library and API key carry over automatically. Then:

- If you turned on **Daily run**, press **Turn on** again in Settings so it points at the new app.
- Delete the old unzipped folder (including its `.venv`), or keep it if you like running from source.

## Running from source

Developers can still run from a copy of this repository: double-click `Launch Shoot Clipper.bat`, which sets up Python 3.12, the packages and ffmpeg. Updates for a source copy go through Settings > Updates > **Install update…** with a zip.

To publish a new version, change the `VERSION` file on main (for example to `2026.11.14`). GitHub Actions builds the installer, runs a self-test on Windows and attaches it to a release, and installed copies pick it up the next time they open.
