# Optional integrations

All collectors below are disabled by default. Enable them in the local TOML, which is excluded from Git. `~` is expanded for configured paths. No collector mounts storage, starts sync, or sends messages.

## LLM quota

Set `llm_enabled = true` after installing and authenticating an external reader, such as an existing `ai-usagebar` installation. The default command is `ai-usagebar usage --json`. A compatible reader can be configured as an argument array:

```toml
llm_enabled = true
llm_command = ["/path/to/quota-reader", "--json"]
```

The command must print a JSON object with this structure (values shown are fictional):

```json
{
  "entries": [{
    "id": "demo",
    "display_name": "Example",
    "status": "ready",
    "stale": false,
    "fetched_at": "2026-01-01T12:00:00Z",
    "metrics": [{
      "label": "Weekly quota",
      "headline": "percent",
      "percent": 34,
      "reset_at": "2026-01-08T12:00:00Z",
      "window_secs": 604800
    }]
  }]
}
```

Use the actual source observation timestamp in `fetched_at`, not the command execution timestamp of cached data. Readings over ten minutes old, explicitly stale readings, missing timestamps and provider errors do not become new history observations. Percentages must be numeric and between 0 and 100. The exact `window_secs` is optional; without it no elapsed-window marker is shown.

The reader manages its own login. Do not put tokens/passwords in command arguments or in the example configuration.

## NAS

```toml
# Documentation address only: replace with your own NAS hostname/IP.
nas_host = "192.0.2.10"
nas_port = 445
nas_mounts = ["/mnt/nas/documents", "/mnt/nas/projects"]
```

The monitor checks a TCP connection to the configured SMB port with a two-second timeout and counts existing CIFS/NFS/NFS4 mountpoints. Authentication, SMB share contents and storage health are not inspected.

## Sync service / Unison

```toml
sync_service = "my-sync.service"
sync_mount = "/mnt/nas/projects"
sync_log = "~/.local/state/my-sync/sync.log"
```

This integration expects an existing **systemd user service**, a required CIFS/NFS mount and optionally an English Unison log. It only reads their state. An active/activating service is RODANDO; an absent required mount is ADIADO. A failed service is ERRO. Unison skipped/failed items produce PARCIAL/ERRO instead of OK.

The last OK timestamp requires a dated log header followed by a complete summary with zero skipped and zero failed items. Up to 2,000 summaries are searched backwards without copying the whole log; a missing confirmed date shows `--`. The parsed result is reused until inode, size or modification time changes. This is a conservative log-completion indicator, not a file-integrity audit.

## VPN status

```toml
vpn_status_command = ["/path/to/vpn-status", "--json"]
```

The command must print `{"class":"active"}` or `{"class":"inactive"}`. A missing command, failed command or unknown status produces `--`. Arguments are executed directly without a shell; shell operators are not supported.

## Tailscale

```toml
tailscale_enabled = true
```

Reads `tailscale status --json`. ONLINE requires `BackendState=Running` and `Self.Online=true`. Missing/invalid JSON is unavailable. This does not modify Tailscale routes or settings.

## T3 turn counters

```toml
agents_enabled = true
agents_db = "~/.t3/userdata/state.sqlite"
```

The database is opened in read-only mode with SQLite `query_only`. Only selected state/counter columns from `projection_threads`, `projection_thread_sessions` and `projection_turns` are queried. Titles, messages, prompts and event payloads are not read.

- ATIVOS: running/starting sessions with an active turn, excluding threads waiting for input/approval/plan.
- ESPERA: open threads with a pending approval, user input or actionable plan.
- OK HOJE: completed turns since local midnight, not completed projects/tasks.

Archived/deleted threads are excluded from active/waiting counts. Deleted threads are excluded from completed-turn counts. The internal T3 schema can change; incompatible or missing databases show unavailable rather than zero and are not created by the monitor.
