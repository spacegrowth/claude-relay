# Privacy

relay runs entirely on your machine and collects nothing.

- **No telemetry, no analytics, no phoning home.** The plugin makes no network requests of its
  own. (The only network activity you'll ever see is what you or your Claude Code sessions
  explicitly run — e.g. `git push`.)
- **All state is local**: session records, work packets, reports, diff pages, and the event ledger
  live under `~/.relay-tasks/` on your machine. Delete that directory and every trace is gone
  (`relay prune` clears old entries selectively).
- **Desktop notifications** are posted locally via `terminal-notifier` or macOS's built-in
  notification facility; their content never leaves your machine.
- **Vendored assets** (see [VENDOR.md](VENDOR.md)) are checked into this repository and loaded
  from disk — no CDN or remote fetch at runtime.

Questions: open an issue at https://github.com/spacegrowth/claude-relay/issues.
