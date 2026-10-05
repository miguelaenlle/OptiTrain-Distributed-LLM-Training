# Source attribution

Fleet Lab extends KAIST's LLMServingSim (MIT) and its pinned ASTRA-Sim,
Chakra, analytical network/memory dependencies. Upstream licenses remain in
their checkouts; authored integration changes are supplied as auditable patches.
Upstream scheduler, cache, profile lookup, graph generation and analytical
execution are reused, not counted as authored Fleet Lab code.

RTXPRO6000 timings are kernel measurements for NVIDIA RTX PRO 6000 Blackwell
Server Edition; they are not a measured AWS fleet or empirical failure model.
See `upstream-lock.json` and each run manifest for exact provenance.
