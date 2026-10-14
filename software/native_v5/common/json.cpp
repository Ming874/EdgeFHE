#include "common/json.hpp"
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <stdexcept>

namespace edgefhe::v5 {
namespace {
[[noreturn]] void fail(const std::string& message) { throw std::invalid_argument(message); }
const char* type_name(JsonValue::Type type) {
  switch (type) {
    case JsonValue::Type::null: return "null";
    case JsonValue::Type::boolean: return "boolean";
    case JsonValue::Type::number: return "number";
    case JsonValue::Type::string: return "string";
    case JsonValue::Type::array: return "array";
    case JsonValue::Type::object: return "object";
  }
  return "?";
}
void append_utf8(std::string& out, uint32_t code) {
  if (code < 0x80) {
    out += char(code);
  } else if (code < 0x800) {
    out += char(0xC0 | (code >> 6));
    out += char(0x80 | (code & 0x3F));
  } else if (code < 0x10000) {
    out += char(0xE0 | (code >> 12));
    out += char(0x80 | ((code >> 6) & 0x3F));
    out += char(0x80 | (code & 0x3F));
  } else {
    out += char(0xF0 | (code >> 18));
    out += char(0x80 | ((code >> 12) & 0x3F));
    out += char(0x80 | ((code >> 6) & 0x3F));
    out += char(0x80 | (code & 0x3F));
  }
}
}  // namespace

class JsonParser {
 public:
  explicit JsonParser(const std::string& text) : text_(text) {}

  JsonValue document() {
    JsonValue value = parse(0);
    skip_space();
    if (pos_ != text_.size()) error("trailing data");
    return value;
  }

 private:
  static constexpr int max_depth = 64;

  [[noreturn]] void error(const std::string& what) const {
    fail("Invalid JSON at byte " + std::to_string(pos_) + ": " + what);
  }
  void skip_space() {
    while (pos_ < text_.size() &&
           (text_[pos_] == ' ' || text_[pos_] == '\t' || text_[pos_] == '\n' || text_[pos_] == '\r'))
      pos_++;
  }
  bool consume(char c) {
    skip_space();
    if (pos_ < text_.size() && text_[pos_] == c) {
      pos_++;
      return true;
    }
    return false;
  }
  void expect_word(const char* word) {
    for (const char* p = word; *p; p++, pos_++)
      if (pos_ >= text_.size() || text_[pos_] != *p) error(std::string("expected ") + word);
  }

  JsonValue parse(int depth) {
    if (depth > max_depth) error("nesting too deep");
    skip_space();
    if (pos_ >= text_.size()) error("unexpected end");
    JsonValue value;
    const char c = text_[pos_];
    if (c == '{') {
      pos_++;
      value.type_ = JsonValue::Type::object;
      if (consume('}')) return value;
      do {
        skip_space();
        if (pos_ >= text_.size() || text_[pos_] != '"') error("expected object key");
        std::string key = parse_string();
        for (const auto& existing : value.keys_)
          if (existing == key) error("duplicate key '" + key + "'");
        if (!consume(':')) error("expected ':'");
        value.keys_.push_back(std::move(key));
        value.items_.push_back(parse(depth + 1));
      } while (consume(','));
      if (!consume('}')) error("expected ',' or '}'");
    } else if (c == '[') {
      pos_++;
      value.type_ = JsonValue::Type::array;
      if (consume(']')) return value;
      do value.items_.push_back(parse(depth + 1));
      while (consume(','));
      if (!consume(']')) error("expected ',' or ']'");
    } else if (c == '"') {
      value.type_ = JsonValue::Type::string;
      value.string_ = parse_string();
    } else if (c == 't') {
      expect_word("true");
      value.type_ = JsonValue::Type::boolean;
      value.boolean_ = true;
    } else if (c == 'f') {
      expect_word("false");
      value.type_ = JsonValue::Type::boolean;
    } else if (c == 'n') {
      expect_word("null");
    } else {
      value.type_ = JsonValue::Type::number;
      value.number_ = parse_number();
    }
    return value;
  }

  uint32_t hex4() {
    if (pos_ + 4 > text_.size()) error("short \\u escape");
    uint32_t code = 0;
    for (int i = 0; i < 4; i++) {
      const char h = text_[pos_++];
      code <<= 4;
      if (h >= '0' && h <= '9') code |= uint32_t(h - '0');
      else if (h >= 'a' && h <= 'f') code |= uint32_t(h - 'a' + 10);
      else if (h >= 'A' && h <= 'F') code |= uint32_t(h - 'A' + 10);
      else error("bad \\u escape");
    }
    return code;
  }

