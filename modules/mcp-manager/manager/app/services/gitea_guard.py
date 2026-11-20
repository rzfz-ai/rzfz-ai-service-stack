# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The gateway's Gitea guard (#2554; design record
docs/superpowers/specs/2026-10-mcp-rework-design.md §9, ruling C1).

gitea-mcp 1.7.0 commits to the repository's DEFAULT branch when a file write
gets no `branch_name`: the handler reads `branchName, _ :=
args["branch_name"].(string)`, so a missing, empty or non-string value
becomes "" and Gitea writes the default branch (gitea-mcp#282 fixes it on
main, in no release yet). Every knowledge-repo write must be a branch, then a
PR, so the gateway enforces the rule itself, per tool, before the upstream:

  1. The tool must be one of the pinned version's tools (TOOLS, read from its
     tool registry): an unknown tool is refused, whatever it is called.
  2. Arguments are matched EXACTLY as gitea-mcp reads them: it decodes
     `arguments` with Go's encoding/json into a map[string]any and reads keys
     with exact, case-sensitive map lookups (`args["branch_name"]`). So a key
     that differs from a schema property only in case (`Branch_Name`) is a
     different key to gitea-mcp — refused for every tool, since the caller
     cannot have meant anything good by it. For a write-capable tool every
     argument must be a property of its schema (so `new_branch_name` on
     `delete_file` is refused). `output_path` is refused everywhere: it
     writes a file into the ONE container every caller shares.
  3. Branch-content writes (WRITE_RULES) name their target branch per tool;
     an empty, missing or non-string target is refused.
  4. The repository's default branch is read from Gitea with the CALLER'S
     own injected credential (`GET /api/v1/repos/{owner}/{repo}`); any
     failure — a non-200, a transport error, a body that is not a repository
     with a non-empty `default_branch` — refuses the write (no lookup, no
     write).
  5. The target must not be the default branch. Compared after removing a
     `refs/heads/` prefix and case-folding: refusing a spelling Gitea might
     still map to the default branch is the safe direction.

`pull_request_write`: `merge` is refused for every technical caller (a
technical user opens PRs and never merges them), `force_merge` other than
false is refused for everyone; a person's merge is allowed (the PR is the
review step). `update_branch` merges the base into the PR's head: the head,
read from Gitea by `pull_number` with the caller's credential, must not be a
default branch (fail closed when it cannot be read). The other methods write
no branch.

