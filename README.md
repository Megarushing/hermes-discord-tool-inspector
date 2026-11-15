# Hermes Discord Tool Inspector

Inspect each Hermes tool call directly in Discord: one compact message, one expand button, and the actual input and returned output.

Verbose tool logs can overwhelm a conversation; short progress messages omit the details needed to understand what happened. This plugin keeps each completed call collapsed until you want to inspect it. There is no dashboard to open, second Discord bot, or extra model request.

## What it looks like

Illustrative layout (Discord places the button on the right):

````text
terminal · printf 'hello\n'                         [⋁]

# After expanding that entry:
terminal · printf 'hello\n'                         [⋀]

Input
```
{
  "command": "printf 'hello\n'"
}
```

Output (exit code: 0)
```
hello

```
````

Each call expands independently. Long records get Previous/Next buttons within that same message. Clicking a button reads a stored record; it **never runs the tool again**.

**Privacy first:** expanded messages are visible to everyone who can read the channel. Button permissions do not make the displayed output private. Even collapsed previews can contain private commands or paths. Enable this only in conversations trusted for the underlying tool data.

## Features

- Actual tool arguments and returned results, including errors—not an AI-generated account of what ran.
- Separate, copyable Input and Output blocks, readable Unicode/newlines, and per-call pagination.
- Optional-by-provider reasoning-summary entries: only explicitly supplied `summary_text`, never raw or encrypted thinking. Providers that do not supply this field produce no reasoning entry.
- Persistent buttons backed by profile-local SQLite; records survive gateway restarts until they expire.
- Explicit channel/thread allowlists, or an opt-in wildcard for every originating Discord conversation.
- Original-requester controls, optional linked owner accounts, and separately configured administrators.
- Configurable local retention, secret-pattern redaction before persistence, and suppressed mentions.

## Requirements and compatibility

1. A working [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) installation with its Discord gateway already connected. The plugin reuses that bot and its token; it does not configure Discord or change which users Hermes accepts.
2. A Hermes build with native `post_tool_call` and `post_api_request` hooks, `register_platform_handler`, `on_unload`, profile-local plugin storage, task-local gateway session context, delegated-child detection, and forced text redaction. See the [native plugin documentation](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/).
3. Hermes's Discord environment must include `discord.py` with Components V2 `LayoutView`, `Section`, `TextDisplay`, and `ActionRow` support. These APIs were introduced in discord.py 2.6; use the Discord dependency version supported by your Hermes installation rather than upgrading its packages independently.
4. The bot needs access to each enabled conversation and permission to send messages there, including Send Messages in Threads where applicable. An allowlist entry does not grant Discord access.

Verified locally with Python **3.11.15**, discord.py **2.7.1**, and Hermes source revision **`3b42157858`**. No minimum Hermes release number or blanket compatibility with older builds is claimed. Windows and other Python versions have not been exercised by this project's tests.

The plugin uses native extension points: no Hermes core patches, monkeypatches, additional bot connection, web service, or additional LLM calls.

## Install

### Default profile

Run these commands in your terminal. Replace `YOUR_CHANNEL_ID` with the actual numeric Discord channel ID **before** configuring it. Discord's Developer Mode exposes Copy Channel ID.

```sh
hermes plugins install https://github.com/Megarushing/hermes-discord-tool-inspector --no-enable --yes-deps
hermes config set plugins.entries.discord-tool-inspector.settings.channels '["YOUR_CHANNEL_ID"]'
hermes plugins enable discord-tool-inspector
```

`--no-enable` keeps installation separate from activation. `--yes-deps` explicitly accepts any dependency preparation requested by Hermes's installer; inspect the source before trusting any plugin. This plugin has no separate Python dependency file and relies on Hermes's existing Discord environment. Hermes may nevertheless prepare or download its managed Python/runtime dependencies during installation, so a fresh install needs network access and a healthy package-manager setup (`hermes pm doctor`).

Optionally suppress Hermes's separate verbose Discord progress messages to avoid duplicate noise. This does not disable inspector capture and does not change other platforms:

```sh
hermes config set display.platforms.discord.tool_progress off
```

Then **restart the gateway from an external terminal**, not from the bot conversation it is serving:

```sh
hermes gateway restart
```

If you run the gateway in a foreground terminal or container rather than as a managed service, restart that process through its normal supervisor. Do not start a second gateway for the same bot token.

### Named profiles

Use the same profile for installation, configuration, enabling, and restart. For a profile named `work`:

