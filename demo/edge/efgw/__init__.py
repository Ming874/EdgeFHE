"""EdgeFHE demo gateway core (board side; Python 3.10 standard library only).

Modules:
  config   load and validate gateway.json
  model    form rules, CSV text, plaintext reference logit, stable sigmoid, risk levels and the boundary band
  permit   invocation contracts (invoke_permit.hpp, client.cpp:141-150) and the single-use PL permit file
  records  event envelope and log, public record whitelist, salted form commitment, private store, disclosure
  runner   the efnet_client process (never signalled), DMA IRQ counters, wipe, and the Gateway object that the
           command line (edgefhe_gateway.py) and the loopback HTTP API share

demo/edge/EVENTS.md is the contract of the events, records and HTTP API built on these modules.
"""
