**Much less waiting**
- Before downloading anything, Shoot Clipper reads YouTube's captions and has a small Claude model find the stories. Found interviews now show how many stories each holds and name the best ones; interviews with none are dropped.
- Only the story sections are transcribed, instead of the whole interview. If you stretch a clip past a transcribed section, the extra is transcribed when you render (or press Transcribe more around this clip in Review). To transcribe everything as before: Settings > Transcription > Transcribe.

**Better edits**
- On shots with two or more people, the interview crop cuts to whoever is talking and reframes for their height. Switch off with Settings > 2K footage > Follow the speaker.
- 2K captures no longer need trimming: the app finds the winner screen (or the victory celebration) once per capture and times the clip to end there. Switch off with Find the finish.
- Old 4:3 interviews with bars down the sides are cropped inside the real picture.

**Fewer things that can break**
- The YouTube downloader now updates itself, daily and whenever a download fails, without waiting for a Shoot Clipper update.
- A full code review fixed a batch of bugs: Stop could leave thumbnails and previews failing afterwards; Find more replaced your found interviews instead of adding to them; a file that isn't a video left a broken entry behind; a damaged settings or news file could stop the app loading; the daily run and the app could work on the same interview at once.
- Claude errors now say what to do (bad key, out of credit, busy servers) instead of showing raw error text.
- Every release now has to pass 116 automated tests on Windows, plus the packaged app's self-test, before it is published.
