"""SQF extractor for Arma 3 mods (ported from the DD_graph tool).

SQF has no tree-sitter grammar, and the names in it resolve through the configs of the mod,
not through imports. So the extraction has two parts:

- ``extract_sqf(path)``: the per-file pass. It gives the file node of a ``.sqf`` file.
- ``sqf_project_pass(...)``: one pass over the whole extraction. ``extract()`` calls it at the end,
  when the ids and the ``source_file`` values are in their final form. It gives:

  - one node for each function of the ``CfgFunctions`` of the addons (``<TAG>_fnc_<name>``), in its
    ``.sqf`` file. The parser knows the ``CfgFunctions`` of Arma (tag, category, the ``file``
    attribute, ``#include``) and the CBA macros (``class ADDON``, ``QPATHTOF(...)``,
    ``DOUBLES(...)``). ``addons/<addon>/XEH_PREP.hpp`` gives the CBA / ACE functions:
    ``PREP(name)`` is ``<PREFIX>_<COMPONENT>_fnc_<name>`` in ``functions/fnc_<name>.sqf``;
  - ``calls`` edges between the SQF functions. The code outside a function (``XEH_preInit.sqf``
    and other scripts) gives edges from its file node;
  - one node for each function that the project uses but does not have (``CBA_fnc_*``,
    ``BIS_fnc_*``, the functions of other mods), kind ``sqf_external``;
  - one node for each Intercept plugin command (``register_sqf_command`` in the C++ files),
    with a ``references`` edge (context ``handler``) to its C++ function;
  - ``references`` edges from SQF to the plugin commands that it uses, directly
    (``dd_ta_visible``) or through a variable made with ``compile "<command> ..."``;
  - ``defines`` edges from the ``CfgFunctions`` classes (graphify reads them as C++) to the
    functions.

Before the pass reads the SQF, it changes the macros ``FUNC(name)``, ``EFUNC(component,name)``,
``ACEFUNC(component,name)`` and their ``Q``, ``D`` and ``LINK`` forms to the function names.

A mod is the folder that holds an ``addons`` folder. One scan can hold more than one mod: the
functions of all mods are in one namespace, as in the game.

Names in SQF do not know case, so the matching uses lower case. Comments do not count.
A name in a string counts as INFERRED (the code can use the string as a name or not).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id

_LOG = logging.getLogger(__name__)

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FNC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*?_fnc_[A-Za-z0-9_]+$", re.IGNORECASE)
INCLUDE = re.compile(r'^\s*#include\s+"([^"]+)"', re.M)
PREP = re.compile(r"^\s*PREP\(\s*(\w+)\s*\)", re.M)
# The CBA / ACE function macros: (Q forms, macro, first argument, second argument).
MACRO_CALL = re.compile(r"\b(Q{0,2})(D?E?FUNC|LINKFUNC|ACEFUNC|ACELINKFUNC)\(\s*(\w+)\s*(?:,\s*(\w+)\s*)?\)")
REGISTER = re.compile(
    r'register_sqf_command\(\s*"([^"]+)"\s*,\s*(?:"(?:[^"\\]|\\.)*"\s*)+,\s*([A-Za-z_][A-Za-z0-9_:]*)'
)
# The CBA variable macros: GVAR(name), EGVAR(component,name) and their Q forms (strings).
GVAR_CALL = re.compile(r"\b(Q{0,2})(E?GVAR)\(\s*(\w+)\s*(?:,\s*(\w+)\s*)?\)")
WRAPPER = re.compile(r'\b(\w+)\s*=\s*compile\s*"\s*(\w+)')
DEFINE = re.compile(r"#define\s+(\w+)\s+(\S+)")
CFG_TOKEN = re.compile(r'"(?:[^"]|"")*"|[A-Za-z_][\w]*|[{};=:]|[^\s{};=:"]+')

_CPP_SUFFIXES = frozenset({".cpp", ".cc", ".cxx"})
# The config files of an addon (addons/<addon>/...): config.cpp and its includes.
_CONFIG_SUFFIXES = frozenset({".hpp", ".h", ".hh", ".inc", ".cpp"})
# The CBA functions that raise an event, and the ones that add a handler (lower case).
_EVENT_RAISERS = frozenset({
    "cba_fnc_localevent", "cba_fnc_serverevent", "cba_fnc_globalevent", "cba_fnc_globaleventjip",
    "cba_fnc_targetevent", "cba_fnc_remoteevent", "cba_fnc_ownerevent", "cba_fnc_turretevent",
})
_EVENT_ADDERS = frozenset({"cba_fnc_addeventhandler", "cba_fnc_addeventhandlerargs"})
_EVENT_CALL = re.compile(r"\bcall\s+(CBA_fnc_\w+)", re.IGNORECASE)
# A function made in the code: TAG_fnc_name = { ... } (also with compileFinal before the block).
_INLINE_FUNCTION = re.compile(r"(?<![\w.])([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*?_fnc_\w+)\s*=(?!=)\s*(?:compileFinal\s*)?\{")
# The script loads: execVM "x.sqf", preprocessFile(LineNumbers) "x.sqf", compileScript ["x.sqf"], loadFile.
_SCRIPT_LOAD = re.compile(
    r"""\b(execVM|preprocessFileLineNumbers|preprocessFile|compileScript|loadFile)\s*\[?\s*(?:"([^"]+?\.sqf)"|'([^']+?\.sqf)')""",
    re.IGNORECASE)
# The scripts that the engine runs in a mission (lower case).
_MISSION_EVENT_SCRIPTS = frozenset({
    "init.sqf", "initserver.sqf", "initplayerlocal.sqf", "initplayerserver.sqf", "onplayerkilled.sqf",
    "onplayerrespawn.sqf", "initintro.sqf", "exit.sqf", "exitserver.sqf", "onpause.sqf",
})
# The CfgFunctions categories and the folders of the test and console functions (lower case).
_TEST_NAMES = frozenset({"test", "tests", "debug", "console"})
# The config files that change the SQF part of the graph (lower case).
_SQF_CONFIG_NAMES = frozenset({
    "cfgfunctions.hpp", "xeh_prep.hpp", "config.cpp", "script_component.hpp", "script_mod.hpp",
})


def _stem_id(rel: Path) -> str:
    """The id of a file node: the same recipe as graphify (``_file_node_id``)."""
    return _make_id(_file_stem(rel))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _key(path: Path) -> str:
    """A lookup key for a path on disk: absolute, POSIX, lower case (Arma paths do not know case)."""
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        path = path.absolute()
    return path.as_posix().lower()


@dataclass
class Token:
    name: str
    line: int
    in_string: bool


@dataclass
class Function:
    name: str          # TAG_fnc_name as in the config
    rel: Path          # the .sqf file, relative to the scan root
    cfg_path: list[str]
    cfg_file: Path     # the config file on disk
    init: str | None = None
    description: str | None = None