```sh
hermes --profile work plugins install https://github.com/Megarushing/hermes-discord-tool-inspector --no-enable --yes-deps
hermes --profile work config set plugins.entries.discord-tool-inspector.settings.channels '["YOUR_CHANNEL_ID"]'
hermes --profile work plugins enable discord-tool-inspector
hermes --profile work config set display.platforms.discord.tool_progress off
hermes --profile work gateway restart
```

Each profile has its own settings and database. Installing in the default profile does not install it in other profiles.

### Verify the installation

```sh
hermes plugins list
hermes plugins doctor discord-tool-inspector --ci
```

Then, in an enabled Discord conversation, ask your bot to run a harmless command such as `printf 'inspector test\n'`. Check that:

1. A separate compact entry appears after the tool finishes.
2. The requesting account can expand it and read the real arguments/output.
3. Collapse works, and long output can be paged.
4. After a gateway restart, the old entry still expands and new calls produce new entries.

CLI discovery and passing local tests alone do not prove that live callbacks are attached. A plugin hot-reload success response is also insufficient: use a gateway restart and the checks above.

## Configuration

Settings live under `plugins.entries.discord-tool-inspector.settings` in the **active profile's** `config.yaml`. Prefer `hermes config set` to editing YAML by hand. All plugin settings below require a gateway restart to take effect.

| Setting | Default | Meaning |
| --- | --- | --- |
| `channels` | `[]` | List of channel/thread/DM IDs as strings. Empty disables capture and registration. `["*"]` explicitly enables all originating Discord conversations handled by that bot. |
| `owner_aliases` | `[]` | IDs belonging to one owner. Cross-account access requires **both** the original requester and clicking account to be in this list. It is one equivalence group, not a set of independent user pairs. |
| `admin_user_ids` | `[]` | Accounts allowed to operate any requester's retained entries, still only on the original message/channel and within the channel/retention policies. |
| `retention_days` | `30` | Non-negative integer. Local records expire by capture age; `0` disables expiry. This does **not** delete Discord messages. |

### Restrict capture to selected conversations

```sh
hermes config set plugins.entries.discord-tool-inspector.settings.channels '["CHANNEL_ID", "THREAD_ID"]'
```

Replace the placeholders with numeric IDs. Threads require their **own** ID; allowing a parent channel does not implicitly allow its threads. For a DM, use its conversation/channel ID, not the account ID.

### Capture in every originating conversation

```sh
hermes config set plugins.entries.discord-tool-inspector.settings.channels '["*"]'
```

The wildcard does not broadcast results, join channels, bypass Hermes's accepted-user policy, or grant Discord permissions. It can include shared channels and DMs: review that exposure before enabling it.

### Let two accounts operate their own shared history

```sh
hermes config set plugins.entries.discord-tool-inspector.settings.owner_aliases '["PRIMARY_USER_ID", "SECONDARY_USER_ID"]'
```

Use actual numeric account IDs. This does not grant access to unrelated users' entries.

### Give an administrator cross-requester controls

```sh
hermes config set plugins.entries.discord-tool-inspector.settings.admin_user_ids '["ADMIN_USER_ID"]'
```

This is distinct from owner aliases. Administrators cannot revive expired records, operate copied buttons on another message, bypass the channel setting, or gain Discord channel access. Removing an administrator and restarting revokes future controls; it does not hide already-expanded messages.

### Change retention

```sh
hermes config set plugins.entries.discord-tool-inspector.settings.retention_days 7
```

A shorter policy can expire existing records immediately on activation. `0` keeps records indefinitely. Cleanup runs at startup, on capture/read, and each idle hour in the existing publication worker—no cron or additional process. A stopped gateway catches up on startup. Old records without timestamps receive a one-time migration grace period, not a new grace period at every restart.

There are no current per-tool exclusions, per-user capture policies, custom button labels, or independent reasoning-summary toggle. For sensitive workflows, restrict the entire conversation or disable the plugin; button access and redaction are not substitutes for that boundary.

## How it works

| Component | Responsibility |
| --- | --- |
| `post_tool_call` | Captures the completed root-agent call's arguments and returned result independently of native progress display. |
| `post_api_request` | Captures explicit provider summary text, when supplied, using persistent request-ID deduplication. |
| Task-local route checks | Accept only bound root Discord turns; exclude CLI, cron, delegated children, and unbound process context. |
| Redaction and SQLite | Mask recognised credential fields/patterns before persistence; retain original message/channel ownership and capture age. |
| Native platform handler | Reuses the connected Discord client to publish Components V2 messages and handle persistent button IDs. |
| Publication queue | Orders captured entries without extra model calls; delivery may lag behind the final answer. |

