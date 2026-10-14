#include "edge/dataset.hpp"
#include "common/json.hpp"
#include <cerrno>
#include <cmath>
#include <cstdlib>

namespace edgefhe::v5 {
namespace {
std::vector<std::string> split_row(const std::string& line) {
  require(line.find('"') == std::string::npos, "Quoted CSV fields are not supported");
  std::vector<std::string> fields;
  size_t begin = 0;
  for (;;) {
    const size_t end = line.find(',', begin);
    fields.push_back(line.substr(begin, end == std::string::npos ? std::string::npos : end - begin));
    if (end == std::string::npos) return fields;
    begin = end + 1;
  }
}

double parse_number(const std::string& text, size_t row, const std::string& column) {
  const std::string where = "row " + std::to_string(row) + " column " + column;
  require(!text.empty(), ("Empty value at " + where).c_str());
  // Plain decimal only, like the Python side: no hex, inf, nan, spaces or underscores.
  require(text.find_first_not_of("0123456789+-.eE") == std::string::npos, ("Invalid number at " + where).c_str());
  errno = 0;
  char* end = nullptr;
  const double value = std::strtod(text.c_str(), &end);
  require(end == text.c_str() + text.size() && errno != ERANGE && std::isfinite(value),
          ("Invalid number at " + where).c_str());
  return value;
}
}  // namespace

Dataset load_csv(const std::string& path, const Preprocess& pre, size_t limit) {
  std::string text = read_text_file(path, size_t(256) << 20);
  if (text.compare(0, 3, "\xEF\xBB\xBF") == 0) text.erase(0, 3);  // UTF-8 byte order mark (Excel exports)
  Dataset data;
  data.path = path;
  std::vector<std::string> header;
  std::array<size_t, 4> column{};
  size_t label_column = std::string::npos, row = 0, begin = 0;
  while (begin < text.size()) {
    size_t end = text.find('\n', begin);
    if (end == std::string::npos) end = text.size();
    std::string line = text.substr(begin, end - begin);
    begin = end + 1;
    while (!line.empty() && line.back() == '\r') line.pop_back();
    if (line.empty()) continue;
    const auto fields = split_row(line);
    if (header.empty()) {
      header = fields;
      for (size_t f = 0; f < 4; f++) {
        column[f] = std::string::npos;
        for (size_t c = 0; c < header.size(); c++)
          if (header[c] == pre.features[f]) {
            require(column[f] == std::string::npos, ("Repeated CSV column " + pre.features[f]).c_str());
            column[f] = c;
          }
        require(column[f] != std::string::npos, ("CSV has no column " + pre.features[f]).c_str());
      }
      for (size_t c = 0; c < header.size(); c++)
        if (header[c] == pre.label) label_column = c;
      continue;
    }
    row++;
    require(fields.size() == header.size(), ("Row " + std::to_string(row) + " has the wrong number of fields").c_str());
    std::array<double, 4> raw{};
    for (size_t f = 0; f < 4; f++) raw[f] = parse_number(fields[column[f]], row, pre.features[f]);
    data.raw.push_back(raw);
    if (label_column != std::string::npos) {
      const double label = parse_number(fields[label_column], row, pre.label);
      require(label == 0 || label == 1, ("Label must be 0 or 1 at row " + std::to_string(row)).c_str());
      data.labels.push_back(int8_t(label));
    }
    if (limit && data.raw.size() == limit) break;
  }
  require(!header.empty(), "CSV has no header row");
  require(!data.raw.empty(), "CSV has no records");
  return data;
}
}  // namespace edgefhe::v5
