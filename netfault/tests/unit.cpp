#include "netfault/common.hpp"
#include <iostream>
#include <stdexcept>
void require(bool value) {
  if (!value)
    throw std::runtime_error("unit assertion failed");
}
template <class F> void rejects(F f) {
  bool rejected = false;
  try {
    f();
  } catch (const std::exception &) {
    rejected = true;
  }
  require(rejected);
}
int main() {
  require(nf::endpoint("127.0.0.1:1234").port == 1234);
  require(nf::endpoint("[::1]:8080").host == "::1");
  for (auto bad :
       {"localhost", "host:0", "host:70000", "host:-1", "host:12oops"})
    rejects([&] { nf::endpoint(bad); });
  nf::Json valid = {{"target", "worker-1"},
                    {"direction", "response"},
                    {"action", "delay"},
                    {"delay_ms", 300},
                    {"duration_ms", 100}};
  auto r = nf::parse_rule(valid, 1);
  require(r.delay_ms == 300);
  require(r.revision == 1);
  for (const auto &[key, value] :
       std::vector<std::pair<std::string, nf::Json>>{{"delay_ms", -1},
                                                     {"direction", "both"},
                                                     {"action", "unknown"},
                                                     {"duration_ms", 0},
                                                     {"duration_ms", 300001},
                                                     {"unexpected", 1}}) {
    auto bad = valid;
    bad[key] = value;
    rejects([&] { nf::parse_rule(bad, 2); });
  }
  auto bandwidth = valid;
  bandwidth["action"] = "bandwidth";
  rejects([&] { nf::parse_rule(bandwidth, 2); });
  auto idle = valid;
  idle["action"] = "idle";
  rejects([&] { nf::parse_rule(idle, 2); });
  std::cout << "endpoint and fault validation passed\n";
}
