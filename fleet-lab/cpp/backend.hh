#pragma once
#include <astra-network-analytical/common/EventQueue.h>
#include <memory>
#include <memory_backend/analytical/AnalyticalMemory.hh>
#include <vector>

namespace fleetlab {
int run_backend(
    std::shared_ptr<NetworkAnalytical::EventQueue> queue,
    const std::vector<AstraSim::Sys *> &systems,
    const std::vector<std::unique_ptr<Analytical::AnalyticalMemory>> &memory);
}
