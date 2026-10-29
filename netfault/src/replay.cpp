#include "netfault/common.hpp"
#include <algorithm>
#include <cmath>
#include <deque>
#include <unordered_set>

namespace nf {
namespace {
struct Sample {
  int input = 0, output = 0;
  double first = 0;
  std::vector<double> gaps;
};
std::vector<Sample> samples(const Json &profile) {
  auto kind = profile.at("kind").get<std::string>();
  if (kind != "synthetic" && kind != "model-derived" && kind != "recorded")
    throw std::invalid_argument(
        "profile kind must be synthetic, model-derived or recorded");
  if (!profile.at("samples").is_array() || profile.at("samples").empty())
    throw std::invalid_argument("empty timing profile");
  std::vector<Sample> result;
  std::unordered_set<std::string> seen;
  for (const auto &row : profile.at("samples")) {
    Sample s{row.at("input_tokens"), row.at("output_tokens"),
             row.at("first_output_ms"),
             row.at("gaps_ms").get<std::vector<double>>()};
    if (s.input < 1 || s.output < 1 || s.output > 4096 ||
        s.gaps.size() != static_cast<std::size_t>(s.output - 1) ||
        !std::isfinite(s.first) || s.first < 0 || s.first > 300000)
      throw std::invalid_argument("invalid timing sample shape");
    for (auto gap : s.gaps)
      if (!std::isfinite(gap) || gap < 0 || gap > 60000)
        throw std::invalid_argument("invalid output gap");
    if (!seen.insert(std::to_string(s.input) + ":" + std::to_string(s.output))
             .second)
      throw std::invalid_argument("duplicate timing shape");
    result.push_back(std::move(s));
  }
  return result;
}
class Worker;
class Generation : public std::enable_shared_from_this<Generation> {
public:
  Generation(Worker &worker, std::shared_ptr<HttpSession> http, Sample sample,
             std::string id, std::string model, Events &events);
  void watch();
  void start();
  void cancel();
  bool finished() const { return finished_; }

private:
  void produce();
  void write();
  void finish(bool canceled);
  Worker &worker_;
  std::shared_ptr<HttpSession> http_;
  Sample sample_;
  std::string id_, model_;
  Events &events_;
  asio::steady_timer timer_;
  Clock::time_point accepted_ = Clock::now(), started_, next_;
  std::deque<std::string> pending_;
  int index_ = 0;
  bool writing_ = false, finished_ = false, produced_all_ = false;
};
class Worker {
public:
  Worker(asio::io_context &io, const Json &cfg, Events &events)
      : events_(events), acceptor_(io), id_(cfg.at("id")),
        model_(cfg.value("model", std::string("timing-replay"))),
        samples_(samples(read_json(cfg.at("profile")))),
        maximum_(cfg.value("max_active", 1)) {
    if (maximum_ < 1 || maximum_ > 128)
      throw std::invalid_argument("max_active must be 1..128");
    auto ep = endpoint(cfg.at("listen"));
    auto address = Tcp::endpoint(asio::ip::make_address(ep.host), ep.port);
    acceptor_.open(address.protocol());
    acceptor_.set_option(Tcp::acceptor::reuse_address(true));
    acceptor_.bind(address);
    acceptor_.listen(128);
  }
  void start() {
    accept();
    events_.emit({{"event", "ready"}, {"mode", "replay"}, {"worker", id_}});
  }
  void finished(Generation *generation) {
    active_.erase(generation);
    while (active_.size() < static_cast<std::size_t>(maximum_) &&
           !queue_.empty()) {
      auto next = queue_.front();
      queue_.pop_front();
      if (next->finished())
        continue;
      active_[next.get()] = next;
      next->start();
    }
  }
  const std::string &id() const { return id_; }
  int quiet_ms() const { return quiet_ms_; }

private:
  void accept() {
    acceptor_.async_accept([this](Error ec, Tcp::socket socket) {
      if (!ec)
        std::make_shared<HttpSession>(std::move(socket))
            ->read([this](auto http, auto method, auto path, Json data) {
              if (method == "GET" && (path == "/healthz" || path == "/stats")) {
                http->reply(200, {{"ok", true},
                                  {"worker", id_},
                                  {"active", active_.size()},
                                  {"waiting", queue_.size()}});
                return;
              }
              // Explicit fixture control: introduce one real quiet interval
              // after the first output, so an idle-expiry test exercises
              // expiry.
              if (method == "POST" && path == "/replay/quiet") {
                if (!data.is_object() || !data.contains("quiet_ms") ||
                    !data["quiet_ms"].is_number_integer() ||
                    data["quiet_ms"] < 0 || data["quiet_ms"] > 60000) {
                  http->reply(400, {{"error", "quiet_ms must be 0..60000"}});
                  return;
                }
                int ms = data["quiet_ms"];
                quiet_ms_ = ms;
                events_.emit({{"event", "quiet_changed"},
                              {"worker", id_},
                              {"quiet_ms", ms}});
                http->reply(200, {{"quiet_ms", ms}});
                return;
              }
              if (method != "POST" || path != "/v1/completions") {
                http->reply(404, {{"error", "unknown replay route"}});
                return;
              }
              try {
                if (!data.value("stream", false))
                  throw std::invalid_argument(
                      "replay endpoint requires stream=true");
                int input = data.at("replay_input_tokens");
                int output = data.at("max_tokens");
                auto match = std::find_if(
                    samples_.begin(), samples_.end(), [&](const auto &s) {
                      return s.input == input && s.output == output;
                    });
                if (match == samples_.end()) {
                  http->reply(422, {{"error", "unsupported timing shape; no "
                                              "interpolation or fallback"}});
                  return;
                }
                if (queue_.size() >= 64) {
                  http->reply(503, {{"error", "replay queue full"}});
                  return;
                }
                auto request = data.value(
                    "request_id",
                    std::string("replay-" + std::to_string(++next_id_)));
                auto task = std::make_shared<Generation>(
                    *this, http, *match, request, model_, events_);
                task->watch();
                if (active_.size() < static_cast<std::size_t>(maximum_)) {
                  active_[task.get()] = task;
                  task->start();
                } else
                  queue_.push_back(task);
              } catch (const std::exception &e) {
                http->reply(400, {{"error", e.what()}});
              }
            });
      if (acceptor_.is_open())
        accept();
    });
  }
  Events &events_;
  Tcp::acceptor acceptor_;
  std::string id_, model_;
  std::vector<Sample> samples_;
  int maximum_;
  int quiet_ms_ = 0;
  std::deque<std::shared_ptr<Generation>> queue_;
  std::unordered_map<Generation *, std::shared_ptr<Generation>> active_;
  std::uint64_t next_id_ = 0;
};
Generation::Generation(Worker &worker, std::shared_ptr<HttpSession> http,
                       Sample sample, std::string id, std::string model,
                       Events &events)
    : worker_(worker), http_(std::move(http)), sample_(std::move(sample)),
      id_(std::move(id)), model_(std::move(model)), events_(events),
      timer_(http_->socket().get_executor()) {}
void Generation::watch() {
  http_->watch_disconnect([weak = weak_from_this()] {
    if (auto self = weak.lock())
      self->cancel();
  });
}
void Generation::start() {
  if (finished_)
    return;
  started_ = Clock::now();
  next_ =
      started_ + std::chrono::duration_cast<Clock::duration>(
                     std::chrono::duration<double, std::milli>(sample_.first));
  events_.emit({{"event", "generation_started"},
                {"worker", worker_.id()},
                {"request_id", id_},
                {"queue_ms",
                 std::chrono::duration<double, std::milli>(started_ - accepted_)
                     .count()}});
  writing_ = true;
  http_->send(
      "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: "
      "no-cache\r\nConnection: close\r\n\r\n",
      [self = shared_from_this()](Error ec) {
        self->writing_ = false;
        if (ec)
          self->cancel();
        else
          self->write();
      });
  produce();
}
void Generation::produce() {
  if (finished_ || produced_all_)
    return;
  // Production and socket delivery are separately scheduled. At the explicit
  // queue bound we pause production rather than accumulate unbounded output.
  if (pending_.size() >= 64) {
    timer_.expires_after(Millis(10));
    timer_.async_wait([self = shared_from_this()](Error ec) {
      if (!ec)
        self->produce();
    });
    return;
  }
  timer_.expires_at(next_);
  timer_.async_wait([self = shared_from_this()](Error ec) {
    if (ec || self->finished_)
      return;
    Json event = {{"id", self->id_},
                  {"object", "text_completion"},
                  {"model", self->model_},
                  {"choices", Json::array({{{"index", 0},
                                            {"text", "x"},
                                            {"finish_reason", nullptr}}})}};
    self->pending_.push_back("data: " + event.dump() + "\n\n");
    self->events_.emit({{"event", "output_produced"},
                        {"request_id", self->id_},
                        {"worker", self->worker_.id()},
                        {"index", self->index_}});
    ++self->index_;
    if (self->index_ >= self->sample_.output) {
      self->pending_.push_back("data: [DONE]\n\n");
      self->produced_all_ = true;
    } else {
      self->next_ += std::chrono::duration_cast<Clock::duration>(
          std::chrono::duration<double, std::milli>(
              self->sample_.gaps[static_cast<std::size_t>(self->index_ - 1)]));
      if (self->index_ == 1)
        self->next_ += Millis(self->worker_.quiet_ms());
      self->produce();
    }
    self->write();
  });
}
void Generation::write() {
  if (finished_ || writing_ || pending_.empty())
    return;
  writing_ = true;
  auto data = std::move(pending_.front());
  pending_.pop_front();
  http_->send(std::move(data), [self = shared_from_this()](Error ec) {
    self->writing_ = false;
    if (self->finished_)
      return;
    if (ec) {
      self->cancel();
      return;
    }
    if (self->produced_all_ && self->pending_.empty())
      self->finish(false);
    else
      self->write();
  });
}
void Generation::finish(bool canceled) {
  if (finished_)
    return;
  finished_ = true;
  timer_.cancel();
  http_->close();
  events_.emit(
      {{"event", canceled ? "generation_canceled" : "generation_completed"},
       {"request_id", id_},
       {"worker", worker_.id()},
       {"produced", index_}});
  worker_.finished(this);
}
void Generation::cancel() { finish(true); }
} // namespace
void run_replay(asio::io_context &io, const Json &config, Events &events) {
  std::vector<std::unique_ptr<Worker>> workers;
  if (config.at("workers").empty() || config.at("workers").size() > 64)
    throw std::invalid_argument("require 1..64 replay workers");
  for (const auto &cfg : config.at("workers")) {
    workers.push_back(std::make_unique<Worker>(io, cfg, events));
    workers.back()->start();
  }
  asio::steady_timer lifetime(io);
  int seconds = config.value("max_runtime_s", 600);
  if (seconds < 1 || seconds > 3600)
    throw std::invalid_argument("max_runtime_s must be 1..3600");
  lifetime.expires_after(std::chrono::seconds(seconds));
  lifetime.async_wait([&io](Error ec) {
    if (!ec)
      io.stop();
  });
  io.run();
}
} // namespace nf
