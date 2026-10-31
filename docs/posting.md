# Posting without the TUI

`post`, `reply`, and `edit` reuse the existing Board forms, Markdown/BBCode
converters, form-key timing, and submission requests. These commands consume
an existing authenticated session and never launch curses, an editor, a
browser, or an interactive confirmation. Run `phpbb-tui login example` first.

## Commands

Create a topic:

```sh
phpbb-tui post example --forum 2 --subject "Report" --body-file report.md
```

Reply, optionally quoting a post through phpBB's quote form:

```sh
phpbb-tui reply example --topic 42 --body "A reply"
phpbb-tui reply example --topic 42 --quote 101 --body-file response.md
```

Edit an editable post:

```sh
phpbb-tui edit example --post 101 --body-file replacement.md
phpbb-tui edit example --post 101 --subject "Corrected subject" --body-file replacement.md
```

An edit body is the complete replacement, never an append or patch. An omitted
subject preserves the form's current subject for edits and replies. A supplied
subject must not be blank. New topics require `--subject`; a Markdown heading
in the body does not supply it. phpBB still enforces target permissions and
posting restrictions.

Quote replies convert the server-supplied BBCode quote to Markdown, append the
supplied response, and run the same Markdown-to-BBCode conversion as the TUI.
The existing converter's formatting and quote-metadata behavior also applies.

## Body input

Select at most one explicit source:

- `--body TEXT` (`-b`): inline text.
- `--body-file FILE` (`-B`): a UTF-8 file.
- `--body-file -`: piped stdin.

With no explicit source, piped stdin is used automatically:

```sh
generate-report | phpbb-tui reply example --topic 42
```

Empty bodies, conflicting sources, unreadable files, invalid UTF-8 files, and
missing required IDs fail before network access. With a terminal on stdin,
missing input (including `--body-file -`) fails instead of prompting or
waiting. Body text is Markdown, converted by the shared converter.

## Dry-run and preview

```sh
phpbb-tui post example --forum 2 --subject "Report" --body-file report.md --dry-run --json
phpbb-tui reply example --topic 42 --body-file response.md --preview --json
```

`--dry-run` (`-n`) validates local input, the stored session, and availability
of the target posting form. It performs GET requests but no posting or preview
POST. It returns the resolved subject and converted BBCode without hidden
fields, cookies, or tokens. It does not prove that phpBB will accept the final
content: server content validation happens during preview/submission.

`--preview` (`-v`) sends phpBB's existing preview POST and returns rendered
plain text through the existing preview converter. It never performs the
final submission. Missing previews and explicit phpBB errors fail. Preview
and dry-run are mutually exclusive.

Both modes may update ordinary session/visit state. Neither intentionally
creates or edits a persistent post. Repeated `--attach FILE` options add files
where permitted; see the [attachment reference](attachments.md) for upload
validation, dry-run, preview, named inline placement, and multi-request
behavior. The existing
interactive attachment workflow is unchanged.

## Output and exit statuses

`--json` (`-j`) and `--quiet` (`-q`) follow the
[automation output contract](automation.md#output-contract). Quiet mode
suppresses successful output and still reports errors. Guest access and
credential flags are not accepted by posting commands.

Success objects contain `success`, `action`, `board`, `subject`, and
`submitted`. The target `forum_id`, `topic_id`, or `post_id` is included where
known. A confirmed submission has `submitted: true`; IDs and a canonical
`url` are added when obtained from the confirmation. URLs exclude session
parameters. Missing identifiers/URLs are omitted rather than invented.

Dry-run adds `dry_run: true` and `body_bbcode`; preview adds `preview: true`
and `preview_text`. Both have `submitted: false`.

```json
{"success": true, "action": "reply", "board": "example", "subject": "Re: Report", "submitted": true, "topic_id": 42, "post_id": 102, "url": "https://example.org/viewtopic.php?p=102#p102"}
```

Expected failures retain `success: false`, `error`, and `message`. In addition
to argument/session/configuration statuses from the read commands:

| Status | Meaning / error identifiers |
| --- | --- |
| 4 | Explicit phpBB content rejection, returned posting form, or HTTP 4xx during submission: `submission_rejected` |
| 5 | Unreadable or non-UTF-8 body file: `local_input_error` |
| 8 | No authorized posting/quote form or missing preview: `operation_unavailable`, `preview_unavailable` |
| 9 | Failure before final submission, a connection timeout before connecting, or failed preview: `network_error`, `preview_failed` |
| 10 | Final submission may have completed, but confirmation is unavailable: `submission_uncertain` |

Final POST requests are sent once. A connection interruption, read timeout,
server error, session-file failure after posting, or unrecognized final
response can mean the post was stored. In those cases, status 10 tells the
caller to inspect the board before retrying. Never automatically retry status
10: that could duplicate content. Expected error output does not echo raw
responses or request exceptions, and the CLI does not save raw diagnostic
HTML containing form/session data. The TUI's existing diagnostics remain
unchanged.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Posting tests mock HTTP while exercising the real CLI, forms, converter, and
submission code. They cover body sources, replacement editing, quote handling,
dry-run/preview separation, rejection, session failures, and uncertain writes
without automatic retries. Live-board checks must use a controlled disposable
board: these commands can create and modify real content.
