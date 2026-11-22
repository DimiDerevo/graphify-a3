#include "script_component.hpp"

class CfgPatches {
    class ADDON {
        units[] = {};
    };
};

class CfgFunctions {
    #include "CfgFunctions.hpp"
};

class RscTstDialog {
    class Controls {
        class Button {
            onButtonClick = "[] call TST_main_fnc_onButton";
        };
    };
};
