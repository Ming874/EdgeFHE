// Input records for the edge: the model's four raw features (in model order) and the
// label when the CSV has one, for example data/cdc_diabetes/splits/test.csv. Both groups
// read the same file, so "same data" is checked by the file digest in every result.
#pragma once
#include "ckks/model.hpp"
#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace edgefhe::v5 {
struct Dataset {
  std::string path;
  std::vector<std::array<double, 4>> raw;
  std::vector<int8_t> labels;  // 0 / 1; empty when the CSV has no label column
};

// Plain comma-separated values with a header row; quoted fields are rejected. Columns
// are found by the names in `pre`. limit = 0 reads every record.
Dataset load_csv(const std::string& path, const Preprocess& pre, size_t limit = 0);
}  // namespace edgefhe::v5
