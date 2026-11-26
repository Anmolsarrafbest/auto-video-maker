# Auto Video Maker

Generate 12-, 15-, or 20-second vertical science Shorts with original animated illustrations, Piper narration, and captions. Draft generation works without a Gemini key or a YouTube account. Uploading uses the free YouTube Data API after you connect your channel.

Quiz Shorts ask a question, say “Think about it,” then wait 1.5 seconds. Science-history stories, myth checks, and visual demonstrations use different timing. Archive, laboratory, observatory, and ocean treatments use different palettes and custom illustrations. The app selects one of three local Piper voices and composes an instrumental score for each episode. The exported Flow prompt and upload metadata record the selected treatment and narration credit.

## Run locally on Windows

From this folder in PowerShell, using Python 3.11 or newer on 64-bit Windows:

```powershell
python -m pip install --target .deps -r requirements.txt
python shorts.py setup-voice
python shorts.py generate --topic venus-spin --seconds 12 --offline
```

The first two commands download open-source dependencies and three English Piper voice models, including Scottish and U.S. English narrators. The Python package supplies FFmpeg. `setup-voice` saves each pinned model card, and video descriptions credit the voice used.

Topics can use `en-IN` (the installed Windows English (India) voice Microsoft Heera) or `hi` (offline eSpeak NG with Devanagari captions and Hindi YouTube metadata). Windows Indian English needs the English (India) speech pack. Hindi captions use Nirmala UI on Windows and require Noto Sans Devanagari on Linux. Choose a language for queue preparation with `--language en`, `--language en-IN`, or `--language hi`. Each episode remains a single topic in the queue.

History topics can use an archival photograph from a catalogue entry in `topics.json`. The entry records the real image, its archive page, its credit, and its caption. Download the verified files once, then the renderer can use the cache offline:

```powershell
python shorts.py setup-assets --topic katherine-johnson --topic wright-first-flight
```

Run `python shorts.py setup-assets` to try all the listed archives. If a source is unavailable, the video uses its original diagram and leaves the image credit out. When footage is available, the opening photo is identified on screen, followed by the original explanatory animation. Check usage terms before adding a new archive entry.

Open the printed `short.mp4` path. The same folder contains `preview.png`, `metadata.json`, and `flow-prompt.txt`. The Flow prompt is optional and can be pasted into Google Flow manually.

```powershell
python shorts.py generate --seconds 15 --region IN
python shorts.py daily --count 2 --seconds 12 --region IN
```

`generate` tries Google Trends and renders one draft. `daily` creates dated slots in India time by default. Re-running it on the same date reuses the existing slots. The worker only picks exact matches from its sourced science, space, and ocean topic library. Without an eligible match, it picks an unused evergreen topic. Add fresh entries to [topics.json](topics.json) when the library runs out. Each entry needs a unique ID, factual source, two short narration beats, two labels, and one of the existing scene names. Political and religious topics should stay outside the library.

## Optional Gemini drafts

Use this only if you have a separate Gemini API key from a project on the free tier with billing disabled. Your Jio Google AI Pro chat and Flow access work independently of the API.

Set `GEMINI_FREE_API_KEY` in your shell, then run:

```powershell
python shorts.py generate --topic saturn-rings --seconds 15 --gemini
```

