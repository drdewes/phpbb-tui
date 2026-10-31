# Interactive and unattended login

Authentication follows `login → stored session → normal commands`. Login uses
the existing Board authentication forms, form-key timing, and XDG cookie
storage. Ordinary automation commands consume an established session and never
log in automatically. Expired sessions require an explicit new login.

## Existing interactive login

```sh
phpbb-tui login example
```

This keeps the existing username and hidden-password prompts. Bare `login`
without a board also keeps the existing board-selection behavior.

## Unattended login

```sh
phpbb-tui login example --user alice --password 'example-only-password' --json
```

Prefer redirected stdin when practical:

```sh
printf '%s' "$PASSWORD" | phpbb-tui login example --user alice --password-stdin
```

`PASSWORD` in this example is a shell variable supplied by the caller, not an
additional application configuration setting. Avoid tracing shell commands
containing credentials. `--password` values can be visible in shell history and
process listings. Interactive hidden-password entry remains available.

| Option | Short form | Meaning |
| --- | --- | --- |
| `--user USER` | `-u` | Override the configured `user` |
| `--password PASSWORD` | `-p` | Supply a password as an argument |
| `--password-stdin` | `-s` | Read a password from redirected stdin |
| `--json` | `-j` | Emit one success/error JSON object |
| `--quiet` | `-q` | Suppress successful output; keep errors on stderr |

Unattended login requires an explicit board and exactly one password
source. Missing information fails without prompting. If `--user` is omitted,
the board's configured `user` is used; an empty/missing username fails. Board
names, slugs, and unambiguous name substrings follow normal automation lookup.
`--json` and `--quiet` are mutually exclusive. Use `login --help` for help.

Stdin must be redirected and reach EOF. One trailing LF or CRLF is removed;
password spaces are preserved. Empty passwords, embedded line breaks/NUL, and
passwords exceeding 65,536 characters are rejected before network access.
Passwords are never written to application configuration or output. The shared
client stores only the session cookie, with mode `0600` after saving it.
Unattended login authenticates in a fresh temporary session and atomically
replaces the saved cookie only after success. Rejected credentials and network
failures leave an existing saved session intact; temporary files are cleaned up.

Success output contains `success`, `action: "login"`, `board`, and `username`.
Expected failures contain `success: false`, `error`, and `message`; raw board
errors, form fields, tokens, and passwords are excluded. Login submits once;
a failed final login request is not automatically retried.

| Status | Meaning |
| --- | --- |
| 0 | Successful authentication or help |
| 2 | Invalid arguments, board selection, or missing credentials |
| 3 | Authentication rejected or an additional board challenge is required |
| 4 | Other shared-client login failure |
| 5 | Local configuration, input, or cookie access failure |
| 9 | Network/HTTP failure |

CAPTCHA, external authentication, and other board challenges are not bypassed.
The existing interactive login remains available but does not gain new
challenge-solving capabilities. Credential options belong only to `login`;
commands such as `post`, `reply`, and `pm` reject them.
