# V8b RTL co-simulation on a short subset (V8B_SHORT). Usage: vitis_hls -f run_v8b_cosim.tcl (from this directory)
open_project -reset v8b_cosim_prj
set_top edgefhe_v8b
add_files v8b.cpp -cflags "-std=c++14"
add_files -tb v8b_tb.cpp -cflags "-std=c++14 -I.. -DV8B_SHORT"
open_solution -reset sol200 -flow_target vivado
set_part xck26-sfvc784-2LV-c
create_clock -period 5 -name default
csim_design
csynth_design
cosim_design -rtl verilog -trace_level none
exit
