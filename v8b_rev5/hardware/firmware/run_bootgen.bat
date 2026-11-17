@echo off
rem Convert the rev5 bitstream to .bit.bin for xmutil / fpga-manager. Put edgefhe-v8b-r5.bit next to this script first.
setlocal
cd /d %~dp0
if "%VIVADO_BIN%"=="" set "VIVADO_BIN=C:\Xilinx\Vivado\2022.1\bin"
call "%VIVADO_BIN%\bootgen.bat" -arch zynqmp -image edgefhe-v8b-r5.bif -process_bitstream bin -w on
exit /b %ERRORLEVEL%
