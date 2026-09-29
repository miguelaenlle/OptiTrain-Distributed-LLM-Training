#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "kvtransfer/engine.h"
#ifdef KVT_CUDA
#include "kvtransfer/cuda_staging.h"
#endif
namespace py = pybind11;
using namespace kvtransfer;
using namespace std::chrono_literals;
namespace {
std::shared_ptr<TransferEngine> wrap_engine(Socket socket, Config config) {
  return std::shared_ptr<TransferEngine>(new TransferEngine(std::move(socket), std::move(config)),
                                         [](TransferEngine* p) {
                                           // Native teardown joins a staging thread which may
                                           // release a Python-owned tensor. Do not hold the GIL
                                           // while waiting for that thread.
                                           if (PyGILState_Check()) {
                                             py::gil_scoped_release release;
                                             delete p;
                                           } else
                                             delete p;
                                         });
}
}  // namespace
PYBIND11_MODULE(_native, m) {
  py::register_exception<Error>(m, "TransferError");
  py::class_<TransferId>(m, "TransferId")
      .def(py::init<uint64_t, uint64_t>())
      .def_readonly("incarnation", &TransferId::incarnation)
      .def_readonly("sequence", &TransferId::sequence);
  py::class_<Config>(m, "Config")
      .def(py::init([](std::string model, uint32_t layers, uint32_t heads, uint32_t dim,
                       uint32_t block, std::string dtype) {
        Config c;
        auto& l = c.layout;
        l.set_model_revision(model);
        l.set_layers(layers);
        l.set_kv_heads(heads);
        l.set_head_dim(dim);
        l.set_block_tokens(block);
        l.set_dtype(dtype);
        l.set_element_bytes(2);
        l.set_ordering("block-kv-token-head-dim");
        validate_layout(l);
        return c;
      }))
      .def_readwrite("chunk_bytes", &Config::chunk_bytes)
      .def_readwrite("queue_frames", &Config::queue_frames)
      .def_readwrite("max_transfers", &Config::max_transfers)
      .def_property(
          "timeout_ms", [](const Config& c) { return c.timeout.count(); },
          [](Config& c, int64_t n) { c.timeout = std::chrono::milliseconds(n); });
  py::class_<LayerStorage, std::shared_ptr<LayerStorage>>(m, "LayerStorage");
  py::class_<HostLayer, LayerStorage, std::shared_ptr<HostLayer>>(m, "HostLayer")
      .def(py::init<size_t, size_t>())
      .def("snapshot",
           [](HostLayer& layer) {
             auto b = layer.snapshot();
             return py::bytes(reinterpret_cast<const char*>(b.data()), b.size());
           })
      .def("assign", [](HostLayer& layer, py::bytes data) {
        std::string s = data;
        layer.assign(std::as_bytes(std::span(s)));
      });
  py::class_<Completion>(m, "Completion")
      .def_readonly("id", &Completion::id)
      .def_readonly("sending", &Completion::sending)
      .def_readonly("success", &Completion::success)
      .def_readonly("error", &Completion::error);
  py::class_<Metrics>(m, "Metrics")
      .def_readonly("sent_bytes", &Metrics::sent_bytes)
      .def_readonly("received_bytes", &Metrics::received_bytes)
      .def_readonly("active", &Metrics::active)
      .def_readonly("completed", &Metrics::completed)
      .def_readonly("failed", &Metrics::failed);
  py::class_<Socket>(m, "Listener")
      .def(py::init([](std::string ip, uint16_t port) { return listen_tcp(ip, port); }))
      .def_property_readonly("port", &local_port)
      .def(
          "accept",
          [](Socket& listener, Config c) {
            return wrap_engine(accept_tcp(listener, c.timeout), c);
          },
          py::call_guard<py::gil_scoped_release>());
  py::class_<TransferEngine, std::shared_ptr<TransferEngine>>(m, "Engine")
      .def_static(
          "connect",
          [](std::string ip, uint16_t port, Config c) {
            return wrap_engine(connect_tcp(ip, port, c.timeout), c);
          },
          py::call_guard<py::gil_scoped_release>())
      .def_static(
          "accept",
          [](std::string ip, uint16_t port, Config c) {
            auto listener = listen_tcp(ip, port);
            return wrap_engine(accept_tcp(listener, c.timeout), c);
          },
          py::call_guard<py::gil_scoped_release>())
      .def("begin_send", &TransferEngine::begin_send, py::call_guard<py::gil_scoped_release>())
      .def("begin_recv", &TransferEngine::begin_recv, py::call_guard<py::gil_scoped_release>())
      .def("send_layer", &TransferEngine::send_layer, py::call_guard<py::gil_scoped_release>())
      .def(
          "wait_layer",
          [](TransferEngine& e, TransferId id, uint32_t layer, int timeout) {
            e.wait_layer(id, layer, std::chrono::milliseconds(timeout));
          },
          py::call_guard<py::gil_scoped_release>())
      .def(
          "wait_all",
          [](TransferEngine& e, int timeout) { e.wait_all(std::chrono::milliseconds(timeout)); },
          py::call_guard<py::gil_scoped_release>())
      .def(
          "wait_staged",
          [](TransferEngine& e, TransferId id, int timeout) {
            e.wait_staged(id, std::chrono::milliseconds(timeout));
          },
          py::call_guard<py::gil_scoped_release>())
      .def("poll_finished", &TransferEngine::poll_finished,
           py::call_guard<py::gil_scoped_release>())
      .def("cancel", &TransferEngine::cancel, py::call_guard<py::gil_scoped_release>())
      .def("metrics", &TransferEngine::metrics, py::call_guard<py::gil_scoped_release>());
#ifdef KVT_CUDA
  m.attr("cuda_enabled") = true;
  py::class_<CudaStaging, std::shared_ptr<CudaStaging>>(m, "CudaStaging")
      .def(py::init<int, size_t>(), py::arg("device"), py::arg("capacity") = kMaxPayload);
  m.def("cuda_layer", [](std::shared_ptr<CudaStaging> context, py::object tensor,
                         uintptr_t stream) {
    // Check the actual tensor, not user-supplied byte lengths or pointers.
    auto shape = tensor.attr("shape").cast<std::vector<size_t>>();
    if (!tensor.attr("is_cuda").cast<bool>() || !tensor.attr("is_contiguous")().cast<bool>() ||
        shape.size() != 5 || shape[0] != 2 || !shape[1] ||
        tensor.attr("element_size")().cast<size_t>() != 2)
      throw Error("expected contiguous CUDA tensor [2, blocks, tokens, heads, dim], 16-bit dtype");
    size_t bytes = tensor.attr("numel")().cast<size_t>() * 2 / shape[1];
    auto owner = std::shared_ptr<void>(new py::object(tensor), [](void* p) {
      py::gil_scoped_acquire gil;
      delete static_cast<py::object*>(p);
    });
    return std::make_shared<CudaLayer>(
        context, reinterpret_cast<void*>(tensor.attr("data_ptr")().cast<uintptr_t>()), shape[1],
        bytes, reinterpret_cast<cudaStream_t>(stream), std::move(owner));
  });
#else
  m.attr("cuda_enabled") = false;
#endif
}
