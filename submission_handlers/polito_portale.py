#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""ASE Studio submission handler for the Politecnico di Torino.

Uploads the submission ZIP to the Elaborati of the course on the Portale della
Didattica, through the API of the official PoliTO Students app
(app.didattica.polito.it/api, spec in github.com/polito/api-spec). It is an
example of the protocol described in README.md and is experimental: the
course organizers and the PoliTO Area IT should confirm that using this API
is appropriate before students rely on it.

Usage: polito_portale.py ACTION < request.json > response.json
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

API = os.environ.get("ASE_STUDIO_PORTAL_API",
                     "https://app.didattica.polito.it/api").rstrip("/")
REQUEST_TIMEOUT = 30
UPLOAD_TIMEOUT = 300
# Architetture dei sistemi di elaborazione (02GOL..) or its English edition,
# Computer architectures (02LSE..). The course id changes every academic year
# and the last two letters depend on the degree programme.
COURSE_CODES = tuple(
    code.strip().upper()[:5] for code in
    os.environ.get("ASE_STUDIO_PORTAL_COURSE", "02GOL,02LSE").split(",") if code.strip())
DEVICE = {"platform": "linux", "toothPicCompatible": False}
KEYRING_SERVICE = "ASE Studio Portale"
KEYRING_ACCOUNT = "portal-session"


# What happened during the current action, reported with any error so that a
# failure can be diagnosed from the student's message: API calls and their
# HTTP status, careers and courses found. Never passwords or tokens.
TRACE: list[str] = []


def trace(line: str) -> None:
    TRACE.append(line)


class HandlerError(Exception):
    def __init__(self, message, sign_in=False, details=True):
        super().__init__(message)
        self.sign_in = sign_in
        self.details = details and not sign_in


# --------------------------------------------------------------- remembered session
# The password is never stored. With "Keep me signed in", only the API token
# is kept, in the system keyring or else in a file only the user can read.
def credentials_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ase-studio" / "portal.json"


def keyring() -> str | None:
    # ASE_STUDIO_KEYRING=none keeps tests away from the user's real keyring.
    if os.environ.get("ASE_STUDIO_KEYRING") == "none":
        return None
    if sys.platform == "darwin" and os.access("/usr/bin/security", os.X_OK):
        return "macOS Keychain"
    if shutil.which("secret-tool") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return "system keyring"
    return None


