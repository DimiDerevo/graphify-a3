"""SQF (Arma 3) extraction: the per-file pass and the project pass (graphify/extractors/sqf.py)."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from graphify.detect import CODE_EXTENSIONS
from graphify.extract import collect_files, extract
from graphify.extractors.sqf import extract_sqf, refresh_sqf_paths, sqf_project_pass

MOD = Path(__file__).parent / "fixtures" / "sqf_mod"
SQF_CONTEXTS = {"call", "external_call", "plugin_command", "handler", "cfgfunctions", "file", "string",
                "config", "event_raise", "event_handler", "engine_call", "global_write", "script"}


def _extract(root: Path, cache: Path) -> dict:
    return extract(collect_files(root), cache_root=cache, root=root)


@pytest.fixture(scope="module")
def mod_graph(tmp_path_factory) -> dict:
    return _extract(MOD.resolve(), tmp_path_factory.mktemp("cache"))


def _nodes(result: dict) -> dict[str, dict]:
    return {n["label"]: n for n in result["nodes"]}


def _edges(result: dict) -> set[tuple[str, str, str, str]]:
    """(source label, target label, relation, context) of the SQF edges."""
    label = {n["id"]: n["label"] for n in result["nodes"]}
    return {(label.get(e["source"], e["source"]), label.get(e["target"], e["target"]),
             e["relation"], e.get("context")) for e in result["edges"] if e.get("context") in SQF_CONTEXTS}


def test_sqf_is_a_code_extension():
    assert ".sqf" in CODE_EXTENSIONS
    assert MOD / "addons" / "main" / "functions" / "fn_init.sqf" in collect_files(MOD)


def test_per_file_pass_gives_the_file_node():
    r = extract_sqf(MOD / "addons" / "main" / "functions" / "fn_helper.sqf")
    assert r.get("error") is None
    assert [n["label"] for n in r["nodes"]] == ["fn_helper.sqf"]
    assert r["edges"] == []


def test_cfgfunctions_and_prep_functions(mod_graph):
    nodes = _nodes(mod_graph)
    for name in ("TST_main_fnc_init", "TST_main_fnc_helper", "TST_util_fnc_prepped", "TST_util_fnc_other"):
        assert nodes[name]["kind"] == "sqf_function", name
        assert nodes[name]["_callable"] is True
    assert nodes["TST_main_fnc_init"]["source_file"] == "addons/main/functions/fn_init.sqf"
    assert nodes["TST_main_fnc_init"]["init"] == "preInit"
    # The header comment wins over the config attribute. " * " lines (ACE / CBA style) are text.
    assert nodes["TST_main_fnc_init"]["description"] == "Starts the mod and the plugin."
    assert nodes["TST_main_fnc_helper"]["description"] == "Helps the init."
    edges = _edges(mod_graph)
    assert ("fn_init.sqf", "TST_main_fnc_init", "contains", "file") in edges
    assert ("init", "TST_main_fnc_init", "defines", "cfgfunctions") in edges
    assert ("XEH_PREP.hpp", "TST_util_fnc_prepped", "defines", "cfgfunctions") in edges


def test_calls_between_functions(mod_graph):
    edges = _edges(mod_graph)
    assert ("TST_main_fnc_init", "TST_main_fnc_helper", "calls", "call") in edges
    # EFUNC(util,prepped) and FUNC(other) are the function names.
    assert ("TST_main_fnc_init", "TST_util_fnc_prepped", "calls", "call") in edges
    assert ("TST_util_fnc_prepped", "TST_util_fnc_other", "calls", "call") in edges
    # The code outside a function: an edge from the file node.
    assert ("XEH_preInit.sqf", "TST_util_fnc_prepped", "calls", "call") in edges


def test_a_name_in_a_string_is_inferred(mod_graph):
    label = {n["id"]: n["label"] for n in mod_graph["nodes"]}
    strings = [e for e in mod_graph["edges"] if e.get("context") == "string"]
    pairs = {(label[e["source"]], label[e["target"]]) for e in strings}
    assert ("TST_main_fnc_init", "TST_main_fnc_helper") in pairs
    assert ("TST_util_fnc_prepped", "TST_util_fnc_other") in pairs  # QFUNC(other)
    assert all(e["confidence"] == "INFERRED" for e in strings)


def test_comments_do_not_count(mod_graph):
    labels = {n["label"].lower() for n in mod_graph["nodes"]}
    assert "tst_main_fnc_ghost" not in labels


def test_functions_of_other_mods(mod_graph):
    nodes = _nodes(mod_graph)
    for name in ("CBA_fnc_addEventHandler", "CBA_fnc_globalEvent", "BIS_fnc_log"):
        assert nodes[name]["kind"] == "sqf_external", name
    edges = _edges(mod_graph)
    assert ("TST_main_fnc_init", "CBA_fnc_addEventHandler", "calls", "external_call") in edges
    assert ("TST_util_fnc_prepped", "BIS_fnc_log", "calls", "external_call") in edges


def test_plugin_commands_and_handlers(mod_graph):
    nodes = _nodes(mod_graph)
    assert nodes["tst_visible"]["kind"] == "sqf_command"
    assert nodes["tst_visible"]["source_file"] == "extension/src/plugin.cpp"
    edges = _edges(mod_graph)
    assert ("tst_visible", "cmdVisible()", "references", "handler") in edges
    assert ("tst_count", "cmdCount()", "references", "handler") in edges
    assert ("TST_main_fnc_init", "tst_visible", "references", "plugin_command") in edges
    # TST_cmd_count = compile "tst_count _this": a use of the variable is a use of the command.
    assert ("TST_main_fnc_init", "tst_count", "references", "plugin_command") in edges


def test_no_duplicate_ids(mod_graph):
    ids = [n["id"] for n in mod_graph["nodes"]]
    assert len(ids) == len(set(ids))


def test_report_of_the_project_pass(tmp_path, caplog):
    mod = tmp_path / "mod"
    shutil.copytree(MOD, mod)
    cfg = mod / "addons" / "main" / "CfgFunctions.hpp"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("class helper", "class lost {};\n        class helper"),
                   encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="graphify.extractors.sqf"):
        report = sqf_project_pass(collect_files(mod), mod.resolve(), [], [])
    assert report.missing_files == ["TST_main_fnc_lost"]
    # No AST nodes go in, so the pass finds no C++ handler.
    assert report.missing_handlers == ["tst_visible -> cmdVisible", "tst_count -> cmdCount"]
    assert "tst_not_here" in report.unknown_commands
    assert "TST_main_fnc_lost" in caplog.text


def test_one_namespace_for_all_mods_of_a_scan(tmp_path):
    shutil.copytree(MOD, tmp_path / "mod_a")
    script = tmp_path / "mod_b" / "addons" / "other" / "fn_use.sqf"
    script.parent.mkdir(parents=True)
    script.write_text("[] call TST_main_fnc_helper;\n", encoding="utf-8")
    result = _extract(tmp_path.resolve(), tmp_path / "cache")
    assert ("fn_use.sqf", "TST_main_fnc_helper", "calls", "call") in _edges(result)
    helper = _nodes(result)["TST_main_fnc_helper"]
    assert helper["source_file"] == "mod_a/addons/main/functions/fn_helper.sqf"


def test_refresh_extracts_the_whole_sqf_part_again():
    corpus = collect_files(MOD)
    other = MOD / "addons" / "main" / "notes.py"
    assert refresh_sqf_paths([other], corpus) == [other]
    changed = [MOD / "addons" / "main" / "functions" / "fn_helper.sqf"]
    refreshed = set(refresh_sqf_paths(changed, corpus))
    assert {p for p in corpus if p.suffix == ".sqf"} <= refreshed
    assert MOD / "addons" / "main" / "CfgFunctions.hpp" in refreshed
    assert MOD / "addons" / "util" / "XEH_PREP.hpp" in refreshed
    assert MOD / "extension" / "src" / "plugin.cpp" in refreshed
    # A change of the plugin commands refreshes the SQF part too.
    assert MOD / "addons" / "main" / "functions" / "fn_init.sqf" in refresh_sqf_paths(
        [MOD / "extension" / "src" / "plugin.cpp"], corpus)


def test_refresh_with_a_config_file():
    corpus = collect_files(MOD)
    refreshed = refresh_sqf_paths([MOD / "addons" / "main" / "config.cpp"], corpus)
    assert MOD / "addons" / "main" / "functions" / "fn_onButton.sqf" in refreshed


# ---------------------------------------------------------------- entry points, config calls, events

def test_entry_points(mod_graph):
    nodes = _nodes(mod_graph)
    assert nodes["TST_main_fnc_init"]["entry"] == "preInit"
    assert nodes["TST_main_fnc_api"]["entry"] == "readme"
    assert nodes["TST_main_fnc_onButton"]["entry"] == "config"
    assert nodes["TST_main_fnc_selfTest"]["entry"] == "test"  # the "tests" category
    assert nodes["TST_util_fnc_prepped"]["entry"] == "xeh"
    assert "entry" not in nodes["TST_main_fnc_dead"]


def test_a_config_calls_a_function(mod_graph):
    assert ("config.cpp", "TST_main_fnc_onButton", "calls", "config") in _edges(mod_graph)


def test_cba_events(mod_graph):
    nodes = _nodes(mod_graph)
    assert nodes["tst_ready"]["kind"] == "sqf_event"
    assert nodes["TST_util_ping"]["kind"] == "sqf_event"  # QEGVAR(util,ping) and QGVAR(ping)
    edges = _edges(mod_graph)
    assert ("TST_main_fnc_init", "tst_ready", "calls", "event_raise") in edges
    assert ("TST_main_fnc_init", "TST_util_ping", "calls", "event_raise") in edges
    # A handler with a function in it: the edge goes to the function. Else to the owner of the handler.
    assert ("TST_util_ping", "TST_util_fnc_other", "calls", "event_handler") in edges
    assert ("tst_ready", "TST_main_fnc_helper", "calls", "event_handler") in edges


def test_uncalled_functions_in_the_report(mod_graph):
    from graphify.analyze import god_nodes, surprising_connections
    from graphify.build import build_from_json
    from graphify.cluster import cluster, score_all
    from graphify.extractors.sqf import uncalled_sqf_functions
    from graphify.report import generate

    G = build_from_json(mod_graph, root=str(MOD.resolve()), directed=False)
    assert [label for label, _ in uncalled_sqf_functions(G)] == ["TST_main_fnc_dead"]
    communities = cluster(G)
    detection = {"files": {}, "total_files": 0, "total_words": 0, "warning": None}
    report = generate(G, communities, score_all(G, communities), {}, god_nodes(G),
                      surprising_connections(G, communities), detection, {"input": 0, "output": 0}, str(MOD))
    assert "## SQF Functions Without a Caller" in report
    assert "`TST_main_fnc_dead`" in report


# ---------------------------------------------------------------- the C++ plugin

def test_engine_calls(mod_graph):
    nodes = _nodes(mod_graph)
    assert nodes["sqf::line_intersects_surfaces"]["kind"] == "sqf_engine"
    edges = _edges(mod_graph)
    # The call is in a lambda: the edge goes from the function that holds the lambda.
    assert ("cmdVisible()", "sqf::line_intersects_surfaces", "calls", "engine_call") in edges
    assert ("cmdCount()", "sqf::diag_log", "calls", "engine_call") in edges
    assert "sqf::in_comment" not in nodes


def test_global_writes(mod_graph):
    nodes = _nodes(mod_graph)
    assert nodes["queryCount"]["kind"] == "cpp_global"
    writes = {(s, t) for s, t, _r, c in _edges(mod_graph) if c == "global_write"}
    assert ("cmdVisible()", "queryCount") in writes
    assert ("cmdVisible()", "pending") in writes  # pending.push_back(...) in a lambda
    assert ("intercept::pre_start()", "handleVisible") in writes
    # A local variable and a class field hide the global with the same name.
    assert ("cmdCount()", "pending") not in writes
    assert not any(s.endswith("bump()") for s, _t in writes)
    edge = next(e for e in mod_graph["edges"] if e.get("context") == "global_write")
    assert edge["relation"] == "writes" and edge["confidence"] == "INFERRED"


def test_no_global_pass_without_an_intercept_plugin(tmp_path):
    (tmp_path / "plain.cpp").write_text("int counter = 0;\nvoid f() { counter++; }\n", encoding="utf-8")
    result = _extract(tmp_path.resolve(), tmp_path / "cache")
    assert not [n for n in result["nodes"] if n.get("kind") == "cpp_global"]


# ---------------------------------------------------------------- callers and callees

def _directed(result: dict):
    import networkx as nx

    G = nx.MultiDiGraph()
    for n in result["nodes"]:
        G.add_node(n["id"], **n)
    for e in result["edges"]:
        if e["source"] in G and e["target"] in G:
            G.add_edge(e["source"], e["target"], **e)
    return G


def test_callees_tree(mod_graph):
    from graphify.calltree import call_tree, format_call_tree

    G = _directed(mod_graph)
    seed = _nodes(mod_graph)["TST_main_fnc_init"]["id"]
    text = format_call_tree(G, call_tree(G, seed, direction="callees", depth=0), direction="callees", depth=0)
    assert "- TST_main_fnc_helper [call] (sqf_function) addons/main/functions/fn_init.sqf:L11" in text
    assert "  - cmdVisible() [handler]" in text  # tst_visible -> its handler, one level down
    assert "    - sqf::line_intersects_surfaces [engine_call]" in text
    assert "    - queryCount [global_write]" in text
    assert "game_state" not in text  # no type uses (the parameter types of the handler)


def test_callers_tree_with_a_kind(mod_graph):
    from graphify.calltree import call_tree, format_call_tree

    G = _directed(mod_graph)
    seed = _nodes(mod_graph)["TST_util_fnc_other"]["id"]
    tree = call_tree(G, seed, direction="callers", depth=0)
    text = format_call_tree(G, tree, direction="callers", depth=0, kinds=["sqf_function"])
    assert "- TST_util_fnc_prepped [call]" in text
    assert "XEH_preInit.sqf" not in text  # a file node: not of the kind
    assert "TST_main_fnc_init" in text  # the walk goes through the hidden nodes


def test_callers_cli(tmp_path, capsys, monkeypatch):
    import sys

    from graphify.build import build_from_json
    from graphify.cli import dispatch_command
    from graphify.cluster import cluster
    from graphify.export import to_json

    result = _extract(MOD.resolve(), tmp_path / "cache")
    G = build_from_json(result, root=str(MOD.resolve()), directed=False)
    graph = tmp_path / "graph.json"
    to_json(G, cluster(G), str(graph), force=True)
    monkeypatch.setattr(sys, "argv", ["graphify", "callers", "TST_main_fnc_helper", "--graph", str(graph)])
    dispatch_command("callers")
    out = capsys.readouterr().out
    assert "Callers of TST_main_fnc_helper" in out
    assert "TST_main_fnc_init [call]" in out


# ---------------------------------------------------------------- the report

def test_no_surprise_for_the_sqf_bridges():
    import networkx as nx

    from graphify.analyze import surprising_connections

    def graph(context: str | None):
        G = nx.Graph()
        G.add_node("f", label="DD_fnc_a", source_file="addons/a/fn_a.sqf", file_type="code")
        G.add_node("c", label="dd_cmd", source_file="extension/plugin.cpp", file_type="code")
        G.add_edge("f", "c", relation="references", context=context, confidence="EXTRACTED")
        return G

    def pairs(G):
        return {(s["source"], s["target"]) for s in surprising_connections(G, {0: ["f"], 1: ["c"]})}

    assert ("DD_fnc_a", "dd_cmd") in pairs(graph(None))
    assert ("DD_fnc_a", "dd_cmd") not in pairs(graph("plugin_command"))


def test_min_community_size_from_graphifyrc(tmp_path):
    from graphify.rcfile import load_graphifyrc, rc_min_community_size

    assert rc_min_community_size(tmp_path) == 3
    (tmp_path / ".graphifyrc").write_text("min_community_size=10\n", encoding="utf-8")
    assert load_graphifyrc(tmp_path)["min_community_size"] == 10
    assert rc_min_community_size(tmp_path) == 10
    (tmp_path / ".graphifyrc").write_text("min_community_size=0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_graphifyrc(tmp_path)
    assert rc_min_community_size(tmp_path) == 3


# ---------------------------------------------------------------- a mission (description.ext)

MISSION = Path(__file__).parent / "fixtures" / "sqf_mission"


@pytest.fixture(scope="module")
def mission_graph(tmp_path_factory) -> dict:
    return _extract(MISSION.resolve(), tmp_path_factory.mktemp("mission_cache"))


def test_mission_cfgfunctions(mission_graph):
    nodes = _nodes(mission_graph)
    # The "file" attribute and the default folder (functions/<category>) are relative to the mission.
    assert nodes["TST_fnc_alpha"]["source_file"] == "scripts/fn_alpha.sqf"
    assert nodes["TST_fnc_beta"]["source_file"] == "functions/core/fn_beta.sqf"
    assert nodes["TST_fnc_alpha"]["kind"] == "sqf_function"
    # init.sqf is a mission event script: the engine runs it.
    assert nodes["TST_fnc_alpha"]["entry"] == "mission"


def test_functions_made_in_the_code(mission_graph):
    nodes = _nodes(mission_graph)
    assert nodes["TST_fnc_made"]["defined"] == "assignment"
    assert nodes["TST_fnc_made"]["source_file"] == "scripts/helpers.sqf"
    assert nodes["TST_fnc_unused"]["source_location"] == "L4"  # = compileFinal { ... }
    edges = _edges(mission_graph)
    # The calls in the { ... } block belong to the function, the rest to the file.
    assert ("TST_fnc_made", "TST_fnc_alpha", "calls", "call") in edges
    assert ("TST_fnc_unused", "BIS_fnc_log", "calls", "external_call") in edges
    assert ("helpers.sqf", "TST_fnc_beta", "calls", "call") in edges
    assert ("helpers.sqf", "TST_fnc_alpha", "calls", "call") not in edges
    assert ("TST_fnc_beta", "TST_fnc_made", "calls", "call") in edges
    assert "TST_fnc_made" not in {n["label"] for n in mission_graph["nodes"] if n.get("kind") == "sqf_external"}


def test_script_loads(mission_graph):
    edges = _edges(mission_graph)
    assert ("init.sqf", "helpers.sqf", "calls", "script") in edges  # execVM "scripts\helpers.sqf"
    assert ("init.sqf", "settings.sqf", "calls", "script") in edges  # preprocessFileLineNumbers '...'


# ---------------------------------------------------------------- two functions that call each other

def test_mutual_calls_keep_both_directions(tmp_path):
    """docs/a3/graph-bugs.md, item 1: an undirected graph kept one edge for A -> B and B -> A."""
    import json

    from graphify.affected import load_graph
    from graphify.calltree import call_tree, format_call_tree
    from graphify.extractors.sqf import uncalled_sqf_functions, wants_directed
    from graphify.watch import _rebuild_code

    mod = tmp_path / "mod"
    shutil.copytree(MOD, mod)
    helper = mod / "addons" / "main" / "functions" / "fn_helper.sqf"
    # TST_main_fnc_init calls helper (fn_init.sqf:11), and now helper calls init back.
    helper.write_text(helper.read_text(encoding="utf-8") + "[] call TST_main_fnc_init;\n", encoding="utf-8")
    assert _rebuild_code(mod.resolve(), block_on_lock=True)

    graph_path = mod / "graphify-out" / "graph.json"
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    assert data["directed"] is True
    assert wants_directed(data["nodes"])
    label = {n["id"]: n["label"] for n in data["nodes"]}
    pairs = {(label[e["source"]], label[e["target"]]) for e in data["links"] if e.get("context") == "call"}
    assert ("TST_main_fnc_init", "TST_main_fnc_helper") in pairs
    assert ("TST_main_fnc_helper", "TST_main_fnc_init") in pairs

    G = load_graph(graph_path)
    seed = next(n for n, d in G.nodes(data=True) if d.get("label") == "TST_main_fnc_helper")
    text = format_call_tree(G, call_tree(G, seed, direction="callers"), direction="callers", depth=1)
    assert "- TST_main_fnc_init [call]" in text
    assert "TST_main_fnc_helper" not in [lbl for lbl, _ in uncalled_sqf_functions(G)]


def test_no_directed_graph_without_sqf():
    from graphify.extractors.sqf import wants_directed

    assert not wants_directed([{"id": "a", "label": "a()"}, {"id": "b", "kind": "sqf_external"}])
