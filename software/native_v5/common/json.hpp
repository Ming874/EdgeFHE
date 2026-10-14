// Minimal strict JSON for the tools: read model / preprocessing files, write result
// files. Reading rejects duplicate keys, non-finite numbers, trailing data and deep
// nesting, like the Python side (protocol.load_json). Writing produces numbers that
// read back to the same double.
#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace edgefhe::v5 {
class JsonValue {
 public:
  enum class Type { null, boolean, number, string, array, object };

  Type type() const { return type_; }
  bool is_number() const { return type_ == Type::number; }
  bool is_string() const { return type_ == Type::string; }
  // Typed accessors throw std::invalid_argument naming `what` on a type mismatch.
  double number(const std::string& what) const;
  bool boolean(const std::string& what) const;
  const std::string& string(const std::string& what) const;
  const std::vector<JsonValue>& array(const std::string& what) const;
  // Objects keep their key order.
  const std::vector<std::string>& keys(const std::string& what) const;
  const JsonValue* find(const std::string& key) const;  // nullptr when absent or not an object
  const JsonValue& at(const std::string& key) const;    // throws when absent

 private:
  friend class JsonParser;
  Type type_ = Type::null;
  bool boolean_ = false;
  double number_ = 0;
  std::string string_;
  std::vector<JsonValue> items_;  // array items or object values
  std::vector<std::string> keys_; // object keys, parallel to items_
};

JsonValue parse_json(const std::string& text);
// Reads a whole file (at most max_bytes) and parses it.
JsonValue read_json_file(const std::string& path, size_t max_bytes = 1 << 20);
std::string read_text_file(const std::string& path, size_t max_bytes);

// Streaming pretty printer. Call order is checked: key() only inside objects, values
// only after key() in objects, matching begin/end.
class JsonWriter {
 public:
  JsonWriter& begin_object();
  JsonWriter& end_object();
  JsonWriter& begin_array();
  JsonWriter& end_array();
  JsonWriter& key(const std::string& name);
  JsonWriter& value(const std::string& text);
  JsonWriter& value(const char* text) { return value(std::string(text)); }
  JsonWriter& value(double number);  // non-finite numbers are written as null
  JsonWriter& value(int64_t number);
  JsonWriter& value(uint64_t number);
  JsonWriter& value(int number) { return value(int64_t(number)); }
  JsonWriter& value(unsigned number) { return value(uint64_t(number)); }
  JsonWriter& value(bool flag);
  JsonWriter& null();
  // Finished document; throws if an object or array is still open.
  std::string str() const;

 private:
  void before_value();
  void raw(const std::string& text);
  struct Level {
    bool object;
    size_t count;
    bool key_pending;
  };
  std::vector<Level> stack_;
  std::string out_;
  bool done_ = false;
};

std::string json_quote(const std::string& text);
std::string format_double(double value);  // shortest of %.15g / %.17g that round-trips
}  // namespace edgefhe::v5
