# Grook — X reply bot

A standalone companion to the supplied Grook Next.js site. The site's existing xAI chat is specialised for Solana; this worker uses xAI with a separate general-purpose voice. No changes to the site are required. Copy this directory next to `web/` if you want both in one repository.

## Deploy this repository to Render

Open https://dashboard.render.com/blueprint/new?repo=https://github.com/Sticksimsai/grook-x-bot .
Choose your intended workspace, connect GitHub if prompted, and enter the five secrets requested by the Blueprint: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, XAI_API_KEY. Review the worker and disk price shown by Render before applying. This is a paid worker with 0.5 CPU / 512 MB RAM and a 1 GB persistent disk.

The Blueprint configures @grook, preview-only mode, and a daily cap of 10. On a successful start, logs show `Grook @grook: PREVIEW`. Tag the account under a new post after startup and check logs for a draft. No public reply is sent in preview mode. Keep credentials in Render, never in this repository.

Before going live, change the two launch flags in `render.yaml` as well as completing X approval and preview review; values declared here are reapplied on Blueprint sync. The source-controlled Blueprint is the source of truth for non-secret configuration. Supply/rotate secret values through Render's Environment settings.

## What it does

- Polls mentions of your account and responds only to direct summons. Bare tags under a post mean “your take?”. The model skips incidental mentions and spam.
- Reads the mention, up to eight posts along its parent chain, quoted posts, and the conversation root when available. It replies to the mention, not to an uninviting original author.
- Writes concise, natural replies with reasoning. Dry humour is optional. No canned greetings, forced crypto talk or unsolicited promotion.
- Gives the model web and X search for current/disputed facts. Image understanding is available through X search, not through the initial text lookup. If essential media or context remains unavailable, it asks for clarification.
- Uses SQLite to track mentions, drafts, opt-outs, retries and posted reply IDs. A file lock prevents overlapping processes using the same data directory.
- Starts in preview mode. Preview drafts remain drafts when you later switch to live; they are never silently bulk-posted.

## Requirements

1. Python 3.11+ on Linux or macOS, or Docker. No pip dependencies.
2. An X Developer app with API access/credits for mentions, post lookup, user lookup and creating replies. A normal X account alone is insufficient.
3. OAuth 1.0a API key + secret and the bot account's user access token + secret, with Read and Write permissions. Regenerate user tokens if permissions changed after issue.
4. An xAI API key with access to the selected model and its search tools. You can use the same provider/key as the site's chat.
5. X's explicit approval before operating an AI automated reply bot. Keep preview mode until approved. Read https://help.x.com/en/rules-and-policies/x-automation .

Set the account's automated-account disclosure and make its independent identity clear. Publish the opt-out instruction in the bio or pinned post: “Tag me with a question. Reply @YOUR_HANDLE stop to opt out; @YOUR_HANDLE start to opt back in.” The bot processes these commands without posting a confirmation.

## Local preview

```sh
cd Grook-x-bot
cp .env.example .env
# Fill .env locally with your credentials and exact X handle. Never commit it.
python3 -m unittest -v
python3 bot.py
```

The first run records its start time and does not process old mentions. After starting it, tag the account beneath a new post with a question. The next poll should print a JSON draft. Preview makes real API calls and can incur X/xAI charges, but cannot post.

Stop the worker with Ctrl+C. To inspect the latest 50 stored outcomes:

```sh
python3 bot.py --status
```

`--status` needs no API calls; stop the active worker first because both use the same lock. `python3 bot.py --once` runs one poll for a scheduler. The first invocation only establishes the start time, so invoke again after a new mention.

## Voice tuning

Edit `voice.txt`. It controls the breadth, style, evidence standards, and decision to reply. Changes take effect on the next generation without restarting. Start with real examples before adjusting:

