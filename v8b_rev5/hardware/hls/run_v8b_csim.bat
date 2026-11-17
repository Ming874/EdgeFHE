@echo off
cd /d %~dp0
call C:\Xilinx\Vitis_HLS\2022.1\bin\vitis_hls.bat -f run_v8b_csim.tcl
exit /b %ERRORLEVEL%
