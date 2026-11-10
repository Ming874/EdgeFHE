#pragma once
#include "common.hpp"
struct Model {
  const char* id;const char* source_sha256;std::vector<std::string> features;
  std::vector<double> mean,sd,weights;std::vector<int64_t> weights_int;
  double bias;int64_t bias_int;int uci,dictionary;
};
#include "models/model_catalog.hpp"
namespace efnet {
inline const std::vector<Model>& catalogue(){static const auto c=model_catalog();return c;}
inline const Model& model_name(const std::string& id){for(const auto& m:catalogue())if(id==m.id)return m;throw std::runtime_error("MODEL_UNKNOWN");}
inline const Model& model_digest(const Digest& id){for(const auto& m:catalogue())if(equal(id,unhex(m.source_sha256)))return m;throw std::runtime_error("MODEL_SOURCE_ID_UNKNOWN");}
}
