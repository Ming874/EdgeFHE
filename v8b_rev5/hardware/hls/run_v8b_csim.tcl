# V8b C simulation (xck26-sfvc784-2LV-c, 5 ns). Usage: vitis_hls -f run_v8b_csim.tcl (from this directory)
open_project -reset v8b_csim_prj
set_top edgefhe_v8b
add_files v8b.cpp -cflags "-std=c++14"
add_files -tb v8b_tb.cpp -cflags "-std=c++14 -I.."
open_solution -reset sol200 -flow_target vivado
set_part xck26-sfvc784-2LV-c
create_clock -period 5 -name default
csim_design
exit
