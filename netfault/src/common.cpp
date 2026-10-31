#include "netfault/common.hpp"
#include <algorithm>
#include <cctype>
#include <charconv>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <unordered_set>

namespace nf {
Endpoint endpoint(const std::string &address) {
  auto colon = address.rfind(':');
  if (colon == std::string::npos || colon == 0)
    throw std::invalid_argument("expected host:port");
  std::string host = address.substr(0, colon);
  if (host.front() == '[' && host.back() == ']')
    host = host.substr(1, host.size() - 2);
  auto port_string = address.substr(colon + 1);
  unsigned int port = 0;
  auto [end, ec] = std::from_chars(
      port_string.data(), port_string.data() + port_string.size(), port);
  if (ec != std::errc{} || end != port_string.data() + port_string.size() ||
      port == 0 || port > 65535)
    throw std::invalid_argument("port must be 1..65535");
  return {host, static_cast<unsigned short>(port)};
}
Json read_json(const std::string &path) {
  std::ifstream file(path);
  if (!file)
    throw std::runtime_error("cannot open config: " + path);
  return Json::parse(file);
}
double elapsed_ms(Clock::time_point start) {
  return std::chrono::duration<double, std::milli>(Clock::now() - start)
      .count();
}
std::string lower(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) {
    return static_cast<char>(std::tolower(c));
  });
  return text;
}
Events::Events(const std::string &path) : file_(path) {
  if (!file_)
    throw std::runtime_error("cannot write events: " + path);
}
void Events::emit(Json record) {
  record["monotonic_ms"] = elapsed_ms(start_);
  record["unix_ms"] = std::chrono::duration_cast<Millis>(
                          std::chrono::system_clock::now().time_since_epoch())
                          .count();
  file_ << record.dump() << '\n';
  file_.flush();
  if (!file_)
    throw std::runtime_error("failed to persist event evidence");
}
HttpSession::HttpSession(Tcp::socket socket)
    : socket_(std::move(socket)), deadline_(socket_.get_executor()) {}
void HttpSession::read(Handler handler) {
  deadline_.expires_after(std::chrono::seconds(10));
  auto self = shared_from_this();
  deadline_.async_wait([self](Error ec) {
    if (!ec)
      self->close();
  });
  asio::async_read_until(
      socket_, input_, "\r\n\r\n",
      [self, handler = std::move(handler)](Error ec, std::size_t) {
        if (ec) {
          self->close();
          return;
        }
        self->parse_headers(handler);
      });
}
void HttpSession::parse_headers(Handler handler) {
  try {
    std::istream stream(&input_);
    std::string first, method, path, version;
    std::getline(stream, first);
    std::istringstream request(first);
    request >> method >> path >> version;
    if (version != "HTTP/1.1" && version != "HTTP/1.0")
      throw std::invalid_argument("invalid HTTP version");
    std::optional<std::size_t> length;
    std::string line;
    while (std::getline(stream, line) && line != "\r") {
      auto colon = line.find(':');
      if (colon == std::string::npos)
        throw std::invalid_argument("invalid header");
      auto name = lower(line.substr(0, colon));
      auto value = line.substr(colon + 1);
      if (name == "transfer-encoding")
        throw std::invalid_argument(
            "chunked requests unsupported by fixture/control endpoint");
      if (name == "content-length") {
        if (length)
          throw std::invalid_argument("duplicate content-length");
        auto begin = value.find_first_not_of(" \t");
        auto end = value.find_last_not_of(" \t\r");
        if (begin == std::string::npos)
          throw std::invalid_argument("empty content-length");
        auto digits = value.substr(begin, end - begin + 1);
        std::size_t parsed = 0;
        auto [ptr, ec] = std::from_chars(digits.data(),
                                         digits.data() + digits.size(), parsed);
        if (ec != std::errc{} || ptr != digits.data() + digits.size() ||
            parsed > 512 * 1024)
          throw std::invalid_argument("invalid or oversized content-length");
        length = parsed;
      }
    }
    deliver(std::move(handler), method, path, length.value_or(0));
  } catch (const std::exception &e) {
    reply(400, {{"error", e.what()}});
  }
}
void HttpSession::deliver(Handler handler, std::string method, std::string path,
                          std::size_t length) {
  auto self = shared_from_this();
  auto finish = [self, handler = std::move(handler), method = std::move(method),
                 path = std::move(path), length](Error ec, std::size_t) {
    if (ec) {
      self->close();
      return;
    }
    self->deadline_.cancel();
    try {
      std::string body(length, '\0');
      std::istream stream(&self->input_);
      stream.read(body.data(), static_cast<std::streamsize>(length));
      Json data = body.empty() ? Json::object() : Json::parse(body);
      handler(self, method, path, std::move(data));
    } catch (const std::exception &e) {
      self->reply(400, {{"error", e.what()}});
    }
  };
  if (input_.size() >= length)
    finish({}, 0);
  else
    asio::async_read(socket_, input_,
                     asio::transfer_exactly(length - input_.size()),
                     std::move(finish));
}
void HttpSession::reply(int status, const Json &body) {
  auto payload = body.dump();
  std::string reason = status == 200 ? "OK" : "Error";
  send("HTTP/1.1 " + std::to_string(status) + " " + reason +
           "\r\nContent-Type: application/json\r\nConnection: "
           "close\r\nContent-Length: " +
           std::to_string(payload.size()) + "\r\n\r\n" + payload,
       [self = shared_from_this()](Error) { self->close(); });
}
void HttpSession::send(std::string data, std::function<void(Error)> done) {
  auto bytes = std::make_shared<std::string>(std::move(data));
  asio::async_write(socket_, asio::buffer(*bytes),
                    [self = shared_from_this(), bytes, done = std::move(done)](
                        Error ec, std::size_t) { done(ec); });
}
void HttpSession::watch_disconnect(std::function<void()> callback) {
  socket_.async_read_some(
      asio::buffer(disconnect_byte_),
      [self = shared_from_this(),
       callback = std::move(callback)](Error, std::size_t) { callback(); });
}
void HttpSession::close() {
  Error ignored;
  deadline_.cancel();
  socket_.cancel(ignored);
  socket_.shutdown(Tcp::socket::shutdown_both, ignored);
  socket_.close(ignored);
}

