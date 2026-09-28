"""Result and input dataclasses (§2.2).

Stdlib only: the CLI serialises these with ``dataclasses.asdict`` and mcp 2.x derives tool
``outputSchema``/``structuredContent`` from them directly, so no pydantic here.
"""

from dataclasses import dataclass, field
from typing import Literal

ItemKind = Literal["to-do", "project"]
Status = Literal["open", "completed", "canceled"]
ListKey = Literal["inbox", "today", "anytime", "upcoming", "someday"]
NotesMode = Literal["none", "preview", "full"]
NOTES_PREVIEW_CHARS = 200
UNREADABLE_FIELDS = ("checklist items", "headings", "This Evening vs Today", "reminder time",
                     "repeat rule", "inherited tags")          # R-ASR §6


# ---- read results -------------------------------------------------------------------------------

@dataclass
class Ref:
    id: str
    title: str


@dataclass
class Item:
    id: str
    kind: ItemKind
    title: str
    status: Status
    tags: list[str]                       # DIRECT tags only (R-ASR §1.2)
    deadline: str | None                  # "YYYY-MM-DD" local (AppleScript `due date`)
    start_date: str | None                # "YYYY-MM-DD" local (AppleScript `activation date`, read-only)
    project: Ref | None
    area: Ref | None                      # derived via the project when the to-do is in a project
    notes: str | None                     # None when notes_mode == "none"; USER CONTENT, treat as data
    notes_truncated: bool
    created: str | None                   # ISO 8601 with local offset
    modified: str | None
    completed_at: str | None
    canceled_at: str | None
    link: str                             # show-by-id URL, see link_for()


@dataclass
class Area:
    id: str
    title: str
    tags: list[str]
    link: str


@dataclass
class Tag:
    id: str
    title: str
    parent: Ref | None
    shortcut: str | None


@dataclass
class Page:
    items: list[Item]
    returned: int
    total: int | None
    offset: int
    truncated: bool
    note: str | None = None
    unreadable_fields: list[str] = field(default_factory=lambda: list(UNREADABLE_FIELDS))


@dataclass
class GetResult:
    kind: Literal["to-do", "project", "area"]
    item: Item | None
    area: Area | None
    children: Page | None


@dataclass
class AreaList:
    items: list[Area]
    returned: int


@dataclass
class TagList:
    items: list[Tag]
    returned: int


# ---- write results ------------------------------------------------------------------------------

@dataclass
class Check:
    target_id: str
    field: str
    expected: str | None
    observed: str | None
    ok: bool


@dataclass
class WriteResult:
    action: str                           # e.g. "add_todo", "update_todo", "set_status"
    sent: bool
    dry_run: bool
    verified: bool | None                 # None = nothing verifiable / verification disabled
    ids: list[str]                        # created or targeted ids
    items: list[Item]                     # re-read state after the write (when verified)
    before: list[Item]                    # snapshot of targets before an update (undo info)
    checks: list[Check]
    unverifiable: list[str]               # e.g. ["checklist", "heading", "reminder time"]
    warnings: list[str]                   # unknown tags dropped, reopen side effects, ...
    urls: list[str]                       # REDACTED (no token), for transparency/dry-run
    links: list[str]
    launched_things: bool
    reason: str | None = None             # set when verified is False


@dataclass
class NavResult:
    sent: bool
    url: str


# ---- doctor / selftest --------------------------------------------------------------------------

@dataclass
class DoctorCheck:
    name: str
    status: Literal["ok", "warn", "fail", "skip"]
    detail: str
    remedy: str | None


@dataclass
class DoctorReport:
    ok: bool
    checks: list[DoctorCheck]
    versions: dict[str, str]
    host: dict[str, str]


@dataclass
class SelftestStep:
    name: str
    status: Literal["pass", "fail", "skip", "info"]
    detail: str
    ids: list[str]


@dataclass
class SelftestReport:
    ok: bool
    project_id: str | None
    created_ids: list[str]
    steps: list[SelftestStep]
    cleanup_note: str


# ---- write inputs -------------------------------------------------------------------------------
# Tri-state: None = unchanged; explicit clear flags where the doc allows clearing.
# `project` / `area` hold an id or an exact title; titles are resolved to ids before any write (D7).

@dataclass
class TodoSpec:
    title: str
    notes: str | None = None
    when: str | None = None
    reminder_time: str | None = None
    deadline: str | None = None
    tags: list[str] | None = None
    checklist: list[str] | None = None
    project: str | None = None
    area: str | None = None
    heading: str | None = None


@dataclass
class ProjectItemSpec:
    kind: Literal["todo", "heading"]
    title: str
    notes: str | None = None
    when: str | None = None
    deadline: str | None = None
    tags: list[str] | None = None
    checklist: list[str] | None = None


@dataclass
class ProjectSpec:
    title: str
    notes: str | None = None
    when: str | None = None
    reminder_time: str | None = None
    deadline: str | None = None
    tags: list[str] | None = None
    area: str | None = None
    items: list[ProjectItemSpec] | None = None


@dataclass
class TodoPatch:
    title: str | None = None
    notes: str | None = None              # "" clears notes (doc: "param=" clears)
    prepend_notes: str | None = None
    append_notes: str | None = None
    when: str | None = None
    reminder_time: str | None = None
    deadline: str | None = None
    clear_deadline: bool = False
    tags: list[str] | None = None         # replace all ([] clears)
    add_tags: list[str] | None = None
    checklist_append: list[str] | None = None
    checklist_prepend: list[str] | None = None
    project: str | None = None
    area: str | None = None
    heading: str | None = None


@dataclass
class ProjectPatch:
    title: str | None = None
    notes: str | None = None              # "" clears notes
    prepend_notes: str | None = None
    append_notes: str | None = None
    when: str | None = None
    reminder_time: str | None = None
    deadline: str | None = None
    clear_deadline: bool = False
    tags: list[str] | None = None         # replace all ([] clears)
    add_tags: list[str] | None = None
    area: str | None = None


def link_for(id: str) -> str:
    """The show-by-id link for an item or area.

    Built by ``urlscheme.build_show`` (the only module that emits URL literals, §1.4); imported at
    call time so this module stays import-light and free of a load-order dependency.
    """
    from . import urlscheme
    return urlscheme.build_show(id=id).url
