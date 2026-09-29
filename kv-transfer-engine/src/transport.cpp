#include "kvtransfer/transport.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#ifdef __linux__
#include <sys/epoll.h>
#endif
#include <utility>

namespace kvtransfer {
namespace {
[[noreturn]] void fail(const char* operation) {
  throw Error(std::string(operation) + ": " + std::strerror(errno));
}
sockaddr_in address(const std::string& ip, uint16_t port) {
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  if (inet_pton(AF_INET, ip.c_str(), &addr.sin_addr) != 1)
    throw Error("expected numeric IPv4 address");
  return addr;
}
void wait_poll(int fd, short events, std::chrono::steady_clock::time_point deadline) {
  while (true) {
    auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
                         deadline - std::chrono::steady_clock::now())
                         .count();
    if (remaining <= 0) throw Error("connection deadline expired");
    pollfd p{fd, events, 0};
    int n = poll(&p, 1, int(std::min<int64_t>(remaining, 1000)));
    if (n > 0) return;
    if (n < 0 && errno != EINTR) fail("poll");
  }
}
}  // namespace
Socket::Socket(int fd) : fd_(fd) {
  if (fd < 0) return;
  int flags = fcntl(fd, F_GETFL, 0);
  if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0 ||
      fcntl(fd, F_SETFD, FD_CLOEXEC) < 0) {
    close(fd_);
    fd_ = -1;
    fail("fcntl");
  }
#ifdef SO_NOSIGPIPE
  int yes = 1;
  setsockopt(fd_, SOL_SOCKET, SO_NOSIGPIPE, &yes, sizeof(yes));
#endif
  int yes_tcp = 1;
  setsockopt(fd_, IPPROTO_TCP, TCP_NODELAY, &yes_tcp, sizeof(yes_tcp));
}
Socket::~Socket() {
  if (fd_ >= 0) close(fd_);
}
Socket::Socket(Socket&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
Socket& Socket::operator=(Socket&& other) noexcept {
  if (this != &other) {
    if (fd_ >= 0) close(fd_);
    fd_ = std::exchange(other.fd_, -1);
  }
  return *this;
}
size_t Socket::read(std::span<std::byte> into) {
  if (into.empty()) return 0;
  auto n = recv(fd_, into.data(), into.size(), 0);
  if (n > 0) return size_t(n);
  if (n == 0) throw EndOfStream();
  if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) return 0;
  fail("recv");
}
size_t Socket::write(std::span<const std::byte> from) {
  if (from.empty()) return 0;
#ifdef MSG_NOSIGNAL
  constexpr int flags = MSG_NOSIGNAL;
#else
  constexpr int flags = 0;
#endif
  auto n = send(fd_, from.data(), from.size(), flags);
  if (n >= 0) return size_t(n);
  if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) return 0;
  fail("send");
}
Readiness::Readiness(int fd) : fd_(fd) {
#ifdef __linux__
  epoll_ = epoll_create1(EPOLL_CLOEXEC);
  if (epoll_ < 0) fail("epoll_create1");
  epoll_event event{};
  event.events = EPOLLIN;
  event.data.fd = fd;
  if (epoll_ctl(epoll_, EPOLL_CTL_ADD, fd, &event) < 0) {
    close(epoll_);
    fail("epoll add");
  }
#endif
}
Readiness::~Readiness() {
  if (epoll_ >= 0) close(epoll_);
}
void Readiness::wait(bool read, bool write, int ms) {
#ifdef __linux__
  epoll_event event{};
  event.events = (read ? uint32_t(EPOLLIN) : 0u) | (write ? uint32_t(EPOLLOUT) : 0u);
  event.data.fd = fd_;
  if (epoll_ctl(epoll_, EPOLL_CTL_MOD, fd_, &event) < 0) fail("epoll modify");
  if (epoll_wait(epoll_, &event, 1, ms) < 0 && errno != EINTR) fail("epoll wait");
#else
  pollfd p{fd_, short((read ? POLLIN : 0) | (write ? POLLOUT : 0)), 0};
  if (poll(&p, 1, ms) < 0 && errno != EINTR) fail("poll");
#endif
}
Socket listen_tcp(const std::string& ip, uint16_t port) {
  Socket socket(::socket(AF_INET, SOCK_STREAM, 0));
  if (socket.fd() < 0) fail("socket");
  int yes = 1;
  setsockopt(socket.fd(), SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
  auto addr = address(ip, port);
  if (bind(socket.fd(), reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0 ||
      listen(socket.fd(), 128) < 0)
    fail("listen");
  return socket;
}
uint16_t local_port(const Socket& socket) {
  sockaddr_in addr{};
  socklen_t n = sizeof(addr);
  if (getsockname(socket.fd(), reinterpret_cast<sockaddr*>(&addr), &n) < 0) fail("getsockname");
  return ntohs(addr.sin_port);
}
Socket accept_tcp(const Socket& listener, std::chrono::milliseconds timeout) {
  auto deadline = std::chrono::steady_clock::now() + timeout;
  while (true) {
    int fd = accept(listener.fd(), nullptr, nullptr);
    if (fd >= 0) return Socket(fd);
    if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) fail("accept");
    wait_poll(listener.fd(), POLLIN, deadline);
  }
}
Socket connect_tcp(const std::string& ip, uint16_t port, std::chrono::milliseconds timeout) {
  Socket socket(::socket(AF_INET, SOCK_STREAM, 0));
  if (socket.fd() < 0) fail("socket");
  auto addr = address(ip, port);
  if (connect(socket.fd(), reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
    if (errno != EINPROGRESS) fail("connect");
    wait_poll(socket.fd(), POLLOUT, std::chrono::steady_clock::now() + timeout);
    int error = 0;
    socklen_t n = sizeof(error);
    if (getsockopt(socket.fd(), SOL_SOCKET, SO_ERROR, &error, &n) < 0) fail("getsockopt");
    if (error) {
      errno = error;
      fail("connect");
    }
  }
  return socket;
}
}  // namespace kvtransfer
