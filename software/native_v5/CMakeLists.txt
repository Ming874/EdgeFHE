cmake_minimum_required(VERSION 3.20)
project(EdgeFHE_native_v5 LANGUAGES CXX)
if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "V5 uses Linux ioctl APIs; build on Linux or WSL (x86_64 or aarch64)")
endif()
find_package(Threads REQUIRED)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(EDGEFHE_V4_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../native_ckks)
set(EDGEFHE_V5_FLAGS -Wall -Wextra -Werror -fno-fast-math)

# The accepted V4 arithmetic (core.cpp) is compiled unchanged; V5 adds scheduling,
# backends and the shared measurement harness. Keep this list in sync with
# tools/v5_sources.py (used by the host checks and the board runner).
add_library(edgefhe_v5 STATIC
  ${EDGEFHE_V4_DIR}/core.cpp
  common/json.cpp common/platform.cpp common/sha256.cpp
  runtime/thread_pool.cpp
  backend/backend.cpp backend/factory.cpp backend/cpu/cpu_backend.cpp
  backend/fpga/device.cpp backend/fpga/fpga_backend.cpp
  ckks/client.cpp ckks/model.cpp ckks/cloud.cpp
  edge/dataset.cpp edge/qa_random.cpp edge/session.cpp edge/runner.cpp)
target_include_directories(edgefhe_v5 PUBLIC ${CMAKE_CURRENT_SOURCE_DIR} ${EDGEFHE_V4_DIR})
target_link_libraries(edgefhe_v5 PUBLIC Threads::Threads)
target_compile_options(edgefhe_v5 PRIVATE ${EDGEFHE_V5_FLAGS})
set_target_properties(edgefhe_v5 PROPERTIES POSITION_INDEPENDENT_CODE ON)

foreach(tool v5_parity v5_bench v5_e2e)
  add_executable(${tool} tools/${tool}.cpp)
endforeach()
add_executable(v5_tests tests/v5_tests.cpp)
foreach(target v5_tests v5_parity v5_bench v5_e2e)
  target_link_libraries(${target} PRIVATE edgefhe_v5)
  target_compile_options(${target} PRIVATE ${EDGEFHE_V5_FLAGS})
endforeach()

# Same C ABI as the V4 library (software/native_ckks/native_api.py), with V5 backends:
# the two-machine demo (stage 3) runs the accepted Python protocol on top of it.
add_library(edgefhe_v5_capi SHARED capi/c_api.cpp)
target_link_libraries(edgefhe_v5_capi PRIVATE edgefhe_v5)
target_compile_options(edgefhe_v5_capi PRIVATE ${EDGEFHE_V5_FLAGS})

include(CTest)
if(BUILD_TESTING)
  find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
  set(EDGEFHE_REQUIRE_PASS ${CMAKE_CURRENT_SOURCE_DIR}/../../tools/require_pass.py)
  set(EDGEFHE_SAMPLE ${CMAKE_CURRENT_SOURCE_DIR}/tests/data)
  add_test(NAME v5_unit COMMAND ${Python3_EXECUTABLE} ${EDGEFHE_REQUIRE_PASS}
    --marker "V5_TESTS_PASS tests=25" -- $<TARGET_FILE:v5_tests> ${EDGEFHE_SAMPLE})
  add_test(NAME v5_parity_simulated COMMAND ${Python3_EXECUTABLE} ${EDGEFHE_REQUIRE_PASS}
    --marker "V5_PARITY_PASS device=simulated-dma" -- $<TARGET_FILE:v5_parity> --cases 32)
  add_test(NAME v5_e2e_sample COMMAND ${Python3_EXECUTABLE} ${EDGEFHE_REQUIRE_PASS}
    --marker "V5_E2E_PASS" -- $<TARGET_FILE:v5_e2e>
    --data ${EDGEFHE_SAMPLE}/sample_records.csv --preprocess ${EDGEFHE_SAMPLE}/sample_preprocess.json
    --model ${EDGEFHE_SAMPLE}/sample_model.json --output -
    --backends cpu-w1,cpu-w3,fpga-serial,fpga-overlap --batch-records 128
    --rounds 2 --warmup 1 --repeats 1 --capacity 0,2 --qa-seed 7 --power off)
  add_test(NAME v5_capi_python COMMAND ${Python3_EXECUTABLE} ${EDGEFHE_REQUIRE_PASS}
    --marker "V5_CAPI_PASS" -- ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/capi_test.py
    --library $<TARGET_FILE:edgefhe_v5_capi>)
  set_tests_properties(v5_unit v5_parity_simulated v5_e2e_sample v5_capi_python PROPERTIES
    LABELS "cpu-only;v5" ENVIRONMENT "PYTHONDONTWRITEBYTECODE=1;PYTHONUNBUFFERED=1")
endif()
