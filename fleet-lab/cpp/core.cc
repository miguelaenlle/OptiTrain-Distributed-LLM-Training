#include <algorithm>
#include <cmath>
#include <fstream>
#include <iostream>
#include <json/json.hpp>
#include <map>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>
using J = nlohmann::json;
using Tick = uint64_t;

J percentile(std::vector<Tick> values) {
  if (values.empty())
    return nullptr;
  std::sort(values.begin(), values.end());
  return values.at(size_t(std::ceil(.95 * values.size())) - 1);
}
J distribution(std::vector<Tick> values) {
  if (values.empty())
    return {{"samples", 0}, {"unit", "ns"}};
  std::sort(values.begin(), values.end());
  J result = {{"samples", values.size()},
              {"unit", "ns"},
              {"min", values.front()},
              {"max", values.back()}};
  for (auto pair : {std::pair<const char *, double>{"p50", .5},
                    {"p90", .9},
                    {"p95", .95},
                    {"p99", .99}})
    result[pair.first] =
        values.at(size_t(std::ceil(pair.second * values.size())) - 1);
  J histogram = J::array();
  Tick previous = 0;
  for (Tick bound :
       {Tick(10000000), Tick(25000000), Tick(50000000), Tick(100000000),
        Tick(250000000), Tick(1000000000), Tick(5000000000), UINT64_MAX}) {
    size_t count = std::count_if(values.begin(), values.end(), [&](Tick x) {
      return x >= previous && x < bound;
    });
    histogram.push_back({{"from_ns", previous},
                         {"to_ns", bound == UINT64_MAX ? J(nullptr) : J(bound)},
                         {"count", count}});
    previous = bound;
  }
  result["histogram"] = histogram;
  return result;
}
J cohort(const J &rows, const J &cfg) {
  int completed = 0, compliant = 0;
  std::vector<Tick> ttft, gaps;
  J outcomes = J::object();
  Tick ttft_limit = cfg.value("ttft_limit_ns", Tick(1000000000)),
       itl_limit = cfg.value("itl_limit_ns", Tick(100000000));
  Tick deadline = cfg.value("deadline_ns", Tick(60000000000));
  for (const auto &row : rows) {
    std::string state = row.at("state");
    outcomes[state] = outcomes.value(state, 0) + 1;
    if (state != "Completed")
      continue;
    ++completed;
    Tick first = row.at("ttft_ns");
    ttft.push_back(first);
    bool good =
        first <= ttft_limit && row.at("finish_ns").get<Tick>() <=
                                   row.at("arrival_ns").get<Tick>() + deadline;
    for (Tick gap : row.at("itl_ns")) {
      gaps.push_back(gap);
      good &= gap <= itl_limit;
    }
    compliant += good;
  }
  return {{"offered", rows.size()},
          {"completed", completed},
          {"compliant", compliant},
          {"completion_rate",
           rows.empty() ? J(nullptr) : J(double(completed) / rows.size())},
          {"compliance_rate",
           rows.empty() ? J(nullptr) : J(double(compliant) / rows.size())},
          {"conditional_ttft_p95_ns", percentile(ttft)},
          {"conditional_itl_p95_ns", percentile(gaps)},
          {"ttft_sample_count", ttft.size()},
          {"itl_sample_count", gaps.size()},
          {"outcomes", outcomes}};
}
J metrics(const J &summary, const J &cfg) {
  if (summary.value("partial", false)) {
    J observed = J::array();
    Tick end = summary.at("end_ns");
    for (auto &row : summary.at("requests"))
      if (row.at("arrival_ns").get<Tick>() <= end)
        observed.push_back(row);
    return {{"feasible", false},
            {"complete_metrics", false},
            {"certificate", summary.at("certificate")},
            {"accrued_usd", summary.at("total_usd")},
            {"observed_prefix_metrics", cohort(observed, cfg)}};
  }
  auto result = cohort(summary.at("requests"), cfg);
  double cost = summary.at("total_usd");
  int delivered = 0;
  for (auto &r : summary.at("requests"))
    delivered += r.at("delivered").get<int>();
  result["total_usd"] = cost;
  result["delivered_tokens"] = delivered;
  result["usd_per_delivered_token"] =
      delivered ? J(cost / delivered) : J(nullptr);
  for (auto kind : {"completed", "compliant"}) {
    int count = result.at(kind);
    result[std::string("usd_per_") + kind + "_request"] =
        count ? J(cost / count) : J(nullptr);
  }
  result["feasible"] = !result["completion_rate"].is_null() &&
                       result["completion_rate"].get<double>() >=
                           cfg.value("completion_threshold", .99) &&
                       result["compliance_rate"].get<double>() >=
                           cfg.value("compliance_threshold", .95) &&
                       !result["conditional_ttft_p95_ns"].is_null() &&
                       result["conditional_ttft_p95_ns"].get<Tick>() <=
                           cfg.value("ttft_limit_ns", Tick(1000000000)) &&
                       (result["conditional_itl_p95_ns"].is_null() ||
                        result["conditional_itl_p95_ns"].get<Tick>() <=
                            cfg.value("itl_limit_ns", Tick(100000000)));
  std::vector<Tick> ttfts, itls, tpots, e2e, delivered_itls;
  for (const auto &row : summary.at("requests")) {
    for (Tick gap : row.at("itl_ns"))
      delivered_itls.push_back(gap);
    if (row.at("state") != "Completed")
      continue;
    ttfts.push_back(row.at("ttft_ns"));
    Tick total = 0;
    for (Tick gap : row.at("itl_ns")) {
      itls.push_back(gap);
      total += gap;
    }
    if (!row.at("itl_ns").empty())
      tpots.push_back(total / row.at("itl_ns").size());
    e2e.push_back(row.at("finish_ns").get<Tick>() -
                  row.at("arrival_ns").get<Tick>());
  }
  result["distributions"] = {
      {"completed_ttft", distribution(ttfts)},
      {"completed_itl", distribution(itls)},
      {"completed_tpot", distribution(tpots)},
      {"completed_e2e", distribution(e2e)},
      {"all_delivered_itl", distribution(delivered_itls)}};
  double duration = cfg.at("horizon_ns").get<double>() / 1e9;
  result["rates"] = {{"window_start_ns", 0},
                     {"window_end_ns", cfg.at("horizon_ns")},
                     {"completed_requests_per_second",
                      result.at("completed").get<double>() / duration},
                     {"compliant_requests_per_second",
                      result.at("compliant").get<double>() / duration},
                     {"delivered_tokens_per_second", delivered / duration}};
  result["worker_diagnostics"] =
      summary.value("worker_diagnostics", J::array());
  result["incident_cohorts"] = J::array();
  for (auto &fault : cfg.at("faults")) {
    Tick start = fault.at("time_ns"),
         end = std::min(cfg.at("horizon_ns").get<Tick>(),
                        start +
                            fault.value("availability_ns", Tick(60000000000)) +
                            fault.value("init_ns", Tick(5000000000)) +
                            fault.value("load_ns", Tick(30000000000)) +
                            cfg.value("discovery_ns", Tick(5000000000)) +
                            Tick(120000000000));
    J rows = J::array();
    for (auto &r : summary.at("requests")) {
      Tick arrival = r.at("arrival_ns");
      if (arrival >= start && arrival <= end)
        rows.push_back(r);
    }
    auto item = cohort(rows, cfg);
    item["cohort_definition"] =
        "original arrivals within inclusive incident observation window";
    J active = J::array();
    size_t incident_idx = result["incident_cohorts"].size();
    if (summary.contains("incidents") &&
        incident_idx < summary.at("incidents").size()) {
      const auto &incident = summary.at("incidents")[incident_idx];
      for (auto id : incident.at("active_request_ids"))
        for (const auto &r : summary.at("requests"))
          if (r.at("id") == id)
            active.push_back(r);
      item["affected_inflight_ids"] = incident.at("affected_inflight_ids");
      item["interrupted_stream_ids"] = incident.at("interrupted_stream_ids");
    }
    item["active_at_fault"] = cohort(active, cfg);
    size_t delivered_window = 0;
    std::vector<Tick> window_gaps;
    for (const auto &r : summary.at("requests"))
      if (r.value("delivered", 0) > 0) {
        Tick delivery = r.value("first_token_ns", Tick(0));
        if (delivery >= start && delivery <= end)
          ++delivered_window;
        for (Tick gap : r.at("itl_ns")) {
          delivery += gap;
          if (delivery >= start && delivery <= end) {
            ++delivered_window;
            window_gaps.push_back(gap);
          }
        }
      }
    item["delivery_window"] = {{"token_samples", delivered_window},
                               {"itl", distribution(window_gaps)}};
    item["start_ns"] = start;
    item["end_ns"] = end;
    result["incident_cohorts"].push_back(item);
  }
  return result;
}
void reduce(J &workers, J &requests, const J &event) {
  std::string type = event.at("event");
  if (type == "worker")
    workers[std::to_string(event.at("worker").get<int>())].update(event);
  if (type == "request")
    requests[std::to_string(event.at("id").get<int>())] = event;
  if (type == "stats")
    workers[std::to_string(event.at("worker").get<int>())]["stats"] =
        event.at("value");
}
J build_index(const J &command) {
  std::ifstream file(command.at("path").get<std::string>());
  if (!file)
    throw std::invalid_argument("Cannot open journal");
  J workers = J::object(), requests = J::object(), snapshots = J::array(),
    offsets = J::array();
  J by_worker = J::object(), by_request = J::object(), by_event = J::object();
  snapshots.push_back({{"time_ns", 0},
                       {"offset", 0},
                       {"workers", workers},
                       {"requests", requests}});
  std::string line;
  size_t count = 0;
  uint64_t offset = 0;
  while (std::getline(file, line)) {
    J event = J::parse(line);
    reduce(workers, requests, event);
    ++count;
    J location = {event.at("time_ns"), offset};
    offsets.push_back(location);
    std::string type = event.at("event");
    if (!by_event.contains(type))
      by_event[type] = J::array();
    by_event[type].push_back(location);
    if (event.contains("worker")) {
      auto key = std::to_string(event.at("worker").get<int>());
      if (!by_worker.contains(key))
        by_worker[key] = J::array();
      by_worker[key].push_back(location);
    }
    if (event.contains("id")) {
      auto key = std::to_string(event.at("id").get<int>());
      if (!by_request.contains(key))
        by_request[key] = J::array();
      by_request[key].push_back(location);
    }
    offset = uint64_t(file.tellg());
    if (count % command.value("interval", size_t(5000)) == 0)
      snapshots.push_back({{"time_ns", event.at("time_ns")},
                           {"offset", offset},
                           {"workers", workers},
                           {"requests", requests}});
  }
  return {{"snapshots", snapshots}, {"offsets", offsets},
          {"by_worker", by_worker}, {"by_request", by_request},
          {"by_event", by_event},   {"events", count},
          {"format_version", 1}};
}
J replay(const J &command) {
  std::string path = command.at("path");
  std::ifstream file(path);
  if (!file)
    throw std::invalid_argument("Cannot open journal");
  Tick until = command.value("until_ns", UINT64_MAX),
       from = command.value("from_ns", Tick(0));
  size_t limit = command.value("limit", size_t(1000));
  J events = J::array(), workers = J::object(), requests = J::object();
  std::string line;
  std::ifstream index_file(path + ".index.json");
  uint64_t offset = 0;
  J index;
  if (index_file) {
    index_file >> index;
    for (auto &snapshot : index.at("snapshots"))
      if (snapshot.at("time_ns").get<Tick>() <= until) {
        workers = snapshot.at("workers");
        requests = snapshot.at("requests");
        offset = snapshot.at("offset");
      } else
        break;
  }
  file.seekg(offset);
  size_t state_events = 0;
  while (std::getline(file, line)) {
    J event = J::parse(line);
    if (event.at("time_ns").get<Tick>() > until)
      break;
    reduce(workers, requests, event);
    ++state_events;
  }
  auto matches = [&](const J &event) {
    if (command.contains("worker") &&
        event.value("worker", -1) != command["worker"].get<int>())
      return false;
    if (command.contains("request") &&
        event.value("id", -1) != command["request"].get<int>())
      return false;
    if (command.contains("event") && event.at("event") != command["event"])
      return false;
    return true;
  };
  if (!index.is_null()) {
    const J *locations = &index.at("offsets");
    for (auto pair :
         {std::pair<const char *, const char *>{"request", "by_request"},
          {"worker", "by_worker"},
          {"event", "by_event"}})
      if (command.contains(pair.first)) {
        std::string key = command[pair.first].is_string()
                              ? command[pair.first].get<std::string>()
                              : std::to_string(command[pair.first].get<int>());
        static const J empty = J::array();
        locations = index.at(pair.second).contains(key)
                        ? &index.at(pair.second).at(key)
                        : &empty;
        break;
      }
    auto upper = std::upper_bound(
        locations->begin(), locations->end(), until,
        [](Tick t, const J &loc) { return t < loc.at(0).get<Tick>(); });
    while (upper != locations->begin() && events.size() < limit) {
      --upper;
      Tick tick = upper->at(0);
      if (tick < from)
        break;
      file.clear();
      file.seekg(upper->at(1).get<uint64_t>());
      std::getline(file, line);
      J event = J::parse(line);
      if (matches(event))
        events.push_back(event);
    }
    std::reverse(events.begin(), events.end());
  } else {
    file.clear();
    file.seekg(0);
    while (std::getline(file, line)) {
      J event = J::parse(line);
      Tick tick = event.at("time_ns");
      if (tick > until)
        break;
      if (tick < from || !matches(event))
        continue;
      events.push_back(event);
      if (events.size() > limit)
        events.erase(events.begin());
    }
  }
  return {{"time_ns", until},
          {"workers", workers},
          {"requests", requests},
          {"events", events},
          {"event_limit", limit},
          {"mode", "inspection"},
          {"state_scan_offset", offset},
          {"state_events_scanned", state_events},
          {"indexed", !index.is_null()}};
}
J rebill(const J &command) {
  J summary = command.at("summary");
  const J &cfg = command.at("config");
  double total = 0;
  for (auto &worker : summary.at("worker_costs")) {
    int id = worker.at("worker");
    double price = cfg.at(
        "prices")[id < cfg.at("regular").get<int>() ? "regular" : "spot"];
    worker["usd"] =
        worker.value("billed_ns", worker.at("allocated_ns").get<uint64_t>()) *
        price / 3.6e12;
    total += worker["usd"].get<double>();
  }
  summary["total_usd"] = total;
  return {{"summary", summary}, {"metrics", metrics(summary, cfg)}};
}
J analytic(const J &command) {
  double q = command.at("q"), lambda = command.at("rate"),
         horizon = command.at("horizon_seconds"),
         recovery = command.value("recovery_seconds", 100.0);
  int regular = command.at("regular"), spot = command.at("spot"),
      n = regular + spot, k = command.value("failures", (n + 1) / 2);
  double remaining = std::max(0, n - k) * q, healthy = n * q,
         backlog = std::max(0.0, lambda - remaining) * recovery;
  return {
      {"baseline", "calibrated fluid capacity heuristic; not an SLO guarantee"},
      {"healthy_capacity", healthy},
      {"survivor_capacity", remaining},
      {"headroom", healthy - lambda},
      {"failure_headroom", remaining - lambda},
      {"backlog_requests", backlog},
      {"drain_seconds",
       healthy > lambda ? J(backlog / (healthy - lambda)) : J(nullptr)},
      {"allocated_cost_usd",
       horizon *
           (regular * command.value("regular_price", 3.0) +
            spot * command.value("spot_price", 1.2)) /
           3600},
      {"capacity_pass", remaining >= lambda}};
}
J candidates(const J &command) {
  int max = command.value("max_replicas", 16);
  if (max < 1 || max > 16)
    throw std::invalid_argument("max_replicas must be 1–16");
  J values = J::array();
  for (int n = max; n >= 1; --n)
    for (int regular = 0; regular <= n; ++regular)
      for (auto policy : {"round_robin", "least_work"})
        values.push_back(
            {{"regular", regular}, {"spot", n - regular}, {"policy", policy}});
  return values;
}
J rank(const J &command) {
  J feasible = J::array();
  size_t pairs = 0;
  for (auto candidate : command.at("results")) {
    ++pairs;
    bool pass = true;
    double worst = 0;
    Tick latency = 0;
    if (candidate.at("scenarios").size() != command.at("suite").size())
      pass = false;
    for (const auto &name : command.at("suite")) {
      if (!candidate.at("scenarios").contains(name.get<std::string>())) {
        pass = false;
        continue;
      }
      auto m = candidate["scenarios"][name.get<std::string>()];
      pass &= m.at("feasible").get<bool>();
      if (!m.value("complete_metrics", true))
        continue;
      worst = std::max(worst, m.at("total_usd").get<double>());
      if (!m["conditional_ttft_p95_ns"].is_null())
        latency = std::max(latency, m["conditional_ttft_p95_ns"].get<Tick>());
    }
    candidate["worst_scenario_usd"] = worst;
    candidate["objective_usd"] = worst;
    candidate["worst_ttft_ns"] = latency;
    if (command.contains("scenario_weights") && pass) {
      double sum = 0, weighted = 0;
      for (auto &name : command.at("suite")) {
        std::string key = name;
        double weight = command.at("scenario_weights").at(key);
        if (weight < 0)
          throw std::invalid_argument("Scenario weights must be nonnegative");
        sum += weight;
        weighted +=
            weight * candidate["scenarios"][key].at("total_usd").get<double>();
      }
      if (std::abs(sum - 1) > 1e-9)
        throw std::invalid_argument(
            "Explicit scenario weights must sum to one");
      candidate["objective_usd"] = weighted;
    }
    if (pass)
      feasible.push_back(candidate);
  }
  std::sort(feasible.begin(), feasible.end(), [](const J &a, const J &b) {
    auto key = [](const J &x) {
      return std::make_tuple(
          x.at("objective_usd").get<double>(),
          x.at("worst_ttft_ns").get<Tick>(),
          x.at("regular").get<int>() + x.at("spot").get<int>(),
          x.at("regular").get<int>(), x.at("policy").get<std::string>());
    };
    return key(a) < key(b);
  });
  return {{"evaluated_pairs", pairs},
          {"feasible_pairs", feasible.size()},
          {"ranking", feasible},
          {"winner", feasible.empty() ? J(nullptr) : feasible[0]},
          {"objective",
           command.contains("scenario_weights")
               ? "minimize explicitly weighted scenario cost"
               : "minimize maximum selected scenario total modeled USD"}};
}
int main() {
  try {
    J command;
    std::cin >> command;
    std::string op = command.at("op");
    J result;
    if (op == "metrics")
      result = metrics(command.at("summary"), command.at("config"));
    else if (op == "replay")
      result = replay(command);
    else if (op == "index")
      result = build_index(command);
    else if (op == "rebill")
      result = rebill(command);
    else if (op == "analytic")
      result = analytic(command);
    else if (op == "candidates")
      result = candidates(command);
    else if (op == "rank")
      result = rank(command);
    else
      throw std::invalid_argument("Unknown core operation");
    std::cout << result.dump() << std::endl;
    return 0;
  } catch (const std::exception &e) {
    std::cerr << e.what() << std::endl;
    return 2;
  }
}