@dataclass
class SqfReport:
    """What the project pass found. ``extract()`` logs the problems."""
    functions: int = 0
    externals: int = 0
    missing_files: list[str] = field(default_factory=list)
    missing_handlers: list[str] = field(default_factory=list)
    unknown_commands: dict[str, set[str]] = field(default_factory=dict)
    events: int = 0
    config_calls: int = 0
    script_calls: int = 0
    inline_functions: int = 0


# ---------------------------------------------------------------- the per-file pass

def extract_sqf(path: Path) -> dict:
    """The file node of a ``.sqf`` file. ``sqf_project_pass`` adds the functions and the edges."""
    if not path.is_file():
        return {"nodes": [], "edges": [], "error": f"cannot read {path}"}
    return {
        "nodes": [{"id": _make_id(_file_stem(path)), "label": path.name, "file_type": "code",
                   "source_file": str(path), "source_location": "L1"}],
        "edges": [],
    }


def refresh_sqf_paths(
    changed: list[Path], corpus: list[Path], invalidated: Sequence[Path] = (),
) -> list[Path]:
    """The files to extract again on an incremental scan.

    The SQF part comes from the whole mod: a change of one ``.sqf`` file, of a config file or of
    a C++ file with plugin commands can change the edges of all ``.sqf`` files. So when one of
    these files changed, the scan extracts all of them again (the cache keeps the per-file work).
    """
    def related(p: Path) -> bool:
        if p.suffix.lower() == ".sqf" or p.name.lower() in _SQF_CONFIG_NAMES:
            return True
        # A config file of an addon can call SQF functions (config_calls in sqf_project_pass).
        return p.suffix.lower() in _CONFIG_SUFFIXES and (mod_root_of(p) is not None or mission_root_of(p) is not None)

    def registers(p: Path) -> bool:
        if p.suffix.lower() not in _CPP_SUFFIXES:
            return False
        try:
            return "register_sqf_command" in _read(p)
        except OSError:
            return False

    if not any(p.suffix.lower() == ".sqf" for p in corpus):
        return changed
    if not any(related(p) or registers(p) for p in [*changed, *invalidated]):
        return changed
    extra = [p for p in corpus if related(p) or registers(p)]
    return list(dict.fromkeys([*changed, *extra]))


# ---------------------------------------------------------------- SQF text

def strip_comments(text: str) -> str:
    """The text with spaces in place of the comments (the lines stay). Strings stay."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            j = (text.find("\n", i) if text[i + 1] == "/" else text.find("*/", i + 2))
            j = n if j < 0 else (j if text[i + 1] == "/" else j + 2)
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def tokens(text: str) -> list[Token]:
    """The names of an SQF text without the comments. Strings: "..." and '...' with doubled quotes."""
    text = strip_comments(text)
    out: list[Token] = []
    i, n, line = 0, len(text), 1
    start, start_line = 0, 1

    def flush(end: int) -> None:
        segment = text[start:end]
        for m in IDENT.finditer(segment):
            out.append(Token(m.group(), start_line + segment.count("\n", 0, m.start()), False))

    while i < n:
        c = text[i]
        if c in "\"'":
            flush(i)
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            body = text[i + 1:j]
            for m in IDENT.finditer(body):
                out.append(Token(m.group(), line + body.count("\n", 0, m.start()), True))
            line += text.count("\n", i, j + 1)
            i = j + 1
            start, start_line = i, line
            continue
        if c == "\n":
            line += 1
        i += 1
    flush(n)
    return out


def header_description(text: str) -> str | None:
    """The first paragraph of the "Description:" part of the header comment."""
    m = re.search(r"Description:[ \t]*\n(.*?)(?:\*/|\Z)", text, re.S)
    if not m:
        return None
    words: list[str] = []
    for line in m.group(1).split("\n"):
        line = line.strip().lstrip("*").strip()  # " * text": the comment style of ACE and CBA
        if not line:
            if words:
                break
            continue
        words.extend(line.split())
    return " ".join(words) or None


def expand_macros(text: str, prefix: str | None, component: str | None) -> str:
    """FUNC(name), EFUNC(component, name), ACEFUNC(...) and their Q/D/LINK forms as the function names,
    GVAR(name) and EGVAR(component, name) as the variable names. A Q form gives a string. The lines stay."""
    if not prefix:
        return text

    def put(m: re.Match) -> str:
        quote, macro, a, b = m.group(1), m.group(2), m.group(3), m.group(4)
        if macro in ("FUNC", "DFUNC", "LINKFUNC") and b is None and component:
            name = f"{prefix}_{component}_fnc_{a}"
        elif macro in ("EFUNC", "DEFUNC") and b is not None:
            name = f"{prefix}_{a}_fnc_{b}"
        elif macro in ("ACEFUNC", "ACELINKFUNC") and b is not None:
            name = f"ace_{a}_fnc_{b}"
        else:
            return m.group(0)
        return f'"{name}"' if quote else name

    def put_var(m: re.Match) -> str:
        quote, macro, a, b = m.group(1), m.group(2), m.group(3), m.group(4)
        if macro == "GVAR" and b is None and component:
            name = f"{prefix}_{component}_{a}"
        elif macro == "EGVAR" and b is not None:
            name = f"{prefix}_{a}_{b}"
        else:
            return m.group(0)
        return f'"{name}"' if quote else name

    return GVAR_CALL.sub(put_var, MACRO_CALL.sub(put, text))


def addon_component(addon_dir: Path) -> str | None:
    path = addon_dir / "script_component.hpp"
    if not path.is_file():
        return None
    m = re.search(r"#define\s+COMPONENT\s+(\w+)", _read(path))
    return m.group(1) if m else None


def read_prep(mod_root: Path, prefix: str | None, sqf_lookup: dict[str, Path],
              ) -> tuple[list[Function], list[str]]:
    """The functions of addons/*/XEH_PREP.hpp: PREP(name) is <PREFIX>_<COMPONENT>_fnc_<name>
    in functions/fnc_<name>.sqf."""
    functions: list[Function] = []
    missing: list[str] = []
    if not prefix:
        return functions, missing
    for prep in sorted((mod_root / "addons").glob("*/XEH_PREP.hpp")):
        addon_dir = prep.parent
        component = addon_component(addon_dir) or addon_dir.name
        for m in PREP.finditer(strip_comments(_read(prep))):
            name = f"{prefix}_{component}_fnc_{m.group(1)}"
            rel = sqf_lookup.get(_key(addon_dir / "functions" / f"fnc_{m.group(1)}.sqf"))
            if rel is None:
                missing.append(name)
                continue
            functions.append(Function(name=name, rel=rel, cfg_path=[], cfg_file=prep))
    return functions, missing


# ---------------------------------------------------------------- CfgFunctions

@dataclass
class CfgClass:
    name: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["CfgClass"] = field(default_factory=list)


