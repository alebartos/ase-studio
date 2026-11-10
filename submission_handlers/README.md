# Submission handlers

ASE Studio can hand the submission ZIP to an optional **submission handler**:
a separate program, chosen by the course or institution, that delivers it
(to a university portal, by e-mail, to a shared folder, ...). ASE Studio
provides the interface; the handler does the actual delivery. Without a
handler, nothing changes: the ZIP is saved in the submission folder.

## Enabling a handler

ASE Studio reads `submission_handler.json` from the project root (the folder
that contains `setup_default`), or the file named by the
`ASE_STUDIO_SUBMISSION_HANDLER` environment variable:

```json
{ "script": "ase_studio/submission_handlers/polito_portale.py" }
```

- `script`: a Python handler, run with ASE Studio's own Python.
- `command`: any other program, as a list (`["/path/to/handler", "--flag"]`).
- `enabled`: `false` turns the handler off without deleting the file.

Relative paths are resolved from the folder of the configuration file, which
is also the handler's working directory.

When a handler is configured, the submission dialog shows an **Upload the ZIP
to …** checkbox. It is unchecked by default, so students opt in. If the handler
can list submissions, a button in the toolbar opens that list.

## Protocol

ASE Studio runs the handler once per action, with the action as the last
argument, a JSON object on stdin and a JSON object expected on stdout:

```sh
handler ACTION < request.json > response.json
```

Every request contains:

- `session`: the value the handler last returned as `session`, or `null`.
  ASE Studio keeps it in memory only, so the handler stays stateless; a
  handler that remembers a sign-in across launches stores it itself (for
  example in the system keyring).
- `studio`: `{"version": "<ASE Studio version>"}`.

A response may contain `session` (stored and sent back with the next
request; `null` clears it). An error is reported as
`{"error": "message"}`; add `"signIn": true` when the session is missing or
expired, and ASE Studio asks the student to sign in and retries once.

| Action | Request | Response |
| --- | --- | --- |
| `describe` | – | `name`, `description`, `experimental` (shows a warning), `website` (offered after a manual save), `signIn` (or `null`): `{title, note, fields: [{id, label, type: "text"\|"password", placeholder}], remember}`, `list` (or `null`): `{label, withdraw}` |
| `status` | – | `signedIn`, `account`, `destination` |
| `signIn` | `fields` (values by field id), `remember` | as `status` |
| `signOut` | – | `{"signedIn": false}` |
| `preview` | `archive` | `destination`, `existing`: items with the same file name already delivered |
| `upload` | `archive`, `description`, `replace` (`true`, `false`, or `null` to report duplicates) | `message`, `warnings`; or `conflict: true` with `existing` |
| `list` | – | `destination`, `items` |
| `withdraw` | `id`, `withdrawn` | as `list` |

`archive` is `{path, filename, stem, size}`. ASE Studio only passes ZIPs that
it created in the submission folder. An item is
`{id, name, description, date, sizeKiB, url, withdrawn, withdrawnAt}`.

Only `describe`, `status` and `upload` are required. A handler that needs no
sign-in returns `signIn: null`; one that cannot list submissions returns
`list: null`.

## Example: Politecnico di Torino

`polito_portale.py` uploads to the Elaborati of *Architetture dei sistemi di
elaborazione* (or *Computer architectures*) on the Portale della Didattica,
using the API of the official PoliTO Students app. It is **experimental**:
the course organizers and the PoliTO Area IT should confirm that this use of
the API is appropriate before students rely on it. The password is never
stored; "Keep me signed in" keeps only the session token, in the macOS
Keychain, the Secret Service keyring, or a file readable only by the user.
