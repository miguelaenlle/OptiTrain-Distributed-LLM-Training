#pragma once
#include <algorithm>
#include <deque>
#include <functional>
#include <json/json.hpp>
#include <map>
#include <random>
#include <string>
#include <vector>

namespace fleetlab {
// All lifecycle decisions run as ASTRA events. Python only answers scheduler
// commands; it neither advances a second clock nor invents compute durations.
class Controller {
  using J = nlohmann::json;
  using Tick = uint64_t;
  struct Worker {
    std::string state = "Ready";
    bool observed = true, allocated = true, busy = false;
    uint64_t generation = 0;
    double report = 0, reserved = 0, cost = 0, dispatch_total = 0,
           report_base = 0;
    Tick billing_start = 0, billed_ns = 0, allocated_ns = 0,
         observation_stamp = 0;
    J batch = nullptr;
    uint64_t scheduled_tokens = 0, lost_tokens = 0;
    Tick observed_recovery = 0;
    J peaks = {{"waiting", 0}, {"active", 0}, {"kv_bytes", 0}};
    J stats = J::object();
  };
  struct Request {
    J input;
    std::string state = "NotArrived";
    int worker = -1, attempts = 0, delivered = 0;
    Tick first = 0, last = 0, finish = 0;
    std::vector<Tick> gaps;
  };
  J config;
  std::vector<Worker> workers;
  std::map<int, Request> requests;
  std::map<int, int> attempt_request;
  std::deque<int> waiting;
  std::function<Tick()> now;
  std::function<void(Tick, int, std::function<void()>)> at;
  std::function<void(J)> emit;
  std::function<size_t(int)> cancel;
  bool dispatch_pending = false;
  J incidents = J::array();
  int rr = 0;
  bool terminated = false;
  std::mt19937_64 hazard_rng;
  Tick horizon, discovery, report_period, report_delay, deadline, backoff;
  static bool terminal(const Request &r) {
    return r.state == "Completed" || r.state == "Failed" ||
           r.state == "TimedOut" || r.state == "Rejected" ||
           r.state == "Censored";
  }
  double rate(int w) const {
    return config["prices"]
                 [w < config["regular"].get<int>() ? "regular" : "spot"]
                     .get<double>();
  }
  void record(int id) {
    auto &r = requests.at(id);
    emit({{"event", "request"},
          {"id", id},
          {"state", r.state},
          {"worker", r.worker},
          {"attempts", r.attempts},
          {"attempt_id", id * 1024 + r.attempts},
          {"incarnation",
           r.worker >= 0 ? J(workers[r.worker].generation) : J(nullptr)},
          {"delivered", r.delivered}});
  }
  void worker_record(int w) {
    emit({{"event", "worker"},
          {"worker", w},
          {"state", workers[w].state},
          {"observed_ready", workers[w].observed},
          {"incarnation", workers[w].generation},
          {"allocated", workers[w].allocated}});
  }
  void schedule() {
    if (dispatch_pending)
      return;
    dispatch_pending = true;
    at(now(), 5, [this] {
      dispatch_pending = false;
      dispatch();
    });
  }
  void dispatch() {
    size_t count = waiting.size();
    while (count-- && !waiting.empty()) {
      int id = waiting.front();
      waiting.pop_front();
      auto &r = requests.at(id);
      if (terminal(r))
        continue;
      int chosen = -1;
      for (int k = 0; k < int(workers.size()); ++k) {
        int w = (rr + k) % workers.size();
        if (!workers[w].observed ||
            workers[w].stats.value("waiting", 0) >=
                config.value("worker_admission_limit", 4096))
          continue;
        if (chosen < 0 ||
            (config["policy"] == "least_work" &&
             workers[w].report + workers[w].reserved <
                 workers[chosen].report + workers[chosen].reserved))
          chosen = w;
        if (config["policy"] == "round_robin")
          break;
      }
      if (chosen < 0) {
        waiting.push_back(id);
        continue;
      }
      rr = (chosen + 1) % workers.size();
      r.worker = chosen;
      ++r.attempts;
      int aid = id * 1024 + r.attempts;
      attempt_request[aid] = id;
      r.state = "Assigned";
      record(id);
      workers[chosen].dispatch_total += r.input["prompt_length"].get<int>() +
                                        r.input["output_length"].get<int>();
      workers[chosen].reserved =
          workers[chosen].dispatch_total - workers[chosen].report_base;
      if (workers[chosen].state != "Ready") {
        // A dispatch can use stale health, but cannot execute on a dead
        // replica.
        int attempt = r.attempts;
        at(now() + config.value("dispatch_error_ns", discovery), 2,
           [this, id, attempt] {
             auto &request = requests.at(id);
             if (request.attempts == attempt && !terminal(request))
               retry_or_fail(id, "stale_health");
           });
        continue;
      }
      J input = r.input;
      input["attempt_id"] = aid;
      emit({{"event", "admit"}, {"worker", chosen}, {"request", input}});
    }
    for (int w = 0; w < int(workers.size()); ++w)
      if (workers[w].state == "Ready" && !workers[w].busy &&
          (workers[w].stats.value("active", 0) > 0 ||
           workers[w].stats.value("waiting", 0) > 0)) {
        workers[w].busy = true;
        emit({{"event", "schedule"}, {"worker", w}});
      }
  }
  void retry_or_fail(int id, const std::string &reason) {
    auto &r = requests.at(id);
    if (terminal(r))
      return;
    if (r.delivered || r.attempts >= config.value("max_attempts", 3)) {
      r.state = "Failed";
      r.finish = now();
      record(id);
      return;
    }
    r.state = "RetryWait";
    record(id);
    emit({{"event", "retry_reason"}, {"id", id}, {"reason", reason}});
    at(now() + backoff, 3, [this, id] {
      auto &r = requests.at(id);
      if (terminal(r))
        return;
      r.state = "Waiting";
      r.worker = -1;
      waiting.push_back(id);
      record(id);
      schedule();
    });
  }
  void observe(int w, uint64_t generation, bool ready, Tick stamp) {
    if (workers[w].generation != generation ||
        stamp < workers[w].observation_stamp)
      return;
    workers[w].observation_stamp = stamp;
    workers[w].observed = ready;
    if (ready)
      workers[w].observed_recovery = now();
    worker_record(w);
    schedule();
  }
  void fail_worker(int w, bool terminate, Tick availability, Tick init,
                   Tick load) {
    auto &worker = workers.at(w);
    if (worker.state == "Down") {
      emit({{"event", "duplicate_fault"}, {"worker", w}});
      return;
    }
    auto removed = cancel(w);
    ++worker.generation;
    worker.state = "Down";
    if (terminate && worker.allocated) {
      Tick duration = now() - worker.billing_start;
      worker.allocated_ns += duration;
      Tick billed =
          std::max(duration, config.value("minimum_billing_ns", Tick(0)));
      worker.cost += billed * rate(w) / 3.6e12;
      worker.billed_ns += billed;
      worker.allocated = false;
    }
    emit(
        {{"event", "cancelled"},
         {"worker", w},
         {"callbacks_removed", removed},
         {"incarnation", worker.generation},
         {"lost_scheduled_tokens", worker.busy ? worker.scheduled_tokens : 0}});
    if (worker.busy)
      worker.lost_tokens += worker.scheduled_tokens;
    worker.busy = false;
    worker.scheduled_tokens = 0;
    emit({{"event", "reset"}, {"worker", w}});
    worker_record(w);
    for (auto &[id, r] : requests)
      if (r.worker == w && !terminal(r) && r.state != "RetryWait") {
        int attempt = r.attempts;
        at(now() + discovery, 2, [this, id, attempt] {
          auto &request = requests.at(id);
          if (request.attempts == attempt && !terminal(request))
            retry_or_fail(id, "worker_crash");
        });
      }
    if (worker.allocated)
      schedule_hazard(w);
    auto generation = worker.generation;
    Tick failure_stamp = now();
    at(now() + discovery, 2, [this, w, generation, failure_stamp] {
      observe(w, generation, false, failure_stamp);
    });
    at(now() + availability, 2, [this, w, generation, init, load] {
      auto &worker = workers[w];
      if (worker.generation != generation)
        return;
      if (!worker.allocated) {
        worker.allocated = true;
        worker.billing_start = now();
        schedule_hazard(w);
      }
      worker.state = "Initializing";
      worker_record(w);
      at(now() + init, 2, [this, w, generation, load] {
        if (workers[w].generation != generation)
          return;
        workers[w].state = "Loading";
        worker_record(w);
        at(now() + load, 2, [this, w, generation] {
          if (workers[w].generation != generation)
            return;
          workers[w].state = "Ready";
          worker_record(w);
          Tick ready_stamp = now();
          at(now() + discovery, 2, [this, w, generation, ready_stamp] {
            observe(w, generation, true, ready_stamp);
          });
          schedule();
        });
      });
    });
  }
  void report() {
    for (int w = 0; w < int(workers.size()); ++w) {
      auto generation = workers[w].generation;
      double value = workers[w].stats.value("estimated_work", 0.0);
      double reservation_at_sample = workers[w].dispatch_total;
      at(now() + report_delay, 2,
         [this, w, generation, value, reservation_at_sample] {
           if (workers[w].generation != generation)
             return;
           workers[w].report = value;
           workers[w].report_base =
               std::max(workers[w].report_base, reservation_at_sample);
           workers[w].reserved =
               workers[w].dispatch_total - workers[w].report_base;
           emit(
               {{"event", "work_observation"}, {"worker", w}, {"work", value}});
         });
    }
    if (now() + report_period <= horizon)
      at(now() + report_period, 2, [this] { report(); });
  }
  void schedule_hazard(int w) {
    if (!config.value("stochastic_failures", false))
      return;
    double hours = w < config["regular"].get<int>()
                       ? config.value("regular_mean_active_hours", 1000.0)
                       : config.value("spot_mean_active_hours", 24.0);
    std::exponential_distribution<double> draw(1.0 / (hours * 3.6e12));
    Tick delay = std::max(Tick(1), Tick(draw(hazard_rng)));
    auto generation = workers[w].generation;
    if (now() + delay > horizon)
      return;
    at(now() + delay, 0, [this, w, generation] {
      if (workers[w].generation != generation || !workers[w].allocated)
        return;
      if (workers[w].state == "Down") {
        schedule_hazard(w);
        return;
      }
      bool spot = w >= config["regular"].get<int>();
      emit({{"event", "assumed_hazard_fault"}, {"worker", w}});
      fail_worker(w, spot, config.value("availability_ns", Tick(60000000000)),
                  config.value("init_ns", Tick(5000000000)),
                  config.value("load_ns", Tick(30000000000)));
    });
  }
  void check_certificate() {
    if (terminated)
      return;
    int impossible_completion = 0, impossible_compliance = 0;
    Tick ttft_limit = config.value("ttft_limit_ns", Tick(1000000000)),
         itl_limit = config.value("itl_limit_ns", Tick(100000000));
    for (const auto &[id, r] : requests) {
      bool failed =
          r.state == "Failed" || r.state == "TimedOut" || r.state == "Rejected";
      impossible_completion += failed;
      bool bad = failed;
      Tick arrival = r.input.at("arrival_ns");
      if (now() >= arrival) {
        if (!r.delivered && now() > arrival + ttft_limit)
          bad = true;
        if (r.delivered && r.first - arrival > ttft_limit)
          bad = true;
        for (Tick gap : r.gaps)
          bad |= gap > itl_limit;
        if (r.delivered && !terminal(r) && now() > r.last + itl_limit)
          bad = true;
      }
      impossible_compliance += bad;
    }
    double best_completion =
        1.0 - double(impossible_completion) / requests.size();
    double best_compliance =
        1.0 - double(impossible_compliance) / requests.size();
    if (best_completion + 1e-12 < config.value("completion_threshold", .99) ||
        best_compliance + 1e-12 < config.value("compliance_threshold", .95)) {
      terminated = true;
      J certificate = {
          {"planned_requests", requests.size()},
          {"irreversible_noncompletion", impossible_completion},
          {"irreversible_noncompliance", impossible_compliance},
          {"maximum_possible_completion", best_completion},
          {"maximum_possible_compliance", best_compliance},
          {"time_ns", now()},
          {"reason",
           "even perfect future outcomes cannot meet all-offered thresholds"}};
      auto value = summary();
      value["partial"] = true;
      value["certificate"] = certificate;
      emit({{"event", "finished"}, {"summary", value}});
      return;
    }
    if (now() + 1000000000 <= horizon)
      at(now() + 1000000000, 6, [this] { check_certificate(); });
  }

public:
  Controller(J cfg, std::function<Tick()> clock,
             std::function<void(Tick, int, std::function<void()>)> enqueue,
             std::function<void(J)> output,
             std::function<size_t(int)> cancellation)
      : config(std::move(cfg)), now(clock), at(enqueue), emit(output),
        cancel(cancellation) {
    horizon = config.at("horizon_ns");
    discovery = config.value("discovery_ns", Tick(5000000000));
    report_period = config.value("report_period_ns", Tick(1000000000));
    report_delay = config.value("report_delay_ns", Tick(5000000000));
    deadline = config.value("deadline_ns", Tick(60000000000));
    backoff = config.value("backoff_ns", Tick(100000000));
    workers.resize(config["regular"].get<int>() + config["spot"].get<int>());
    hazard_rng.seed(config.value("seed", 42));
    for (int w = 0; w < int(workers.size()); ++w)
      schedule_hazard(w);
    for (int w = 0; w < int(workers.size()); ++w)
      worker_record(w);
    for (const auto &input : config.at("requests")) {
      int id = input.at("id");
      requests[id].input = input;
      Tick arrival = input.at("arrival_ns");
      at(arrival, 3, [this, id] {
        auto &r = requests.at(id);
        if (waiting.size() >= config.value("waiting_limit", 4096)) {
          r.state = "Rejected";
          r.finish = now();
        } else {
          r.state = "Waiting";
          waiting.push_back(id);
          schedule();
        }
        record(id);
      });
      at(arrival + deadline, 4, [this, id] {
        auto &r = requests.at(id);
        if (terminal(r))
          return;
        if (r.worker >= 0)
          emit({{"event", "abort"},
                {"worker", r.worker},
                {"attempt_id", id * 1024 + r.attempts}});
        r.state = "TimedOut";
        r.finish = now();
        record(id);
      });
    }
    for (auto &fault : config.at("faults")) {
      if (fault.value("scope", std::string("worker")) == "node") {
        J placement = config.value("placement", J::array());
        if (placement.empty())
          for (int i = 0; i < int(workers.size()); ++i)
            placement.push_back(i);
        std::vector<int> nodes;
        for (int w : fault.at("workers"))
          nodes.push_back(placement.at(w));
        J affected = J::array();
        for (int w = 0; w < int(workers.size()); ++w)
          if (std::find(nodes.begin(), nodes.end(),
                        placement.at(w).get<int>()) != nodes.end())
            affected.push_back(w);
        fault["workers"] = affected;
      }
      Tick tick = fault.at("time_ns");
      at(tick, 0, [this, fault] {
        emit({{"event", "fault"}, {"fault", fault}});
        J incident = fault;
        incident["active_request_ids"] = J::array();
        incident["affected_inflight_ids"] = J::array();
        incident["interrupted_stream_ids"] = J::array();
        for (const auto &[id, r] : requests)
          if (!terminal(r) && r.input.at("arrival_ns").get<Tick>() <= now()) {
            incident["active_request_ids"].push_back(id);
            bool affected = std::find(fault.at("workers").begin(),
                                      fault.at("workers").end(),
                                      r.worker) != fault.at("workers").end();
            if (affected) {
              incident["affected_inflight_ids"].push_back(id);
              if (r.delivered)
                incident["interrupted_stream_ids"].push_back(id);
            }
          }
        incidents.push_back(incident);
        for (int w : fault.at("workers"))
          fail_worker(w, fault.value("terminate", false),
                      fault.value("availability_ns", Tick(60000000000)),
                      fault.value("init_ns", Tick(5000000000)),
                      fault.value("load_ns", Tick(30000000000)));
      });
    }
    at(0, 2, [this] { report(); });
    if (config.value("optimizer_prune", false))
      at(1000000000, 6, [this] { check_certificate(); });
    at(horizon, 6, [this] {
      if (terminated)
        return;
      terminated = true;
      for (auto &[id, r] : requests)
        if (!terminal(r)) {
          r.state = "Censored";
          r.finish = now();
          record(id);
        }
      emit({{"event", "finished"}, {"summary", summary()}});
    });
  }
  void stats(int w, const J &value) {
    workers.at(w).stats = value;
    for (auto field : {"waiting", "active", "kv_bytes"})
      workers[w].peaks[field] = std::max(workers[w].peaks[field].get<double>(),
                                         value.value(field, 0.0));
    emit({{"event", "stats"},
          {"worker", w},
          {"incarnation", workers[w].generation},
          {"value", value}});
  }
  void scheduled(int w, bool active, const J &result) {
    auto &worker = workers.at(w);
    worker.busy = active;
    worker.batch = result.at("batch");
    worker.scheduled_tokens = 0;
    if (!active)
      return;
    for (auto &attempt : result.at("attempts")) {
      int aid = attempt.at("id");
      auto found = attempt_request.find(aid);
      if (found == attempt_request.end())
        continue;
      int id = found->second;
      auto &r = requests.at(id);
      worker.scheduled_tokens += attempt.at("scheduled").get<uint64_t>();
      if (terminal(r) || r.worker != w || aid != id * 1024 + r.attempts)
        continue;
      r.state = r.delivered ? "Streaming" : "Running";
      record(id);
    }
  }
  void batch_done(int w) { workers.at(w).busy = false; }
  void deliver(int w, const J &value) {
    Tick delay = config.value("delivery_delay_ns", Tick(0));
    auto generation = workers[w].generation;
    auto batch = workers[w].batch;
    auto output = value.at("tokens");
    schedule();
    if (delay == 0) {
      tokens(w, output, batch);
      return;
    }
    at(now() + delay, 1, [this, w, generation, batch, output] {
      if (workers[w].generation != generation) {
        emit({{"event", "stale_delivery_suppressed"},
              {"worker", w},
              {"batch", batch},
              {"incarnation", generation}});
        return;
      }
      tokens(w, output, batch);
    });
  }
  void tokens(int w, const J &tokens, const J &batch_tag) {
    for (const auto &token : tokens) {
      int aid = token.at("id");
      auto found = attempt_request.find(aid);
      if (found == attempt_request.end())
        continue;
      int id = found->second;
      auto &r = requests.at(id);
      if (terminal(r) || r.worker != w || id * 1024 + r.attempts != aid)
        continue;
      int count = token.at("produced");
      if (count <= 0)
        continue;
      r.state = "Streaming";
      for (int i = 0; i < count; ++i) {
        if (!r.delivered)
          r.first = now();
        else
          r.gaps.push_back(now() - r.last);
        ++r.delivered;
        r.last = now();
        emit({{"event", "token"},
              {"id", id},
              {"attempt_id", aid},
              {"worker", w},
              {"ordinal", r.delivered},
              {"incarnation", workers[w].generation},
              {"batch", batch_tag}});
      }
      if (r.delivered >= r.input["output_length"].get<int>()) {
        r.state = "Completed";
        r.finish = now();
      }
      record(id);
    }
    schedule();
  }
  J summary() const {
    J rows = J::array(), costs = J::array(), diagnostics = J::array();
    double cost = 0;
    for (int w = 0; w < int(workers.size()); ++w) {
      auto &worker = workers[w];
      double usd =
          worker.cost +
          (worker.allocated
               ? std::max(now() - worker.billing_start,
                          config.value("minimum_billing_ns", Tick(0))) *
                     rate(w) / 3.6e12
               : 0);
      costs.push_back(
          {{"worker", w},
           {"usd", usd},
           {"allocated_ns",
            worker.allocated_ns +
                (worker.allocated ? now() - worker.billing_start : 0)}});
      costs.back()["billed_ns"] =
          worker.billed_ns +
          (worker.allocated
               ? std::max(now() - worker.billing_start,
                          config.value("minimum_billing_ns", Tick(0)))
               : 0);
      cost += usd;
      diagnostics.push_back(
          {{"worker", w},
           {"peaks", worker.peaks},
           {"lost_scheduled_tokens", worker.lost_tokens},
           {"last_observed_recovery_ns", worker.observed_recovery},
           {"final_stats", worker.stats}});
    }
    for (auto &[id, r] : requests) {
      Tick arrival = r.input["arrival_ns"];
      rows.push_back(
          {{"id", id},
           {"arrival_ns", arrival},
           {"state", r.state},
           {"attempts", r.attempts},
           {"delivered", r.delivered},
           {"first_token_ns", r.first},
           {"last_token_ns", r.last},
           {"ttft_ns", r.delivered ? J(r.first - arrival) : J(nullptr)},
           {"itl_ns", r.gaps},
           {"finish_ns", r.finish}});
    }
    return {{"requests", rows},       {"worker_costs", costs},
            {"total_usd", cost},      {"worker_diagnostics", diagnostics},
            {"incidents", incidents}, {"end_ns", now()}};
  }
};
} // namespace fleetlab