  std::string parse_string() {
    pos_++;  // opening quote
    std::string out;
    for (;;) {
      if (pos_ >= text_.size()) error("unterminated string");
      const unsigned char c = static_cast<unsigned char>(text_[pos_++]);
      if (c == '"') return out;
      if (c < 0x20) error("control character in string");
      if (c != '\\') {
        out += char(c);
        continue;
      }
      if (pos_ >= text_.size()) error("unterminated escape");
      const char e = text_[pos_++];
      switch (e) {
        case '"': out += '"'; break;
        case '\\': out += '\\'; break;
        case '/': out += '/'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case 'n': out += '\n'; break;
        case 'r': out += '\r'; break;
        case 't': out += '\t'; break;
        case 'u': {
          uint32_t code = hex4();
          if (code >= 0xD800 && code <= 0xDBFF) {
            if (pos_ + 2 > text_.size() || text_[pos_] != '\\' || text_[pos_ + 1] != 'u') error("lone surrogate");
            pos_ += 2;
            const uint32_t low = hex4();
            if (low < 0xDC00 || low > 0xDFFF) error("bad surrogate pair");
            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
          } else if (code >= 0xDC00 && code <= 0xDFFF) {
            error("lone surrogate");
          }
          append_utf8(out, code);
          break;
        }
        default: error("bad escape");
      }
    }
  }

  // RFC 8259 number grammar, then strtod on exactly that span.
  double parse_number() {
    const size_t begin = pos_;
    const auto digits = [&] {
      const size_t start = pos_;
      while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') pos_++;
      return pos_ - start;
    };
    if (pos_ < text_.size() && text_[pos_] == '-') pos_++;
    if (pos_ < text_.size() && text_[pos_] == '0') pos_++;
    else if (digits() == 0) error("expected a value");
    if (pos_ < text_.size() && text_[pos_] == '.') {
      pos_++;
      if (digits() == 0) error("digits required after '.'");
    }
    if (pos_ < text_.size() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
      pos_++;
      if (pos_ < text_.size() && (text_[pos_] == '+' || text_[pos_] == '-')) pos_++;
      if (digits() == 0) error("digits required in exponent");
    }
    const std::string span = text_.substr(begin, pos_ - begin);
    errno = 0;
    char* end = nullptr;
    const double value = std::strtod(span.c_str(), &end);
    if (end != span.c_str() + span.size()) error("bad number");
    // ERANGE with a finite result is underflow toward zero, which JSON allows.
    if (!std::isfinite(value)) error("number out of range");
    return value;
  }

  const std::string& text_;
  size_t pos_ = 0;
};

double JsonValue::number(const std::string& what) const {
  if (type_ != Type::number) fail(what + " must be a number, got " + type_name(type_));
  return number_;
}
bool JsonValue::boolean(const std::string& what) const {
  if (type_ != Type::boolean) fail(what + " must be true/false, got " + type_name(type_));
  return boolean_;
}
const std::string& JsonValue::string(const std::string& what) const {
  if (type_ != Type::string) fail(what + " must be a string, got " + type_name(type_));
  return string_;
}
const std::vector<JsonValue>& JsonValue::array(const std::string& what) const {
  if (type_ != Type::array) fail(what + " must be an array, got " + type_name(type_));
  return items_;
}
const std::vector<std::string>& JsonValue::keys(const std::string& what) const {
  if (type_ != Type::object) fail(what + " must be an object, got " + type_name(type_));
  return keys_;
}
const JsonValue* JsonValue::find(const std::string& key) const {
  if (type_ != Type::object) return nullptr;
  for (size_t i = 0; i < keys_.size(); i++)
    if (keys_[i] == key) return &items_[i];
  return nullptr;
}
const JsonValue& JsonValue::at(const std::string& key) const {
  const JsonValue* value = find(key);
  if (!value) fail("Missing JSON field '" + key + "'");
  return *value;
}

JsonValue parse_json(const std::string& text) { return JsonParser(text).document(); }

std::string read_text_file(const std::string& path, size_t max_bytes) {
  std::ifstream file(path, std::ios::binary);
  if (!file) fail("Cannot read " + path);
  std::string text;
  char buffer[65536];
  while (file.read(buffer, sizeof(buffer)) || file.gcount() > 0) {
    text.append(buffer, size_t(file.gcount()));
    if (text.size() > max_bytes) fail(path + " is larger than " + std::to_string(max_bytes) + " bytes");
  }
  if (file.bad()) fail("Read error on " + path);
  return text;
}

JsonValue read_json_file(const std::string& path, size_t max_bytes) {
  const std::string text = read_text_file(path, max_bytes);
  try {
    return parse_json(text);
  } catch (const std::invalid_argument& e) {
    fail(path + ": " + e.what());
  }
}

std::string json_quote(const std::string& text) {
  std::string out = "\"";
  for (const unsigned char c : text) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          char escaped[8];
          std::snprintf(escaped, sizeof(escaped), "\\u%04x", unsigned(c));
          out += escaped;
        } else {
          out += char(c);
        }
    }
  }
  return out + "\"";
}

