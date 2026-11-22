"""``graphify callers`` and ``graphify callees``: the call tree of one node.

``affected`` gives a flat list of the nodes that a change touches. These two
commands give the call tree itself, in the two directions, with the file and the
line of each call site:

    graphify callees DD_TA_task_fnc_defend --depth 0
    graphify callers DD_TA_query_fnc_visible --depth 3 --kind sqf_function

A node that the tree already shows is not expanded again: the line ends with
``(see above)``. ``--depth 0`` has no depth limit. ``--kind K`` shows only the
nodes of that kind (``sqf_function``, ``sqf_command``, ``sqf_engine``, ...),
but the walk goes through all nodes.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import networkx as nx

# The relations of a call tree. "references" holds the SQF plugin commands, the
# C++ handlers and the function names in strings, "writes" the changes of the C++
# globals (graphify/extractors/sqf.py).
CALL_RELATIONS = ("calls", "indirect_call", "references", "writes")
# A "references" edge with one of these contexts is the use of a type, not a call.
TYPE_CONTEXTS = frozenset({"parameter_type", "return_type", "generic_arg", "type", "field", "decorator"})


@dataclass
class CallNode:
    node_id: str
    relation: str = ""
    context: str = ""
    site: str = ""          # file:line of the call, in the file of the caller
    children: list["CallNode"] = field(default_factory=list)
    repeated: bool = False  # the tree shows this node above


def _edges(graph: nx.DiGraph, node: str, direction: str):
    if direction == "callees":
        for _src, other, data in graph.out_edges(node, data=True):
            yield other, data
    else:
        for other, _dst, data in graph.in_edges(node, data=True):
            yield other, data


def call_tree(graph: nx.DiGraph, seed: str, *, direction: str = "callees", depth: int = 1,
              relations: Iterable[str] = CALL_RELATIONS,
              skip_contexts: Iterable[str] = TYPE_CONTEXTS) -> CallNode:
    """The call tree of ``seed``. ``direction``: "callees" or "callers". ``depth`` 0: no limit.
    The edges with a context in ``skip_contexts`` do not count."""
    if direction not in ("callees", "callers"):
        raise ValueError(f"direction must be 'callees' or 'callers', not {direction!r}")
    relation_set = set(relations)
    skip = set(skip_contexts)
    root = CallNode(seed)
    shown = {seed}

    def grow(item: CallNode, level: int) -> None:
        if depth and level >= depth:
            return
        found: list[tuple[str, dict]] = []
        seen_here: set[str] = set()
        for other, data in _edges(graph, item.node_id, direction):
            other = str(other)
            if str(data.get("relation", "")) not in relation_set or other == item.node_id:
                continue
            if data.get("context") in skip:
                continue
            if other in seen_here:
                continue
            seen_here.add(other)
            found.append((other, data))
        found.sort(key=lambda pair: str(graph.nodes[pair[0]].get("label") or pair[0]).lower())
        for other, data in found:
            site_file = str(data.get("source_file") or "")
            site_loc = str(data.get("source_location") or "")
            child = CallNode(other, str(data.get("relation", "")), str(data.get("context") or ""),
                             f"{site_file}:{site_loc}" if site_file and site_loc else site_file)
            item.children.append(child)
            if other in shown:
                child.repeated = True
                continue
            shown.add(other)
            grow(child, level + 1)

    grow(root, 0)
    return root


def format_call_tree(graph: nx.DiGraph, tree: CallNode, *, direction: str, depth: int,
                     kinds: Iterable[str] = ()) -> str:
    """The tree as text: one node on each line, indented by its level."""
    kind_set = set(kinds)

    def label(node_id: str) -> str:
        return str(graph.nodes[node_id].get("label") or node_id)

    def where(node_id: str) -> str:
        data = graph.nodes[node_id]
        src = data.get("source_file") or ""
        loc = data.get("source_location") or ""
        return f"{src}:{loc}" if src and loc else str(src)

    title = "Callees of" if direction == "callees" else "Callers of"
    lines = [f"{title} {label(tree.node_id)} ({where(tree.node_id) or '-'})",
             f"Depth: {depth or 'no limit'}" + (f" · kind: {', '.join(sorted(kind_set))}" if kind_set else "")]
    count = 0

    def show(item: CallNode, level: int) -> None:
        nonlocal count
        for child in item.children:
            visible = not kind_set or graph.nodes[child.node_id].get("kind") in kind_set
            if kind_set and child.repeated:
                visible = False  # the filtered list shows each node once
            next_level = level
            if visible:
                count += 1
                kind = graph.nodes[child.node_id].get("kind")
                tag = child.context or child.relation
                text = f"{'  ' * level}- {label(child.node_id)} [{tag}]"
                if kind and not kind_set:
                    text += f" ({kind})"
                text += f" {child.site or where(child.node_id) or '-'}"
                if child.repeated:
                    text += " (see above)"
                lines.append(text)
                next_level = level + 1
            show(child, next_level)

    show(tree, 0)
    if not count:
        lines.append("No callees found." if direction == "callees" else "No callers found.")
    return "\n".join(lines)