def parse_classes(text: str) -> list[CfgClass]:
    """The class tree of a config text (no #include, the macros stay as words)."""
    text = re.sub(r"^\s*#.*$", "", strip_comments(text), flags=re.M)
    toks = CFG_TOKEN.findall(text)
    pos = 0

    def parse_body() -> tuple[dict[str, str], list[CfgClass]]:
        nonlocal pos
        attrs: dict[str, str] = {}
        children: list[CfgClass] = []
        while pos < len(toks) and toks[pos] != "}":
            t = toks[pos]
            if t == "class" and pos + 1 < len(toks):
                name = toks[pos + 1]
                pos += 2
                if pos < len(toks) and toks[pos].startswith("("):
                    name += toks[pos]  # a macro: DOUBLES(TAG,COMPONENT)
                    pos += 1
                if pos < len(toks) and toks[pos] == ":":
                    pos += 2
                if pos < len(toks) and toks[pos] == "{":
                    pos += 1
                    a, c = parse_body()
                    pos += 1  # "}"
                    children.append(CfgClass(name, a, c))
                elif pos < len(toks) and toks[pos] == ";":
                    children.append(CfgClass(name))
                continue
            if pos + 1 < len(toks) and toks[pos + 1] == "=":
                value_parts: list[str] = []
                pos += 2
                while pos < len(toks) and toks[pos] not in (";", "}"):
                    value_parts.append(toks[pos])
                    pos += 1
                attrs[t.lower()] = "".join(value_parts)
                continue
            pos += 1
        return attrs, children

    roots: list[CfgClass] = []
    while pos < len(toks):
        _, children = parse_body()
        roots.extend(children)
        pos += 1
    return roots


def read_prefix(mod_root: Path) -> str | None:
    """PREFIX of addons/main/script_mod.hpp, else of any addons/*/script_mod.hpp."""
    paths = [mod_root / "addons" / "main" / "script_mod.hpp"] + sorted((mod_root / "addons").glob("*/script_mod.hpp"))
    for path in paths:
        if path.is_file():
            m = re.search(r"#define\s+PREFIX\s+(\w+)", _read(path))
            if m:
                return m.group(1)
    return None


def read_defines(paths: list[Path]) -> dict[str, str]:
    defines: dict[str, str] = {}
    for path in paths:
        if path.is_file():
            for m in DEFINE.finditer(_read(path)):
                defines.setdefault(m.group(1), m.group(2))
    return defines


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace('""', '"')
    return value


def arma_path(value: str, addon_dir: Path, mod_root: Path) -> Path | None:
    """The folder or file on disk for a config path: "\\z\\mod\\addons\\x\\functions" or QPATHTOF(functions)."""
    value = unquote(value)
    m = re.fullmatch(r"Q?PATHTO_?F\((.+)\)", value)
    if m:
        return addon_dir / m.group(1).replace("\\", "/")
    parts = [p for p in value.replace("/", "\\").split("\\") if p]
    lower = [p.lower() for p in parts]
    if "addons" in lower:
        i = lower.index("addons")
        return mod_root.joinpath(*parts[i:])
    if parts:
        return addon_dir.joinpath(*parts)
    return None


def read_config(path: Path, mod_root: Path, depth: int = 0) -> str:
    """The text of a config file with its #include "..." files in place (the paths of the mod only)."""
    text = _read(path)
    if depth > 8:
        return text

    def put(m: re.Match) -> str:
        target = arma_path(m.group(1), path.parent, mod_root)
        if target is not None and target.is_file():
            return read_config(target, mod_root, depth + 1)
        return ""

    return INCLUDE.sub(put, text)


def read_functions(mod_root: Path, sqf_lookup: dict[str, Path]) -> tuple[list[Function], list[str]]:
    """The functions of the CfgFunctions of each addon: addons/<addon>/CfgFunctions.hpp, else the
    class CfgFunctions of addons/<addon>/config.cpp. sqf_lookup: {_key(path): path relative to the scan root}."""
    functions: list[Function] = []
    missing: list[str] = []
    main_mod = mod_root / "addons" / "main" / "script_mod.hpp"
    sources: list[Path] = []
    addons = mod_root / "addons"
    for addon_dir in sorted(p for p in addons.iterdir() if p.is_dir()) if addons.is_dir() else []:
        if (addon_dir / "CfgFunctions.hpp").is_file():
            sources.append(addon_dir / "CfgFunctions.hpp")
        elif (addon_dir / "config.cpp").is_file() and "CfgFunctions" in _read(addon_dir / "config.cpp"):
            sources.append(addon_dir / "config.cpp")
    for cfg in sources:
        addon_dir = cfg.parent
        defines = read_defines([addon_dir / "script_component.hpp", addon_dir / "script_mod.hpp", main_mod])
        roots = parse_classes(read_config(cfg, mod_root))
        top = [r for r in roots if r.name.lower() == "cfgfunctions"]
        if top:
            prefix_path, tags = ["CfgFunctions"], [t for r in top for t in r.children]
        elif cfg.name.lower() == "config.cpp":
            continue
        else:
            prefix_path, tags = [], roots
        _tag_functions(tags, prefix_path, cfg, addon_dir, mod_root, defines, sqf_lookup, functions, missing)
    return functions, missing


def read_mission_functions(mission: Path, sqf_lookup: dict[str, Path]) -> tuple[list[Function], list[str]]:
    """The functions of the class CfgFunctions of <mission>/description.ext, with its #include files.
    In a mission, a "file" attribute and the default folder (functions/<category>) are relative to
    the mission folder."""
    functions: list[Function] = []
    missing: list[str] = []
    ext = mission / "description.ext"
    try:
        roots = parse_classes(read_config(ext, mission))
    except OSError:
        return functions, missing
    tags = [t for r in roots if r.name.lower() == "cfgfunctions" for t in r.children]
    _tag_functions(tags, ["CfgFunctions"], ext, mission, mission, {}, sqf_lookup, functions, missing)
    return functions, missing


def _tag_functions(tags: list[CfgClass], prefix_path: list[str], cfg: Path, addon_dir: Path, mod_root: Path,
                   defines: dict[str, str], sqf_lookup: dict[str, Path],
                   functions: list[Function], missing: list[str]) -> None:
    """The functions of the tag classes of one CfgFunctions (in place: ``functions``, ``missing``)."""
    addon_key = _key(addon_dir) + "/"
    for tag in tags:
        tag_name = unquote(tag.attrs.get("tag", tag.name))
        if tag_name == "ADDON" and "PREFIX" in defines and "COMPONENT" in defines:
            tag_name = f"{defines['PREFIX']}_{defines['COMPONENT']}"
        m = re.fullmatch(r"DOUBLES\((\w+),(\w+)\)", tag_name)
        if m:
            tag_name = f"{defines.get(m.group(1), m.group(1))}_{defines.get(m.group(2), m.group(2))}"
        for cat in tag.children:
            cat_dir = arma_path(cat.attrs["file"], addon_dir, mod_root) if "file" in cat.attrs else None
            for fn in cat.children:
                name = f"{tag_name}_fnc_{fn.name}"
                if "file" in fn.attrs:
                    target = arma_path(fn.attrs["file"], addon_dir, mod_root)
                elif cat_dir is not None:
                    target = cat_dir / f"fn_{fn.name}.sqf"
                else:
                    target = addon_dir / "functions" / cat.name / f"fn_{fn.name}.sqf"
                rel = sqf_lookup.get(_key(target)) if target is not None else None
                if rel is None:
                    # The file is somewhere else in the addon: take the only fn_<name>.sqf.
                    want = f"/fn_{fn.name.lower()}.sqf"
                    found = [v for k, v in sqf_lookup.items() if k.startswith(addon_key) and k.endswith(want)]
                    rel = found[0] if len(found) == 1 else None
                if rel is None:
                    missing.append(name)
                    continue
                flags = [f for f in ("preInit", "postInit", "preStart") if fn.attrs.get(f.lower()) == "1"]
                functions.append(Function(
                    name=name, rel=rel, cfg_path=prefix_path + [tag.name, cat.name, fn.name],
                    cfg_file=cfg, init=", ".join(flags) or None,
                    description=unquote(fn.attrs["description"]) if "description" in fn.attrs else None,
                ))


