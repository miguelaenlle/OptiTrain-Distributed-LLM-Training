#include <iostream>

#include "kvtransfer/engine.h"

using namespace kvtransfer;
using namespace std::chrono_literals;
int main(int argc, char** argv) {
  try {
    if (argc != 5) {
      std::cerr << "usage: fake_kv send|recv IPV4 PORT REQUESTS\n";
      return 2;
    }
    const bool send = std::string(argv[1]) == "send";
    if (!send && std::string(argv[1]) != "recv") throw Error("expected send or recv");
    const int port = std::stoi(argv[3]), count = std::stoi(argv[4]);
    if (port < 1 || port > 65535 || count < 1 || count > 128)
      throw Error("invalid port/request count");
    Config c;
    auto& l = c.layout;
    l.set_model_revision("synthetic-qwen2.5-0.5b-v1");
    l.set_layers(24);
    l.set_kv_heads(2);
    l.set_head_dim(64);
    l.set_block_tokens(16);
    l.set_element_bytes(2);
    l.set_dtype("float16");
    l.set_ordering("block-kv-token-head-dim");
    c.timeout = 120s;
    Socket socket;
    if (send)
      socket = connect_tcp(argv[2], uint16_t(port), 30s);
    else {
      auto listener = listen_tcp(argv[2], uint16_t(port));
      std::cerr << "listening\n";
      socket = accept_tcp(listener, 30s);
    }
    TransferEngine engine(std::move(socket), c);
    std::vector<std::vector<std::shared_ptr<HostLayer>>> outputs;
    auto start = std::chrono::steady_clock::now();
    for (int request = 1; request <= count; ++request) {
      TransferId id{4321, uint64_t(request)};
      if (send) engine.begin_send(id, {2, 0});
      std::vector<std::shared_ptr<LayerStorage>> views;
      outputs.emplace_back();
      for (uint32_t layer = 0; layer < l.layers(); ++layer) {
        auto view = std::make_shared<HostLayer>(3, block_bytes(l));
        Bytes data(3 * block_bytes(l), std::byte{0xef});
        if (send)
          for (size_t i = 0; i < data.size(); ++i) data[i] = std::byte((i + request + layer) % 251);
        view->assign(data);
        views.push_back(view);
        if (send)
          engine.send_layer(id, layer, view);
        else
          outputs.back().push_back(view);
      }
      if (!send) engine.begin_recv(id, {0, 2}, std::move(views));
    }
    engine.wait_all(120s);
    if (!send)
      for (int request = 1; request <= count; ++request)
        for (uint32_t layer = 0; layer < l.layers(); ++layer) {
          auto data = outputs[request - 1][layer]->snapshot();
          const size_t b = block_bytes(l);
          for (size_t i = 0; i < data.size(); ++i) {
            auto expected =
                i / b == 1
                    ? std::byte{0xef}
                    : std::byte(((i / b == 0 ? i + 2 * b : i - 2 * b) + request + layer) % 251);
            if (data[i] != expected) throw Error("destination byte mismatch");
          }
        }
    auto stats = engine.metrics();
    double seconds =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    std::cout << "{\"requests\":" << count << ",\"seconds\":" << seconds
              << ",\"wire_sent\":" << stats.sent_bytes
              << ",\"wire_received\":" << stats.received_bytes
              << ",\"completed\":" << stats.completed << "}\n";
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
