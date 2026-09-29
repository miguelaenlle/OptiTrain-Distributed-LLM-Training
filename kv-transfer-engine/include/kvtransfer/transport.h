#pragma once
#include <chrono>
#include <string>

#include "kvtransfer/protocol.h"

namespace kvtransfer {
struct EndOfStream : Error {
  EndOfStream() : Error("peer disconnected") {}
};
class Socket {
 public:
  explicit Socket(int fd = -1);
  ~Socket();
  Socket(Socket&& other) noexcept;
  Socket& operator=(Socket&& other) noexcept;
  Socket(const Socket&) = delete;
  Socket& operator=(const Socket&) = delete;
  int fd() const { return fd_; }
  size_t read(std::span<std::byte> into);  // EAGAIN => 0, EOF => Error
  size_t write(std::span<const std::byte> from);

 private:
  int fd_;
};
class Readiness {
 public:
  explicit Readiness(int fd);
  ~Readiness();
  Readiness(const Readiness&) = delete;
  Readiness& operator=(const Readiness&) = delete;
  void wait(bool read, bool write, int milliseconds);

 private:
  int fd_, epoll_ = -1;
};
Socket listen_tcp(const std::string& ipv4, uint16_t port);
uint16_t local_port(const Socket&);
Socket accept_tcp(const Socket&, std::chrono::milliseconds timeout);
Socket connect_tcp(const std::string& ipv4, uint16_t port, std::chrono::milliseconds timeout);
}  // namespace kvtransfer
