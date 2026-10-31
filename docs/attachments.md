# Attachment automation

Attachment commands reuse the existing Board authentication, parsing, download,
and multipart-upload behavior. They never open downloaded files or launch a
browser/editor. Stored-session authentication, JSON, quiet output, and expected
errors follow the [automation reference](automation.md).

## Listing

```sh
phpbb-tui attachments example --post 101 --json
phpbb-tui attachments example --message 42 --folder inbox --json
```

Specify exactly one `--post ID` (`-p`) or `--message ID` (`-m`). Post IDs work
across topic pages through phpBB's own post lookup. PMs require authentication;
their optional `--folder` (`-f`) defaults to `inbox` and accepts `sent`, `outbox`,
or the `sentbox` alias. Folder options apply only to PM context.

Results expose visible attachment IDs, filenames, source size text, and image
flags from the existing parser. An empty list means no attachments were visible
in that resource. Missing resources or unsupported response markup fail rather
than silently treating another post/PM as the selected resource.

## Downloading

```sh
phpbb-tui attachment example --attachment 7 --output ./downloads/
phpbb-tui attachment example --post 101 --attachment 7 --output ./chosen.png
phpbb-tui attachment example --message 42 --attachment 9 --output ./files/
```

`--attachment ID` (`-a`) is required. phpBB's file endpoint accepts the file ID
alone and enforces its own forum/PM permissions. Optional post/PM context also
verifies that the ID is visible in that specific resource before downloading.

`--output` (`-o`) selects a file or directory. An existing directory, or a path
ending in `/`, receives the server-provided filename. A new path without `/`
at the end is treated as a filename. Without `--output`, the existing configured
downloads directory is used. Missing directories can be created under a writable
ancestor. Server filenames are decoded and sanitized to stay in that directory;
an unavailable filename falls back to the existing attachment name or file ID.

Existing files are protected by default. `--force` (`-F`) permits replacing a
regular file after the complete transfer. Symlink targets and non-regular file
targets are rejected. A temporary file in the destination directory receives the
stream; failures leave an existing destination intact and clean up the temporary
file. Publishing without `--force` fails atomically if another file appeared
during the transfer. This uses filesystem hard-link support; unsupported
filesystems fail safely instead of weakening overwrite protection.

Downloads keep finite HTTP timeouts. HTML responses without an attachment
filename are rejected as possible login/error pages. JSON reports the saved
path and actual byte count only after successful publication. Downloaded files
are not automatically opened.

`--guest` (`-g`) permits anonymous post listing/downloads where phpBB allows
them, using an isolated temporary session. Explicit PM context rejects guest
mode. File-ID-only downloads still enforce the board's PM authorization rules.

## Uploading with posts and PMs

```sh
phpbb-tui post example --forum 2 --subject "Report" --body-file report.md --attach chart.png
phpbb-tui reply example --topic 42 --body "Files attached" --attach first.png --attach second.png
phpbb-tui edit example --post 101 --body-file replacement.md --attach extra.png
phpbb-tui pm example --to alice --subject "Report" --body "See attached" --attach chart.png
phpbb-tui pm-reply example --message 42 --body "An attachment" --attach extra.png
```

Repeat `--attach FILE` (`-A`) to select multiple files. Every file must be a
readable regular file before any avoidable network mutation. The form must
offer attachment upload; disabled attachments, permissions, extension policies,
file sizes, and per-message limits remain controlled by phpBB.

Each upload uses the existing multipart form process, retaining the returned
attachment fields for final submission. A failed or unconfirmed upload prevents
the final post/PM send. Existing attachments on an edited post are preserved
when adding new files. Uploads are separate requests: earlier successful uploads
can remain as orphan files after a later failure or preview, subject to phpBB's
normal cleanup. The workflow is not transactional.

`--dry-run` validates local files and whether the form offers attachments,
but does not upload them. It returns the requested filenames. PM dry-run may
still use non-final POSTs to resolve recipients. Dry-run cannot prove that a
file will pass server-side size/extension checks.

`--preview` uploads the selected files and requests phpBB's preview without
final content submission. Orphan upload files may remain for normal board
cleanup. Final sending still uses the existing single-attempt/uncertainty rules.

