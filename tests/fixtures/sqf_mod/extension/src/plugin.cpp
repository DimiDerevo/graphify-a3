#include <intercept.hpp>

using namespace intercept;

static types::registered_sqf_function handleVisible;
static types::registered_sqf_function handleCount;
static int queryCount = 0;
std::vector<int> pending;

struct Stats {
    int queryCount = 0;
    void bump() { queryCount++; }
};

game_value cmdVisible(game_state&, game_value_parameter args) {
    queryCount++;
    auto probe = [&](int i) { pending.push_back(i); return sqf::line_intersects_surfaces(i); };
    // sqf::in_comment(1) does not count
    return probe(1);
}

game_value cmdCount(game_state&, game_value_parameter args) {
    int pending = 0;
    pending = 2;
    sqf::diag_log("count");
    return 0.0f;
}

void intercept::pre_start() {
    handleVisible = client::host::register_sqf_command("tst_visible", "Test: is a point visible", cmdVisible,
        game_data_type::BOOL, game_data_type::ARRAY);
    handleCount = client::host::register_sqf_command("tst_count", "Test: count", cmdCount, game_data_type::SCALAR, game_data_type::ANY);
}
