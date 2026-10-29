#pragma once
#include <array>
#include <asio.hpp>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <functional>
#include <memory>
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace nf {
using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;
using Millis = std::chrono::milliseconds;
using Tcp = asio::ip::tcp;
using Error = asio::error_code;
struct Endpoint {
  std::string host;
  unsigned short port;
};
Endpoint endpoint(const std::string &address);
Json read_json(const std::string &path);
double elapsed_ms(Clock::time_point start);
std::string lower(std::string text);

class Events {
public:
  explicit Events(const std::string &path);
  void emit(Json record);

private:
  std::ofstream file_;
  Clock::time_point start_ = Clock::now();
};

// Deliberately narrow, bounded HTTP/1.1 fixture/control server. The proxy
// itself is opaque TCP and can carry chunked responses or arbitrary application
// bytes.
class HttpSession : public std::enable_shared_from_this<HttpSession> {
public:
  using Handler = std::function<void(std::shared_ptr<HttpSession>, std::string,
                                     std::string, Json)>;
  explicit HttpSession(Tcp::socket socket);
  void read(Handler handler);
  void reply(int status, const Json &body);
  void send(std::string data, std::function<void(Error)> done);
  void watch_disconnect(std::function<void()> callback);
  void close();
  Tcp::socket &socket() { return socket_; }

private:
  void parse_headers(Handler handler);
  void deliver(Handler handler, std::string method, std::string path,
               std::size_t length);
  Tcp::socket socket_;
  asio::streambuf input_{1024 * 1024};
  asio::steady_timer deadline_;
  std::array<char, 1> disconnect_byte_{};
};

struct Rule {
  std::string target, direction, action;
  std::uint64_t revision = 0, seed = 0;
  std::size_t bytes = 0, rate = 0;
  int delay_ms = 0, jitter_ms = 0, idle_ms = 0;
  Clock::time_point expires;
};
Rule parse_rule(const Json &data, std::uint64_t revision);
class Rules {
public:
  explicit Rules(asio::io_context &io, Events &events);
  std::optional<Rule> get(const std::string &target,
                          const std::string &direction) const;
  Json apply(const Json &data);
  void clear();
  std::function<void()> changed;

private:
  struct Entry {
    Rule rule;
    std::shared_ptr<asio::steady_timer> timer;
  };
  asio::io_context &io_;
  Events &events_;
  std::unordered_map<std::string, Entry> entries_;
  std::uint64_t revision_ = 0;
};
void run_proxy(asio::io_context &io, const Json &config, Events &events);
void run_replay(asio::io_context &io, const Json &config, Events &events);
} // namespace nf