std::string format_double(double value) {
  char buffer[40];
  std::snprintf(buffer, sizeof(buffer), "%.15g", value);
  if (std::strtod(buffer, nullptr) != value) std::snprintf(buffer, sizeof(buffer), "%.17g", value);
  return buffer;
}

void JsonWriter::raw(const std::string& text) { out_ += text; }

void JsonWriter::before_value() {
  if (done_) fail("JsonWriter: document already complete");
  if (stack_.empty()) return;
  Level& level = stack_.back();
  if (level.object) {
    if (!level.key_pending) fail("JsonWriter: value without key inside object");
    level.key_pending = false;
    return;
  }
  raw(level.count ? ",\n" : "\n");
  raw(std::string(2 * stack_.size(), ' '));
  level.count++;
}

JsonWriter& JsonWriter::key(const std::string& name) {
  if (stack_.empty() || !stack_.back().object || stack_.back().key_pending) fail("JsonWriter: misplaced key");
  Level& level = stack_.back();
  raw(level.count ? ",\n" : "\n");
  raw(std::string(2 * stack_.size(), ' '));
  raw(json_quote(name) + ": ");
  level.count++;
  level.key_pending = true;
  return *this;
}

JsonWriter& JsonWriter::begin_object() {
  before_value();
  raw("{");
  stack_.push_back({true, 0, false});
  return *this;
}

JsonWriter& JsonWriter::begin_array() {
  before_value();
  raw("[");
  stack_.push_back({false, 0, false});
  return *this;
}

JsonWriter& JsonWriter::end_object() {
  if (stack_.empty() || !stack_.back().object || stack_.back().key_pending) fail("JsonWriter: misplaced end_object");
  const bool empty = stack_.back().count == 0;
  stack_.pop_back();
  if (!empty) raw("\n" + std::string(2 * stack_.size(), ' '));
  raw("}");
  if (stack_.empty()) done_ = true;
  return *this;
}

JsonWriter& JsonWriter::end_array() {
  if (stack_.empty() || stack_.back().object) fail("JsonWriter: misplaced end_array");
  const bool empty = stack_.back().count == 0;
  stack_.pop_back();
  if (!empty) raw("\n" + std::string(2 * stack_.size(), ' '));
  raw("]");
  if (stack_.empty()) done_ = true;
  return *this;
}

JsonWriter& JsonWriter::value(const std::string& text) {
  before_value();
  raw(json_quote(text));
  if (stack_.empty()) done_ = true;
  return *this;
}

JsonWriter& JsonWriter::value(double number) {
  before_value();
  raw(std::isfinite(number) ? format_double(number) : "null");
  if (stack_.empty()) done_ = true;
  return *this;
}

JsonWriter& JsonWriter::value(int64_t number) {
  before_value();
  raw(std::to_string(number));
  if (stack_.empty()) done_ = true;
  return *this;
}

JsonWriter& JsonWriter::value(uint64_t number) {
  before_value();
  raw(std::to_string(number));
  if (stack_.empty()) done_ = true;
  return *this;
}

JsonWriter& JsonWriter::value(bool flag) {
  before_value();
  raw(flag ? "true" : "false");
  if (stack_.empty()) done_ = true;
  return *this;
}

JsonWriter& JsonWriter::null() {
  before_value();
  raw("null");
  if (stack_.empty()) done_ = true;
  return *this;
}

std::string JsonWriter::str() const {
  if (!stack_.empty() || !done_) fail("JsonWriter: document is not complete");
  return out_ + "\n";
}
}  // namespace edgefhe::v5
