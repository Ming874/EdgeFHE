set root [file normalize [file join [file dirname [info script]] ../..]]
if {$argc != 3} {error "Usage: build.tcl <new-project-directory> <ip-repository> <bd|implement>"}
set output [file normalize [lindex $argv 0]]
set iprepo [file normalize [lindex $argv 1]]
set mode [lindex $argv 2]
if {$mode ni {bd implement}} {error "Expected bd or implement"}
if {[file exists $output]} {error "Refusing existing project output"}
if {![file exists [file join $iprepo component.xml]]} {error "Missing extracted HLS component.xml"}
if {[version -short] ne "2022.1"} {error "Frozen BD requires Vivado 2022.1"}
create_project edgefhe_ii1 $output -part xck26-sfvc784-2LV-c
set_property board_part xilinx.com:kv260_som:part0:1.3 [current_project]
set_property ip_repo_paths [list $iprepo] [current_project]
update_ip_catalog
source [file join $root hardware vivado recreate_bd.tcl]
validate_bd_design
save_bd_design
set bd [get_files -quiet */edgefhe_ii1.bd]
if {[llength $bd] != 1} {error "Expected one BD"}
add_files -norecurse [make_wrapper -files $bd -top]
set_property top edgefhe_ii1_wrapper [current_fileset]
if {$mode eq "bd"} {
    puts "PORTABLE_BD_STRUCTURE_PASS_NOT_ROUTED_OR_BOARD_ACCEPTANCE"
    close_project
    exit
}
generate_target all $bd
set_param general.maxThreads 4
launch_runs synth_1 -jobs 4
wait_on_run synth_1
if {![string match "*Complete*" [get_property STATUS [get_runs synth_1]]]} {error "Synthesis incomplete"}
launch_runs impl_1 -to_step route_design -jobs 4
wait_on_run impl_1
if {![string match "*Complete*" [get_property STATUS [get_runs impl_1]]]} {error "Route incomplete"}
open_run impl_1
set reports [file join $output reports]
file mkdir $reports
report_utilization -file [file join $reports utilization_routed.rpt]
report_timing_summary -delay_type min_max -report_unconstrained -file [file join $reports timing_routed.rpt]
check_timing -verbose -file [file join $reports check_timing.rpt]
report_drc -file [file join $reports drc.rpt]
report_methodology -file [file join $reports methodology.rpt]
report_route_status -file [file join $reports route.rpt]
foreach kind {max min} {
    set paths [get_timing_paths -delay_type $kind -max_paths 1]
    if {[llength $paths]!=1 || [get_property SLACK $paths]<0} {error "Negative/missing $kind timing"}
}
set blockers [get_drc_violations -quiet -filter {SEVERITY == Error || SEVERITY == {Critical Warning}}]
if {[llength $blockers]} {error "DRC blocks export"}
write_checkpoint [file join $output routed.dcp]
write_bitstream [file join $output edgefhe-ii1-n4096-kv260.bit]
write_hw_platform -fixed -include_bit [file join $output edgefhe-ii1-n4096-kv260.xsa]
puts "PORTABLE_IMPLEMENTATION_EXPORTED_COMPLETE_REPORT_AND_BOARD_REVIEW_PENDING"
close_project
