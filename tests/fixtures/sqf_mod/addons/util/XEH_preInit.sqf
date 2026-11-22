#include "script_component.hpp"
#include "XEH_PREP.hpp"
[] call FUNC(prepped);
[QGVAR(ping), {_this call FUNC(other)}] call CBA_fnc_addEventHandler;