Reasoning extraction only accepts `codex_reasoning_items[*].summary[*]` parts whose type is `summary_text`. Missing summaries or request IDs produce no reasoning entry. The plugin neither invents explanations nor associates a summary with a particular tool. It retains the full supplied summary for paging, but cannot establish that an upstream provider/transport supplied every possible summary block.

## Privacy and storage

- Database: `<HERMES_HOME>/plugin-data/discord-tool-inspector/calls.sqlite3`. On the tested POSIX environment the directory is mode `0700` and database `0600`. Do not commit or publish it.
- Credential-field masking and forced Hermes text redaction cover mapping keys as well as values. Redaction failure aborts capture rather than saving raw data.
- **Pattern-based redaction is not a universal secret detector.** Ordinary personal information, paths, document text, and unrecognised secrets can remain in previews, records, and expanded output. Trust the channel accordingly.
- Only the original requester, configured owner aliases, or explicit administrators may operate the exact original message. Expanded content is shared with all channel readers.
- Mentions are suppressed. Buttons identify records and pages, not executable commands.
- Symlink, hardlink, and database-replacement guards reject unsafe state paths. They are not a defence against a malicious process running as the same OS user.
- Retention is logical SQLite deletion, not forensic erasure. The file may not shrink, and backups may still hold old records. **Expiry never deletes Discord posts or already-expanded content.**

## Limits and troubleshooting

- **No entries:** check the active profile, enabled plugin, numeric channel/thread ID, and live gateway restart. The default empty allowlist deliberately registers no capture hooks. The bot must actually handle a root user turn there.
- **Unavailable/expired/another user:** verify requester/alias/admin identity, original message/channel, channel configuration, and retention age. A copied button is intentionally rejected.
- **Buttons time out after an update:** restart the gateway externally and check both old and new entries. Do not rely solely on hot-reload acknowledgements.
- **Missing tail of output:** only the result returned by the tool is captured. The plugin cannot recover material the tool itself truncated into another file.
- **Delayed or missing entries:** the SDK handles Discord rate limits, but busy turns can backlog. Look for `Inspector publication failed` in gateway logs. Redacted records are retained after delivery failure; unbound records are not automatically replayed because an ambiguous send could otherwise create duplicates.
- **Crash/reload during delivery:** queued publications may be abandoned; a send before database binding can leave a button unavailable. Disable the plugin while investigating. There is no claim of durable exactly-once delivery.
- **Retention failure:** look for `Inspector retention cleanup failed`. Cleanup pauses while the gateway is stopped.
- **Long-running sessions:** per-record interaction locks/arrival markers remain in memory until plugin unload; storage expiry does not evict those markers.
- **Rendering:** structured values have a readable JSON-like display, not a strict JSON export. Actual newlines are readable; literal escape sequences in command source remain literal. Embedded code fences split across pages without inserting hidden characters.
- **Rollback:** Discord's Components V2 conversion is irreversible per message. Keep a V2-capable handler for existing entries; pre-V2 releases cannot reliably operate converted messages.

## Update or disable

For an installation managed by the Hermes plugin installer:

```sh
hermes plugins update discord-tool-inspector
hermes gateway restart
```

Use the corresponding `--profile NAME` prefix for a named profile. Existing records remain subject to retention. Test an old button and a fresh capture after updating.

To disable:

```sh
hermes plugins disable discord-tool-inspector
hermes gateway restart
```

Existing messages and stored records remain; controls stop responding while the handler is absent. Restore your previous native `display.platforms.discord.tool_progress` setting if you changed it and want that output back.

For a manually linked development checkout, update the checkout with Git rather than treating it as an installer-managed copy. Disable first before removing an integration symlink; never delete the maintained source through that link.

## Development and tests

Clone the repository and run the standard-library unittest suite **inside a Hermes Python environment** with its source, Discord SDK, and dependencies available:

```sh
git clone https://github.com/Megarushing/hermes-discord-tool-inspector
cd hermes-discord-tool-inspector
PYTHONPATH=/path/to/hermes-agent:. /path/to/hermes-python -m unittest discover -s tests -v
hermes plugins doctor . --ci
```

`/path/to/hermes-python` means the interpreter belonging to your Hermes environment, not an arbitrary system Python. No `pip install` of this repository is required.

Tests cover redaction before disk/display, unsafe state paths, channel/owner/admin boundaries, retained callbacks, retention migration, ordering and client lifecycle, readable lossless pagination, and actual Discord SDK component serialization. Discord network transport is mocked. These tests do not claim to exercise human button clicks or every native gateway dispatch path.

Doctor with the default empty channel list may warn that declared hooks are not registered; that is the deliberate opt-in boundary. A configured smoke test is needed to check active registration.

## Licence

MIT — see [LICENSE](LICENSE).
