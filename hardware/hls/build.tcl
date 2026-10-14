# Portable wrapper only; frozen arithmetic sources are unchanged.
set root [file normalize [file join [file dirname [info script]] ../..]]
foreach key {EDGEFHE_HLS_MODE EDGEFHE_HLS_OUTPUT EDGEFHE_HLS_COSIM} {
    if {![info exists ::env($key)]} {error "Use python3 tools/build_hls.py; missing $key"}
}
set mode $::env(EDGEFHE_HLS_MODE)
set output [file normalize $::env(EDGEFHE_HLS_OUTPUT)]
set rtl $::env(EDGEFHE_HLS_COSIM)
if {$mode ni {public fixed}} {error "Expected public or fixed"}
if {$rtl ni {0 1}} {error "Invalid cosim flag"}
if {[file exists $output]} {error "Refusing existing output: $output"}
set src [file join $root hardware hls rns3_n4096 hls_source src]
set ::env(EDGEFHE_VECTOR_FILE) [file join $root validation vectors vectors.bin]
file mkdir $output
cd $output
open_project project
set_top rns3_ntt_parallel_accel_n4096
foreach f {rns_ntt_parallel_core.cpp rns_ntt_parallel_rns3.cpp} {
    add_files [file join $src $f] -cflags "-DRNS_LANES=4"
}
if {$mode eq "public"} {
    add_files -tb [file join $root hardware hls tb_openfhe_vectors.cpp] -cflags "-I$src -DRNS_LANES=4 -O2"
} else {
    add_files -tb [file join $root hardware hls rns3_n4096 tb_fixed_bank.cpp] -cflags "-I$src -DRNS_LANES=4 -O2"
}
open_solution solution1 -flow_target vivado
set_part {xck26-sfvc784-2LV-c}
create_clock -period 10 -name default
csim_design
csynth_design
if {$rtl} {cosim_design -rtl verilog -tool xsim -trace_level none}
export_design -format ip_catalog -rtl verilog -vendor edgefhe.local -library hls -version 1.1 -display_name "EdgeFHE II1 N4096 RNS3 Four-Lane Research Snapshot"
puts "LOCAL_HLS_EXPORT_COMPLETE_REPORT_REVIEW_REQUIRED"
exit
