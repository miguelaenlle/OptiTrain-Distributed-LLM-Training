#include <gtest/gtest.h>

#include <random>

#include "kvtransfer/protocol.h"
#include "kvtransfer/session.h"

using namespace kvtransfer;
TEST(Protocol, EverySplitAndCoalescedFrames) {
  Frame frame{{Kind::Chunk, {91, 7}, 3, 123}, Bytes(1031, std::byte{0x7f})};
  auto bytes = encode(frame);
  for (size_t split = 0; split <= bytes.size(); ++split) {
    Parser parser;
    std::vector<Frame> got;
    auto collect = [&](Frame f) { got.push_back(std::move(f)); };
    parser.feed(std::span(bytes).first(split), collect);
    parser.feed(std::span(bytes).subspan(split), collect);
    ASSERT_EQ(got.size(), 1);
    EXPECT_EQ(got[0].header.id, frame.header.id);
    EXPECT_EQ(got[0].header.offset, 123);
    EXPECT_EQ(got[0].payload, frame.payload);
    EXPECT_TRUE(parser.empty());
  }
  auto empty = encode({{Kind::Ack, {91, 7}, 0, 0}, {}});
  bytes.insert(bytes.end(), empty.begin(), empty.end());
  Parser parser;
  int frames = 0;
  parser.feed(bytes, [&](Frame) { ++frames; });
  EXPECT_EQ(frames, 2);
}
TEST(Protocol, CorruptionAndOversizedPayload) {
  auto good = encode({{Kind::Chunk, {1, 1}, 0, 0}, Bytes(16)});
  for (size_t i = 0; i < good.size(); ++i) {
    auto bad = good;
    bad[i] ^= std::byte{0x80};
    Parser parser;
    EXPECT_THROW(parser.feed(bad, [](Frame) {}), Error) << i;
  }
  EXPECT_THROW(encode(Frame{{}, Bytes(kMaxPayload + 1)}), Error);
}
TEST(Protocol, KnownCrc) {
  std::string input = "123456789";
  EXPECT_EQ(crc32(std::as_bytes(std::span(input))), 0xcbf43926u);
}
TEST(Staging, ShuffledBlocksPartialCopiesAndGuards) {
  HostLayer source(9, 32), dest(11, 32);
  Bytes data(9 * 32);
  for (size_t i = 0; i < data.size(); ++i) data[i] = std::byte(i);
  source.assign(data);
  dest.assign(Bytes(11 * 32, std::byte{0xff}));
  std::vector<uint32_t> src{8, 1, 5}, dst{2, 9, 4};
  for (size_t offset = 0; offset < 96;) {
    Bytes chunk(std::min<size_t>(17, 96 - offset));
    source.gather(src, offset, chunk);
    dest.scatter(dst, offset, chunk);
    offset += chunk.size();
  }
  auto result = dest.snapshot();
  for (size_t i = 0; i < dst.size(); ++i)
    for (size_t b = 0; b < 32; ++b) EXPECT_EQ(result[dst[i] * 32 + b], data[src[i] * 32 + b]);
  for (size_t block = 0; block < 11; ++block)
    if (std::find(dst.begin(), dst.end(), block) == dst.end())
      for (size_t b = 0; b < 32; ++b) EXPECT_EQ(result[block * 32 + b], std::byte{0xff});
  Bytes one(1);
  EXPECT_THROW(source.gather(std::vector<uint32_t>{9}, 0, one), Error);
  EXPECT_THROW(source.gather(std::vector<uint32_t>{1, 1}, 0, one), Error);
  EXPECT_THROW(source.gather(src, 96, one), Error);
}
TEST(Session, RejectsMissingDuplicateAndOutOfOrderChunks) {
  auto layer = std::make_shared<HostLayer>(4, 16);
  ReceiveSession s({3, 1}, {layer});
  EXPECT_THROW(s.request_done(), Error);
  EXPECT_THROW(s.layer_done(0), Error);
  EXPECT_THROW(s.chunk(1, 0, Bytes(16)), Error);
  EXPECT_THROW(s.chunk(0, 1, Bytes(16)), Error);
  s.chunk(0, 0, Bytes(16, std::byte{1}));
  EXPECT_THROW(s.chunk(0, 0, Bytes(16)), Error);
  EXPECT_THROW(s.layer_done(0), Error);
  s.chunk(0, 16, Bytes(16, std::byte{2}));
  s.layer_done(0);
  EXPECT_TRUE(s.ready(0));
  EXPECT_THROW(s.layer_done(0), Error);
  s.request_done();
  EXPECT_EQ(s.state(), State::Complete);
  EXPECT_THROW(s.request_done(), Error);
}
TEST(Layout, ValidatesDimensionsAndIdentity) {
  wire::Layout a;
  a.set_model_revision("qwen@test");
  a.set_layers(2);
  a.set_kv_heads(2);
  a.set_head_dim(64);
  a.set_block_tokens(16);
  a.set_element_bytes(2);
  a.set_dtype("float16");
  a.set_ordering("block-kv-token-head-dim");
  EXPECT_NO_THROW(validate_layout(a));
  EXPECT_EQ(block_bytes(a), 8192);
  auto b = a;
  b.set_model_revision("qwen@other");
  EXPECT_FALSE(same_layout(a, b));
  b = a;
  b.set_head_dim(0);
  EXPECT_THROW(validate_layout(b), Error);
}
