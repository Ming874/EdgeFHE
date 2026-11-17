# V8b: C synthesis + IP catalog export for Vivado (vendor edgefhe.local, library hls, version 8.0).
# Usage: vitis_hls -f run_v8b_export.tcl (from this directory). Output: v8b_ip_prj/sol200/impl/ip
open_project -reset v8b_ip_prj
set_top edgefhe_v8b
add_files v8b.cpp -cflags "-std=c++14"
open_solution -reset sol200 -flow_target vivado
set_part xck26-sfvc784-2LV-c
create_clock -period 5 -name default
csynth_design
export_design -rtl verilog -format ip_catalog -vendor "edgefhe.local" -library "hls" -version "8.0" -display_name "EdgeFHE V8b ENC/PAIR"
puts "V8B_IP_EXPORT_DONE"
exit
