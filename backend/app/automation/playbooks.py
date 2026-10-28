"""Remediation playbook loading and validation (YAML)."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "playbooks"
RISKS = ("low", "medium", "high")
BUILTIN_ACTIONS = {"wait", "verify", "notify", "escalate"}


@dataclass
class Step:
    action: str
    params: dict = field(default_factory=dict)
    when: str | None = None
    unless: str | None = None


@dataclass
class Playbook:
    name: str
    title: str
    description: str
    triggers: list[str]
    kinds: list[str]  # alert kinds allowed to trigger it (empty = any)
    risk: str
    # "downstream": the change can disrupt devices behind this one (impact
    # analysis applies); "none": it only touches this device/host (e.g. an ACL).
    impact: str
    requires_approval: bool
    steps: list[Step]
    rollback: list[Step]

    def to_dict(self) -> dict:
        def steps(items):
            return [{"action": s.action, "params": s.params, "when": s.when, "unless": s.unless} for s in items]

        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "triggers": self.triggers,
            "kinds": self.kinds,
            "risk": self.risk,
            "impact": self.impact,
            "requires_approval": self.requires_approval,
            "steps": steps(self.steps),
            "rollback": steps(self.rollback),
        }


def _steps(raw: list | None, where: str) -> list[Step]:
    out = []
    for i, s in enumerate(raw or []):
        if not isinstance(s, dict) or "action" not in s:
            raise ValueError(f"{where}[{i}]: each step needs an 'action'")
        out.append(Step(s["action"], s.get("params") or {}, s.get("when"), s.get("unless")))
    return out


def parse(doc: dict) -> Playbook:
    for key in ("name", "triggers", "steps"):
        if key not in doc:
            raise ValueError(f"playbook missing required key {key!r}")
    if not re.fullmatch(r"[a-z0-9-]+", doc["name"]):
        raise ValueError("playbook name must be lowercase kebab-case")
    risk = doc.get("risk", "medium")
    if risk not in RISKS:
        raise ValueError(f"risk must be one of {RISKS}")
    if doc.get("impact", "downstream") not in ("downstream", "none"):
        raise ValueError("impact must be 'downstream' or 'none'")
    return Playbook(
        name=doc["name"],
        title=doc.get("title", doc["name"]),
        description=doc.get("description", ""),
        triggers=list(doc["triggers"]),
        kinds=list(doc.get("kinds") or []),
        risk=risk,
        impact=doc.get("impact", "downstream"),
        requires_approval=bool(doc.get("requires_approval", False)),
        steps=_steps(doc["steps"], "steps"),
        rollback=_steps(doc.get("rollback"), "rollback"),
    )


def load_all(directory: Path = PLAYBOOK_DIR) -> dict[str, Playbook]:
    books = {}
    for path in sorted(directory.glob("*.y*ml")):
        pb = parse(yaml.safe_load(path.read_text()))
        books[pb.name] = pb
    return books


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class _Fmt(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(value, ctx: dict):
    """Substitute ``{placeholders}`` from the run context (recursively)."""
    if isinstance(value, str):
        if m := _PLACEHOLDER.fullmatch(value):
            return ctx.get(m[1], value)
        flat = {k: ", ".join(map(str, v)) if isinstance(v, list) else v for k, v in ctx.items()}
        return value.format_map(_Fmt(flat))
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    return value
