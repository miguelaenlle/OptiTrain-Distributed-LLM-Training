#include "backend.hh"
#include "astra-sim/system/BasicEventHandlerData.hh"
#include "astra-sim/system/WorkloadLayerHandlerData.hh"
#include "astra-sim/workload/Workload.hh"
#include "controller.hh"
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <functional>
#include <iostream>
#include <json/json.hpp>
#include <set>
#include <stdexcept>

namespace fleetlab {
using json = nlohmann::json;
using namespace AstraSim;
using namespace NetworkAnalytical;

struct Scheduled {
  std::function<void()> action;
  std::set<Scheduled *> *registry;
  std::exception_ptr *error;
};

static void invoke_scheduled(void *arg) noexcept {
  std::unique_ptr<Scheduled> owned(static_cast<Scheduled *>(arg));
  owned->registry->erase(owned.get());
  try {
    owned->action();
  } catch (...) {
    // ASTRA callbacks are noexcept. Surface errors after the queue tick,
    // rather than terminating or swallowing an invariant failure.
    *owned->error = std::current_exception();
  }
}

// Independent TP1 replicas have no network collectives. Cancelling a Sys
// removes its graph callbacks before they can execute and releases its hardware
// context. PER_NPU remote memory has no shared transaction reservation to
// retain.
static size_t cancel_worker(EventQueue &queue, Sys *sys) {
  queue.cancel_if([&](Callback callback, CallbackArg arg) {
    if (callback != &Sys::handleEvent || !arg)
      return false;
    auto *data = static_cast<BasicEventHandlerData *>(arg);
    if (data->sys_id != sys->id)
      return false;
    if (data->event != EventType::CallEvents)
      throw std::runtime_error(
          "Unsupported collective callback in TP1 cancellation");
    delete data;
    return true;
  });
  std::set<WorkloadLayerHandlerData *> owned;
  size_t removed = 0;
  for (auto &[tick, callbacks] : sys->event_queue) {
    for (auto &[callable, event, data] : callbacks) {
      if (callable != sys->workload)
        throw std::runtime_error(
            "Cancellation requires independent PER_NPU memory");
      if (data)
        owned.insert(static_cast<WorkloadLayerHandlerData *>(data));
      ++removed;
    }
  }
  sys->event_queue.clear();
  sys->pending_events = 0;
  for (auto *data : owned)
    delete data;
  auto *work = sys->workload;
  work->completion_hook = {};
  work->is_finished = true;
  work->is_sleep = false;
  work->pending_workloads = {};
  delete work->et_feeder;
  work->et_feeder = nullptr;
  delete work->hw_resource;
  work->hw_resource = new HardwareResource(1);
  return removed;
}

int run_backend(
    std::shared_ptr<EventQueue> queue, const std::vector<Sys *> &systems,
    const std::vector<std::unique_ptr<Analytical::AnalyticalMemory>> &memory) {
  FILE *protocol = stdout;
  if (const char *fd = std::getenv("OPTITRAIN_FLEET_FD"))
    protocol = fdopen(std::stoi(fd), "w");
  if (!protocol)
    throw std::runtime_error("Cannot open protocol descriptor");
  std::unique_ptr<Controller> controller;
  bool finished = false;
  std::function<void(int, json)> start_graph;
  std::function<void(json)> emit = [&](json value) {
    value["protocol"] = 1;
    value["time_ns"] = queue->get_current_time();
    auto line = value.dump();
    std::fprintf(protocol, "FLEETLAB %s\n", line.c_str());
    std::fflush(protocol);
    const auto event = value.at("event").get<std::string>();
    if (event == "finished")
      finished = true;
    if (event != "admit" && event != "reset" && event != "schedule" &&
        event != "abort" && event != "frontend_complete")
      return;
    std::string response;
    if (!std::getline(std::cin, response))
      throw std::runtime_error("Frontend disconnected");
    auto reply = json::parse(response);
    if (reply.at("op") != "response")
      throw std::invalid_argument("Expected frontend response");
    if (reply.contains("error"))
      throw std::runtime_error(reply.at("error").get<std::string>());
    auto result = reply.at("value");
    int worker = value.at("worker");
    controller->stats(worker, result);
    if (event == "schedule") {
      bool active = !result.at("batch").is_null();
      controller->scheduled(worker, active, result);
      if (active) {
        emit({{"event", "batch_started"},
              {"worker", worker},
              {"batch", result.at("batch")},
              {"attempts", result.at("attempts")}});
        start_graph(worker, result);
      }
    } else if (event == "frontend_complete") {
      controller->batch_done(worker);
      controller->deliver(worker, result);
    }
  };
  std::vector<uint64_t> incarnation(systems.size(), 0);
  for (auto *sys : systems)
    sys->workload->is_finished = true;
  std::set<Scheduled *> callbacks;
  std::exception_ptr callback_error;
  auto at = [&](uint64_t tick, int phase, std::function<void()> action) {
    if (tick < queue->get_current_time())
      throw std::invalid_argument("Time moves backwards");
    auto *owned = new Scheduled{std::move(action), &callbacks, &callback_error};
    callbacks.insert(owned);
    queue->schedule_event(tick, invoke_scheduled, owned, phase);
  };
  auto cleanup = [&] {
    for (auto *sys : systems)
      cancel_worker(*queue, sys);
    queue->cancel_if([&](Callback callback, CallbackArg arg) {
      if (callback != invoke_scheduled)
        return false;
      auto *owned = static_cast<Scheduled *>(arg);
      callbacks.erase(owned);
      delete owned;
      return true;
    });
    emit({{"event", "cleanup"}, {"owned_callbacks", callbacks.size()}});
  };
  start_graph = [&](int worker, json command) {
    auto *sys = systems.at(worker);
    if (!sys->workload->is_finished)
      throw std::invalid_argument("Worker busy");
    auto tag = command.at("batch");
    auto generation = incarnation.at(worker);
    sys->workload->completion_hook = [&, worker, tag, generation] {
      if (incarnation.at(worker) != generation)
        return;
      emit({{"event", "batch_completed"},
            {"worker", worker},
            {"batch", tag},
            {"incarnation", generation}});
      if (controller)
        emit({{"event", "frontend_complete"},
              {"worker", worker},
              {"batch", tag}});
    };
    sys->workload->add_workload(command.at("graph"), {});
  };
  // Low-level integration protocol used by the cancellation spike. The full
  // lifecycle reducer is layered on this same backend, never a second clock.
  emit({{"event", "ready"}, {"workers", systems.size()}});
  std::string line;
  try {
    for (const auto &level : memory)
      if (!level->supports_independent_cancellation())
        throw std::invalid_argument("Fleet Lab requires PER_NPU memory: "
                                    "shared-memory cancellation is deferred");
    while (std::getline(std::cin, line)) {
      auto command = json::parse(line);
      const auto op = command.at("op").get<std::string>();
      if (op == "exit")
        break;
      if (op == "configure") {
        if (controller)
          throw std::invalid_argument("Already configured");
        controller = std::make_unique<Controller>(
            command.at("config"), [&] { return queue->get_current_time(); }, at,
            emit,
            [&](int worker) {
              auto removed = cancel_worker(*queue, systems.at(worker));
              ++incarnation.at(worker);
              return removed;
            });
      } else if (op == "run") {
        if (!controller)
          throw std::invalid_argument("Not configured");
        while (!finished) {
          if (queue->finished())
            throw std::runtime_error("Clock lost lifecycle events");
          queue->proceed();
          if (callback_error)
            std::rethrow_exception(callback_error);
        }
      } else if (op == "start") {
        start_graph(command.at("worker"), command);
      } else if (op == "crash") {
        int worker = command.at("worker");
        systems.at(worker);
        uint64_t tick = command.at("time_ns");
        at(tick, 0, [&, worker] {
          auto removed = cancel_worker(*queue, systems.at(worker));
          ++incarnation.at(worker);
          emit({{"event", "cancelled"},
                {"worker", worker},
                {"callbacks_removed", removed},
                {"incarnation", incarnation.at(worker)}});
        });
      } else if (op == "advance") {
        uint64_t tick = command.at("time_ns");
        bool reached = false;
        at(tick, 6, [&] { reached = true; });
        while (!reached) {
          if (queue->finished())
            throw std::runtime_error("Clock lost future event");
          queue->proceed();
          if (callback_error)
            std::rethrow_exception(callback_error);
        }
      } else {
        throw std::invalid_argument("Unknown backend operation");
      }
      emit({{"event", "ack"}, {"op", op}});
    }
    cleanup();
    return 0;
  } catch (const std::exception &error) {
    emit({{"event", "error"}, {"message", error.what()}});
    cleanup();
    return 2;
  }
}
} // namespace fleetlab