Gemini rewrites the narration using the sourced fact supplied by the app. Review the result before upload. Daily jobs use the curated scripts and need no Gemini key. [Gemini API pricing](https://aistudio.google.com/docs/pricing#gemini-3.5-flash-lite)

## Connect YouTube and test a private upload

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable **YouTube Data API v3**. This uses an API project for authentication, with no Cloud Run deployment, VM, paid model, or billing setup needed for this app.
2. Configure the OAuth consent screen. For external Testing, add the Google account that owns the channel as a test user. Create a **Desktop app** OAuth client and download its JSON.
3. Run `python shorts.py connect-youtube --client "C:\path\to\downloaded-client.json"` with the actual downloaded file path, and sign in to the intended channel. You can also save the file as `secrets/youtube-client.json` and omit `--client`. Copy the channel ID shown in the terminal. Tokens stay in `secrets/`.
4. Review the draft and its metadata, including the audience and synthetic content settings. Use the printed output folder name in the example below.

```powershell
python shorts.py upload 'venus-spin-REPLACE_WITH_ACTUAL_SUFFIX' --visibility private
```

For a Gemini draft, add `--reviewed` after checking its facts and narration. Uploads default to private. If an upload is interrupted, rerun with the same job, visibility, and metadata. The app saves the resumable session and then the YouTube video ID. If a session expires, check YouTube Studio before clearing the local record. A private test job stays associated with that upload. You can change its visibility directly in Studio.

External OAuth consent screens in Testing issue refresh tokens that usually expire in seven days. For recurring use, switch the app to In production as appropriate for your account and Google’s OAuth requirements, then reconnect. See [Google OAuth refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

## Run on a schedule

Each upload includes a question headline, a short explanation, a source link, and topic-specific tags and hashtags. For every new draft, the app checks Google Trends in the U.S. and India, plus the selected region if different. It adds a trend to the description only when it matches a vetted alias for that topic. The standard hashtags are used when there are no matches, a feed is unavailable, or you pass `--offline`. Review or edit the draft's `metadata.json` before starting its upload.

After connecting the channel, register the daily Windows task from PowerShell. The first example uses private visibility. After verifying a private upload in YouTube Studio, run the second command to switch the schedule to public:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup-publisher.ps1 -At 18:30 -Count 1 -Seconds 15
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup-publisher.ps1 -At 18:30 -Count 1 -Seconds 15 -Visibility public
```

The second registration replaces the first schedule. The PC needs to be on with your Windows user signed in. Use `-Count 2` to upload two videos in the same daily run. Use `-WhatIf` to preview the schedule, or disable **Auto Video Maker - YouTube Shorts** in Task Scheduler to pause future runs. If you already ran today's slots privately, the public schedule applies to new slots. You can change a private video's visibility directly in YouTube Studio.

To run the daily publisher manually:

```powershell
python shorts.py daily --count 1 --seconds 12 --upload public --channel-id 'UC_REPLACE_WITH_YOUR_CHANNEL_ID'
```

Use `--count 2` for two slots per run. Keep `--upload private` for continued private testing or omit `--upload` for local drafts. Review the applicable defaults in each draft’s `metadata.json`. The standard renderer produces stylized illustrations for a general audience.

If an upload fails, rerun the task on the same day, or resume an older job explicitly with `upload`.

The project is local. Credentials, generated videos, voice files, and upload records are ignored by Git. Keep those files on the same machine so daily counts, the used-topic history, and upload resumption survive restarts.

## No-bill publishing queue

Run the queue on your Windows PC. It uses local Piper narration, audio, illustrations, and FFmpeg. GitHub stores the code. The hosted pilot is paused. To resume its remaining topics locally, prepare them for review:

```powershell
python queue_publish.py prepare --topic rainbow-split --topic moon-distance --topic mars-sunset
python queue_publish.py status
```

Use `python queue_publish.py prepare --count 4` to prepare a mixed batch of unused topics. Preview each MP4 in `output/` and confirm its factual claim, source, and visuals. Approve individual drafts after checking them. Editing the video or metadata clears the effective approval until you approve again.

To update older unpublished drafts after installing the voice set, run `python queue_publish.py refresh`. Older media and any prior approvals are saved in `state/previous-renders/`. Review the new version as its own draft. At review time, check that the explanation or demonstration teaches something distinct. A change of voice, music, or colors alone is not an editorial reason to publish.

```powershell
python queue_publish.py approve queue-katherine-johnson
python queue_publish.py publish --count 1 --visibility public --channel-id 'YOUR_CONNECTED_CHANNEL_ID'
```

For daily uploads from the approved queue, use the Windows scheduler installer with `-Queue`. The PC needs to be on and signed in. The scheduler skips drafts that still need review.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup-publisher.ps1 -Queue -At 18:30 -Count 1 -Visibility public
```

Start with at most one approved public Short a day. Skip days when a draft needs more work. After 10–15 releases, compare their viewer retention and decide which topics and formats merit more episodes. Keep this on your PC while the budget is zero. Archive credits, sensible pacing, and editorial review reduce risk, but there is no guaranteed strike-free cadence. [YouTube spam policy](https://support.google.com/youtube/answer/2801973)

Record YouTube Studio metrics at the same age after upload so you can compare formats in `status`. Use your actual numbers:

```powershell
python queue_publish.py metrics queue-katherine-johnson --age-hours 48 --stayed 55 --average 81 --engaged 120
```

For strict zero GitHub runner spend, keep the cloud workflow disabled. If you later want to run hosted Actions, first check [GitHub Billing](https://github.com/settings/billing). GitHub blocks overages when the account has no valid payment method. If you use a payment method, verify an Actions budget with **Stop usage when budget limit is reached** before enabling hosted runs. [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) · [GitHub budget settings](https://docs.github.com/en/billing/how-tos/set-up-budgets)

## Five-Short cloud pilot

The private GitHub repository's **Five-Short cloud pilot** workflow tests the uploader on a hosted Linux runner. It is specific to the Skill_issue channel and uses the repository Actions secret `YOUTUBE_TOKEN_JSON`. The Git history contains only pilot topic IDs, dates, and public video IDs. Keep the secret out of source control.

When enabled, the pilot can run on demand or around 6:37 p.m. India time, uploads at most two public Shorts per India calendar day, and disables itself after five. See `pilot-progress.json` for completed uploads. A reservation with no video ID stops further uploads until the channel owner checks YouTube Studio and reconciles the record. This protects against a second upload after a runner interruption. Finish the outstanding topics through either the local queue or the hosted pilot, since they use separate upload records.

## Prepare Instagram Reels

The local publisher uses **Instagram API with Facebook Login** so it can send local MP4 files directly. Create an Instagram profile, switch it to a **professional Creator** account, and link it to a free Facebook Page you manage. Create a Meta developer app with the Instagram API with Facebook Login publishing use case. Use a Page access token for the linked Page and the numeric Instagram professional account ID returned by `instagram_business_account`. Follow Meta’s current steps and permission requirements for the app mode you use. [Meta setup](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/get-started) · [Meta Reels publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing)

```powershell
python instagram_publish.py preview queue-katherine-johnson
python instagram_publish.py connect --account-id YOUR_NUMERIC_ID --username YOUR_PROFILE_NAME
python instagram_publish.py approve queue-katherine-johnson
python instagram_publish.py publish-next
```

Enter the Page token at the hidden prompt. The token stays in the ignored `secrets/instagram.json` file. Review the video in the local queue first, then approve it explicitly for Instagram. `publish-next` can publish one approved Reel per India calendar day. It publishes immediately when Meta finishes processing. Start with one manual test, then use Windows Task Scheduler to run `python.exe` with the absolute path to `instagram_publish.py` and argument `publish-next`. Use the project folder for **Start in**. The PC must be on and signed in. If an earlier publish has an uncertain outcome, reconcile it on the Instagram profile before changing the local receipt.