# ---------------------------------------------------------------- the project pass

_EVENT_NAME = re.compile(r"""\[\s*(["'])([\w.:-]+)\1""")


def _string_mask(text: str) -> bytearray:
    """1 for each character in a string ("..." or '...', doubled quotes), else 0."""
    mask = bytearray(len(text))
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            end = min(j, n - 1)
            mask[i:end + 1] = b"\x01" * (end - i + 1)
            i = j + 1
            continue
        i += 1
    return mask


def _block_end(text: str, mask: bytearray, start: int) -> int | None:
    """The position of the "}" that closes the "{" at ``start`` (strings skipped), else None."""
    depth = 0
    for j in range(start, len(text)):
        if mask[j]:
            continue
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return None


def _array_before(text: str, mask: bytearray, end: int) -> int | None:
    """The start of the array "[...]" just before ``end`` (spaces between), else None."""
    j = end - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    if j < 0 or text[j] != "]" or mask[j]:
        return None
    depth = 0
    while j >= 0:
        if not mask[j]:
            if text[j] == "]":
                depth += 1
            elif text[j] == "[":
                depth -= 1
                if depth == 0:
                    return j
        j -= 1
    return None


def mod_root_of(path: Path) -> Path | None:
    """The mod of a file: the folder that holds the last "addons" folder of its path."""
    parts = path.parts
    for i in range(len(parts) - 2, 0, -1):
        if parts[i].lower() == "addons":
            return Path(*parts[:i])
    return None


_MISSION_ROOTS: dict[Path, Path | None] = {}


def mission_root_of(path: Path) -> Path | None:
    """The mission of a file: the nearest folder above it with a description.ext."""
    folder = path.parent
    seen: list[Path] = []
    found: Path | None = None
    while True:
        if folder in _MISSION_ROOTS:
            found = _MISSION_ROOTS[folder]
            break
        seen.append(folder)
        if (folder / "description.ext").is_file():
            found = folder
            break
        if folder.parent == folder:
            break
        folder = folder.parent
    for f in seen:
        _MISSION_ROOTS[f] = found
    return found


def _relative(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root)
    except (ValueError, OSError, RuntimeError):
        return None


