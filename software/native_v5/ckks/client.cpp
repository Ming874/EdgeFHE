#include "ckks/client.hpp"

namespace edgefhe::v5 {
PublicKey Client::keygen(const Material& material) {
  validate_ternary(material.secret, 64);
  validate_ternary(material.error, 16);
  validate(material.a);
  const auto secret = residues(material.secret);
  Rns product;
  backend_.multiply_batch({{&material.a, &secret, &product}});
  auto b = residues(material.error);
  for (size_t l = 0; l < limbs; l++)
    for (size_t i = 0; i < degree; i++) {
      const uint32_t r = b[l][i] + primes[l] - product[l][i];
      b[l][i] = r >= primes[l] ? r - primes[l] : r;
    }
  return {std::move(b), material.a};
}

std::array<Cipher, 4> Client::encrypt(const PublicKey& key, const std::array<Message, 4>& message,
                                      const std::array<Ternary, 4>& mask, const std::array<Ternary, 4>& e0,
                                      const std::array<Ternary, 4>& e1) {
  validate(key.b);
  validate(key.a);
  std::array<Rns, 4> m, u, r0, r1, bu, au;
  std::vector<MulJob> jobs;
  for (size_t f = 0; f < features; f++) {
    validate_ternary(mask[f], 64);
    validate_ternary(e0[f], 16);
    validate_ternary(e1[f], 16);
    m[f] = residues(message[f]);
    u[f] = residues(mask[f]);
    r0[f] = residues(e0[f]);
    r1[f] = residues(e1[f]);
    jobs.push_back({&key.b, &u[f], &bu[f]});
    jobs.push_back({&key.a, &u[f], &au[f]});
  }
  backend_.multiply_batch(jobs);
  std::array<Cipher, 4> out;
  for (size_t f = 0; f < features; f++) out[f] = {add(add(bu[f], r0[f]), m[f]), add(au[f], r1[f])};
  return out;
}

std::vector<Wide> Client::decrypt(const Cipher& cipher, const Ternary& secret) {
  validate_ternary(secret, 64);
  validate(cipher.c0);
  validate(cipher.c1);
  const auto s = residues(secret);
  Rns product;
  backend_.multiply_batch({{&cipher.c1, &s, &product}});
  return centered(add(cipher.c0, product));
}
}  // namespace edgefhe::v5
