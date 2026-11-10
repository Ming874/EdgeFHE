`timescale 1ns/1ps
// SPDX-License-Identifier: MIT
// V8b identity (make_v8b_identity.py) + clocking v3 frequency check (make_v8b_mmcm.py). Research candidate: reset control remains reachable while the datapath is reset.
// A reset request is NOT evidence that outstanding AXI/DMA accesses have stopped.
module edgefhe_supervisor (
 (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 S_AXI_ACLK CLK", X_INTERFACE_PARAMETER="ASSOCIATED_BUSIF S_AXI, ASSOCIATED_RESET S_AXI_ARESETN" *) input wire S_AXI_ACLK,
 (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 S_AXI_ARESETN RST", X_INTERFACE_PARAMETER="POLARITY ACTIVE_LOW" *) input wire S_AXI_ARESETN,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWADDR", X_INTERFACE_PARAMETER="PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 8" *) input wire [7:0] S_AXI_AWADDR,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWPROT" *) input wire [2:0] S_AXI_AWPROT,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *) input wire S_AXI_AWVALID,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *) output wire S_AXI_AWREADY,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WDATA" *) input wire [31:0] S_AXI_WDATA,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *) input wire [3:0] S_AXI_WSTRB,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WVALID" *) input wire S_AXI_WVALID,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WREADY" *) output wire S_AXI_WREADY,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BRESP" *) output reg [1:0] S_AXI_BRESP,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BVALID" *) output reg S_AXI_BVALID,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BREADY" *) input wire S_AXI_BREADY,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *) input wire [7:0] S_AXI_ARADDR,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARPROT" *) input wire [2:0] S_AXI_ARPROT,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *) input wire S_AXI_ARVALID,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *) output wire S_AXI_ARREADY,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RDATA" *) output reg [31:0] S_AXI_RDATA,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RRESP" *) output reg [1:0] S_AXI_RRESP,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RVALID" *) output reg S_AXI_RVALID,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RREADY" *) input wire S_AXI_RREADY,
 (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 ref_clk CLK", X_INTERFACE_PARAMETER="FREQ_HZ 99999001" *) input wire ref_clk,
 input wire mmcm_locked,
 (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 datapath_resetn RST", X_INTERFACE_PARAMETER="POLARITY ACTIVE_LOW" *) input wire datapath_resetn,
 (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 datapath_reset_req RST", X_INTERFACE_PARAMETER="POLARITY ACTIVE_HIGH" *) output wire datapath_reset_req
);
 reg have_aw,have_w,requested,previous_resetn,epoch_overflow;
 reg [7:0] address;
 reg [31:0] data,epoch;
 reg [3:0] strobes;
 reg [5:0] minimum_hold;
 // Frequency check: ref_clk window of 65536 cycles, counted in the S_AXI_ACLK (fabric) domain.
 reg [15:0] ref_count=0;
 reg ref_toggle=0;
 always @(posedge ref_clk) begin ref_count<=ref_count+1; if(ref_count==16'hffff) ref_toggle<=~ref_toggle; end
 (* ASYNC_REG="TRUE" *) reg [2:0] toggle_sync;
 (* ASYNC_REG="TRUE" *) reg [1:0] locked_sync;
 reg [31:0] fast_count,measured,windows;
 assign S_AXI_AWREADY=S_AXI_ARESETN&&!have_aw&&!S_AXI_BVALID;
 assign S_AXI_WREADY=S_AXI_ARESETN&&!have_w&&!S_AXI_BVALID;
 assign S_AXI_ARREADY=S_AXI_ARESETN&&!S_AXI_RVALID;
 assign datapath_reset_req=requested||(minimum_hold!=0);
 function [31:0] register_value;
 input [7:0] a;
 begin case(a)
  8'h00:register_value=32'h45464738; // EFG8 (V8b)
  8'h04:register_value=32'h00080003; // V8b, MMCM 200 MHz
  8'h08:register_value=32'h000003c0; // commands 6..9
  8'h0c:register_value=4096;
  8'h10:register_value=32'h00040003; // lanes,limbs
  8'h14:register_value=262192; // largest input frame (bytes)
  8'h18:register_value=786464; // largest output frame (bytes)
  8'h1c:register_value=200000000;
  8'h20:register_value=epoch;
  8'h24:register_value={epoch_overflow,29'b0,datapath_reset_req,datapath_resetn};
  8'h28:register_value=measured; // fabric clock cycles per 65536 ref_clk cycles
  8'h2c:register_value=windows;
  8'h30:register_value={31'b0,locked_sync[1]};
  // Active research profile SHA256, four low-to-high bytes per register.
  8'h80:register_value=32'h3a65eee5;
  8'h84:register_value=32'h3c7f40c4;
  8'h88:register_value=32'h8089769d;
  8'h8c:register_value=32'h85bb6f18;
  8'h90:register_value=32'h97a491ae;
  8'h94:register_value=32'hd8918fd5;
  8'h98:register_value=32'h35a93225;
  8'h9c:register_value=32'hc939f739;
  default:register_value=0;
 endcase end endfunction
 function valid_read;
 input [7:0] a;
 begin valid_read=(a<=8'h30&&a[1:0]==0)||(a>=8'h80&&a<=8'h9c&&a[1:0]==0);end
 endfunction
 always @(posedge S_AXI_ACLK)begin
  if(!S_AXI_ARESETN)begin
   have_aw<=0;have_w<=0;requested<=0;previous_resetn<=0;epoch_overflow<=0;
   minimum_hold<=0;epoch<=0;S_AXI_BVALID<=0;S_AXI_BRESP<=0;S_AXI_RVALID<=0;S_AXI_RRESP<=0;S_AXI_RDATA<=0;
   address<=0;data<=0;strobes<=0;
   toggle_sync<=0;locked_sync<=0;fast_count<=0;measured<=0;windows<=0;
  end else begin
   toggle_sync<={toggle_sync[1:0],ref_toggle};
   locked_sync<={locked_sync[0],mmcm_locked};
   if(toggle_sync[2]!=toggle_sync[1])begin measured<=fast_count+1;fast_count<=0;if(windows!=32'hffffffff)windows<=windows+1;end
   else fast_count<=fast_count+1;
   previous_resetn<=datapath_resetn;
   if(datapath_resetn&&!previous_resetn)begin
    if(epoch==32'hffffffff)epoch_overflow<=1;else epoch<=epoch+1;
   end
   if(minimum_hold!=0)minimum_hold<=minimum_hold-1;
   if(S_AXI_AWVALID&&S_AXI_AWREADY)begin address<=S_AXI_AWADDR;have_aw<=1;end
   if(S_AXI_WVALID&&S_AXI_WREADY)begin data<=S_AXI_WDATA;strobes<=S_AXI_WSTRB;have_w<=1;end
   if(have_aw&&have_w&&!S_AXI_BVALID)begin
    have_aw<=0;have_w<=0;S_AXI_BVALID<=1;S_AXI_BRESP<=2;
    if(address==8'h40&&strobes==4'hf&&(data==32'he7f70001||data==32'he7f70000))begin
     requested<=data[0];S_AXI_BRESP<=0;if(data[0])minimum_hold<=32;
    end
   end
   if(S_AXI_BVALID&&S_AXI_BREADY)S_AXI_BVALID<=0;
   if(S_AXI_ARVALID&&S_AXI_ARREADY)begin
    S_AXI_RVALID<=1;S_AXI_RDATA<=register_value(S_AXI_ARADDR);S_AXI_RRESP<=valid_read(S_AXI_ARADDR)?0:2;
   end
   if(S_AXI_RVALID&&S_AXI_RREADY)S_AXI_RVALID<=0;
  end
 end
endmodule