def sqf_project_pass(paths: Sequence[Path], root: Path, all_nodes: list[dict], all_edges: list[dict],
                     context_nodes: Sequence[dict] | None = None) -> SqfReport:
    """Add the SQF functions, the plugin commands and their edges to the extraction (in place).

    ``paths``: the files of this extraction. ``root``: the resolved scan root: the ``source_file``
    values and the ids of ``all_nodes`` are relative to it. ``context_nodes``: the nodes of the
    files that this run does not extract (an incremental rebuild), for the id lookups only.
    """
    report = SqfReport()
    new_nodes: list[dict] = []
    new_edges: list[dict] = []
    known = list(all_nodes) + list(context_nodes or [])
    known_ids = {n.get("id") for n in known}
    known_labels = {(str(n.get("source_file") or ""), n.get("label")): n.get("id") for n in known}

    absolute = [Path(p) if Path(p).is_absolute() else (root / p) for p in paths]
    sqf_files = sorted({p.resolve() for p in absolute if p.suffix.lower() == ".sqf" and p.is_file()})
    cpp_files = sorted({p.resolve() for p in absolute if p.suffix.lower() in _CPP_SUFFIXES and p.is_file()})

    # The plugin commands and their handlers.
    commands: dict[str, str] = {}  # name lower case -> node id
    for path in cpp_files:
        rel = _relative(path, root)
        if rel is None:
            continue
        try:
            text = _read(path)
        except OSError:
            continue
        if "register_sqf_command" not in text:
            continue
        stem = _stem_id(rel)
        for m in REGISTER.finditer(text):
            name, handler = m.group(1), m.group(2).split("::")[-1]
            line = text.count("\n", 0, m.start()) + 1
            cid = _make_id(stem, "sqf_command", name)
            commands[name.lower()] = cid
            new_nodes.append(_node(cid, name, rel, line, kind="sqf_command"))
            hid = _make_id(stem, handler)
            if hid not in known_ids:
                hid = known_labels.get((rel.as_posix(), handler + "()"), "")
            if hid:
                new_edges.append(_edge(cid, hid, "references", rel, line, "handler"))
            else:
                report.missing_handlers.append(f"{name} -> {handler}")

    # The SQF files.
    sqf_lookup: dict[str, Path] = {}  # _key(path) -> path relative to the scan root
    texts: dict[Path, str] = {}
    owner: dict[Path, str] = {}  # file -> the node of its code (the function, else the file)
    prefixes: dict[Path, str | None] = {}
    components: dict[Path, str | None] = {}
    mod_roots: list[Path] = []
    mission_roots: list[Path] = []
    _MISSION_ROOTS.clear()  # description.ext files can come and go between two builds

    def context_of(path: Path) -> tuple[str | None, str | None]:
        """PREFIX of the mod and COMPONENT of the addon of a file (for the macros)."""
        mod = mod_root_of(path)
        if mod is None:
            return None, None
        if mod not in prefixes:
            prefixes[mod] = read_prefix(mod)
            mod_roots.append(mod)
        component = None
        in_mod = path.relative_to(mod).parts
        if len(in_mod) > 2:  # addons/<addon>/.../<file>
            addon_dir = mod / in_mod[0] / in_mod[1]
            if addon_dir not in components:
                components[addon_dir] = addon_component(addon_dir)
            component = components[addon_dir]
        return prefixes[mod], component

    for path in sqf_files:
        rel = _relative(path, root)
        if rel is None:
            continue
        sqf_lookup[_key(path)] = rel
        prefix, component = context_of(path)
        if mod_root_of(path) is None:
            mission = mission_root_of(path)
            if mission is not None and mission not in mission_roots:
                mission_roots.append(mission)
        try:
            texts[rel] = expand_macros(_read(path), prefix, component)
        except OSError:
            continue
        fid = _stem_id(rel)
        if fid not in known_ids:
            new_nodes.append(_node(fid, path.name, rel, 1))
            known_ids.add(fid)
        owner[rel] = fid

    # The functions.
    functions_list: list[Function] = []
    for mod in mod_roots:
        found, missing = read_functions(mod, sqf_lookup)
        functions_list += found
        report.missing_files += missing
        found, missing = read_prep(mod, prefixes[mod], sqf_lookup)
        functions_list += found
        report.missing_files += missing
    for mission in mission_roots:
        found, missing = read_mission_functions(mission, sqf_lookup)
        functions_list += found
        report.missing_files += missing
    functions: dict[str, str] = {}  # name lower case -> node id
    fn_nodes: dict[str, dict] = {}  # name lower case -> node
    entries: dict[str, set[str]] = {}  # name lower case -> why the engine or another mod can call it
    for fn in functions_list:
        if fn.rel not in texts or fn.name.lower() in functions:
            continue
        stem = _stem_id(fn.rel)
        fid = _make_id(stem, fn.name)
        functions[fn.name.lower()] = fid
        extra: dict = {"kind": "sqf_function", "_callable": True}
        desc = header_description(texts[fn.rel]) or fn.description
        if desc:
            extra["description"] = desc
        if fn.init:
            extra["init"] = fn.init
        fn_nodes[fn.name.lower()] = _node(fid, fn.name, fn.rel, 1, **extra)
        new_nodes.append(fn_nodes[fn.name.lower()])
        new_edges.append(_edge(stem, fid, "contains", fn.rel, 1, "file"))
        why = entries.setdefault(fn.name.lower(), set())
        if fn.init:
            why.update(fn.init.split(", "))
        places = [p.lower() for p in fn.rel.parts[:-1]] + [c.lower() for c in fn.cfg_path[-2:-1]]
        if any(p in _TEST_NAMES for p in places) or re.search(r"_fnc_test[A-Z0-9_]", fn.name):
            why.add("test")
        owner[fn.rel] = fid
        cfg_rel = _relative(fn.cfg_file, root)
        if cfg_rel is not None:
            cfg_id = _make_id(_stem_id(cfg_rel), *fn.cfg_path)
            if cfg_id in known_ids:
                new_edges.append(_edge(cfg_id, fid, "defines", cfg_rel, 1, "cfgfunctions"))

    # The functions made in the code: TAG_fnc_name = { ... }; (common in missions). A CfgFunctions
    # function of the same name wins. The calls in the { ... } block belong to the function.
    inline_ranges: dict[Path, list[tuple[int, int, str]]] = {}  # file -> (first line, last line, node id)
    for rel, text in sorted(texts.items()):
        clean = strip_comments(text)
        mask = _string_mask(clean)
        for m in _INLINE_FUNCTION.finditer(clean):
            if mask[m.start()]:
                continue
            name = m.group(1)
            open_at = m.end() - 1
            close_at = _block_end(clean, mask, open_at)
            if close_at is None:
                continue
            first = clean.count("\n", 0, m.start()) + 1
            last = first + clean.count("\n", m.start(), close_at)
            low = name.lower()
            if low not in functions:
                stem = _stem_id(rel)
                fid = _make_id(stem, name)
                functions[low] = fid
                fn_nodes[low] = _node(fid, name, rel, first, kind="sqf_function", _callable=True,
                                      defined="assignment")
                new_nodes.append(fn_nodes[low])
                new_edges.append(_edge(stem, fid, "contains", rel, first, "file"))
                report.inline_functions += 1
            if fn_nodes.get(low, {}).get("source_file") == rel.as_posix():
                inline_ranges.setdefault(rel, []).append((first, last, functions[low]))
    report.functions = len(functions)

    def owner_at(rel: Path, line: int) -> str:
        """The node of the code at a line: the innermost function made in the code, else the file owner."""
        best = None
        for first, last, fid in inline_ranges.get(rel, ()):
            if first <= line <= last and (best is None or last - first < best[1] - best[0]):
                best = (first, last, fid)
        return best[2] if best else owner[rel]

    # The variables made with compile "<command> ...": variable lower case -> command lower case.
    wrappers: dict[str, str] = {}
    for text in texts.values():
        for m in WRAPPER.finditer(text):
            if m.group(2).lower() in commands:
                wrappers[m.group(1).lower()] = m.group(2).lower()
    command_prefixes = {c.split("_")[0] + "_" for c in commands}

    # The uses.
    externals: dict[str, str] = {}
    for rel, text in texts.items():
        seen: set[tuple[str, str, str]] = set()
        for tok in tokens(text):
            low = tok.name.lower()
            source = owner_at(rel, tok.line)
            target, relation, context = None, None, None
            if low in functions:
                target = functions[low]
                relation, context = ("references", "string") if tok.in_string else ("calls", "call")
            elif low in commands:
                target, relation, context = commands[low], "references", "plugin_command"
            elif low in wrappers:
                target, relation, context = commands[wrappers[low]], "references", "plugin_command"
            elif FNC_NAME.match(tok.name) and not low.endswith("_fnc_"):
                # A function of another mod (CBA, BIS, ...). A name that ends with "_fnc_" is the start
                # of a name made in the code ("..._fnc_" + _task).
                if low not in externals:
                    externals[low] = _make_id("sqf_external", low)
                    new_nodes.append({"id": externals[low], "label": tok.name, "file_type": "code",
                                      "source_file": "", "source_location": None, "_origin": "ast",
                                      "kind": "sqf_external"})
                target = externals[low]
                relation, context = ("references", "string") if tok.in_string else ("calls", "external_call")
            elif commands and tok.name.islower() and any(low.startswith(p) for p in command_prefixes) \
                    and "_fnc_" not in low and low.count("_") >= 2:
                # A lower case name like the plugin commands, but no command has it.
                report.unknown_commands.setdefault(low, set()).add(rel.as_posix())
            if not target or target == source:
                continue
            key = (source, target, relation)
            if key in seen:
                continue
            seen.add(key)
            e = _edge(source, target, relation, rel, tok.line, context)
            if tok.in_string:
                e["confidence"], e["confidence_score"] = "INFERRED", 0.95
            new_edges.append(e)
            if context == "call" and rel.name.lower().startswith("xeh_"):
                entries.setdefault(low, set()).add("xeh")
            elif context == "call" and rel.name.lower() in _MISSION_EVENT_SCRIPTS:
                entries.setdefault(low, set()).add("mission")
    report.externals = len(externals)

    # The config files of the addons: the code in them (UI events, ACE actions, ...) calls functions.
    for path in sorted({p.resolve() for p in absolute if p.suffix.lower() in _CONFIG_SUFFIXES}):
        name = path.name.lower()
        if name in ("cfgfunctions.hpp", "xeh_prep.hpp") or name.startswith("script_") or not path.is_file() \
                or (mod_root_of(path) is None and mission_root_of(path) is None):
            continue
        rel = _relative(path, root)
        if rel is None or _stem_id(rel) not in known_ids:
            continue
        try:
            text = expand_macros(_read(path), *context_of(path))
        except OSError:
            continue
        fid, done = _stem_id(rel), set()
        for tok in tokens(text):
            low = tok.name.lower()
            if low in functions and low not in done:
                done.add(low)
                new_edges.append(_edge(fid, functions[low], "calls", rel, tok.line, "config"))
                entries.setdefault(low, set()).add("config")
                report.config_calls += 1

    # The functions in the README files (the API of the mod, for other mods).
    for mod in [*mod_roots, *mission_roots]:
        for md in [*sorted(mod.glob("*.md")), *sorted((mod / "addons").glob("*/*.md"))]:
            try:
                text = _read(md)
            except OSError:
                continue
            for m in IDENT.finditer(text):
                if m.group().lower() in functions:
                    entries.setdefault(m.group().lower(), set()).add("readme")

    # The scripts that a file runs: execVM "x.sqf", preprocessFileLineNumbers "x.sqf", compileScript ["x.sqf"].
    # A mission path is relative to the mission folder, a mod path names the addon (\\z\\mod\\addons\\...).
    for rel, text in texts.items():
        clean = strip_comments(text)
        path = (root / rel).resolve()
        mission = mission_root_of(path) if mod_root_of(path) is None else None
        base = mission or mod_root_of(path) or path.parent
        done_scripts: set[str] = set()
        for m in _SCRIPT_LOAD.finditer(clean):
            script = m.group(2) or m.group(3)
            target = arma_path(script, base, base)
            target_rel = sqf_lookup.get(_key(target)) if target is not None else None
            if target_rel is None:
                target_rel = sqf_lookup.get(_key(path.parent / script.replace("\\", "/")))
            if target_rel is None or target_rel == rel:
                continue
            dst = owner.get(target_rel)
            if dst and dst not in done_scripts:
                done_scripts.add(dst)
                new_edges.append(_edge(owner[rel], dst, "calls", rel, clean.count("\n", 0, m.start()) + 1, "script"))
                report.script_calls += 1

    # The CBA events: ["name", ...] call CBA_fnc_localEvent -> the event -> the handlers of
    # ["name", {...}] call CBA_fnc_addEventHandler (the functions in the handler, else its owner).
    events: dict[str, str] = {}
    for rel, text in texts.items():
        clean = strip_comments(text)
        mask = _string_mask(clean)
        done_edges: set[tuple[str, str]] = set()
        for m in _EVENT_CALL.finditer(clean):
            kind = m.group(1).lower()
            if mask[m.start()] or (kind not in _EVENT_RAISERS and kind not in _EVENT_ADDERS):
                continue
            start = _array_before(clean, mask, m.start())
            if start is None:
                continue
            array = clean[start:m.start()]
            first = _EVENT_NAME.match(array)
            if not first:
                continue  # a name made in the code
            event = first.group(2)
            if event.lower() not in events:
                events[event.lower()] = _make_id("sqf_event", event.lower())
                new_nodes.append({"id": events[event.lower()], "label": event, "file_type": "code",
                                  "source_file": "", "source_location": None, "_origin": "ast",
                                  "kind": "sqf_event"})
            eid = events[event.lower()]
            line = clean.count("\n", 0, m.start()) + 1
            if kind in _EVENT_RAISERS:
                pairs = [(owner[rel], eid, "event_raise")]
            else:
                handlers = [functions[w.lower()] for w in IDENT.findall(array[first.end():])
                            if w.lower() in functions]
                pairs = [(eid, h, "event_handler") for h in (dict.fromkeys(handlers) or [owner[rel]])]
            for src, dst, context in pairs:
                if (src, dst) not in done_edges:
                    done_edges.add((src, dst))
                    new_edges.append(_edge(src, dst, "calls", rel, line, context))
    report.events = len(events)

    for low, why in entries.items():
        if why and low in fn_nodes:
            fn_nodes[low]["entry"] = ", ".join(sorted(why))

    all_nodes.extend(new_nodes)
    all_edges.extend(new_edges)
    for text, items in (("CfgFunctions function without a file", report.missing_files),
                        ("plugin command without a handler", report.missing_handlers)):
        if items:
            _LOG.warning("SQF: %s (%d): %s%s", text, len(items), ", ".join(items[:30]),
                         " ..." if len(items) > 30 else "")
    return report


