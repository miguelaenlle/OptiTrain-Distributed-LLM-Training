#include <astra-network-analytical/common/EventQueue.h>
#include <cassert>
#include <vector>

int main() {
  using namespace NetworkAnalytical;
  EventQueue clock;
  std::vector<int> order;
  struct Action {
    int phase;
    EventQueue *clock;
    std::vector<int> *order;
  };
  Action schedule{5, &clock, &order}, deadline{4, &clock, &order},
      crash{0, &clock, &order};
  Action completion{1, &clock, &order}, observed{2, &clock, &order},
      arrival{3, &clock, &order};
  auto callback = [](void *arg) {
    auto *action = static_cast<Action *>(arg);
    action->order->push_back(action->phase);
  };
  for (auto *action :
       {&schedule, &deadline, &completion, &observed, &arrival, &crash})
    clock.schedule_event(100, callback, action, action->phase);
  clock.proceed();
  assert((order == std::vector<int>{0, 1, 2, 3, 4, 5}));
  assert(clock.get_current_time() == 100);
  clock.schedule_event(120, callback, &completion);
  clock.cancel_if(
      [&](Callback, CallbackArg arg) { return arg == &completion; });
  clock.schedule_event(130, callback, &observed);
  clock.proceed(); // empty cancelled tick has no callback effects
  assert(order.size() == 6);
  clock.proceed();
  assert(order.back() == 2);
  assert(clock.finished());
}
