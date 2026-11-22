# SQF graph bugs (found by the users of the graph)

A list of defects of the SQF part of the graph. Each item has the case, the cause and the check.

| # | Defect | Status |
|---|---|---|
| 1 | A pair of functions that refer to each other keeps one edge only | Fixed 2026-11-22 |

## 1. A pair of functions that refer to each other keeps one edge only

**Fixed 2026-11-22.** The cause was correct: an undirected graph keeps one edge for a pair of nodes. `build.py`
drops the reverse edge of the same relation (#1061) and writes over a reverse edge of another relation.
The fix: a graph with SQF functions is now directed.

- `DD_graph\graph.py` builds with `directed=True`.
- `graphify update` (`watch.py`) and `graphify extract` (`cli.py`) build a directed graph when the extraction has an
  `sqf_function` node (`wants_directed` in `graphify/extractors/sqf.py`). An old undirected SQF graph becomes directed
  at the next build. Other corpora keep the old behavior.
- Test: `tests/test_sqf.py::test_mutual_calls_keep_both_directions`.

The checks on soldierFSM after the fix (graph built 2026-11-22):
- `graphify callers "SFSM_fnc_SuppressedEH"` shows `SFSM_fnc_InitMan [call] ... fn_InitMan.sqf:L24`.
- `graphify callers "SFSM_fnc_say"` shows `SFSM_fnc_SquadSay [string] ... fn_SquadSay.sqf:L14`.
- The list "SQF Functions Without a Caller" has 2 items less: 87 -> 85. (77 -> 75 in the old count. The functions
  made in the code, `TAG_fnc_x = { ... };`, added 10 items to the list before this fix.)
- The graph has 8 edges more: 7941 -> 7949. These are the lost reverse edges.

The original report:

Found: 2026-11-22, the DCO rewrite inventory (`E:\A3Projects\soldierFSM\notes\rewrite-inventory.md`, section "Graph notes").
Graph: `E:\A3Projects\DD_graph\graphs\soldierFSM\graphify-out\graph.json`, built 2026-11-22 12:30.

The cases:
- `SFSM_fnc_InitMan` calls `SFSM_fnc_SuppressedEH` (`addons/framework/functions/init/fn_InitMan.sqf:24`).
  `SFSM_fnc_SuppressedEH` calls `SFSM_fnc_InitMan` back (`fn_SuppressedEH.sqf:28`). The graph has only
  `SuppressedEH -> InitMan`.
- `SFSM_fnc_SquadSay` calls `SFSM_fnc_say` through `remoteExec ["SFSM_fnc_say", 0]` (`fn_SquadSay.sqf`).
  `SFSM_fnc_say` refers to `SquadSay` in `isNil "SFSM_fnc_SquadSay"` (`fn_say.sqf:4`). The graph has only
  `say -> SquadSay` (relation `references`, context `string`).

The effect: `SuppressedEH` and `say` are in the section "SQF Functions Without a Caller" of `GRAPH_REPORT.md`, but the
code calls them. `graphify callers` gives "No callers found" for them. `callers` and `callees` lose the other direction
for each such pair.

The cause (probable, not tested): the saved graph is undirected (`"directed": false` in `graph.json`). An undirected
graph keeps one edge for a pair of nodes. The second edge of the pair (the other direction) writes over the first, and
`_src` / `_tgt` keep the direction of the last one. `uncalled_sqf_functions` (`graphify/extractors/sqf.py`) reads
`_src` / `_tgt`, so it sees one direction only. A string reference (`isNil "..."`, `remoteExec ["..."]`) counts as
the second direction too.

The check after a fix:
- `graphify callers "SFSM_fnc_SuppressedEH" --graph <soldierFSM graph>` shows `SFSM_fnc_InitMan` (line 24).
- `graphify callers "SFSM_fnc_say" --graph <soldierFSM graph>` shows `SFSM_fnc_SquadSay`.
- The list "SQF Functions Without a Caller" of soldierFSM has 75 items, not 77.
- A count of the pairs A -> B and B -> A in the soldierFSM code, before and after, gives the number of lost edges.

## 2. Notes from the same check (not bugs)

- A call in a comment gives no edge, as expected. Examples in soldierFSM: `Tally_fnc_ChopperEvasion` (`fn_EvadeVEh.sqf:223`),
  `Tally_fnc_ResumeOriginalPath` (`fn_EndEvasionVeh.sqf`, in `/* */`), `SFSM_fnc_sprintInit` and
  `SFSM_fnc_sprintLooper` (the block "OLD FUNCTION" of `fn_sprint.sqf`).
- The other 75 items of the soldierFSM list "without a caller" are correct (grep, case insensitive).
