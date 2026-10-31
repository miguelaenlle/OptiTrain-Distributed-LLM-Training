#include "netfault/common.hpp"
#include <csignal>
#include <iostream>
int main(int argc, char **argv) {
  if (argc == 2 && std::string(argv[1]) == "--version") {
    std::cout << "optitrain-netfault 0.1.0\n";
    return 0;
  }
  if (argc != 4 || std::string(argv[2]) != "--config") {
    std::cerr << "usage: optitrain-netfault {proxy|replay} --config FILE\n";
    return 2;
  }
  try {
    auto config = nf::read_json(argv[3]);
    nf::Events events(config.at("events"));
    asio::io_context io;
    asio::signal_set signals(io, SIGINT, SIGTERM);
    signals.async_wait([&](nf::Error, int) { io.stop(); });
    std::string mode = argv[1];
    if (mode == "proxy")
      nf::run_proxy(io, config, events);
    else if (mode == "replay")
      nf::run_replay(io, config, events);
    else
      throw std::invalid_argument("unknown mode");
    events.emit({{"event", "shutdown"}});
  } catch (const std::exception &e) {
    std::cerr << "netfault: " << e.what() << '\n';
    return 2;
  }
}
