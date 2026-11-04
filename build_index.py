#!/usr/bin/env python3
"""Generate INDEX.md: a compact, always-current map of every cog in this repo.

Why it exists: so "does a cog already cover this?" and "what depends on what?"
can be answered by reading one small file instead of cloning and reading every
cog. It is built from the code itself (AST, no discord/redbot install needed),
never from hand-written notes, so it cannot drift as long as it is regenerated.

    python build_index.py          # rewrite INDEX.md
    python build_index.py --check  # exit 1 if INDEX.md is stale (test_index.py does this)

Scope: this repo only. It cannot see Red core, third-party cogs or YAGPDB, so
"external" cogs below are only the ones our code calls bot.get_cog() on.
"""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "INDEX.md"

SKIP_FILES = {"conftest.py", "_testkit.py", "_testdata.py", "_redbot_stub.py", "settings_panel.py"}
SKIP_DIRS = {"tests", "__pycache__", "assets"}
COMMAND_ATTRS = {"command", "group", "hybrid_command", "hybrid_group"}
VERSION_NAMES = {"COG_VERSION", "VERSION", "BUILD", "__version__"}
CONFIG_SCOPES = ("guild", "global", "member", "user", "channel", "role")


def cog_dirs():
    return sorted(p.parent for p in ROOT.glob("*/info.json"))


def is_test_path(path, cog_dir):
    rel = path.relative_to(cog_dir)
    return (
        path.name.startswith("test_")
        or path.name in SKIP_FILES
        or any(part in SKIP_DIRS for part in rel.parts[:-1])
    )


def source_files(cog_dir):
    return sorted(p for p in cog_dir.rglob("*.py") if not is_test_path(p, cog_dir))


def test_files(cog_dir):
    return sorted(
        p
        for p in cog_dir.rglob("*.py")
        if p.name.startswith("test_") and "__pycache__" not in p.parts
    )


def parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def str_const(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def module_string_constants(tree):
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = str_const(node.value)
            if value is not None:
                consts[node.targets[0].id] = value
    return consts


# --------------------------------------------------------------------- versions

def find_version(trees):
    """First version-ish string: COG_VERSION / VERSION / BUILD / __version__
    (module or class level), else a 'build: <x>' string literal."""
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]  # e.g. COG_VERSION: str = "1.1.0"
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in VERSION_NAMES:
                    value = str_const(node.value)
                    if value:
                        return value.split()[0]
    for tree in trees:
        for node in ast.walk(tree):
            value = str_const(node)
            if value:
                match = re.search(r"build: (\S+)", value)
                if match:
                    return match.group(1)
    return "n/a"


# --------------------------------------------------------------------- commands

class Cmd:
    def __init__(self, name, aliases, slash):
        self.name = name
        self.aliases = aliases
        self.slash = slash
        self.children = []


def decorator_command(dec):
    """Return (kind, base, call) for a command/group decorator, else None."""
    call = dec if isinstance(dec, ast.Call) else None
    target = dec.func if call else dec
    if not isinstance(target, ast.Attribute) or target.attr not in COMMAND_ATTRS:
        return None
    return target.attr, target.value, call


def kwarg(call, key):
    if call is None:
        return None
    for kw in call.keywords:
        if kw.arg == key:
            return kw.value
    return None


