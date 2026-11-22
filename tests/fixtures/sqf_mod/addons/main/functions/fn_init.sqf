/*
 * Description:
 * Starts the mod and the plugin.
 *
 * Arguments: none
 */
// TST_main_fnc_ghost is in a comment: no edge.
TST_cmd_count = compile "tst_count _this";
private _ok = tst_visible [0, 0];
private _n = TST_cmd_count;
[] call TST_main_fnc_helper;
["tst_event", { [] call CBA_fnc_globalEvent }] call CBA_fnc_addEventHandler;
[] spawn (missionNamespace getVariable "TST_main_fnc_helper");
[] call EFUNC(util,prepped);
private _wrong = tst_not_here [];
["tst_ready", []] call CBA_fnc_localEvent;
[QEGVAR(util,ping), [1]] call CBA_fnc_globalEvent;
