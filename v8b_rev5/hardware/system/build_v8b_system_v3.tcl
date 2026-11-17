# Isolated system build. No hardware-server connections or board programming.
if {$argc != 3 || [version -short] ne "2022.1"} {error "STOP_EXPECT_2022_1 output iprepo source_root"}
set output [file normalize [lindex $argv 0]]
set iprepo [file normalize [lindex $argv 1]]
set sources [file normalize [lindex $argv 2]]
if {[file exists $output]} {error "STOP_OUTPUT_EXISTS"}
create_project edgefhe_v8b_system $output -part xck26-sfvc784-2LV-c
set_property board_part xilinx.com:kv260_som:part0:1.3 [current_project]
set_property ip_repo_paths [list $iprepo] [current_project]
add_files -norecurse [file join $sources edgefhe_supervisor.v]
update_compile_order -fileset sources_1
update_ip_catalog
source [file join $sources recreate_bd_v8b.tcl]
validate_bd_design
save_bd_design
set bd [get_files -quiet */edgefhe_v8b.bd]
if {[llength $bd]!=1} {error "STOP_EXPECT_ONE_BD"}
file copy $bd [file join $output candidate.bd]
foreach {cell property expected} {
 ps PSU__MAXIGP0__DATA_WIDTH 128
 ps PSU__SAXIGP2__DATA_WIDTH 128
 ps PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ 100
 clk200 PRIMITIVE MMCM
 clk200 CLKOUT1_REQUESTED_OUT_FREQ 200.000
 clk200 RESET_TYPE ACTIVE_LOW
 clk200 USE_LOCKED true
 dma c_mm2s_burst_size 4
 dma c_s2mm_burst_size 16
 dma c_addr_width 32
 dma c_include_sg 0
 dma c_include_mm2s_dre 0
 dma c_include_s2mm_dre 0
 dma c_m_axi_mm2s_data_width 128
 dma c_m_axi_s2mm_data_width 128
 dma c_m_axis_mm2s_tdata_width 128
 dma c_s_axis_s2mm_tdata_width 128
 rst_ps_99M C_EXT_RESET_HIGH 0
 rst_data C_EXT_RESET_HIGH 0
 rst_data C_AUX_RESET_HIGH 1
} {
 set got [get_property CONFIG.$property [get_bd_cells $cell]]
 if {$got!=$expected} {error "STOP_PROPERTY $cell/$property=$got expected=$expected"}
}
if {[get_property VLNV [get_bd_cells ntt]] ne "edgefhe.local:hls:edgefhe_v8b:8.0"} {error "STOP_V8B_IP_ID"}
# Candidate-only reset checks before synthesis. No board connection.
if {[get_property CONFIG.C_AUX_RESET_HIGH [get_bd_cells rst_ps_99M]] != 0} {error "STOP_CONTROL_AUX_POLARITY"}
if {[get_property CONFIG.CONST_VAL [get_bd_cells one]] != 1} {error "STOP_ONE_CONSTANT"}
set auxnet [get_bd_nets -quiet -of_objects [get_bd_pins rst_ps_99M/aux_reset_in]]
set onenet [get_bd_nets -quiet -of_objects [get_bd_pins one/dout]]
if {[llength $auxnet] != 1 || $auxnet ne $onenet} {error "STOP_AUX_NOT_INACTIVE"}
set locknet [get_bd_nets -quiet -of_objects [get_bd_pins clk200/locked]]
foreach pin {rst_ps_99M/dcm_locked rst_data/dcm_locked supervisor/mmcm_locked} {
 if {[get_bd_nets -quiet -of_objects [get_bd_pins $pin]] ne $locknet} {error "STOP_DCM_LOCKED_$pin"}
}
set fabric [get_bd_nets -quiet -of_objects [get_bd_pins clk200/clk_out1]]
foreach pin {ntt/ap_clk dma/s_axi_lite_aclk dma/m_axi_mm2s_aclk dma/m_axi_s2mm_aclk ps/maxihpm0_fpd_aclk ps/saxihp0_fpd_aclk supervisor/S_AXI_ACLK rst_ps_99M/slowest_sync_clk rst_data/slowest_sync_clk} {
 if {[get_bd_nets -quiet -of_objects [get_bd_pins $pin]] ne $fabric} {error "STOP_FABRIC_CLOCK_$pin"}
}
puts "V8B_MMCM_CLOCK_AND_LOCKED_RESET_INVARIANTS_PASS_NOT_BOARD"
puts "V8B_BD_AND_AUXFIX_RESET_INVARIANTS_PASS_NOT_BOARD"
puts "V8B_SYSTEM_BD_VALIDATED"
add_files -norecurse [make_wrapper -files $bd -top]
set_property top edgefhe_v8b_wrapper [current_fileset]
generate_target all $bd
set_param general.maxThreads 4
launch_runs synth_1 -jobs 4
wait_on_run synth_1
if {![string match "*Complete*" [get_property STATUS [get_runs synth_1]]]} {error "STOP_SYNTHESIS_INCOMPLETE"}
launch_runs impl_1 -to_step route_design -jobs 4
wait_on_run impl_1
if {![string match "*Complete*" [get_property STATUS [get_runs impl_1]]]} {error "STOP_ROUTE_INCOMPLETE"}
open_run impl_1
set reports [file join $output reports];file mkdir $reports
report_utilization -file [file join $reports utilization.rpt]
report_utilization -hierarchical -file [file join $reports hierarchical.rpt]
report_timing_summary -delay_type min_max -report_unconstrained -file [file join $reports timing.rpt]
check_timing -verbose -file [file join $reports check_timing.rpt]
report_drc -file [file join $reports drc.rpt]
report_methodology -file [file join $reports methodology.rpt]
report_route_status -file [file join $reports route.rpt]
report_cdc -file [file join $reports cdc.rpt]
write_checkpoint [file join $output routed.dcp]
set result [open [file join $output routed_gate.txt] w]
set fail 0
foreach kind {max min} {
 set paths [get_timing_paths -delay_type $kind -max_paths 1]
 if {[llength $paths]!=1} {puts $result "$kind=MISSING";set fail 1} else {
  set slack [get_property SLACK $paths];puts $result "$kind=$slack";if {$slack<0} {set fail 1}
 }
}
set blockers [get_drc_violations -quiet -filter {SEVERITY == Error || SEVERITY == {Critical Warning}}]
puts $result "drc_blockers=[llength $blockers]";if {[llength $blockers]} {set fail 1};close $result
if {$fail} {error "V8B_ROUTE_GATE_FAILED_PRESERVE_REPORTS"}
write_bitstream [file join $output edgefhe-v8b.bit]
puts "V8B_ROUTED_BITSTREAM_WRITTEN_NOT_BOARD"
puts "V8B_SYSTEM_ROUTE_REPORTS_READY_NOT_BOARD_ACCEPTANCE"
close_project
exit
