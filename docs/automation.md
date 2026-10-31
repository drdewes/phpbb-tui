# Read-only automation

The automation commands reuse the existing `Board` HTTP/session handling and
HTML parsers. They do not initialize curses, open an editor or browser, or ask
for missing input. Python 3.11+ and the existing `requests` dependency are
required. No additional runtime or test dependency is introduced.

For `post`, `reply`, and `edit`, see the [posting reference](posting.md).
Those commands share the output/session conventions here and add explicit
body input, dry-run, preview, and submission outcome handling.
The [PM reference](private-messages.md) covers mailbox reads, sending, replies,
quotes, and deletion with explicit confirmation.
The [attachment reference](attachments.md) covers listing, downloads, and uploads.

## Commands

```sh
phpbb-tui forums example
phpbb-tui topics example --forum 2
phpbb-tui topic example --topic 42
phpbb-tui search example --unread
```

Replace `example` with a configured board name, slug, or unambiguous name
substring. Matching is case-insensitive. Unknown or ambiguous names fail.
IDs and page numbers must be positive integers. Command options follow the
command name; `phpbb-tui topics --help` lists all options for that command.

| Option | Short form | Commands | Meaning |
| --- | --- | --- | --- |
| `--guest` | `-g` | All four | Use a temporary anonymous session |
| `--json` | `-j` | All four | Emit one JSON result on stdout |
| `--quiet` | `-q` | All four | Suppress normal output on success |
| `--page NUMBER` | `-p` | topics, topic, search | Request a page, starting at 1 |
| `--filter TEXT` | `-F` | forums, topics, search | Filter retrieved names/titles locally |
| `--forum ID` | `-f` | topics | Required forum ID |
| `--topic ID` | `-t` | topic | Required topic ID |
| `--query TEXT` | `-s` | search | Search through phpBB's existing keyword search |

For `search`, select exactly one of `--unread`, `--new`, `--mine`, `--active`,
`--unanswered`, or `--query TEXT`. The board may restrict or disable a search
mode. Guest access depends on board permissions; it does not bypass them.

`--filter` performs a case-insensitive substring match on forum names or topic
titles in the retrieved page, like the existing TUI filter. It does not run a
server search or fetch other pages. For topic lists, subforum entries remain
visible independently of the topic-title filter.

## Authentication and guest access

Authentication follows `login → stored session → normal commands`:

```sh
phpbb-tui login example
phpbb-tui topics example --forum 2 --json
```

Bare `login <board>` retains its existing interactive prompts. Explicit login
options enable unattended authentication; see the
[authentication reference](authentication.md) for `--user`, `--password`,
`--password-stdin`, JSON/quiet output, and credential handling. Other automation
commands reject credentials, never log in automatically, and return status 3 for a
missing or expired authenticated session. Establish the session before starting
an unattended job. Session cookies stay in the existing XDG state location;
they are credentials and should be protected accordingly.

```sh
phpbb-tui forums example --guest --json
```

Guest mode uses a temporary cookie file, never loads or changes the saved
authenticated cookie file, and removes the temporary session afterwards.
Authenticated reads may update the saved session as the existing client does.
Reading may also update phpBB's normal read/visit state; read-only describes
the absence of content submission, not an absence of server-side effects.

## Pagination

`--page` is one-based and defaults to 1. Later-page reads first fetch the first
page, derive the offset from phpBB's numeric pagination links, and then request
the selected page through the same `Board` operation. The CLI does not assume
a universal topics/posts-per-page setting or fetch every intervening page.
Inline topic links are excluded when navigating a forum list.

The response must confirm the selected page through its active-page marker or
English/German page-count text. Missing or inconsistent navigation fails
explicitly rather than silently returning another page. Pagination totals are
`null` when the response does not provide a recognized total; numeric links
alone do not establish a total. No fixed-size fallback is used.

## Output contract

Default text is for human readers; scripts should use `--json`. `--quiet` and
`--json` are mutually exclusive. Quiet mode still reports errors on stderr.
JSON mode writes one JSON object to stdout for success or expected failure,
with no banners, progress messages, or ANSI control sequences added by the CLI.

Every success object contains `success: true`, `action` (the command name), and
`board` (the configured display name), plus the command-specific fields:

| Command | Fields |
| --- | --- |
| forums | `forums` |
| topics | `forum_id`, `title`, `forums` (subforums), `topics`, `pagination` |
| topic | `topic_id`, `title`, `forum_id`, `posts`, `pagination` |
| search | `title`, `forums`, `topics`, `pagination` |