def extract_commands(tree, unresolved):
    """Return root Cmd objects for one module, with sub-commands nested."""
    funcs = sorted(
        (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        key=lambda n: n.lineno,
    )
    groups = {}  # function name -> Cmd
    roots = []
    for fn in funcs:
        for dec in fn.decorator_list:
            found = decorator_command(dec)
            if not found:
                continue
            kind, base, call = found
            name_node = kwarg(call, "name")
            name = str_const(name_node) if name_node is not None else None
            name = name or fn.name
            aliases = []
            alias_node = kwarg(call, "aliases")
            if isinstance(alias_node, (ast.List, ast.Tuple)):
                aliases = [str_const(e) for e in alias_node.elts if str_const(e)]
            slash = False
            parent = None
            if isinstance(base, ast.Name) and base.id in {"commands", "app_commands"}:
                slash = base.id == "app_commands"
            elif isinstance(base, ast.Attribute) and base.attr == "app_commands":
                slash = True
            elif isinstance(base, ast.Name) and base.id in groups:
                parent = groups[base.id]
            else:
                unresolved.append(f"{fn.name} (base {ast.unparse(base)})")
            cmd = Cmd(name, aliases, slash)
            if parent is not None:
                parent.children.append(cmd)
            else:
                roots.append(cmd)
            if kind in {"group", "hybrid_group"}:
                groups[fn.name] = cmd
    return roots


def render_children(cmd):
    parts = []
    for child in sorted(cmd.children, key=lambda c: c.name):
        label = child.name
        if child.children:
            label += "{" + ", ".join(render_children(child)) + "}"
        parts.append(label)
    return parts


def render_root(cmd):
    prefix = "/" if cmd.slash else "."
    label = prefix + "|".join([cmd.name] + sorted(cmd.aliases))
    if cmd.children:
        return f"`{label}`: " + ", ".join(render_children(cmd))
    return f"`{label}`"


# ------------------------------------------------------------------- per-cog data

def cog_classes(trees):
    names = []
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    if (isinstance(base, ast.Attribute) and base.attr == "Cog") or (
                        isinstance(base, ast.Name) and base.id == "Cog"
                    ):
                        names.append(node.name)
    return sorted(set(names))


def get_cog_refs(trees, unresolved):
    refs = set()
    for tree in trees:
        consts = module_string_constants(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_cog"
                and node.args
            ):
                arg = node.args[0]
                value = str_const(arg)
                if value is None and isinstance(arg, ast.Name):
                    value = consts.get(arg.id)
                if value:
                    refs.add(value)
                else:
                    unresolved.append(f"get_cog({ast.unparse(arg)})")
    return refs


def config_scopes(trees):
    scopes = set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                for scope in CONFIG_SCOPES:
                    if node.func.attr == f"register_{scope}":
                        scopes.add(scope)
    return sorted(scopes)


def first_sentence(text, limit=170):
    text = " ".join((text or "").split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def analyse(cog_dir, warnings):
    info = json.loads((cog_dir / "info.json").read_text(encoding="utf-8"))
    files = source_files(cog_dir)
    trees = [parse(p) for p in files]
    unresolved = []
    roots = []
    for path, tree in zip(files, trees):
        module_unresolved = []
        roots.extend(extract_commands(tree, module_unresolved))
        unresolved.extend(f"{path.relative_to(ROOT)}: {u}" for u in module_unresolved)
    refs = get_cog_refs(trees, unresolved)
    for item in unresolved:
        warnings.append(f"{cog_dir.name}: unresolved {item}")
    tests = sum(
        len(re.findall(r"^\s*(?:async\s+)?def test_", p.read_text(encoding="utf-8"), re.M))
        for p in test_files(cog_dir)
    )
    return {
        "dir": cog_dir.name,
        "info": info,
        "version": find_version(trees),
        "classes": cog_classes(trees),
        "lines": sum(len(p.read_text(encoding="utf-8").splitlines()) for p in files),
        "tests": tests,
        "roots": sorted(roots, key=lambda c: (c.slash, c.name)),
        "refs": refs,
        "scopes": config_scopes(trees),
    }


# ------------------------------------------------------------------------ render

def command_clashes(cogs):
    """Top-level prefix command names/aliases claimed by more than one cog.
    Red refuses to load a cog whose command name is already taken."""
    owners = {}
    for c in cogs:
        for r in c["roots"]:
            if r.slash:
                continue
            for name in [r.name] + r.aliases:
                owners.setdefault(name.lower(), set()).add(c["dir"])
    return {n: sorted(o) for n, o in owners.items() if len(o) > 1}


def build(warnings=None):
    warnings = warnings if warnings is not None else []
    cogs = [analyse(d, warnings) for d in cog_dirs()]
    class_to_dir = {cls: c["dir"] for c in cogs for cls in c["classes"]}

    used_by = {c["dir"]: set() for c in cogs}
    for c in cogs:
        for ref in c["refs"]:
            target = class_to_dir.get(ref)
            if target and target != c["dir"]:
                used_by[target].add(c["dir"])

    out = [
        "# evac-cogs index",
        "",
        "<!-- GENERATED by build_index.py. Do not edit. Regenerate: python build_index.py -->",
        "",
        f"{len(cogs)} cogs. Generated from the code (commands, versions, cross-cog calls), so it is current as of the last push.",
        "Covers this repo only: it cannot see Red core, third-party cogs or YAGPDB. Check `.cogs` live for what is loaded.",
        "Use it to answer \"does a cog already cover this?\" and \"what depends on this cog?\". Read the real source before changing anything.",
        "",
        "## At a glance",
        "",
    ]
    for c in cogs:
        out.append(f"- **{c['dir']}** {c['version']}: {c['info'].get('short', '').strip()}")

    out += ["", "## Cogs", ""]
    for c in cogs:
        info = c["info"]
        flags = []
        if info.get("hidden"):
            flags.append("hidden")
        if info.get("disabled"):
            flags.append("disabled")
        classes = ", ".join(c["classes"]) or "no Cog class found"
        out.append(f"### {c['dir']}")
        ver = c["version"]
        ver = f"v{ver}" if ver[:1].isdigit() else ver
        out.append(
            f"{ver} · {classes} · {c['lines']} lines · {c['tests']} tests"
            + (f" · {', '.join(flags)}" if flags else "")
        )
        if c["roots"]:
            groups = [r for r in c["roots"] if r.children]
            plain = [r for r in c["roots"] if not r.children]
            out.append("Commands:")
            for r in groups:
                out.append(f"- {render_root(r)}")
            if plain:
                out.append("- Standalone: " + ", ".join(render_root(r) for r in plain))
        else:
            out.append("Commands: none (listeners/loops only)")
        uses_repo = sorted({class_to_dir[r] for r in c["refs"] if r in class_to_dir and class_to_dir[r] != c["dir"]})
        uses_ext = sorted(r for r in c["refs"] if r not in class_to_dir)
        links = []
        if uses_repo:
            links.append("calls " + ", ".join(uses_repo))
        if uses_ext:
            links.append("optional/external " + ", ".join(uses_ext))
        if used_by[c["dir"]]:
            links.append("called by " + ", ".join(sorted(used_by[c["dir"]])))
        out.append("Links: " + ("; ".join(links) if links else "none"))
        reqs = info.get("requirements") or []
        out.append(
            "Needs: " + (", ".join(reqs) if reqs else "no extra pip packages")
            + (" · Config scopes: " + ", ".join(c["scopes"]) if c["scopes"] else "")
        )
        stores = first_sentence(info.get("end_user_data_statement", ""))
        if stores:
            out.append(f"Data: {stores}")
        out.append("")

    out += ["## Command-name collisions inside this repo", ""]
    clashes = command_clashes(cogs)
    if clashes:
        for name in sorted(clashes):
            out.append(f"- `.{name}` is claimed by: {', '.join(clashes[name])}")
    else:
        out.append("None. Top-level command names and aliases are unique across cogs.")
    out.append("(Does not check Red core, third-party cogs or YAGPDB. Grep those separately before naming a command.)")

    tag_map = {}
    for c in cogs:
        for tag in c["info"].get("tags") or []:
            tag_map.setdefault(tag.lower(), []).append(c["dir"])
    shared = {t: d for t, d in tag_map.items() if len(d) > 1}
    out += ["", "## Shared tags (leads for overlap review, not verdicts)", ""]
    if shared:
        for tag in sorted(shared):
            out.append(f"- {tag}: {', '.join(sorted(shared[tag]))}")
    else:
        out.append("None.")
    out.append("")
    return "\n".join(out)


def main(argv):
    warnings = []
    text = build(warnings)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if "--check" in argv:
        current = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
        if current != text:
            print("INDEX.md is stale: run `python build_index.py`", file=sys.stderr)
            return 1
        print("INDEX.md is current")
        return 0
    INDEX.write_text(text, encoding="utf-8")
    print(f"wrote {INDEX.name}: {len(text.splitlines())} lines, {len(text)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