| Original post | Tagging reply | Desired behaviour |
|---|---|---|
| A claim about breaking news | “@handle is this true?” | Search, distinguish confirmed facts from the claim, cite a useful source |
| “50% down needs 50% up” | “@handle?” | Explain the arithmetic directly |
| A confusing argument | “@handle explain” | Explain the argument in plain language |
| Inaccessible screenshot | “@handle what does this say?” | Ask for the text if search cannot read it |
| “I saw @handle yesterday” | No question | Skip the incidental mention |

Natural writing does not require pretending to be human. The prompt identifies Grook as an independent automated account. Style and factual quality still need review on real drafts; prompt rules alone do not guarantee every response is correct.

## Going live

Once API access, X approval and real preview quality are confirmed, set:

```dotenv
X_AI_REPLY_APPROVED=true
X_BOT_LIVE=true
```

Restart. Only new/pending eligible mentions will be answered. Start with a low `MAX_REPLIES_PER_DAY` such as 10. The default is 50 in a rolling 24-hour window; this includes drafts and uncertain sends. A five-minute per-author cooldown reduces rapid reply loops. Mentions older than 24 hours expire. Search/token charges are separate: the reply cap is not a hard monetary budget, so configure provider spending limits too.

## Hosting

Run a single always-on background worker with a persistent disk. This is deliberately separate from Next.js request handlers: it is not a Vercel serverless loop. No inbound port is needed. Supply secrets using the host's private environment settings.

```sh
docker build -t grook-x-bot .
docker run --restart unless-stopped --env-file .env \
  -v grook-bot-data:/data grook-x-bot
```

Persist and back up `/data` (SQLite plus its WAL). Never run multiple replicas on separate disks: each would have its own deduplication record and could post duplicates. Do not delete the database to fix an error. Credentials and local runtime data are excluded from the Docker build context and Git example.

## Failures and operations

- 401/402/403 stops the worker: check credentials, permission, credits or X restrictions. Logs omit API bodies and credentials.
- 429 backs off using X's rate-limit reset header when supplied. Failed generation retries with backoff, up to five attempts, then becomes `failed`.
- Every outgoing reply is recorded as `sending` before the API call. If the result is unknown it becomes `uncertain`; a crash may leave `sending`. Neither is retried automatically. This trades an occasional missed reply for avoiding duplicates.
- For `sending`/`uncertain`, check the account's replies manually. Do not blindly reset it to pending. There is no exactly-once transaction shared between SQLite and X.
- `failed` jobs require inspection; there is no automatic replay command. `draft`, `skipped`, `expired`, `unavailable`, `opted_out` and `command` are terminal outcomes.
- Private/deleted/restricted posts, unavailable media, long threads beyond the traversal limit, and provider failures limit what it can understand. This is not an unrestricted “read any post” capability.
- SQLite holds mention text and drafts on your host. Keep the disk private, apply your retention/deletion policy, and preserve deduplication IDs when cleaning old content.

## Verification and remaining work

The included tests use mocked X and model calls. They cover OAuth signing against a reference vector, pagination/cursor failure, context traversal, long-post text, directed mentions, reply validation, preview isolation, successful/ambiguous sends, opt-out, unavailable posts, retry scheduling and budget limits.

No real X or xAI calls were run during creation: credentials were not provided. Before launch, verify your account's API entitlement, model/tool compatibility, read/response field names, one complete preview and one approved live reply. The implementation uses the established X v2 `/2/tweets`, `tweet.fields`, and `referenced_tweets` interface; text parsing also accepts `note_post` and `referenced_posts` response aliases. X's current reference pages and guides use mixed naming; validate with your app's actual response before scaling.

References consulted:
- https://docs.x.com/x-api/users/get-mentions
- https://docs.x.com/x-api/posts/get-post-by-id
- https://docs.x.com/x-api/posts/manage-tweets/introduction
- https://docs.x.ai/developers/tools/web-search
- https://docs.x.ai/developers/tools/x-search
- https://help.x.com/en/rules-and-policies/x-automation
