#include "netfault/common.hpp"
#include <deque>
#include <random>
#include <unordered_set>

namespace nf {
namespace {
class Session;
struct Chunk {
  std::vector<char> bytes;
  Clock::time_point due;
  std::size_t offset = 0;
};
class Flow : public std::enable_shared_from_this<Flow> {
public:
  Flow(std::weak_ptr<Session> owner, Tcp::socket &source,
       Tcp::socket &destination, Rules &rules, std::string target,
       std::string direction, std::uint64_t id);
  void start();
  void wake();
  void stop();

private:
  void read();
  void pump();
  void wait(Clock::time_point until);
  void fail(const std::string &reason);
  std::weak_ptr<Session> owner_;
  Tcp::socket &source_, &destination_;
  Rules &rules_;
  std::string target_, direction_;
  std::array<char, 16384> input_{};
  std::deque<Chunk> queue_;
  std::size_t queued_ = 0, rule_bytes_ = 0;
  asio::steady_timer timer_;
  Clock::time_point activity_ = Clock::now(), rate_due_ = Clock::now();
  std::uint64_t revision_ = 0, generation_ = 0, id_;
  std::mt19937_64 random_;
  bool reading_ = false, writing_ = false, waiting_ = false, eof_ = false,
       finished_ = false, stopped_ = false;
};
class Session : public std::enable_shared_from_this<Session> {
public:
  Session(Tcp::socket socket, Endpoint upstream, Rules &rules, Events &events,
          std::string target, std::uint64_t id,
          std::function<void(std::uint64_t)> done)
      : client_(std::move(socket)), upstream_(client_.get_executor()),
        resolver_(client_.get_executor()), deadline_(client_.get_executor()),
        endpoint_(std::move(upstream)), rules_(rules), events_(events),
        target_(std::move(target)), id_(id), done_(std::move(done)) {}
  void start() {
    events_.emit({{"event", "connection_open"},
                  {"target", target_},
                  {"connection_id", id_}});
    deadline_.expires_after(std::chrono::seconds(3));
    deadline_.async_wait([self = shared_from_this()](Error ec) {
      if (!ec)
        self->close("connect_timeout");
    });
    resolver_.async_resolve(
        endpoint_.host, std::to_string(endpoint_.port),
        [self = shared_from_this()](Error ec,
                                    Tcp::resolver::results_type results) {
          if (ec) {
            self->close("resolve_error");
            return;
          }
          asio::async_connect(
              self->upstream_, results,
              [self](Error error, const Tcp::endpoint &) {
                if (error) {
                  self->close("connect_error");
                  return;
                }
                if (self->closed_)
                  return;
                Error ignored;
                self->client_.set_option(Tcp::no_delay(true), ignored);
                self->upstream_.set_option(Tcp::no_delay(true), ignored);
                self->deadline_.expires_after(std::chrono::minutes(5));
                self->deadline_.async_wait([self](Error e) {
                  if (!e)
                    self->close("session_lifetime");
                });
                self->request_ = std::make_shared<Flow>(
                    self, self->client_, self->upstream_, self->rules_,
                    self->target_, "request", self->id_);
                self->response_ = std::make_shared<Flow>(
                    self, self->upstream_, self->client_, self->rules_,
                    self->target_, "response", self->id_);
                self->request_->start();
                self->response_->start();
              });
        });
  }
  void matched(const Rule &rule, const std::string &direction) {
    events_.emit({{"event", "fault_matched"},
                  {"connection_id", id_},
                  {"target", target_},
                  {"direction", direction},
                  {"revision", rule.revision}});
  }
  void wake() {
    if (request_)
      request_->wake();
    if (response_)
      response_->wake();
  }
  void finished() {
    if (++finished_ == 2)
      close("eof");
  }
  void close(const std::string &reason) {
    if (closed_)
      return;
    closed_ = true;
    if (request_)
      request_->stop();
    if (response_)
      response_->stop();
    resolver_.cancel();
    deadline_.cancel();
    Error ignored;
    for (auto *socket : {&client_, &upstream_}) {
      socket->cancel(ignored);
      socket->shutdown(Tcp::socket::shutdown_both, ignored);
      socket->close(ignored);
    }
    events_.emit({{"event", "connection_closed"},
                  {"target", target_},
                  {"connection_id", id_},
                  {"reason", reason}});
    done_(id_);
  }

private:
  Tcp::socket client_, upstream_;
  Tcp::resolver resolver_;
  asio::steady_timer deadline_;
  Endpoint endpoint_;
  Rules &rules_;
  Events &events_;
  std::string target_;
  std::uint64_t id_;
  std::function<void(std::uint64_t)> done_;
  std::shared_ptr<Flow> request_, response_;
  int finished_ = 0;
  bool closed_ = false;
};
Flow::Flow(std::weak_ptr<Session> owner, Tcp::socket &source, Tcp::socket &dest,
           Rules &rules, std::string target, std::string direction,
           std::uint64_t id)
    : owner_(std::move(owner)), source_(source), destination_(dest),
      rules_(rules), target_(std::move(target)),
      direction_(std::move(direction)), timer_(source.get_executor()), id_(id) {
}
void Flow::start() {
  read();
  pump();
}
void Flow::stop() {
  stopped_ = true;
  ++generation_;
  timer_.cancel();
}
void Flow::fail(const std::string &reason) {
  if (auto owner = owner_.lock())
    owner->close(reason);
}
void Flow::wake() {
  if (stopped_)
    return;
  ++generation_;
  timer_.cancel();
  waiting_ = false;
  pump();
  read();
}
void Flow::read() {
  constexpr std::size_t maximum = 256 * 1024;
  if (stopped_ || reading_ || eof_ || queued_ + input_.size() > maximum)
    return;
  reading_ = true;
  source_.async_read_some(asio::buffer(input_), [self = shared_from_this()](
                                                    Error ec, std::size_t n) {
    self->reading_ = false;
    if (self->stopped_)
      return;
    if (n) {
      auto now = Clock::now();
      self->activity_ = now;
      auto rule = self->rules_.get(self->target_, self->direction_);
      auto due = now;
      if (rule && rule->action == "delay") {
        if (self->revision_ != rule->revision)
          self->random_.seed(rule->seed + self->id_);
        int jitter = rule->jitter_ms
                         ? std::uniform_int_distribution<int>(
                               -rule->jitter_ms, rule->jitter_ms)(self->random_)
                         : 0;
        due += Millis(std::max(0, rule->delay_ms + jitter));
      }
      // TCP byte order must survive jitter, even when a later chunk draws less
      // delay.
      if (!self->queue_.empty())
        due = std::max(due, self->queue_.back().due);
      self->queue_.push_back(
          {std::vector<char>(self->input_.begin(),
                             self->input_.begin() +
                                 static_cast<std::ptrdiff_t>(n)),
           due, 0});
      self->queued_ += n;
    }
    if (ec == asio::error::eof)
      self->eof_ = true;
    else if (ec) {
      self->fail("read_error");
      return;
    }
    self->pump();
    self->read();
  });
}
void Flow::wait(Clock::time_point until) {
  if (waiting_)
    return;
  waiting_ = true;
  auto generation = ++generation_;
  timer_.expires_at(until);
  timer_.async_wait([self = shared_from_this(), generation](Error ec) {
    if (self->stopped_ || generation != self->generation_)
      return;
    self->waiting_ = false;
    if (!ec)
      self->pump();
  });
}
void Flow::pump() {
  if (stopped_ || writing_ || waiting_ || finished_)
    return;
  auto now = Clock::now();
  auto rule = rules_.get(target_, direction_);
  if (rule && rule->revision != revision_) {
    revision_ = rule->revision;
    if (auto owner = owner_.lock())
      owner->matched(*rule, direction_);
    rule_bytes_ = 0;
    random_.seed(rule->seed + id_);
    rate_due_ = now;
  }
  if (rule && rule->action == "idle") {
    auto expiry = activity_ + Millis(rule->idle_ms);
    if (now >= expiry) {
      fail("idle_expiry");
      return;
    }
    if (queue_.empty()) {
      wait(std::min(expiry, rule->expires));
      return;
    }
  }
  if (rule && rule->action == "stall") {
    wait(rule->expires);
    return;
  }
  if (queue_.empty()) {
    if (eof_) {
      finished_ = true;
      Error ignored;
      destination_.shutdown(Tcp::socket::shutdown_send, ignored);
      if (auto owner = owner_.lock())
        owner->finished();
    }
    return;
  }
  auto &chunk = queue_.front();
  auto due = chunk.due;
  if (rule && rule->action == "bandwidth")
    due = std::max(due, rate_due_);
  if (due > now) {
    wait(due);
    return;
  }
  std::size_t n = chunk.bytes.size() - chunk.offset;
  bool cutoff = false;
  if (rule && rule->action == "cutoff") {
    if (rule_bytes_ >= rule->bytes) {
      fail("byte_cutoff");
      return;
    }
    n = std::min(n, rule->bytes - rule_bytes_);
    cutoff = rule_bytes_ + n >= rule->bytes;
  }
  if (rule && rule->action == "bandwidth")
    n = std::min(n, std::max<std::size_t>(1, rule->rate / 20));
  writing_ = true;
  // Keep the backing chunk alive until async_write completes, including
  // cancellation.
  auto data = std::make_shared<std::vector<char>>(
      chunk.bytes.begin() + static_cast<std::ptrdiff_t>(chunk.offset),
      chunk.bytes.begin() + static_cast<std::ptrdiff_t>(chunk.offset + n));
  auto rate = (rule && rule->action == "bandwidth") ? rule->rate : 0;
  asio::async_write(
      destination_, asio::buffer(*data),
      [self = shared_from_this(), data, n, cutoff, rate](Error ec,
                                                         std::size_t) {
        self->writing_ = false;
        if (self->stopped_)
          return;
        if (ec) {
          self->fail("write_error");
          return;
        }
        self->activity_ = Clock::now();
        self->rule_bytes_ += n;
        auto &front = self->queue_.front();
        front.offset += n;
        self->queued_ -= n;
        if (front.offset == front.bytes.size())
          self->queue_.pop_front();
        if (rate)
          self->rate_due_ =
              Clock::now() +
              std::chrono::duration_cast<Clock::duration>(
                  std::chrono::duration<double>(static_cast<double>(n) /
                                                static_cast<double>(rate)));
        if (cutoff) {
          self->fail("byte_cutoff");
          return;
        }
        self->pump();
        self->read();
      });
}

class Proxy {
public:
  Proxy(asio::io_context &io, const Json &config, Events &events)
      : events_(events), rules_(io, events), control_(io), config_(config) {
    auto management = endpoint(config.at("control").get<std::string>());
    auto address = asio::ip::make_address(management.host);
    if (!address.is_loopback())
      throw std::invalid_argument("management listener must bind loopback; use "
                                  "SSH control forwarding remotely");
    bind(control_, {address, management.port});
    if (config.at("listeners").empty() || config.at("listeners").size() > 64)
      throw std::invalid_argument("require 1..64 proxy listeners");
    for (const auto &item : config.at("listeners")) {
      auto name = item.at("name").get<std::string>();
      if (name.empty() || listeners_.contains(name))
        throw std::invalid_argument("duplicate or empty listener name");
      auto ep = endpoint(item.at("listen"));
      auto upstream = endpoint(item.at("upstream"));
      auto acceptor = std::make_shared<Tcp::acceptor>(io);
      bind(*acceptor, {asio::ip::make_address(ep.host), ep.port});
      listeners_.emplace(name,
                         Listener{acceptor,
                                  {asio::ip::make_address(ep.host), ep.port},
                                  upstream});
    }
    rules_.changed = [this] {
      for (auto &[name, listener] : listeners_) {
        bool disabled = rules_.get(name, "request").has_value() &&
                        rules_.get(name, "request")->action == "disable";
        if (disabled && listener.acceptor->is_open()) {
          Error ignored;
          ++listener.generation;
          listener.acceptor->close(ignored);
        } else if (!disabled && !listener.acceptor->is_open()) {
          ++listener.generation;
          bind(*listener.acceptor, listener.address);
          accept(name);
        }
      }
      std::vector<std::shared_ptr<Session>> active;
      for (auto &[id, s] : sessions_)
        active.push_back(s);
      for (auto &s : active)
        s->wake();
    };
  }
  void start() {
    for (const auto &[name, l] : listeners_)
      accept(name);
    control_accept();
    events_.emit({{"event", "ready"},
                  {"mode", "proxy"},
                  {"listeners", config_.at("listeners")}});
  }

private:
  struct Listener {
    std::shared_ptr<Tcp::acceptor> acceptor;
    Tcp::endpoint address;
    Endpoint upstream;
    std::uint64_t generation = 0;
  };
  static void bind(Tcp::acceptor &acceptor, const Tcp::endpoint &address) {
    acceptor.open(address.protocol());
    acceptor.set_option(Tcp::acceptor::reuse_address(true));
    acceptor.bind(address);
    acceptor.listen(128);
  }
  void accept(const std::string &name) {
    auto &listener = listeners_.at(name);
    listener.acceptor->async_accept(
        [this, name, generation = listener.generation](Error ec,
                                                       Tcp::socket socket) {
          auto &l = listeners_.at(name);
          if (generation != l.generation)
            return;
          if (!ec) {
            if (sessions_.size() >= 4096) {
              Error ignored;
              socket.close(ignored);
              events_.emit({{"event", "connection_limit"}});
            } else {
              auto id = ++next_id_;
              auto session = std::make_shared<Session>(
                  std::move(socket), l.upstream, rules_, events_, name, id,
                  [this](auto key) { sessions_.erase(key); });
              sessions_[id] = session;
              session->start();
            }
          }
          if (l.acceptor->is_open())
            accept(name);
        });
  }
  void control_accept() {
    control_.async_accept([this](Error ec, Tcp::socket socket) {
      if (!ec)
        std::make_shared<HttpSession>(std::move(socket))
            ->read([this](auto session, auto method, auto path, Json data) {
              try {
                if (method == "GET" && path == "/healthz")
                  session->reply(
                      200, {{"ok", true}, {"connections", sessions_.size()}});
                else if (method == "POST" && path == "/rules") {
                  auto target = data.at("target").template get<std::string>();
                  if (!listeners_.contains(target))
                    throw std::invalid_argument("unknown target");
                  session->reply(200, rules_.apply(data));
                } else if (method == "DELETE" && path == "/rules") {
                  rules_.clear();
                  session->reply(200, {{"ok", true}});
                } else
                  session->reply(404, {{"error", "unknown control route"}});
              } catch (const std::exception &e) {
                session->reply(400, {{"error", e.what()}});
              }
            });
      if (control_.is_open())
        control_accept();
    });
  }
  Events &events_;
  Rules rules_;
  Tcp::acceptor control_;
  Json config_;
  std::unordered_map<std::string, Listener> listeners_;
  std::unordered_map<std::uint64_t, std::shared_ptr<Session>> sessions_;
  std::uint64_t next_id_ = 0;
};
} // namespace
void run_proxy(asio::io_context &io, const Json &config, Events &events) {
  auto proxy = std::make_shared<Proxy>(io, config, events);
  proxy->start();
  // An executor-owned guard keeps callbacks' raw owner pointers valid through
  // io.run().
  asio::steady_timer lifetime(io);
  int seconds = config.value("max_runtime_s", 600);
  if (seconds < 1 || seconds > 3600)
    throw std::invalid_argument("max_runtime_s must be 1..3600");
  lifetime.expires_after(std::chrono::seconds(seconds));
  lifetime.async_wait([&io, proxy](Error ec) {
    if (!ec)
      io.stop();
  });
  io.run();
}
} // namespace nf