Rule parse_rule(const Json &data, std::uint64_t revision) {
  if (!data.is_object())
    throw std::invalid_argument("rule must be an object");
  const std::unordered_set<std::string> keys = {
      "target",    "direction",    "action", "duration_ms", "delay_ms",
      "jitter_ms", "rate_bytes_s", "bytes",  "idle_ms",     "seed"};
  for (auto it = data.begin(); it != data.end(); ++it)
    if (!keys.contains(it.key()))
      throw std::invalid_argument("unknown rule field: " + it.key());
  Rule r;
  r.target = data.at("target").get<std::string>();
  r.direction = data.at("direction").get<std::string>();
  r.action = data.at("action").get<std::string>();
  if (r.target.empty() ||
      (r.direction != "request" && r.direction != "response"))
    throw std::invalid_argument("invalid target or direction");
  auto number = [&](const char *key, std::int64_t fallback,
                    std::int64_t maximum) {
    if (data.contains(key) && !data.at(key).is_number_integer())
      throw std::invalid_argument(std::string("integer required: ") + key);
    auto n = data.value(key, fallback);
    if (n < 0 || n > maximum)
      throw std::invalid_argument(std::string("out of range: ") + key);
    return n;
  };
  int duration = static_cast<int>(number("duration_ms", 30000, 300000));
  if (duration == 0)
    throw std::invalid_argument("duration must be positive");
  r.delay_ms = static_cast<int>(number("delay_ms", 0, 60000));
  r.jitter_ms = static_cast<int>(number("jitter_ms", 0, 60000));
  r.idle_ms = static_cast<int>(number("idle_ms", 0, 300000));
  r.rate = static_cast<std::size_t>(number("rate_bytes_s", 0, 1000000000));
  r.bytes = static_cast<std::size_t>(number("bytes", 0, 1000000000));
  r.seed = static_cast<std::uint64_t>(
      number("seed", 0, std::numeric_limits<std::int64_t>::max()));
  const std::unordered_set<std::string> actions = {
      "delay", "stall", "bandwidth", "cutoff", "idle", "disable"};
  if (!actions.contains(r.action))
    throw std::invalid_argument("unsupported action");
  if (r.action == "bandwidth" && r.rate == 0)
    throw std::invalid_argument("bandwidth rate must be positive");
  if (r.action == "cutoff" && r.bytes == 0)
    throw std::invalid_argument("cutoff bytes must be positive");
  if (r.action == "idle" && r.idle_ms == 0)
    throw std::invalid_argument("idle duration must be positive");
  if (r.action == "disable" && r.direction != "request")
    throw std::invalid_argument("disable uses request direction");
  r.expires = Clock::now() + Millis(duration);
  r.revision = revision;
  return r;
}
Rules::Rules(asio::io_context &io, Events &events) : io_(io), events_(events) {}
std::optional<Rule> Rules::get(const std::string &target,
                               const std::string &direction) const {
  auto it = entries_.find(target + ":" + direction);
  if (it == entries_.end() || it->second.rule.expires <= Clock::now())
    return std::nullopt;
  return it->second.rule;
}
Json Rules::apply(const Json &data) {
  auto r = parse_rule(data, ++revision_);
  auto key = r.target + ":" + r.direction;
  if (get(r.target, r.direction))
    throw std::invalid_argument("overlapping rule for target/direction");
  auto timer = std::make_shared<asio::steady_timer>(io_, r.expires);
  entries_[key] = {r, timer};
  timer->async_wait([this, key, revision = r.revision](Error ec) {
    if (ec)
      return;
    auto it = entries_.find(key);
    if (it != entries_.end() && it->second.rule.revision == revision) {
      events_.emit({{"event", "rule_expired"},
                    {"target", it->second.rule.target},
                    {"direction", it->second.rule.direction},
                    {"revision", revision}});
      entries_.erase(it);
      if (changed)
        changed();
    }
  });
  events_.emit(
      {{"event", "rule_applied"}, {"rule", data}, {"revision", r.revision}});
  if (changed)
    changed();
  return {{"ok", true}, {"revision", r.revision}};
}
void Rules::clear() {
  for (auto &[key, entry] : entries_)
    entry.timer->cancel();
  entries_.clear();
  events_.emit({{"event", "rules_cleared"}});
  if (changed)
    changed();
}
} // namespace nf
