#include "kvtransfer/engine.h"

#include <gtest/gtest.h>
#include <sys/socket.h>

#include <future>
#include <thread>

using namespace kvtransfer;
using namespace std::chrono_literals;
namespace {
Config config() {
  Config c;
  auto& l = c.layout;
  l.set_model_revision("qwen-test@fixed");
  l.set_layers(3);
  l.set_kv_heads(2);
  l.set_head_dim(8);
  l.set_block_tokens(4);
  l.set_element_bytes(2);
  l.set_dtype("float16");
  l.set_ordering("block-kv-token-head-dim");
  c.chunk_bytes = 113;
  c.queue_frames = 2;
  c.timeout = 20s;
  return c;
}
std::pair<Socket, Socket> sockets() {
  auto listener = listen_tcp("127.0.0.1", 0);
  auto client = connect_tcp("127.0.0.1", local_port(listener), 1s);
  auto server = accept_tcp(listener, 1s);
  int small = 1024;
  setsockopt(client.fd(), SOL_SOCKET, SO_SNDBUF, &small, sizeof(small));
  setsockopt(server.fd(), SOL_SOCKET, SO_SNDBUF, &small, sizeof(small));
  return {std::move(client), std::move(server)};
}
}  // namespace
TEST(Engine, HundredConcurrentTransfersExactBytesAndGuards) {
  auto [a, b] = sockets();
  auto c = config();
  TransferEngine sender(std::move(a), c), receiver(std::move(b), c);
  std::vector<std::vector<std::shared_ptr<HostLayer>>> outputs;
  const size_t width = block_bytes(c.layout);
  for (uint64_t request = 1; request <= 100; ++request) {
    TransferId id{771, request};
    sender.begin_send(id, {7, 2, 5});
    std::vector<std::shared_ptr<LayerStorage>> dst;
    outputs.emplace_back();
    for (uint32_t layer = 0; layer < 3; ++layer) {
      auto view = std::make_shared<HostLayer>(10, width);
      view->assign(Bytes(10 * width, std::byte{0xee}));
      outputs.back().push_back(view);
      dst.push_back(view);
    }
    receiver.begin_recv(id, {1, 8, 4}, dst);
    for (uint32_t layer = 0; layer < 3; ++layer) {
      auto src = std::make_shared<HostLayer>(8, width);
      Bytes data(8 * width);
      for (size_t i = 0; i < data.size(); ++i)
        data[i] = std::byte((i * 7 + request + layer * 13) % 251);
      src->assign(data);
      sender.send_layer(id, layer, src);
    }
  }
  sender.wait_all(20s);
  receiver.wait_all(20s);
  for (size_t request = 1; request <= 100; ++request)
    for (size_t layer = 0; layer < 3; ++layer) {
      auto data = outputs[request - 1][layer]->snapshot();
      for (size_t block = 0; block < 10; ++block)
        for (size_t i = 0; i < width; ++i) {
          size_t source = block == 1 ? 7 : block == 8 ? 2 : 5;
          auto expected = (block == 1 || block == 8 || block == 4)
                              ? std::byte(((source * width + i) * 7 + request + layer * 13) % 251)
                              : std::byte{0xee};
          ASSERT_EQ(data[block * width + i], expected);
        }
    }
  EXPECT_EQ(sender.poll_finished().size(), 100);
  EXPECT_TRUE(sender.poll_finished().empty());
  EXPECT_EQ(receiver.poll_finished().size(), 100);
  EXPECT_EQ(sender.metrics().active, 0);
  EXPECT_LE(sender.metrics().inbound_high_water, c.queue_frames);
  EXPECT_LE(receiver.metrics().inbound_high_water, c.queue_frames);
}
TEST(Engine, LayerReadyBeforeWholeRequest) {
  auto [a, b] = sockets();
  auto c = config();
  TransferEngine tx(std::move(a), c), rx(std::move(b), c);
  auto layer = std::make_shared<HostLayer>(1, block_bytes(c.layout));
  tx.begin_send({1, 1}, {0});
  rx.begin_recv({1, 1}, {0}, {layer, layer, layer});
  tx.send_layer({1, 1}, 0, layer);
  rx.wait_layer({1, 1}, 0, 2s);
  EXPECT_TRUE(rx.poll_finished().empty());
  EXPECT_TRUE(tx.poll_finished().empty());
  tx.send_layer({1, 1}, 1, layer);
  tx.send_layer({1, 1}, 2, layer);
  tx.wait_all(2s);
}
TEST(Engine, HandshakeMismatchFailsCleanly) {
  auto [a, b] = sockets();
  auto c = config(), other = c;
  other.layout.set_dtype("bfloat16");
  TransferEngine tx(std::move(a), c), rx(std::move(b), other);
  EXPECT_THROW(tx.wait_all(1s), Error);
  EXPECT_THROW(rx.wait_all(1s), Error);
}
TEST(Engine, TimeoutAndCancellationReleaseSessions) {
  auto [a, b] = sockets();
  auto c = config();
  c.timeout = 50ms;
  TransferEngine tx(std::move(a), c), rx(std::move(b), c);
  tx.begin_send({1, 1}, {0});
  EXPECT_THROW(tx.wait_all(1s), Error);
  auto completions = tx.poll_finished();
  ASSERT_EQ(completions.size(), 1);
  EXPECT_FALSE(completions[0].success);
  EXPECT_EQ(tx.metrics().active, 0);
  auto [x, y] = sockets();
  TransferEngine cancelled(std::move(x), config());
  cancelled.begin_send({1, 1}, {0});
  cancelled.cancel({1, 1});
  auto failed = cancelled.poll_finished();
  ASSERT_EQ(failed.size(), 1);
  EXPECT_FALSE(failed[0].success);
}
TEST(Engine, PeerDisconnectAndInvalidMapping) {
  auto [a, b] = sockets();
  TransferEngine tx(std::move(a), config());
  EXPECT_THROW(tx.begin_send({1, 1}, {0, 0}), Error);
  tx.begin_send({1, 1}, {0});
  b = Socket();
  EXPECT_THROW(tx.wait_all(1s), Error);
  auto result = tx.poll_finished();
  ASSERT_EQ(result.size(), 1);
  EXPECT_FALSE(result[0].success);
}
TEST(Engine, CancellationDoesNotPublishCompletionDuringCopy) {
  struct DelayedLayer : LayerStorage {
    std::promise<void> entered, release;
    std::shared_future<void> gate = release.get_future().share();
    size_t blocks() const override { return 1; }
    size_t block_bytes() const override { return 256; }
    void gather(std::span<const uint32_t>, size_t, std::span<std::byte> out) override {
      entered.set_value();
      gate.wait();
      std::fill(out.begin(), out.end(), std::byte{1});
    }
    void scatter(std::span<const uint32_t>, size_t, std::span<const std::byte>) override {}
  };
  auto [a, b] = sockets();
  auto c = config();
  c.layout.set_layers(1);
  TransferEngine tx(std::move(a), c), rx(std::move(b), c);
  auto source = std::make_shared<DelayedLayer>();
  auto target = std::make_shared<HostLayer>(1, 256);
  tx.begin_send({1, 1}, {0});
  rx.begin_recv({1, 1}, {0}, {target});
  tx.send_layer({1, 1}, 0, source);
  auto entered = source->entered.get_future();
  if (entered.wait_for(2s) != std::future_status::ready) {
    source->release.set_value();
    FAIL() << "copy never started";
  }
  auto cancelled = std::async(std::launch::async, [&] { tx.cancel({1, 1}); });
  EXPECT_EQ(cancelled.wait_for(20ms), std::future_status::timeout);
  EXPECT_TRUE(tx.poll_finished().empty());
  source->release.set_value();
  cancelled.get();
  auto results = tx.poll_finished();
  ASSERT_EQ(results.size(), 1);
  EXPECT_FALSE(results[0].success);
}
TEST(Engine, RejectedIdDoesNotCreateOrphanSession) {
  auto [a, b] = sockets();
  TransferEngine tx(std::move(a), config()), rx(std::move(b), config());
  tx.begin_send({10, 2}, {0});
  EXPECT_THROW(tx.begin_send({10, 1}, {0}), Error);
  EXPECT_EQ(tx.metrics().active, 1);
  tx.cancel({10, 2});
}