# ---------------------------------------------------------------- the engine calls of the C++ plugin

_ENGINE_CALL = re.compile(r"^(?:::)?(?:intercept::)?sqf::(\w+)$")


def _bare(label: str) -> str:
    """``S2::q()`` -> ``q``, ``.m()`` -> ``m``: the name of a function node, lower case."""
    return re.split(r"::|\.", label.removesuffix("()"))[-1].lower()


def _declarator_name(definition, source: bytes) -> str:
    """The name of a C++ function_definition (the last part of a qualified name), lower case."""
    node = definition.child_by_field_name("declarator")
    while node is not None and node.type not in ("function_declarator",):
        node = node.child_by_field_name("declarator")
    if node is None:
        return ""
    name = node.child_by_field_name("declarator")
    text = source[name.start_byte:name.end_byte].decode("utf-8", "replace") if name is not None else ""
    return re.split(r"::", text)[-1].strip().lower()


def engine_call_pass(paths: Sequence[Path], root: Path, all_nodes: list[dict], all_edges: list[dict]) -> int:
    """Add a ``calls`` edge (context ``engine_call``) from each C++ function to each SQF engine
    command that it calls through Intercept (``sqf::line_intersects_surfaces(...)``).

    A call in a lambda counts for the function that holds the lambda. A call outside a function
    gives an edge from the file node. Each command gets one node, kind ``sqf_engine``. Comments
    and strings do not count. Returns the number of the new edges.
    """
    absolute = [Path(p) if Path(p).is_absolute() else (root / p) for p in paths]
    cpp_files = sorted({p.resolve() for p in absolute if p.suffix.lower() in _CPP_SUFFIXES and p.is_file()})
    jobs: list[tuple[Path, bytes]] = []
    for path in cpp_files:
        try:
            source = path.read_bytes()
        except OSError:
            continue
        if b"sqf::" in source:
            jobs.append((path, source))
    if not jobs:
        return 0
    try:
        import tree_sitter_cpp
        from tree_sitter import Language, Parser
    except ImportError:
        _LOG.warning("SQF engine calls: tree_sitter_cpp is not installed, skipping")
        return 0
    parser = Parser(Language(tree_sitter_cpp.language()))

    by_line, by_name = _function_index(all_nodes)
    known_ids = {n.get("id") for n in all_nodes}

    engines: dict[str, str] = {}
    count = 0
    for path, source in jobs:
        rel = _relative(path, root)
        if rel is None:
            continue
        file_id = _stem_id(rel)
        tree = parser.parse(source)
        seen: set[tuple[str, str]] = set()
        owner_of = _owner_resolver(rel.as_posix(), source, by_line, by_name)

        stack = [(tree.root_node, None)]
        while stack:
            node, definition = stack.pop()
            if node.type == "function_definition":
                definition = node
            elif node.type == "call_expression":
                fn = node.child_by_field_name("function")
                text = source[fn.start_byte:fn.end_byte].decode("utf-8", "replace") if fn is not None else ""
                m = _ENGINE_CALL.match(re.sub(r"\s+", "", text))
                if m:
                    command = m.group(1)
                    source_id = (owner_of(definition) if definition is not None else "") or file_id
                    if source_id in known_ids:
                        low = command.lower()
                        if low not in engines:
                            engines[low] = _make_id("sqf_engine", low)
                            all_nodes.append({"id": engines[low], "label": f"sqf::{command}", "file_type": "code",
                                              "source_file": "", "source_location": None, "_origin": "ast",
                                              "kind": "sqf_engine"})
                            known_ids.add(engines[low])
                        if (source_id, engines[low]) not in seen:
                            seen.add((source_id, engines[low]))
                            all_edges.append(_edge(source_id, engines[low], "calls", rel,
                                                   node.start_point[0] + 1, "engine_call"))
                            count += 1
            stack.extend((child, definition) for child in reversed(node.children))
    return count