## Inline placement

Use `{{attach:ALIAS}}` in the body and repeat `--attach-inline ALIAS=FILE`
(`-I`) for each named upload:

```text
Before the image.

{{attach:mascot}}

After the image.
```

```sh
phpbb-tui reply example --topic 42 --body-file reply.md --attach-inline "mascot=phpBB Bear Mascot in Blue Polo.png"
```

This works with `post`, `reply`, `edit`, `pm`, and `pm-reply`. Aliases are
case-sensitive, start with an ASCII letter, and contain only letters, digits,
underscores, or hyphens. Each alias must be declared once and referenced in the
body; a placeholder may appear more than once. Split `ALIAS=FILE` at the first
`=`, so filenames may contain `=`. Unknown, malformed, duplicate, or unused
aliases fail with status 2 before network access. Unreadable files fail with
status 5.

Ordinary `--attach` files are uploaded first, followed by named uploads in their
option order. The client tracks each named upload by its server attachment ID
and resolves its final index after all uploads. Duplicate filenames and phpBB's
reordering therefore do not change which file an alias selects. Existing inline
attachment tags referring to attachments already present in an edit form are
remapped to their new positions when named uploads are added. Regular
`--attach`-only behavior and manually supplied tags remain available.

Dry-run checks the aliases, files, and form without uploading. Its `body_bbcode`
retains the placeholders because the final server indexes are not known yet;
it is not ready for manual submission. Preview uploads and resolves the tags
without publishing the final content, with the same orphan-upload caveat as
ordinary attachments. Ambiguous or missing returned attachment IDs stop final
submission with status 4. The `attachments` result lists all new filenames,
including both ordinary and named uploads. Board settings determine how images
and other file types are displayed inline.

## Inline placement in the TUI

After uploading a file with the existing `d` action, press `i` in the compose
preview to select an attachment. The current editor reopens with a placeholder
such as `{{attach:id123}}` appended to the body. Move it to the desired position,
then save and close the editor to preview. Only the existing `a` action sends.
Canceling the selection returns to preview without reopening the editor.

The same action is available for topics, replies, edits, and PMs where the
board permits attachments. Existing inline attachment tags are converted to
stable placeholders when editing. Placeholders follow server attachment IDs,
so adding files or reordering fields does not select a different attachment,
even when filenames are identical. The final BBCode uses the current index.

If a referenced file is removed, correct or remove its placeholder in the
editor before continuing. Missing or malformed placeholders prevent both
preview and final submission. Regular attachments and existing compose keys
remain available. Images still appear according to the board's rendering
settings; the TUI preview remains a text preview.

## Output and errors

Both read commands support `--json` (`-j`) or `--quiet` (`-q`), mutually exclusive.
The standard success envelope contains `success`, `action`, and `board`:

- `attachments`: context `post_id`, or `message_id` and canonical `folder`, plus
  `attachments` containing `id`, `name`, `size` (source text or `null`), and
  `image` (boolean).
- `attachment`: `attachment_id`, `path`, and actual `bytes`, plus resource
  context when supplied.
- Writing with `--attach`: adds `attachments`, an array of filenames newly
  selected/uploaded by this invocation. Existing edit attachments stay in the
  form but are not repeated in that new-files list.

Status 2 covers invalid combinations and IDs. Status 3 covers session failure.
Status 4 covers rejected/unconfirmed uploads; status 5 covers local file/output
failures, including an existing destination without `--force`. Status 7 covers
missing attachment IDs or context mismatches. Status 8 covers unavailable
content/permissions, unsafe download responses, or forms without upload support.
Status 9 covers network/HTTP failures during downloading or uploading. Failed
uploads never proceed to final sending, although orphan uploads may exist.

Once the final post/PM request begins, the existing status-10 uncertainty rules
apply. No final content submission is automatically retried. Hidden form fields,
session parameters, cookies, and raw response diagnostics are excluded from
normal/JSON output.

Run the offline suite with `python3 -m unittest discover -s tests -v`. Live
upload checks must use a controlled disposable board with suitable attachment
permissions; public forums should not receive uncontrolled test writes.
