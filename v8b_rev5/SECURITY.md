# Security policy

EdgeFHE is a research and educational prototype. It has not had a production cryptographic audit, a side-channel
assessment or any certification. Do not use it to protect real personal, medical, financial or industrial data.

Known scope limits:
- FHE protects inputs and results, not the model; the key holder can learn about the model by querying it.
- The PL design wipes key material (rev5 H2/H3/H4) but is not evaluated against power or timing side channels.
- Example certificates under `public/` folders are public certificates of a local test PKI only. No private keys are
  included in this repository; generate your own PKI before any use.

Reporting: please use GitHub private vulnerability reporting (Security tab > Report a vulnerability) instead of a
public issue.