# ---------------------------------------------------------------- the global writes of the C++ plugin

_HEADER_SUFFIXES = frozenset({".h", ".hpp", ".hh", ".hxx"})
# The methods that change the object they are called on (containers, atomics, smart pointers).
_MUTATORS = frozenset({
    "push_back", "emplace_back", "push_front", "emplace_front", "pop_back", "pop_front",
    "insert", "emplace", "emplace_hint", "try_emplace", "insert_or_assign", "erase", "clear",
    "resize", "reserve", "shrink_to_fit", "assign", "swap", "reset", "release", "store", "exchange",
    "fetch_add", "fetch_sub", "fetch_and", "fetch_or", "fetch_xor", "push", "pop", "splice", "merge",
    "sort", "unique", "remove", "remove_if", "append", "replace", "fill", "notify_one", "notify_all",
})
# The parents in a C++ tree that hold namespace-level declarations.
_SCOPE_TYPES = frozenset({"translation_unit", "declaration_list", "linkage_specification"})


def _declared_name(node, source: bytes) -> tuple[str, bool]:
    """(name, is a function) of one declarator of a declaration."""
    while node is not None:
        if node.type == "function_declarator":
            return "", True
        if node.type in ("identifier", "field_identifier"):
            return source[node.start_byte:node.end_byte].decode("utf-8", "replace"), False
        if node.type == "qualified_identifier":
            return "", False  # Foo::bar = ...: a static member, not a global
        node = node.child_by_field_name("declarator") or (
            node.named_children[0] if node.type in ("parenthesized_declarator",) and node.named_children else None)
    return "", False


def _written_base(node, source: bytes) -> str:
    """The variable that a write changes: x in x = 1, x.a.b = 1, x[i] = 1, *x = 1, x.at(i) = 1."""
    while node is not None:
        if node.type == "identifier":
            return source[node.start_byte:node.end_byte].decode("utf-8", "replace")
        if node.type == "qualified_identifier":
            name = node.child_by_field_name("name")
            if name is not None and name.type == "identifier" and \
                    source[node.start_byte:node.start_byte + 5] != b"std::":
                return source[name.start_byte:name.end_byte].decode("utf-8", "replace")
            return ""
        if node.type in ("field_expression", "subscript_expression", "pointer_expression"):
            node = node.child_by_field_name("argument")
        elif node.type == "parenthesized_expression":
            node = node.named_children[0] if node.named_children else None
        elif node.type == "call_expression":  # x.at(i) = 1
            fn = node.child_by_field_name("function")
            node = fn.child_by_field_name("argument") if fn is not None and fn.type == "field_expression" else None
        else:
            return ""
    return ""