Collection fields are arrays, including when empty. IDs and counts are JSON
numbers; flags are booleans. Optional scalar data not supplied by the existing
parser is `null` unless the shared parser supplies a default:

- Forum: `id`, `name`, `description`, `category`, `level`.
- Topic: `id`, `title`, `author`, `replies`, `views`, `last_author`, `last_date`,
  `unread`, `sticky`, `forum`.
- Post: `id`, `author`, `date`, `subject`, `body`, `own`, `attachments`.
- Attachment metadata: `id`, `name`, `size` (source text), `image`.
- Pagination: `page`, `pages` (total or `null`), `start` (phpBB offset).

The existing parser defaults missing reply/view counts to zero, ownership to
false when the author cannot be matched, and an unparsed post author to `?`.
These reflect the shared client's current parsing limits, not additional
server verification. Dates and sizes preserve phpBB's source formatting; the
CLI does not invent timezones or byte counts. Post bodies use the client's
existing HTML-to-text conversion.

Example:

```json
{"success": true, "action": "forums", "board": "example", "forums": [{"id": 2, "name": "Support", "description": null, "category": null, "level": 0}]}
```

Expected errors have `success: false`, `error` (machine identifier), and
`message` (human explanation), with a nonzero exit status:

```json
{"success": false, "error": "authentication_required", "message": "Authentication required; run: phpbb-tui login example"}
```

## Exit statuses

These statuses apply to the new automation commands. Existing management/TUI
commands retain their current statuses.

| Status | Meaning / error identifiers |
| --- | --- |
| 0 | Success, including help |
| 2 | Invalid arguments or unknown/ambiguous board: `invalid_arguments`, `invalid_board` |
| 3 | Session required or expired: `authentication_required` |
| 4 | Other shared-client failure: `board_error` |
| 5 | Local configuration/session access: `configuration_error`, `local_error` |
| 7 | HTTP 404 or known page beyond total: `resource_not_found`, `page_not_found` |
| 8 | HTTP 403, unavailable content, unusable page navigation: `permission_denied`, `operation_unavailable`, `pagination_unavailable` |
| 9 | Network/other HTTP failure during reading: `network_error` |

Unexpected internal failures may still produce diagnostics on stderr. The
shared HTTP client retains its finite timeouts and single connection-error
retry for GET requests. These commands never submit content or issue a POST.
An HTTP-200 phpBB information/error response may not distinguish a nonexistent
resource from one hidden by permissions; those responses use status 8.

## Existing TUI commands and board-name collisions

`phpbb-tui`, `phpbb-tui <board>`, and the existing `add`, `list`, `login`,
`logout`, and `status` commands keep their established behavior. Their
interactive guest aliases (`--guest`, `--gast`) remain available.

A configured board named after an automation command still opens
interactively when invoked alone, optionally with an existing guest alias.
Automation is selected when additional command arguments are present:

```sh
phpbb-tui forums                 # TUI, when a matching board is configured
phpbb-tui tui forums --guest     # Explicit TUI launch
phpbb-tui forums forums --json   # List forums on that configured board
phpbb-tui forums --help          # Automation command help
```

`tui <board>` provides an explicit interactive path. The existing bare-board
invocation `phpbb-tui tui` still selects a configured board named `tui`.

## Unattended examples

After establishing a session, a cron job can save a JSON result:

```cron
0 2 * * * /home/alice/.local/bin/phpbb-tui topics example --forum 2 --json > /home/alice/reports/topics.json
```

A systemd oneshot service can check a board without normal success output:

```ini
[Unit]
Description=Check phpBB topic access

[Service]
Type=oneshot
ExecStart=/home/alice/.local/bin/phpbb-tui topics example --forum 2 --quiet
```

Run it as the user who owns the configured session, with the same XDG
configuration/state paths. Expired sessions cause failure instead of prompting.

## Tests and current scope

```sh
python3 -m unittest discover -s tests -v
```

The CLI tests use synthetic HTML and mocked requests to verify argument
validation, search dispatch, pagination, guest isolation, JSON/error handling,
and command-name compatibility. They do not require a public board account.

The read commands implement forums, topics, thread reading, and searches.
[Posting commands](posting.md) provide new topics, replies, quotes, edits,
preview, and dry-run. [PM commands](private-messages.md) cover mailbox reads,
sending, replies, quotes, and deletion. [Attachment commands](attachments.md)
provide scoped listing, safe downloads, and uploads where the form permits them.
