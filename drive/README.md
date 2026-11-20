# Drive -> cloud (no PC)

`drive.yml` takes a stream straight from Google Drive: download, probe (length, audio tracks, mic volume, 4 frames),
mic audio, parallel Whisper large-v3 transcription (10-min parts, up to 20 at once), cleaned transcript.

Start a job: commit ONE file `jobs/drive/<stem>.json`:

    {"file_id": "<Drive file id>", "stem": "Record_2026-09-05-21-04-02", "game": "league"}

Results land on branch `drive-out/<stem>/`: `probe.sealed` + `<stem>.wav.json.sealed` (reviewer key, drive/review_pub.pem)
and `<stem>.wav.json.enc` (RENDER_KEY, for the PC pipeline: put it in Scripts\scan\ as <stem>.wav.json after CLOUD_FETCH_DECRYPT).

## One-time setup (owner)
1. console.cloud.google.com -> new project "slade-render" -> APIs & Services -> enable **Google Drive API**.
2. IAM & Admin -> Service Accounts -> Create (name `slade-drive`, no roles) -> Keys -> Add key -> JSON (downloads a .json).
3. In Google Drive, share the stream backup folder with the service account's e-mail (…@….iam.gserviceaccount.com) as **Viewer**.
4. GitHub -> slade-render -> Settings -> Secrets and variables -> Actions -> New repository secret `GDRIVE_SA_KEY` = the whole .json text.
Then delete the .json from Downloads.