§9 extension (controller ruling, fix round 1 of #2554): three write tools
that §9's table does not name are refused, every method, for technical
callers, the same split as `merge`: `pull_request_review_write` (a technical
user's APPROVE would satisfy the "approved by someone other than the author"
branch protection that §9 names as the backstop), `actions_run_write`
(workflow dispatch/rerun runs repository code wherever a runner is attached)
and `package_write` (package deletion). A person keeps their own Gitea rights
on all three.

The other tools outside WRITE_RULES change no branch content (issues,
labels, milestones, releases, tags, wiki, repositories, actions config,
notifications, time tracking); they are governed by the caller's own Gitea
permissions and the token's scopes (§9).

Downloads are refused (check_call): `actions_run_read` `download_job_log` /
`download_artifact` always write a file, and `attachment_read` `download`
writes one unless Gitea answers with a small image (decided from the
response, so the gateway cannot tell the cases apart from the arguments).
gitea-mcp writes under $HOME/.gitea-mcp, and the container's root filesystem
is read-only, so such a call can only fail.

A pin bump (GITEA_MCP_VERSION) must re-read the registry and update TOOLS and
this table in the same change: tests/unit/mcp-manager/
test_2554_write_without_branch_is_refused.py fails while PINNED_VERSION and
the shipped version differ.

Stdlib only: gateway_registry imports this module, and gunicorn.conf.py
imports gateway_registry in the master process."""
from __future__ import annotations

import json
import re
from urllib.parse import quote

#: The gitea-mcp release this table was read from (docker.gitea.com/
#: gitea-mcp-server:1.7.0, sha256:273827c5cf8fec3846c09bd35461b59bdd06941bdcfcfefd388cbc22f2343f6b).
PINNED_VERSION = "1.7.0"
#: Where the default-branch lookups go: the in-stack Gitea, the same host
#: gitea-mcp is started with (`-H http://gitea:3000`). Never configurable.
GITEA_API = "http://gitea:3000/api/v1"
#: The largest Gitea API answer a lookup reads (a PR carries its body).
MAX_LOOKUP_BYTES = 4 * 1024 * 1024

#: #2554: a revoked OAuth grant. gitea-mcp turns Gitea's 401 into a tool
#: RESULT (HTTP 200, `isError: true`, Gitea's own text such as "invalid
#: username, password or token"), so the gateway never sees a 401 status.
#: When an OAuth caller's tool call comes back as an error, the gateway asks
#: Gitea itself, with the same injected credential, who that credential is:
#: 401/403 here means the grant is dead. Matching the error text instead
#: would break on the next wording change and could mark a live grant.
GRANT_PROBE_PATH = "/user"
#: A tool error is small; only a response up to this size is held back and
#: checked. Anything larger is relayed as it streams.
GRANT_PEEK_BYTES = 64 * 1024


def is_tool_error(content_type: str, body: bytes) -> bool:
    """True iff the body is exactly one JSON-RPC response whose result is a
    tool error (`isError: true`), as JSON or as one SSE `data:` event."""
    text = body.decode("utf-8", errors="replace")
    if "text/event-stream" in (content_type or "").lower():
        datas = []
        for event in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(line[5:].lstrip(" ") for line in re.split(r"\r?\n", event)
                             if line.startswith("data:"))
            if data.strip():
                datas.append(data)
        if len(datas) != 1:
            return False
        text = datas[0]
    try:
        msg = json.loads(text)
    except ValueError:
        return False
    result = msg.get("result") if isinstance(msg, dict) else None
    return isinstance(result, dict) and result.get("isError") is True


#: Every tool of gitea-mcp 1.7.0 -> the properties of its input schema, from
#: its domain registries (tool.Tool.ReadTools/WriteTools; the record is
#: tests/fixtures/gitea-mcp-1.7.0-tools.json).
READ_TOOLS = {
    "actions_config_read": frozenset({"method", "name", "org", "owner", "page", "per_page", "repo"}),
    "actions_run_read": frozenset({"artifact_id", "artifact_name", "job_id", "max_bytes", "method",
                                   "output_path", "owner", "page", "per_page", "repo", "run_id",
                                   "status", "tail_lines", "workflow_id"}),
    "attachment_read": frozenset({"attachment_id", "attachment_uuid", "comment_id", "issue_number",
                                  "method", "output_path", "owner", "repo"}),
    "get_commit": frozenset({"owner", "repo", "sha"}),
    "get_dir_contents": frozenset({"owner", "path", "ref", "repo"}),
    "get_file_contents": frozenset({"owner", "path", "ref", "repo", "withLines"}),
    "get_gitea_mcp_server_version": frozenset(),
    "get_latest_release": frozenset({"owner", "repo"}),
    "get_me": frozenset(),
    "get_release": frozenset({"id", "owner", "repo"}),
    "get_repository_tree": frozenset({"owner", "page", "per_page", "recursive", "repo", "tree_sha"}),
    "get_tag": frozenset({"owner", "repo", "tag_name"}),
    "get_user_orgs": frozenset({"page", "per_page"}),
    "issue_read": frozenset({"issue_number", "method", "owner", "repo"}),
    "label_read": frozenset({"id", "method", "org", "owner", "page", "per_page", "repo"}),
    "list_branches": frozenset({"owner", "page", "per_page", "repo"}),
    "list_commits": frozenset({"owner", "page", "path", "per_page", "repo", "sha"}),
    "list_issues": frozenset({"before", "labels", "milestones", "owner", "page", "per_page", "repo",
                              "since", "state", "type"}),
    "list_my_repos": frozenset({"page", "per_page"}),
    "list_org_repos": frozenset({"org", "page", "per_page"}),
    "list_pull_requests": frozenset({"milestone", "owner", "page", "per_page", "repo", "sort", "state"}),
    "list_releases": frozenset({"is_draft", "is_pre_release", "owner", "page", "per_page", "repo"}),
    "list_tags": frozenset({"owner", "page", "per_page", "repo"}),
    "milestone_read": frozenset({"id", "method", "name", "owner", "page", "per_page", "repo", "state"}),
    "notification_read": frozenset({"before", "id", "method", "owner", "page", "per_page", "repo",
                                    "since", "status", "subject_type"}),
    "package_read": frozenset({"method", "name", "owner", "page", "per_page", "q", "type", "version"}),
    "pull_request_read": frozenset({"binary", "method", "owner", "page", "per_page", "pull_number",
                                    "repo", "review_id"}),
    "search_issues": frozenset({"labels", "owner", "page", "per_page", "query", "state", "type"}),
    "search_org_teams": frozenset({"includeDescription", "org", "page", "per_page", "query"}),
    "search_repos": frozenset({"isArchived", "isPrivate", "keywordInDescription", "keywordIsTopic",
                               "order", "ownerID", "page", "per_page", "query", "sort"}),
    "search_users": frozenset({"page", "per_page", "query"}),
    "timetracking_read": frozenset({"issue_number", "method", "owner", "page", "per_page", "repo"}),
    "wiki_read": frozenset({"method", "owner", "pageName", "repo"}),
}
WRITE_TOOLS = {
    "actions_config_write": frozenset({"data", "description", "method", "name", "org", "owner", "repo",
                                       "value"}),
    "actions_run_write": frozenset({"inputs", "method", "owner", "ref", "repo", "run_id", "workflow_id"}),
    "create_branch": frozenset({"branch", "old_branch", "owner", "repo"}),
    "create_or_update_file": frozenset({"branch_name", "content", "message", "new_branch_name", "owner",
                                        "path", "repo", "sha"}),
    "create_release": frozenset({"body", "is_draft", "is_pre_release", "owner", "repo", "tag_name",
                                 "target", "title"}),
    "create_repo": frozenset({"auto_init", "default_branch", "description", "gitignores", "issue_labels",
                              "license", "name", "object_format_name", "organization", "private",
                              "readme", "template", "trust_model"}),
    "create_tag": frozenset({"message", "owner", "repo", "tag_name", "target"}),
    "delete_branch": frozenset({"branch", "owner", "repo"}),
    "delete_file": frozenset({"branch_name", "message", "owner", "path", "repo", "sha"}),
    "delete_release": frozenset({"id", "owner", "repo"}),
    "delete_tag": frozenset({"owner", "repo", "tag_name"}),
    "fork_repo": frozenset({"name", "organization", "repo", "user"}),
    "issue_write": frozenset({"assignees", "body", "commentID", "deadline", "issue_number", "label_id",
                              "labels", "method", "milestone", "owner", "ref", "remove_deadline", "repo",
                              "state", "title"}),
    "label_write": frozenset({"color", "description", "exclusive", "id", "is_archived", "method", "name",
                              "org", "owner", "repo"}),
    "milestone_write": frozenset({"description", "due_on", "id", "method", "owner", "repo", "state",
                                  "title"}),
    "notification_write": frozenset({"id", "last_read_at", "method", "owner", "repo"}),
    "package_write": frozenset({"method", "name", "owner", "type", "version"}),
    "pull_request_review_write": frozenset({"body", "comment_id", "comments", "commit_id", "message",
                                            "method", "owner", "pull_number", "repo", "review_id",
                                            "state"}),
    "pull_request_write": frozenset({"allow_maintainer_edit", "assignee", "assignees", "base", "body",
                                     "deadline", "delete_branch", "draft", "force_merge", "head",
                                     "head_commit_id", "labels", "merge_style",
                                     "merge_when_checks_succeed", "message", "method", "milestone",
                                     "owner", "pull_number", "remove_deadline", "repo", "reviewers",
                                     "state", "team_reviewers", "title"}),
    "rename_branch": frozenset({"branch", "new_name", "owner", "repo"}),
    "timetracking_write": frozenset({"id", "issue_number", "method", "owner", "repo", "time"}),
    "wiki_write": frozenset({"content", "message", "method", "owner", "pageName", "repo", "title"}),
}
TOOLS = {**READ_TOOLS, **WRITE_TOOLS}
#: Arguments no caller may pass to any tool: gitea-mcp writes `output_path`
#: into its own filesystem, which every caller of the one container shares.
FORBIDDEN_ARGS = frozenset({"output_path"})
#: pull_request_write's `method` enum at 1.7.0 (operation/pull/pull.go).
PR_METHODS = frozenset({"create", "update", "close", "reopen", "merge", "update_branch",
                        "add_reviewers", "remove_reviewers"})
#: tool -> the `method` values that download into gitea-mcp's own
#: filesystem (operation/actions/runs.go, artifacts.go, issue/attachment.go).
DOWNLOAD_METHODS = {
    "actions_run_read": frozenset({"download_job_log", "download_artifact"}),
    "attachment_read": frozenset({"download"}),
}
DOWNLOAD_REFUSAL = ("{tool}: method {method!r} is not supported through the gateway: it downloads "
                    "into the Gitea MCP container, whose root filesystem is read-only. Read the "
                    "content with the tool's get/list methods or in Gitea itself")

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_DIGITS = re.compile(r"^[0-9]{1,18}$")


class LookupFailed(Exception):
    """A Gitea lookup the rule needs did not give a usable answer."""


def check_call(tool, args) -> str | None:
    """Rules 1 and 2 for any `tools/call` on the Gitea server, and the
    download refusal (DOWNLOAD_METHODS): a refusal message, or None. Needs no credential and no network."""
    if not isinstance(tool, str) or tool not in TOOLS:
        return f"{tool!r} is not a known tool of this Gitea server"
    args = args if isinstance(args, dict) else {}
    schema = TOOLS[tool]
    folded = {p.casefold(): p for p in schema | FORBIDDEN_ARGS}
    for key in args:
        if key in FORBIDDEN_ARGS:
            return f"{tool}: {key!r} is not allowed through the gateway"
        want = folded.get(key.casefold())
        if want is not None and key != want:
            return f"{tool}: argument {key!r} is not spelled {want!r}"
        if tool in WRITE_TOOLS and key not in schema:
            return f"{tool}: unknown argument {key!r}"
    method = args.get("method")
    if isinstance(method, str) and method in DOWNLOAD_METHODS.get(tool, ()):
        return DOWNLOAD_REFUSAL.format(tool=tool, method=method)
    return None


# ---------------------------------------------------------------------------
# Branch-content writes (rules 3-5)
# ---------------------------------------------------------------------------

def _branch_arg(args, key, tool, *, required=True):
    """The branch named by `key`, exactly as gitea-mcp would read it — or a
    refusal. A present non-string is refused: gitea-mcp would read it as ""."""
    if key not in args:
        if required:
            return None, f"{tool}: {key} is required (writes go to a branch, never the default branch)"
        return "", None
    value = args[key]
    if not isinstance(value, str):
        return None, f"{tool}: {key} must be a branch name"
    if not value.strip():
        if required:
            return None, f"{tool}: {key} is empty (writes go to a branch, never the default branch)"
        return "", None
    if value != value.strip() or any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        return None, f"{tool}: {key} is not a valid branch name"
    return value, None


def _same_branch(a: str, b: str) -> bool:
    def norm(x):
        return (x[len("refs/heads/"):] if x.startswith("refs/heads/") else x).casefold()
    return norm(a) == norm(b)


def _repo_path(args, tool):
    owner, repo = args.get("owner"), args.get("repo")
    for label, value in (("owner", owner), ("repo", repo)):
        if not isinstance(value, str) or not _NAME.match(value) or value in (".", ".."):
            return None, f"{tool}: {label} is not a valid Gitea name"
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}", None


def _default_branch(fetch, repo_path, tool):
    """The repository's default branch, read with the caller's credential, or
    raise LookupFailed (fail closed)."""
    status, body = fetch(repo_path)
    if status != 200 or not isinstance(body, dict):
        raise LookupFailed(f"{tool}: the repository's default branch could not be checked")
    default = body.get("default_branch")
    if not isinstance(default, str) or not default.strip():
        raise LookupFailed(f"{tool}: the repository's default branch could not be checked")
    return default


def _targets_refusal(tool, args, targets, fetch):
    """Rules 4 and 5 for the already-validated `targets`."""
    repo_path, refusal = _repo_path(args, tool)
    if refusal:
        return refusal
    try:
        default = _default_branch(fetch, repo_path, tool)
    except LookupFailed as e:
        return f"{e}; nothing was written"
    for target in targets:
        if _same_branch(target, default):
            return (f"{tool}: {target!r} is the default branch; write to a new branch and "
                    "open a pull request")
    return None


def _create_or_update_file(caller, args, fetch):
    # gitea-mcp: FileOptions{BranchName, NewBranchName}; the commit lands on
    # cmp.Or(new_branch_name, branch_name). branch_name is the source when a
    # new branch is created, so it may name the default branch then.
    tool = "create_or_update_file"
    new, refusal = _branch_arg(args, "new_branch_name", tool, required=False)
    if refusal:
        return refusal
    base, refusal = _branch_arg(args, "branch_name", tool, required=not new)
    if refusal:
        return refusal
    return _targets_refusal(tool, args, [new or base], fetch)


def _delete_file(caller, args, fetch):
    tool = "delete_file"
    target, refusal = _branch_arg(args, "branch_name", tool)
    return refusal or _targets_refusal(tool, args, [target], fetch)


def _create_branch(caller, args, fetch):
    # `branch` is the new branch; `old_branch` (the source) may be the default.
    tool = "create_branch"
    target, refusal = _branch_arg(args, "branch", tool)
    if refusal:
        return refusal
    _old, refusal = _branch_arg(args, "old_branch", tool, required=False)
    return refusal or _targets_refusal(tool, args, [target], fetch)


def _delete_branch(caller, args, fetch):
    tool = "delete_branch"
    target, refusal = _branch_arg(args, "branch", tool)
    return refusal or _targets_refusal(tool, args, [target], fetch)


def _rename_branch(caller, args, fetch):
    tool = "rename_branch"
    old, refusal = _branch_arg(args, "branch", tool)
    if refusal:
        return refusal
    new, refusal = _branch_arg(args, "new_name", tool)
    return refusal or _targets_refusal(tool, args, [old, new], fetch)


def _pull_number(args, tool):
    """The PR index exactly as gitea-mcp's params.GetIndex reads it, or None
    for anything it might read differently (a fraction it would truncate, a
    signed or padded string, a bool)."""
    value = args.get("pull_number")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    elif isinstance(value, str) and _DIGITS.match(value) and not (len(value) > 1 and value[0] == "0"):
        n = int(value)
    else:
        return None
    return n if 0 < n < 2 ** 53 else None


def _pull_request_write(caller, args, fetch):
    tool = "pull_request_write"
    method = args.get("method")
    if not isinstance(method, str) or method not in PR_METHODS:
        return f"{tool}: unknown method {method!r}"
    if "force_merge" in args and args["force_merge"] is not False:
        return f"{tool}: force_merge is not allowed through the gateway"
    if method == "merge":
        if getattr(caller, "kind", "") != "user":
            return (f"{tool}: a technical user opens pull requests and never merges them; "
                    "a person reviews and merges")
        return None
    if method != "update_branch":
        return None
    # update_branch merges the base INTO the PR's head: the head is written.
    repo_path, refusal = _repo_path(args, tool)
    if refusal:
        return refusal
    index = _pull_number(args, tool)
    if index is None:
        return f"{tool}: pull_number must be a positive whole number"
    try:
        default = _default_branch(fetch, repo_path, tool)
        status, pr = fetch(f"{repo_path}/pulls/{index}")
        if status != 200 or not isinstance(pr, dict):
            raise LookupFailed(f"{tool}: the pull request's head could not be checked")
        head = pr.get("head")
        ref = head.get("ref") if isinstance(head, dict) else None
        if not isinstance(ref, str) or not ref.strip():
            raise LookupFailed(f"{tool}: the pull request's head could not be checked")
        defaults = [default]
        head_repo, base = head.get("repo"), pr.get("base")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        head_id = head_repo.get("id") if isinstance(head_repo, dict) else None
        base_id = base_repo.get("id") if isinstance(base_repo, dict) else None
        if head_id is None or base_id is None:
            raise LookupFailed(f"{tool}: the pull request's head could not be checked")
        if head_id != base_id:
            # A fork's head: the write lands on the FORK's branch.
            fork_default = head_repo.get("default_branch")
            if not isinstance(fork_default, str) or not fork_default.strip():
                raise LookupFailed(f"{tool}: the pull request's head could not be checked")
            defaults.append(fork_default)
    except LookupFailed as e:
        return f"{e}; nothing was written"
    if any(_same_branch(ref, d) for d in defaults):
        return f"{tool}: the pull request's head {ref!r} is a default branch; nothing was written"
    return None


def _persons_only(tool, what):
    """A rule that refuses `tool` (every method) to every technical caller;
    a person's call goes to Gitea under their own rights."""
    def rule(caller, args, fetch):
        if getattr(caller, "kind", "") != "user":
            return (f"{tool}: a technical user may not {what} through the gateway; "
                    "a person does it with their own Gitea rights")
        return None
    rule.__name__ = f"_{tool}"
    return rule


#: tool -> rule(caller, args, fetch) -> refusal | None, for every gitea-mcp
#: tool that writes branch content, and the §9 extension's persons-only
#: tools. `fetch(path) -> (status, json)` reads the
#: in-stack Gitea API with the caller's own credential and raises
#: LookupFailed on a transport error.
WRITE_RULES = {
    "create_or_update_file": _create_or_update_file,
    "delete_file": _delete_file,
    "create_branch": _create_branch,
    "delete_branch": _delete_branch,
    "rename_branch": _rename_branch,
    "pull_request_write": _pull_request_write,
    # §9 extension: persons only (see the module docstring).
    "pull_request_review_write": _persons_only("pull_request_review_write",
                                               "review, approve or dismiss pull requests"),
    "actions_run_write": _persons_only("actions_run_write", "dispatch, cancel or rerun workflows"),
    "package_write": _persons_only("package_write", "delete packages"),
}