def global_write_pass(paths: Sequence[Path], root: Path, all_nodes: list[dict], all_edges: list[dict]) -> int:
    """Add a ``writes`` edge (context ``global_write``) from each C++ function to each namespace-level
    variable that it changes: ``x = ...``, ``x += ...``, ``x++``, ``x[i] = ...``, ``x.push_back(...)``
    and the other mutating methods. A write in a lambda counts for the function that holds it.

    The pass runs only for an Intercept plugin (a C++ file of the scan calls ``sqf::`` or
    ``register_sqf_command``). Each variable gets one node, kind ``cpp_global``. A local
    variable, a parameter or a class field with the same name hides the global: the pass then
    gives no edge. The name match does not know the types, so the edges are INFERRED.
    Returns the number of the new edges.
    """
    absolute = [Path(p) if Path(p).is_absolute() else (root / p) for p in paths]
    files = sorted({p.resolve() for p in absolute
                    if p.suffix.lower() in _CPP_SUFFIXES | _HEADER_SUFFIXES and p.is_file()})
    files = [p for p in files if mod_root_of(p) is None]  # not the configs of the addons
    sources: dict[Path, bytes] = {}
    for path in files:
        try:
            sources[path] = path.read_bytes()
        except OSError:
            continue
    if not any(b"sqf::" in s or b"register_sqf_command" in s for s in sources.values()):
        return 0
    try:
        import tree_sitter_cpp
        from tree_sitter import Language, Parser
    except ImportError:
        _LOG.warning("C++ global writes: tree_sitter_cpp is not installed, skipping")
        return 0
    parser = Parser(Language(tree_sitter_cpp.language()))
    trees = {path: parser.parse(src) for path, src in sources.items()}

    # 1. The globals: the variables declared at the namespace level.
    defined: dict[str, list[tuple[Path, int, bool]]] = {}  # name -> (file, line, extern)
    for path, tree in trees.items():
        src = sources[path]
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in ("namespace_definition",):
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append(body)
                continue
            if node.type in _SCOPE_TYPES:
                stack.extend(node.children)
                continue
            if node.type != "declaration" or node.parent is None or node.parent.type not in _SCOPE_TYPES:
                continue
            is_extern = any(c.type == "storage_class_specifier" and src[c.start_byte:c.end_byte] == b"extern"
                            for c in node.children)
            for decl in node.children_by_field_name("declarator"):
                name, is_function = _declared_name(decl, src)
                if name and not is_function:
                    defined.setdefault(name, []).append((path, decl.start_point[0] + 1, is_extern))
    if not defined:
        return 0

    globals_by_file: dict[tuple[Path, str], str] = {}
    unique: dict[str, str] = {}
    new_nodes: dict[str, dict] = {}
    for name, places in defined.items():
        defs = [p for p in places if not p[2]] or places
        ids = []
        for path, line, _ext in defs:
            rel = _relative(path, root)
            if rel is None:
                continue
            gid = _make_id(_stem_id(rel), "global", name)
            if gid not in new_nodes:
                new_nodes[gid] = _node(gid, name, rel, line, kind="cpp_global")
            ids.append(gid)
            globals_by_file[(path, name)] = gid
        for path, _line, _ext in places:  # an extern declaration names the same variable
            if (path, name) not in globals_by_file and len(set(ids)) == 1:
                globals_by_file[(path, name)] = ids[0]
        if len(set(ids)) == 1:
            unique[name] = ids[0]

    # The fields of all classes of the scan: in a method, a field hides a global with its name
    # (the class can be in a header and its methods in a .cpp file).
    fields: set[str] = set()
    for path, tree in trees.items():
        for node in _walk(tree.root_node):
            if node.type == "field_declaration":
                for decl in node.children_by_field_name("declarator"):
                    name, _f = _declared_name(decl, sources[path])
                    if name:
                        fields.add(name)

    # 2. The writes in the function bodies.
    by_line, by_name = _function_index(all_nodes)
    known_ids = {n.get("id") for n in all_nodes}
    used: set[str] = set()
    count = 0
    for path, tree in trees.items():
        if path.suffix.lower() not in _CPP_SUFFIXES | _HEADER_SUFFIXES:
            continue
        rel = _relative(path, root)
        if rel is None:
            continue
        src = sources[path]
        owner_of = _owner_resolver(rel.as_posix(), src, by_line, by_name)
        seen: set[tuple[str, str]] = set()
        for definition in _walk(tree.root_node):
            if definition.type != "function_definition" or _inside(definition, "function_definition"):
                continue
            owner = owner_of(definition)
            if not owner or owner not in known_ids:
                continue
            locals_: set[str] = set()
            for node in _walk(definition):
                if node.type in ("parameter_declaration", "optional_parameter_declaration", "declaration",
                                 "for_range_loop", "init_declarator"):
                    for decl in node.children_by_field_name("declarator"):
                        name, _f = _declared_name(decl, src)
                        if name:
                            locals_.add(name)
                elif node.type == "structured_binding_declarator":
                    locals_.update(src[c.start_byte:c.end_byte].decode() for c in node.named_children
                                   if c.type == "identifier")
            is_method = definition.parent is not None and definition.parent.type == "field_declaration_list"
            declarator = definition.child_by_field_name("declarator")
            if declarator is not None and b"::" in src[declarator.start_byte:declarator.end_byte]:
                is_method = True
            for node in _walk(definition):
                target = None
                if node.type == "assignment_expression":
                    target = node.child_by_field_name("left")
                elif node.type == "update_expression":
                    target = node.child_by_field_name("argument")
                elif node.type == "call_expression":
                    fn = node.child_by_field_name("function")
                    if fn is not None and fn.type == "field_expression":
                        method = fn.child_by_field_name("field")
                        if method is not None and src[method.start_byte:method.end_byte].decode() in _MUTATORS:
                            target = fn.child_by_field_name("argument")
                if target is None:
                    continue
                name = _written_base(target, src)
                if not name or name in locals_ or (is_method and name in fields):
                    continue
                gid = globals_by_file.get((path, name)) or unique.get(name)
                if gid is None or (owner, gid) in seen:
                    continue
                seen.add((owner, gid))
                used.add(gid)
                e = _edge(owner, gid, "writes", rel, node.start_point[0] + 1, "global_write")
                e["confidence"], e["confidence_score"] = "INFERRED", 0.95
                all_edges.append(e)
                count += 1
    all_nodes.extend(n for gid, n in new_nodes.items() if gid in used)
    return count


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _inside(node, node_type: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == node_type:
            return True
        parent = parent.parent
    return False


def _function_index(all_nodes: list[dict]) -> tuple[dict, dict]:
    """The function nodes of each file: by (file, line of the definition) and by (file, bare name)."""
    by_line: dict[tuple[str, int], list[dict]] = {}
    by_name: dict[tuple[str, str], list[str]] = {}
    for n in all_nodes:
        sf, loc, label = n.get("source_file") or "", n.get("source_location") or "", str(n.get("label") or "")
        if not sf or not label.endswith("()") or not str(loc).startswith("L"):
            continue
        try:
            line = int(str(loc)[1:])
        except ValueError:
            continue
        by_line.setdefault((sf, line), []).append(n)
        by_name.setdefault((sf, _bare(label)), []).append(n["id"])
    return by_line, by_name


def _owner_resolver(sf: str, source: bytes, by_line: dict, by_name: dict):
    """A function: function_definition -> the id of its graph node ("" when it is not clear)."""
    resolved: dict[int, str] = {}

    def owner_of(definition) -> str:
        key = definition.id
        if key not in resolved:
            name = _declarator_name(definition, source)
            line = definition.start_point[0] + 1
            found = [n["id"] for n in by_line.get((sf, line), []) if _bare(n["label"]) == name]
            if not found:
                found = by_name.get((sf, name), [])
            resolved[key] = found[0] if len(set(found)) == 1 else ""
        return resolved[key]

    return owner_of


def wants_directed(nodes) -> bool:
    """True when an extraction has SQF functions: its graph must be directed.

    An undirected graph keeps one edge for a pair of nodes, so of two functions that call each
    other (A -> B and B -> A) only one direction stays, and the other function looks uncalled
    (docs/a3/graph-bugs.md, item 1).
    """
    return any(n.get("kind") == "sqf_function" for n in nodes)


_CALLER_RELATIONS = frozenset({"calls", "references", "indirect_call"})


def uncalled_sqf_functions(G) -> list[tuple[str, str]]:
    """The SQF functions that nothing calls and that are not an entry point: (label, source_file).

    An entry point is a function that the engine or another mod can call (the ``entry``
    attribute: preInit / postInit, XEH, config, README, test). The rest can be dead code.
    """
    called: set[str] = set()
    directed = G.is_directed()
    for u, v, data in G.edges(data=True):
        if data.get("relation") not in _CALLER_RELATIONS:
            continue
        src, dst = (u, v) if directed else (data.get("_src", u), data.get("_tgt", v))
        if src != dst:
            called.add(dst)
    out = []
    for nid, data in G.nodes(data=True):
        if data.get("kind") == "sqf_function" and not data.get("entry") and nid not in called:
            out.append((str(data.get("label") or nid), str(data.get("source_file") or "")))
    return sorted(out, key=lambda item: item[0].lower())


def _node(nid: str, label: str, rel: Path, line: int, **extra) -> dict:
    return {"id": nid, "label": label, "file_type": "code", "source_file": rel.as_posix(),
            "source_location": f"L{line}", "_origin": "ast", **extra}


def _edge(source: str, target: str, relation: str, rel: Path, line: int, context: str) -> dict:
    return {
        "source": source, "target": target, "relation": relation, "confidence": "EXTRACTED",
        "confidence_score": 1.0, "source_file": rel.as_posix(), "source_location": f"L{line}",
        "context": context, "weight": 1.0, "_origin": "ast",
    }