def keyring_run(arguments, stdin=""):
    try:
        result = subprocess.run(arguments, input=stdin, capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def keyring_store(secret: str) -> bool:
    # Base64 keeps the secret free of quotes and off the command line.
    encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
    if keyring() == "macOS Keychain":
        return keyring_run(["/usr/bin/security", "-i"],
                           f'add-generic-password -U -s "{KEYRING_SERVICE}" '
                           f"-a {KEYRING_ACCOUNT} -w {encoded}\n") is not None
    if keyring() == "system keyring":
        return keyring_run(["secret-tool", "store", f"--label={KEYRING_SERVICE}",
                            "service", KEYRING_SERVICE, "account", KEYRING_ACCOUNT],
                           encoded) is not None
    return False


def keyring_load() -> str | None:
    if keyring() == "macOS Keychain":
        output = keyring_run(["/usr/bin/security", "find-generic-password", "-s",
                              KEYRING_SERVICE, "-a", KEYRING_ACCOUNT, "-w"])
    elif keyring() == "system keyring":
        output = keyring_run(["secret-tool", "lookup", "service", KEYRING_SERVICE,
                              "account", KEYRING_ACCOUNT])
    else:
        output = None
    if not output:
        return None
    try:
        return base64.b64decode(output.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def keyring_delete() -> None:
    if keyring() == "macOS Keychain":
        keyring_run(["/usr/bin/security", "delete-generic-password", "-s",
                     KEYRING_SERVICE, "-a", KEYRING_ACCOUNT])
    elif keyring() == "system keyring":
        keyring_run(["secret-tool", "clear", "service", KEYRING_SERVICE,
                     "account", KEYRING_ACCOUNT])


def remember(session: dict) -> bool:
    secret = json.dumps({key: session.get(key) for key in ("username", "token", "clientId")})
    if keyring_store(secret):
        forget_file()
        return True
    path = credentials_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary = path.with_name(path.name + ".tmp")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(secret)
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def forget_file() -> None:
    try:
        credentials_path().unlink()
    except OSError:
        pass


def forget() -> None:
    keyring_delete()
    forget_file()


def remembered() -> dict | None:
    raw = keyring_load()
    if raw is None:
        try:
            raw = credentials_path().read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        stored = json.loads(raw)
    except ValueError:
        return None
    if (isinstance(stored, dict) and isinstance(stored.get("token"), str)
            and stored["token"] and isinstance(stored.get("username"), str)):
        return {"username": stored["username"], "token": stored["token"],
                "clientId": stored.get("clientId"), "remembered": True}
    return None


# --------------------------------------------------------------- API
def error_message(raw: bytes) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return message.strip()[:300] if isinstance(message, str) else ""


def api(method, path, token=None, body=None, content_type=None,
        missing_ok=False, timeout=REQUEST_TIMEOUT, version=""):
    headers = {"Accept": "application/json", "User-Agent": f"ASE-Studio/{version}"}
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
        content_type = "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(API + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            trace(f"{method} {path} -> HTTP {response.status}")
    except urllib.error.HTTPError as error:
        try:
            message = error_message(error.read())
        except OSError:
            message = ""
        trace(f"{method} {path} -> HTTP {error.code}" + (f" ({message})" if message else ""))
        if missing_ok and error.code == 404:
            return None
        if token and error.code in {401, 403}:
            raise HandlerError("Your Portale della Didattica session has expired. "
                               "Sign in again.", sign_in=True)
        if not token and 400 <= error.code < 500:
            raise HandlerError(message or "The Portale della Didattica rejected the "
                                          f"sign-in (HTTP {error.code}).", details=False)
        raise HandlerError(message or f"The Portale della Didattica returned HTTP {error.code}.")
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", None) or error
        trace(f"{method} {path} -> not reachable ({reason})")
        raise HandlerError(f"The Portale della Didattica could not be reached: {reason}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise HandlerError("The Portale della Didattica returned an unexpected response.")


def build_number(version: str) -> str:
    parts = [int(part) for part in re.findall(r"\d+", version)[:3]]
    parts += [0] * (3 - len(parts))
    return "".join(f"{min(part, 99):02d}" for part in parts)


# --------------------------------------------------------------- handler
class Portale:
    def __init__(self, request: dict):
        self.request = request
        self.version = str((request.get("studio") or {}).get("version") or "")
        session = request.get("session")
        self.session = session if isinstance(session, dict) and session.get("token") else None
        if self.session is None:
            self.session = remembered()
        self.changed = self.session is not None and request.get("session") is None

    def call(self, *args, **kwargs):
        return api(*args, version=self.version, **kwargs)

    def token(self) -> str:
        if not self.session:
            raise HandlerError("Sign in to the Portale della Didattica first.", sign_in=True)
        return self.session["token"]

    def reply(self, response: dict) -> dict:
        if self.changed:
            response["session"] = self.session
        return response

    def drop_session(self) -> None:
        if self.session and self.session.get("remembered"):
            forget()
        self.session = None
        self.changed = True

    # The course is looked up once per session.
    def client(self) -> dict:
        client = {"name": "ASE Studio", "appVersion": self.version,
                  "buildNumber": build_number(self.version)}
        if self.session and self.session.get("clientId"):
            client["id"] = self.session["clientId"]
        return client

    def course_of_career(self, token: str) -> dict | None:
        """The course in the career the token belongs to, newest year first."""
        response = self.call("GET", "/v2/courses", token, missing_ok=True)
        if response is None:
            response = self.call("GET", "/courses", token)
        courses = []
        for course in response.get("data") or []:
            if not isinstance(course, dict):
                continue
            for entry in [course] + [m for m in course.get("modules") or [] if isinstance(m, dict)]:
                if isinstance(entry.get("id"), int) and not isinstance(entry.get("id"), bool):
                    courses.append({
                        "id": entry["id"],
                        "name": str(entry.get("name") or course.get("name") or entry["id"]),
                        "shortcode": str(entry.get("shortcode") or course.get("shortcode") or ""),
                        "year": entry.get("year") or course.get("year"),
                    })
        listed = ", ".join(f"{c['name']} ({c['shortcode'] or c['id']}, {c['year']})"
                           for c in courses[:40])
        trace(f"Courses of career {self.session.get('username', '?')}: {listed or 'none'}"
              + (f" and {len(courses) - 40} more" if len(courses) > 40 else ""))
        matches = [c for c in courses if c["shortcode"].upper()[:5] in COURSE_CODES]
        return max(matches, key=lambda c: (c["year"] or 0, c["id"])) if matches else None

    def switch_career(self, username: str) -> None:
        response = self.call("POST", "/auth/switch-career", self.token(), {
            "username": username, "client": self.client(), "device": DEVICE})
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, dict) and isinstance(data.get("token"), str) and data["token"]:
            self.session["token"] = data["token"]
        self.session["username"] = str((data or {}).get("username") or username)
        trace(f"Switched to career {self.session['username']}")
        self.changed = True

    # A student can have several careers (e.g. bachelor's and master's), each
    # with its own username; sign-in opens the default one, which may not be
    # the one that includes the course. The course is looked up once per
    # session, switching to the career that has it.
    def course(self) -> dict:
        token = self.token()
        if self.session.get("course"):
            return self.session["course"]
        course = self.course_of_career(token)
        if course is None:
            profile = self.call("GET", "/auth/profile", token, missing_ok=True) or {}
            data = profile.get("data") if isinstance(profile.get("data"), dict) else {}
            original = str(data.get("username") or self.session.get("username") or "").lower()
            others = [str(name).lower() for name in data.get("allUsernames") or []
                      if isinstance(name, str) and str(name).lower() != original]
            trace(f"Course codes searched: {', '.join(COURSE_CODES)}; careers of the account: "
                  f"{', '.join(data.get('allUsernames') or []) or 'unknown'} (current: {original or '?'})")
            for username in others:
                self.switch_career(username)
                course = self.course_of_career(self.session["token"])
                if course is not None:
                    break
            if course is None:
                if others and original:
                    self.switch_career(original)
                raise HandlerError("None of the careers of your PoliTO account includes "
                                   "Architetture dei sistemi di elaborazione or Computer "
                                   "architectures on the Portale della Didattica.")
            if self.session.get("remembered"):
                remember(self.session)
        self.session["course"] = course
        self.changed = True
        return course

    def destination(self) -> str:
        course = self.course()
        return f"{course['name']} · {course['shortcode']}" if course["shortcode"] else course["name"]

    def elaborati(self) -> list[dict]:
        response = self.call("GET", f"/courses/{self.course()['id']}/assignments", self.token())
        entries = [e for e in response.get("data") or [] if isinstance(e, dict)]
        return sorted(entries, key=lambda e: str(e.get("uploadedAt") or ""), reverse=True)

    @staticmethod
    def item(entry: dict) -> dict:
        return {"id": str(entry.get("id")), "name": entry.get("filename") or "",
                "description": entry.get("description") or "",
                "date": entry.get("uploadedAt"), "sizeKiB": entry.get("sizeInKiloBytes"),
                "url": entry.get("url"), "withdrawn": bool(entry.get("deletedAt")),
                "withdrawnAt": entry.get("deletedAt")}

    def withdraw(self, assignment_id, withdrawn: bool) -> None:
        deleted_at = (datetime.now(timezone.utc).isoformat(timespec="seconds")
                      .replace("+00:00", "Z") if withdrawn else None)
        self.call("PATCH", f"/courses/{self.course()['id']}/assignments/{assignment_id}",
                  self.token(), {"deletedAt": deleted_at})

    # ---- actions
    def describe(self) -> dict:
        store = keyring()
        return {
            "name": "Portale della Didattica",
            "experimental": True,
            "description": "Upload the ZIP to the Elaborati of Architetture dei sistemi di "
                           "elaborazione (or Computer architectures) on the Portale della "
                           "Didattica of the Politecnico di Torino.",
            "signIn": {
                "title": "Sign in to the Portale della Didattica",
                "note": "Use your PoliTO credentials. Your password is never stored.",
                "fields": [
                    {"id": "username", "label": "Username", "placeholder": "s123456"},
                    {"id": "password", "label": "Password", "type": "password"},
                ],
                "remember": f"stored in the {store}" if store else "stored in a file only you can read",
            },
            "list": {"label": "Elaborati", "withdraw": True},
            "website": "https://didattica.polito.it/",
        }

    def status(self) -> dict:
        if not self.session:
            return self.reply({"signedIn": False})
        response = {"signedIn": True}
        try:
            # Looking up the course may switch to another career first.
            response["destination"] = self.destination()
        except HandlerError as error:
            if error.sign_in:
                raise
            response["destinationError"] = with_details(str(error), "status")
        response.update(account=self.session.get("username", ""),
                        remembered=bool(self.session.get("remembered")))
        return self.reply(response)

    def signIn(self) -> dict:
        fields = self.request.get("fields") or {}
        username = str(fields.get("username") or "").strip()
        password = str(fields.get("password") or "")
        if not username or len(username) > 64:
            raise HandlerError("Enter your PoliTO username.", details=False)
        if not password or len(password) > 256:
            raise HandlerError("Enter your password.", details=False)
        response = self.call("POST", "/auth/login", body={
            "loginType": "basic", "username": username, "password": password,
            "preferences": {"language": "en"}, "client": self.client(), "device": DEVICE,
        })
        data = response.get("data") if isinstance(response, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise HandlerError("The Portale della Didattica did not return a session.")
        if data.get("type") != "student":
            try:
                self.call("DELETE", "/auth/logout", token)
            except HandlerError:
                pass
            raise HandlerError("Only student accounts can upload Elaborati.")
        client_id = data.get("clientId")
        self.session = {"username": str(data.get("username") or username), "token": token,
                        "clientId": client_id if isinstance(client_id, str) else None}
        forget()
        if self.request.get("remember") is True:
            self.session["remembered"] = remember(self.session)
        self.changed = True
        return self.status()

    def signOut(self) -> dict:
        if self.session:
            try:
                self.call("DELETE", "/auth/logout", self.session["token"])
            except HandlerError:
                pass
        forget()
        self.session = None
        self.changed = True
        return self.reply({"signedIn": False})

    def preview(self) -> dict:
        archive = self.request.get("archive") or {}
        existing = [self.item(e) for e in self.elaborati()
                    if e.get("filename") == archive.get("filename") and not e.get("deletedAt")]
        return self.reply({"destination": self.destination(), "existing": existing})

    def upload(self) -> dict:
        archive = self.request.get("archive") or {}
        path = Path(str(archive.get("path") or ""))
        if not path.is_file():
            raise HandlerError("The submission ZIP could not be found.")
        description = str(self.request.get("description") or path.stem).strip()
        if not description or len(description) > 200 or any(ord(c) < 32 for c in description):
            raise HandlerError("Invalid Elaborato description.")
        replace = self.request.get("replace")
        course_id = self.course()["id"]
        existing = self.elaborati()
        duplicates = [e for e in existing if e.get("filename") == path.name and not e.get("deletedAt")]
        if duplicates and replace is None:
            return self.reply({"conflict": True, "existing": [self.item(e) for e in duplicates]})

        boundary = f"----ASEStudio{uuid.uuid4().hex}"
        quoted = path.name.replace("\\", "\\\\").replace('"', '\\"')
        body = b"".join([
            (f'--{boundary}\r\nContent-Disposition: form-data; name="description"\r\n\r\n'
             f"{description}\r\n").encode("utf-8"),
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             f'filename="{quoted}"\r\nContent-Type: application/zip\r\n\r\n').encode("utf-8"),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ])
        self.call("POST", f"/courses/{course_id}/assignments", self.token(), body,
                  f"multipart/form-data; boundary={boundary}", timeout=UPLOAD_TIMEOUT)

        warnings = []
        uploaded = None
        try:
            known = {e.get("id") for e in existing}
            candidates = [e for e in self.elaborati()
                          if e.get("filename") == path.name and not e.get("deletedAt")
                          and e.get("id") not in known]
            if candidates:
                uploaded = max(candidates, key=lambda e: e.get("id") or 0)
            else:
                warnings.append("The upload was accepted but does not appear in the "
                                "course's Elaborati yet.")
        except HandlerError as error:
            warnings.append(f"The upload was accepted but could not be verified: {error}")
        withdrawn = 0
        if replace and duplicates:
            if uploaded is None:
                warnings.append("The previous copies were kept because the new upload "
                                "could not be confirmed.")
            else:
                for entry in duplicates:
                    try:
                        self.withdraw(entry["id"], True)
                        withdrawn += 1
                    except HandlerError as error:
                        warnings.append(f"Elaborato {entry['id']} could not be withdrawn: {error}")
        message = (f"Uploaded to Elaborati: {uploaded.get('filename')} "
                   f"({uploaded.get('sizeInKiloBytes')} KiB, {uploaded.get('uploadedAt')})."
                   if uploaded else f"Uploaded {path.name} to Elaborati.")
        if withdrawn:
            message += f"\nWithdrew {withdrawn} previous copy(ies)."
        return self.reply({"message": message, "warnings": warnings,
                           "item": self.item(uploaded) if uploaded else None})

    def list(self) -> dict:
        return self.reply({"destination": self.destination(),
                           "items": [self.item(e) for e in self.elaborati()]})

    def withdraw_action(self) -> dict:
        item_id = str(self.request.get("id") or "")
        withdrawn = self.request.get("withdrawn")
        if not item_id.isdigit() or not isinstance(withdrawn, bool):
            raise HandlerError("Invalid Elaborato action.")
        self.withdraw(int(item_id), withdrawn)
        return self.list()


ACTIONS = {"describe": Portale.describe, "status": Portale.status, "signIn": Portale.signIn,
           "signOut": Portale.signOut, "preview": Portale.preview, "upload": Portale.upload,
           "list": Portale.list, "withdraw": Portale.withdraw_action}


def log_path() -> Path:
    return credentials_path().parent / "polito_portale.log"


def with_details(message: str, action: str) -> str:
    """Append the trace of the current action to an error, and save it to the log."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    lines = [f"Details ({stamp}, action {action}, API {API}, "
             f"handler on Python {sys.version.split()[0]}):"]
    lines += [f"  {line}" for line in TRACE] or ["  (no request was sent)"]
    try:
        path = log_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            log.write(f"ERROR: {message}\n" + "\n".join(lines) + "\n\n")
        lines.append(f"Saved in {path}")
    except OSError:
        pass
    return message + "\n\n" + "\n".join(lines)


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        request = json.loads(sys.stdin.read() or "{}")
        if not isinstance(request, dict):
            raise ValueError
    except ValueError:
        request = {}
    handler = Portale(request)
    try:
        if action not in ACTIONS:
            raise HandlerError(f"Unknown action: {action or '(none)'}")
        response = ACTIONS[action](handler)
    except HandlerError as error:
        if error.sign_in:
            handler.drop_session()
        message = with_details(str(error), action) if error.details else str(error)
        response = handler.reply({"error": message, "signIn": error.sign_in})
    json.dump(response, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
