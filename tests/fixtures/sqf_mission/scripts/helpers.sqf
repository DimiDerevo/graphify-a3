TST_fnc_made = {
    [] call TST_fnc_alpha;
};
TST_fnc_unused = compileFinal {
    [] call BIS_fnc_log;
};
[] call TST_fnc_beta;
