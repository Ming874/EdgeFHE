cmake_minimum_required(VERSION 3.20)
project(EdgeFHE_native_candidate LANGUAGES CXX)
if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "Native PS candidate currently supports Linux GCC/Clang (x86_64 or aarch64)")
endif()
find_package(Threads REQUIRED)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_library(edgefhe_native SHARED core.cpp c_api.cpp)
target_link_libraries(edgefhe_native PRIVATE Threads::Threads)
target_compile_options(edgefhe_native PRIVATE -Wall -Wextra -Werror -fno-fast-math)
add_executable(edgefhe_native_demo interactive.cpp core.cpp)
target_link_libraries(edgefhe_native_demo PRIVATE Threads::Threads)
target_compile_options(edgefhe_native_demo PRIVATE -Wall -Wextra -Werror -fno-fast-math)
add_executable(edgefhe_native_selftest selftest.cpp core.cpp)
target_link_libraries(edgefhe_native_selftest PRIVATE Threads::Threads)
target_compile_options(edgefhe_native_selftest PRIVATE -Wall -Wextra -Werror -fno-fast-math)
include(CTest)
if(BUILD_TESTING)
  find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
  add_test(NAME native_complete_math COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/../../tools/require_pass.py
    --marker NATIVE_CPP_SELFTEST_PASS -- $<TARGET_FILE:edgefhe_native_selftest>)
  add_test(NAME native_protocol_offline COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/../../tools/require_pass.py
    --marker V4_PROTOCOL_OFFLINE_NEGATIVE_PASS -- ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_protocol.py)
endif()
